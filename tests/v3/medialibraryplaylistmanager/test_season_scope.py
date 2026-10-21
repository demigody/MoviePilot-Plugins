"""One work identity, with an explicit season boundary shared by every output."""
import copy

import pytest

from app.plugins.medialibraryplaylistmanager.models import MediaMember, MediaList
from .test_season_playlist import season_server, add_episode
from .test_sync import run


def scoped_member(seasons):
    return MediaMember.from_dict({'tmdb_id': 1, 'media_type': 'tv', 'title': '剧', 'seasons': seasons})


def add_seasons(server):
    server.seasons = {'s1': ['season1', 'season2', 'season3']}
    for number in (1, 2, 3):
        server.media[f'season{number}'] = {'Id': f'season{number}', 'Name': f'S{number}',
            'Type': 'Season', 'SeriesId': 's1', 'IndexNumber': number}


def test_selected_seasons_round_trip_without_splitting_work_identity():
    member = scoped_member([4, 3, 3])
    assert member.to_dict()['seasons'] == [3, 4]
    assert member.key == 'tv:1'
    value = MediaList('范围', members=[member])
    assert MediaList.from_dict(value.to_dict()).to_dict() == value.to_dict()
    assert 'seasons' not in MediaMember(1, 'tv').to_dict()


@pytest.mark.parametrize('seasons', [[], '3,4', [True], [-1], ['3'], [1.5]])
def test_invalid_scope_never_silently_widens_to_whole_show(seasons):
    with pytest.raises(ValueError):
        scoped_member(seasons)


def test_scope_limits_both_outputs_and_prunes_previously_published_other_seasons():
    server, value = season_server()
    add_seasons(server)
    assert run(server, value)['success']
    value.members[1] = scoped_member([2, 3])
    saved = copy.deepcopy(value.to_dict())
    result = run(server, value)
    assert result['success'], result
    assert set(server.collections['c']) == {'m1', 'm2', 'season2'}
    assert [item for _, item in server.playlists['p']] == ['b1', 'b2', 'm1', 'm2']
    add_episode(server, 'c1', 3, 1)
    add_episode(server, 'a3', 1, 3)
    assert run(server, value)['success']
    assert set(server.collections['c']) == {'m1', 'm2', 'season2', 'season3'}
    assert [item for _, item in server.playlists['p']] == ['b1', 'b2', 'c1', 'm1', 'm2']
    assert value.to_dict() == saved
    writes = len(server.writes)
    assert run(server, value)['success']
    assert len(server.writes) == writes


def test_missing_inventory_cannot_retain_episodes_outside_selected_scope():
    server, value = season_server()
    add_seasons(server)
    assert run(server, value)['success']
    server.inventory.remove('s1')
    value.members[1] = scoped_member([2])
    result = run(server, value)
    assert result['success'], result
    assert [item for _, item in server.playlists['p']] == ['b1', 'b2', 'm1', 'm2']
    assert 's1' not in server.collections['c']


def test_failed_season_scan_prevents_writes_to_any_output():
    server, value = season_server()
    add_seasons(server)
    value.members[1] = scoped_member([2])
    server.fail = 'seasons'
    assert not run(server, value)['success']
    assert not server.writes
