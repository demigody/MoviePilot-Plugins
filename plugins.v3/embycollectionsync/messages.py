"""Chinese diagnostics, including messages kept in older run records.

Only diagnostic fields are localized; media titles, IDs and protocol values
are data and must never be translated.
"""
from __future__ import annotations

import copy
import re


LEGACY_MESSAGES = {
    "Media server returned invalid provider IDs": "媒体库返回的外部编号格式无效",
    "Media server returned an invalid TMDB identity": "媒体库返回的 TMDB 编号无效",
    "Media server returned an item without an ID": "媒体库返回的条目缺少 ID",
    "Media server returned an invalid year": "媒体库返回的年份无效",
    "Media server returned invalid image metadata": "媒体库返回的图片信息无效",
    "Media server URL must be an http(s) URL without credentials or query": "媒体服务器地址必须以 http 或 https 开头，且不能包含账号密码或查询参数",
    "Media server object ID is required": "缺少媒体库对象 ID",
    "Media server request timed out": "媒体库请求超时，请稍后重试",
    "Media server connection failed": "无法连接媒体服务器，请检查地址和网络",
    "Media server transport failed": "媒体库请求失败，请检查连接",
    "Media server returned invalid JSON": "媒体库返回的数据无法解析",
    "Media server returned an invalid paged response": "媒体库返回的分页数据格式无效",
    "Media server did not return a valid total record count": "媒体库未返回有效的条目总数",
    "Media server inventory changed during pagination; retry required": "读取期间媒体库内容发生变化，请重新同步",
    "Media server returned inconsistent pagination": "媒体库返回的分页信息不一致",
    "Media server returned invalid item data": "媒体库返回的条目数据无效",
    "Media server returned an incomplete page": "媒体库返回的分页内容不完整",
    "Media server pagination limit exceeded; no changes allowed": "媒体库内容超过读取上限，本次未作修改",
    "Media server returned missing or repeated entry identity": "媒体库条目缺少唯一编号，或编号重复",
    "Media server returned an item outside the requested Movie/Series type": "媒体库返回的内容不是请求的电影或电视剧",
    "Media server returned conflicting media identity": "媒体库返回的媒体身份存在冲突",
    "Media server returned a library without identity": "媒体库目录缺少 ID",
    "Media server returned invalid users": "媒体库返回的用户列表无效",
    "Media server username is ambiguous": "媒体库存在多个同名用户，请检查用户配置",
    "Media server did not return a collection ID": "媒体库未返回新建合集的 ID",
    "Media server did not return a playlist ID": "媒体库未返回新建播放列表的 ID",
    "playlist index cannot be negative": "播放位置不能为负数",
    "playlist index must be a non-negative integer": "播放位置必须是非负整数",
    "media_type must be all, movie or tv": "请选择全部、电影或电视剧",
    "TMDB ID must be a positive integer": "TMDB 编号必须是正整数",
    "media_type must be movie or tv (Series); Season/Episode are unsupported": "媒体类型必须是电影或电视剧，不能将季或单集作为独立作品添加",
    "member key must contain movie: or tv:": "媒体编号必须包含电影或电视剧的类型前缀",
    "year must be an integer or null": "年份必须是整数或留空",
    "year must be between 1 and 9999": "年份必须在 1 到 9999 之间",
    "TMDB collection mode must be once or follow": "TMDB 合集导入方式必须是仅导入一次或持续跟踪",
    "TMDB collections only contain movies": "TMDB 合集只能包含电影",
    "target.server_id cannot be empty": "请选择媒体服务器",
    "unsupported server kind": "不支持此媒体服务器类型",
    "invalid media server connection fingerprint": "媒体服务器连接标识无效，请重新选择服务器",
    "invalid target bindings": "媒体库同步的绑定信息无效",
    "duplicate playlist ID": "播放列表 ID 重复",
    "remote output cannot be bound twice in a target": "同一组同步设置不能重复绑定同一个合集或播放列表",
    "list.name cannot be empty": "片单名称不能为空",
    "revision must be a non-negative integer": "片单版本号必须是非负整数",
    "invalid media member": "片单中的媒体信息无效",
    "invalid source or target": "片单来源或媒体库同步设置无效",
    "duplicate TMDB source": "TMDB 合集来源重复",
    "duplicate target ID": "媒体库同步设置的 ID 重复",
    "invalid member": "媒体信息无效",
    "cron must have five fields": "执行周期必须包含分钟、小时、日期、月份和星期五个字段",
    "invalid cron field": "执行周期的字段格式无效",
    "invalid cron step": "执行周期的间隔无效",
    "invalid cron value": "执行周期的数值超出允许范围",
    "invalid cron range": "执行周期的起止范围无效",
    "legacy TMDB IDs must be text or an array": "旧版 TMDB 编号必须是文本或列表",
    "legacy rule must be an object": "旧版规则格式无效",
    "legacy movies must be an array": "旧版影片内容必须是列表",
    "legacy collection must be an object": "旧版合集设置格式无效",
    "legacy playlists must be an array": "旧版播放列表设置必须是列表",
    "legacy playlist must be an object": "旧版播放列表格式无效",
    "legacy library_ids must be an array": "旧版媒体库目录必须是列表",
    "plugin config must be an object": "插件配置格式无效",
    "unsupported config schema": "不支持此版本的插件配置",
    "lists must be an array": "片单内容必须是列表",
    "legacy rules must be an array": "旧版规则必须是列表",
    "schedule must be an object": "自动任务设置格式无效",
    "legacy daily_time must be HH:MM": "旧版每日执行时间必须为“小时:分钟”格式",
    "duplicate list ID": "片单 ID 重复",
    "enabled must be boolean": "启用状态必须为开启或关闭",
    "another publish is already running": "已有同步任务正在执行，请稍后重试",
    "Media server did not return a complete item list": "媒体库返回的成员列表不完整",
    "Unsupported remote identity; publication stopped": "媒体库包含无法识别的媒体身份，本次同步已停止",
    "Episode identity is incomplete; publication stopped": "媒体库中的单集信息不完整，本次同步已停止",
    "Season identity is incomplete; publication stopped": "媒体库中的季信息不完整，本次同步已停止",
    "Invalid remote TMDB identity": "媒体库中的 TMDB 编号无效",
    "Missing or repeated remote entry identity": "媒体库成员编号缺失或重复",
    "Publication cancelled": "同步已取消",
    "Publication timed out": "同步超时，请稍后重试",
    "Media server did not return a binding ID": "媒体库未返回新建对象的绑定 ID",
    "Remote binding persistence failed; publication stopped": "媒体库绑定信息保存失败，本次同步已停止",
    "dry_run and allow_empty must be boolean": "预览和允许清空的选项必须为开启或关闭",
    "Media server adapter is unsupported": "不支持此媒体服务器",
    "Collections are unsupported by this server": "此媒体服务器不支持合集",
    "Ordered playlists are unsupported by this server": "此媒体服务器不支持播放列表排序",
    "This server does not support the configured playlist media types": "此媒体服务器的播放列表不支持所选媒体类型",
    "Empty publication requires explicit allow_empty": "片单为空，如需清空媒体库中的内容，请明确允许清空",
    "Bound playlist contains unsupported media identities": "已绑定的播放列表包含不支持的媒体类型",
    "No configured media matched; all writes skipped": "媒体库中未找到片单内容，本次未作修改",
    "Published membership could not be verified; removals skipped": "无法确认内容已加入媒体库，已跳过移除",
    "Remote contents changed concurrently; removals skipped": "同步期间媒体库内容被其他操作修改，已跳过移除",
    "Remote identities changed concurrently; removals skipped": "同步期间媒体库成员身份发生变化，已跳过移除",
    "Playlist entries changed concurrently; removals skipped": "同步期间播放列表成员发生变化，已跳过移除",
    "Playlist identities changed while sorting; removals skipped": "排序期间播放列表成员身份发生变化，已跳过移除",
    "Playlist order could not be verified; removals skipped": "无法确认播放列表顺序，已跳过移除",
    "Remote contents changed before removal; removals skipped": "移除前媒体库内容发生变化，已跳过移除",
    "Media server did not apply the removal": "媒体库未完成移除操作，请重新同步",
    "Final published membership could not be verified": "无法确认同步后的媒体库内容，请重新同步",
    "Final playlist order could not be verified": "无法确认同步后的播放列表顺序，请重新同步",
    "Media server operation failed": "媒体库操作失败，请稍后重试",
    "members must be an array": "片单内容必须是列表",
    "storage rejected": "存储未接受本次保存",
    "Recovered an invalid legacy minute cron from its preserved interval.": "已根据保留的执行间隔修复旧版周期设置。",
}

FIELD_NAMES = {
    "member": "媒体信息", "source": "片单来源", "target": "媒体库同步设置", "list": "片单",
    "policy": "自动任务设置", "collection": "合集设置", "playlist": "播放列表",
    "members": "片单内容", "sources": "片单来源", "targets": "媒体库同步设置",
    "playlists": "播放列表", "library_ids": "媒体库目录", "order": "播放顺序",
    "seen_keys": "已导入的媒体编号", "enabled": "启用状态", "auto_subscribe": "自动订阅",
    "collection.enabled": "合集启用状态", "playlist.enabled": "播放列表启用状态",
    "list.enabled": "片单启用状态", "playlist.order": "播放顺序", "publish_on_library": "入库后自动同步",
    "sync_on_library_change": "入库后自动同步", "subscribe_missing": "自动订阅",
}


def field_name(value: str) -> str:
    return FIELD_NAMES.get(value, "配置项")


def diagnostic(value, *, fallback=False) -> str:
    """Translate known legacy text; never guess translations for user content."""
    if not isinstance(value, str) or not value:
        return value if isinstance(value, str) else ""
    if value in LEGACY_MESSAGES:
        return LEGACY_MESSAGES[value]
    match = re.fullmatch(r"Media server returned HTTP (\d{3})", value)
    if match:
        return f"媒体服务器返回 HTTP {match[1]}，请检查服务器状态和访问权限"
    match = re.fullmatch(r"(.+) must be (an object|an array|boolean)", value)
    if match:
        suffix = {"an object": "格式无效", "an array": "必须是列表", "boolean": "必须为开启或关闭"}[match[2]]
        return field_name(match[1]) + suffix
    match = re.fullmatch(r"Legacy (\d+) minute interval retained as migrated_interval_minutes; scheduler must use the interval until cron is changed\.", value)
    if match:
        return f"已保留旧版每 {match[1]} 分钟执行的设置，修改执行周期后将使用新设置。"
    if fallback and not re.search(r"[\u3400-\u9fff]", value):
        return "操作失败，请稍后重试"
    return value


def public_diagnostics(value):
    """Localize response copies without changing saved history or domain data."""
    if isinstance(value, list):
        return [public_diagnostics(item) for item in value]
    if not isinstance(value, dict):
        return copy.deepcopy(value)
    result = {}
    for key, item in value.items():
        if key in {"error", "message"} and isinstance(item, str):
            result[key] = diagnostic(item, fallback=key == "error")
        elif key == "migration_warnings" and isinstance(item, list):
            result[key] = [diagnostic(row, fallback=True) for row in item]
        else:
            result[key] = public_diagnostics(item)
    return result
