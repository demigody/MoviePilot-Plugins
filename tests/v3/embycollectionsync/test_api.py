import copy
import threading
import time

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.plugins.embycollectionsync import EmbyCollectionSync, _manage_user
from app.plugins.embycollectionsync.emby import EmbyItem
from app.plugins.embycollectionsync.models import MediaList, MediaMember, Target, CollectionBinding
from app.plugins.embycollectionsync.requests import ProviderCalls


class FakeHost:
    def __init__(self):
        self.results = []
        self.search_error = False
        self.collections = {}
        self.collection_calls = []
        self.subscriptions = []
        self.active_subscriptions = {}
    def search(self, query, kind):
        if self.search_error:
            raise RuntimeError("secret-api-key")
        return self.results
    def resolve(self, tmdb_id, kind):
        return {"tmdb_id": tmdb_id, "media_type": kind, "title": "已解析片名", "year": 2020}
    def collection(self, tmdb_id):
        self.collection_calls.append(tmdb_id)
        return self.collections[tmdb_id]
    def seasons(self, tmdb_id):
        return [{'season_number': number, 'episode_count': 2, 'name': f'第 {number} 季'} for number in (1, 2, 3, 4)]
    def servers(self):
        return {"Emby": {"name": "Emby", "kind": "emby", "host": "http://localhost", "api_key": "secret", "user_id": "u"}}
    def subscribe(self, member):
        self.subscriptions.append(member)
        sid = len(self.subscriptions)
        self.active_subscriptions[sid] = member
        return sid, None
    def subscription_exists(self, sid, member):
        current = self.active_subscriptions.get(sid)
        return bool(current and (current['media_type'], current['tmdb_id'], current.get('season')) == (member['media_type'], member['tmdb_id'], member.get('season')))


@pytest.fixture
def plugin():
    item = EmbyCollectionSync()
    item.init_plugin({"enabled": False})
    item._host = FakeHost()
    yield item
    item.stop_service()


def member(tmdb_id=1, kind="movie", title="片名"):
    return {"tmdb_id": tmdb_id, "media_type": kind, "title": title}


def test_plugin_uses_plugin_page_without_sidebar_navigation(plugin):
    assert plugin.get_sidebar_nav() == []


def test_selected_seasons_subscribe_independently_without_widening_or_skipping_partial_show(plugin):
    from .test_season_playlist import season_server, add_episode
    server, _ = season_server()
    plugin._clients['Emby'] = server.client()
    listing = MediaList('只看后两季', members=[MediaMember(1, 'tv', '剧', seasons=[3, 4])])
    add_episode(server, 'c1', 3, 1)
    assert all(result['success'] for result in plugin._subscribe_pending(listing))
    assert [(item['tmdb_id'], item['season']) for item in plugin._host.subscriptions] == [(1, 3), (1, 4)]
    assert set(plugin._subscriptions) == {'tv:1:season:3', 'tv:1:season:4'}
    cached = plugin._subscribe_pending(listing)
    assert len(cached) == 2 and all(row.get("already_exists") and row["success"] for row in cached)
    assert len(plugin._host.subscriptions) == 2
    # Reused IDs for a different season must not suppress the requested season.
    plugin._host.active_subscriptions[1] = {**plugin._host.subscriptions[0], 'season': 1}
    assert plugin._subscribe_pending(listing)[0]['success']
    assert plugin._host.subscriptions[-1]['season'] == 3
    assert not server.writes


def test_complete_selected_season_skips_subscription_but_other_season_is_added(plugin):
    from .test_season_playlist import season_server
    server, _ = season_server()
    plugin._clients['Emby'] = server.client()
    listing = MediaList('范围', members=[MediaMember(1, 'tv', '剧', seasons=[2, 3])])
    assert all(result['success'] for result in plugin._subscribe_pending(listing))
    assert [item['season'] for item in plugin._host.subscriptions] == [3]


def test_whole_show_checks_each_known_season_instead_of_skipping_existing_series(plugin):
    from .test_season_playlist import season_server
    server, _ = season_server()
    plugin._clients['Emby'] = server.client()
    result = plugin._subscribe_pending(MediaList('整部', members=[MediaMember(1, 'tv', '剧')]))
    assert all(item['success'] for item in result)
    assert [item['season'] for item in plugin._host.subscriptions] == [3, 4]


def test_saving_empty_explicit_season_selection_cannot_overwrite_existing_list(plugin):
    stored = create(plugin, members=[{**member(1, 'tv'), 'seasons':[3,4]}])
    before = copy.deepcopy(plugin._config)
    changed = copy.deepcopy(stored)
    changed['members'][0]['seasons'] = []
    result = plugin.api_save_lists({'action':'save', 'list':changed, 'revision':stored['revision']})
    assert not result['success']
    assert plugin._config == before


def test_missing_season_metadata_does_not_fall_back_to_default_season(plugin):
    from .test_season_playlist import season_server
    server, _ = season_server()
    plugin._clients['Emby'] = server.client()
    plugin._host.seasons = lambda _: []
    result = plugin._subscribe_pending(MediaList('范围', members=[MediaMember(1, 'tv', '剧', seasons=[3])]))
    assert result and not result[0]['success']
    assert not plugin._host.subscriptions


def test_season_choices_use_metadata_without_reading_emby_or_mutating_configuration(plugin):
    before = copy.deepcopy(plugin._config)
    result = plugin.api_seasons({'tmdb_id': 211089})
    assert result['success'] and [item['season_number'] for item in result['items']] == [1, 2, 3, 4]
    assert plugin._config == before and not plugin._host.subscriptions


def test_import_season_collection_keeps_parent_identity_and_combines_only_selected_seasons(plugin):
    from .test_season_playlist import season_server
    from .test_season_scope import add_seasons
    server, _ = season_server()
    add_seasons(server)
    server.collections['c'] = ['season2', 'season3']
    plugin._clients['Emby'] = server.client()
    result = plugin.api_server_collection({'server_id':'Emby', 'collection_id':'c'})
    assert result['success'], result
    assert len(result['items']) == 1
    assert result['items'][0]['tmdb_id'] == 1 and result['items'][0]['seasons'] == [2, 3]


def create(plugin, **extra):
    if "enabled" not in extra and not extra.get("targets"):
        extra["enabled"] = False
    return plugin.api_save_lists({"action": "save", "list": {"name": "片单", **extra}})["list"]


def test_quick_enable_only_changes_state_and_revision_and_is_idempotent(plugin):
    stored = create(plugin, members=[{**member(1,'tv'), 'seasons':[3,4]}], policy={'auto_subscribe':True}, targets=[{"server_id":"Emby"}])
    other = create(plugin, name='另一份片单', enabled=False)
    result = plugin.api_save_lists({'action':'set_enabled','list_id':stored['id'],'revision':stored['revision'],'enabled':False})
    assert result['success'], result
    assert result['list'] == {**stored,'enabled':False,'revision':stored['revision']+1}
    assert next(item for item in result['lists'] if item['id'] == other['id']) == other
    snapshot = copy.deepcopy(plugin._config)
    repeated = plugin.api_save_lists({'action':'set_enabled','list_id':stored['id'],'revision':result['list']['revision'],'enabled':False})
    assert repeated['success'] and plugin._config == snapshot
    assert not plugin._host.subscriptions and not plugin._jobs


def test_quick_enable_rejects_conflict_invalid_state_and_missing_list_without_changes(plugin, monkeypatch):
    stored = create(plugin, targets=[{"server_id": "Emby"}])
    before = copy.deepcopy(plugin._config)
    for payload, code in [
        ({'list_id':stored['id'],'revision':-1,'enabled':False},'conflict'),
        ({'list_id':stored['id'],'revision':0,'enabled':'false'},'invalid_input'),
        ({'list_id':'missing','revision':0,'enabled':False},'not_found'),
    ]:
        result = plugin.api_save_lists({'action':'set_enabled',**payload})
        assert not result['success'] and result['error_code'] == code, result
        assert plugin._config == before
    monkeypatch.setattr(plugin,'update_config',lambda _:False)
    result = plugin.api_save_lists({'action':'set_enabled','list_id':stored['id'],'revision':0,'enabled':False})
    assert result['error_code'] == 'persistence_error'
    assert plugin._config == before and plugin._lists[0].enabled


@pytest.mark.parametrize('entry',['manual_all','cron','library_event'])
def test_stopped_list_is_excluded_from_sync_sources_and_subscriptions_for_every_entry(plugin, monkeypatch, entry):
    calls = []
    class Client:
        def list_libraries(self): return []

    class Publisher:
        def __init__(self, client): pass
        def publish(self, media_list, target, **kwargs):
            calls.append(('publish',media_list.id))
            return {'success':True,'list_id':media_list.id}
    class ImmediateTimer:
        def __init__(self, delay, callback): self.callback = callback
        def start(self): self.callback()
        def cancel(self): pass
    monkeypatch.setattr('app.plugins.embycollectionsync.Publisher',Publisher)
    monkeypatch.setattr('app.plugins.embycollectionsync.threading.Timer',ImmediateTimer)
    monkeypatch.setattr(plugin,'_refresh_sources',lambda item:calls.append(('source',item.id)) or [])
    monkeypatch.setattr(plugin,'_subscribe_pending',lambda item:calls.append(('subscribe',item.id)) or [])
    plugin._clients['Emby'] = Client()
    active = create(plugin,name='启用片单',members=[member()],policy={'auto_subscribe':True,'publish_on_library':True},targets=[{'server_id':'Emby'}])
    stopped = create(plugin,name='停用片单',enabled=False,members=[member(2)],policy={'auto_subscribe':True,'publish_on_library':True},targets=[{'server_id':'Emby'}])
    plugin._config['enabled'] = True
    if entry == 'manual_all': queued = plugin.api_publish({})
    elif entry == 'cron': queued = plugin.refresh()
    else:
        plugin.on_transfer_complete(None)
        queued = {'job_id':next(iter(plugin._jobs))}
    deadline = time.monotonic()+2
    while plugin.api_job(queued['job_id'])['status'] in {'queued','running'} and time.monotonic() < deadline:
        time.sleep(.005)
    assert plugin.api_job(queued['job_id'])['status'] == 'success'
    assert calls and all(list_id == active['id'] for _,list_id in calls)
    assert ('publish',active['id']) in calls and ('subscribe',active['id']) in calls
    assert (('source',active['id']) in calls) == (entry == 'cron')
    assert plugin._find(stopped['id']).to_dict() == stopped


def test_direct_sync_and_automatic_entries_do_nothing_when_all_lists_are_stopped(plugin):
    stopped = create(plugin,enabled=False,policy={'auto_subscribe':True,'publish_on_library':True})
    plugin._config['enabled'] = True
    assert not plugin.api_publish({'list_id':stopped['id']})['success']
    assert not plugin.api_publish({})['success']
    assert not plugin.refresh()['success']
    plugin.on_transfer_complete(None)
    assert not plugin._jobs and not plugin._host.subscriptions and plugin._timer is None


def test_lists_without_media_server_cannot_be_enabled_or_saved_as_enabled(plugin):
    draft = create(plugin, enabled=False)
    enabled = plugin.api_save_lists({"action": "set_enabled", "list_id": draft["id"], "revision": 0, "enabled": True})
    assert not enabled["success"] and enabled["error_code"] == "invalid_input"

    created = plugin.api_save_lists({"list": {"name": "缺少媒体服务器", "enabled": True}})
    assert not created["success"] and created["error_code"] == "invalid_input"
    assert [item["id"] for item in plugin.api_lists()["lists"]] == [draft["id"]]


def test_sync_all_skips_legacy_enabled_lists_without_media_server(plugin, monkeypatch):
    from app.plugins.embycollectionsync.models import MediaList

    seen = []

    class Client:
        def list_libraries(self):
            return [{"id": "movies", "name": "电影"}]

    class Publisher:
        def __init__(self, client):
            pass

        def publish(self, media_list, target, **kwargs):
            seen.append(media_list.name)
            return {"success": True, "status": "success", "list_id": media_list.id, "target_id": target.id}

    monkeypatch.setattr("app.plugins.embycollectionsync.Publisher", Publisher)
    plugin._clients["Emby"] = Client()
    plugin._library_catalog["Emby"] = [{"id":"movies", "name":"电影"}]
    unconfigured = MediaList("旧未配置片单", members=[MediaList.from_dict({"name": "x", "members": [member()]}).members[0]])
    configured = MediaList.from_dict({"name": "已配置片单", "members": [member()], "targets": [{"server_id": "Emby", "library_ids": ["movies"]}]})
    plugin._lists = [unconfigured, configured]
    plugin._config["lists"] = [item.to_dict() for item in plugin._lists]

    queued = plugin.api_publish({})
    assert queued["success"]
    deadline = time.monotonic() + 1
    while plugin.api_job(queued["job_id"])["status"] in {"queued", "running"} and time.monotonic() < deadline:
        time.sleep(0.005)
    assert seen == ["已配置片单"]
    assert [item["list_name"] for item in plugin.api_job(queued["job_id"])["results"]] == ["已配置片单"]


def test_publish_results_include_human_readable_library_names(plugin, monkeypatch):
    class Client:
        def list_libraries(self):
            return [{"id": "movies", "name": "电影"}, {"id": "tv", "name": "电视剧"}]

    class Publisher:
        def __init__(self, client):
            pass

        def publish(self, media_list, target, **kwargs):
            return {"success": True, "status": "success", "list_id": media_list.id, "target_id": target.id}

    monkeypatch.setattr("app.plugins.embycollectionsync.Publisher", Publisher)
    plugin._clients["Emby"] = Client()
    stored = create(plugin, targets=[{"server_id": "Emby", "library_ids": ["tv"]}])
    assert plugin.api_libraries({"server_id": "Emby"})["success"]
    results = plugin._publish_selected([plugin._find(stored["id"])], generation=plugin._stop)
    assert results[0]["library_names"] == ["电视剧"]


def test_publish_loads_library_names_when_process_cache_is_empty(plugin, monkeypatch):
    calls = []

    class Client:
        def list_libraries(self):
            calls.append("list_libraries")
            return [{"id": "a1b2-c3d4", "name": "电影"}]

    class Publisher:
        def __init__(self, client):
            pass

        def publish(self, media_list, target, **kwargs):
            return {"success": True, "status": "success", "list_id": media_list.id, "target_id": target.id}

    monkeypatch.setattr("app.plugins.embycollectionsync.Publisher", Publisher)
    plugin._clients["Emby"] = Client()
    stored = create(plugin, targets=[{"server_id": "Emby", "library_ids": ["A1B2C3D4"]}])

    results = plugin._publish_selected([plugin._find(stored["id"])], generation=plugin._stop)

    assert results[0]["library_names"] == ["电影"]
    assert calls == ["list_libraries"]


def test_routes_read_real_fastapi_json_body(plugin):
    app = FastAPI()
    app.dependency_overrides[_manage_user] = lambda: True
    for route in plugin.get_api():
        app.add_api_route("/plugin/EmbyCollectionSync" + route["path"], route["endpoint"], methods=route["methods"], dependencies=route["dependencies"])
    with TestClient(app) as client:
        response = client.post("/plugin/EmbyCollectionSync/lists", json={"action": "save", "list": {"name": "HTTP片单", "enabled": False}})
        assert response.status_code == 200
        assert response.json()["list"]["name"] == "HTTP片单"
        result = client.post("/plugin/EmbyCollectionSync/search", json={"query": "a", "media_type": "tv"})
        assert result.json()["status"] == "empty"
        assert client.post("/plugin/EmbyCollectionSync/lists").status_code == 422
        assert all(route["auth"] == "bear" for route in plugin.get_api())


def test_server_setup_failure_uses_generic_user_message_without_internal_adapter_name(plugin,monkeypatch):
    from app.plugins.embycollectionsync.emby import EmbyAPIError
    stored = create(plugin,members=[member()],targets=[{'server_id':'server'}])
    def fail(_): raise EmbyAPIError('private-connection-detail',code='unsupported')
    monkeypatch.setattr(plugin,'_client',fail)
    result = plugin._publish_selected([plugin._find(stored['id'])],generation=plugin._stop)[0]
    assert result['error_code'] == 'unsupported' and '媒体库同步失败' in result['error']
    assert 'Emby' not in result['error'] and 'private' not in result['error']


def test_playlist_projection_uses_only_selected_ids_without_server_or_metadata_queries(plugin):
    from .test_season_playlist import season_server
    server, _ = season_server()
    plugin._clients['Emby'] = server.client()
    before = copy.deepcopy(plugin._config)
    selected = [member(111697, 'tv'), member(286679, 'tv'), member(286680, 'tv')]
    result = plugin.api_playlist_groups({'server_id': 'Emby', 'members': selected})
    assert result['success'], result
    assert [row['key'] for row in result['items']] == ['tv:111697', 'tv:286679', 'tv:286680']
    assert not any('season_number' in row or 'playback_pending' in row for row in result['items'])
    assert plugin._config == before and server.calls == []
    server.inventory = []
    assert plugin.api_playlist_groups({'members': selected}) == result
    assert not plugin.api_playlist_groups({'server_id': 'Emby', 'members': [member(1, 'Episode')]})['success']


def test_plugin_routes_require_host_manage_permission(plugin):
    app = FastAPI()
    def deny():
        raise HTTPException(status_code=403, detail="no management permission")
    app.dependency_overrides[_manage_user] = deny
    for route in plugin.get_api():
        app.add_api_route("/" + route["path"].lstrip("/"), route["endpoint"], methods=route["methods"], dependencies=route["dependencies"])
    with TestClient(app) as client:
        assert client.post("/publish", json={"allow_empty": True}).status_code == 403
        assert client.get("/lists").status_code == 403
    assert plugin._jobs == {}


def test_failed_migration_preserves_original_and_backup(plugin):
    plugin = EmbyCollectionSync()
    original = {"enabled": True, "rules": [{"name": "bad", "movies": [{"tmdb_id": 1, "media_type": "Episode"}]}]}
    plugin.init_plugin(original)
    assert plugin.api_lists()["error_code"] == "migration_error"
    assert plugin.get_data("migration_backup_v2")["config"] == original
    assert not hasattr(plugin, "_stored_config")
    assert not plugin.api_save_lists({"list": {"name": "new"}})["success"]
    assert plugin._config == original


def test_migration_backup_does_not_get_overwritten_on_reload():
    plugin = EmbyCollectionSync()
    original = {"rules": [{"name": "old", "movies": [1]}]}
    plugin.init_plugin(original)
    plugin.init_plugin(plugin._stored_config)
    assert plugin.get_data("migration_backup_v2")["config"] == original


def test_atomic_save_revision_and_typed_members(plugin):
    item = create(plugin, members=[member(1), member(1, "tv")])
    assert len(item["members"]) == 2
    saved = plugin.api_save_lists({"list": {**item, "name": "new"}})
    assert saved["list"]["revision"] == 1
    assert plugin.api_save_lists({"list": item})["error_code"] == "conflict"
    invalid = plugin.api_save_lists({"list": {**saved["list"], "members": [member(9, "Episode")]}})
    assert not invalid["success"]
    assert plugin.api_lists()["lists"][0] == saved["list"]


def test_members_default_no_subscription_and_deduplicate(plugin):
    item = create(plugin)
    result = plugin.api_members({"list_id": item["id"], "items": [member(1), member(1), member(1, "tv")]})
    assert len(result["list"]["members"]) == 2
    assert plugin._host.subscriptions == []
    assert not result["list"]["policy"]["auto_subscribe"]
    assert not result["list"]["policy"]["publish_on_library"]


def test_search_success_empty_failure_are_distinct_and_hide_secrets(plugin):
    plugin._host.results = [member(1), member(1), member(1, "tv")]
    result = plugin.api_search({"query": "test", "exclude_keys": ["movie:1"]})
    assert result["status"] == "success"
    assert [item["key"] for item in result["items"]] == ["tv:1"]
    plugin._host.results = []
    assert plugin.api_search({"query": "empty"})["status"] == "empty"
    plugin._host.search_error = True
    failure = plugin.api_search({"query": "broken"})
    assert failure["status"] == "error"
    assert "secret" not in str(failure)
    assert plugin.api_search({"query": "broken"})["status"] == "cooldown"


def test_timeout_keeps_slots_bounded_and_feedback(plugin):
    gate = threading.Event()
    calls = ProviderCalls(timeout=0.03, cooldown=0.03, capacity=1)
    try:
        assert calls.call(lambda: gate.wait(1), provider="subscribe")["status"] == "timeout"
        cooling = calls.call(lambda: 1, provider="subscribe")
        assert cooling["status"] == "cooldown"
        assert "订阅请求" in cooling["error"] and "插件暂停重试" in cooling["error"]
        assert cooling["retry_after"] >= 1
        time.sleep(0.04)
        assert calls.call(lambda: 1)["status"] == "busy"
    finally:
        gate.set()
        calls.stop()


def test_cancel_returns_early_and_discards_late_success(plugin):
    gate = threading.Event()
    started = threading.Event()
    result = []
    def search():
        started.set()
        gate.wait(1)
        return [member()]
    worker = threading.Thread(target=lambda: result.append(plugin._calls.call(search, request_id="cancel-me")))
    worker.start()
    assert started.wait(1)
    plugin.api_cancel({"request_id": "cancel-me"})
    worker.join(0.5)
    gate.set()
    assert not worker.is_alive()
    assert result[0]["status"] == "cancelled"


def test_tmdb_id_resolves_title_and_type_without_mutating_lists(plugin):
    result = plugin.api_resolve({"tmdb_ids": "99,99 100", "media_type": "tv"})
    assert result["success"], result
    assert [item["tmdb_id"] for item in result["items"]] == [99, 100]
    assert all(item["title"] == "已解析片名" and item["media_type"] == "tv" for item in result["items"])
    assert plugin.api_lists()["lists"] == []
    assert not plugin.api_resolve({"tmdb_ids": "1.5"})["success"]


def test_numeric_id_lookup_defaults_to_both_types_and_keeps_both_candidates(plugin):
    result = plugin.api_resolve({"tmdb_ids": [10]})
    assert result["success"]
    assert {(item["media_type"], item["tmdb_id"]) for item in result["items"]} == {("movie", 10), ("tv", 10)}
    assert plugin.api_lists()["lists"] == []


def test_all_type_id_lookup_does_not_report_absent_other_type_as_failure(plugin):
    def resolve(tmdb_id, kind):
        if kind == "tv":
            raise ValueError("not found")
        return member(tmdb_id)
    plugin._host.resolve = resolve
    result = plugin.api_resolve({"tmdb_ids": [10], "media_type": "all"})
    assert result["success"] and result["status"] == "success" and result["errors"] == []
    assert result["items"] == [member(10)]


def test_all_type_id_lookup_reports_failed_provider_even_when_other_type_succeeds(plugin):
    def resolve(tmdb_id, kind):
        if kind == "tv":
            raise RuntimeError("private-provider-token")
        return member(tmdb_id)
    plugin._host.resolve = resolve
    result = plugin.api_resolve({"tmdb_ids": [10]})
    assert result["success"] and result["status"] == "partial"
    assert result["items"] == [member(10)]
    assert result["errors"][0]["media_type"] == "tv"
    assert "private" not in str(result)


def test_search_preserves_unmapped_native_candidates_and_excludes_only_known_members(plugin):
    plugin._host.results = [member(10), {"media_source": "douban", "media_id": "9", "media_type": "movie", "title": "未关联的影片", "year": 2025, "mapping_error": "未确认 TMDB 对应关系"}]
    result = plugin.api_search({"query": "影片", "exclude_keys": ["movie:10"]})
    assert result["success"] and len(result["items"]) == 1
    assert result["items"][0]["key"] == "movie:douban:9"
    assert not result["items"][0].get("tmdb_id")
    assert result["errors"] == [] and plugin.api_lists()["lists"] == []
    assert "mapping_error" not in result["items"][0]


def test_search_allows_five_minutes_for_host_multi_source_search(plugin):
    observed = {}

    def call(fn, *, request_id, provider, timeout):
        observed.update(request_id=request_id, provider=provider, timeout=timeout)
        return {"success": True, "status": "success", "value": []}

    plugin._calls.call = call
    result = plugin.api_search({"query": "慢搜索", "request_id": "search-1"})

    assert result["success"] and result["status"] == "empty"
    assert observed == {"request_id": "search-1", "provider": "metadata", "timeout": 300}


def test_duplicate_remote_bindings_keep_saved_lists_visible_and_repairable(plugin):
    from app.plugins.embycollectionsync.models import CollectionBinding, Target

    first = MediaList("旧规则一", id="list-1", targets=[
        Target("Emby", id="target-1", collection=CollectionBinding(True, "合集一", "7473"))
    ])
    second = MediaList("旧规则二", id="list-2", targets=[
        Target("Emby", id="target-2", collection=CollectionBinding(True, "合集二", "7473"))
    ])
    plugin.init_plugin({"schema_version": 3, "enabled": True, "lists": [first.to_dict(), second.to_dict()]})

    result = plugin.api_lists()
    assert result["success"], result
    assert [item["name"] for item in result["lists"]] == ["旧规则一", "旧规则二"]
    assert "重复绑定" in plugin.api_settings()["binding_error"]
    assert not plugin.get_state()
    assert plugin._config["enabled"] is False

    repaired = plugin.api_save_lists({"action": "delete", "list_id": "list-2", "revision": 0})
    assert repaired["success"], repaired
    assert [item["id"] for item in repaired["lists"]] == ["list-1"]
    assert plugin.api_settings()["binding_error"] is None


def test_once_does_not_refresh_follow_adds_only_unseen_and_does_not_resurrect_removed(plugin):
    plugin._host.collections = {10: [member(1)], 11: [member(2), member(3)]}
    preview = plugin.api_tmdb_collection({"tmdb_id": 10, "mode": "once"})
    assert preview["source"]["mode"] == "once"
    item = create(plugin, members=[member(1)], sources=[preview["source"],
        {"tmdb_id": 11, "mode": "follow", "seen_keys": ["movie:2"]}])
    plugin._host.collection_calls = []
    media_list = plugin._find(item["id"])
    plugin._refresh_sources(media_list)
    assert plugin._host.collection_calls == [11]
    assert media_list.member_keys == ["movie:1", "movie:3"]
    media_list.remove_member("movie:3")
    plugin._refresh_sources(media_list)
    assert media_list.member_keys == ["movie:1"]


def test_failed_follow_source_keeps_members(plugin):
    item = create(plugin, members=[member()], sources=[{"tmdb_id": 404, "mode": "follow"}])
    media_list = plugin._find(item["id"])
    plugin._refresh_sources(media_list)
    assert media_list.member_keys == ["movie:1"]
    assert media_list.sources[0].error


def test_follow_invalid_late_record_is_atomic(plugin):
    plugin._host.collections = {10: [member(2), member(3, "tv")]}
    item = create(plugin, members=[member(1)], sources=[{"tmdb_id": 10, "mode": "follow", "seen_keys": ["movie:1"]}])
    media_list = plugin._find(item["id"])
    results = plugin._refresh_sources(media_list)
    assert results[0]["success"] is False
    assert media_list.member_keys == ["movie:1"]
    assert media_list.sources[0].seen_keys == ["movie:1"]


def test_unsupported_server_is_explicit_and_never_called(plugin):
    plugin._host.servers = lambda: {"Plex": {"name": "Plex", "kind": "unsupported"}}
    response = plugin.api_library_media({"server_id": "Plex"})
    assert response["error_code"] == "unsupported"
    assert not response["success"]


def test_library_picker_excludes_selected_typed_keys_and_keeps_series(plugin):
    item = create(plugin, members=[member(1)])
    class Client:
        def list_media(self, library_ids=None, media_type="all"):
            assert media_type == "all"
            return [EmbyItem("m", "Movie", tmdb_id=1, media_type="movie"), EmbyItem("s", "Series", tmdb_id=1, media_type="tv")]
    plugin._clients["Emby"] = Client()
    result = plugin.api_library_media({"server_id": "Emby", "list_id": item["id"]})
    assert [record["key"] for record in result["items"]] == ["tv:1"]


def test_server_api_never_returns_credentials(plugin):
    response = plugin.api_servers()
    assert "secret" not in str(response)
    assert "api_key" not in str(response)
    assert response["items"][0]["capabilities"]["tv_playlist"] is True


def test_plex_factory_reuses_mp_token_without_user_lookup_and_refreshes_config(plugin):
    from app.plugins.embycollectionsync.plex import PlexClient
    conf = {'kind':'plex', 'host':'http://server/plex', 'token':'plex-private', 'username':'not-needed'}
    plugin._host.servers = lambda: {'Plex': conf}
    client = plugin._client('Plex')
    assert isinstance(client, PlexClient) and client.api_key == 'plex-private'
    assert plugin._client('Plex') is client
    conf['token'] = 'replacement-private'
    assert plugin._client('Plex') is not client
    response = plugin.api_servers()
    assert response['items'][0]['capabilities']['tv_playlist']
    assert 'private' not in str(response)


def test_plex_save_and_member_api_force_incompatible_collection_off(plugin):
    listing = MediaList('Plex', members=[MediaMember(1)], targets=[Target('Plex','plex',collection=CollectionBinding(True,remote_id='500'))])
    response = plugin.api_save_lists({'list': listing.to_dict()})
    assert response['success']
    result = plugin.api_members({'list_id':listing.id,'items':[member(1,'tv')]})
    assert result['success'] and result['list']['targets'][0]['collection'] == {'enabled':False,'name':'','remote_id':'500'}
    raw = result['list']; raw['targets'][0]['collection']['enabled'] = True
    again = plugin.api_save_lists({'list':raw})
    assert again['success'] and not again['list']['targets'][0]['collection']['enabled']


@pytest.mark.parametrize('kind', ['emby', 'jellyfin'])
def test_factory_uses_protocol_and_preserves_existing_list_content(plugin, monkeypatch, kind):
    import app.plugins.embycollectionsync as module
    calls = []
    class Client:
        def __init__(self, host, key, user, **kwargs):
            calls.append((host, key, user))
            self.user_id = user
        def resolve_authenticated_user_id(self):
            return 'token-owner'
    monkeypatch.setitem(module.SERVER_CLIENTS, kind, Client)
    plugin._host.servers = lambda: {'family': {'kind': kind, 'host':'http://local/base', 'apikey':'api-secret',
        'access_token':'user-secret', 'user_id':'token-owner'}}
    item = create(plugin, members=[member(1), {**member(2,'tv'), 'seasons':[3,4]}])
    before = plugin._find(item['id']).to_dict()
    client = plugin._client('family')
    assert calls == [('http://local/base', 'api-secret', 'token-owner')]
    assert plugin._client('family') is client
    assert plugin._find(item['id']).to_dict() == before
    response = plugin.api_servers()
    assert response['items'][0]['capabilities']['tv_playlist'] is True
    assert 'secret' not in str(response)


def test_jellyfin_factory_resolves_user_token_and_rejects_different_configured_owner(plugin, monkeypatch):
    import app.plugins.embycollectionsync as module
    class Client:
        def __init__(self, *args, **kwargs): self.user_id = ''
        def resolve_authenticated_user_id(self): return 'aabb'
    monkeypatch.setitem(module.SERVER_CLIENTS,'jellyfin',Client)
    conf = {'kind':'jellyfin','host':'http://local','apikey':'private','default_user_id':'mp-default-user'}
    plugin._host.servers = lambda: {'j':conf}
    assert plugin._client('j').user_id == 'aabb'
    plugin._clients.clear()
    conf['user_id'] = 'different'
    from app.plugins.embycollectionsync.jellyfin import JellyfinAuthError
    with pytest.raises(JellyfinAuthError, match='不一致'):
        plugin._client('j')
    assert not plugin._clients


def test_jellyfin_application_key_resolves_configured_username_for_collections(plugin, monkeypatch):
    import app.plugins.embycollectionsync as module
    class Client:
        def __init__(self, *args, **kwargs): self.user_id = ''
        def resolve_authenticated_user_id(self): return None
        def resolve_user_id(self, name):
            assert name == '影院用户'
            return 'owner'
    monkeypatch.setitem(module.SERVER_CLIENTS,'jellyfin',Client)
    plugin._host.servers = lambda: {'j':{'kind':'jellyfin','host':'http://local','apikey':'private','username':'影院用户'}}
    assert plugin._client('j').user_id == 'owner'


def test_network_task_does_not_block_api_or_allow_concurrent_save(plugin, monkeypatch):
    gate, started = threading.Event(), threading.Event()
    class Publisher:
        def __init__(self, client):
            pass
        def publish(self, *args, **kwargs):
            started.set()
            gate.wait(2)
            return {"success": True}
    monkeypatch.setattr("app.plugins.embycollectionsync.Publisher", Publisher)
    plugin._clients["Emby"] = object()
    item = create(plugin, targets=[{"server_id": "Emby", "collection": {"enabled": True}}])
    before = time.monotonic()
    queued = plugin.api_publish({"list_id": item["id"]})
    assert time.monotonic() - before < 0.3
    assert started.wait(1)
    before = time.monotonic()
    assert plugin.api_lists()["lists"]
    assert plugin.api_save_lists({"list": item})["error_code"] == "busy"
    assert plugin.api_publish({})["error_code"] == "busy"
    assert time.monotonic() - before < 0.3
    gate.set()
    deadline = time.monotonic() + 1
    while plugin.api_job(queued["job_id"])["status"] in {"queued", "running"} and time.monotonic() < deadline:
        time.sleep(0.005)
    assert plugin.api_job(queued["job_id"])["status"] == "success"


def test_pre_publish_draft_cannot_erase_new_binding(plugin, monkeypatch):
    class Publisher:
        def __init__(self, client):
            pass
        def publish(self, media_list, target, **kwargs):
            target.collection.remote_id = "new-remote-id"
            kwargs["on_binding"]()
            return {"success": True}
    monkeypatch.setattr("app.plugins.embycollectionsync.Publisher", Publisher)
    plugin._clients["Emby"] = object()
    draft = create(plugin, members=[member()], targets=[{"server_id": "Emby", "collection": {"enabled": True}}])
    queued = plugin.api_publish({"list_id": draft["id"]})
    deadline = time.monotonic() + 1
    while plugin.api_job(queued["job_id"])["status"] in {"queued", "running"} and time.monotonic() < deadline:
        time.sleep(0.005)
    assert plugin.api_save_lists({"list": draft})["error_code"] == "conflict"
    assert plugin.api_lists()["lists"][0]["targets"][0]["collection"]["remote_id"] == "new-remote-id"


def test_automatic_subscription_is_opt_in_and_idempotent(plugin):
    class Client:
        def list_media(self, *args):
            return []
    plugin._clients["Emby"] = Client()
    item = create(plugin, members=[member()], policy={"auto_subscribe": True})
    media_list = plugin._find(item["id"])
    assert plugin._subscribe_pending(media_list)[0]["success"]
    cached = plugin._subscribe_pending(media_list)
    assert len(cached) == 1 and cached[0].get("already_exists") and cached[0]["success"]
    assert len(plugin._host.subscriptions) == 1


def test_saving_auto_subscribe_list_does_not_create_subscriptions(plugin):
    result = plugin.api_save_lists({"list": {
        "name": "保存时不订阅", "members": [member()],
        "policy": {"auto_subscribe": True}, "targets": [{"server_id": "Emby"}],
    }})
    assert result["success"]
    assert plugin._host.subscriptions == []


def test_target_publish_error_does_not_skip_separate_auto_subscription(plugin, monkeypatch):
    class Client:
        def list_media(self, *args):
            return []

    class Publisher:
        def __init__(self, client):
            pass

        def publish(self, media_list, target, **kwargs):
            return {"success": False, "status": "error", "operation": "publish", "target_id": target.id}

    monkeypatch.setattr("app.plugins.embycollectionsync.Publisher", Publisher)
    plugin._clients["Emby"] = Client()
    stored = create(plugin, members=[member()], policy={"auto_subscribe": True}, targets=[{"server_id": "Emby"}])
    results = plugin._publish_selected([plugin._find(stored["id"])], generation=plugin._stop)
    assert any(result.get("operation") == "publish" and not result["success"] for result in results)
    assert any(result.get("operation") == "subscribe" and result["success"] for result in results)
    assert len(plugin._host.subscriptions) == 1


def test_unmatched_library_is_nonblocking_when_automatic_subscriptions_succeed(plugin, monkeypatch):
    class Client:
        def list_media(self, *args):
            return []

    class Publisher:
        def __init__(self, client):
            pass

        def publish(self, media_list, target, **kwargs):
            return {
                "success": False,
                "status": "error",
                "error": "媒体库中未找到片单内容，本次未作修改",
                "error_code": "no_matches",
                "list_id": media_list.id,
                "target_id": target.id,
            }

    monkeypatch.setattr("app.plugins.embycollectionsync.Publisher", Publisher)
    plugin._clients["Emby"] = Client()
    item = create(plugin, members=[member()], policy={"auto_subscribe": True},
                  targets=[{"server_id": "Emby"}])

    queued = plugin.api_publish({"list_id": item["id"]})
    deadline = time.monotonic() + 1
    while plugin.api_job(queued["job_id"])["status"] in {"queued", "running"} and time.monotonic() < deadline:
        time.sleep(0.005)

    result = plugin.api_job(queued["job_id"])
    assert result["status"] == "success"
    media = next(row for row in result["results"] if row.get("error_code") == "no_matches")
    assert media["success"] is True and media["status"] == "success" and media["error"] is None
    assert len(plugin._host.subscriptions) == 1


def test_unmatched_library_succeeds_when_subscription_already_exists(plugin, monkeypatch):
    class Client:
        def list_media(self, *args):
            return []

    class Publisher:
        def __init__(self, client):
            pass

        def publish(self, media_list, target, **kwargs):
            return {"success": False, "status": "error", "error_code": "no_matches",
                    "error": "媒体库中未找到片单内容，本次未作修改"}

    monkeypatch.setattr("app.plugins.embycollectionsync.Publisher", Publisher)
    plugin._clients["Emby"] = Client()
    plugin._subscriptions["movie:1"] = 41
    plugin._host.active_subscriptions[41] = member()
    item = create(plugin, members=[member()], policy={"auto_subscribe": True},
                  targets=[{"server_id": "Emby"}])

    queued = plugin.api_publish({"list_id": item["id"]})
    deadline = time.monotonic() + 1
    while plugin.api_job(queued["job_id"])["status"] in {"queued", "running"} and time.monotonic() < deadline:
        time.sleep(0.005)

    result = plugin.api_job(queued["job_id"])
    assert result["status"] == "success"
    assert any(row.get("already_exists") and row["success"] for row in result["results"])
    assert plugin._host.subscriptions == []


def test_no_matches_without_other_actions_still_completes_successfully(plugin, monkeypatch):
    class Client:
        def list_media(self, *args):
            return []

    class Publisher:
        def __init__(self, client):
            pass

        def publish(self, media_list, target, **kwargs):
            return {"success": False, "status": "error", "error_code": "no_matches",
                    "error": "媒体库中未找到片单内容，本次未作修改"}

    monkeypatch.setattr("app.plugins.embycollectionsync.Publisher", Publisher)
    plugin._clients["Emby"] = Client()
    item = create(plugin, members=[member()], targets=[{"server_id": "Emby"}])
    queued = plugin.api_publish({"list_id": item["id"]})
    deadline = time.monotonic() + 1
    while plugin.api_job(queued["job_id"])["status"] in {"queued", "running"} and time.monotonic() < deadline:
        time.sleep(0.005)

    result = plugin.api_job(queued["job_id"])
    assert result["status"] == "success"
    assert result["results"][0]["success"] is True
    assert result["results"][0]["status"] == "success"
    assert result["results"][0]["error"] is None


def test_subscription_call_allows_slow_host_chain(plugin, monkeypatch):
    import app.plugins.embycollectionsync as module

    class Client:
        def list_media(self, *args):
            return []

    monkeypatch.setattr(module, "SUBSCRIPTION_TIMEOUT_SECONDS", 0.2, raising=False)
    plugin._calls.stop()
    plugin._calls = ProviderCalls(timeout=0.015, cooldown=0.02, capacity=2)
    original_subscribe = plugin._host.subscribe

    def slow_subscribe(payload):
        time.sleep(0.05)
        return original_subscribe(payload)

    monkeypatch.setattr(plugin._host, "subscribe", slow_subscribe)
    plugin._clients["Emby"] = Client()
    item = create(plugin, members=[member()], policy={"auto_subscribe": True},
                  targets=[{"server_id": "Emby"}])

    result = plugin._subscribe_pending(plugin._find(item["id"]))
    assert len(result) == 1 and result[0]["status"] == "subscribed"


@pytest.mark.parametrize("auto_subscribe,dry_run", [(True, False), (False, False), (True, True), (False, True)])
def test_manual_publish_applies_opt_in_subscription_only_for_real_runs(plugin, monkeypatch, auto_subscribe, dry_run):
    class Client:
        def list_media(self, *args):
            return [EmbyItem("existing", tmdb_id=1)]

    class Publisher:
        def __init__(self, client):
            pass

        def publish(self, media_list, target, **kwargs):
            return {"success": True, "list_id": media_list.id, "target_id": target.id}

    monkeypatch.setattr("app.plugins.embycollectionsync.Publisher", Publisher)
    plugin._clients["Emby"] = Client()
    item = create(plugin, members=[member(1), member(2), member(3)],
                  policy={"auto_subscribe": auto_subscribe}, targets=[{"server_id": "Emby"}])
    # 全局开关仅关闭定时与事件自动化，用户仍可明确发起正式发布。
    assert plugin._config["enabled"] is False

    def publish_and_wait():
        queued = plugin.api_publish({"list_id": item["id"], "dry_run": dry_run})
        deadline = time.monotonic() + 1
        while plugin.api_job(queued["job_id"])["status"] in {"queued", "running"} and time.monotonic() < deadline:
            time.sleep(0.005)
        result = plugin.api_job(queued["job_id"])
        assert result["status"] == "success"
        return result

    first = publish_and_wait()
    if auto_subscribe and not dry_run:
        assert [member["tmdb_id"] for member in plugin._host.subscriptions] == [2, 3]
        subscribed = [result for result in first["results"] if result.get("status") == "subscribed"]
        assert [result["key"] for result in subscribed] == ["movie:2", "movie:3"]
        assert all(result["list_id"] == item["id"] and result["operation"] == "subscribe" for result in subscribed)
        publish_and_wait()
        assert len(plugin._host.subscriptions) == 2
    else:
        assert plugin._host.subscriptions == []
        assert plugin._subscriptions == {}


def test_auto_subscription_requires_confirmed_library_read(plugin):
    item = create(plugin, members=[member()], policy={"auto_subscribe": True})
    plugin._host.servers = lambda: {}
    results = plugin._subscribe_pending(plugin._find(item["id"]))
    assert results[0]["error_code"] == "library_check_unavailable"
    assert plugin._host.subscriptions == []


@pytest.mark.parametrize("replacement", [None, member(999)])
def test_subscription_cache_is_rechecked_after_host_deletion_or_id_reuse(plugin, replacement):
    class Client:
        def list_media(self, *args):
            return []
    plugin._clients["Emby"] = Client()
    item = create(plugin, members=[member()], policy={"auto_subscribe": True})
    listing = plugin._find(item["id"])
    assert plugin._subscribe_pending(listing)[0]["success"]
    if replacement is None:
        plugin._host.active_subscriptions.clear()
    else:
        plugin._host.active_subscriptions[1] = replacement
    assert plugin._subscribe_pending(listing)[0]["success"]
    assert len(plugin._host.subscriptions) == 2
    assert plugin._subscriptions["movie:1"] == 2
    cached = plugin._subscribe_pending(listing)
    assert len(cached) == 1 and cached[0].get("already_exists") and cached[0]["success"]


def test_recent_submission_waits_for_download_before_recreating_subscription(plugin):
    class Client:
        def list_media(self, *args):
            return []

    plugin._clients["Emby"] = Client()
    plugin._host.download_pending = lambda member, submitted_at: True
    item = create(plugin, members=[member()], policy={"auto_subscribe": True})
    listing = plugin._find(item["id"])
    assert plugin._subscribe_pending(listing)[0]["success"]
    plugin._host.active_subscriptions.clear()
    result = plugin._subscribe_pending(listing)
    assert result[0]["success"] and result[0]["pending_download"]
    assert result[0]["pending_download_state"] == "downloading"
    assert len(plugin._host.subscriptions) == 1


def test_subscription_lookup_failure_keeps_cache_and_does_not_create_again(plugin):
    class Client:
        def list_media(self, *args):
            return []
    plugin._clients["Emby"] = Client()
    item = create(plugin, members=[member()], policy={"auto_subscribe": True})
    listing = plugin._find(item["id"])
    assert plugin._subscribe_pending(listing)[0]["success"]
    def unavailable(*args):
        raise RuntimeError("secret")
    plugin._host.subscription_exists = unavailable
    result = plugin._subscribe_pending(listing)
    assert result[0]["error_code"] == "subscription_check_failed"
    assert "secret" not in str(result)
    assert plugin._subscriptions["movie:1"] == 1
    assert len(plugin._host.subscriptions) == 1


@pytest.mark.parametrize("value", ["false", "true", 1, 0, None])
def test_destructive_allow_empty_requires_real_boolean(plugin, value):
    assert not plugin.api_publish({"allow_empty": value})["success"]


def test_two_lists_cannot_share_remote_output(plugin):
    create(plugin, targets=[{"server_id": "Emby", "collection": {"remote_id": "c1"}}])
    result = plugin.api_save_lists({"list": {"name": "other", "targets": [{"server_id": "Emby", "collection": {"remote_id": "c1"}}]}})
    assert not result["success"]
    assert "远端输出" in result["error"] and "c1" in result["error"]
    assert "片单" in result["error"] and "other" in result["error"]
    assert "第1条" in result["error"] and "第2条" in result["error"]
    assert len(plugin.api_lists()["lists"]) == 1


def test_conflicting_legacy_rules_keep_backup_show_lists_and_explain_owners():
    plugin = EmbyCollectionSync()
    original = {"rules": [
        {"name": "新规则", "collection": {"emby_item_id": "7473"}},
        {"name": "新规则", "collection": {"emby_item_id": "7473"}},
    ]}
    try:
        plugin.init_plugin(original)
        result = plugin.api_lists()
        assert result["success"]
        assert [item["name"] for item in result["lists"]] == ["新规则", "新规则"]
        assert "远端输出" in plugin.api_settings()["binding_error"] and "7473" in plugin.api_settings()["binding_error"]
        assert "第1条" in plugin.api_settings()["binding_error"] and "第2条" in plugin.api_settings()["binding_error"]
        assert plugin.get_data("migration_backup_v2")["config"] == original
        assert plugin._config["schema_version"] == 3
        assert plugin._config["enabled"] is False
        assert len(plugin._config["lists"]) == 2
    finally:
        plugin.stop_service()


def test_failed_storage_does_not_mutate_saved_members_or_lists(plugin, monkeypatch):
    item = create(plugin, members=[member()])
    monkeypatch.setattr(plugin, "update_config", lambda config: False)
    result = plugin.api_members({"list_id": item["id"], "items": [member(2)]})
    assert result["error_code"] == "persistence_error"
    assert plugin._find(item["id"]).member_keys == ["movie:1"]
    result = plugin.api_save_lists({"list": {**item, "name": "changed"}})
    assert result["error_code"] == "persistence_error"
    assert plugin.api_lists()["lists"] == [item]


def test_reload_waits_for_create_and_retains_new_binding(plugin, monkeypatch):
    gate, created = threading.Event(), threading.Event()
    class Publisher:
        def __init__(self, client):
            pass
        def publish(self, media_list, target, **kwargs):
            created.set()
            gate.wait(1)
            target.collection.remote_id = "created-during-reload"
            kwargs["on_binding"]()
            return {"success": False}
    monkeypatch.setattr("app.plugins.embycollectionsync.Publisher", Publisher)
    plugin._clients["Emby"] = object()
    item = create(plugin, targets=[{"server_id": "Emby", "collection": {"enabled": True}}])
    pending = copy.deepcopy(plugin._config)
    queued = plugin.api_publish({"list_id": item["id"]})
    assert created.wait(1)
    started = time.monotonic()
    plugin.init_plugin(pending)
    assert time.monotonic() - started < 0.3
    assert plugin._stop.is_set()
    gate.set()
    plugin._reload_thread.join(1)
    assert not plugin._reload_thread.is_alive()
    assert plugin.api_lists()["lists"][0]["targets"][0]["collection"]["remote_id"] == "created-during-reload"
    assert plugin._stored_config["lists"][0]["targets"][0]["collection"]["remote_id"] == "created-during-reload"


def test_provider_capacity_survives_reload(plugin):
    gate = threading.Event()
    plugin._calls.timeout = 0.01
    for index in range(2):
        assert plugin._calls.call(lambda: gate.wait(1), provider=str(index))["status"] == "timeout"
    config = copy.deepcopy(plugin._config)
    try:
        plugin.init_plugin(config)
        assert plugin._calls.call(lambda: 1)["status"] == "busy"
    finally:
        gate.set()


def test_scheduler_legacy_nonintegral_hour_uses_exact_interval():
    plugin = EmbyCollectionSync()
    plugin.init_plugin({"enabled": True, "interval_minutes": 90})
    assert plugin.get_service()[0]["trigger"].interval.total_seconds() == 5400
    plugin.stop_service()
