"""Independent acceptance regressions discovered against PRODUCT_SPEC.md."""
import copy
import threading
import pytest

from app.plugins.embycollectionsync import EmbyCollectionSync


def member(tmdb_id=1):
    return {"tmdb_id": tmdb_id, "media_type": "movie", "title": "电影"}


def test_failed_settings_storage_keeps_prior_committed_settings(monkeypatch):
    plugin = EmbyCollectionSync()
    plugin.init_plugin({"enabled": False, "cron": "*/30 * * * *"})
    original = copy.deepcopy(plugin._config)
    monkeypatch.setattr(plugin, "update_config", lambda _config: False)
    try:
        result = plugin.api_save_settings({"enabled": True, "cron": "0 * * * *"})
        assert result["success"] is False
        assert result["error_code"] == "persistence_error"
        assert plugin._config == original
        assert plugin.api_settings()["enabled"] is False
        assert plugin.api_settings()["cron"] == "*/30 * * * *"
    finally:
        plugin.stop_service()


def test_follow_import_remembers_already_selected_collection_members():
    plugin = EmbyCollectionSync()
    plugin.init_plugin({"enabled": False})
    plugin._host.collection = lambda _id: [member(1), member(2)]
    try:
        listing = plugin.api_save_lists({"list": {"name": "片单", "members": [member(1)]}})["list"]
        preview = plugin.api_tmdb_collection({"tmdb_id": 10, "mode": "follow", "exclude_keys": ["movie:1"]})
        assert [item["key"] for item in preview["items"]] == ["movie:2"]
        listing["members"].append(member(2))
        listing["sources"] = [preview["source"]]
        saved = plugin.api_save_lists({"list": listing})["list"]
        removed = plugin.api_members({"list_id": saved["id"], "action": "remove", "keys": ["movie:1"]})
        assert [item["tmdb_id"] for item in removed["list"]["members"]] == [2]
        domain = plugin._find(saved["id"])
        plugin._refresh_sources(domain)
        assert domain.member_keys == ["movie:2"]
        assert set(domain.sources[0].seen_keys) == {"movie:1", "movie:2"}
    finally:
        plugin.stop_service()


@pytest.mark.parametrize("minutes", [300, 420, 540, 600, 1380])
def test_legacy_hour_intervals_that_do_not_divide_day_remain_exact(minutes):
    plugin = EmbyCollectionSync()
    plugin.init_plugin({"enabled": True, "interval_minutes": minutes})
    try:
        trigger = plugin.get_service()[0]["trigger"]
        assert plugin._config.get("migrated_interval_minutes") == minutes
        assert trigger.interval.total_seconds() == minutes * 60
    finally:
        plugin.stop_service()


def test_cancelled_server_lookup_cannot_cache_old_credentials_after_reload(monkeypatch):
    started, release, finished = threading.Event(), threading.Event(), threading.Event()

    class Client:
        def __init__(self, base_url, api_key, user_id, **_kwargs):
            self.base_url, self.api_key, self.user_id = base_url, api_key, user_id

        def resolve_user_id(self, _username):
            started.set()
            release.wait(1)
            return "old-user"

    from app.plugins.embycollectionsync import SERVER_CLIENTS
    monkeypatch.setitem(SERVER_CLIENTS, "emby", Client)
    plugin = EmbyCollectionSync()
    plugin._host.servers = lambda: {}
    plugin.init_plugin({"servers": {"Emby": {"host": "http://old", "api_key": "old-key", "username": "old"}}})

    def lookup():
        try:
            return plugin._client("Emby")
        finally:
            finished.set()

    caller = threading.Thread(target=lambda: plugin._calls.call(lookup, provider="server:Emby"))
    try:
        caller.start()
        assert started.wait(1)
        plugin.init_plugin({"servers": {"Emby": {"host": "http://new", "api_key": "new-key", "user_id": "new-user"}}})
        release.set()
        assert finished.wait(1)
        caller.join(1)
        current = plugin._client("Emby")
        assert current.base_url == "http://new"
        assert current.api_key == "new-key"
        assert current.user_id == "new-user"
    finally:
        release.set()
        caller.join(1)
        plugin.stop_service()
