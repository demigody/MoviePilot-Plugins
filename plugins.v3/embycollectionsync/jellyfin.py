"""Jellyfin REST adapter (official 10.11 and 12.2 controller contracts).

The transport, bounded pagination and canonical identities are shared with
EmbyClient. Jellyfin's root routes, virtual-folder response, item query options
and playlist creation payload are deliberately separate. Playlist ordering uses
the existing playlist and entry IDs; it never replaces or recreates a playlist.

Contracts: https://github.com/jellyfin/jellyfin/tree/v12.2/Jellyfin.Api/Controllers
Playlist moves require a user access token: the controller authorizes the
authenticated owner/editor, and does not accept a UserId query override.
"""
from __future__ import annotations

from typing import Any, Iterable
import threading
import time
from urllib.parse import quote
from uuid import UUID

from .emby import EmbyAPIError, EmbyClient, EmbyEpisode, EmbyItem, EmbySeason, _value
from .models import media_type, text


class JellyfinAuthError(EmbyAPIError):
    """Only fixed, credential-free authorization diagnostics may cross the API."""


def same_user(left: str, right: str) -> bool:
    return bool(left and right) and left.replace("-", "").casefold() == right.replace("-", "").casefold()


class JellyfinClient(EmbyClient):
    kind = "jellyfin"
    capabilities = {**EmbyClient.capabilities, "kinds": ["jellyfin"]}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.token_provider = None
        self.authorization_check = None
        self._playlist_user = None
        self._playlist_lock = threading.RLock()
        self.device_id = None

    def _headers(self) -> dict[str, str]:
        # Since Jellyfin 12, legacy X-Emby-Token authorization is disabled by
        # default. URL-encode header values as Jellyfin's parser decodes them.
        auth = f'MediaBrowser Token="{quote(self.api_key, safe="")}"'
        if self.device_id:
            auth += f', Client="MoviePilot ListManager", Device="MoviePilot", DeviceId="{quote(self.device_id, safe="")}", Version="2.0.1"'
        return {"Accept": "application/json", "Content-Type": "application/json", "Authorization": auth}

    def with_token(self, token: str) -> "JellyfinClient":
        return JellyfinClient(self.base_url, token, self.user_id, self.transport, self.timeout,
            page_size=self.page_size, scan_timeout=self.scan_timeout, max_pages=self.max_pages)

    def _path(self, path: str) -> str:
        # base_url may already contain Jellyfin's configured reverse-proxy path.
        return path.lstrip("/")

    def _request(self, method: str, path: str, params: dict[str, Any] | None = None,
                 body: Any = None, *, timeout: float | None = None) -> Any:
        params = dict(params or {})
        parts = path.strip("/").split("/")
        # The shared readers use the old user-scoped item routes. Jellyfin's
        # supported routes take this scope as an explicit query parameter.
        if method.upper() == "GET" and len(parts) in {3, 4} and parts[0] == "Users" and parts[2] == "Items":
            params["UserId"] = parts[1]
            path = "Items" + ("/" + parts[3] if len(parts) == 4 else "")
        if "GroupItemsIntoCollections" in params:
            params["CollapseBoxSetItems"] = params.pop("GroupItemsIntoCollections")
        return super()._request(method, path, params, body, timeout=timeout)

    def list_libraries(self) -> list[dict[str, Any]]:
        # GET /Library/VirtualFolders is an unpaged JSON array. /Query is an
        # Emby-only endpoint, and imposing paged Items/TotalRecordCount here
        # would either fail or silently lose libraries.
        try:
            raw = self._request("GET", "Library/VirtualFolders")
            id_field = "ItemId"
        except EmbyAPIError as error:
            if error.status != 403:
                raise
            # Library management is elevated. Read only this user's physical
            # root library folders instead; grouped UserViews may be virtual
            # and cannot safely substitute for the saved ParentId scope.
            root = self._request("GET", "Items", {"UserId": self.user_id, "Recursive": "false"})
            # Both supported server lines return all unfiltered root children
            # without paging. Type filters can select a recursive path instead,
            # and StartIndex/Limit are ignored by the older root handler.
            count = _value(root, "TotalRecordCount")
            rows = _value(root, "Items")
            if (not isinstance(rows, list) or any(not isinstance(item, dict) for item in rows)
                    or isinstance(count, bool) or not isinstance(count, int) or count != len(rows)):
                raise EmbyAPIError("媒体库返回的用户目录不完整")
            raw = [item for item in rows if text(_value(item, "Type")).casefold() == "collectionfolder"]
            id_field = "Id"
        if not isinstance(raw, list) or any(not isinstance(item, dict) for item in raw):
            raise EmbyAPIError("媒体库返回的目录列表格式无效")
        result, seen = [], set()
        for item in raw:
            identity = text(_value(item, id_field))
            if not identity or identity in seen:
                raise EmbyAPIError("媒体库目录缺少有效且唯一的 ID")
            seen.add(identity)
            kind = text(_value(item, "CollectionType")).casefold()
            if kind in {"", "movies", "tvshows", "mixed"}:
                result.append({"id": identity, "name": text(_value(item, "Name")), "collection_type": kind})
        return result

    def _media_query(self, library_ids: Iterable[str] | None, requested: str, query: str = "") -> list[EmbyItem]:
        """补读版本分组隐藏的影片，按各自身份与所选媒体库判断入库。"""
        kinds = {"movie", "tv"} if requested in {None, "all"} else {media_type(requested)}
        include = ",".join(name for kind, name in (("movie", "Movie"), ("tv", "Series")) if kind in kinds)
        parents = [library_ids] if isinstance(library_ids, str) else list(library_ids or [])
        scope = {text(parent).replace('-', '').casefold() for parent in parents}
        deadline = time.monotonic() + self.scan_timeout

        def remaining():
            value = deadline - time.monotonic()
            if value <= 0:
                raise EmbyAPIError("Jellyfin 媒体库扫描超时，请稍后重试", code="timeout")
            return value

        rows = []
        for parent in parents or [None]:
            remaining()
            page_rows = self._paged_raw("Items", {"UserId": self.user_id, "ParentId": parent,
                "IncludeItemTypes": include, "Recursive": "true", "CollapseBoxSetItems": "false",
                "Fields": "ProviderIds,MediaSources,ParentId", "SearchTerm": query or None}, deadline=deadline)
            identities = [text(_value(row, "Id")).replace('-', '').casefold() for row in page_rows]
            if len(set(identities)) != len(identities):
                raise EmbyAPIError("Jellyfin 媒体分页包含重复条目，请重新读取")
            rows.extend(page_rows)
            remaining()
        result, candidates = {}, set()
        for raw in rows:
            item = EmbyItem.from_dict(raw)
            if item.media_type not in kinds:
                raise EmbyAPIError("Jellyfin 返回的媒体类型与请求不一致")
            identity = item.id.replace('-', '').casefold()
            if identity in result and result[identity].key != item.key:
                raise EmbyAPIError("Jellyfin 返回了冲突的媒体身份")
            result.setdefault(identity, item)
            if item.media_type != "movie":
                continue
            sources = _value(raw, "MediaSources")
            if sources is None:
                continue
            if not isinstance(sources, list) or any(not isinstance(source, dict) for source in sources):
                raise EmbyAPIError("Jellyfin 返回的影片版本信息无效")
            for source in sources:
                kind = text(_value(source, "Type")).casefold()
                if kind not in {"grouping", "default"}:
                    continue
                try:
                    candidate = UUID(text(_value(source, "Id"))).hex
                except ValueError as error:
                    if kind == "grouping":
                        raise EmbyAPIError("Jellyfin 合并版本缺少有效的条目 ID") from error
                    continue
                candidates.add(candidate)
        known = {key.replace('-', '').casefold() for key in result}
        for candidate in sorted(candidates - known):
            raw = self._request("GET", f"Items/{candidate}", {"UserId": self.user_id,
                "Fields": "ProviderIds,ParentId"}, timeout=remaining())
            if text(_value(raw, "Id")).replace('-', '').casefold() != candidate:
                raise EmbyAPIError("Jellyfin 影片版本的身份与请求不一致")
            # Older releases may expose a local alternate as Video, with no
            # Movie identity. Never inherit its primary version's provider IDs.
            if text(_value(raw, "Type")).casefold() != "movie":
                continue
            if scope and text(_value(raw, "ParentId")).replace('-', '').casefold() not in scope:
                ancestors = self._request("GET", f"Items/{candidate}/Ancestors",
                    {"UserId": self.user_id}, timeout=remaining())
                if not isinstance(ancestors, list) or any(not isinstance(row, dict) or not text(_value(row, "Id")) for row in ancestors):
                    raise EmbyAPIError("Jellyfin 影片版本的媒体库归属信息无效")
                if not any(text(_value(row, "Id")).replace('-', '').casefold() in scope for row in ancestors):
                    continue
            result.setdefault(candidate, EmbyItem.from_dict(raw))
        remaining()
        return list(result.values())

    def get_series_seasons(self, series: EmbyItem) -> list[EmbyItem]:
        if series.media_type != "tv" or not series.tmdb_id:
            raise EmbyAPIError("电视剧身份无效，无法读取季")
        # Shows/{id}/Seasons does not implement StartIndex/Limit in Jellyfin.
        # Query direct children through Items to keep complete bounded paging.
        raw = self._paged_raw("Items", {"UserId": self.user_id, "ParentId": series.id,
            "IncludeItemTypes": "Season", "Recursive": "false", "IsMissing": "false", "Fields": "ProviderIds,ParentId"})
        result = [EmbySeason.from_dict(item, series) for item in raw]
        if len({item.id for item in result}) != len(result):
            raise EmbyAPIError("季分页包含重复条目")
        unique = {}
        for item in sorted(result, key=lambda item: (item.season_number, item.id)):
            unique.setdefault(item.key, item)
        return list(unique.values())

    def get_series_episodes(self, series: EmbyItem) -> list[EmbyItem]:
        if series.media_type != "tv" or not series.tmdb_id:
            raise EmbyAPIError("电视剧身份无效，无法读取季和集")
        raw = self._paged_raw(f"Shows/{self._id(series.id)}/Episodes", {"UserId": self.user_id,
            "Fields": "ProviderIds", "IsMissing": "false"})
        result = [EmbyEpisode.from_dict(item, series) for item in raw]
        if len({item.id for item in result}) != len(result):
            raise EmbyAPIError("剧集分页包含重复条目")
        unique = {}
        for item in sorted(result, key=lambda item: (item.season_number, item.episode_number, item.id)):
            unique.setdefault(item.key, item)
        return list(unique.values())

    def explain_unverified_membership(self, kind: str, remote_id: str, missing: list[EmbyItem], *, timeout: float) -> str | None:
        """Diagnose path aliases without accepting them as published members.

        10.10 stores collection links by path. Old Folder and current Season
        records can share that path after library roots change. FindByPath
        resolves the link to the Folder, which the fixed user's view hides.
        The application key can read those links with a recursive unscoped
        query. This is diagnostic only: it never changes the user, writes, or
        substitutes these records for the required typed membership.
        """
        seasons = [item for item in missing if isinstance(item, EmbySeason)]
        if kind != "collection" or not seasons or timeout <= 0:
            return None
        deadline = time.monotonic() + min(timeout, self.scan_timeout)

        def remaining():
            return max(0.001, deadline - time.monotonic())

        try:
            linked = self._paged_raw("Items", {"ParentId": remote_id, "Recursive": "true", "Fields": "Path"}, deadline=deadline)
            aliases = {text(_value(row, "Path")): text(_value(row, "Id")) for row in linked
                if text(_value(row, "Type")).casefold() == "folder" and text(_value(row, "Path"))}
            if not aliases:
                return None
            for item in seasons[:5]:
                if time.monotonic() >= deadline:
                    return None
                raw = self._request("GET", f"Items/{self._id(item.id)}", {"UserId": self.user_id, "Fields": "Path"}, timeout=remaining())
                if (text(_value(raw, "Id")) == item.id and text(_value(raw, "Type")).casefold() == "season"
                        and text(_value(raw, "Path")) in aliases and aliases[text(_value(raw, "Path"))] != item.id):
                    return "Jellyfin 内部索引中存在同路径的普通文件夹，导致合集关联错误；这些旧记录可能不会显示在媒体库页面。需重新校验媒体库根目录，或备份后重建该电视剧库。"
        except EmbyAPIError:
            # The extra diagnostic is optional, never a fallback for membership
            # or authorization. The publisher still checks its own deadline.
            return None
        return None

    def create_playlist(self, name: str, item_ids: Iterable[str] = ()) -> str:
        raw = self._playlist_request("POST", "Playlists", body={"Name": name, "Ids": self._ids(item_ids),
            "UserId": self.user_id, "MediaType": "Video", "IsPublic": False})
        remote = text(_value(raw, "Id"))
        if not remote:
            raise EmbyAPIError("媒体库未返回新建播放列表的 ID")
        return remote

    def add_playlist_items(self, playlist_id: str, item_ids: Iterable[str]) -> None:
        values = self._ids(item_ids)
        if values:
            self._playlist_request("POST", f"Playlists/{self._id(playlist_id)}/Items",
                {"UserId": self.user_id, "Ids": ",".join(values)})

    def remove_playlist_entries(self, playlist_id: str, entry_ids: Iterable[str]) -> None:
        values = self._ids(entry_ids)
        if values:
            # EntryIds are playlist-entry identities, not the media ID supplied
            # to the add endpoint. The removal route has no UserId argument.
            self._playlist_request("DELETE", f"Playlists/{self._id(playlist_id)}/Items", {"EntryIds": ",".join(values)})

    def move_playlist_entry(self, playlist_id: str, entry_id: str, new_index: int) -> None:
        if isinstance(new_index, bool) or not isinstance(new_index, int) or new_index < 0:
            raise ValueError("播放位置必须是非负整数")
        self._playlist_request("POST", f"Playlists/{self._id(playlist_id)}/Items/{self._id(entry_id)}/Move/{new_index}")

    def resolve_authenticated_user_id(self) -> str | None:
        """Return a user-token owner; an application API key has no user."""
        try:
            raw = self._request("GET", "Users/Me")
        except EmbyAPIError as error:
            if error.status == 400:
                return None
            raise
        if not isinstance(raw, dict) or not text(_value(raw, "Id")):
            raise EmbyAPIError("媒体库未返回有效的认证用户")
        return text(_value(raw, "Id"))

    def ensure_playlist_access(self) -> None:
        with self._playlist_lock:
            if self._playlist_user is not None:
                return
            if self.token_provider:
                token = self.token_provider(self, False)
                self._playlist_user = self.with_token(token)
            elif same_user(self.resolve_authenticated_user_id() or '', self.user_id):
                self._playlist_user = self
            else:
                raise JellyfinAuthError("Jellyfin 播放列表的用户授权尚未配置，请重新加载插件后重试。", code="permission_denied")

    def check_playlist_access(self) -> None:
        """Preview checks authorization prerequisites without creating a session."""
        if self.authorization_check:
            self.authorization_check(self)
        else:
            self.ensure_playlist_access()

    def _playlist_request(self, method, path, params=None, body=None):
        self.ensure_playlist_access()
        for attempt in range(2):
            try:
                return self._playlist_user._request(method, path, params, body)
            except EmbyAPIError as error:
                if error.status == 403:
                    raise JellyfinAuthError("Jellyfin 已识别授权用户，但该用户无权创建或编辑这个播放列表；请检查列表所属用户及编辑权限。", code="permission_denied", status=403) from error
                if error.status != 401:
                    raise
                if attempt or not self.token_provider:
                    raise JellyfinAuthError("Jellyfin 拒绝了播放列表用户令牌（HTTP 401）；请检查用户状态及服务器认证设置后重新同步。", code="authorization_denied", status=401) from error
            # Only explicit 401 is safe to retry, and only once. Every attempt
            # uses the same diagnostic mapping; timeouts/5xx never repeat writes.
            with self._playlist_lock:
                token = self.token_provider(self, True)
                self._playlist_user = self.with_token(token)
