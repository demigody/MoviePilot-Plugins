import copy
import threading
import time

from .test_api import create, member, plugin
from .test_sync import RestServer, listing
from app.plugins.embycollectionsync.sync import Publisher


def test_publisher_reports_real_stages_without_changing_publication():
    server = RestServer()
    value = listing()
    stages = []
    result = Publisher(server.client()).publish(value, value.targets[0], on_progress=stages.append)
    assert result['success'], result
    assert stages[0] == 'preflight'
    assert all(stage in stages for stage in ['inventory', 'plan', 'add', 'verify', 'order', 'remove', 'verify_final'])
    assert stages.index('add') < stages.index('verify') < stages.index('remove') < stages.index('verify_final')
    assert server.collections['c'] == ['m1', 'm2']


def test_progress_callback_failure_does_not_interrupt_or_duplicate_publication():
    server = RestServer()
    value = listing()
    def broken(_stage):
        raise RuntimeError('display unavailable')
    result = Publisher(server.client()).publish(value, value.targets[0], on_progress=broken)
    assert result['success'] and server.collections['c'] == ['m1', 'm2']


def test_job_progress_remains_readable_during_blocked_media_read_and_counts_targets(plugin, monkeypatch):
    first, second = RestServer(), RestServer()
    blocked, release = threading.Event(), threading.Event()
    def wait(action, _identity):
        if action == 'inventory':
            blocked.set()
            assert release.wait(3)
    second.hook = wait
    plugin._clients.update({'Emby': first.client(), 'other': second.client()})
    value = create(plugin, members=[member(1)], targets=[
        {'server_id': server, 'collection': {'enabled': True, 'remote_id': 'c'}} for server in ['Emby', 'other']])
    try:
        queued = plugin.api_publish({'list_id': value['id']})
        assert blocked.wait(2)
        start = time.monotonic()
        snapshot = plugin.api_job(queued['job_id'])
        assert time.monotonic() - start < .2
        assert snapshot['progress'] == {'completed': 1, 'total': 2, 'stage': 'inventory',
            'list_name': value['name'], 'server_id': 'other'}
        saved = copy.deepcopy(snapshot['progress'])
        snapshot['progress']['completed'] = 100
        assert plugin.api_job(queued['job_id'])['progress'] == saved
    finally:
        release.set()
    deadline = time.monotonic() + 2
    while plugin.api_job(queued['job_id'])['status'] in {'queued', 'running'} and time.monotonic() < deadline:
        time.sleep(.005)
    final = plugin.api_job(queued['job_id'])
    assert final['status'] == 'success'
    assert final['progress']['completed'] == final['progress']['total'] == 2


def test_progress_counts_subscription_and_source_checks_only_when_they_run(plugin, monkeypatch):
    server = RestServer()
    plugin._clients['Emby'] = server.client()
    value = create(plugin, members=[member(1)], policy={'auto_subscribe': True}, targets=[
        {'server_id': 'Emby', 'collection': {'enabled': True, 'remote_id': 'c'}}])
    phases = []
    monkeypatch.setattr(plugin, '_refresh_sources', lambda _: [])
    monkeypatch.setattr(plugin, '_subscribe_pending', lambda _: [])
    selected = [plugin._find(value['id'])]
    results = plugin._publish_selected(selected, generation=plugin._stop, refresh_sources=True,
        on_progress=lambda value: phases.append(copy.deepcopy(value)))
    assert all(result['success'] for result in results)
    assert phases[0]['stage'] == 'sources' and phases[0]['total'] == 3
    assert next(value for value in phases if value['stage'] == 'inventory')['completed'] == 1
    assert next(value for value in phases if value['stage'] == 'subscribe')['completed'] == 2
    assert phases[-1]['completed'] == phases[-1]['total'] == 3
    phases.clear()
    plugin._publish_selected(selected, generation=plugin._stop, refresh_sources=True, dry_run=True,
        on_progress=lambda value: phases.append(copy.deepcopy(value)))
    assert not any(value['stage'] in {'sources', 'subscribe'} for value in phases)
    assert phases[-1]['completed'] == phases[-1]['total'] == 1
