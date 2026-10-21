"""Portable media-list files and read-only Trakt list imports."""
from __future__ import annotations

import copy
import json
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen
from uuid import uuid4

from .models import MediaList, MediaMember, normalize_playlist_order

FORMAT_ID = "moviepilot.media-lists"
FORMAT_VERSION = 1
# Trakt Client IDs are public identifiers (not secrets). This is the ID the
# plugin maintainer supplied for the shared MoviePilot integration.
DEFAULT_TRAKT_CLIENT_ID = "rT-NNfqsFhFwcdd_26EZR3GgSp5r_1ysHhug2Or96io"
MAX_LISTS = 500
MAX_MEMBERS = 20000
_TRAKT_HOSTS = {"trakt.tv", "www.trakt.tv", "app.trakt.tv"}


def export_document(lists: list[dict], *, exported_at: str, list_id: str | None = None) -> dict:
    selected = [item for item in lists if list_id is None or str(item.get("id")) == str(list_id)]
    if list_id is not None and not selected:
        raise ValueError("片单不存在")
    portable = []
    for raw in selected:
        item = copy.deepcopy(raw)
        # Remote IDs and connection fingerprints belong to the source server.
        for target in item.get("targets", []):
            target.pop("connection_fingerprint", None)
            target.pop("pending_mapping", None)
            target.pop("source_server_id", None)
            target.pop("source_server_name", None)
            target.pop("source_library_ids", None)
            target.pop("source_kind", None)
            target.pop("server_id", None)
            target.pop("library_ids", None)
            target["server_id"] = ""
            target["library_ids"] = []
            collection = target.get("collection") or {}
            collection["remote_id"] = None
            for playlist in target.get("playlists", []):
                playlist["remote_id"] = None
        portable.append(item)
    return {"format": FORMAT_ID, "version": FORMAT_VERSION, "exported_at": exported_at, "lists": portable}


def _validate_document(document: object) -> list[dict]:
    if not isinstance(document, dict) or document.get("format") != FORMAT_ID:
        raise ValueError("文件不是 MoviePilot 片单标准格式")
    if document.get("version") != FORMAT_VERSION:
        raise ValueError("片单文件版本不受支持")
    raw_lists = document.get("lists")
    if not isinstance(raw_lists, list) or not raw_lists or len(raw_lists) > MAX_LISTS:
        raise ValueError("文件中的片单数量无效")
    if sum(len(item.get("members", [])) for item in raw_lists if isinstance(item, dict) and isinstance(item.get("members", []), list)) > MAX_MEMBERS:
        raise ValueError("文件中的媒体数量过多")
    result = []
    for raw in raw_lists:
        if not isinstance(raw, dict):
            raise ValueError("片单文件中包含无效内容")
        value = copy.deepcopy(raw)
        if not str(value.get("name") or "").strip():
            raise ValueError("片单文件中有片单缺少名称")
        if not isinstance(value.get("members", []), list) or not isinstance(value.get("targets", []), list):
            raise ValueError("片单成员或媒体库设置格式无效")
        # Recreate local identities and remove source bindings during import.
        value["id"] = uuid4().hex
        value["revision"] = 0
        value["enabled"] = False
        value["policy"] = {"auto_subscribe": False, "publish_on_library": False, "organize_category": ""}
        for target in value.get("targets", []):
            if (not isinstance(target, dict) or not isinstance(target.get("playlists", []), list)
                    or not isinstance(target.get("collection", {}), dict)):
                raise ValueError("媒体库同步设置格式无效")
            target["id"] = uuid4().hex
            target["server_id"] = ""
            target["library_ids"] = []
            target["connection_fingerprint"] = None
            target["collection"] = {**(target.get("collection") or {}), "remote_id": None}
            for playlist in target.get("playlists", []):
                if not isinstance(playlist, dict):
                    raise ValueError("播放列表设置格式无效")
                playlist["id"] = uuid4().hex
                playlist["remote_id"] = None
        # Validate members and playlist order independently of the not-yet-mapped targets.
        members = [MediaMember.from_dict(member).to_dict() for member in value.get("members", [])]
        value["members"] = members
        keys = [f"{member['media_type']}:{member['tmdb_id']}" for member in members]
        for target in value.get("targets", []):
            for playlist in target.get("playlists", []):
                order = playlist.get("order", [])
                if not isinstance(order, list):
                    raise ValueError("播放列表顺序格式无效")
                playlist["order"] = normalize_playlist_order(order, keys)
        result.append(value)
    return result


def parse_standard_file(document: object) -> list[dict]:
    return _validate_document(document)


def trakt_link_parts(value: str) -> tuple[str, ...]:
    try:
        raw = str(value).strip()
        if "://" not in raw:
            raw = "https://" + raw.lstrip("/")
        parsed = urlparse(raw)
    except Exception as error:
        raise ValueError("请输入有效的 Trakt 片单链接") from error
    if parsed.scheme.lower() not in {"http", "https"} or (parsed.hostname or "").lower() not in _TRAKT_HOSTS or parsed.username or parsed.password:
        raise ValueError("只支持 trakt.tv、www.trakt.tv 或 app.trakt.tv 上的公开片单链接")
    parts = [part for part in parsed.path.split("/") if part]
    lower_parts = [part.lower() for part in parts]
    try:
        lists_index = lower_parts.index("lists")
    except ValueError as error:
        raise ValueError("请粘贴 Trakt 公开片单页面链接") from error
    if lists_index + 1 >= len(parts):
        raise ValueError("链接中没有片单标识，请粘贴 Trakt 公开片单页面链接")
    identifier = parts[lists_index + 1]
    # Official lists have a different web route. Trakt exposes them in the API
    # through the shared "any" user namespace.
    if identifier.lower() == "official":
        if lists_index + 2 < len(parts) and parts[lists_index + 2]:
            return ("user", "any", parts[lists_index + 2])
        raise ValueError("链接中没有片单标识，请粘贴 Trakt 公开片单页面链接")
    if lists_index == 0:
        return ("public", identifier)
    if lists_index >= 2 and lower_parts[lists_index - 2] == "users":
        return ("user", parts[lists_index - 1], identifier)
    raise ValueError("请粘贴 Trakt 公开片单页面链接")


def _trakt_get(path: str, client_id: str, params: dict[str, object] | None = None, *, deadline: float | None = None):
    from urllib.parse import urlencode

    url = "https://api.trakt.tv" + path
    if params:
        url += "?" + urlencode(params)
    request = Request(url, headers={
        "trakt-api-key": client_id,
        "trakt-api-version": "2",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "MoviePilot-MediaList-Import/1.0",
    })
    try:
        remaining = 15 if deadline is None else min(15, deadline - time.monotonic())
        if remaining <= 0:
            raise ValueError("读取 Trakt 片单超时，请稍后重试")
        with urlopen(request, timeout=remaining) as response:
            return json.loads(response.read().decode("utf-8")), {key.lower(): value for key, value in response.headers.items()}
    except HTTPError as error:
        if error.code in {401, 403}:
            raise ValueError("Trakt 拒绝访问：请检查 Client ID，或确认该片单可公开读取") from error
        if error.code == 404:
            raise ValueError("Trakt 中没有找到此片单") from error
        if error.code == 429:
            raise ValueError("Trakt 请求过于频繁，请稍后重试") from error
        raise ValueError(f"Trakt 返回 HTTP {error.code}，请稍后重试") from error
    except (URLError, TimeoutError, json.JSONDecodeError) as error:
        raise ValueError("无法读取 Trakt 片单，请检查 MoviePilot 的网络连接后重试") from error


def import_trakt_list(value: str, client_id: str) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9_-]{20,100}", str(client_id or "")):
        raise ValueError("请先在插件设置中填写有效的 Trakt Client ID")
    parts = trakt_link_parts(value)
    deadline = time.monotonic() + 110
    if parts[0] == "public":
        base = "/lists/" + quote(parts[1], safe="")
    else:
        base = "/users/{}/lists/{}".format(quote(parts[1], safe=""), quote(parts[2], safe=""))
    metadata, _ = _trakt_get(base, client_id, deadline=deadline)
    name = str(metadata.get("name") or "Trakt 片单").strip()
    sort_by = str(metadata.get("sort_by") or "rank")
    sort_how = str(metadata.get("sort_how") or "asc")
    items, page = [], 1
    while page <= 200:
        rows, headers = _trakt_get(base + "/items/movies,shows,seasons,episodes", client_id, {
            "page": page, "limit": 100,
            "sort_by": sort_by, "sort_how": sort_how,
        }, deadline=deadline)
        if not isinstance(rows, list):
            raise ValueError("Trakt 返回的片单内容格式无效")
        items.extend(rows)
        page_count = headers.get("x-pagination-page-count")
        if not rows or (page_count and page >= int(page_count)) or len(rows) < 100:
            break
        page += 1
    else:
        raise ValueError("Trakt 片单页数超出安全上限")
    members, unsupported = [], []
    seen, whole_shows, order_warned, scope_warned = set(), set(), set(), set()
    last_key = None
    for row in items:
        kind = row.get("type")
        data = row.get(kind) if kind in {"movie", "show", "season", "episode"} else None
        if not isinstance(data, dict):
            unsupported.append("未知媒体类型")
            continue
        parent = data if kind in {"movie", "show"} else row.get("show")
        tmdb_id = (parent or {}).get("ids", {}).get("tmdb") if isinstance(parent, dict) else None
        if not tmdb_id:
            unsupported.append(str(data.get("title") or data.get("season") or "缺少 TMDB 编号"))
            continue
        media_type = "movie" if kind == "movie" else "tv"
        member = {"tmdb_id": tmdb_id, "media_type": media_type,
                  "title": str((parent or {}).get("title") or "").strip(),
                  "year": (parent or {}).get("year"), "poster": None}
        if kind == "season":
            number = data.get("number")
            if isinstance(number, int) and number >= 0:
                member["seasons"] = [number]
            else:
                unsupported.append(member["title"] or "无效季号")
                continue
        elif kind == "episode":
            unsupported.append(f"{member['title'] or tmdb_id}：精确到单集的条目暂不支持导入")
            continue
        parsed = MediaMember.from_dict(member).to_dict()
        if parsed["media_type"] == "movie":
            parsed.pop("seasons", None)
        key = f"{parsed['media_type']}:{parsed['tmdb_id']}"
        if key not in seen:
            seen.add(key)
            members.append(parsed)
            if kind == "show":
                whole_shows.add(key)
        elif kind == "season":
            if last_key is not None and last_key != key and key not in order_warned:
                unsupported.append(f"{parsed['title'] or parsed['tmdb_id']}：片单中该剧的季与其他作品交错，已合并到首次出现的位置")
                order_warned.add(key)
            existing = next(entry for entry in members if f"{entry['media_type']}:{entry['tmdb_id']}" == key)
            if key in whole_shows:
                if key not in scope_warned:
                    unsupported.append(f"{existing['title'] or parsed['tmdb_id']}：片单同时包含整部剧和指定季，已保留整部剧")
                    scope_warned.add(key)
            elif existing.get("seasons") is not None:
                existing["seasons"] = sorted(set(existing["seasons"] + parsed["seasons"]))
            else:
                existing["seasons"] = parsed["seasons"]
        elif kind == "show":
            existing = next(entry for entry in members if f"{entry['media_type']}:{entry['tmdb_id']}" == key)
            if existing.get("seasons") is not None:
                existing.pop("seasons", None)
                whole_shows.add(key)
                if key not in scope_warned:
                    unsupported.append(f"{existing['title'] or parsed['tmdb_id']}：片单同时包含整部剧和指定季，已保留整部剧")
                    scope_warned.add(key)
        last_key = key
    return {"name": name, "members": members, "unsupported": unsupported, "source": "trakt"}
