"""Exercise season groups through the real REST adapter and publish barriers."""
import copy

import pytest

from .test_sync import RestServer, listing, media, run
from app.plugins.embycollectionsync.models import MediaList, MediaMember


def add_episode(server, identity, season, episode, series='s1'):
    server.media[identity] = {**media(identity, 900 + len(server.media), 'Episode'), 'SeriesId': series,
                              'ParentIndexNumber': season, 'IndexNumber': episode}
    server.episodes.setdefault(series, []).append(identity)


def season_server():
    server = RestServer()
    server.inventory = ['m1', 'm2', 's1']
    for identity, season, episode in [('a2', 1, 2), ('b2', 2, 2), ('b1', 2, 1), ('a1', 1, 1)]:
        add_episode(server, identity, season, episode)
    server.playlists['p'] = [('old-a2', 'a2'), ('stale', 'm3'), ('duplicate-a2', 'a2')]
    value = listing(members=[MediaMember(1), MediaMember(1, 'tv'), MediaMember(2)])
    value.targets[0].playlists[0].order = ['tv:1:season:2', 'movie:1', 'tv:1:season:1', 'movie:2']
    return server, value


def test_whole_show_expands_internally_and_reconciles_in_place_idempotently():
    server, value = season_server()
    saved = value.to_dict()
    result = run(server, value)
    assert result['success'], result
    assert [item for _, item in server.playlists['p']] == ['a1', 'a2', 'b1', 'b2', 'm1', 'm2']
    assert set(server.collections['c']) == {'m1', 's1', 'm2'}
    assert value.to_dict() == saved
    for method, action, params in server.writes:
        if action in {'create_playlist', 'add_playlist'}:
            assert 's1' not in params['Ids'].split(',')
    writes = len(server.writes)
    assert run(server, value)['success']
    assert len(server.writes) == writes
    assert value.targets[0].playlists[0].remote_id == 'p'


def test_new_episodes_and_seasons_stay_inside_their_selected_work_position():
    server, value = season_server()
    assert run(server, value)['success']
    add_episode(server, 'a3', 1, 3)
    add_episode(server, 'c1', 3, 1)
    result = run(server, value)
    assert result['success'], result
    assert [item for _, item in server.playlists['p']] == ['a1', 'a2', 'a3', 'b1', 'b2', 'c1', 'm1', 'm2']


def test_legacy_whole_show_order_expands_all_seasons_and_specials():
    server, value = season_server()
    add_episode(server, 'special', 0, 1)
    value.targets[0].playlists[0].order = ['movie:1', 'tv:1', 'movie:2']
    assert run(server, value)['success']
    assert [item for _, item in server.playlists['p']] == ['m1', 'special', 'a1', 'a2', 'b1', 'b2', 'm2']


def test_pending_show_retains_existing_episodes_and_removing_show_prunes_them():
    server, value = season_server()
    assert run(server, value)['success']
    server.inventory.remove('s1')
    result = run(server, value)
    assert result['success'] and result['pending'] == ['tv:1'], result
    assert [item for _, item in server.playlists['p']] == ['a1', 'a2', 'b1', 'b2', 'm1', 'm2']
    value.remove_member('tv:1')
    assert run(server, value)['success']
    assert [item for _, item in server.playlists['p']] == ['m1', 'm2']


@pytest.mark.parametrize('field,bad', [('SeriesId', 'wrong-show'), ('ParentIndexNumber', None), ('IndexNumber', -1), ('IsPlaceHolder', True)])
def test_incomplete_or_wrong_episode_identity_prevents_all_writes(field, bad):
    server, value = season_server()
    server.media['a1'][field] = bad
    before = copy.deepcopy((server.playlists, server.collections))
    assert not run(server, value)['success']
    assert not server.writes
    assert (server.playlists, server.collections) == before


def test_incomplete_episode_scan_blocks_collection_and_playlist_writes():
    server, value = season_server()
    server.fail = 'episodes'
    assert not run(server, value)['success']
    assert not server.writes


def test_season_order_round_trip_preserves_members_and_prunes_only_removed_parent():
    _, value = season_server()
    parsed = MediaList.from_dict(value.to_dict())
    assert parsed.to_dict() == value.to_dict()
    parsed.remove_member('tv:1')
    assert parsed.targets[0].playlists[0].order == ['movie:1', 'movie:2']


def test_episode_key_cannot_be_persisted_as_a_sortable_group():
    _, value = season_server()
    raw = value.to_dict()
    raw['targets'][0]['playlists'][0]['order'] = ['tv:1:season:1:episode:1']
    with pytest.raises(ValueError):
        MediaList.from_dict(raw)


def test_large_playlist_batches_writes_and_keeps_created_binding_after_partial_failure():
    server = RestServer()
    server.inventory = ['s1']
    for episode in range(1, 236):
        add_episode(server, f'ep{episode}', 1, episode)
    value = listing(bound=False, members=[MediaMember(1, 'tv')], collection=False)
    server.fail = 'add_playlist'
    result = run(server, value)
    assert result['status'] == 'partial'
    binding = value.targets[0].playlists[0].remote_id
    assert binding and len(server.playlists[binding]) == 100
    server.fail = None
    result = run(server, value)
    assert result['success'], result
    assert value.targets[0].playlists[0].remote_id == binding
    assert len(server.playlists[binding]) == 235
    assert [item for _, item in server.playlists[binding]] == [f'ep{number}' for number in range(1,236)]
    assert all(len(params['Ids'].split(',')) <= 100 for _, action, params in server.writes if action in {'create_playlist','add_playlist'})


def test_selected_ids_drive_both_outputs_and_later_arrivals_fill_their_original_positions():
    server = RestServer()
    server.media['s1'] = media('s1',111697,'Series')
    server.inventory = ['s1','m1']
    add_episode(server,'a1',1,1)
    add_episode(server,'a2',2,1)
    selected = [MediaMember(111697,'tv'), MediaMember(286679,'tv'), MediaMember(286680,'tv'), MediaMember(1)]
    value = listing(bound=False,members=selected)
    value.targets[0].playlists[0].order = ['tv:286680','movie:1','tv:111697','tv:286679']
    before = [member.to_dict() for member in value.members]
    result = run(server,value)
    assert result['success'] and result['pending'] == ['tv:286679','tv:286680'], result
    target = value.targets[0]
    assert set(server.collections[target.collection.remote_id]) == {'s1','m1'}
    assert [item for _,item in server.playlists[target.playlists[0].remote_id]] == ['m1','a1','a2']
    # An unrelated show with a similar name must never replace a selected ID.
    server.media['wrong'] = {**media('wrong',999,'Series'),'Name':'马大帅 III'}
    server.inventory.append('wrong')
    add_episode(server,'wrong-episode',1,1,'wrong')
    assert run(server,value)['pending'] == ['tv:286679','tv:286680']
    for identifier,tmdb in [('s2',286679),('s3',286680)]:
        server.media[identifier] = media(identifier,tmdb,'Series')
        server.inventory.append(identifier)
        add_episode(server,identifier+'-episode',1,1,identifier)
    result = run(server,value)
    assert result['success'] and result['pending'] == [], result
    assert set(server.collections[target.collection.remote_id]) == {'s1','s2','s3','m1'}
    assert [item for _,item in server.playlists[target.playlists[0].remote_id]] == ['s3-episode','m1','a1','a2','s2-episode']
    assert [member.to_dict() for member in value.members] == before
    writes = len(server.writes)
    assert run(server,value)['success'] and len(server.writes) == writes
