"""Jellyfin HTTP contracts and publisher barriers through a stateful server.

The double deliberately accepts the Jellyfin routes/payloads only, including
the unpaged virtual-folder array and user-token authorization for playlist
mutations. It shares state-transition helpers with the existing REST fixture.
"""
import copy
import json
from urllib.parse import parse_qs, urlsplit

import pytest

from app.plugins.medialibraryplaylistmanager.emby import EmbyAPIError, EmbyItem
from app.plugins.medialibraryplaylistmanager.jellyfin import JellyfinClient
from app.plugins.medialibraryplaylistmanager.models import MediaMember
from app.plugins.medialibraryplaylistmanager.sync import Publisher
from .test_sync import RestServer, listing
from .test_season_playlist import add_episode
from .test_season_scope import add_seasons, scoped_member


class JellyfinServer(RestServer):
    def __init__(self):
        super().__init__()
        self.wire = []
        self.authenticated_user = 'u'
        self.libraries = [{'ItemId': f'library{n}', 'Name': f'Library {n}', 'CollectionType': 'movies'} for n in range(7)]

    def client(self):
        return JellyfinClient('http://server/jellyfin', 'private', 'u', self, page_size=2)

    def request(self, method, path, params=None, json=None, headers=None, timeout=None):
        params = dict(params or {})
        self.wire.append((method, path, copy.deepcopy(params), copy.deepcopy(json), dict(headers or {})))
        assert not path.startswith('emby/')
        assert headers['Authorization'] == 'MediaBrowser Token="private"'
        assert 'X-Emby-Token' not in headers
        assert headers['Accept'] == 'application/json'
        assert headers['Content-Type'] == 'application/json'
        if path == 'Users/Me':
            assert method == 'GET' and not params and json is None
            return {'Id': self.authenticated_user, 'Name': 'Owner'} if self.authenticated_user else (400, {})
        if path == 'Library/VirtualFolders':
            assert method == 'GET' and not params
            return copy.deepcopy(self.libraries)
        route = path.split('/')
        if route[0] == 'Items':
            assert method == 'GET' and params.pop('UserId') == 'u'
            if len(route) == 1:
                assert 'GroupItemsIntoCollections' not in params
                if params.get('IncludeItemTypes') == 'Season':
                    assert params['Recursive'] == 'false' and params['IsMissing'] == 'false'
                    # Jellyfin's Shows/Seasons is not paged. Items is.
                    if self.fail == 'seasons':
                        return 503, {}
                    rows = [self.media[key] for key in self.seasons.get(params['ParentId'], [])]
                    start, limit = params['StartIndex'], params['Limit']
                    return {'Items': copy.deepcopy(rows[start:start + limit]), 'TotalRecordCount': len(rows)}
                if params.get('IncludeItemTypes') in {'Movie,Series', 'Movie', 'Series', 'Movie,Series,Season'}:
                    assert params['CollapseBoxSetItems'] == 'false'
                path = 'Users/u/Items'
            else:
                path = 'Users/u/Items/' + route[1]
        elif route[0] == 'Shows':
            assert route[2] == 'Episodes'
            assert params['IsMissing'] == 'false' and 'IsPlaceHolder' not in params
        elif route[0] == 'Playlists':
            if len(route) == 1:
                assert method == 'POST' and not params
                assert set(json) == {'Name', 'Ids', 'UserId', 'MediaType', 'IsPublic'}
                assert json['UserId'] == 'u' and json['MediaType'] == 'Video'
                assert json['IsPublic'] is False and isinstance(json['Ids'], list)
                params = {'Name': json['Name'], 'Ids': ','.join(json['Ids'])}
            elif 'Move' in route:
                assert method == 'POST' and not params and json is None
            elif method == 'DELETE':
                assert set(params) == {'EntryIds'} and json is None
            else:
                assert params['UserId'] == 'u'
        return super().request(method, path, params)


def jellyfin_listing(**kwargs):
    value = listing(**kwargs)
    value.targets[0].kind = 'jellyfin'
    return value


def publish(server, value, **kwargs):
    return Publisher(server.client()).publish(value, value.targets[0], **kwargs)


def test_virtual_folders_uses_complete_unpaged_array_and_retains_proxy_base():
    server = JellyfinServer()
    client = server.client()
    assert client.base_url == 'http://server/jellyfin/'
    assert [row['id'] for row in client.list_libraries()] == [f'library{n}' for n in range(7)]
    assert [(method, path, params) for method, path, params, _, _ in server.wire] == [('GET', 'Library/VirtualFolders', {})]


def test_regular_user_library_fallback_keeps_physical_parent_ids_and_complete_unpaged_roots():
    calls = []
    folders = [{'Id': f'folder{n}', 'Type':'CollectionFolder', 'Name':f'库{n}',
        'CollectionType': 'tvshows' if n % 2 else 'movies'} for n in range(5)]
    def transport(method, path, params=None, **kwargs):
        calls.append((method,path,dict(params or {})))
        if path == 'Library/VirtualFolders': return 403, {}
        assert method == 'GET' and path == 'Items'
        assert params == {'UserId':'ordinary','Recursive':'false'}
        return {'Items':folders, 'TotalRecordCount':len(folders)}
    client = JellyfinClient('http://local','private','ordinary',transport,page_size=2)
    assert [item['id'] for item in client.list_libraries()] == [item['Id'] for item in folders]
    assert len(calls) == 2


@pytest.mark.parametrize('rows,count', [([],1),([None],1),([],True),([],None)])
def test_user_root_library_response_must_be_complete(rows,count):
    def transport(method,path,**kwargs):
        return (403,{}) if path=='Library/VirtualFolders' else {'Items':rows,'TotalRecordCount':count}
    with pytest.raises(EmbyAPIError):
        JellyfinClient('http://local','private','u',transport).list_libraries()


@pytest.mark.parametrize('status', [401,404,503])
def test_library_failures_other_than_forbidden_do_not_change_endpoint_or_scope(status):
    calls=[]
    def transport(method,path,**kwargs):
        calls.append(path)
        return status,{}
    client=JellyfinClient('http://local','private','u',transport)
    with pytest.raises(EmbyAPIError) as error:
        client.list_libraries()
    assert error.value.status==status and calls==['Library/VirtualFolders']


def test_builtin_http_transport_preserves_proxy_path_and_sends_modern_auth_and_json(monkeypatch):
    from app.plugins.medialibraryplaylistmanager import emby
    requests = []

    class Response:
        def __init__(self, body):
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def read(self):
            return json.dumps(self.body).encode()

    def urlopen(request, timeout):
        requests.append(request)
        assert request.get_header('Authorization') == 'MediaBrowser Token="private"'
        assert not request.get_header('X-emby-token')
        path = urlsplit(request.full_url).path
        if path.endswith('/Users/Me'):
            return Response({'Id': 'u'})
        if path.endswith('/Playlists'):
            assert request.get_method() == 'POST'
            assert not urlsplit(request.full_url).query
            assert json.loads(request.data) == {'Name': '我的片单', 'Ids': ['m1'], 'UserId': 'u', 'MediaType': 'Video', 'IsPublic': False}
            return Response({'Id': 'playlist'})
        assert path == '/proxy/jellyfin/Items'
        assert parse_qs(urlsplit(request.full_url).query)['UserId'] == ['u']
        return Response({'Items': [], 'TotalRecordCount': 0})

    monkeypatch.setattr(emby, 'urlopen', urlopen)
    client = JellyfinClient('http://server/proxy/jellyfin', 'private', 'u')
    assert client.list_media() == []
    assert client.create_playlist('我的片单', ['m1', 'm1']) == 'playlist'
    assert all('/emby/' not in request.full_url and 'private' not in request.full_url for request in requests)


@pytest.mark.parametrize('invalid', [{}, {'Items': []}, [None], [{'Name': 'missing ID'}], [{'ItemId': 'a'}, {'ItemId': 'a'}]])
def test_invalid_virtual_folder_responses_fail_closed(invalid):
    server = JellyfinServer()
    server.libraries = invalid
    with pytest.raises(EmbyAPIError):
        server.client().list_libraries()


def test_folder_types_filter_only_video_libraries_without_losing_mixed_folders():
    server = JellyfinServer()
    server.libraries = [{'ItemId': str(n), 'CollectionType': kind} for n, kind in enumerate(['movies', 'tvshows', None, 'mixed', 'music', 'boxsets'])]
    assert [row['id'] for row in server.client().list_libraries()] == ['0', '1', '2', '3']


def test_user_queries_keep_movie_and_tv_identity_and_disable_collection_collapsing():
    server = JellyfinServer()
    server.inventory = ['m1', 's1']
    client = server.client()
    assert [item.key for item in client.list_media()] == ['movie:1', 'tv:1']
    assert server.wire[0][1:3] == ('Items', {'UserId': 'u', 'IncludeItemTypes': 'Movie,Series',
        'Recursive': 'true', 'CollapseBoxSetItems': 'false', 'Fields': 'ProviderIds,MediaSources,ParentId', 'StartIndex': 0,
        'Limit': 2, 'EnableTotalRecordCount': 'true'})


def test_movie_publish_uses_native_create_payload_and_stable_original_entry_ids():
    server = JellyfinServer()
    value = jellyfin_listing(bound=False)
    saved_members = copy.deepcopy([member.to_dict() for member in value.members])
    result = publish(server, value)
    assert result['success'], result
    playlist = value.targets[0].playlists[0].remote_id
    collection = value.targets[0].collection.remote_id
    assert [item for _, item in server.playlists[playlist]] == ['m2', 'm1']
    assert set(server.collections[collection]) == {'m1', 'm2'}
    assert [member.to_dict() for member in value.members] == saved_members
    creation = next(call for call in server.wire if call[0] == 'POST' and call[1] == 'Playlists')
    assert creation[2] == {} and creation[3]['Ids'] == ['m2', 'm1']
    before = len(server.writes)
    assert publish(server, value)['success']
    assert len(server.writes) == before
    assert value.targets[0].playlists[0].remote_id == playlist


def test_bound_reordering_moves_entry_ids_and_preserves_playlist_before_removal():
    server = JellyfinServer()
    value = jellyfin_listing()
    result = publish(server, value)
    assert result['success'], result
    assert [item for _, item in server.playlists['p']] == ['m2', 'm1']
    moves = [call for call in server.wire if '/Move/' in call[1]]
    assert moves and moves[0][2] == {}
    # The moved entry comes from the playlist read, not its media object's ID.
    assert '/Items/m2/Move/' not in moves[0][1]
    deletes = [call for call in server.wire if call[0] == 'DELETE']
    assert any(call[2] == {'EntryIds': 'e3'} for call in deletes)
    assert all('/Items' in call[1] for call in deletes)
    assert not any(call[1] == 'Playlists/p' and call[0] == 'DELETE' for call in server.wire)
    before = len(server.writes)
    assert publish(server, value)['success'] and len(server.writes) == before


def test_selected_tv_seasons_use_collection_season_ids_and_order_only_selected_episodes():
    server = JellyfinServer()
    server.inventory = ['m1', 's1']
    add_seasons(server)
    for name, season, number in [('a1', 1, 1), ('b2', 2, 2), ('b1', 2, 1), ('c1', 3, 1)]:
        add_episode(server, name, season, number)
    server.collections['c'] = ['s1', 'season1', 'm1']
    server.playlists['p'] = [('a-old', 'a1'), ('b-old', 'b2')]
    value = jellyfin_listing(members=[MediaMember(1), scoped_member([2, 3])])
    value.targets[0].playlists[0].order = ['tv:1', 'movie:1']
    result = publish(server, value)
    assert result['success'], result
    assert set(server.collections['c']) == {'season2', 'season3', 'm1'}
    assert [item for _, item in server.playlists['p']] == ['b1', 'b2', 'c1', 'm1']
    assert server.playlists['p'][1][0] == 'b-old'
    assert [member.key for member in value.members] == ['movie:1', 'tv:1']
    season_reads = [params for method, path, params, _, _ in server.wire if path == 'Items' and params.get('IncludeItemTypes') == 'Season']
    assert [row['StartIndex'] for row in season_reads] == [0, 2]
    assert all('s1' not in params.get('Ids', '').split(',') for method, path, params, _, _ in server.wire if method == 'POST' and path.startswith('Collections/'))


def test_large_tv_playlist_batches_and_persists_binding_before_failed_append():
    server = JellyfinServer()
    server.inventory = ['s1']
    for number in range(1, 236):
        add_episode(server, f'ep{number}', 1, number)
    value = jellyfin_listing(bound=False, members=[MediaMember(1, 'tv')], collection=False)
    snapshots = []
    server.fail = 'add_playlist'
    result = publish(server, value, on_binding=lambda: snapshots.append(copy.deepcopy(value.to_dict())))
    assert result['status'] == 'partial', result
    binding = value.targets[0].playlists[0].remote_id
    assert binding and len(server.playlists[binding]) == 100
    assert snapshots[0]['targets'][0]['playlists'][0]['remote_id'] == binding
    assert not any(call[0] == 'DELETE' for call in server.wire)
    server.fail = None
    assert publish(server, value)['success']
    assert value.targets[0].playlists[0].remote_id == binding
    assert [item for _, item in server.playlists[binding]] == [f'ep{number}' for number in range(1, 236)]
    batches = [len(body['Ids']) if path == 'Playlists' else len(params['Ids'].split(','))
        for method, path, params, body, _ in server.wire if method == 'POST' and (path == 'Playlists' or (path.startswith('Playlists/') and path.endswith('/Items')))]
    assert max(batches) <= 100
    assert len([call for call in server.wire if call[0] == 'POST' and call[1] == 'Playlists']) == 1


@pytest.mark.parametrize('failure', ['inventory', 'get_playlist', 'get_collection', 'get_output'])
def test_failed_initial_reads_do_not_write_or_remove(failure):
    server = JellyfinServer()
    server.fail = failure
    before = copy.deepcopy((server.collections, server.playlists))
    assert not publish(server, jellyfin_listing())['success']
    assert not server.writes and (server.collections, server.playlists) == before


@pytest.mark.parametrize('failure', ['add_playlist', 'add_collection', 'move'])
def test_failed_add_or_move_never_reaches_delete_barrier(failure):
    server = JellyfinServer()
    server.fail = failure
    assert not publish(server, jellyfin_listing())['success']
    assert not any(call[0] == 'DELETE' for call in server.wire)
    assert ('e3', 'm3') in server.playlists['p'] and 'm3' in server.collections['c']


@pytest.mark.parametrize('authenticated', [None, 'other-owner'])
@pytest.mark.parametrize('operation', ['create', 'add', 'remove', 'move'])
def test_playlist_mutations_reject_application_key_or_mismatched_user_before_any_write(authenticated, operation):
    server = JellyfinServer()
    server.authenticated_user = authenticated
    client = server.client()
    action = {'create': lambda: client.create_playlist('new', ['m1']),
        'add': lambda: client.add_playlist_items('p', ['m2']),
        'remove': lambda: client.remove_playlist_entries('p', ['e1']),
        'move': lambda: client.move_playlist_entry('p', 'e1', 0)}[operation]
    with pytest.raises(EmbyAPIError) as error:
        action()
    assert error.value.code == 'permission_denied'
    assert all(call[0] == 'GET' for call in server.wire)


def test_application_key_still_supports_collections_without_claiming_user_identity():
    server = JellyfinServer()
    server.authenticated_user = None
    value = jellyfin_listing(playlist=False)
    assert publish(server, value)['success']
    assert set(server.collections['c']) == {'m1', 'm2'}
    assert all(call[1] != 'Users/Me' for call in server.wire)


def test_user_auth_probe_distinguishes_application_key_from_transport_failure():
    assert JellyfinServer().client().resolve_authenticated_user_id() == 'u'
    client = JellyfinClient('http://server', 'private', 'u', lambda *_args, **_kwargs: (503, {}))
    with pytest.raises(EmbyAPIError) as error:
        client.resolve_authenticated_user_id()
    assert error.value.status == 503


def test_invalid_season_or_episode_parent_fails_before_all_output_writes():
    server = JellyfinServer()
    server.inventory = ['s1']
    add_seasons(server)
    server.media['season2']['SeriesId'] = 'different-series'
    add_episode(server, 'episode', 2, 1)
    result = publish(server, jellyfin_listing(members=[scoped_member([2])]))
    assert not result['success'] and not server.writes


@pytest.mark.parametrize('alias_read', ['collision', 'none', 'denied', 'unavailable'])
def test_acknowledged_season_add_that_is_not_visible_reports_the_season_and_never_removes(alias_read):
    server = JellyfinServer()
    server.inventory = ['m1', 's1']
    add_seasons(server)
    add_episode(server, 'episode', 3, 1)
    value = jellyfin_listing(members=[MediaMember(1), scoped_member([3])])
    original = server.request
    diagnostics = []

    def request(method, path, **kwargs):
        if method == 'GET' and path == 'Items' and 'UserId' not in kwargs.get('params', {}):
            params = kwargs['params']
            diagnostics.append(copy.deepcopy(params))
            assert params['ParentId'] == 'c' and params['Recursive'] == 'true'
            if alias_read == 'denied': return 403, {}
            if alias_read == 'unavailable': return 503, {}
            rows = [{'Id': 'old-folder', 'Type': 'Folder', 'Path': '/series/show/Season 3'}] if alias_read == 'collision' else []
            return {'Items': rows, 'TotalRecordCount': len(rows)}
        response = original(method, path, **kwargs)
        if method == 'GET' and path == 'Items/season3':
            response = {**response, 'Path': '/series/show/Season 3'}
        if method == 'POST' and path == 'Collections/c/Items':
            # 10.10.7 can resolve a Season path to an old Folder record. The
            # fixed user's collection read then omits that invisible folder.
            server.collections['c'] = [identity for identity in server.collections['c'] if not identity.startswith('season')]
        return response

    server.request = request
    result = publish(server, value)
    assert not result['success'] and result['error_code'] == 'membership_unverified'
    assert '合集' in result['error'] and 'S3' in result['error'] and '跳过移除' in result['error']
    assert ('同路径的普通文件夹' in result['error']) == (alias_read == 'collision')
    assert diagnostics and server.authenticated_user == 'u'
    assert not any(method == 'DELETE' for method, *_ in server.wire)
    assert 'm3' in server.collections['c'] and ('e3', 'm3') in server.playlists['p']
