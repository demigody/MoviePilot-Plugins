from app.plugins.medialibraryplaylistmanager.import_export import DEFAULT_TRAKT_CLIENT_ID, export_document, parse_standard_file, trakt_link_parts
from app.plugins.medialibraryplaylistmanager.models import MediaList, MediaMember, Playlist, Target
from .test_api import plugin


def test_trakt_urls_accept_legacy_and_app_hosts():
    assert trakt_link_parts("https://app.trakt.tv/users/alice/lists/favorites?sort=rank,asc") == (
        "user", "alice", "favorites"
    )
    assert trakt_link_parts("https://trakt.tv/lists/12345") == ("public", "12345")
    assert trakt_link_parts("https://app.trakt.tv/lists/official/james-bond-collection") == (
        "user", "any", "james-bond-collection"
    )
    assert trakt_link_parts("http://www.trakt.tv/lists/official/james-bond-collection/items?ref=share") == (
        "user", "any", "james-bond-collection"
    )
    assert trakt_link_parts("app.trakt.tv/users/alice/lists/favorites/items") == (
        "user", "alice", "favorites"
    )
    assert DEFAULT_TRAKT_CLIENT_ID


def test_pending_target_mapping_is_portable_and_preserves_playlist_order(plugin):
    listing = MediaList(
        "分享片单",
        members=[MediaMember(1, "movie", "甲"), MediaMember(2, "tv", "乙")],
        targets=[Target.from_dict({
            "server_id": "",
            "kind": "plex",
            "pending_mapping": True,
            "collection": {"enabled": True, "name": "我的合集", "remote_id": None},
            "playlists": [{
                "id": "playlist",
                "name": "自定义顺序",
                "enabled": True,
                "remote_id": None,
                "order": ["tv:2", "movie:1"],
            }],
        })],
    )

    exported = export_document([listing.to_dict()], exported_at="2026-10-19")
    exported_target = exported["lists"][0]["targets"][0]
    assert exported_target["server_id"] == ""
    assert exported_target["library_ids"] == []
    assert not {"source_server_id", "source_server_name", "source_library_ids", "pending_mapping"} & exported_target.keys()
    imported = plugin.api_import_commit({"lists": exported["lists"]})

    assert imported["success"]
    target = imported["imported"][0]["targets"][0]
    assert target["server_id"] == ""
    assert target["library_ids"] == []
    assert target["pending_mapping"] is True
    assert target["collection"]["name"] == "我的合集"
    assert target["playlists"][0]["order"] == ["tv:2", "movie:1"]


def test_standard_file_keeps_selected_season_order_keys():
    listing = MediaList(
        "季顺序",
        members=[MediaMember(2, "tv", "剧", seasons=[1, 2])],
        targets=[Target.from_dict({
            "server_id": "",
            "kind": "emby",
            "pending_mapping": True,
            "playlists": [{"id": "p", "name": "季顺序", "enabled": True,
                           "order": ["tv:2:season:2", "tv:2:season:1"]}],
        })],
    )
    document = export_document([listing.to_dict()], exported_at="2026-10-19")
    parsed = parse_standard_file(document)
    assert parsed[0]["targets"][0]["playlists"][0]["order"] == ["tv:2:season:2", "tv:2:season:1"]


def test_import_requires_unique_editable_names_and_pending_target_mapping(plugin):
    current = plugin.api_save_lists({"action": "save", "list": {"name": "已有片单"}})["list"]
    duplicate = plugin.api_import_commit({"lists": [{"name": " 已有片单 ", "members": [], "targets": []}]})
    assert not duplicate["success"]
    assert "名称" in duplicate["error"] and "已存在" in duplicate["error"]

    imported = plugin.api_import_commit({"lists": [{
        "name": "外部片单",
        "members": [],
        "targets": [{"server_id": "", "kind": "emby", "pending_mapping": True}],
    }]})
    assert imported["success"]
    new_list = imported["imported"][0]
    blocked = plugin.api_save_lists({
        "action": "set_enabled", "list_id": new_list["id"], "revision": 0, "enabled": True
    })
    assert not blocked["success"]
    assert "选择媒体服务器" in blocked["error"]

    target = new_list["targets"][0]
    target.update(server_id="Emby", kind="emby", pending_mapping=False)
    saved = plugin.api_save_lists({"action": "save", "list": {**new_list, "targets": [target]}, "revision": 0})
    assert saved["success"]
    assert saved["list"]["targets"][0]["server_id"] == "Emby"
    assert current["name"] == "已有片单"
