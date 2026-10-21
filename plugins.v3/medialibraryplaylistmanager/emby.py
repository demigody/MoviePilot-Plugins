"""Bounded Media server REST adapter with strict, complete pagination.

API contracts: https://dev.emby.media/reference/RestAPI/index.html
Movie and Series are the only accepted member types. For playlist playback,
Series expand into episodes grouped by season; episode identities stay inside
the adapter and are never persisted as canonical list members.
"""
from __future__ import annotations

import inspect
import json
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, urlopen

from .models import media_type, positive_id, text
from .messages import diagnostic


class EmbyAPIError(RuntimeError):
    def __init__(self, message: str, *, code: str = "server_error", status: int | None = None) -> None:
        super().__init__(diagnostic(message, fallback=True))
        self.code = code
        self.status = status


Transport = Callable[..., Any]


def _value(data: Any, *keys: str, default: Any = None) -> Any:
    if not isinstance(data, dict):
        return default
    normalized = {str(key).casefold(): value for key, value in data.items()}
    return next((normalized[key.casefold()] for key in keys if key.casefold() in normalized), default)


def tmdb_id(item: dict[str, Any]) -> int | None:
    providers = _value(item, "ProviderIds", "provider_ids", default={})
    if not isinstance(providers, dict):
        raise EmbyAPIError("媒体库返回的外部编号格式无效")
    raw = _value(providers, "Tmdb", "TmdbId")
    if raw is None:
        return None
    try:
        return positive_id(raw)
    except ValueError as error:
        raise EmbyAPIError("媒体库返回的 TMDB 编号无效") from error


@dataclass
class EmbyItem:
    id: str
    name: str = ""
    year: int | None = None
    poster: str | None = None
    tmdb_id: int | None = None
    playlist_item_id: str | None = None
    media_type: str | None = "movie"

    @property
    def key(self) -> str | None:
        return f"{self.media_type}:{self.tmdb_id}" if self.media_type in {"movie", "tv"} and self.tmdb_id else None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "EmbyItem":
        if not isinstance(data, dict) or not text(_value(data, "Id")):
            raise EmbyAPIError("媒体库返回的条目缺少 ID")
        kind = text(_value(data, "Type", "media_type")).casefold()
        if kind not in {"movie", "series", "tv", "boxset", "playlist"}:
            raise EmbyAPIError(f"媒体库返回了不支持的条目类型（{text(_value(data, 'Type', 'media_type')) or '未知'}），这里只接受电影或整部电视剧。", code="unsupported")
        kind = "tv" if kind in {"series", "tv"} else "movie" if kind == "movie" else None
        year = _value(data, "ProductionYear", "year")
        if year is not None:
            try:
                year = int(year)
            except (TypeError, ValueError) as error:
                raise EmbyAPIError("媒体库返回的年份无效") from error
        tags = _value(data, "ImageTags", default={})
        if not isinstance(tags, dict):
            raise EmbyAPIError("媒体库返回的图片信息无效")
        return cls(text(_value(data, "Id")), text(_value(data, "Name")), year, text(_value(tags, "Primary")) or None, tmdb_id(data), text(_value(data, "PlaylistItemId", "playlist_item_id")) or None, kind)


@dataclass
class EmbySeason(EmbyItem):
    """Remote collection identity; the selected work remains its parent Series."""
    series_id: str = ""
    series_tmdb_id: int | None = None
    season_number: int = 0
    series_name: str = ""

    @property
    def parent_key(self) -> str | None:
        return f"tv:{self.series_tmdb_id}" if self.series_tmdb_id else None

    @property
    def key(self) -> str | None:
        return f"{self.parent_key}:season:{self.season_number}" if self.parent_key else None

    @classmethod
    def from_dict(cls, data: dict[str, Any], series: EmbyItem) -> "EmbySeason":
        number = _value(data, "IndexNumber")
        if (text(_value(data, "Type")).casefold() != "season" or text(_value(data, "SeriesId", "ParentId")) != series.id
                or isinstance(number, bool) or not isinstance(number, int) or number < 0):
            raise EmbyAPIError("媒体库 季信息缺少有效的所属电视剧或季号")
        base = EmbyItem.from_dict({**data, "Type": "Movie"})
        return cls(**{**vars(base), "media_type": "season"}, series_id=series.id,
                   series_tmdb_id=series.tmdb_id, season_number=number, series_name=series.name)


@dataclass
class EmbyEpisode(EmbyItem):
    """Playback-only identity. Never accepted as a canonical MediaMember."""
    series_id: str = ""
    series_tmdb_id: int | None = None
    season_number: int = 0
    episode_number: int = 0
    episode_number_end: int | None = None

    @property
    def episode_numbers(self) -> range:
        return range(self.episode_number, (self.episode_number_end if self.episode_number_end is not None else self.episode_number) + 1)

    @property
    def parent_key(self) -> str | None:
        return f"tv:{self.series_tmdb_id}" if self.series_tmdb_id else None

    @property
    def group_key(self) -> str | None:
        return f"{self.parent_key}:season:{self.season_number}" if self.parent_key else None

    @property
    def key(self) -> str | None:
        return f"{self.group_key}:episode:{self.episode_number}" if self.group_key else None

    @classmethod
    def from_dict(cls, data: dict[str, Any], series: EmbyItem) -> "EmbyEpisode":
        if text(_value(data, "Type")).casefold() != "episode" or text(_value(data, "SeriesId")) != series.id:
            raise EmbyAPIError("媒体库返回的剧集与所属电视剧不一致")
        numbers = []
        for field in ("ParentIndexNumber", "IndexNumber"):
            number = _value(data, field)
            if isinstance(number, bool) or not isinstance(number, int) or number < 0:
                raise EmbyAPIError("剧集缺少有效的季号或集号，无法保证播放顺序")
            numbers.append(number)
        if _value(data, "IsPlaceHolder", default=False) or _value(data, "IsMissing", default=False):
            raise EmbyAPIError("媒体库返回了尚未入库的占位剧集")
        base = EmbyItem.from_dict({**data, "Type": "Movie"})
        end = _value(data, 'IndexNumberEnd')
        if end is not None and (isinstance(end, bool) or not isinstance(end, int) or end < numbers[1] or end - numbers[1] > 1000):
            raise EmbyAPIError("剧集的结束集号无效")
        return cls(**{**vars(base), "media_type": "episode"}, series_id=series.id, series_tmdb_id=series.tmdb_id,
                   season_number=numbers[0], episode_number=numbers[1], episode_number_end=end)


class EmbyClient:
    kind = "emby"
    capabilities = {"supported": True, "kinds": ["emby"], "media_types": ["movie", "tv"], "collections": True, "playlists": True, "reorder": True, "movie_playlist": True, "tv_playlist": True, "playlist_media_types": ["movie", "tv", "episode"], "playlist_grouping": "member"}

    def ensure_playlist_access(self) -> None:
        """Emby accepts its configured API key plus user ID for playlist writes."""

    def check_playlist_access(self) -> None:
        self.ensure_playlist_access()

    def __init__(self, base_url: str, api_key: str, user_id: str, transport: Transport | Any | None = None, timeout: float = 10, *, page_size: int = 200, scan_timeout: float = 90, max_pages: int = 1000) -> None:
        url = text(base_url).rstrip("/")
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("媒体服务器地址必须以 http 或 https 开头，且不能包含账号密码或查询参数")
        self.base_url = url + "/"
        self._has_emby_prefix = parsed.path.casefold().endswith("/emby")
        self.api_key = text(api_key)
        self.user_id = text(user_id)
        self.transport = transport
        self.timeout = min(max(float(timeout), 0.1), 30)
        self.page_size = min(max(int(page_size), 1), 1000)
        self.scan_timeout = min(max(float(scan_timeout), 0.1), 300)
        self.max_pages = min(max(int(max_pages), 1), 5000)

    def _path(self, path: str) -> str:
        return path.lstrip("/") if self._has_emby_prefix else "emby/" + path.lstrip("/")

    @staticmethod
    def _id(value: Any) -> str:
        value = text(value)
        if not value:
            raise ValueError("缺少媒体库对象 ID")
        return quote(value, safe="")

    def _headers(self) -> dict[str, str]:
        return {"Accept": "application/json", "Content-Type": "application/json", "X-Emby-Token": self.api_key}

    def _request(self, method: str, path: str, params: dict[str, Any] | None = None, body: Any = None, *, timeout: float | None = None) -> Any:
        params = {key: value for key, value in (params or {}).items() if value is not None}
        endpoint = self._path(path)
        timeout = min(self.timeout, timeout) if timeout is not None else self.timeout
        if timeout <= 0:
            raise EmbyAPIError("媒体库请求超时，请稍后重试", code="timeout")
        headers = self._headers()
        try:
            if self.transport is not None:
                operation = self.transport.request if hasattr(self.transport, "request") else self.transport
                kwargs = {"params": params, "json": body, "headers": headers, "timeout": timeout}
                # Choose compatible arguments BEFORE invoking. Retrying on
                # TypeError could execute a POST twice after a transport bug.
                signature = inspect.signature(operation)
                if not any(p.kind == inspect.Parameter.VAR_KEYWORD for p in signature.parameters.values()):
                    kwargs = {key: value for key, value in kwargs.items() if key in signature.parameters}
                signature.bind(method, endpoint, **kwargs)
                return self._decode_response(operation(method, endpoint, **kwargs))
            url = self.base_url + endpoint
            if params:
                url += "?" + urlencode(params, doseq=True)
            payload = None if body is None else json.dumps(body).encode("utf-8")
            request = Request(url, data=payload, headers=headers, method=method.upper())
            with urlopen(request, timeout=timeout) as response:
                return self._decode_response(response.read())
        except EmbyAPIError:
            raise
        except HTTPError as error:
            raise EmbyAPIError(f"媒体服务器返回 HTTP {error.code}，请检查服务器状态和访问权限", status=error.code) from error
        except (TimeoutError, URLError) as error:
            timed_out = isinstance(error, TimeoutError) or isinstance(getattr(error, "reason", None), TimeoutError)
            raise EmbyAPIError("媒体库请求超时，请稍后重试" if timed_out else "无法连接媒体服务器，请检查地址和网络", code="timeout" if timed_out else "server_error") from error
        except Exception as error:
            # Transport exceptions may include the credential-bearing URL.
            raise EmbyAPIError("媒体库请求失败，请检查连接") from error

    @staticmethod
    def _decode_response(response: Any) -> Any:
        if isinstance(response, tuple) and len(response) == 2:
            status, response = response
            if not 200 <= int(status) < 300:
                raise EmbyAPIError(f"媒体服务器返回 HTTP {int(status)}，请检查服务器状态和访问权限", status=int(status))
        status = getattr(response, "status_code", None)
        if status is not None and not 200 <= int(status) < 300:
            raise EmbyAPIError(f"媒体服务器返回 HTTP {int(status)}，请检查服务器状态和访问权限", status=int(status))
        if hasattr(response, "content") and not response.content:
            return {}
        if hasattr(response, "json") and callable(response.json):
            response = response.json()
        if isinstance(response, bytes):
            response = response.decode("utf-8")
        if isinstance(response, str):
            try:
                response = json.loads(response) if response else {}
            except json.JSONDecodeError as error:
                raise EmbyAPIError("媒体库返回的数据无法解析") from error
        return response if response is not None else {}

    def _paged_raw(self, path: str, params: dict[str, Any], *, deadline: float | None = None) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        total: int | None = None
        deadline = min(deadline, time.monotonic() + self.scan_timeout) if deadline is not None else time.monotonic() + self.scan_timeout
        for _ in range(self.max_pages):
            raw = self._request("GET", path, {**params, "StartIndex": len(result), "Limit": self.page_size, "EnableTotalRecordCount": "true"}, timeout=deadline - time.monotonic())
            if not isinstance(raw, dict) or not isinstance(raw.get("Items"), list):
                raise EmbyAPIError("媒体库返回的分页数据格式无效")
            count = raw.get("TotalRecordCount")
            if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                raise EmbyAPIError("媒体库未返回有效的条目总数")
            if total is not None and count != total:
                raise EmbyAPIError("读取期间媒体库内容发生变化，请重新同步")
            total = count
            items = raw["Items"]
            if len(items) > self.page_size or len(result) + len(items) > total:
                raise EmbyAPIError("媒体库返回的分页信息不一致")
            for value in items:
                if not isinstance(value, dict):
                    raise EmbyAPIError("媒体库返回的条目数据无效")
                result.append(value)
            if len(result) == total:
                return result
            if not items:
                raise EmbyAPIError("媒体库返回的分页内容不完整")
        raise EmbyAPIError("媒体库内容超过读取上限，本次未作修改", code="timeout")

    def _paged(self, path: str, params: dict[str, Any], *, playlist: bool = False, expected_type: str | None = None) -> list[EmbyItem]:
        result = []
        seen: set[str] = set()
        for raw in self._paged_raw(path, params):
            if expected_type and text(_value(raw, "Type")).casefold() != expected_type.casefold():
                raise EmbyAPIError("媒体库返回的输出列表类型与请求不一致。", code="unsupported")
            item = EmbyItem.from_dict(raw)
            identity = item.playlist_item_id if playlist else item.id
            if not identity or identity in seen:
                raise EmbyAPIError("媒体库条目缺少唯一编号，或编号重复")
            seen.add(identity)
            result.append(item)
        return result

    def list_media(self, library_ids: Iterable[str] | None = None, media_type: str = "all") -> list[EmbyItem]:
        return self._media_query(library_ids, media_type)

    def _media_query(self, library_ids: Iterable[str] | None, requested: str, query: str = "") -> list[EmbyItem]:
        if requested in {None, "all"}:
            kinds = {"movie", "tv"}
            include = "Movie,Series"
        else:
            kind = media_type(requested)
            kinds = {kind}
            include = "Series" if kind == "tv" else "Movie"
        parents = [library_ids] if isinstance(library_ids, str) else list(library_ids or [])
        result: dict[str, EmbyItem] = {}
        for parent in parents or [None]:
            items = self._paged(f"Users/{self._id(self.user_id)}/Items", {"IncludeItemTypes": include, "Recursive": "true", "GroupItemsIntoCollections": "false", "ParentId": parent, "Fields": "ProviderIds", "SearchTerm": query or None})
            for item in items:
                if item.media_type not in kinds:
                    raise EmbyAPIError("媒体库返回的内容不是请求的电影或电视剧")
                if item.id in result and result[item.id].key != item.key:
                    raise EmbyAPIError("媒体库返回的媒体身份存在冲突")
                result.setdefault(item.id, item)
        return list(result.values())

    def list_movies(self, library_ids: Iterable[str] | None = None, media_type: str = "movie") -> list[EmbyItem]:
        return self.list_media(library_ids, media_type)

    def search_movies(self, query: str = "", limit: int = 50, library_ids: Iterable[str] | None = None, media_type: str = "movie") -> list[EmbyItem]:
        return self._media_query(library_ids, media_type, query)[:min(max(int(limit), 1), 1000)]

    def _validate_output(self, output_id: str, expected: str) -> None:
        """先验证输出，避免媒体库将失效 ParentId 的查询退回媒体库根目录。"""
        label = "合集" if expected == "BoxSet" else "播放列表"
        try:
            raw = self._request("GET", f"Users/{self._id(self.user_id)}/Items/{self._id(output_id)}")
        except EmbyAPIError as error:
            if error.status == 404:
                raise EmbyAPIError(f"绑定的媒体库{label}已不存在（ID：{output_id}），可在正式发布时重新建立。",
                    code="missing_output", status=404) from error
            raise
        if not isinstance(raw, dict) or text(_value(raw, "Id")) != text(output_id):
            raise EmbyAPIError(f"媒体库返回的{label}身份与绑定不一致。")
        actual = text(_value(raw, "Type"))
        if actual.casefold() != expected.casefold():
            raise EmbyAPIError(f"绑定的媒体库{label}（ID：{output_id}）实际类型为 {actual or '未知'}。", code="unsupported")

    def get_collection_items(self, collection_id: str) -> list[EmbyItem]:
        self._validate_output(collection_id, "BoxSet")
        raw = self._paged_raw(f"Users/{self._id(self.user_id)}/Items", {"ParentId": collection_id, "IncludeItemTypes": "Movie,Series,Season", "Recursive": "false", "GroupItemsIntoCollections": "false", "Fields": "ProviderIds"})
        series = {}
        result = []
        for value in raw:
            if text(_value(value, "Type")).casefold() == "season":
                parent = text(_value(value, "SeriesId", "ParentId"))
                if not parent:
                    raise EmbyAPIError("合集中的季缺少所属电视剧")
                if parent not in series:
                    item = EmbyItem.from_dict(self._request("GET", f"Users/{self._id(self.user_id)}/Items/{self._id(parent)}"))
                    if item.id != parent or item.media_type != "tv":
                        raise EmbyAPIError("季所属电视剧身份不一致")
                    series[parent] = item
                result.append(EmbySeason.from_dict(value, series[parent]))
            else:
                result.append(EmbyItem.from_dict(value))
        return result

    def get_series_seasons(self, series: EmbyItem) -> list[EmbyItem]:
        if series.media_type != "tv" or not series.tmdb_id:
            raise EmbyAPIError("电视剧身份无效，无法读取季")
        raw = self._paged_raw(f"Shows/{self._id(series.id)}/Seasons", {"UserId": self.user_id, "Fields": "ProviderIds", "IsMissing": "false"})
        result = [EmbySeason.from_dict(value, series) for value in raw]
        if len({item.id for item in result}) != len(result):
            raise EmbyAPIError("季分页包含重复条目")
        unique = {}
        for item in sorted(result, key=lambda item: (item.season_number, item.id)):
            unique.setdefault(item.key, item)
        return list(unique.values())

    def get_playlist_items(self, playlist_id: str) -> list[EmbyItem]:
        self._validate_output(playlist_id, "Playlist")
        raw = self._paged_raw(f"Playlists/{self._id(playlist_id)}/Items", {"UserId": self.user_id, "Fields": "ProviderIds"})
        series = {}
        result, seen = [], set()
        for value in raw:
            if text(_value(value, "Type")).casefold() == "episode":
                parent = text(_value(value, "SeriesId"))
                if not parent:
                    raise EmbyAPIError("播放列表剧集缺少所属电视剧")
                if parent not in series:
                    item = EmbyItem.from_dict(self._request("GET", f"Users/{self._id(self.user_id)}/Items/{self._id(parent)}"))
                    if item.id != parent or item.media_type != "tv":
                        raise EmbyAPIError("剧集所属电视剧身份不一致")
                    series[parent] = item
                item = EmbyEpisode.from_dict(value, series[parent])
            else:
                item = EmbyItem.from_dict(value)
            if not item.playlist_item_id or item.playlist_item_id in seen:
                raise EmbyAPIError("媒体库条目缺少唯一编号，或编号重复")
            seen.add(item.playlist_item_id)
            result.append(item)
        return result

    def get_series_episodes(self, series: EmbyItem) -> list[EmbyItem]:
        if series.media_type != "tv" or not series.tmdb_id:
            raise EmbyAPIError("电视剧身份无效，无法读取季和集")
        raw = self._paged_raw(f"Shows/{self._id(series.id)}/Episodes", {"UserId": self.user_id, "Fields": "ProviderIds", "IsPlaceHolder": "false"})
        result = [EmbyEpisode.from_dict(value, series) for value in raw]
        if len({item.id for item in result}) != len(result):
            raise EmbyAPIError("剧集分页包含重复条目")
        # Multiple versions use one deterministic entry per season/episode.
        unique = {}
        for item in sorted(result, key=lambda item: (item.season_number, item.episode_number, item.id)):
            unique.setdefault(item.key, item)
        return list(unique.values())

    def list_collections(self) -> list[EmbyItem]:
        return self._paged(f"Users/{self._id(self.user_id)}/Items", {"IncludeItemTypes": "BoxSet", "Recursive": "true"}, expected_type="BoxSet")

    def list_playlists(self) -> list[EmbyItem]:
        return self._paged(f"Users/{self._id(self.user_id)}/Items", {"IncludeItemTypes": "Playlist", "Recursive": "true"}, expected_type="Playlist")

    def list_libraries(self) -> list[dict[str, Any]]:
        raw = self._paged_raw("Library/VirtualFolders/Query", {})
        result = []
        seen = set()
        for item in raw:
            identity = text(_value(item, "ItemId", "Id"))
            if not identity or identity in seen:
                raise EmbyAPIError("媒体库目录缺少 ID")
            seen.add(identity)
            kind = text(_value(item, "CollectionType")).casefold()
            if kind in {"", "movies", "tvshows", "mixed"}:
                result.append({"id": text(_value(item, "ItemId", "Id")), "name": text(_value(item, "Name")), "collection_type": kind})
        return result

    def resolve_user_id(self, username: str) -> str | None:
        raw = self._request("GET", "Users")
        if not isinstance(raw, list) or any(not isinstance(item, dict) for item in raw):
            raise EmbyAPIError("媒体库返回的用户列表无效")
        matches = [text(_value(item, "Id")) for item in raw if text(_value(item, "Name")).casefold() == text(username).casefold()]
        if len(matches) > 1:
            raise EmbyAPIError("媒体库存在多个同名用户，请检查用户配置")
        return matches[0] if matches else None

    @staticmethod
    def _ids(values: Iterable[str]) -> list[str]:
        return list(dict.fromkeys(text(value) for value in values if text(value)))

    def create_collection(self, name: str, item_ids: Iterable[str] = ()) -> str:
        raw = self._request("POST", "Collections", {"Name": name, "Ids": ",".join(self._ids(item_ids)) or None})
        remote = text(_value(raw, "Id"))
        if not remote:
            raise EmbyAPIError("媒体库未返回新建合集的 ID")
        return remote

    def add_collection_items(self, collection_id: str, item_ids: Iterable[str]) -> None:
        values = self._ids(item_ids)
        if values:
            self._request("POST", f"Collections/{self._id(collection_id)}/Items", {"Ids": ",".join(values)})

    def remove_collection_items(self, collection_id: str, item_ids: Iterable[str]) -> None:
        values = self._ids(item_ids)
        if values:
            self._request("DELETE", f"Collections/{self._id(collection_id)}/Items", {"Ids": ",".join(values)})

    def create_playlist(self, name: str, item_ids: Iterable[str] = ()) -> str:
        raw = self._request("POST", "Playlists", {"Name": name, "UserId": self.user_id, "MediaType": "Video", "Ids": ",".join(self._ids(item_ids)) or None})
        remote = text(_value(raw, "Id"))
        if not remote:
            raise EmbyAPIError("媒体库未返回新建播放列表的 ID")
        return remote

    def add_playlist_items(self, playlist_id: str, item_ids: Iterable[str]) -> None:
        values = self._ids(item_ids)
        if values:
            self._request("POST", f"Playlists/{self._id(playlist_id)}/Items", {"UserId": self.user_id, "Ids": ",".join(values)})

    def remove_playlist_entries(self, playlist_id: str, entry_ids: Iterable[str]) -> None:
        values = self._ids(entry_ids)
        if values:
            self._request("DELETE", f"Playlists/{self._id(playlist_id)}/Items", {"UserId": self.user_id, "EntryIds": ",".join(values)})

    def move_playlist_entry(self, playlist_id: str, entry_id: str, new_index: int) -> None:
        if int(new_index) < 0:
            raise ValueError("播放位置不能为负数")
        self._request("POST", f"Playlists/{self._id(playlist_id)}/Items/{self._id(entry_id)}/Move/{int(new_index)}", {"UserId": self.user_id})
