import copy

import pytest

from app.plugins.medialibraryplaylistmanager.migration import migrate_config, validate_cron
from app.plugins.medialibraryplaylistmanager.models import CollectionBinding, MediaList, MediaMember, Playlist, Source, Target, validate_bindings


def test_typed_identity_dedup_and_independent_order_roundtrip():
    target = Target("Emby", playlists=[Playlist("reverse", order=["tv:1", "movie:1"]), Playlist("forward")])
    listing = MediaList("Mixed", members=[MediaMember(1), MediaMember(1, "tv"), MediaMember(1)], targets=[target])
    assert listing.member_keys == ["movie:1", "tv:1"]
    assert target.playlists[0].order == ["tv:1", "movie:1"]
    assert target.playlists[1].order == ["movie:1", "tv:1"]
    listing.add_member(MediaMember(2))
    assert target.playlists[0].order == ["tv:1", "movie:1", "movie:2"]
    listing.remove_member("movie:1")
    assert listing.member_keys == ["tv:1", "movie:2"]
    assert target.playlists[0].order == ["tv:1", "movie:2"]
    assert MediaList.from_dict(listing.to_dict()).to_dict() == listing.to_dict()
    assert "emby_item_id" not in listing.members[0].to_dict()


@pytest.mark.parametrize("kind", ["Episode", "Season", "garbage"])
def test_invalid_media_kind_rejects_whole_list(kind):
    with pytest.raises(ValueError):
        MediaList.from_dict({"name": "invalid", "members": [{"tmdb_id": 1}, {"tmdb_id": 2, "media_type": kind}]})


@pytest.mark.parametrize("value", [0, -1, True, "1.2", "1;rm", None])
def test_invalid_tmdb_id_is_not_silently_dropped(value):
    with pytest.raises(ValueError):
        MediaMember(value)


def test_new_list_defaults_to_no_automation_and_no_target():
    listing = MediaList("Local")
    assert listing.targets == []
    assert listing.policy == {"auto_subscribe": False, "organize_category": "", "publish_on_library": False}


def test_source_seen_keys_survive_member_removal():
    source = Source(10, mode="follow", seen_keys=["movie:1"])
    listing = MediaList("Source", members=[MediaMember(1)], sources=[source])
    listing.remove_member("movie:1")
    assert listing.sources[0].seen_keys == ["movie:1"]
    with pytest.raises(ValueError):
        Source(10, seen_keys=["tv:1"])


def test_legacy_migration_preserves_metadata_typed_ids_binding_order_and_credentials():
    raw = {"enabled": True, "server_name": "My Emby", "library_ids": ["lib"], "emby": {"host": "http://emby", "api_key": "private"}, "daily_time": "04:05", "rules": [{"id": "r", "name": "Series", "media_type": "tv", "movies": [{"tmdb_id": 1, "title": "A", "year": 2020, "emby_item_id": "s1"}, {"tmdb_id": 2}], "collection": {"enabled": True, "emby_item_id": "c"}, "playlists": [{"id": "p", "name": "Order", "ordered_movie_ids": [2, 1], "emby_item_id": "p1"}]}]}
    original = copy.deepcopy(raw)
    migrated = migrate_config(raw)
    assert raw == original
    listing = MediaList.from_dict(migrated["lists"][0])
    assert listing.id == "r" and listing.member_keys == ["tv:1", "tv:2"]
    assert listing.members[0].title == "A" and listing.members[0].year == 2020
    assert listing.targets[0].server_id == "My Emby"
    assert listing.targets[0].library_ids == ["lib"]
    assert listing.targets[0].collection.remote_id == "c"
    assert listing.targets[0].playlists[0].order == ["tv:2", "tv:1"]
    assert listing.targets[0].playlists[0].remote_id == "p1"
    assert migrated["emby"] == raw["emby"] and migrated["cron"] == "5 4 * * *"
    assert migrate_config(migrated) == migrated


def test_flat_form_overrides_old_snapshot_and_preserves_matching_metadata():
    raw = {"rules": [{"id": "r", "name": "Before", "movies": [{"tmdb_id": 1, "title": "One"}], "playlists": [{"id": "p", "ordered_movie_ids": [1]}]}], "rule_r_name": "After", "rule_r_movie_ids": "2\n1", "rule_r_enabled": False, "rule_r_collection_enabled": True, "playlist_p_order": "1,2", "playlist_p_name": "Sorted"}
    listing = MediaList.from_dict(migrate_config(raw)["lists"][0])
    assert listing.name == "After" and not listing.enabled
    assert listing.member_keys == ["movie:2", "movie:1"]
    assert listing.members[1].title == "One"
    assert listing.targets[0].collection.enabled
    assert listing.targets[0].playlists[0].order == ["movie:1", "movie:2"]
    assert listing.targets[0].playlists[0].name == "Sorted"


def test_legacy_text_ids_and_flat_only_are_migrated_with_stable_generated_ids():
    raw = {"rule_old_name": "Legacy", "rule_old_movie_ids": "1,2;3", "interval_minutes": 120}
    first = migrate_config(raw)
    assert first == migrate_config(raw)
    assert [m["tmdb_id"] for m in first["lists"][0]["members"]] == [1, 2, 3]
    assert first["cron"] == "0 */2 * * *"
    text_config = {"rules": [{"name": "Text", "movie_ids_text": "1 2", "playlists": [{"order_text": "2,1"}]}]}
    listing = MediaList.from_dict(migrate_config(text_config)["lists"][0])
    assert listing.targets[0].playlists[0].order == ["movie:2", "movie:1"]


def test_non_cron_legacy_interval_is_preserved_for_interval_scheduler():
    migrated = migrate_config({"interval_minutes": 90})
    assert validate_cron(migrated["cron"])
    assert migrated["migrated_interval_minutes"] == 90
    assert migrated["interval_minutes"] == 90
    assert migrated["migration_warnings"]


def test_known_legacy_invalid_minute_cron_recovers_using_same_preserved_interval():
    migrated = migrate_config({"cron": "*/90 * * * *", "interval_minutes": 90})
    assert migrated["migrated_interval_minutes"] == 90
    assert validate_cron(migrated["cron"])
    assert len(migrated["migration_warnings"]) == 2
    with pytest.raises(ValueError):
        migrate_config({"cron": "*/90 * * * *", "interval_minutes": 120})


@pytest.mark.parametrize("raw", [{"rules": [{"name": "bad", "movies": [{"tmdb_id": 1}, {"tmdb_id": 0}]}]}, {"rules": [{"name": "bad", "media_type": "Episode", "movie_ids_text": "1"}]}, {"rules": [{"name": "bad", "movie_ids_text": "1 wrong 2"}]}, {"daily_time": "28:00"}, {"cron": "*/90 * * * *"}])
def test_invalid_legacy_data_aborts_without_mutating_raw(raw):
    before = copy.deepcopy(raw)
    with pytest.raises(ValueError):
        migrate_config(raw)
    assert raw == before


def test_schema3_validation_does_not_discard_invalid_member():
    raw = {"schema_version": 3, "lists": [{"name": "invalid", "members": [{"tmdb_id": 1}, {"tmdb_id": "bad"}]}]}
    with pytest.raises(ValueError):
        migrate_config(raw)


def test_remote_binding_cannot_be_reused_in_one_target():
    with pytest.raises(ValueError):
        Target("Emby", collection=CollectionBinding(True, remote_id="remote"), playlists=[Playlist(remote_id="remote")])


def test_remote_binding_cannot_be_shared_across_lists_on_same_server():
    lists = [MediaList("One", targets=[Target("Server", collection=CollectionBinding(True, remote_id="remote"))]), MediaList("Two", targets=[Target("Server", playlists=[Playlist(remote_id="remote")])])]
    with pytest.raises(ValueError):
        validate_bindings(lists)
    lists[1].targets[0].server_id = "AnotherServer"
    validate_bindings(lists)


def test_legacy_binding_conflict_is_preserved_for_runtime_repair_without_mutation():
    original = {"rules": [
        {"name": "新规则", "movies": [1], "collection": {"emby_item_id": "7473"}},
        {"name": "新规则", "movies": [1, 2], "collection": {"emby_item_id": "7473"}},
    ]}
    snapshot = copy.deepcopy(original)
    migrated = migrate_config(original)
    assert migrated["schema_version"] == 3
    assert len(migrated["lists"]) == 2
    assert [item["targets"][0]["collection"]["remote_id"] for item in migrated["lists"]] == ["7473", "7473"]
    assert original == snapshot


def test_binding_conflict_identifies_two_targets_within_one_list():
    listing = MediaList("Same list", targets=[
        Target("Emby", collection=CollectionBinding(remote_id="shared")),
        Target("Emby", playlists=[Playlist("Playlist", remote_id="shared")]),
    ])
    with pytest.raises(ValueError) as error:
        validate_bindings([listing])
    message = str(error.value)
    assert "Emby" in message and "shared" in message and "Same list" in message
    assert "第1个发布目标" in message and "第2个发布目标" in message


@pytest.mark.parametrize("invalid", [{"tmdb_id": 3, "media_type": "tv"}, {"tmdb_id": 3, "media_type": "Episode"}, {"tmdb_id": "bad", "media_type": "movie"}])
def test_follow_invalid_late_record_never_partially_appends_members_or_seen_keys(invalid):
    from types import SimpleNamespace
    from app.plugins.medialibraryplaylistmanager import MediaLibraryPlaylistManager

    listing = MediaList("Follow", id="l", members=[MediaMember(1)], sources=[Source(10, mode="follow", seen_keys=["movie:1"])])
    plugin = MediaLibraryPlaylistManager()
    plugin.init_plugin({"schema_version": 3, "enabled": False, "lists": [listing.to_dict()]})
    plugin._host = SimpleNamespace(collection=lambda _: [{"tmdb_id": 2, "media_type": "movie"}, invalid])
    try:
        current = plugin._find("l")
        result = plugin._refresh_sources(current)
        assert result[0]["success"] is False
        assert current.member_keys == ["movie:1"]
        assert current.sources[0].seen_keys == ["movie:1"]
        # The recorded error changes the editor snapshot even though membership
        # and the source's seen identities remain untouched.
        assert current.sources[0].error and current.revision == 1
    finally:
        plugin.stop_service()
