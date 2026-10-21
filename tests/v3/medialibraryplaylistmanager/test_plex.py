"""PMS contracts: numeric rating keys, per-entry playlist IDs, one-section collections.

The server double accepts only the documented Plex routes and parameter forms.
It intentionally caps pages below the requested size and supports duplicate media.
"""
import copy

import pytest

from app.plugins.medialibraryplaylistmanager.emby import EmbyAPIError
from app.plugins.medialibraryplaylistmanager.models import CollectionBinding, MediaList, MediaMember, Playlist, Target
from app.plugins.medialibraryplaylistmanager.plex import PlexClient
from app.plugins.medialibraryplaylistmanager.sync import Publisher


def movie(key, tmdb, section='1'):
    return {'ratingKey': str(key), 'type': 'movie', 'title': f'Movie {key}',
            'librarySectionID': section, 'Guid': [{'id': f'tmdb://{tmdb}'}]}


class PlexServer:
    def __init__(self):
        self.sections = [{'key': '1', 'title': 'Movies', 'type': 'movie'},
                         {'key': '2', 'title': 'Shows', 'type': 'show'},
                         {'key': '3', 'title': 'Other movies', 'type': 'movie'}]
        self.media = {'11': movie(11, 1), '12': movie(12, 2), '13': movie(13, 3),
                      '21': {'ratingKey': '21', 'type': 'show', 'title': 'Series',
                             'librarySectionID': '2', 'Guid': [{'id': 'tmdb://1'}]}}
        for number in (2, 10):
            self.media[str(100 + number)] = {'ratingKey': str(100 + number), 'type': 'season',
                'title': f'Season {number}', 'parentRatingKey': '21', 'index': number, 'librarySectionID': '2'}
            for episode in (1, 2):
                key = str(1000 + number * 10 + episode)
                self.media[key] = {'ratingKey': key, 'type': 'episode', 'title': key,
                    'grandparentRatingKey': '21', 'parentRatingKey': str(100 + number),
                    'parentIndex': number, 'index': episode, 'librarySectionID': '2',
                    'Media': [{'Part': [{'file': f'/show/{key}.mkv'}]}]}
        self.collections, self.playlists, self.wire = {}, {}, []
        self.next_id, self.next_entry = 500, 8000
        self.fail, self.bad_page, self.machine = None, None, 'machine'

    def client(self, **kwargs):
        return PlexClient('http://server/proxy/plex', 'private', transport=self, page_size=7, **kwargs)

    @property
    def writes(self):
        return [row for row in self.wire if row[0] != 'GET']

    def page(self, rows, params, tag='Metadata'):
        start = params.get('X-Plex-Container-Start', 0)
        size = min(params.get('X-Plex-Container-Size', 2), 2)
        if self.bad_page == 'repeat': start = 0
        part = copy.deepcopy(rows[start:start + size])
        result = {'size': len(part), 'offset': start, 'totalSize': len(rows), tag: part}
        if self.bad_page == 'unpaged': return {'MediaContainer': {'size':len(rows),tag:copy.deepcopy(rows)}}
        if self.bad_page == 'missing_size': result.pop('size')
        if self.bad_page == 'false_empty': result[tag] = []; result['size'] = 0
        if self.bad_page == 'size': result['size'] += 1
        return {'MediaContainer': result}

    def ids(self, params):
        uri = params['uri']
        assert uri.startswith('server://machine/com.plexapp.plugins.library/library/metadata/')
        return uri.rsplit('/', 1)[-1].split(',')

    def append(self, output, ids):
        for key in ids:
            assert key in self.media
            if output['type'] == 'playlist':
                self.next_entry += 1
                output['items'].append((key, str(self.next_entry)))
            elif key not in output['items']:
                assert self.media[key]['librarySectionID'] == output['librarySectionID']
                assert self.media[key]['type'] == output['subtype']
                output['items'].append(key)

    def output(self, kind, ids=(), **extra):
        self.next_id += 1
        key = str(self.next_id)
        value = {'ratingKey': key, 'type': kind, 'title': kind, 'smart': False, 'items': [], **extra}
        if kind == 'playlist': value['playlistType'] = 'video'
        else: value.setdefault('librarySectionID', '1'); value.setdefault('subtype', 'movie')
        (self.playlists if kind == 'playlist' else self.collections)[key] = value
        self.append(value, ids)
        return key

    def request(self, method, path, params=None, json=None, headers=None, timeout=None):
        params = dict(params or {})
        self.wire.append((method, path, copy.deepcopy(params), copy.deepcopy(json), dict(headers or {})))
        assert headers['X-Plex-Token'] == 'private' and headers['Accept'] == 'application/json'
        assert headers['X-Plex-Client-Identifier'] and headers['X-Plex-Product']
        assert 'X-Emby-Token' not in headers and 'UserId' not in params and json is None
        assert not path.startswith(('emby/', '/'))
        if self.fail == (method, path): return 503, {}
        if isinstance(self.fail, int): return self.fail, {}
        bits = path.split('/')
        if path == 'identity':
            assert method == 'GET'
            return {'MediaContainer': {'machineIdentifier': self.machine}}
        if path == 'library/sections':
            assert method == 'GET'
            return {'MediaContainer': {'size': len(self.sections), 'Directory': copy.deepcopy(self.sections)}}
        if bits[:2] == ['library', 'sections']:
            assert method == 'GET' and len(bits) == 4
            if bits[-1] == 'collections':
                rows = [row for row in self.collections.values() if row['librarySectionID'] == bits[2]]
            else:
                assert bits[-1] == 'all' and params['includeGuids'] == 1
                kind = {1: 'movie', 2: 'show'}[params['type']]
                rows = [row for row in self.media.values() if row['type'] == kind and row['librarySectionID'] == bits[2]]
            return self.page(rows, params)
        if path == 'playlists' and method == 'GET':
            assert params['playlistType'] == 'video'
            return self.page(list(self.playlists.values()), params)
        if method == 'POST':
            assert path in ('playlists', 'library/collections') and params['smart'] == 0
            kind = 'playlist' if path == 'playlists' else 'collection'
            if kind == 'playlist': assert params['type'] == 'video'; extra = {}
            else:
                assert params['type'] in (1, 2)
                extra = {'subtype': {1: 'movie', 2: 'show'}[params['type']], 'librarySectionID': params['sectionId']}
            key = self.output(kind, self.ids(params), title=params['title'], **extra)
            row = (self.playlists if kind == 'playlist' else self.collections)[key]
            return {'MediaContainer': {'size': 1, 'Metadata': [copy.deepcopy(row)]}}
        if bits[:2] == ['library', 'metadata']:
            key = bits[2]
            row = self.media.get(key) or self.collections.get(key)
            if not row: return 404, {}
            if len(bits) == 3:
                assert method == 'GET'
                return {'MediaContainer': {'size': 1, 'Metadata': [copy.deepcopy(row)]}}
            if bits[-1] in ('children', 'allLeaves'):
                assert method == 'GET' and len(bits) == 4
                if key in self.collections: rows = [self.media[n] for n in row['items']]
                else:
                    assert params['includeGuids'] == 1
                    if bits[-1] == 'children':
                        assert params['excludeAllLeaves'] == 1
                        rows = [m for m in self.media.values() if m.get('parentRatingKey') == key and m['type'] == 'season']
                    else: rows = [m for m in self.media.values() if m.get('grandparentRatingKey') == key]
                return self.page(rows, params)
            # Metadata routes read media details; membership changes use collections.
            return 404, {}
        if bits[:2] == ['library', 'collections']:
            assert bits[3] == 'items'
            if len(bits) == 4:
                assert method == 'PUT'
                self.append(self.collections[bits[2]], self.ids(params))
            else:
                assert method == 'DELETE' and len(bits) == 5
                self.collections[bits[2]]['items'].remove(bits[4])
            return {}
        if bits[0] == 'playlists':
            row = self.playlists.get(bits[1])
            if not row: return 404, {}
            if len(bits) == 2:
                assert method == 'GET'
                return {'MediaContainer': {'size': 1, 'Metadata': [copy.deepcopy(row)]}}
            if method == 'GET':
                assert len(bits) == 3 and bits[2] == 'items' and params['includeGuids'] == 1
                return self.page([{**self.media[key], 'playlistItemID': entry} for key, entry in row['items']], params)
            if len(bits) == 3:
                assert method == 'PUT' and bits[2] == 'items'
                self.append(row, self.ids(params)); return {}
            assert bits[2] == 'items'
            assert (method == 'DELETE' and len(bits) == 4) or (method == 'PUT' and len(bits) == 5 and bits[4] == 'move')
            index = next(i for i, (_, entry) in enumerate(row['items']) if entry == bits[3])
            item = row['items'].pop(index)
            if method == 'DELETE': return {}
            assert method == 'PUT' and bits[-1] == 'move'
            after = params.get('after')
            position = 0 if after is None else next(i + 1 for i, (_, entry) in enumerate(row['items']) if entry == after)
            row['items'].insert(position, item); return {}
        raise AssertionError((method, path, params))


def listing(members=None, collection=True, playlist=True):
    value = MediaList('Plex list', members=members if members is not None else [MediaMember(1), MediaMember(2)],
        targets=[Target('p', 'plex', collection=CollectionBinding(collection),
                        playlists=[Playlist('Watch', order=['movie:2', 'movie:1'])] if playlist else [])])
    # Existing test bindings belong to this connection; production sets this at create.
    value.targets[0].connection_fingerprint = PlexServer().client().connection_fingerprint()
    return value


def publish(server, value, **kwargs):
    return Publisher(server.client()).publish(value, value.targets[0], **kwargs)


def test_inventory_complete_across_sections_and_typed_guid_matching():
    server = PlexServer(); client = server.client()
    assert client.base_url == 'http://server/proxy/plex/'
    assert [row['id'] for row in client.list_libraries()] == ['1', '2', '3']
    assert [row.key for row in client.list_media()] == ['movie:1', 'movie:2', 'movie:3', 'tv:1']
    assert [row.key for row in client.list_media(['2'])] == ['tv:1']
    assert not server.writes


@pytest.mark.parametrize('bad', ['repeat', 'missing_size', 'false_empty', 'size'])
def test_incomplete_inventory_never_becomes_an_empty_or_partial_snapshot(bad):
    server = PlexServer(); server.bad_page = bad
    with pytest.raises(EmbyAPIError): server.client().list_media()
    assert not server.writes


def test_create_and_reconcile_in_place_uses_entry_ids_and_preserves_bindings():
    server = PlexServer(); value = listing(); target = value.targets[0]; bindings = []
    assert publish(server, value, on_binding=lambda: bindings.append((target.collection.remote_id, target.playlists[0].remote_id)))['success']
    cid, pid = target.collection.remote_id, target.playlists[0].remote_id
    assert len(bindings) == 2 and bindings[0] == (cid, None)
    assert server.collections[cid]['items'] == ['11', '12']
    assert [row[0] for row in server.playlists[pid]['items']] == ['12', '11']
    old_entries = dict(server.playlists[pid]['items'])
    value.remove_member('movie:1'); value.add_member(MediaMember(3))
    target.playlists[0].order = ['movie:3', 'movie:2']
    assert publish(server, value)['success']
    assert target.collection.remote_id == cid and target.playlists[0].remote_id == pid
    assert server.collections[cid]['items'] == ['12', '13']
    assert [row[0] for row in server.playlists[pid]['items']] == ['13', '12']
    assert dict(server.playlists[pid]['items'])['12'] == old_entries['12']


def test_removing_an_early_member_does_not_move_already_ordered_survivors():
    server = PlexServer()
    cid = server.output('collection', ['11', '12', '13'])
    pid = server.output('playlist', ['11', '12', '13'])
    value = listing([MediaMember(2), MediaMember(3)])
    target = value.targets[0]
    target.collection.remote_id = cid
    target.playlists[0].remote_id = pid
    target.playlists[0].order = ['movie:2', 'movie:3']
    original = server.request

    def request(method, path, **kwargs):
        if method == 'PUT' and path.endswith('/move'):
            # Deletion alone must not depend on successful sorting mutations.
            server.wire.append((method, path, kwargs.get('params'), None, {}))
            return {}
        return original(method, path, **kwargs)

    server.request = request
    result = publish(server, value)
    assert result['success'], result
    assert server.collections[cid]['items'] == ['12', '13']
    assert [key for key, _ in server.playlists[pid]['items']] == ['12', '13']
    assert not any(path.endswith('/move') for _, path, *_ in server.writes)


def test_failed_real_reorder_still_prevents_removal_on_every_output():
    server = PlexServer()
    cid = server.output('collection', ['11', '12', '13'])
    pid = server.output('playlist', ['11', '12', '13'])
    value = listing([MediaMember(2), MediaMember(3)])
    target = value.targets[0]
    target.collection.remote_id = cid
    target.playlists[0].remote_id = pid
    target.playlists[0].order = ['movie:3', 'movie:2']
    original = server.request

    def request(method, path, **kwargs):
        if method == 'PUT' and path.endswith('/move'):
            return {}
        return original(method, path, **kwargs)

    server.request = request
    result = publish(server, value)
    assert not result['success']
    assert server.collections[cid]['items'] == ['11', '12', '13']
    assert [key for key, _ in server.playlists[pid]['items']] == ['11', '12', '13']
    assert not any(method == 'DELETE' for method, *_ in server.writes)


def test_collection_removal_uses_the_collection_membership_route():
    server = PlexServer()
    cid = server.output('collection', ['11', '12'])
    client = server.client()
    client.remove_collection_items(cid, ['11'])
    assert server.collections[cid]['items'] == ['12']
    assert [(method, path) for method, path, *_ in server.writes] == [
        ('DELETE', f'library/collections/{cid}/items/11')]
    assert set(server.media) >= {'11', '12'}


@pytest.mark.parametrize('order', [[2, 3, 1], [3, 1, 2], [2, 1, 3], [1, 3, 2]])
def test_single_movie_drag_uses_one_move_and_preserves_entry_ids(order):
    server = PlexServer()
    pid = server.output('playlist', ['11', '12', '13'])
    original_entries = dict(server.playlists[pid]['items'])
    value = listing([MediaMember(n) for n in (1, 2, 3)], collection=False)
    value.targets[0].playlists[0].remote_id = pid
    value.targets[0].playlists[0].order = [f'movie:{n}' for n in order]
    result = publish(server, value)
    assert result['success'], result
    assert sum(path.endswith('/move') for _, path, *_ in server.writes) == 1
    assert dict(server.playlists[pid]['items']) == original_entries


def test_plex_repositions_in_place_when_interior_moves_are_acknowledged_but_not_applied():
    server = PlexServer()
    pid = server.output('playlist', ['11', '12', '13'])
    original_entries = dict(server.playlists[pid]['items'])
    value = listing([MediaMember(n) for n in (1, 2, 3)], collection=False)
    value.targets[0].playlists[0].remote_id = pid
    value.targets[0].playlists[0].order = ['movie:3', 'movie:1', 'movie:2']
    original = server.request

    def request(method, path, **kwargs):
        if method == 'PUT' and path.endswith('/move'):
            entry = path.split('/')[-2]
            remaining = [row for row in server.playlists[pid]['items'] if row[1] != entry]
            if kwargs['params'].get('after') != remaining[-1][1]:
                return {}
        return original(method, path, **kwargs)

    server.request = request
    result = publish(server, value)
    assert result['success'], result
    assert [key for key, _ in server.playlists[pid]['items']] == ['13', '11', '12']
    assert dict(server.playlists[pid]['items']) == original_entries
    assert not any(method in ('POST', 'DELETE') for method, *_ in server.writes)


def test_mixed_list_forces_collection_off_but_publishes_movie_and_sorted_episodes():
    server = PlexServer(); value = listing([MediaMember(1), MediaMember(1, 'tv')])
    target = value.targets[0]; target.collection.remote_id = '999'
    target.playlists[0].order = ['tv:1', 'movie:1']
    assert not target.collection.enabled
    assert publish(server, value)['success']
    assert target.collection.remote_id == '999' and not server.collections
    assert [key for key, _ in server.playlists[target.playlists[0].remote_id]['items']] == ['1021', '1022', '1101', '1102', '11']


def test_selected_season_does_not_include_other_seasons_or_widen_collection():
    server = PlexServer(); value = listing([MediaMember(1, 'tv', seasons=[10])])
    assert not value.targets[0].collection.enabled
    assert publish(server, value)['success']
    assert [key for key, _ in next(iter(server.playlists.values()))['items']] == ['1101', '1102']


def test_cross_section_collection_fails_before_any_output_write():
    server = PlexServer(); server.media['12']['librarySectionID'] = '3'
    result = publish(server, listing())
    assert not result['success'] and result['error_code'] == 'unsupported'
    assert '媒体库' in result['error'] and not server.writes


@pytest.mark.parametrize('status', [401, 403, 404])
def test_auth_or_ambiguous_missing_binding_never_recreates(status):
    server = PlexServer(); value = listing(collection=False); value.targets[0].playlists[0].remote_id = '567'
    original = server.request
    def request(method, path, **kwargs):
        if path.startswith('playlists/567'): return status, {}
        return original(method, path, **kwargs)
    server.request = request
    result = publish(server, value)
    assert not result['success'] and not server.writes
    assert value.targets[0].playlists[0].remote_id == '567'
    assert 'private' not in result['error']


@pytest.mark.parametrize('kind', ['collection', 'playlist'])
def test_smart_output_is_not_taken_over(kind):
    server = PlexServer(); key = server.output(kind, ['11'], smart=True)
    value = listing(collection=kind == 'collection', playlist=kind == 'playlist')
    binding = value.targets[0].collection if kind == 'collection' else value.targets[0].playlists[0]
    binding.remote_id = key
    assert not publish(server, value)['success'] and not server.writes


def test_duplicate_playlist_rating_keys_have_distinct_entries_and_are_pruned_safely():
    server = PlexServer(); value = listing(collection=False)
    pid = server.output('playlist', ['11', '12', '11'])
    value.targets[0].playlists[0].remote_id = pid
    assert publish(server, value)['success']
    assert [key for key, _ in server.playlists[pid]['items']] == ['12', '11']


def test_collection_restriction_releases_without_reenabling_or_losing_binding():
    value = listing(); target = value.targets[0]; target.collection.remote_id = '500'
    value.add_member(MediaMember(1, 'tv'))
    assert not target.collection.enabled
    value.remove_member('tv:1')
    assert not target.collection.enabled and target.collection.remote_id == '500'
    target.collection.enabled = True; value.validate()
    assert target.collection.enabled


def test_complete_show_collection_and_libraries_remain_independent_from_playback():
    server = PlexServer(); value = listing([MediaMember(1, 'tv')])
    assert publish(server, value)['success']
    collection = next(iter(server.collections.values()))
    assert collection['items'] == ['21'] and collection['subtype'] == 'show' and collection['librarySectionID'] == '2'
    seasons = server.client().get_series_seasons(server.client().list_media(['2'])[0])
    assert [season.season_number for season in seasons] == [2, 10]


def test_existing_collection_in_another_section_blocks_playlist_writes_too():
    server = PlexServer(); cid = server.output('collection', [], librarySectionID='3')
    value = listing(); value.targets[0].collection.remote_id = cid
    result = publish(server, value)
    assert not result['success'] and not server.writes


def test_regular_collection_without_smart_field_matches_real_pms_response():
    server = PlexServer()
    value = listing()
    target = value.targets[0]
    key = server.output('collection', ['11'])
    server.collections[key].pop('smart')
    target.collection.remote_id = key
    result = publish(server, value)
    assert result['success'], result
    assert target.collection.remote_id == key
    assert server.collections[key]['items'] == ['11', '12']


@pytest.mark.parametrize('smart', [True, 1, '1', None, 'unknown'])
def test_bound_smart_or_invalid_collection_is_rejected_before_writes(smart):
    server = PlexServer()
    value = listing()
    key = server.output('collection', ['11'], smart=smart)
    value.targets[0].collection.remote_id = key
    result = publish(server, value)
    assert result['error_code'] == 'unsupported' and not server.writes


@pytest.mark.parametrize('kind', ['collection', 'playlist'])
def test_same_name_smart_objects_block_creation_without_takeover(kind):
    server = PlexServer(); server.output(kind, ['11'], title='Plex list' if kind == 'collection' else 'Watch', smart=True)
    value = listing(collection=kind == 'collection', playlist=kind == 'playlist')
    result = publish(server, value)
    assert result['error_code'] == 'name_conflict' and not server.writes


def test_create_large_collection_persists_binding_before_remaining_batch_can_fail():
    server = PlexServer()
    members = []
    for number in range(1, 106):
        key = str(3000 + number); server.media[key] = movie(key, number)
        members.append(MediaMember(number))
    value = listing(members, playlist=False); saved = []
    # Large paging must fit; the double still caps pages to two rows.
    server.fail = ('PUT', 'library/collections/501/items')
    result = publish(server, value, on_binding=lambda: saved.append(value.targets[0].collection.remote_id))
    assert result['status'] == 'partial' and saved == ['501']
    assert value.targets[0].collection.remote_id == '501'
    assert len(server.collections['501']['items']) == 100
    server.fail = None
    assert publish(server, value)['success'] and len(server.collections['501']['items']) == 105
    assert len(server.collections) == 1


def test_failed_binding_persistence_stops_before_playlist_creation_or_deletions():
    server = PlexServer(); value = listing()
    def fail(): raise RuntimeError('storage failed')
    result = publish(server, value, on_binding=fail)
    assert result['status'] == 'partial' and value.targets[0].collection.remote_id
    assert not server.playlists and len(server.writes) == 1


def test_noop_accepted_mutation_is_detected_without_pruning_existing_items():
    server = PlexServer(); value = listing()
    cid = server.output('collection', ['11', '13']); value.targets[0].collection.remote_id = cid
    original = server.request
    def request(method, path, **kwargs):
        if method == 'PUT' and path.endswith('/items'): return {}
        return original(method, path, **kwargs)
    server.request = request
    result = publish(server, value)
    assert not result['success'] and server.collections[cid]['items'] == ['11', '13']
    assert not any(method == 'DELETE' for method, *_ in server.writes)


def test_move_first_middle_and_last_uses_predecessor_entry_never_rating_key():
    server = PlexServer(); pid = server.output('playlist', ['11', '12', '13']); client = server.client()
    entry = server.playlists[pid]['items'][2][1]
    for index, expected in [(0, ['13', '11', '12']), (1, ['11', '13', '12']), (2, ['11', '12', '13'])]:
        client.move_playlist_entry(pid, entry, index)
        assert [key for key, _ in server.playlists[pid]['items']] == expected
    for index in (-1, 3, True):
        with pytest.raises(EmbyAPIError): client.move_playlist_entry(pid, entry, index)


@pytest.mark.parametrize('mutate', [
    lambda row: row.update(parentIndex=None),
    lambda row: row.update(Media=[]),
    lambda row: row.update(type='track'),
])
def test_invalid_episode_never_publishes_any_output(mutate):
    server = PlexServer(); mutate(server.media['1021'])
    result = publish(server, listing([MediaMember(1, 'tv')]))
    assert not result['success'] and not server.writes


@pytest.mark.parametrize('guid', ['tmdb://0', 'tmdb://oops'])
def test_malformed_tmdb_guid_never_matches_another_identity(guid):
    server = PlexServer(); server.media['11']['Guid'] = [{'id': guid}]
    with pytest.raises(EmbyAPIError): server.client().list_media()


def test_conflicting_guids_fail_and_absent_guid_stays_unidentified():
    server = PlexServer(); server.media['11']['Guid'].append({'id':'tmdb://2'})
    with pytest.raises(EmbyAPIError): server.client().list_media()
    server.media['11']['Guid'] = [{'id':'imdb://tt123'}]
    assert server.client().list_media()[0].key is None


def test_selected_library_disappearing_is_an_error_and_dry_run_has_zero_writes():
    server = PlexServer()
    with pytest.raises(EmbyAPIError): server.client().list_media(['999'])
    with pytest.raises(ValueError): server.client().list_media(media_type='track')
    result = publish(server, listing(), dry_run=True)
    assert result['success'] and result['plan'] and not server.writes


def test_bounded_pagination_and_invalid_response_are_explicit_failures():
    server = PlexServer()
    with pytest.raises(EmbyAPIError): server.client(max_pages=1).list_media()
    client = PlexClient('http://server', 'private', transport=lambda *args, **kwargs: {'MediaContainer': {'size': 2}})
    with pytest.raises(EmbyAPIError): client.list_libraries()
    with pytest.raises(EmbyAPIError): PlexClient('http://server', 'private', transport=lambda *a, **k: {}).list_media()


def test_builtin_transport_preserves_proxy_path_hides_token_and_refuses_redirects(monkeypatch):
    import json
    from urllib.parse import urlsplit
    from app.plugins.medialibraryplaylistmanager import plex
    seen = []
    class Response:
        headers = {}
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self): return json.dumps({'MediaContainer': {'size': 0, 'Directory': []}}).encode()
    class Opener:
        def open(self, request, timeout):
            seen.append(request)
            assert request.get_header('X-plex-token') == 'private'
            assert urlsplit(request.full_url).path == '/proxy/plex/library/sections'
            assert 'private' not in request.full_url
            return Response()
    def opener(handler):
        assert handler.redirect_request(None, None, 302, '', {}, 'http://untrusted') is None
        return Opener()
    monkeypatch.setattr(plex, 'build_opener', opener)
    assert PlexClient('http://server/proxy/plex', 'private').list_libraries() == [] and seen


@pytest.mark.parametrize('url', ['ftp://server', 'http://u:p@server', 'http://server?token=x', 'http://server#x'])
def test_invalid_server_url_rejected(url):
    with pytest.raises(ValueError): PlexClient(url, 'private')


def test_transport_is_called_once_and_does_not_expose_exception_text():
    calls = []
    def transport(method, path, **kwargs):
        calls.append(path); raise TypeError('private URL credentials')
    with pytest.raises(EmbyAPIError) as error: PlexClient('http://server', 'private', transport=transport).list_media()
    assert len(calls) == 1 and 'private' not in str(error.value)


def test_official_unpaged_metadata_responses_are_complete_and_supported():
    server = PlexServer(); server.bad_page = 'unpaged'
    assert publish(server, listing())['success']


def test_header_only_paging_and_optional_total_size_are_supported():
    for total in (True, False):
        server = PlexServer(); original = server.request
        class Response:
            status_code = 200
            content = b'not-empty'
            def __init__(self, body):
                container = body['MediaContainer']
                self.headers = {}
                if 'offset' in container:
                    self.headers['X-Plex-Container-Start'] = str(container.pop('offset'))
                    count = container.pop('totalSize')
                    if total: self.headers['X-Plex-Container-Total-Size'] = str(count)
                self.body = body
            def json(self): return self.body
        server.request = lambda *args, **kwargs: Response(original(*args, **kwargs))
        assert publish(server, listing())['success']


def test_connection_fingerprint_saved_with_binding_and_changed_server_never_takes_over():
    first = PlexServer(); value = listing(collection=False); target = value.targets[0]
    target.connection_fingerprint = None
    saved = []
    assert publish(first, value, on_binding=lambda:saved.append(target.to_dict()))['success']
    assert saved[0]['connection_fingerprint'] and 'private' not in str(saved)
    second = PlexServer(); second.machine = 'another-machine'
    second.output('playlist', ['13','12'])
    before = copy.deepcopy(second.playlists)
    result = publish(second, MediaList.from_dict(value.to_dict()))
    assert result['error_code'] == 'connection_changed' and not second.writes and second.playlists == before


def test_existing_unverified_bindings_are_not_implicitly_claimed():
    server = PlexServer(); value = listing(collection=False)
    value.targets[0].playlists[0].remote_id = server.output('playlist', ['13'])
    value.targets[0].connection_fingerprint = None
    result = publish(server, value)
    assert result['error_code'] == 'connection_changed' and not server.writes


@pytest.mark.parametrize('kind', ['collection','playlist'])
def test_cancel_after_first_removal_stops_before_next_delete(kind):
    server = PlexServer(); value = listing([MediaMember(1)], collection=kind=='collection', playlist=kind=='playlist')
    key = server.output(kind,['11','12','13'])
    binding = value.targets[0].collection if kind == 'collection' else value.targets[0].playlists[0]
    binding.remote_id = key
    cancelled = False
    original = server.request
    def request(method, path, **kwargs):
        nonlocal cancelled
        result = original(method,path,**kwargs)
        if method == 'DELETE': cancelled = True
        return result
    server.request = request
    result = publish(server,value,cancelled=lambda:cancelled)
    assert result['status'] == 'cancelled'
    assert len([row for row in server.writes if row[0]=='DELETE']) == 1


@pytest.mark.parametrize('change', ['token','url'])
def test_token_or_host_change_cannot_claim_existing_binding(change):
    server = PlexServer(); value = listing(collection=False)
    assert publish(server,value)['success']
    server.wire.clear()
    client = PlexClient('http://other/plex' if change=='url' else 'http://server/proxy/plex',
                        'different' if change=='token' else 'private', transport=lambda *a, **k:{'MediaContainer':{'machineIdentifier':'machine'}})
    result = Publisher(client).publish(value,value.targets[0])
    assert result['error_code'] == 'connection_changed' and not server.writes


def test_concurrent_request_cannot_replace_another_responses_paging_headers():
    import threading
    from concurrent.futures import ThreadPoolExecutor
    server = PlexServer()
    for number in range(5): server.output('playlist',['11'],title=f'Watch {number}')
    original = server.request
    paused, release = threading.Event(), threading.Event()
    class Response:
        status_code = 200
        content = b'not-empty'
        def __init__(self, body, path, params):
            self.body, self.path, self.params = body,path,params
            container = body['MediaContainer']; self.headers = {}
            if 'offset' in container:
                self.headers['X-Plex-Container-Start'] = str(container.pop('offset'))
                self.headers['X-Plex-Container-Total-Size'] = str(container.pop('totalSize'))
        def json(self):
            if self.path == 'playlists' and self.params.get('X-Plex-Container-Start') == 0:
                paused.set(); assert release.wait(3)
            return self.body
    def request(method,path,params=None,**kwargs):
        return Response(original(method,path,params=params,**kwargs),path,params or {})
    server.request=request; client=server.client()
    with ThreadPoolExecutor(2) as executor:
        reading=executor.submit(client.list_playlists)
        assert paused.wait(3)
        try: assert client.list_libraries()
        finally: release.set()
        assert len(reading.result(3)) == 5

