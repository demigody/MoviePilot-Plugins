"""Validated schema 3 objects. Members own typed TMDB identity, never server IDs."""
from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any, Iterable
from uuid import uuid4

from .messages import field_name


def text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def positive_id(value: Any) -> int:
    raw = text(value)
    if isinstance(value, bool) or not raw.isascii() or not raw.isdigit() or int(raw) <= 0:
        raise ValueError("TMDB 编号必须是正整数")
    return int(raw)


def media_type(value: Any) -> str:
    aliases = {"movie": "movie", "电影": "movie", "tv": "tv", "series": "tv", "电视剧": "tv", "剧集": "tv"}
    raw = text(value).casefold()
    if raw not in aliases:
        raise ValueError("媒体类型必须是电影或电视剧，不能将季或单集作为独立作品添加")
    return aliases[raw]


def typed_key(value: Any) -> str:
    raw = text(value)
    if ":" not in raw:
        raise ValueError("媒体编号必须包含电影或电视剧的类型前缀")
    kind, number = raw.split(":", 1)
    return f"{media_type(kind)}:{positive_id(number)}"


def unique_keys(values: Iterable[Any]) -> list[str]:
    return list(dict.fromkeys(typed_key(value) for value in values))


def playlist_key(value: Any) -> str:
    parts = text(value).split(":")
    base = typed_key(":".join(parts[:2]))
    if len(parts) == 2:
        return base
    if len(parts) != 4 or not base.startswith("tv:") or parts[2] != "season" or not parts[3].isascii() or not parts[3].isdigit():
        raise ValueError("播放列表排序项只能是电影、整部电视剧或电视剧的一季")
    return f"{base}:season:{int(parts[3])}"


def playlist_parent(key: str) -> str:
    return ":".join(key.split(":")[:2])


def normalize_playlist_order(order: Iterable[str], members: Iterable[str]) -> list[str]:
    members = list(members)
    valid = set(members)
    keys = list(dict.fromkeys(playlist_key(key) for key in order))
    explicit = {playlist_parent(key) for key in keys if ":season:" in key}
    keys = [key for key in keys if playlist_parent(key) in valid and not (key in explicit)]
    keys.extend(key for key in members if key not in keys and key not in explicit)
    return keys


def _object(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{field_name(name)}格式无效")
    return value


def _array(value: Any, name: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{field_name(name)}必须是列表")
    return value


def _boolean(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{field_name(name)}必须为开启或关闭")
    return value


@dataclass
class MediaMember:
    tmdb_id: int
    media_type: str = "movie"
    title: str = ""
    year: int | None = None
    poster: str | None = None
    seasons: list[int] | None = None

    def __post_init__(self) -> None:
        self.tmdb_id = positive_id(self.tmdb_id)
        self.media_type = media_type(self.media_type)
        self.title = text(self.title)
        self.poster = text(self.poster) or None
        if self.seasons is not None:
            if self.media_type != "tv" or not isinstance(self.seasons, list) or not self.seasons or any(
                isinstance(number, bool) or not isinstance(number, int) or number < 0 for number in self.seasons
            ):
                raise ValueError("指定季需要至少选择一季，季号必须是非负整数，且仅用于电视剧")
            self.seasons = sorted(set(self.seasons))
        if self.year is not None:
            if isinstance(self.year, bool) or not text(self.year).isdigit():
                raise ValueError("年份必须是整数或留空")
            self.year = int(self.year)
            if not 1 <= self.year <= 9999:
                raise ValueError("年份必须在 1 到 9999 之间")

    @property
    def key(self) -> str:
        return f"{self.media_type}:{self.tmdb_id}"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MediaMember":
        data = _object(data, "member")
        return cls(data.get("tmdb_id"), data.get("media_type", "movie"), data.get("title", ""), data.get("year"), data.get("poster"), data.get("seasons"))

    def to_dict(self) -> dict[str, Any]:
        result = {"tmdb_id": self.tmdb_id, "media_type": self.media_type, "title": self.title, "year": self.year, "poster": self.poster}
        if self.seasons is not None:
            result["seasons"] = list(self.seasons)
        return result


@dataclass
class Source:
    tmdb_id: int
    name: str = ""
    mode: str = "once"
    seen_keys: list[str] = field(default_factory=list)
    last_checked: str | None = None
    error: str | None = None

    def __post_init__(self) -> None:
        self.tmdb_id = positive_id(self.tmdb_id)
        self.name = text(self.name)
        if self.mode not in {"once", "follow"}:
            raise ValueError("TMDB 合集导入方式必须是仅导入一次或持续跟踪")
        self.seen_keys = unique_keys(self.seen_keys)
        if any(not key.startswith("movie:") for key in self.seen_keys):
            raise ValueError("TMDB 合集只能包含电影")
        self.last_checked = text(self.last_checked) or None
        self.error = text(self.error) or None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Source":
        data = _object(data, "source")
        return cls(data.get("tmdb_id"), data.get("name", ""), data.get("mode", "once"), _array(data.get("seen_keys", []), "seen_keys"), data.get("last_checked"), data.get("error"))

    def to_dict(self) -> dict[str, Any]:
        return {"tmdb_id": self.tmdb_id, "name": self.name, "mode": self.mode, "seen_keys": list(self.seen_keys), "last_checked": self.last_checked, "error": self.error}


@dataclass
class CollectionBinding:
    enabled: bool = False
    name: str = ""
    remote_id: str | None = None

    def __post_init__(self) -> None:
        self.enabled = _boolean(self.enabled, "collection.enabled")
        self.name = text(self.name)
        self.remote_id = text(self.remote_id) or None

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None = None) -> "CollectionBinding":
        data = _object(data if data is not None else {}, "collection")
        return cls(data.get("enabled", False), data.get("name", ""), data.get("remote_id"))

    def to_dict(self) -> dict[str, Any]:
        return {"enabled": self.enabled, "name": self.name, "remote_id": self.remote_id}


@dataclass
class Playlist:
    name: str = ""
    id: str = field(default_factory=lambda: uuid4().hex)
    enabled: bool = True
    remote_id: str | None = None
    order: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.name = text(self.name)
        self.id = text(self.id) or uuid4().hex
        self.enabled = _boolean(self.enabled, "playlist.enabled")
        self.remote_id = text(self.remote_id) or None
        self.order = list(dict.fromkeys(playlist_key(key) for key in self.order))

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Playlist":
        data = _object(data, "playlist")
        return cls(data.get("name", ""), data.get("id", ""), data.get("enabled", True), data.get("remote_id"), _array(data.get("order", []), "playlist.order"))

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "enabled": self.enabled, "remote_id": self.remote_id, "order": list(self.order)}


@dataclass
class Target:
    server_id: str
    kind: str = "emby"
    id: str = field(default_factory=lambda: uuid4().hex)
    library_ids: list[str] = field(default_factory=list)
    collection: CollectionBinding = field(default_factory=CollectionBinding)
    playlists: list[Playlist] = field(default_factory=list)
    connection_fingerprint: str | None = None
    pending_mapping: bool = False

    def __post_init__(self) -> None:
        self.server_id = text(self.server_id)
        self.pending_mapping = _boolean(self.pending_mapping, "target.pending_mapping")
        if not self.server_id and not self.pending_mapping:
            raise ValueError("请选择媒体服务器")
        self.kind = text(self.kind).casefold()
        if self.kind not in {"emby", "jellyfin", "plex"}:
            raise ValueError("不支持此媒体服务器类型")
        self.id = text(self.id) or uuid4().hex
        self.connection_fingerprint = text(self.connection_fingerprint) or None
        if self.connection_fingerprint and not re.fullmatch(r'[a-f0-9]{64}', self.connection_fingerprint):
            raise ValueError('媒体服务器连接标识无效，请重新选择服务器')
        self.library_ids = list(dict.fromkeys(text(value) for value in self.library_ids if text(value)))
        if not isinstance(self.collection, CollectionBinding) or any(not isinstance(item, Playlist) for item in self.playlists):
            raise ValueError("媒体库同步的绑定信息无效")
        ids = [item.id for item in self.playlists]
        if len(ids) != len(set(ids)):
            raise ValueError("播放列表 ID 重复")
        remote_ids = ([self.collection.remote_id] if self.collection.remote_id else []) + [item.remote_id for item in self.playlists if item.remote_id]
        if len(remote_ids) != len(set(remote_ids)):
            raise ValueError("同一组同步设置不能重复绑定同一个合集或播放列表")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Target":
        data = _object(data, "target")
        return cls(data.get("server_id", ""), data.get("kind", "emby"), data.get("id", ""), _array(data.get("library_ids", []), "library_ids"), CollectionBinding.from_dict(data.get("collection")), [Playlist.from_dict(item) for item in _array(data.get("playlists", []), "playlists")], data.get('connection_fingerprint'), data.get("pending_mapping", False))

    def to_dict(self) -> dict[str, Any]:
        result = {"id": self.id, "server_id": self.server_id, "kind": self.kind, "library_ids": list(self.library_ids), "collection": self.collection.to_dict(), "playlists": [item.to_dict() for item in self.playlists]}
        if self.pending_mapping:
            result["pending_mapping"] = True
        if self.connection_fingerprint:
            result['connection_fingerprint'] = self.connection_fingerprint
        return result


@dataclass
class MediaList:
    name: str
    id: str = field(default_factory=lambda: uuid4().hex)
    enabled: bool = True
    revision: int = 0
    members: list[MediaMember] = field(default_factory=list)
    sources: list[Source] = field(default_factory=list)
    policy: dict[str, Any] = field(default_factory=dict)
    targets: list[Target] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.name = text(self.name)
        if not self.name:
            raise ValueError("片单名称不能为空")
        self.id = text(self.id) or uuid4().hex
        self.enabled = _boolean(self.enabled, "list.enabled")
        if isinstance(self.revision, bool) or not isinstance(self.revision, int) or self.revision < 0:
            raise ValueError("片单版本号必须是非负整数")
        if any(not isinstance(item, MediaMember) for item in self.members):
            raise ValueError("片单中的媒体信息无效")
        seen: set[str] = set()
        unique = []
        for member in self.members:
            if member.key not in seen:
                unique.append(member)
                seen.add(member.key)
        self.members = unique
        if any(not isinstance(item, Source) for item in self.sources) or any(not isinstance(item, Target) for item in self.targets):
            raise ValueError("片单来源或媒体库同步设置无效")
        source_ids = [item.tmdb_id for item in self.sources]
        if len(source_ids) != len(set(source_ids)):
            raise ValueError("TMDB 合集来源重复")
        target_ids = [item.id for item in self.targets]
        if len(target_ids) != len(set(target_ids)):
            raise ValueError("媒体库同步设置的 ID 重复")
        self.policy = dict(_object(self.policy, "policy"))
        self.policy["auto_subscribe"] = _boolean(self.policy.get("auto_subscribe", False), "auto_subscribe")
        self.policy["publish_on_library"] = _boolean(self.policy.get("publish_on_library", False), "publish_on_library")
        self.policy["organize_category"] = text(self.policy.get("organize_category", ""))
        self.validate()

    @property
    def member_keys(self) -> list[str]:
        return [item.key for item in self.members]

    def validate(self) -> None:
        keys = self.member_keys
        for target in self.targets:
            if collection_disabled_reason(target.kind, self.members):
                target.collection.enabled = False
            for playlist in target.playlists:
                playlist.order = normalize_playlist_order(playlist.order, keys)

    def add_member(self, member: MediaMember) -> bool:
        if not isinstance(member, MediaMember):
            raise ValueError("媒体信息无效")
        if member.key in self.member_keys:
            return False
        self.members.append(member)
        self.validate()
        return True

    def remove_member(self, key: str) -> bool:
        key = typed_key(key)
        if key not in self.member_keys:
            return False
        self.members = [item for item in self.members if item.key != key]
        self.validate()
        return True

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MediaList":
        data = _object(data, "list")
        return cls(data.get("name", ""), data.get("id", ""), data.get("enabled", True), data.get("revision", 0), [MediaMember.from_dict(item) for item in _array(data.get("members", []), "members")], [Source.from_dict(item) for item in _array(data.get("sources", []), "sources")], data.get("policy", {}), [Target.from_dict(item) for item in _array(data.get("targets", []), "targets")])

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "enabled": self.enabled, "revision": self.revision, "members": [item.to_dict() for item in self.members], "sources": [item.to_dict() for item in self.sources], "policy": dict(self.policy), "targets": [item.to_dict() for item in self.targets]}


def collection_disabled_reason(kind: str, members: Iterable[MediaMember]) -> str:
    if kind != 'plex':
        return ''
    members = list(members)
    if len({member.media_type for member in members}) > 1:
        return 'Plex 合集不能混合电影和电视剧，请使用播放列表。'
    if any(member.seasons is not None for member in members):
        return 'Plex 合集暂不支持指定季，请使用播放列表。'
    return ''


def validate_bindings(lists: Iterable[MediaList]) -> None:
    """拒绝重复管理同一远端对象，并指出两处绑定以便修正配置。"""
    used: dict[tuple[str, str], str] = {}
    for list_index, listing in enumerate(lists, 1):
        for target_index, target in enumerate(listing.targets, 1):
            owner = f"片单“{listing.name}”（第{list_index}条）的第{target_index}个发布目标"
            remote_ids = ([target.collection.remote_id] if target.collection.remote_id else []) + [item.remote_id for item in target.playlists if item.remote_id]
            for remote_id in remote_ids:
                identity = (target.server_id, remote_id)
                if identity in used:
                    raise ValueError(
                        f"服务器“{target.server_id}”的远端输出（ID：{remote_id}）重复绑定："
                        f"{used[identity]}与{owner}。请保留一个绑定，另一份使用独立输出。"
                    )
                used[identity] = owner
