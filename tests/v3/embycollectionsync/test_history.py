import copy
import time

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from .test_api import create, member, plugin
from app.plugins.embycollectionsync import _manage_user


def seed_history(plugin, records=None):
    plugin.save_data("history", records if records is not None else [
        {"job_id": f"j{index}", "status": "success", "created_at": "2026-10-14T12:00:00Z", "results": []}
        for index in range(5)
    ])
    plugin.init_plugin(copy.deepcopy(plugin._config))
    return plugin.api_history()


def test_legacy_history_gets_stable_unique_ids_without_losing_record_content(plugin):
    records = [{"timestamp": "old", "message": "legacy"}, {"job_id": "same"}, {"job_id": "same"}]
    snapshot = seed_history(plugin, records)
    assert len({row["history_id"] for row in snapshot["items"]}) == 3
    assert snapshot["items"][-1]["message"] == "legacy"
    assert isinstance(snapshot["revision"], int)
    plugin.init_plugin(copy.deepcopy(plugin._config))
    assert plugin.api_history() == snapshot


def test_selected_history_deletion_is_persistent_and_does_not_touch_domain_or_jobs(plugin):
    snapshot = seed_history(plugin)
    create(plugin, members=[member(1)])
    plugin._subscriptions = {"movie:1": 7}
    plugin._jobs = {"running": {"job_id": "running", "status": "running"}, "j4": {"job_id": "j4", "status": "success"}}
    unrelated = copy.deepcopy((plugin._config, plugin._subscriptions, plugin._jobs))
    chosen = [snapshot["items"][0]["history_id"], snapshot["items"][2]["history_id"]]
    plugin._publish_lock.acquire()
    try:
        result = plugin.api_delete_history({"action": "delete", "ids": chosen})
    finally:
        plugin._publish_lock.release()
    assert result["success"] and result["deleted_count"] == 2
    assert result["revision"] > snapshot["revision"]
    assert {row["history_id"] for row in result["items"]} == {row["history_id"] for row in snapshot["items"]} - set(chosen)
    assert (plugin._config, plugin._subscriptions, plugin._jobs) == unrelated
    plugin.init_plugin(copy.deepcopy(plugin._config))
    assert plugin.api_history()["items"] == result["items"]


def test_clear_all_history_and_empty_clear_are_safe(plugin):
    seed_history(plugin)
    result = plugin.api_delete_history({"action": "clear"})
    assert result["success"] and result["deleted_count"] == 5 and result["items"] == []
    again = plugin.api_delete_history({"action": "clear"})
    assert again["success"] and again["deleted_count"] == 0
    assert again["revision"] == result["revision"]
    plugin.init_plugin(copy.deepcopy(plugin._config))
    assert plugin.api_history()["items"] == []


@pytest.mark.parametrize("payload", [{}, {"action": "delete", "ids": []}, {"action": "delete", "ids": "j1"},
    {"action": "delete", "ids": [True]}, {"action": "delete", "ids": ["missing", "j1"]},
    {"action": "clear", "ids": ["j1"]}, {"action": "unknown"}])
def test_invalid_or_stale_delete_never_partially_clears_history(plugin, payload):
    before = seed_history(plugin)
    result = plugin.api_delete_history(payload)
    assert not result["success"]
    assert plugin.api_history() == before


@pytest.mark.parametrize("failure", [False, RuntimeError("private-database-url")])
def test_delete_storage_failure_preserves_live_history_and_revision(plugin, monkeypatch, failure):
    before = seed_history(plugin)
    def fail(*args):
        if isinstance(failure, Exception):
            raise failure
        return failure
    monkeypatch.setattr(plugin, "save_data", fail)
    result = plugin.api_delete_history({"action": "clear"})
    assert result["error_code"] == "persistence_error" and "private" not in str(result)
    assert plugin.api_history() == before


def test_history_delete_route_reads_json_body_and_uses_manage_permission(plugin):
    seed_history(plugin)
    app = FastAPI()
    app.dependency_overrides[_manage_user] = lambda: True
    for route in plugin.get_api():
        app.add_api_route("/" + route["path"].lstrip("/"), route["endpoint"], methods=route["methods"], dependencies=route["dependencies"])
    with TestClient(app) as client:
        result = client.post("/history", json={"action": "delete", "ids": ["j1"]})
    assert result.status_code == 200 and result.json()["deleted_count"] == 1
    route = next(route for route in plugin.get_api() if route["path"] == "/history" and route["methods"] == ["POST"])
    assert route["auth"] == "bear" and route["dependencies"]


def test_history_delete_rejects_unprivileged_requests_and_stopped_instances(plugin):
    before = seed_history(plugin)
    app = FastAPI()
    def deny():
        raise HTTPException(status_code=403, detail="no management permission")
    app.dependency_overrides[_manage_user] = deny
    for route in plugin.get_api():
        app.add_api_route("/" + route["path"].lstrip("/"), route["endpoint"], methods=route["methods"], dependencies=route["dependencies"])
    with TestClient(app) as client:
        assert client.post("/history", json={"action": "clear"}).status_code == 403
    assert plugin.api_history() == before
    plugin.stop_service()
    assert plugin.api_delete_history({"action": "clear"})["error_code"] == "unavailable"
    assert plugin.api_history() == before


def test_new_finished_jobs_append_after_clear_without_restoring_deleted_records(plugin, monkeypatch):
    before = seed_history(plugin)
    plugin.api_delete_history({"action": "clear"})

    class Client:
        def list_libraries(self):
            return []

    class Publisher:
        def __init__(self, client):
            pass

        def publish(self, media_list, target, **kwargs):
            return {"success": True, "status": "success", "list_id": media_list.id, "target_id": target.id}

    monkeypatch.setattr("app.plugins.embycollectionsync.Publisher", Publisher)
    plugin._clients["Emby"] = Client()
    media_list = create(plugin, members=[member(1)], targets=[{"server_id": "Emby"}])
    queued = plugin.api_publish({"list_id": media_list["id"], "dry_run": True})
    deadline = time.monotonic() + 1
    while plugin.api_job(queued["job_id"])["status"] in {"queued", "running"} and time.monotonic() < deadline:
        time.sleep(0.005)
    after = plugin.api_history()
    assert len(after["items"]) == 1
    assert after["items"][0]["job_id"] == queued["job_id"]
    assert after["revision"] > before["revision"]
    assert {row["history_id"] for row in before["items"]}.isdisjoint({row["history_id"] for row in after["items"]})
