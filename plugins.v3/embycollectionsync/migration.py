"""Atomic, side-effect-free conversion of legacy plugin configuration.

The caller persists a full backup before replacing configuration. Invalid
members or text tokens raise; an incomplete migration must never be saved.
"""
from __future__ import annotations

import copy
import re
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from .models import MediaList, MediaMember, media_type, positive_id, text, typed_key


def validate_cron(value: Any) -> str:
    expression = text(value)
    fields = expression.split()
    if len(fields) != 5:
        raise ValueError("执行周期必须包含分钟、小时、日期、月份和星期五个字段")
    bounds = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 6)]
    for field, (low, high) in zip(fields, bounds):
        for part in field.split(","):
            if part.count("/") > 1:
                raise ValueError("执行周期的字段格式无效")
            pieces = part.split("/")
            base = pieces[0]
            if len(pieces) == 2 and (not pieces[1].isdigit() or not 1 <= int(pieces[1]) <= high - low + 1):
                raise ValueError("执行周期的间隔无效")
            if base == "*":
                continue
            numbers = base.split("-")
            if len(numbers) > 2 or any(not n.isdigit() or not low <= int(n) <= high for n in numbers):
                raise ValueError("执行周期的数值超出允许范围")
            if len(numbers) == 2 and int(numbers[0]) > int(numbers[1]):
                raise ValueError("执行周期的起止范围无效")
    return expression


def _stable_id(label: str) -> str:
    return uuid5(NAMESPACE_URL, f"moviepilot/EmbyCollectionSync/{label}").hex


def _tokens(value: Any, kind: str) -> list[str]:
    if isinstance(value, str):
        values = [token for token in re.split(r"[\s,;，；]+", value.strip()) if token]
    elif isinstance(value, (list, tuple)):
        values = list(value)
    elif value is None:
        values = []
    else:
        raise ValueError("旧版 TMDB 编号必须是文本或列表")
    keys = []
    for value in values:
        key = typed_key(value) if ":" in text(value) else f"{kind}:{positive_id(value)}"
        if key not in keys:
            keys.append(key)
    return keys


def _old_member(value: Any, kind: str) -> MediaMember:
    if isinstance(value, dict):
        return MediaMember(
            value.get("tmdb_id", value.get("tmdbid", value.get("id"))),
            media_type(value.get("media_type", value.get("type", kind))),
            value.get("title", value.get("name", "")),
            value.get("year", value.get("production_year")),
            value.get("poster", value.get("poster_url")),
        )
    key = _tokens([value], kind)[0]
    item_kind, item_id = key.split(":", 1)
    return MediaMember(item_id, item_kind)


def _legacy_rule(rule: Any, config: dict[str, Any], index: int) -> MediaList:
    if not isinstance(rule, dict):
        raise ValueError("旧版规则格式无效")
    raw = copy.deepcopy(rule)
    rule_id = text(raw.get("id")) or _stable_id(f"rule/{index}/{text(raw.get('name'))}")
    prefix = f"rule_{rule_id}_"
    for key in ("name", "enabled", "media_type"):
        if prefix + key in config:
            raw[key] = config[prefix + key]
    kind = media_type(raw.get("media_type", raw.get("type", "movie")))
    raw_members = raw.get("movies", raw.get("movie_list", raw.get("items", [])))
    if not isinstance(raw_members, list):
        raise ValueError("旧版影片内容必须是列表")
    members = [_old_member(item, kind) for item in raw_members]
    text_ids = config.get(prefix + "movie_ids", raw.get("movie_ids_text"))
    if text_ids is not None:
        selected = _tokens(text_ids, kind)
        old_members = {item.key: item for item in members}
        members = [old_members.get(key) or _old_member(key, kind) for key in selected]
    collection = raw.get("collection", {})
    if not isinstance(collection, dict):
        raise ValueError("旧版合集设置格式无效")
    collection = {
        "enabled": config.get(prefix + "collection_enabled", collection.get("enabled", False)),
        "name": config.get(prefix + "collection_name", collection.get("name", "")),
        "remote_id": collection.get("remote_id", collection.get("emby_item_id", collection.get("emby_id"))),
    }
    playlists = raw.get("playlists", [])
    if not isinstance(playlists, list):
        raise ValueError("旧版播放列表设置必须是列表")
    converted = []
    for number, playlist in enumerate(playlists):
        if not isinstance(playlist, dict):
            raise ValueError("旧版播放列表格式无效")
        playlist_id = text(playlist.get("id")) or _stable_id(f"{rule_id}/playlist/{number}")
        flat_prefix = f"playlist_{playlist_id}_"
        order = config.get(flat_prefix + "order", playlist.get("order_text", playlist.get("ordered_movie_ids", playlist.get("movie_ids", playlist.get("order", [])))))
        if isinstance(order, dict):
            order = order.get("ordered_movie_ids", order.get("order", []))
        converted.append({"id": playlist_id, "name": config.get(flat_prefix + "name", playlist.get("name", "")), "enabled": config.get(flat_prefix + "enabled", playlist.get("enabled", True)), "remote_id": playlist.get("remote_id", playlist.get("emby_item_id", playlist.get("emby_id"))), "order": _tokens(order, kind)})
    server_id = text(raw.get("server_name", raw.get("server_id", config.get("server_name")))) or "Emby"
    library_ids = raw.get("library_ids", config.get("library_ids", []))
    if isinstance(library_ids, str):
        library_ids = [v for v in re.split(r"[\s,]+", library_ids) if v]
    if not isinstance(library_ids, list):
        raise ValueError("旧版媒体库目录必须是列表")
    # Keep a target even if outputs are disabled: it preserves server and
    # library choices and stable bindings without publishing by default.
    target = {"id": _stable_id(f"{rule_id}/target/{server_id}"), "server_id": server_id, "kind": "emby", "library_ids": library_ids, "collection": collection, "playlists": converted}
    return MediaList.from_dict({"id": rule_id, "name": raw.get("name", ""), "enabled": raw.get("enabled", True), "revision": 0, "members": [item.to_dict() for item in members], "sources": raw.get("sources", []), "policy": raw.get("policy", {}), "targets": [target]})


def migrate_config(raw: dict[str, Any] | None) -> dict[str, Any]:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError("插件配置格式无效")
    result = copy.deepcopy(raw)
    version = result.get("schema_version", 0)
    if isinstance(version, bool) or not isinstance(version, int) or version > 3:
        raise ValueError("不支持此版本的插件配置")
    if version == 3:
        values = result.get("lists", [])
        if not isinstance(values, list):
            raise ValueError("片单内容必须是列表")
        result["lists"] = [MediaList.from_dict(item).to_dict() for item in values]
        result["cron"] = validate_cron(result.get("cron", "*/30 * * * *"))
    else:
        rules = result.get("rules", [])
        if not isinstance(rules, list):
            raise ValueError("旧版规则必须是列表")
        if not rules:
            # Flat-only forms can occur after an incomplete older save.
            suffix = "_name"
            ids = [key[len("rule_"):-len(suffix)] for key in result if key.startswith("rule_") and key.endswith(suffix)]
            rules = [{"id": rule_id, "name": result[f"rule_{rule_id}_name"]} for rule_id in ids]
        if not rules and "lists" in result:
            if not isinstance(result["lists"], list):
                raise ValueError("片单内容必须是列表")
            result["lists"] = [MediaList.from_dict(item).to_dict() for item in result["lists"]]
        else:
            result["lists"] = [_legacy_rule(item, result, index).to_dict() for index, item in enumerate(rules)]
        schedule = result.get("schedule", {})
        if not isinstance(schedule, dict):
            raise ValueError("自动任务设置格式无效")
        cron = schedule.get("cron") or result.get("cron")
        if cron:
            try:
                validate_cron(cron)
            except ValueError:
                # An older release generated */90 in the minute field from
                # a valid interval. Recover only that known, provable bug;
                # other invalid schedules still abort the migration.
                old_step = re.fullmatch(r"\*/(\d+)\s+\*\s+\*\s+\*\s+\*", text(cron))
                recorded = schedule.get("interval_minutes", result.get("interval_minutes"))
                if not old_step or int(old_step[1]) < 60 or recorded is None or positive_id(recorded) != int(old_step[1]):
                    raise
                cron = None
                result["migration_warnings"] = list(result.get("migration_warnings", [])) + ["已根据保留的执行间隔修复旧版周期设置。"]
        if cron:
            result["cron"] = validate_cron(cron)
        elif schedule.get("daily_time") or result.get("daily_time"):
            daily = text(schedule.get("daily_time") or result.get("daily_time"))
            if not re.fullmatch(r"\d{1,2}:\d{2}", daily):
                raise ValueError("旧版每日执行时间必须为“小时:分钟”格式")
            hour, minute = [int(value) for value in daily.split(":")]
            result["cron"] = validate_cron(f"{minute} {hour} * * *")
        else:
            interval = positive_id(schedule.get("interval_minutes", result.get("interval_minutes", 30)))
            if interval < 60 and 60 % interval == 0:
                result["cron"] = f"*/{interval} * * * *"
            elif interval % 60 == 0 and interval <= 1440 and 1440 % interval == 0:
                hours = interval // 60
                result["cron"] = f"0 */{hours} * * *" if hours < 24 else "0 0 * * *"
            else:
                result["cron"] = "0 */2 * * *"
                result["migrated_interval_minutes"] = interval
                result["migration_warnings"] = list(result.get("migration_warnings", [])) + [f"已保留旧版每 {interval} 分钟执行的设置，修改执行周期后将使用新设置。"]
        for key in list(result):
            if key == "rules" or key.startswith(("rule_", "playlist_")):
                result.pop(key)
    ids = [item["id"] for item in result["lists"]]
    if len(ids) != len(set(ids)):
        raise ValueError("片单 ID 重复")
    # Cross-list remote binding conflicts are recoverable configuration issues.
    # Preserve the lists during migration so the plugin can display and repair them;
    # runtime activation and sync validation handle the conflict safely.
    enabled = result.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ValueError("启用状态必须为开启或关闭")
    result["enabled"] = enabled
    result["schema_version"] = 3
    return result
