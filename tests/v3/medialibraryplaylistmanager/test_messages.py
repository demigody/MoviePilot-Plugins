import copy
import re

import pytest

from app.plugins.medialibraryplaylistmanager.emby import EmbyAPIError
from app.plugins.medialibraryplaylistmanager.messages import diagnostic, public_diagnostics
from app.plugins.medialibraryplaylistmanager.migration import migrate_config
from app.plugins.medialibraryplaylistmanager.models import MediaMember
from .test_api import plugin


def test_legacy_diagnostics_are_localized_without_changing_media_or_protocol_data():
    record = {"status": "partial", "message": "Published membership could not be verified; removals skipped",
        "list_name": "Published membership could not be verified; removals skipped",
        "results": [{"error": "Playlist order could not be verified; removals skipped",
            "error_code": "server_error", "server_id": "Plex", "pending": ["tv:111697:season:3"]}]}
    saved = copy.deepcopy(record)
    rendered = public_diagnostics(record)
    assert "跳过移除" in rendered["message"]
    assert "播放列表顺序" in rendered["results"][0]["error"]
    assert rendered["list_name"] == record["list_name"]
    assert rendered["status"] == "partial"
    assert rendered["results"][0]["pending"] == ["tv:111697:season:3"]
    assert rendered["results"][0]["error_code"] == "server_error"
    assert record == saved


def test_history_and_job_translate_old_errors_only_in_response_copies(plugin):
    record = {"job_id": "old", "status": "partial", "message": "custom note",
        "results": [{"error": "Published membership could not be verified; removals skipped"}]}
    plugin._history = [copy.deepcopy(record)]
    plugin._jobs = {"old": copy.deepcopy(record)}
    saved = copy.deepcopy((plugin._history, plugin._jobs, plugin._history_revision))
    history, job = plugin.api_history(), plugin.api_job("old")
    assert "跳过移除" in history["items"][0]["results"][0]["error"]
    assert job["results"] == history["items"][0]["results"]
    assert history["items"][0]["message"] == "custom note"
    assert (plugin._history, plugin._jobs, plugin._history_revision) == saved


@pytest.mark.parametrize("action,expected", [
    (lambda: MediaMember(False), "TMDB 编号"),
    (lambda: MediaMember(1, "Episode"), "媒体类型"),
    (lambda: migrate_config({"schema_version": 3, "lists": [], "cron": "bad"}), "执行周期"),
    (lambda: migrate_config({"schema_version": 3, "lists": {}, "cron": "* * * * *"}), "片单"),
    (lambda: MediaMember.from_dict([]), "媒体信息"),
])
def test_invalid_configuration_gives_specific_chinese_diagnostics(action, expected):
    with pytest.raises(ValueError) as error:
        action()
    assert expected in str(error.value)


def test_http_status_is_kept_and_legacy_schedule_warning_is_readable():
    assert "HTTP 403" in diagnostic("Media server returned HTTP 403")
    assert "90 分钟" in diagnostic("Legacy 90 minute interval retained as migrated_interval_minutes; scheduler must use the interval until cron is changed.")
    error = EmbyAPIError("Media server connection failed", status=503, code="connection_error")
    assert "无法连接" in str(error) and error.status == 503 and error.code == "connection_error"
    assert re.search("[\u3400-\u9fff]", str(EmbyAPIError("Unexpected remote failure")))
