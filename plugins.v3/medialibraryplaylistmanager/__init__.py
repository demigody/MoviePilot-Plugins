"""MoviePilot V3 media lists. Host chains own recognition and subscriptions."""
from __future__ import annotations
import copy
import hashlib
import json
import re
import threading
from functools import wraps
from datetime import datetime, timezone
from uuid import uuid4
from fastapi import Body, Depends
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

try:
    from app.sdk.plugin import _PluginBase
    from app.sdk.events import eventmanager
    from app.schemas.types import EventType
    from app.api.dependencies.auth import get_current_active_manage_user as _manage_user
except ModuleNotFoundError as error:
    if error.name != "app":
        raise
    # An isolated test harness, not an older-host compatibility path.
    class _PluginBase:
        def update_config(self, config):
            self._stored_config = copy.deepcopy(config)
        def save_data(self, key, value):
            setattr(self, "_data_" + key, copy.deepcopy(value))
        def get_data(self, key):
            return copy.deepcopy(getattr(self, "_data_" + key, None))
    eventmanager = EventType = None
    def _manage_user():
        return True

from .emby import EmbyClient, EmbyAPIError, EmbySeason
from .jellyfin import JellyfinClient, JellyfinAuthError
from .plex import PlexClient
from .jellyfin_auth import JellyfinSessions
from .host import Host, normalize_type, object_dict
from .migration import migrate_config
from .models import MediaList, MediaMember, Source, positive_id, validate_bindings
from .requests import ProviderCalls
from .sync import Publisher
from .library_sync import LibrarySyncBatcher, transfer_keys, expected_for
from .messages import diagnostic, public_diagnostics
from .import_export import DEFAULT_TRAKT_CLIENT_ID, export_document, import_trakt_list, parse_standard_file

SERVER_CLIENTS = {"emby": EmbyClient, "jellyfin": JellyfinClient, "plex": PlexClient}

# MoviePilot's subscription chain may perform additional metadata/database work;
# keep this separate from short lookups so a slow but valid subscription isn't
# mistaken for a failed request after the generic five-second provider timeout.
SUBSCRIPTION_TIMEOUT_SECONDS = 60
# MP removes a normal subscription as soon as its download batch is accepted.
# Retain an upper bound to avoid permanent suppression after a failed download.
SUBSCRIPTION_CLAIM_TTL_SECONDS = 72 * 60 * 60
# Allow MP time to hand an accepted subscription to the downloader before
# trusting an empty download lookup and submitting again.
SUBSCRIPTION_START_GRACE_SECONDS = 30 * 60

def now():
    return datetime.now(timezone.utc).isoformat()

def failure(message, code="invalid_input", **extra):
    return {"success": False, "status": "error", "error": diagnostic(message, fallback=True), "error_code": code, **extra}

def nonblocking_edit(fn):
    @wraps(fn)
    def wrapped(self, *args, **kwargs):
        if not self._data_lock.acquire(blocking=False):
            return failure("片单正在更新，请稍后重试", "busy", status="busy")
        try:
            return fn(self, *args, **kwargs)
        finally:
            self._data_lock.release()
    return wrapped

class MediaLibraryPlaylistManager(_PluginBase):
    plugin_name = "媒体库片单管理"
    plugin_name_en = "Media Library Playlist Manager"
    plugin_desc = (
        "管理电影和电视剧片单，支持 TMDB 合集一次性导入与持续关注；"
        "支持 Emby、Jellyfin、Plex，按媒体库同步合集或播放列表，"
        "并提供预览、创建、增删、校验、排序和重排，可选自动订阅及入库后自动同步。"
    )
    plugin_icon = "medialibraryplaylistmanager.png"
    plugin_version = "1.0.1"
    plugin_author = "demigody"
    author_url = "https://github.com/demigody"
    plugin_config_prefix = "medialibraryplaylistmanager_"
    plugin_order = 50
    auth_level = 1

    def __init__(self):
        super().__init__()
        self._config, self._lists = {}, []
        self._config_error = ""
        self._binding_error = ""
        self._data_lock = threading.RLock()
        self._publish_lock = threading.Lock()
        self._job_lock = threading.Lock()
        self._reload_lock = threading.RLock()
        self._reload_pending = None
        self._reload_thread = None
        self._stop = threading.Event()
        self._provider_slots = threading.BoundedSemaphore(2)
        self._calls = ProviderCalls(slots=self._provider_slots)
        self._host = Host()
        self._clients, self._jobs = {}, {}
        self._client_configs = {}
        self._library_catalog = {}
        self._jellyfin_sessions = None
        self._history, self._subscriptions, self._subscription_claims = [], {}, {}
        self._history_revision, self._history_error = 0, ""
        self._timer = None
        self._library_sync = None

    def init_plugin(self, config=None):
        # A reload cancels the old generation but must retain its domain and
        # binding callback until its last in-flight server call has finished.
        with self._reload_lock:
            acquired = self._data_lock.acquire(blocking=False)
            if not acquired or self._publish_lock.locked():
                if acquired:
                    self._data_lock.release()
                self.stop_service()
                self._reload_pending = copy.deepcopy(config or {})
                if self._reload_thread is None or not self._reload_thread.is_alive():
                    def reload_after_job():
                        with self._publish_lock:
                            pass
                        with self._data_lock, self._reload_lock:
                            pending, self._reload_pending = self._reload_pending, None
                            if pending is not None:
                                self._initialize(self._retain_bindings(pending))
                    self._reload_thread = threading.Thread(target=reload_after_job, daemon=True, name="playlist-reload")
                    self._reload_thread.start()
                return
            try:
                self._reload_pending = None
                self._initialize(config)
            finally:
                self._data_lock.release()

    def _initialize(self, config):
        self.stop_service()
        self._stop = threading.Event()
        self._calls = ProviderCalls(slots=self._provider_slots)
        self._config_error = ""
        self._binding_error = ""
        original = copy.deepcopy(config or {})
        try:
            if original.get("schema_version") != 3 and self.get_data("migration_backup_v2") is None:
                self.save_data("migration_backup_v2", {"saved_at": now(), "config": original})
            migrated = migrate_config(original)
            self._validate_cron(migrated.get("cron") or "*/30 * * * *")
            lists = [MediaList.from_dict(item) for item in migrated.get("lists", [])]
            self._validate_list_ids(lists)
            try:
                validate_bindings(lists)
            except ValueError as error:
                # Keep otherwise valid saved lists visible so the user can repair
                # a stale cross-list binding instead of seeing a misleading empty page.
                self._binding_error = diagnostic(str(error), fallback=True)
                migrated["enabled"] = False
        except (TypeError, ValueError) as error:
            self._config, self._lists, self._config_error = original, [], diagnostic(str(error), fallback=True)
            return
        self._config, self._lists = migrated, lists
        self._config.pop("trakt_client_id", None)
        with self._job_lock:
            try:
                self._load_history()
            except Exception:
                self._history_error = "运行记录读取或升级失败，请重新加载后重试。"
        self._subscriptions = self.get_data("subscriptions") or {}
        claims = self.get_data("subscription_claims") or {}
        self._subscription_claims = claims if isinstance(claims, dict) else {}
        self._persist()
        generation = self._stop
        def save_sessions(data):
            with self._reload_lock:
                if generation.is_set() or self._stop is not generation:
                    raise JellyfinAuthError("Jellyfin 授权已取消，请在插件重新加载后重试。", code="cancelled")
                return self.save_data("jellyfin_sessions", data)
        self._jellyfin_sessions = JellyfinSessions(lambda: self.get_data("jellyfin_sessions"), save_sessions,
            cancelled=generation.is_set)
        self._library_sync = LibrarySyncBatcher(self._library_attempt, self._record_library_job)

    def _retain_bindings(self, config):
        if config.get("schema_version") != 3:
            return config
        raw_lists = config.get("lists", [])
        if not isinstance(raw_lists, list):
            return config
        old_lists = {item.id: item for item in self._lists}
        for raw_list in raw_lists:
            if not isinstance(raw_list, dict):
                continue
            old_list = old_lists.get(raw_list.get("id"))
            if old_list is None:
                continue
            if isinstance(raw_list.get("revision", 0), int) and raw_list.get("revision", 0) < old_list.revision:
                # Generic host form saves can carry a pre-job list snapshot.
                # Preserve the newer domain while applying top-level settings.
                raw_list.update(old_list.to_dict())
                continue
            old_targets = {target.id: target for target in old_list.targets}
            for target in raw_list.get("targets", []):
                if not isinstance(target, dict):
                    continue
                old = old_targets.get(target.get("id"))
                if not old or old.server_id != target.get("server_id") or old.kind != target.get("kind", "emby"):
                    continue
                if old.collection.remote_id:
                    target.setdefault("collection", {})["remote_id"] = old.collection.remote_id
                if old.connection_fingerprint:
                    target['connection_fingerprint'] = old.connection_fingerprint
                old_playlists = {playlist.id: playlist for playlist in old.playlists}
                for playlist in target.get("playlists", []):
                    bound = old_playlists.get(playlist.get("id"))
                    if bound and bound.remote_id:
                        playlist["remote_id"] = bound.remote_id
        return config

    @staticmethod
    def _validate_global_bindings(lists):
        MediaLibraryPlaylistManager._validate_list_ids(lists)
        validate_bindings(lists)

    @staticmethod
    def _validate_list_ids(lists):
        ids = set()
        for media_list in lists:
            if media_list.id in ids:
                raise ValueError("片单 ID 重复")
            ids.add(media_list.id)

    def stop_service(self):
        with self._reload_lock:
            self._stop.set()
            self._calls.stop()
            if self._library_sync:
                self._library_sync.stop()
            if self._jellyfin_sessions:
                self._jellyfin_sessions.stop()
            if self._timer:
                self._timer.cancel()
                self._timer = None
            self._clients.clear()
            self._client_configs.clear()
            self._library_catalog.clear()

    def get_state(self):
        return (bool(self._config.get("enabled")) and not self._config_error
                and not self._binding_error and not self._stop.is_set())

    def _persist(self):
        config = copy.deepcopy(self._config)
        config["lists"] = [item.to_dict() for item in self._lists]
        if self.update_config(config) is False:
            raise RuntimeError("插件配置保存失败")
        self._config = config

    def _commit_lists(self, lists):
        config = copy.deepcopy(self._config)
        config["lists"] = [item.to_dict() for item in lists]
        if self.update_config(config) is False:
            raise RuntimeError("插件配置保存失败")
        self._config, self._lists = config, lists
        try:
            validate_bindings(lists)
            self._binding_error = ""
        except ValueError as error:
            self._binding_error = diagnostic(str(error), fallback=True)

    @staticmethod
    def _has_media_target(media_list):
        return any(target.server_id and not target.pending_mapping for target in media_list.targets)

    def _target_labels(self, target, *, client=None):
        server_id = str(target.server_id or "")
        server_config = self._server_configs().get(server_id, {})
        target_name = str(server_config.get("name") or server_id or "媒体服务器")

        # The catalog is intentionally in memory and is cleared when the
        # plugin reloads. A publish can happen before the settings page has
        # refreshed it, so hydrate it on demand instead of showing library IDs
        # in the run history after every restart.
        if server_id not in self._library_catalog and client is not None:
            list_libraries = getattr(client, "list_libraries", None)
            if callable(list_libraries):
                try:
                    self._library_catalog[server_id] = [
                        object_dict(item) for item in (list_libraries() or [])
                    ]
                except Exception:
                    # Labels are supplementary; a name lookup failure must
                    # never block an otherwise valid media-library sync.
                    pass
        libraries = self._library_catalog.get(server_id, [])

        def normalize_id(value):
            return "".join(char for char in str(value or "").casefold() if char.isalnum())

        by_id = {
            normalize_id(row.get("id") or row.get("value")): str(row.get("name") or row.get("title") or "")
            for row in libraries if isinstance(row, dict) and (row.get("id") or row.get("value"))
        }
        fallback = [f"媒体库 {item}" for item in target.library_ids] if target.library_ids else ["全部媒体库"]
        if target.library_ids:
            names = [by_id.get(normalize_id(item)) or f"媒体库 {item}" for item in target.library_ids]
            return target_name, names or fallback
        names = [str(row.get("name") or row.get("title")) for row in libraries
                 if row.get("name") or row.get("title")]
        return target_name, names or fallback

    @staticmethod
    def _validate_cron(value):
        if len(str(value).split()) != 5:
            raise ValueError("执行周期必须是 5 位 Cron 表达式")
        CronTrigger.from_crontab(str(value))

    def _plugin_id(self):
        return self.__class__.__name__

    def _find(self, list_id):
        item = next((item for item in self._lists if item.id == str(list_id)), None)
        if item is None:
            raise ValueError("片单不存在")
        return item

    def _editable(self):
        if self._config_error:
            return failure(self._config_error, "migration_error")
        if self._publish_lock.locked():
            return failure("发布任务正在运行，请完成后再保存", "busy", status="busy")

    def _server_configs(self):
        try:
            servers = self._host.servers()
        except Exception:
            servers = {}
        private = self._config.get("servers") or {}
        if isinstance(private, list):
            private = {str(item.get("id") or item.get("name")): item for item in private}
        for key, conf in private.items():
            servers[str(key)] = {**conf, "kind": conf.get("kind", "emby")}
        legacy = self._config.get("emby") or self._config.get("server") or {}
        if not legacy:
            legacy = {"host": self._config.get("host") or self._config.get("emby_host"),
                "api_key": self._config.get("api_key") or self._config.get("emby_api_key"),
                "user_id": self._config.get("user_id") or self._config.get("emby_user_id")}
        if legacy.get("host") or legacy.get("url"):
            key = str(self._config.get("server_name") or legacy.get("name") or "Emby")
            servers[key] = {**legacy, "kind": "emby", "name": key}
        return servers

    def _client(self, server_id):
        with self._reload_lock:
            generation = self._stop
            if generation.is_set():
                raise EmbyAPIError("服务器请求已取消", code="cancelled")
            conf = copy.deepcopy(self._server_configs().get(str(server_id)))
            if server_id in self._clients and (server_id not in self._client_configs or self._client_configs[server_id] == conf):
                return self._clients[server_id]
            self._clients.pop(server_id, None)
            sessions = self._jellyfin_sessions
        if not conf:
            raise ValueError("媒体服务器未配置，请先在 MoviePilot 配置媒体服务器")
        kind = str(conf.get("kind") or "").casefold()
        client_class = SERVER_CLIENTS.get(kind)
        if client_class is None:
            raise EmbyAPIError("当前媒体服务器类型暂不支持同步", code="unsupported")
        host = conf.get("host") or conf.get("url")
        key = (conf.get('token') if kind == 'plex' else None) or conf.get("api_key") or conf.get("apikey") or conf.get("token") or ((conf.get("access_token") or conf.get("user_token")) if kind == "jellyfin" else None)
        user = conf.get("user_id") or conf.get("userid") or conf.get("user")
        if not host or not key:
            raise ValueError("媒体服务器地址或访问凭据缺失")
        client = client_class(str(host), str(key), str(user or ""), timeout=10)
        if kind == "jellyfin":
            try:
                token_user = client.resolve_authenticated_user_id()
            except EmbyAPIError as error:
                if error.status == 401:
                    raise JellyfinAuthError("MoviePilot 中的 Jellyfin 访问凭据无效或已撤销，请检查 API 密钥。", code="authorization_credentials_invalid", status=401) from error
                raise JellyfinAuthError("Jellyfin 用户身份检查失败，请检查 MP 配置的地址、网络和认证方式。", code='timeout' if error.code == 'timeout' else 'server_error', status=error.status) from error
            if token_user:
                if user and str(user).replace("-", "").casefold() != str(token_user).replace("-", "").casefold():
                    raise JellyfinAuthError("Jellyfin 用户令牌与 MP 配置的用户不一致，请检查媒体服务器用户设置。", code="authorization_user_mismatch")
                user = token_user
                client.user_id = str(user)
        if not user and kind != 'plex' and conf.get("username"):
            try:
                user = client.resolve_user_id(str(conf["username"]))
            except EmbyAPIError as error:
                if kind != 'jellyfin':
                    raise
                if error.status == 401:
                    message, code = "Jellyfin 用户查询失败：MP 的 API 密钥无效或已撤销，请检查媒体服务器设置。", 'authorization_credentials_invalid'
                elif error.status == 403:
                    message, code = "Jellyfin 拒绝读取用户列表，请确认 MP 使用的是后台创建的 API 密钥。", 'authorization_denied'
                elif error.status == 404:
                    message, code = "Jellyfin 用户查询接口不存在，请检查 MP 的服务器地址和反向代理路径。", 'authorization_unsupported'
                else:
                    message, code = "Jellyfin 用户查询未完成，请检查服务器连接后重试。", 'timeout' if error.code == 'timeout' else 'server_error'
                raise JellyfinAuthError(message, code=code, status=error.status) from error
            client.user_id = str(user or "")
        if not user and kind == "jellyfin" and not conf.get("username"):
            user = conf.get("default_user_id")
            client.user_id = str(user or "")
        if not user and kind != 'plex':
            if kind == 'jellyfin':
                raise JellyfinAuthError("未找到 MP 配置的 Jellyfin 用户，请检查用户名或媒体服务器连接。", code="authorization_user_missing")
            raise ValueError("媒体服务器用户 ID 缺失，请检查用户名或用户凭据")
        if kind == 'jellyfin' and sessions:
            def token_provider(source, force):
                rejected = getattr(getattr(source, '_playlist_user', None), 'api_key', None) if force else None
                return sessions.get(server_id, source, rejected_token=rejected)
            client.token_provider = token_provider
            client.authorization_check = lambda source: sessions.check(server_id, source)
        with self._reload_lock:
            if generation.is_set() or self._stop is not generation or self._server_configs().get(str(server_id)) != conf:
                raise EmbyAPIError("服务器配置已更新，请重试", code="cancelled")
            self._clients[server_id] = client
            self._client_configs[server_id] = copy.deepcopy(conf)
            return client

    def api_settings(self):
        return {"success": not bool(self._config_error), "enabled": self.get_state(),
            "cron": self._config.get("cron", "*/30 * * * *"), "error": self._config_error or None,
            "binding_error": self._binding_error or None,
            "migration_warnings": [diagnostic(row, fallback=True) for row in self._config.get("migration_warnings", [])],
            "migrated_interval_minutes": self._config.get("migrated_interval_minutes"),
            "capabilities": {"organize_category": False, "native_actions": "requires_host_patch"}}

    @nonblocking_edit
    def api_save_settings(self, payload: dict = Body(...)):
        blocked = self._editable()
        if blocked:
            return blocked
        with self._data_lock:
            if self._editable():
                return self._editable()
            try:
                cron = str(payload.get("cron", self._config.get("cron", "*/30 * * * *"))).strip()
                self._validate_cron(cron)
            except (ValueError, TypeError) as error:
                return failure(str(error))
            enabled = payload.get("enabled", self._config.get("enabled"))
            if not isinstance(enabled, bool):
                return failure("启用状态必须为开启或关闭")
            if enabled and self._binding_error:
                return failure(self._binding_error, "duplicate_binding")
            old_cron = self._config.get("cron")
            candidate = copy.deepcopy(self._config)
            candidate.update(enabled=enabled, cron=cron)
            candidate.pop("trakt_client_id", None)
            if cron != old_cron:
                candidate.pop("migrated_interval_minutes", None)
            try:
                if self.update_config(candidate) is False:
                    raise RuntimeError("配置未保存")
            except Exception:
                return failure("设置未保存，请检查宿主配置存储后重试", "persistence_error")
            self._config = candidate
        try:
            from app.sdk.scheduler import update_plugin_job
        except ModuleNotFoundError:
            pass
        else:
            try:
                update_plugin_job(self._plugin_id())
            except Exception:
                return failure("设置已保存，但宿主定时任务刷新失败；请重新启用插件", "scheduler_error")
        return self.api_settings()

    def api_export(self, list_id: str = ""):
        try:
            document = export_document([item.to_dict() for item in self._lists], exported_at=now(), list_id=list_id or None)
            return {"success": True, "document": document}
        except ValueError as error:
            return failure(str(error), "not_found")

    def api_import_preview(self, payload: dict = Body(...)):
        try:
            source = str(payload.get("source") or "file")
            if source == "trakt":
                if not self._provider_slots.acquire(blocking=False):
                    return failure("外部请求正在处理，请稍后重试", "busy", status="busy")
                try:
                    imported = import_trakt_list(payload.get("url", ""), DEFAULT_TRAKT_CLIENT_ID)
                finally:
                    self._provider_slots.release()
                item = {"id": uuid4().hex, "name": imported["name"], "enabled": False, "revision": 0,
                    "members": imported["members"], "sources": [],
                    "policy": {"auto_subscribe": False, "publish_on_library": False, "organize_category": ""}, "targets": []}
                return {"success": True, "source": "trakt", "lists": [item], "unsupported": imported["unsupported"]}
            if source != "file":
                return failure("导入来源不受支持")
            raw = payload.get("document")
            lists = parse_standard_file(raw)
            return {"success": True, "source": "file", "lists": lists, "unsupported": []}
        except (ValueError, TypeError, KeyError) as error:
            return failure(str(error), "invalid_input")
        except Exception:
            return failure("读取导入内容失败，请检查文件或稍后重试", "provider_error")

    @nonblocking_edit
    def api_import_commit(self, payload: dict = Body(...)):
        blocked = self._editable()
        if blocked:
            return blocked
        with self._data_lock:
            if self._editable():
                return self._editable()
            try:
                raw_lists = payload.get("lists")
                if not isinstance(raw_lists, list) or not raw_lists or len(raw_lists) > 500:
                    raise ValueError("请选择至少一个片单导入")
                reserved_names = {item.name.casefold() for item in self._lists}
                imported = []
                for list_index, raw in enumerate(raw_lists):
                    if not isinstance(raw, dict):
                        raise ValueError("导入片单格式无效")
                    item_data = copy.deepcopy(raw)
                    item_data["name"] = str(item_data.get("name") or "").strip()
                    if not item_data["name"]:
                        raise ValueError("请输入导入片单名称")
                    if item_data["name"].casefold() in reserved_names:
                        raise ValueError(f"片单名称“{item_data['name']}”已存在，请修改名称后再导入")
                    reserved_names.add(item_data["name"].casefold())
                    item_data["id"] = uuid4().hex
                    item_data["revision"] = 0
                    item_data["enabled"] = False
                    item_data["policy"] = {"auto_subscribe": False, "publish_on_library": False, "organize_category": ""}
                    for target in item_data.get("targets", []):
                        if not isinstance(target, dict):
                            raise ValueError("导入的媒体库设置无效")
                        target["id"] = uuid4().hex
                        target["server_id"] = ""
                        target["kind"] = str(target.get("source_kind") or target.get("kind") or "emby").casefold()
                        if target["kind"] not in SERVER_CLIENTS:
                            target["kind"] = "emby"
                        target["library_ids"] = []
                        target["pending_mapping"] = True
                        target.pop("source_server_id", None)
                        target.pop("source_kind", None)
                        target.pop("source_library_ids", None)
                        target["connection_fingerprint"] = None
                        target["collection"] = {**(target.get("collection") or {}), "remote_id": None}
                        for playlist in target.get("playlists", []):
                            playlist["id"] = uuid4().hex
                            playlist["remote_id"] = None
                    item = MediaList.from_dict(item_data)
                    if item.targets and len(item.targets) != len(item_data.get("targets", [])):
                        raise ValueError("导入的媒体库设置无效")
                    imported.append(item)
                prospective = self._lists + imported
                self._validate_global_bindings(prospective)
                self._commit_lists(prospective)
                return {**self.api_lists(), "imported": [public_diagnostics(item.to_dict()) for item in imported]}
            except (TypeError, ValueError, KeyError) as error:
                return failure(str(error))
            except Exception:
                return failure("片单未导入，请检查宿主配置存储后重试", "persistence_error")

    def api_lists(self):
        # Read the last committed snapshot without blocking behind network I/O.
        if self._config_error:
            return failure(self._config_error, "migration_error", lists=[])
        return {"success": True, "lists": public_diagnostics(self._config.get("lists", [])),
            "binding_error": self._binding_error or None}

    @nonblocking_edit
    def api_save_lists(self, payload: dict = Body(...)):
        blocked = self._editable()
        if blocked:
            return blocked
        with self._data_lock:
            if self._editable():
                return self._editable()
            try:
                action, raw = payload.get("action", "save"), payload.get("list") or {}
                list_id = str(payload.get("list_id") or raw.get("id") or "")
                existing = next((item for item in self._lists if item.id == list_id), None)
                if existing and int(payload.get("revision", raw.get("revision", -1))) != existing.revision:
                    return failure("片单已更新，请刷新后重试", "conflict")
                if action == "set_enabled":
                    if existing is None:
                        return failure("片单不存在", "not_found")
                    enabled = payload.get("enabled")
                    if not isinstance(enabled, bool):
                        raise ValueError("片单启用状态必须是布尔值")
                    if enabled and self._binding_error:
                        return failure(self._binding_error, "duplicate_binding")
                    if enabled and not self._has_media_target(existing):
                        return failure("请先在片单的“媒体库同步”中选择媒体服务器并保存", "invalid_input")
                    if existing.enabled == enabled:
                        return {**self.api_lists(), "list": public_diagnostics(existing.to_dict())}
                    item = copy.deepcopy(existing)
                    item.enabled = enabled
                    item.revision += 1
                    self._commit_lists([item if entry is existing else entry for entry in self._lists])
                    return {**self.api_lists(), "list": public_diagnostics(item.to_dict())}
                if action == "delete":
                    if existing is None:
                        return failure("片单不存在", "not_found")
                    self._commit_lists([item for item in self._lists if item is not existing])
                    return self.api_lists()
                if action not in {"save", "create", "upsert"}:
                    raise ValueError("未知片单操作")
                raw_item = copy.deepcopy(raw)
                configured_targets = any(isinstance(target, dict) and target.get("server_id") and not target.get("pending_mapping")
                                         for target in raw_item.get("targets") or [])
                if "enabled" not in raw_item and not configured_targets:
                    raw_item["enabled"] = False
                item = MediaList.from_dict(raw_item)
                if any(not target.server_id for target in item.targets):
                    raise ValueError("请先为每组媒体库同步设置选择媒体服务器")
                if item.enabled and not self._has_media_target(item):
                    raise ValueError("请先添加并选择媒体服务器，再启用片单")
                if existing:
                    old_targets = {target.id: target for target in existing.targets}
                    for target in item.targets:
                        old = old_targets.get(target.id)
                        if old and (old.server_id != target.server_id or old.kind != target.kind):
                            target.connection_fingerprint = None
                            target.collection.remote_id = None
                            for playlist in target.playlists:
                                playlist.remote_id = None
                    item.revision = existing.revision + 1
                    prospective = [item if entry is existing else entry for entry in self._lists]
                else:
                    item.revision = 0
                    prospective = self._lists + [item]
                self._validate_global_bindings(prospective)
                self._commit_lists(prospective)
                return {**self.api_lists(), "list": public_diagnostics(item.to_dict())}
            except (TypeError, ValueError, KeyError) as error:
                return failure(str(error))
            except Exception:
                return failure("片单未保存，请检查宿主配置存储后重试", "persistence_error")

    @nonblocking_edit
    def api_members(self, payload: dict = Body(...)):
        blocked = self._editable()
        if blocked:
            return blocked
        with self._data_lock:
            if self._editable():
                return self._editable()
            try:
                item = self._find(payload.get("list_id"))
                candidate = MediaList.from_dict(item.to_dict())
                if payload.get("action", "add") == "remove":
                    for key in payload.get("keys") or []:
                        candidate.remove_member(str(key))
                elif payload.get("action", "add") == "add":
                    members = [MediaMember.from_dict(raw) for raw in payload.get("items", [])]
                    for member in members:
                        candidate.add_member(member)
                else:
                    raise ValueError("未知成员操作")
                candidate.revision += 1
                self._commit_lists([candidate if entry is item else entry for entry in self._lists])
                return {"success": True, "list": public_diagnostics(candidate.to_dict())}
            except (TypeError, ValueError, KeyError) as error:
                return failure(str(error))
            except Exception:
                return failure("成员未保存，请检查宿主配置存储后重试", "persistence_error")

    def api_servers(self):
        return {"success": True, "items": [{"id": key, "value": key, "name": conf.get("name") or key,
            "title": conf.get("name") or key, "kind": conf.get("kind"), "configured": True,
            "capabilities": {"collections": str(conf.get("kind") or "").casefold() in SERVER_CLIENTS, "movie": str(conf.get("kind") or "").casefold() in SERVER_CLIENTS,
                "tv": str(conf.get("kind") or "").casefold() in SERVER_CLIENTS, "playlists": str(conf.get("kind") or "").casefold() in SERVER_CLIENTS, "tv_playlist": str(conf.get("kind") or "").casefold() in SERVER_CLIENTS, "playlist_grouping": "member",
                "mixed_collection": str(conf.get('kind') or '').casefold() in {'emby', 'jellyfin'},
                "season_collection": str(conf.get('kind') or '').casefold() in {'emby', 'jellyfin'},
                "cross_library_collection": str(conf.get('kind') or '').casefold() in {'emby', 'jellyfin'}}}
            for key, conf in self._server_configs().items()]}

    def _candidates(self, fn, payload, provider="metadata", *, allow_unmapped=False, timeout=None):
        result = self._calls.call(fn, request_id=str(payload.get("request_id") or ""), provider=provider, timeout=timeout)
        if not result["success"]:
            return {**result, "items": []}
        excluded = set(payload.get("exclude_keys") or [])
        if payload.get("list_id"):
            stored = next((item for item in self._config.get("lists", []) if item["id"] == payload["list_id"]), None)
            if stored is None:
                return failure("片单不存在", "not_found", items=[])
            excluded.update(MediaList.from_dict(stored).member_keys)
        items, seen, all_keys, errors = [], set(), [], []
        for raw in result.pop("value") or []:
            if allow_unmapped and not raw.get("tmdb_id"):
                kind = normalize_type(raw.get("media_type"))
                source, source_id = str(raw.get("media_source") or "").strip(), str(raw.get("media_id") or "").strip()
                if not source or not source_id:
                    raise ValueError("搜索候选缺少来源身份")
                key = f"{kind}:{source}:{source_id}"
                record = {name: raw.get(name) for name in ("media_source", "media_id", "media_type", "title", "year", "poster", "source_refs")}
            else:
                item = MediaMember.from_dict(raw)
                key, record = item.key, item.to_dict()
                if allow_unmapped:
                    record.update({name: raw[name] for name in ("media_source", "media_id", "source_refs") if name in raw})
            if key not in seen:
                seen.add(key)
                all_keys.append(key)
                if key not in excluded:
                    items.append({**record, "key": key})
            elif key not in excluded and key.startswith("tv:"):
                existing = next(item for item in items if item["key"] == key)
                if existing.get("seasons") is not None and record.get("seasons") is not None:
                    existing["seasons"] = sorted(set(existing["seasons"] + record["seasons"]))
                else:
                    existing.pop("seasons", None)
        return {**result, "status": "success" if items else "empty", "items": items, "all_keys": all_keys, "errors": errors}

    def api_search(self, payload: dict = Body(...)):
        try:
            query = str(payload.get("query") or "").strip()
            if not query or len(query) > 200:
                raise ValueError("请输入 1–200 字符的搜索词")
            kind = payload.get("media_type", "all")
            if kind != "all":
                kind = normalize_type(kind)
            return self._candidates(lambda: self._host.search(query, kind), payload, allow_unmapped=True, timeout=300)
        except (TypeError, ValueError) as error:
            return failure(str(error), items=[])

    def api_cancel(self, payload: dict = Body(...)):
        return {"success": True, "status": "cancelled", "active": self._calls.cancel(str(payload.get("request_id") or ""))}

    def api_resolve(self, payload: dict = Body(...)):
        try:
            kind, raw = payload.get("media_type", "all"), payload.get("tmdb_ids") or []
            if kind != "all":
                kind = normalize_type(kind)
            if isinstance(raw, str):
                raw = [value for value in re.split(r"[\s,，;；]+", raw.strip()) if value]
            ids = list(dict.fromkeys(positive_id(value) for value in raw))
            if not ids or len(ids) > 100 or any(value <= 0 for value in ids):
                raise ValueError("请输入 1–100 个有效 TMDB ID")
        except (ValueError, TypeError) as error:
            return failure(str(error), items=[])
        request_id = str(payload.get("request_id") or uuid4().hex)
        calls = self._calls
        def resolve():
            items, errors = [], []
            for tmdb_id in ids:
                if not calls.is_active(request_id):
                    break
                found = []
                for candidate_type in ("movie", "tv") if kind == "all" else (kind,):
                    if not calls.is_active(request_id):
                        break
                    try:
                        found.append(self._host.resolve(tmdb_id, candidate_type))
                    except ValueError:
                        pass
                    except Exception:
                        errors.append({"tmdb_id": tmdb_id, "media_type": candidate_type, "error": "该类型解析失败，请稍后重试"})
                if found:
                    items.extend(found)
                else:
                    errors.append({"tmdb_id": tmdb_id, "error": "媒体解析失败或 ID 不存在"})
            return {"items": items, "errors": errors}
        result = calls.call(resolve, request_id=request_id, timeout=18)
        if result["success"]:
            value = result.pop("value")
            result.update(value)
            result["status"] = "partial" if value["errors"] and value["items"] else "error" if value["errors"] else "success"
            if not value["items"]:
                result.update(success=False, error="没有解析到媒体，请核对 TMDB ID 或稍后重试")
        else:
            result["items"] = []
        return result

    def api_tmdb_collection(self, payload: dict = Body(...)):
        try:
            tmdb_id, mode = positive_id(payload.get("tmdb_id")), payload.get("mode", "once")
            if tmdb_id <= 0 or mode not in {"once", "follow"}:
                raise ValueError("请输入合集 TMDB ID，并选择一次性导入或持续关注")
            result = self._candidates(lambda: self._host.collection(tmdb_id), payload)
            if result["success"]:
                result["source"] = Source(tmdb_id=tmdb_id, name=str(payload.get("name") or f"TMDB 合集 {tmdb_id}"),
                    mode=mode, seen_keys=result["all_keys"], last_checked=now()).to_dict()
            return result
        except (TypeError, ValueError) as error:
            return failure(str(error), items=[])

    def api_seasons(self, payload: dict = Body(...)):
        try:
            tmdb_id = positive_id(payload.get("tmdb_id"))
        except (TypeError, ValueError) as error:
            return failure(str(error), items=[])
        result = self._calls.call(lambda: self._host.seasons(tmdb_id), provider="seasons", timeout=18)
        if result["success"]:
            result["items"] = result.pop("value")
        else:
            result.update(items=[], error="未取得这部电视剧的季信息，请重试。")
        return result

    @staticmethod
    def _remote_member(item):
        if isinstance(item, EmbySeason):
            if not item.series_tmdb_id:
                return None
            return {"tmdb_id": item.series_tmdb_id, "media_type": "tv", "title": item.series_name,
                    "seasons": [item.season_number]}
        if not item.tmdb_id:
            return None
        return {"tmdb_id": item.tmdb_id, "media_type": item.media_type, "title": item.name, "year": item.year,
            "poster": item.poster if item.poster and str(item.poster).startswith("http") else None}

    def api_library_media(self, payload: dict = Body(...)):
        try:
            kind = payload.get("media_type", "all")
            if kind != "all":
                kind = normalize_type(kind)
            def fetch():
                client = self._client(str(payload.get("server_id") or ""))
                media = client.list_media(payload.get("library_ids") or [], media_type=kind)
                term = str(payload.get("query") or "").strip().casefold()
                return [record for item in media if (record := self._remote_member(item)) and
                    (not term or term in item.name.casefold() or term == str(item.tmdb_id))]
            return self._candidates(fetch, payload, provider="server:" + str(payload.get("server_id")))
        except (ValueError, TypeError) as error:
            return failure(str(error), items=[])

    def _server_query(self, payload, method, *args):
        server_id = str(payload.get("server_id") or "")
        scope = hashlib.sha256(json.dumps([self._server_configs().get(server_id), args], sort_keys=True, default=str).encode()).hexdigest()
        result = self._calls.call_shared(f"{server_id}:{method}:{scope}",
            lambda: getattr(self._client(server_id), method)(*args),
            request_id=str(payload.get("request_id") or ""), provider="server:" + server_id)
        if result["success"]:
            result["items"] = [object_dict(item) for item in result.pop("value") or []]
            result["status"] = "success" if result["items"] else "empty"
        else:
            result["items"] = []
        return result

    def api_libraries(self, payload: dict = Body(...)):
        result = self._server_query(payload, "list_libraries")
        if result.get("success"):
            self._library_catalog[str(payload.get("server_id") or "")] = copy.deepcopy(result.get("items") or [])
        return result

    def api_playlist_groups(self, payload: dict = Body(...)):
        """Compatibility projection; membership never depends on a server read."""
        try:
            raw = payload.get("members", [])
            if not isinstance(raw, list):
                raise ValueError("片单内容必须是列表")
            members = MediaList("播放顺序预览", members=[MediaMember.from_dict(item) for item in raw]).members
        except (TypeError, ValueError) as error:
            return failure(str(error), items=[])
        return {"success": True, "items": [{**item.to_dict(), "key": item.key} for item in members]}
    def api_collections(self, payload: dict = Body(...)):
        return self._server_query(payload, "list_collections")
    def api_server_collection(self, payload: dict = Body(...)):
        def fetch():
            return [record for item in self._client(str(payload.get("server_id") or "")).get_collection_items(
                str(payload.get("collection_id") or "")) if (record := self._remote_member(item))]
        return self._candidates(fetch, payload, provider="server:" + str(payload.get("server_id")))
    @staticmethod
    def _history_records(records):
        """为旧记录补齐稳定身份，保留内容并区分重复任务或无任务 ID 的记录。"""
        if not isinstance(records, list) or any(not isinstance(record, dict) for record in records):
            raise ValueError("运行记录格式无效")
        items, seen = copy.deepcopy(records), set()
        for record in items:
            key = record.get("history_id") or record.get("job_id")
            if not isinstance(key, str) or not key.strip() or len(key) > 128 or key in seen:
                key = uuid4().hex
            record["history_id"] = key
            seen.add(key)
        return items

    def _load_history(self):
        """兼容旧数组并原子保存记录身份与版本，避免重新加载后无法精确删除。"""
        raw = self.get_data("history")
        if raw is None or isinstance(raw, list):
            records, revision = raw or [], 0
        elif isinstance(raw, dict):
            records, revision = raw.get("items"), raw.get("revision", 0)
        else:
            raise ValueError("运行记录格式无效")
        if type(revision) is not int or revision < 0:
            raise ValueError("运行记录版本无效")
        records = self._history_records(records)
        stored = {"items": records, "revision": revision}
        if raw is not None and raw != stored and self.save_data("history", stored) is False:
            raise RuntimeError("运行记录升级未保存")
        self._history, self._history_revision, self._history_error = records, revision, ""

    def _save_history(self, records):
        """持有任务锁时先保存完整快照，成功后再更新内存与版本。"""
        if self._history_error:
            raise RuntimeError("运行记录尚未可靠加载")
        records, revision = self._history_records(records), self._history_revision + 1
        if self.save_data("history", {"items": records, "revision": revision}) is False:
            raise RuntimeError("运行记录未保存")
        self._history, self._history_revision = records, revision

    def _history_snapshot(self, **extra):
        """返回版本化倒序副本；调用方须持有任务锁。"""
        if self._history_error:
            return failure(self._history_error, "persistence_error", items=[])
        return {"success": True, "items": public_diagnostics(self._history[::-1]), "revision": self._history_revision, **extra}

    def api_history(self):
        with self._job_lock:
            return self._history_snapshot()

    def api_delete_history(self, payload: dict = Body(...)):
        """仅删除指定或全部历史记录，保留任务状态、片单、绑定与订阅缓存。"""
        action, ids = payload.get("action"), payload.get("ids")
        if action == "delete":
            if not isinstance(ids, list) or not ids or len(ids) > 1000 or any(not isinstance(key, str) or not key or len(key) > 128 for key in ids):
                return failure("请选择有效的运行记录")
            selected = set(ids)
        elif action == "clear" and "ids" not in payload:
            selected = None
        else:
            return failure("请选择删除指定记录或清空全部记录")
        with self._job_lock:
            if self._stop.is_set():
                return failure("插件正在重新加载，请稍后重试", "unavailable")
            if self._history_error:
                return self._history_snapshot()
            if selected is not None and not selected.issubset({record["history_id"] for record in self._history}):
                return failure("部分记录已被清理，请刷新运行记录后重试", "conflict")
            remaining = [] if selected is None else [record for record in self._history if record["history_id"] not in selected]
            deleted = len(self._history) - len(remaining)
            if deleted:
                try:
                    self._save_history(remaining)
                except Exception:
                    return failure("运行记录未删除，请检查存储后重试", "persistence_error")
            return self._history_snapshot(deleted_count=deleted)
    def api_job(self, job_id: str):
        with self._job_lock:
            item = self._jobs.get(job_id)
            return {"success": True, **public_diagnostics(item)} if item else failure("任务不存在", "not_found")
    def api_publish(self, payload: dict = Body(...)):
        if any(not isinstance(payload.get(key, False), bool) for key in ("dry_run", "allow_empty")):
            return failure("预览和允许清空的选项必须为开启或关闭")
        return self._enqueue(list_id=payload.get("list_id"), dry_run=payload.get("dry_run", False), allow_empty=payload.get("allow_empty", False))

    def _publish_selected(self, selected, *, generation, dry_run=False, allow_empty=False,
                          refresh_sources=False, expected=None, subscribe=True, on_progress=None):
        results = []
        total = sum(len(item.targets) + int(refresh_sources and not dry_run)
            + int(subscribe and not dry_run and item.policy.get("auto_subscribe", False)) for item in selected)
        completed = 0

        def report(stage, media_list, server_id=""):
            if on_progress:
                try:
                    on_progress({"completed": completed, "total": total, "stage": stage,
                        "list_name": media_list.name, "server_id": server_id})
                except Exception:
                    pass

        for media_list in selected:
            if generation.is_set():
                break
            watched = expected_for(media_list.members, expected or set())
            if expected and not watched:
                continue
            if refresh_sources and not dry_run:
                report("sources", media_list)
                results.extend(self._refresh_sources(media_list))
                completed += 1
            for target in media_list.targets:
                if generation.is_set():
                    break
                report("preflight", media_list, target.server_id)
                target_name, library_names = self._target_labels(target)
                try:
                    client = self._client(target.server_id)
                    target_name, library_names = self._target_labels(target, client=client)
                    def persist_binding():
                        media_list.revision += 1
                        self._persist()
                    options = {'expected_keys':watched} if watched else {}
                    if on_progress:
                        options['on_progress'] = lambda stage: report(stage, media_list, target.server_id)
                    result = Publisher(client).publish(media_list, target,
                        dry_run=dry_run, allow_empty=allow_empty, on_binding=persist_binding,
                        cancelled=generation.is_set, **options)
                    if result.get("error_code") == "no_matches":
                        # Completing a zero-match inventory is still a successful
                        # check. Keep the code for the library-arrival retry
                        # scheduler, but don't present an expected empty match as
                        # a failure in manual/automatic run summaries.
                        result.update(success=True, status="success", error=None)
                    result.update(target_name=target_name, library_names=library_names)
                except Exception as error:
                    result = failure(str(error) if isinstance(error, JellyfinAuthError) else "媒体库同步失败，请检查媒体服务器设置。", error.code if isinstance(error, EmbyAPIError) else "server_error",
                        list_id=media_list.id, target_id=target.id, server_id=target.server_id)
                result.setdefault("list_id", media_list.id)
                result.setdefault("list_name", media_list.name)
                result.update(target_name=target_name, library_names=library_names)
                results.append(result)
                completed += 1
                report("target_finished", media_list, target.server_id)
            if subscribe and not dry_run and not generation.is_set() and media_list.policy.get("auto_subscribe"):
                report("subscribe", media_list)
                results.extend({**result, "list_id":media_list.id, "list_name":media_list.name, "operation":"subscribe"}
                               for result in self._subscribe_pending(media_list))
                completed += 1
        if not dry_run and not generation.is_set():
            if selected:
                report("saving", selected[-1])
            self._persist()
        if selected:
            report("finished", selected[-1])
        return results

    def _library_attempt(self, keys, selected_ids, first):
        generation = self._stop
        if not self.get_state():
            return {'results':[], 'cancelled':True}
        if not self._data_lock.acquire(blocking=False):
            return None
        try:
            if not self._publish_lock.acquire(blocking=False):
                return None
            try:
                selected = [item for item in self._lists if item.id in selected_ids and item.enabled and self._has_media_target(item)
                            and item.policy.get('publish_on_library')]
                if not selected:
                    return {'results':[], 'cancelled':True}
                results = self._publish_selected(selected, generation=generation, expected=keys, subscribe=first)
                retry = any(result.get('library_pending') or result.get('error_code') in
                            {'timeout','server_error','no_matches','membership_unverified'} for result in results if result.get('operation') != 'subscribe')
                return {'results':results, 'retry':retry, 'cancelled':generation.is_set()}
            finally:
                self._publish_lock.release()
        finally:
            self._data_lock.release()

    def _record_library_job(self, record):
        with self._job_lock:
            self._trim_jobs(record['job_id'])
            self._jobs[record['job_id']] = copy.deepcopy(record)
            records = [row for row in self._history if row.get('job_id') != record['job_id']]
            try:
                self._save_history((records+[record])[-50:])
            except Exception:
                self._jobs[record['job_id']]['history_error'] = '本批运行记录未保存，请检查配置存储。'

    def _trim_jobs(self, incoming_id):
        if incoming_id not in self._jobs and len(self._jobs) >= 50:
            terminal = next((key for key,value in self._jobs.items() if value.get('status') not in {'queued','running','waiting'}),None)
            if terminal is not None:
                self._jobs.pop(terminal)

    @nonblocking_edit
    def _enqueue(self, *, list_id=None, dry_run=False, allow_empty=False, refresh_sources=False, selected_ids=None):
        if self._config_error or self._stop.is_set():
            return failure(self._config_error or "插件已停止", "unavailable")
        if self._binding_error:
            return failure(self._binding_error, "duplicate_binding")
        if self._publish_lock.locked():
            return failure("另一个发布任务正在运行", "busy", status="busy")
        with self._data_lock:
            selected = [item for item in self._lists if item.enabled and self._has_media_target(item) and (not list_id or item.id == str(list_id))
                and (selected_ids is None or item.id in selected_ids)]
            if not selected:
                if list_id:
                    requested = next((item for item in self._lists if item.id == str(list_id)), None)
                    if requested and requested.enabled and not self._has_media_target(requested):
                        return failure("该片单尚未设置媒体服务器，无法同步", "invalid_input")
                return failure("没有已启用且已设置媒体服务器的片单可同步", "not_found")
            if not self._publish_lock.acquire(blocking=False):
                return failure("另一个发布任务正在运行", "busy", status="busy")
            job_id, generation = uuid4().hex, self._stop
            with self._job_lock:
                self._trim_jobs(job_id)
                self._jobs[job_id] = {"job_id": job_id, "status": "queued", "results": [], "created_at": now()}
        def run():
            results = []
            def progress(value):
                with self._job_lock:
                    self._jobs[job_id]["progress"] = copy.deepcopy(value)
            try:
                with self._job_lock:
                    self._jobs[job_id]["status"] = "running"
                with self._data_lock:
                    results = self._publish_selected(selected, generation=generation, dry_run=dry_run,
                        allow_empty=allow_empty, refresh_sources=refresh_sources, on_progress=progress)
                succeeded = sum(bool(result.get("success")) for result in results)
                status = ("cancelled" if generation.is_set() else "success" if succeeded == len(results)
                    else "partial" if succeeded else "error")
                with self._job_lock:
                    record = {**self._jobs[job_id], "status": status, "results": results, "finished_at": now()}
                    self._jobs[job_id] = record
                    self._save_history((self._history + [record])[-50:])
            except Exception:
                with self._job_lock:
                    self._jobs[job_id].update(status="error", error="片单任务未完成，请稍后重试。", results=results)
            finally:
                self._publish_lock.release()
        threading.Thread(target=run, daemon=True, name="playlist-publish").start()
        return {"success": True, "status": "queued", "job_id": job_id}

    def _refresh_sources(self, media_list):
        results = []
        for source in media_list.sources:
            if source.mode != "follow" or self._stop.is_set():
                continue
            result = self._calls.call(lambda: self._host.collection(source.tmdb_id))
            source.last_checked = now()
            media_list.revision += 1
            if not result["success"]:
                source.error = result.get("error")
                results.append({**result, "list_id": media_list.id, "list_name": media_list.name, "source_id": source.tmdb_id})
                continue
            try:
                # Validate the entire provider snapshot before any mutation.
                members = [MediaMember.from_dict(raw) for raw in result["value"] or []]
                if any(member.media_type != "movie" for member in members):
                    raise ValueError("TMDB 合集只支持电影")
            except (TypeError, ValueError):
                source.error = "TMDB 合集返回无效身份，未更改任何成员"
                results.append(failure(source.error, "provider_error", list_id=media_list.id, list_name=media_list.name, source_id=source.tmdb_id))
                continue
            source.error = None
            for member in members:
                if member.key not in source.seen_keys:
                    media_list.add_member(member)
                    source.seen_keys.append(member.key)
            results.append({"success": True, "status": "success", "list_id": media_list.id, "list_name": media_list.name, "source_id": source.tmdb_id, "operation": "follow"})
        return results

    def _recent_subscription_claim(self, key: str) -> str | None:
        """Return a recent local submission timestamp, if one is retained."""
        raw = self._subscription_claims.get(key)
        submitted_at = raw.get("submitted_at") if isinstance(raw, dict) else raw
        if not submitted_at:
            return None
        try:
            submitted = datetime.fromisoformat(str(submitted_at).replace("Z", "+00:00"))
            age = (datetime.now(timezone.utc) - submitted).total_seconds()
        except (TypeError, ValueError):
            age = SUBSCRIPTION_CLAIM_TTL_SECONDS + 1
        if age < 0 or age >= SUBSCRIPTION_CLAIM_TTL_SECONDS:
            self._subscription_claims.pop(key, None)
            self.save_data("subscription_claims", self._subscription_claims)
            return None
        return str(submitted_at)

    def _subscription_claim_pending(self, key: str, payload: dict) -> str | None:
        """Keep a recent submission while MP is downloading or scanning it."""
        submitted_at = self._recent_subscription_claim(key)
        if not submitted_at:
            return None
        try:
            submitted = datetime.fromisoformat(submitted_at.replace("Z", "+00:00"))
            age = max(0, (datetime.now(timezone.utc) - submitted).total_seconds())
        except (TypeError, ValueError):
            age = SUBSCRIPTION_CLAIM_TTL_SECONDS + 1
        state_checker = getattr(self._host, "download_state", None)
        checker = getattr(self._host, "download_pending", None)
        if not callable(state_checker) and not callable(checker):
            # Older test doubles/hosts do not expose the optional read-only
            # query; retain the legacy cache behavior in that case.
            return None
        result = self._calls.call(
            lambda: (state_checker(payload, submitted_at) if callable(state_checker)
                     else checker(payload, submitted_at)),
            provider="download_lookup",
        )
        if not result["success"]:
            # A temporary downloader/database lookup failure must not cause a
            # duplicate submission during the retention window.
            return "queued" if age < SUBSCRIPTION_CLAIM_TTL_SECONDS else None
        state = result.get("value")
        if state:
            return state if isinstance(state, str) else "downloading"
        return "queued" if age < SUBSCRIPTION_START_GRACE_SECONDS else None

    def _subscribe_pending(self, media_list):
        in_library = set()
        episodes = {}
        tv_keys = {member.key for member in media_list.members if member.media_type == "tv"}
        scopes = [(target.server_id, target.library_ids) for target in media_list.targets]
        if not scopes:
            scopes = [(key, []) for key, conf in self._server_configs().items() if str(conf.get("kind") or "").casefold() in SERVER_CLIENTS and conf.get("enabled", True)]
        if not scopes:
            return [failure("请先配置 媒体库，以确认成员是否已经入库", "library_check_unavailable", list_id=media_list.id)]
        for server_id, libraries in scopes:
            def inventory():
                client = self._client(server_id)
                items = client.list_media(libraries)
                found = []
                for item in items:
                    if item.key in tv_keys:
                        found.extend(client.get_series_episodes(item))
                return items, found
            result = self._calls.call(inventory, provider="server:" + server_id)
            if not result["success"]:
                return [failure("订阅检查未完成：" + (result.get("error") or "媒体服务器不可用"), result.get("error_code") or "server_error", list_id=media_list.id)]
            items, found = result["value"]
            in_library.update(item.key for item in items if item.key)
            for item in found:
                episodes.setdefault((item.parent_key, item.season_number), set()).update(item.episode_numbers)
        results = []
        requests = []
        for member in media_list.members:
            if self._stop.is_set():
                return results
            if member.media_type == "movie":
                if member.key not in in_library:
                    requests.append((member, member.key, member.to_dict()))
                continue
            result = self._calls.call(lambda: self._host.seasons(member.tmdb_id), provider="seasons")
            if not result["success"] or not result.get("value"):
                results.append(failure("订阅前未取得电视剧季信息，未创建订阅", "provider_error", key=member.key))
                continue
            metadata = {item["season_number"]: item for item in result["value"]}
            selected = member.seasons if member.seasons is not None else sorted(number for number in metadata if number > 0)
            for number in selected:
                key = f"{member.key}:season:{number}"
                count = metadata.get(number, {}).get("episode_count", 0)
                if not count:
                    if member.seasons is not None:
                        results.append(failure(f"第 {number} 季暂无可订阅的集数", "season_unavailable", key=key))
                    continue
                if set(range(1, count + 1)).issubset(episodes.get((member.key, number), set())):
                    continue
                payload = member.to_dict()
                payload.pop("seasons", None)
                requests.append((member, key, {**payload, "season": number}))
        for member, key, payload in requests:
            if self._stop.is_set():
                break
            cached_id = self._subscriptions.get(key)
            if cached_id is not None:
                exists = self._calls.call(lambda sid=cached_id, payload=payload:
                    self._host.subscription_exists(sid, payload), provider="subscribe_lookup")
                if not exists["success"] or not isinstance(exists.get("value"), bool):
                    results.append(failure("宿主订阅状态检查失败，未重复创建订阅", "subscription_check_failed", key=key))
                    continue
                if exists["value"]:
                    results.append({"success": True, "status": "subscribed", "key": key, "already_exists": True})
                    continue
                pending_state = self._subscription_claim_pending(key, payload)
                if pending_state:
                    results.append({"success": True, "status": "subscribed", "key": key,
                                    "pending_download": True, "pending_download_state": pending_state,
                                    "already_exists": True})
                    continue
                self._subscriptions.pop(key, None)
                self._subscription_claims.pop(key, None)
                self.save_data("subscriptions", self._subscriptions)
                self.save_data("subscription_claims", self._subscription_claims)
            if not member.title:
                resolved = self._calls.call(lambda: self._host.resolve(member.tmdb_id, member.media_type))
                if not resolved["success"]:
                    results.append(failure("订阅前媒体解析失败", "provider_error", key=key))
                    continue
                member.title, member.year = resolved["value"]["title"], resolved["value"].get("year")
            payload.update(title=member.title, year=member.year)
            result = self._calls.call(lambda: self._host.subscribe(payload), provider="subscribe",
                                      timeout=SUBSCRIPTION_TIMEOUT_SECONDS)
            if result["success"]:
                sid, error = result["value"]
                if sid:
                    self._subscriptions[key] = sid
                    self._subscription_claims[key] = {"submitted_at": now(), "subscription_id": sid}
                    self.save_data("subscriptions", self._subscriptions)
                    self.save_data("subscription_claims", self._subscription_claims)
                    results.append({"success": True, "status": "subscribed", "key": key})
                else:
                    results.append(failure("宿主订阅未成功，请检查订阅页", "subscribe_error", key=key))
            else:
                results.append({**result, "key": key})
        return results

    def refresh(self):
        return self._enqueue(refresh_sources=True) if self.get_state() else {"success": False, "status": "disabled"}
    def on_transfer_complete(self, event):
        if not self.get_state():
            return
        selected = [item.id for item in self._lists if item.enabled and item.policy.get("publish_on_library")]
        if not selected:
            return
        if self._library_sync:
            self._library_sync.notify(transfer_keys(event), set(selected))
    def get_service(self):
        if not self.get_state():
            return []
        interval = self._config.get("migrated_interval_minutes")
        trigger = IntervalTrigger(minutes=interval) if interval else CronTrigger.from_crontab(self._config.get("cron", "*/30 * * * *"))
        return [{"id": self._plugin_id() + ".Refresh", "name": "片单来源刷新与发布", "trigger": trigger, "func": self.refresh, "kwargs": {}}]
    @staticmethod
    def get_render_mode():
        return "vue", "dist/assets"
    def get_form(self):
        return [{"component": "VAlert", "props": {"type": "info", "variant": "tonal"},
            "text": "请在片单管理页设置来源、成员、发布目标及自动化。仅支持 MoviePilot V3。"}], {}
    def get_page(self):
        return [{"component": "VAlert", "props": {"type": "info"}, "text": "请打开 Vue 片单管理页；若页面未加载，请检查 dist 联邦产物安装完整性。"}]
    def get_sidebar_nav(self):
        # 片单管理通过插件页和媒体卡片弹窗进入，不在宿主左侧导航重复占位。
        return []
    def get_media_actions(self):
        return [{"key": "media_lists", "title": "加入片单", "icon": "mdi-playlist-plus", "component": "MediaActions",
            "surfaces": ["media_card", "media_detail", "tmdb_collection"], "media_types": ["movie", "tv", "collection"], "order": 50}]
    @staticmethod
    def get_command():
        return []
    def get_api(self):
        routes = [("settings", "GET", self.api_settings), ("settings", "POST", self.api_save_settings),
            ("lists", "GET", self.api_lists), ("lists", "POST", self.api_save_lists),
            ("export", "GET", self.api_export), ("import/preview", "POST", self.api_import_preview),
            ("import/commit", "POST", self.api_import_commit),
            ("servers", "GET", self.api_servers), ("libraries", "POST", self.api_libraries),
            ("playlist-groups", "POST", self.api_playlist_groups),
            ("library-media", "POST", self.api_library_media), ("search", "POST", self.api_search),
            ("cancel", "POST", self.api_cancel), ("resolve", "POST", self.api_resolve),
            ("seasons", "POST", self.api_seasons),
            ("tmdb-collection", "POST", self.api_tmdb_collection), ("server-collection", "POST", self.api_server_collection),
            ("collections", "POST", self.api_collections), ("members", "POST", self.api_members),
            ("publish", "POST", self.api_publish), ("job", "GET", self.api_job), ("history", "GET", self.api_history),
            ("history", "POST", self.api_delete_history)]
        return [{"path": "/" + path, "endpoint": endpoint, "methods": [method], "auth": "bear",
                 "dependencies": [Depends(_manage_user)]} for path, method, endpoint in routes]

if eventmanager is not None:
    MediaLibraryPlaylistManager.on_transfer_complete = eventmanager.register(EventType.TransferComplete)(MediaLibraryPlaylistManager.on_transfer_complete)
