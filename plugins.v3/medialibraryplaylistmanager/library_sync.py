"""Coalesce transfer notifications and retry visibility without holding publish locks."""
from __future__ import annotations

import copy
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable
from uuid import uuid4


def _field(value, name, default=None):
    return value.get(name, default) if isinstance(value, dict) else getattr(value, name, default)


def transfer_keys(event) -> set[str]:
    """Read MP's stable TransferComplete payload, including precise TV episodes."""
    payload = _field(event, 'event_data', event) or {}
    media, meta = _field(payload, 'mediainfo'), _field(payload, 'meta')
    number = _field(media, 'tmdb_id')
    source = _field(media, 'media_source', '')
    if not number and str(getattr(source, 'value', source)) == 'themoviedb':
        number = _field(media, 'media_id')
    raw_type = _field(media, 'type', _field(media, 'media_type', ''))
    kind = str(getattr(raw_type, 'value', raw_type)).casefold()
    if isinstance(number, bool) or not str(number or '').isascii() or not str(number or '').isdigit() or int(number) <= 0:
        return set()
    if kind in {'movie', '电影'}:
        return {f'movie:{int(number)}'}
    if kind not in {'tv', 'series', '电视剧'}:
        return set()
    parent = f'tv:{int(number)}'
    season = _field(meta, 'begin_season')
    if isinstance(season, bool) or not isinstance(season, int) or season < 0:
        return {parent}
    group = f'{parent}:season:{season}'
    first, last = _field(meta, 'begin_episode'), _field(meta, 'end_episode')
    if isinstance(first, bool) or not isinstance(first, int) or first < 0:
        return {group}
    if isinstance(last, bool) or not isinstance(last, int) or last < first:
        last = first
    if last - first > 1000:
        return {group}
    return {f'{group}:episode:{episode}' for episode in range(first, last + 1)}


def expected_for(members, keys: set[str]) -> set[str]:
    selected = {member.key: member for member in members}
    result = set()
    for key in keys:
        parts = key.split(':')
        member = selected.get(':'.join(parts[:2]))
        if member is None:
            continue
        if member.seasons is not None and len(parts) >= 4 and int(parts[3]) not in member.seasons:
            continue
        result.add(key)
    return result


def pending_keys(expected: set[str], matches, episodes) -> list[str]:
    available = set(matches)
    for episode in episodes:
        if episode.parent_key:
            available.add(episode.parent_key)
        if episode.group_key:
            available.add(episode.group_key)
        if episode.key:
            available.update(f'{episode.group_key}:episode:{number}' for number in episode.episode_numbers)
    return sorted(expected - available)


@dataclass
class _Batch:
    keys: set[str]
    list_ids: set[str]
    job_id: str = field(default_factory=lambda: uuid4().hex)
    history_id: str = field(default_factory=lambda: uuid4().hex)
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    version: int = 0
    retry: int = 0
    count: int = 0
    started: bool = False
    attempts: list[dict] = field(default_factory=list)
    results: list[dict] = field(default_factory=list)


class LibrarySyncBatcher:
    """One active batch; new transfer identities merge without losing their retry budget.

    The callback returns None for lock contention (not a scan attempt), or a
    dict containing results/retry/cancelled. Only timer callbacks perform I/O.
    ``record`` upserts the same job/history ID, keeping log growth bounded.
    """
    def __init__(self, attempt: Callable, record: Callable, *, timer_factory=None,
                 debounce: float = 30, delays=(120, 240, 480)):
        self.attempt, self.record = attempt, record
        self.timer_factory, self.debounce, self.delays = timer_factory, debounce, tuple(delays)
        self._lock = threading.RLock()
        self._batch = None
        self._timer = None
        self._running = False
        self._closed = False
        self._sequence = 0

    def notify(self, keys: set[str], list_ids: set[str]):
        with self._lock:
            if self._closed or not list_ids:
                return
            if self._batch is None:
                self._batch = _Batch(set(keys), set(list_ids))
            else:
                batch = self._batch
                if not keys.issubset(batch.keys) or not list_ids.issubset(batch.list_ids):
                    batch.keys.update(keys)
                    batch.list_ids.update(list_ids)
                    batch.version += 1
                    batch.retry = 0
            schedule = not self._running and self._batch.count == 0
        if schedule:
            self._schedule(self.debounce)

    def _schedule(self, seconds, batch=None):
        with self._lock:
            if self._closed or self._batch is None or (batch is not None and batch is not self._batch):
                return
            self._sequence += 1
            sequence = self._sequence
            if self._timer:
                self._timer.cancel()
            self._timer = (self.timer_factory or threading.Timer)(seconds, lambda: self._fire(sequence))
            self._timer.daemon = True
            timer = self._timer
        timer.start()

    @staticmethod
    def _snapshot(batch, status, delay=None, error=None):
        value = {'job_id':batch.job_id, 'history_id':batch.history_id, 'created_at':batch.created_at,
                 'status':status, 'operation':'library_sync', 'dry_run':False, 'results':copy.deepcopy(batch.results),
                 'attempt_count':batch.count, 'retry_count':batch.retry, 'attempts':copy.deepcopy(batch.attempts)}
        if delay is not None:
            value['next_retry_at'] = (datetime.now(timezone.utc)+timedelta(seconds=delay)).isoformat()
        if error:
            value['error'] = error
        if status not in {'running','waiting'}:
            value['finished_at'] = datetime.now(timezone.utc).isoformat()
        return value

    def _fire(self, sequence):
        with self._lock:
            if self._closed or sequence != self._sequence or self._running or self._batch is None:
                return
            self._timer = None
            self._running = True
            batch = self._batch
            batch.started = True
            keys, ids, version = set(batch.keys), set(batch.list_ids), batch.version
            self.record(self._snapshot(batch, 'running'))
        try:
            outcome = self.attempt(keys, ids, batch.count == 0)
        except Exception:
            outcome = {'results':[{'success':False,'status':'error','error':'媒体库同步失败','error_code':'server_error'}], 'retry':True}
        with self._lock:
            self._running = False
            if self._closed or self._batch is not batch:
                return
            if outcome is None:
                delay, status = 15, 'waiting'
            else:
                batch.count += 1
                batch.results = outcome.get('results', [])
                batch.attempts.append({'attempt':batch.count,'retry':batch.retry,
                    'pending':sum(len(row.get('library_pending', [])) for row in batch.results)})
                batch.attempts = batch.attempts[-8:]
                if outcome.get('cancelled'):
                    delay, status = None, 'cancelled'
                elif version != batch.version:
                    delay, status = self.debounce, 'waiting'
                elif outcome.get('retry') and batch.retry < len(self.delays):
                    delay, status = self.delays[batch.retry], 'waiting'
                    batch.retry += 1
                else:
                    delay = None
                    successful = sum(bool(row.get('success')) for row in batch.results)
                    failed = len(batch.results) - successful
                    status = 'partial' if successful and failed else 'error' if failed else 'success'
            error = '媒体库同步未完成，自动重试已用尽。' if outcome and outcome.get('retry') and delay is None and status in {'partial', 'error'} else None
            snapshot = self._snapshot(batch, status, delay, error)
            if delay is None:
                self._batch = None
            self.record(snapshot)
        if delay is not None:
            self._schedule(delay, batch)

    def stop(self):
        with self._lock:
            self._closed = True
            self._sequence += 1
            if self._timer:
                self._timer.cancel()
                self._timer = None
            batch, self._batch = self._batch, None
        if batch and batch.started:
            self.record(self._snapshot(batch, 'cancelled'))
