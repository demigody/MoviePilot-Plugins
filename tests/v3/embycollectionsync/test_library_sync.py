import copy
import threading
from enum import Enum
from types import SimpleNamespace

import pytest

from app.plugins.embycollectionsync.library_sync import LibrarySyncBatcher, transfer_keys, expected_for
from app.plugins.embycollectionsync.models import MediaMember
from .test_api import plugin, create, member
from .test_sync import RestServer
from .test_season_playlist import season_server, add_episode
from app.plugins.embycollectionsync.sync import Publisher


class Timers:
    def __init__(self): self.items = []
    def __call__(self, seconds, callback):
        owner = self
        class Timer:
            def __init__(self): self.seconds, self.callback, self.cancelled, self.fired = seconds, callback, False, False
            def start(self): owner.items.append(self)
            def cancel(self): self.cancelled = True
            def fire(self): self.fired = True; self.callback()
        return Timer()
    @property
    def pending(self): return [item for item in self.items if not item.cancelled and not item.fired]
    def fire(self):
        timer = self.pending[0]
        timer.fire()
        return timer.seconds


def fixture(outcomes):
    timers, history, calls = Timers(), {}, []
    def attempt(keys, ids, first):
        calls.append((keys,ids,first))
        return outcomes.pop(0)
    batcher = LibrarySyncBatcher(attempt, lambda value:history.update({value['history_id']:copy.deepcopy(value)}),timer_factory=timers)
    return batcher,timers,history,calls


def outcome(pending=True):
    return {'results':[{'success':True,'library_pending':['movie:1'] if pending else []}], 'retry':pending}


def test_exactly_three_exponential_retries_share_one_record_without_real_waits():
    batcher,timers,history,calls = fixture([outcome() for _ in range(4)])
    batcher.notify({'movie:1'},{'list'})
    assert [timers.fire() for _ in range(4)] == [30,120,240,480]
    assert not timers.pending and len(history) == 1 and len(calls) == 4
    record = next(iter(history.values()))
    assert record['status'] == 'success' and record['attempt_count'] == 4 and record['retry_count'] == 3
    assert not record.get('error')
    assert [call[2] for call in calls] == [True,False,False,False]


def test_visibility_stops_early_and_burst_notifications_merge_without_duplicate_records():
    batcher,timers,history,calls = fixture([outcome(),outcome(False)])
    batcher.notify({'movie:1'},{'list'})
    old = timers.pending[0]
    batcher.notify({'movie:2'},{'list2'})
    old.fire()
    assert not calls
    assert timers.fire() == 30
    assert timers.fire() == 120
    assert calls[-1][0] == {'movie:1','movie:2'} and calls[-1][1] == {'list','list2'}
    assert len(history) == 1 and next(iter(history.values()))['status'] == 'success'
    assert not timers.pending


def test_busy_does_not_consume_scan_retry_and_stop_cancels_stale_callbacks():
    batcher,timers,history,calls = fixture([None,outcome()])
    batcher.notify({'movie:1'},{'list'})
    assert timers.fire() == 30 and timers.fire() == 15
    record = next(iter(history.values()))
    assert record['attempt_count'] == 1 and record['retry_count'] == 1
    old = timers.pending[0]
    batcher.stop(); old.fire()
    assert len(calls) == 2 and next(iter(history.values()))['status'] == 'cancelled'
    batcher.notify({'movie:2'},{'list'})
    assert not timers.pending


def test_new_transfer_during_a_probe_is_not_lost_and_gets_its_retry_budget():
    timers, records, calls = Timers(), [], []
    entered, release = threading.Event(), threading.Event()
    def attempt(keys, ids, first):
        calls.append(keys)
        if len(calls) == 1:
            entered.set(); assert release.wait(1)
            return outcome(False)
        return outcome()
    batcher = LibrarySyncBatcher(attempt,lambda record:records.append(record),timer_factory=timers)
    batcher.notify({'movie:1'},{'list'})
    thread = threading.Thread(target=timers.fire); thread.start(); assert entered.wait(1)
    batcher.notify({'movie:2'},{'list'})
    release.set(); thread.join(1)
    assert timers.fire() == 30
    assert [timers.fire() for _ in range(3)] == [120,240,480]
    assert calls[1] == {'movie:1','movie:2'} and len({row['job_id'] for row in records}) == 1
    assert not timers.pending


def test_transfer_contract_supports_native_objects_snapshots_and_selected_seasons():
    class Source(str,Enum): TMDB = 'themoviedb'
    event = SimpleNamespace(event_data={'mediainfo':SimpleNamespace(media_id='211089',media_source=Source.TMDB,type='电视剧'),
        'meta':SimpleNamespace(begin_season=3,begin_episode=1,end_episode=3)})
    keys = transfer_keys(event)
    assert keys == {f'tv:211089:season:3:episode:{number}' for number in (1,2,3)}
    assert expected_for([MediaMember(211089,'tv',seasons=[4])],keys) == set()
    assert expected_for([MediaMember(211089,'tv',seasons=[3])],keys) == keys
    assert transfer_keys({'mediainfo':{'tmdb_id':1,'type':'电影'}}) == {'movie:1'}
    assert transfer_keys(None) == set()


def test_stop_during_first_inflight_attempt_finalizes_running_record():
    timers, records = Timers(), []
    entered, release = threading.Event(), threading.Event()
    def attempt(*_):
        entered.set(); assert release.wait(1)
        return outcome()
    batcher = LibrarySyncBatcher(attempt,lambda record:records.append(record),timer_factory=timers)
    batcher.notify({'movie:1'},{'list'})
    thread = threading.Thread(target=timers.fire); thread.start(); assert entered.wait(1)
    batcher.stop(); release.set(); thread.join(1)
    assert records[-1]['status'] == 'cancelled' and records[-1]['attempt_count'] == 0
    assert not timers.pending


def test_stop_after_initial_lock_contention_finalizes_waiting_record():
    batcher,timers,history,calls = fixture([None])
    batcher.notify({'movie:1'},{'list'})
    timers.fire(); batcher.stop()
    assert len(history) == 1 and next(iter(history.values()))['status'] == 'cancelled'
    assert not timers.pending


def test_existing_series_does_not_hide_a_new_episode_that_is_not_yet_scanned():
    server,value = season_server()
    target = value.targets[0]
    target.playlists = []
    expected = {'tv:1:season:2:episode:3'}
    result = Publisher(server.client()).publish(value,target,dry_run=True,expected_keys=expected)
    assert result['library_pending'] == list(expected)
    add_episode(server,'b3',2,3)
    result = Publisher(server.client()).publish(value,target,dry_run=True,expected_keys=expected)
    assert result['library_pending'] == [] and not server.writes


def test_multi_episode_file_covers_all_expected_episodes_without_duplicate_playback_entries():
    server,value = season_server()
    add_episode(server,'combined',3,1)
    server.media['combined']['IndexNumberEnd'] = 3
    expected = {f'tv:1:season:3:episode:{number}' for number in (1,2,3)}
    result = Publisher(server.client()).publish(value,value.targets[0],dry_run=True,expected_keys=expected)
    assert result['success'] and not result['library_pending']
    playlist = next(plan for plan in result['plan'] if plan['kind'] == 'playlist')
    assert playlist['order'].count('combined') == 1


@pytest.mark.parametrize('end',[True,0,1.5,1002])
def test_invalid_combined_episode_range_stops_publication(end):
    server,value = season_server()
    server.media['a1']['IndexNumberEnd'] = end
    result = Publisher(server.client()).publish(value,value.targets[0],expected_keys={'tv:1:season:1:episode:2'})
    assert not result['success'] and not server.writes


def setup_transfer(plugin,monkeypatch):
    timers,server = Timers(),RestServer()
    monkeypatch.setattr('app.plugins.embycollectionsync.library_sync.threading.Timer',timers)
    plugin._clients['Emby'] = server.client()
    value = create(plugin,members=[member(3),member(99)],policy={'publish_on_library':True},targets=[{'server_id':'Emby','collection':{'enabled':True,'remote_id':'c'}}])
    plugin._config['enabled'] = True
    plugin.on_transfer_complete({'mediainfo':{'tmdb_id':3,'type':'电影'}})
    return timers,server,value


def test_real_publish_retry_adds_newly_visible_content_and_keeps_one_history_record(plugin,monkeypatch):
    timers,server,value = setup_transfer(plugin,monkeypatch)
    timers.fire()
    assert len(plugin._history) == 1 and plugin._history[0]['status'] == 'waiting'
    assert not plugin._publish_lock.locked() and plugin._data_lock.acquire(blocking=False)
    plugin._data_lock.release()
    history_id = plugin._history[0]['history_id']
    server.inventory.append('m3')
    timers.fire()
    assert server.collections['c'] == ['m3']
    assert len(plugin._history) == 1 and plugin._history[0]['history_id'] == history_id
    assert plugin._history[0]['status'] == 'success' and not timers.pending
    assert plugin._history[0]['results'][0]['pending'] == ['movie:99']


def test_unverified_membership_still_uses_the_library_retry_budget(plugin, monkeypatch):
    value = create(plugin, members=[member(3)], policy={'publish_on_library': True}, targets=[{'server_id': 'Emby'}])
    plugin._config['enabled'] = True
    result = {'success': False, 'status': 'partial', 'error_code': 'membership_unverified'}
    monkeypatch.setattr(plugin, '_publish_selected', lambda *args, **kwargs: [result])
    outcome = plugin._library_attempt({'movie:3'}, {value['id']}, False)
    assert outcome['retry'] is True and outcome['results'] == [result]
    assert not plugin._publish_lock.locked()


def test_disabling_a_list_while_waiting_prevents_the_next_probe(plugin,monkeypatch):
    timers,server,value = setup_transfer(plugin,monkeypatch)
    timers.fire()
    result = plugin.api_save_lists({'action':'set_enabled','list_id':value['id'],'revision':0,'enabled':False})
    assert result['success'], result
    count = len(server.calls)
    timers.fire()
    assert len(server.calls) == count and not timers.pending
    assert plugin._history[0]['status'] == 'cancelled'
