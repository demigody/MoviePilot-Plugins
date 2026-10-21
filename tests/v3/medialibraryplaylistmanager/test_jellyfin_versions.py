"""Movie versions may carry different TMDB identities and hide from normal paging."""
import copy

import pytest

from app.plugins.medialibraryplaylistmanager.emby import EmbyAPIError
from app.plugins.medialibraryplaylistmanager.jellyfin import JellyfinClient


IDS = ['1' * 32, '2' * 32, '3' * 32, '4' * 32]


class GroupedMovies:
    def __init__(self):
        self.rows = {key: {'Id': key, 'Type': 'Movie', 'Name': f'Movie {i}',
                          'ProviderIds': {'Tmdb': str(i + 1)}} for i, key in enumerate(IDS)}
        self.rows[IDS[0]]['MediaSources'] = [
            {'Id': IDS[0], 'Type': 'Default'}, {'Id': IDS[1], 'Type': 'Grouping'},
            {'Id': IDS[2], 'Type': 'Default'}]
        self.rows[IDS[1]]['MediaSources'] = [{'Id': IDS[0], 'Type': 'Grouping'}]
        self.parents = {key: 'selected' for key in IDS}
        self.calls = []
        self.fail = None

    def request(self, method, path, params=None, **kwargs):
        params = dict(params or {})
        self.calls.append((method, path, params))
        assert method == 'GET' and params['UserId'] == 'fixed-user'
        if self.fail == path:
            return 404, {}
        if path == 'Items':
            assert params['ParentId'] == 'selected'
            rows = [self.rows[IDS[0]], self.rows[IDS[3]]]
            start, limit = params['StartIndex'], params['Limit']
            return {'Items': copy.deepcopy(rows[start:start + limit]), 'TotalRecordCount': len(rows)}
        bits = path.split('/')
        if len(bits) == 3 and bits[2] == 'Ancestors':
            return [{'Id': self.parents[bits[1]], 'Type': 'CollectionFolder'}]
        assert len(bits) == 2 and bits[0] == 'Items'
        return copy.deepcopy(self.rows[bits[1]])

    def client(self):
        return JellyfinClient('http://local', 'private', 'fixed-user', self, page_size=1)


def test_inventory_expands_grouped_and_local_versions_with_their_own_typed_ids():
    server = GroupedMovies()
    items = server.client().list_media(['selected'])
    assert {item.key for item in items} == {f'movie:{i}' for i in range(1, 5)}
    assert len({item.id for item in items}) == 4
    assert len([path for _, path, _ in server.calls if path == 'Items']) == 2
    assert all('MediaSources' in p['Fields'] for _, path, p in server.calls if path == 'Items')


def test_versions_outside_selected_libraries_are_not_counted():
    server = GroupedMovies()
    server.parents[IDS[1]] = 'other-library'
    assert {item.key for item in server.client().list_media(['selected'])} == {'movie:1', 'movie:3', 'movie:4'}


def test_same_identity_versions_remain_one_canonical_movie_match():
    server = GroupedMovies()
    server.rows[IDS[1]]['ProviderIds']['Tmdb'] = '1'
    items = server.client().list_media(['selected'])
    assert len(items) == 4
    assert {item.key for item in items} == {'movie:1', 'movie:3', 'movie:4'}


def test_non_movie_local_video_versions_do_not_become_movies():
    server = GroupedMovies()
    server.rows[IDS[2]]['Type'] = 'Video'
    assert {item.key for item in server.client().list_media(['selected'])} == {'movie:1', 'movie:2', 'movie:4'}


@pytest.mark.parametrize('failure', ['missing', 'identity', 'ancestors'])
def test_uncertain_version_reads_fail_instead_of_reporting_missing_movies(failure):
    server = GroupedMovies()
    if failure == 'missing':
        server.fail = f'Items/{IDS[1]}'
    elif failure == 'identity':
        server.rows[IDS[1]]['Id'] = IDS[3]
    else:
        server.fail = f'Items/{IDS[1]}/Ancestors'
    with pytest.raises(EmbyAPIError):
        server.client().list_media(['selected'])


def test_repeated_paged_movie_id_cannot_silently_hide_another_movie():
    def transport(method, path, params=None, **kwargs):
        return {'Items': [{'Id': IDS[0], 'Type': 'Movie', 'ProviderIds': {'Tmdb': '1'}}],
                'TotalRecordCount': 2}
    client = JellyfinClient('http://local', 'private', 'fixed-user', transport, page_size=1)
    with pytest.raises(EmbyAPIError):
        client.list_media(['selected'])


def test_library_pages_share_one_scan_deadline(monkeypatch):
    clock, timeouts = [0.0], []
    monkeypatch.setattr('app.plugins.medialibraryplaylistmanager.jellyfin.time.monotonic', lambda: clock[0])
    def transport(method, path, params=None, timeout=None, **kwargs):
        timeouts.append(timeout)
        if params['ParentId'] == 'first':
            clock[0] = 8
        return {'Items': [], 'TotalRecordCount': 0}
    client = JellyfinClient('http://local', 'private', 'fixed-user', transport, timeout=30, scan_timeout=10)
    assert client.list_media(['first', 'second']) == []
    assert timeouts == [10, 2]
