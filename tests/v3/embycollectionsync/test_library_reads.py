"""Opening multiple targets and reopening a dialog share bounded directory reads."""
import threading
from types import SimpleNamespace

from app.plugins.embycollectionsync import EmbyCollectionSync
from app.plugins.embycollectionsync.requests import ProviderCalls


def test_three_target_directories_wait_for_capacity_instead_of_returning_busy():
    plugin = EmbyCollectionSync()
    gate = threading.Event()
    started = [threading.Event(), threading.Event()]
    results = {}
    workers = []

    def read(index):
        if index < 2:
            started[index].set()
        gate.wait(2)
        return [{'id': str(index), 'name': f'Library {index}'}]

    plugin._client = lambda server: SimpleNamespace(list_libraries=lambda: read(int(server)))
    plugin._server_configs = lambda: {str(i): {'kind': 'emby'} for i in range(3)}
    try:
        for i in range(3):
            worker = threading.Thread(target=lambda i=i: results.update({i: plugin.api_libraries({'server_id': str(i)})}))
            workers.append(worker)
            worker.start()
            if i < 2:
                assert started[i].wait(1)
        workers[-1].join(.05)
    finally:
        gate.set()
        for worker in workers:
            worker.join(2)
        plugin.stop_service()
    assert len(results) == 3
    assert all(result['success'] for result in results.values()), results


def test_reopening_while_directory_read_is_running_reuses_the_same_remote_call():
    plugin = EmbyCollectionSync()
    gate, started = threading.Event(), threading.Event()
    calls, results, workers = [], [], []

    def read():
        calls.append(1)
        started.set()
        gate.wait(2)
        return [{'id': '3', 'name': 'Movies'}]

    plugin._client = lambda _: SimpleNamespace(list_libraries=read)
    plugin._server_configs = lambda: {'same': {'kind': 'emby'}}
    try:
        for i in range(2):
            worker = threading.Thread(target=lambda: results.append(plugin.api_libraries({'server_id': 'same'})))
            workers.append(worker)
            worker.start()
            assert started.wait(1)
        workers[-1].join(.05)
    finally:
        gate.set()
        for worker in workers:
            worker.join(2)
        plugin.stop_service()
    assert len(calls) == 1
    assert len(results) == 2 and all(result['success'] for result in results)
    assert all(result['items'] == [{'id': '3', 'name': 'Movies'}] for result in results)


def test_directory_queue_timeout_does_not_start_an_extra_worker_or_release_a_busy_slot():
    calls = ProviderCalls(timeout=.04, cooldown=0, capacity=1)
    gate = threading.Event()
    invoked = []
    try:
        assert calls.call(lambda: gate.wait(2))['status'] == 'timeout'
        result = calls.call_shared('next', lambda: invoked.append(1), provider='other')
        assert result['status'] == 'timeout' and not invoked
        assert calls.call(lambda: 1)['status'] == 'busy'
        assert not calls._shared
    finally:
        gate.set()
        calls.stop()


def test_stop_interrupts_both_directory_leader_and_joiner_without_waiting_for_remote():
    calls = ProviderCalls(timeout=2)
    gate, started = threading.Event(), threading.Event()
    results, workers = [], []

    def read():
        started.set()
        gate.wait(2)

    try:
        for _ in range(2):
            worker = threading.Thread(target=lambda: results.append(calls.call_shared('same', read)))
            workers.append(worker)
            worker.start()
            assert started.wait(1)
        calls.stop()
        for worker in workers:
            worker.join(.3)
            assert not worker.is_alive()
        assert len(results) == 2 and all(result['status'] == 'cancelled' for result in results)
        assert not calls._shared
    finally:
        gate.set()
        calls.stop()


def test_changed_server_configuration_does_not_join_old_directory_read():
    plugin = EmbyCollectionSync()
    gate, started = threading.Event(), threading.Event()
    version = [1]
    results, calls = [], []
    plugin._server_configs = lambda: {'same': {'host': f'http://version{version[0]}'}}

    def client(_):
        current = version[0]
        def read():
            calls.append(current)
            if current == 1:
                started.set()
                gate.wait(2)
            return [{'id': str(current), 'name': f'Library {current}'}]
        return SimpleNamespace(list_libraries=read)

    plugin._client = client
    worker = threading.Thread(target=lambda: results.append(plugin.api_libraries({'server_id': 'same'})))
    try:
        worker.start()
        assert started.wait(1)
        version[0] = 2
        fresh = plugin.api_libraries({'server_id': 'same'})
        assert fresh['success'] and fresh['items'][0]['id'] == '2'
    finally:
        gate.set()
        worker.join(2)
        plugin.stop_service()
    assert sorted(calls) == [1, 2]


def test_cancelling_either_shared_waiter_does_not_cancel_the_other():
    for cancelled_id in ('first', 'second'):
        calls = ProviderCalls(timeout=2)
        gate, started = threading.Event(), threading.Event()
        results, workers, invoked = {}, [], []

        def read():
            invoked.append(1)
            started.set()
            gate.wait(2)
            return ['Movies']

        try:
            for request_id in ('first', 'second'):
                worker = threading.Thread(target=lambda request_id=request_id: results.update({request_id: calls.call_shared('same', read, request_id=request_id)}))
                workers.append(worker)
                worker.start()
                assert started.wait(1)
            # Wait for the second caller to finish entering the shared wait.
            import time
            until = time.monotonic() + 1
            while not calls.is_active('second') and time.monotonic() < until:
                time.sleep(.001)
            assert calls.cancel(cancelled_id)
            workers[0 if cancelled_id == 'first' else 1].join(.3)
            assert results[cancelled_id]['status'] == 'cancelled'
            gate.set()
            for worker in workers:
                worker.join(1)
            other = 'second' if cancelled_id == 'first' else 'first'
            assert results[other]['status'] == 'success'
            assert results[other]['value'] == ['Movies']
            assert invoked == [1]
        finally:
            gate.set()
            calls.stop()
            for worker in workers:
                worker.join(1)
