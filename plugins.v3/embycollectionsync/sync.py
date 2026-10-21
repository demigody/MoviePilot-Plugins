"""Read, plan, add, verify, reorder, then remove: never delete to recover."""
from __future__ import annotations

import threading
import time
from bisect import bisect_left
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterable

from .adapters import MediaServerAdapter
from .emby import EmbyAPIError, EmbyEpisode, EmbyItem, EmbySeason
from .playback import playlist_content, collection_content
from .models import CollectionBinding, MediaList, Playlist, Target, collection_disabled_reason, positive_id, text
from .library_sync import pending_keys


class SyncInProgress(RuntimeError):
    pass


class SyncLock:
    def __init__(self) -> None:
        self._lock = threading.Lock()

    def acquire(self) -> bool:
        return self._lock.acquire(blocking=False)

    def release(self) -> None:
        self._lock.release()

    def __enter__(self) -> "SyncLock":
        if not self.acquire():
            raise SyncInProgress("已有同步任务正在执行，请稍后重试")
        return self

    def __exit__(self, *_: Any) -> None:
        self.release()


@dataclass
class _Output:
    kind: str
    binding: CollectionBinding | Playlist
    name: str
    existing: list[EmbyItem]
    desired: list[EmbyItem]
    additions: list[EmbyItem]
    removals: list[EmbyItem]
    verified: list[EmbyItem] = field(default_factory=list)
    missing_remote_id: str | None = None

    @property
    def id(self) -> str:
        return self.binding.id if isinstance(self.binding, Playlist) else "collection"

    @property
    def remote_id(self) -> str | None:
        return None if self.missing_remote_id else self.binding.remote_id

    def plan(self) -> dict[str, Any]:
        return {"kind": self.kind, "id": self.id, "remote_id": self.remote_id, "missing_remote_id": self.missing_remote_id, "name": self.name, "create": self.remote_id is None and bool(self.desired), "add": [item.id for item in self.additions], "remove": [item.playlist_item_id if self.kind == "playlist" else item.id for item in self.removals], "order": [item.id for item in self.desired] if self.kind == "playlist" else []}


class Publisher:
    """Publish one independent target, retaining IDs immediately after create.

``on_binding()`` must persist the updated target or raise. Once created, a
binding is never rolled back in memory after a later server failure. This
allows retries to reconcile the same remote object instead of duplicating it.
"""

    def __init__(self, client: MediaServerAdapter, *, timeout: float = 120) -> None:
        self.client = client
        self.timeout = min(max(float(timeout), 0.1), 600)

    @staticmethod
    def _items(values: Any, *, playlist: bool = False, playback: bool = False, collection: bool = False) -> list[EmbyItem]:
        if not isinstance(values, list):
            raise EmbyAPIError("媒体库返回的成员列表不完整")
        items = []
        identities = set()
        for value in values:
            item = value if isinstance(value, EmbyItem) else EmbyItem.from_dict(value)
            allowed = {"movie", "tv", "episode"} if playlist or playback else {"movie", "tv", "season"} if collection else {"movie", "tv"}
            if not item.id or item.media_type not in allowed:
                raise EmbyAPIError("媒体库包含无法识别的媒体身份，本次同步已停止", code="unsupported")
            if item.media_type == "episode" and not isinstance(item, EmbyEpisode):
                raise EmbyAPIError("媒体库中的单集信息不完整，本次同步已停止")
            if item.media_type == "season" and not isinstance(item, EmbySeason):
                raise EmbyAPIError("媒体库中的季信息不完整，本次同步已停止")
            if item.tmdb_id is not None:
                try:
                    positive_id(item.tmdb_id)
                except ValueError as error:
                    raise EmbyAPIError("媒体库中的 TMDB 编号无效") from error
            identity = item.playlist_item_id if playlist else item.id
            if not identity or identity in identities:
                raise EmbyAPIError("媒体库成员编号缺失或重复")
            identities.add(identity)
            items.append(item)
        return items

    @staticmethod
    def _plan(kind: str, binding: CollectionBinding | Playlist, name: str, existing: list[EmbyItem], keys: list[str], matches: dict[str, EmbyItem], *, clear: bool) -> _Output:
        by_key: dict[str, EmbyItem] = {}
        for item in existing:
            if item.key:
                by_key.setdefault(item.key, item)
        desired: list[EmbyItem] = []
        for key in keys:
            item = matches.get(key) or by_key.get(key)
            if item:
                desired.append(item)
        # Preserve unidentified manual entries during normal reconciliation.
        # Explicit empty publication is the only operation that clears them.
        if not clear:
            desired.extend(item for item in existing if item.key is None)
        wanted_ids = {item.id for item in desired}
        existing_ids = {item.id for item in existing}
        additions = [item for item in desired if item.id not in existing_ids]
        removals = []
        retained: set[str] = set()
        for item in existing:
            if item.key is None and not clear:
                # Unidentified manual entries are outside normal ownership,
                # including intentional repeated entries of the same item.
                continue
            if item.id not in wanted_ids or item.id in retained:
                removals.append(item)
            else:
                retained.add(item.id)
        return _Output(kind, binding, name, existing, desired, additions, removals)

    @staticmethod
    def _playlist_moves(existing: list[EmbyItem], desired: list[EmbyItem], removals: list[EmbyItem]):
        """保留最长已有顺序，只移动必要条目；待删项不占逻辑排序位置。"""
        rows = list(existing)
        removed = {item.playlist_item_id for item in removals}
        survivors = [item for item in rows if item.playlist_item_id not in removed]
        pool, goal = list(survivors), []
        for item in desired:
            index = next((i for i, row in enumerate(pool) if row.id == item.id and
                          (not item.playlist_item_id or row.playlist_item_id == item.playlist_item_id)), None)
            if index is None:
                raise EmbyAPIError("播放列表中缺少待同步的影片，请重新同步")
            goal.append(pool.pop(index))
        goal.extend(pool)
        positions = {row.playlist_item_id: i for i, row in enumerate(survivors)}
        tail_values, tail_indices, previous = [], [], [-1] * len(goal)
        for i, row in enumerate(goal):
            rank = positions[row.playlist_item_id]
            index = bisect_left(tail_values, rank)
            if index:
                previous[i] = tail_indices[index - 1]
            if index == len(tail_values):
                tail_values.append(rank)
                tail_indices.append(i)
            else:
                tail_values[index], tail_indices[index] = rank, i
        anchors = set()
        index = tail_indices[-1] if tail_indices else -1
        while index >= 0:
            anchors.add(goal[index].playlist_item_id)
            index = previous[index]
        moves, following = [], None
        for row in reversed(goal):
            if row.playlist_item_id not in anchors:
                old_position = rows.index(row)
                rows.remove(row)
                position = rows.index(following) if following is not None else len(rows)
                rows.insert(position, row)
                if old_position != position:
                    moves.append((row.playlist_item_id, position))
            following = row
        return moves, rows

    def _validate_creation_names(self, outputs: list[_Output], guard: Callable[[], None]) -> None:
        """完整检查待新建名称；不同输出类型分开比较，不按同名对象自动接管。"""
        pending: dict[tuple[str, str], _Output] = {}
        for output in outputs:
            if output.remote_id or not output.desired:
                continue
            identity = (output.kind, text(output.name).casefold())
            if identity in pending:
                raise EmbyAPIError(f"本次待新建的播放列表存在同名“{output.name}”，请修改名称后重新发布。", code="name_conflict")
            pending[identity] = output
        for kind in sorted({key[0] for key in pending}):
            guard()
            values = self.client.list_collections() if kind == "collection" else self.client.list_playlists()
            guard()
            if not isinstance(values, list):
                raise EmbyAPIError("媒体库输出列表读取不完整。")
            for value in values:
                item = value if isinstance(value, EmbyItem) else EmbyItem.from_dict(value)
                if not item.id or not text(item.name) or item.media_type is not None:
                    raise EmbyAPIError("媒体库输出列表缺少可靠的身份或名称。")
                output = pending.get((kind, text(item.name).casefold()))
                if output:
                    label = "合集" if kind == "collection" else "播放列表"
                    raise EmbyAPIError(f"媒体库已存在同名{label}“{output.name}”（ID：{item.id}），请修改名称后重新同步。", code="name_conflict")

    def publish(self, media_list: MediaList, target: Target, *, dry_run: bool = False, allow_empty: bool = False, on_binding: Callable[[], Any] | None = None, cancelled: Callable[[], bool] | None = None, expected_keys: set[str] | None = None, on_progress: Callable[[str], Any] | None = None) -> dict[str, Any]:
        result: dict[str, Any] = {"list_id": media_list.id, "list_name": media_list.name, "target_id": target.id, "server_id": target.server_id, "dry_run": bool(dry_run), "success": False, "status": "running", "error": None, "error_code": None, "matched_count": 0, "configured_count": len(media_list.members), "pending": [], "added": [], "removed": [], "plan": [], "collection_id": target.collection.remote_id, "playlist_ids": {p.id: p.remote_id for p in target.playlists if p.enabled}, "synced_at": datetime.now(timezone.utc).isoformat()}
        deadline = time.monotonic() + self.timeout
        writes = 0

        def report(stage: str) -> None:
            if on_progress:
                try:
                    on_progress(stage)
                except Exception:
                    # Display feedback never changes the write/verify barriers.
                    pass

        def guard() -> None:
            if cancelled and cancelled():
                raise EmbyAPIError("同步已取消", code="cancelled")
            if time.monotonic() >= deadline:
                raise EmbyAPIError("同步超时，请稍后重试", code="timeout")

        def read(operation: Callable[..., Any], *args: Any, playlist: bool = False, playback: bool = False, collection: bool = False) -> list[EmbyItem]:
            guard()
            value = self._items(operation(*args), playlist=playlist, playback=playback, collection=collection)
            guard()
            return value

        def write(operation: Callable[..., Any], *args: Any) -> Any:
            nonlocal writes
            guard()
            value = operation(*args)
            writes += 1
            guard()
            return value

        def bind(output: _Output, remote_id: str) -> None:
            if not remote_id:
                raise EmbyAPIError("媒体库未返回新建对象的绑定 ID")
            output.binding.remote_id = str(remote_id)
            output.missing_remote_id = None
            if output.kind == "collection":
                result["collection_id"] = str(remote_id)
            else:
                result["playlist_ids"][output.id] = str(remote_id)
            bind_target = getattr(self.client, 'bind_target', None)
            if callable(bind_target):
                bind_target(target)
            if on_binding:
                try:
                    on_binding()
                except Exception as error:
                    raise EmbyAPIError("媒体库绑定信息保存失败，本次同步已停止") from error

        try:
            guard()
            report("preflight")
            if not isinstance(dry_run, bool) or not isinstance(allow_empty, bool):
                raise EmbyAPIError("预览和允许清空的选项必须为开启或关闭", code="invalid_input")
            if not media_list.enabled:
                result.update(success=True, status="success", skipped=True)
                return result
            capabilities = self.client.capabilities
            if target.kind != self.client.kind or not capabilities.get("supported", False):
                raise EmbyAPIError("不支持此媒体服务器", code="unsupported")
            # Also guard callers that mutate an already validated model directly.
            reason = collection_disabled_reason(target.kind, media_list.members)
            if reason:
                target.collection.enabled = False
                result['collection_disabled_reason'] = reason
            enabled_playlists = [item for item in target.playlists if item.enabled]
            if target.collection.enabled and not capabilities.get("collections"):
                raise EmbyAPIError("此媒体服务器不支持合集", code="unsupported")
            if enabled_playlists:
                if not capabilities.get("playlists") or not capabilities.get("reorder"):
                    raise EmbyAPIError("此媒体服务器不支持播放列表排序", code="unsupported")
                allowed = set(capabilities.get("playlist_media_types", []))
                if any(item.media_type not in allowed for item in media_list.members):
                    raise EmbyAPIError("此媒体服务器的播放列表不支持所选媒体类型", code="unsupported")
            if not target.collection.enabled and not enabled_playlists:
                result.update(success=True, status="success", skipped=True)
                return result
            validate_target = getattr(self.client, 'validate_target', None)
            if callable(validate_target):
                guard()
                validate_target(target)
                guard()
            if enabled_playlists:
                preflight = getattr(self.client, "check_playlist_access" if dry_run else "ensure_playlist_access", None)
                if callable(preflight):
                    guard()
                    preflight()
            if not media_list.members and not allow_empty:
                raise EmbyAPIError("片单为空，如需清空媒体库中的内容，请明确允许清空", code="invalid_input")
            report("inventory")
            inventory = read(self.client.list_media, target.library_ids, "all")
            valid_keys = set(media_list.member_keys)
            matches: dict[str, EmbyItem] = {}
            # Multiple versions of a movie resolve deterministically, never
            # by whatever order a paged server happens to return today.
            for item in sorted(inventory, key=lambda item: item.id):
                if item.key in valid_keys:
                    matches.setdefault(item.key, item)
            result["matched_count"] = len(matches)
            result["pending"] = [key for key in media_list.member_keys if key not in matches]
            episodes, seasons = [], []
            scoped = {member.key for member in media_list.members if member.seasons is not None}
            for item in matches.values():
                if item.media_type != "tv":
                    continue
                series_episodes = []
                if enabled_playlists or (target.collection.enabled and item.key in scoped) or any(key.startswith(item.key + ':') or key == item.key for key in expected_keys or set()):
                    report("episodes")
                    series_episodes = read(self.client.get_series_episodes, item, playback=True)
                    episodes.extend(series_episodes)
                if target.collection.enabled and item.key in scoped:
                    report("seasons")
                    present = {episode.season_number for episode in series_episodes}
                    seasons.extend(season for season in read(self.client.get_series_seasons, item, collection=True)
                                   if season.season_number in present)
            if expected_keys:
                result['library_pending'] = pending_keys(expected_keys, matches, episodes)
            report("plan")
            outputs: list[_Output] = []
            def plan_output(kind, binding, keys):
                missing_id = None
                existing = []
                if binding.remote_id:
                    get = self.client.get_collection_items if kind == "collection" else self.client.get_playlist_items
                    try:
                        existing = read(get, binding.remote_id, playlist=kind == "playlist", collection=kind == "collection")
                    except EmbyAPIError as error:
                        # 只把明确的对象 404 视为已删除；网络、权限或类型错误照常终止。
                        if error.code != "missing_output" or error.status != 404:
                            raise
                        missing_id = binding.remote_id
                output_matches = matches
                if kind == "playlist":
                    keys, output_matches = playlist_content(keys, media_list.members, matches, episodes, existing)
                else:
                    keys, output_matches = collection_content(media_list.members, matches, seasons, existing)
                output = self._plan(kind, binding, binding.name or media_list.name, existing, keys, output_matches, clear=not media_list.members)
                output.missing_remote_id = missing_id
                return output
            if target.collection.enabled:
                binding = target.collection
                outputs.append(plan_output("collection", binding, media_list.member_keys))
            for playlist in enabled_playlists:
                order = playlist.order
                output = plan_output("playlist", playlist, order)
                # Reject pre-existing unsupported entries before any writes.
                if any(item.media_type not in capabilities.get("playlist_media_types", []) for item in output.existing):
                    raise EmbyAPIError("已绑定的播放列表包含不支持的媒体类型", code="unsupported")
                outputs.append(output)
            result["plan"] = [output.plan() for output in outputs]
            playable_shows = {item.parent_key for output in outputs if output.kind == "playlist" for item in output.desired if isinstance(item, EmbyEpisode)}
            result["playback_pending"] = [key for key in matches if key.startswith("tv:") and key not in playable_shows] if enabled_playlists else []
            result["added"] = list(dict.fromkeys(item.key for output in outputs for item in output.additions if item.key))
            result["removed"] = list(dict.fromkeys(item.key for output in outputs for item in output.removals if item.key))
            if media_list.members and not matches:
                raise EmbyAPIError("媒体库中未找到片单内容，本次未作修改", code="no_matches")
            validate_collection = getattr(self.client, 'validate_collection', None)
            if callable(validate_collection):
                for output in outputs:
                    if output.kind == 'collection':
                        guard()
                        validate_collection([item.id for item in output.existing + output.desired], output.remote_id)
                        guard()
            self._validate_creation_names(outputs, guard)
            if dry_run:
                result.update(success=True, status="success")
                return result

            # Phase 1: add everywhere. If any add fails, no output is pruned.
            report("add")
            for output in outputs:
                if not output.remote_id:
                    if not output.desired:
                        continue
                    create = self.client.create_collection if output.kind == "collection" else self.client.create_playlist
                    # Persist a returned ID even when cancellation/deadline
                    # arrives during create. Never lose a successful create.
                    guard()
                    ids = [item.id for item in output.desired]
                    batch = 100 if output.kind == 'playlist' else capabilities.get('collection_batch_size', max(1, len(ids)))
                    remote_id = create(output.name, ids[:batch])
                    writes += 1
                    bind(output, remote_id)
                    guard()
                    add = self.client.add_playlist_items if output.kind == 'playlist' else self.client.add_collection_items
                    for start in range(batch, len(ids), batch):
                        write(add, output.remote_id, ids[start:start + batch])
                elif output.additions:
                    add = self.client.add_collection_items if output.kind == "collection" else self.client.add_playlist_items
                    ids = [item.id for item in output.additions]
                    batch = 100 if output.kind == "playlist" else capabilities.get('collection_batch_size', max(1, len(ids)))
                    for start in range(0, len(ids), batch):
                        write(add, output.remote_id, ids[start:start + batch])

            # Phase 2: verify every addition using the same typed identity.
            report("verify")
            for output in outputs:
                if not output.remote_id:
                    continue
                get = self.client.get_collection_items if output.kind == "collection" else self.client.get_playlist_items
                output.verified = read(get, output.remote_id, playlist=output.kind == "playlist", collection=output.kind == "collection")
                actual = {item.id: item for item in output.verified}
                missing = [item for item in output.desired if item.id not in actual or actual[item.id].key != item.key]
                if missing:
                    label = "合集" if output.kind == "collection" else "播放列表"
                    names = "、".join(item.name or item.key or item.id for item in missing[:5])
                    if len(missing) > 5:
                        names += f"等 {len(missing)} 项"
                    reason = None
                    explain = getattr(self.client, "explain_unverified_membership", None)
                    if callable(explain):
                        guard()
                        reason = explain(output.kind, output.remote_id, missing, timeout=deadline - time.monotonic())
                        guard()
                    reason = reason or "请检查媒体库中的条目和关联信息。"
                    raise EmbyAPIError(f"{label}未能确认已加入的内容：{names}。已跳过移除。{reason}", code="membership_unverified")
                expected_entries = {item.playlist_item_id if output.kind == "playlist" else item.id for item in output.existing}
                actual_entries = {item.playlist_item_id if output.kind == "playlist" else item.id for item in output.verified}
                if not expected_entries.issubset(actual_entries):
                    raise EmbyAPIError("同步期间媒体库内容被其他操作修改，已跳过移除")
                signatures = {item.playlist_item_id if output.kind == "playlist" else item.id: (item.id, item.key) for item in output.verified}
                if any(signatures.get(item.playlist_item_id if output.kind == "playlist" else item.id) != (item.id, item.key) for item in output.existing):
                    raise EmbyAPIError("同步期间媒体库成员身份发生变化，已跳过移除")

            # Phase 3: reorder in place BEFORE removals on any output.
            report("order")
            for output in outputs:
                if output.kind != "playlist" or not output.remote_id:
                    continue
                removal_entries = {item.playlist_item_id for item in output.removals}
                moves, rows = self._playlist_moves(output.verified, output.desired, output.removals)
                for entry_id, position in moves:
                    write(self.client.move_playlist_entry, output.remote_id, entry_id, position)
                refreshed = read(self.client.get_playlist_items, output.remote_id, playlist=True)
                if {item.playlist_item_id for item in refreshed} != {item.playlist_item_id for item in output.verified}:
                    raise EmbyAPIError("同步期间播放列表成员发生变化，已跳过移除")
                before_identity = {item.playlist_item_id: (item.id, item.key) for item in output.verified}
                after_identity = {item.playlist_item_id: (item.id, item.key) for item in refreshed}
                if before_identity != after_identity:
                    raise EmbyAPIError("排序期间播放列表成员身份发生变化，已跳过移除")
                survivors = [item for item in refreshed if item.playlist_item_id not in removal_entries]
                if (self.client.kind == 'plex' and
                        [item.id for item in survivors[:len(output.desired)]] != [item.id for item in output.desired]):
                    # PMS can acknowledge an interior move without applying it.
                    # Reposition the same entries via the tail once; never remove,
                    # recreate, or change membership to recover their positions.
                    for row in rows:
                        fresh = read(self.client.get_playlist_items, output.remote_id, playlist=True)
                        if {item.playlist_item_id: (item.id, item.key) for item in fresh} != before_identity:
                            raise EmbyAPIError("排序期间播放列表成员身份发生变化，已跳过移除")
                        if fresh[-1].playlist_item_id != row.playlist_item_id:
                            write(self.client.move_playlist_entry, output.remote_id, row.playlist_item_id, len(fresh) - 1)
                    refreshed = read(self.client.get_playlist_items, output.remote_id, playlist=True)
                    if {item.playlist_item_id: (item.id, item.key) for item in refreshed} != before_identity:
                        raise EmbyAPIError("排序期间播放列表成员身份发生变化，已跳过移除")
                    survivors = [item for item in refreshed if item.playlist_item_id not in removal_entries]
                if [item.id for item in survivors[:len(output.desired)]] != [item.id for item in output.desired]:
                    raise EmbyAPIError("播放列表顺序与片单设置不一致，请重新同步")
                output.verified = refreshed

            # Barrier after all sorting: another output may have changed
            # while the server processed a move. Revalidate every output
            # before starting removals on any output.
            if any(output.removals for output in outputs):
                for output in outputs:
                    if not output.remote_id:
                        continue
                    get = self.client.get_collection_items if output.kind == "collection" else self.client.get_playlist_items
                    fresh = read(get, output.remote_id, playlist=output.kind == "playlist", collection=output.kind == "collection")
                    before = {item.playlist_item_id if output.kind == "playlist" else item.id: (item.id, item.key) for item in output.verified}
                    after = {item.playlist_item_id if output.kind == "playlist" else item.id: (item.id, item.key) for item in fresh}
                    if before != after or (output.kind == "playlist" and [item.playlist_item_id for item in fresh] != [item.playlist_item_id for item in output.verified]):
                        raise EmbyAPIError("移除前媒体库内容发生变化，已跳过移除")

            # Phase 4: remove only pre-read stale entries. Newly concurrent
            # entries never become deletion candidates during this run.
            report("remove")
            for output in outputs:
                if output.removals and output.remote_id:
                    remove = self.client.remove_collection_items if output.kind == "collection" else self.client.remove_playlist_entries
                    ids = [item.id if output.kind == "collection" else item.playlist_item_id for item in output.removals]
                    batch = capabilities.get('removal_batch_size', 100 if output.kind == "playlist" else max(1, len(ids)))
                    for start in range(0, len(ids), batch):
                        write(remove, output.remote_id, ids[start:start + batch])
            # A 2xx response alone does not prove the requested mutation.
            report("verify_final")
            for output in outputs:
                if not output.remote_id:
                    continue
                get = self.client.get_collection_items if output.kind == "collection" else self.client.get_playlist_items
                final = read(get, output.remote_id, playlist=output.kind == "playlist", collection=output.kind == "collection")
                removed_ids = {item.id if output.kind == "collection" else item.playlist_item_id for item in output.removals}
                if any((item.id if output.kind == "collection" else item.playlist_item_id) in removed_ids for item in final):
                    raise EmbyAPIError("媒体库未完成移除操作，请重新同步")
                final_ids = {item.id: item.key for item in final}
                if any(final_ids.get(item.id) != item.key or item.id not in final_ids for item in output.desired):
                    raise EmbyAPIError("无法确认同步后的媒体库内容，请重新同步")
                if output.kind == "playlist" and [item.id for item in final[:len(output.desired)]] != [item.id for item in output.desired]:
                    raise EmbyAPIError("无法确认同步后的播放列表顺序，请重新同步")
            result.update(success=True, status="success")
        except Exception as error:
            code = error.code if isinstance(error, EmbyAPIError) else "server_error"
            result.update(success=False, status="cancelled" if code == "cancelled" else "partial" if writes else "error", error=str(error) if isinstance(error, EmbyAPIError) else "媒体库操作失败，请稍后重试", error_code=code)
        return result


SyncEngine = Publisher
