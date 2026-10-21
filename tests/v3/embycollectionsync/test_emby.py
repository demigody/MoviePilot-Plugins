import pytest

from app.plugins.embycollectionsync.adapters import UnsupportedAdapter
from app.plugins.embycollectionsync.emby import EmbyAPIError, EmbyClient, EmbyItem


def movie(number, *, kind="Movie", entry=None):
    return {"Id": str(number), "Name": f"Title {number}", "Type": kind, "ProviderIds": {"tMdB": str(number)}, **({"PlaylistItemId": entry} if entry else {})}


def test_complete_paginated_movie_series_inventory_with_server_limit():
    calls = []
    values = [movie(1), movie(2, kind="Series"), movie(3)]

    def transport(method, path, *, params, headers, timeout, json=None):
        calls.append((method, path, params, headers, timeout))
        start = params["StartIndex"]
        return 200, {"Items": values[start:start + 1], "TotalRecordCount": len(values)}

    client = EmbyClient("http://server/emby", "private", "u", transport, page_size=2, timeout=3)
    result = client.list_media()
    assert [item.key for item in result] == ["movie:1", "tv:2", "movie:3"]
    assert [call[2]["StartIndex"] for call in calls] == [0, 1, 2]
    assert all(call[2]["IncludeItemTypes"] == "Movie,Series" for call in calls)
    assert all("Episode" not in str(call[2]) for call in calls)
    assert all(0 < call[4] <= 3 for call in calls)
    assert all(call[3]["X-Emby-Token"] == "private" for call in calls)
    assert all("api_key" not in call[2] and "private" not in call[1] for call in calls)
    assert calls[0][1] == "Users/u/Items"


def test_series_filter_and_collection_never_query_episode_or_descend_series():
    calls = []

    def transport(method, path, params=None, json=None):
        calls.append(params)
        if path.endswith("/Items/c"):
            return {"Id": "c", "Type": "BoxSet"}
        return {"Items": [movie(1, kind="Series")], "TotalRecordCount": 1}

    client = EmbyClient("http://server", "k", "u", transport)
    assert client.list_media(media_type="tv")[0].media_type == "tv"
    assert client.get_collection_items("c")[0].key == "tv:1"
    assert calls[0]["IncludeItemTypes"] == "Series"
    assert calls[2]["IncludeItemTypes"] == "Movie,Series,Season" and calls[2]["Recursive"] == "false"
    before = len(calls)
    with pytest.raises(ValueError):
        client.list_media(media_type="Episode")
    assert len(calls) == before


@pytest.mark.parametrize("payload", [{"Items": []}, {"Items": [], "TotalRecordCount": 1}, {"Items": [movie(1)], "TotalRecordCount": 0}, {"Items": [movie(1), movie(1)], "TotalRecordCount": 2}, {"Items": [movie(1, kind="Episode")], "TotalRecordCount": 1}, {"Items": [None], "TotalRecordCount": 1}, []])
def test_incomplete_or_unsupported_inventory_raises(payload):
    client = EmbyClient("http://server", "k", "u", lambda *a, **kw: payload)
    with pytest.raises(EmbyAPIError):
        client.list_media()


def test_changed_total_record_count_and_repeated_pages_are_rejected():
    for payloads in [
        [{"Items": [movie(1)], "TotalRecordCount": 2}, {"Items": [movie(2)], "TotalRecordCount": 3}],
        [{"Items": [movie(1)], "TotalRecordCount": 2}, {"Items": [movie(1)], "TotalRecordCount": 2}],
    ]:
        client = EmbyClient("http://server", "k", "u", lambda *a, **kw: payloads.pop(0), page_size=1)
        with pytest.raises(EmbyAPIError):
            client.list_media()


def test_playlist_full_pagination_uses_entry_ids_and_accepts_repeated_media():
    items = [movie(1, entry="e1"), movie(1, entry="e2")]
    def transport(method, path, **kwargs):
        if path.endswith("/Items/p"):
            return {"Id": "p", "Type": "Playlist"}
        start = kwargs["params"]["StartIndex"]
        return {"Items": items[start:start + 1], "TotalRecordCount": 2}
    client = EmbyClient("http://server", "k", "u", transport, page_size=1)
    assert [item.playlist_item_id for item in client.get_playlist_items("p")] == ["e1", "e2"]
    invalid = EmbyClient("http://server", "k", "u", lambda method, path, **kwargs: {"Id": "p", "Type": "Playlist"} if path.endswith("/Items/p") else {"Items": [movie(1)], "TotalRecordCount": 1})
    with pytest.raises(EmbyAPIError):
        invalid.get_playlist_items("p")


@pytest.mark.parametrize("output", ["collection", "playlist"])
def test_missing_output_is_identified_before_root_fallback_or_playlist_failure(output):
    calls = []

    def transport(method, path, **kwargs):
        calls.append((method, path))
        if path == "emby/Users/u/Items/gone":
            return 404, {}
        if path.startswith("emby/Playlists/"):
            return 500, {}
        return {"Items": [{"Id": "3", "Type": "CollectionFolder"}], "TotalRecordCount": 1}

    client = EmbyClient("http://server", "secret", "u", transport)
    with pytest.raises(EmbyAPIError) as error:
        getattr(client, f"get_{output}_items")("gone")
    assert error.value.code == "missing_output" and error.value.status == 404
    assert "已不存在" in str(error.value) and "重新建立" in str(error.value)
    assert "gone" in str(error.value) and "secret" not in str(error.value)
    assert calls == [("GET", "emby/Users/u/Items/gone")]


@pytest.mark.parametrize("output", ["collection", "playlist"])
def test_bound_output_must_be_the_expected_container_type(output):
    def transport(method, path, **kwargs):
        if path == "emby/Users/u/Items/wrong":
            return {"Id": "wrong", "Type": "Movie"}
        return {"Items": [], "TotalRecordCount": 0}

    client = EmbyClient("http://server", "secret", "u", transport)
    with pytest.raises(EmbyAPIError) as error:
        getattr(client, f"get_{output}_items")("wrong")
    assert error.value.code == "unsupported"
    assert "Movie" in str(error.value)


def test_unavailable_output_lookup_is_not_classified_as_missing():
    client = EmbyClient("http://server", "secret", "u", lambda *args, **kwargs: (503, {}))
    with pytest.raises(EmbyAPIError) as error:
        client.get_collection_items("c")
    assert error.value.status == 503 and error.value.code == "server_error"


def test_transport_type_error_is_not_retried_after_post():
    calls = []

    def transport(method, path, **kwargs):
        calls.append(method)
        raise TypeError("a bug after the write")

    client = EmbyClient("http://server", "k", "u", transport)
    with pytest.raises(EmbyAPIError):
        client.create_collection("new", ["1"])
    assert calls == ["POST"]


def test_request_errors_are_distinct_and_do_not_disclose_credentials():
    def transport(*a, **kw):
        raise TimeoutError("http://server/?api_key=private")

    client = EmbyClient("http://server", "private", "u", transport)
    with pytest.raises(EmbyAPIError) as error:
        client.list_media()
    assert error.value.code == "timeout" and "private" not in str(error.value)
    for payload in [(401, {}), b"not json"]:
        client.transport = lambda *a, **kw: payload
        with pytest.raises(EmbyAPIError):
            client.list_media()


def test_move_and_remove_use_playlist_entry_identity_in_official_routes():
    calls = []
    client = EmbyClient("http://server", "k", "u", lambda *a, **kw: calls.append((a, kw)) or (204, b""))
    client.move_playlist_entry("playlist", "entry", 2)
    client.remove_playlist_entries("playlist", ["entry"])
    assert calls[0][0] == ("POST", "emby/Playlists/playlist/Items/entry/Move/2")
    assert calls[1][1]["params"]["EntryIds"] == "entry"
    assert "Ids" not in calls[1][1]["params"]


def test_capability_contract_is_honest_about_series_playlist_and_jellyfin():
    assert EmbyClient.capabilities["media_types"] == ["movie", "tv"]
    assert EmbyClient.capabilities["tv_playlist"]
    assert EmbyClient.capabilities["playlist_grouping"] == "member"
    adapter = UnsupportedAdapter()
    assert not adapter.capabilities["supported"]
    with pytest.raises(EmbyAPIError) as error:
        adapter.list_media()
    assert error.value.code == "unsupported"


def test_provider_identity_requires_supported_type_and_valid_provider_id():
    assert EmbyItem.from_dict(movie(2, kind="Series")).key == "tv:2"
    for item in [{"Id": "1", "Type": "Movie", "ProviderIds": {"Tmdb": "bad"}}, {"Id": "1", "ProviderIds": {"Tmdb": "1"}}]:
        with pytest.raises(EmbyAPIError):
            EmbyItem.from_dict(item)


def test_library_query_uses_official_pagination_and_omits_private_metadata():
    calls = []
    libraries = [{"ItemId": "movie", "Name": "Movies", "CollectionType": "movies", "Locations": ["private"]}, {"Id": "series", "Name": "TV", "CollectionType": "tvshows"}, {"ItemId": "music", "CollectionType": "music"}]

    def transport(method, path, params=None, **kwargs):
        calls.append((path, params))
        start = params["StartIndex"]
        return {"Items": libraries[start:start + 1], "TotalRecordCount": 3}

    client = EmbyClient("http://server", "k", "u", transport, page_size=1)
    results = client.list_libraries()
    assert [item["id"] for item in results] == ["movie", "series"]
    assert all(path == "emby/Library/VirtualFolders/Query" for path, _ in calls)
    assert all("Locations" not in item for item in results)


def test_provider_boundary_preserves_unsupported_error_code_without_secrets():
    from app.plugins.embycollectionsync.requests import ProviderCalls
    calls = ProviderCalls()
    try:
        result = calls.call(lambda: UnsupportedAdapter().list_media())
        assert result["error_code"] == "unsupported" and not result["success"]
    finally:
        calls.stop()


def test_provider_boundary_preserves_missing_output_code_without_secrets():
    from app.plugins.embycollectionsync.requests import ProviderCalls
    def missing():
        raise EmbyAPIError("credential: secret", code="missing_output", status=404)
    calls = ProviderCalls()
    try:
        result = calls.call(missing)
        assert result["error_code"] == "missing_output" and not result["success"]
        assert "secret" not in str(result)
    finally:
        calls.stop()


def test_provider_boundary_redacts_internal_adapter_name_and_preserves_permission_code():
    from app.plugins.embycollectionsync.requests import ProviderCalls
    def denied():
        raise EmbyAPIError('private-user-token', code='permission_denied')
    calls = ProviderCalls()
    try:
        result = calls.call(denied)
        assert result['error_code'] == 'permission_denied'
        assert 'Emby' not in result['error'] and 'private' not in str(result)
    finally:
        calls.stop()


@pytest.mark.parametrize("method", ["list_collections", "list_playlists"])
def test_output_catalog_does_not_accept_movie_or_root_folder_as_container(method):
    client = EmbyClient("http://server", "secret", "u", lambda *args, **kwargs:
        {"Items": [{"Id": "3", "Name": "Movies", "Type": "CollectionFolder"}], "TotalRecordCount": 1})
    with pytest.raises(EmbyAPIError) as error:
        getattr(client, method)()
    assert error.value.code == "unsupported"
