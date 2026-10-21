"""Bounded, cancellable calls into host providers that may retry externally.

Threads are daemonized and slots stay occupied until the provider really exits.
A caller timeout never creates another queued worker behind a stuck provider.
Directory reads share in-flight results and use at most sixteen bounded
coordination tasks; the same provider slots still limit actual external calls.
"""
from __future__ import annotations

import threading
import time
import math
from dataclasses import dataclass, field
from typing import Any, Callable
from uuid import uuid4
from .jellyfin import JellyfinAuthError


@dataclass
class _SharedRead:
    request_id: str = field(default_factory=lambda: uuid4().hex)
    done: threading.Event = field(default_factory=threading.Event)
    cancelled: threading.Event = field(default_factory=threading.Event)
    waiters: int = 0
    result: dict[str, Any] = field(default_factory=dict)


class ProviderCalls:
    def __init__(self, *, timeout: float = 5, cooldown: float = 15, capacity: int = 2, slots=None):
        self.timeout = timeout
        self.cooldown = cooldown
        self._slots = slots if slots is not None else threading.BoundedSemaphore(capacity)
        self._lock = threading.Lock()
        self._active: dict[str, threading.Event] = {}
        self._shared: dict[str, _SharedRead] = {}
        self._cooldowns: dict[str, float] = {}
        self._stopped = threading.Event()

    def cancel(self, request_id: str) -> bool:
        with self._lock:
            event = self._active.get(request_id)
            if event:
                event.set()
            return event is not None

    def stop(self):
        self._stopped.set()
        with self._lock:
            for event in self._active.values():
                event.set()

    def is_active(self, request_id: str) -> bool:
        with self._lock:
            event = self._active.get(request_id)
            return event is not None and not event.is_set() and not self._stopped.is_set()

    def call_shared(self, key: str, fn: Callable[[], Any], *, request_id: str = "", provider: str = "metadata", timeout: float | None = None) -> dict[str, Any]:
        """合并同一目录的在途读取；不同目录在原有并发上限内有界等待。"""
        request_id = request_id or uuid4().hex
        deadline = time.monotonic() + (self.timeout if timeout is None else timeout)
        cancelled = threading.Event()
        with self._lock:
            if self._stopped.is_set():
                return self._result("cancelled", request_id, error="请求已取消")
            if request_id in self._active:
                return self._result("busy", request_id, error="请求正在进行")
            job = self._shared.get(key)
            leader = job is None
            if leader:
                if len(self._shared) >= 16:
                    return self._result("busy", request_id, error="媒体库读取任务较多，请稍后重试")
                job = _SharedRead()
                self._shared[key] = job
            job.waiters += 1
            self._active[request_id] = cancelled

        def run_shared():
            try:
                while True:
                    if self._stopped.is_set() or job.cancelled.is_set():
                        result = self._result("cancelled", job.request_id, error="请求已取消")
                        break
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        result = self._result("timeout", job.request_id, error="媒体库读取超时，请稍后重试")
                        break
                    result = self.call(fn, request_id=job.request_id, provider=provider, timeout=remaining)
                    if result["status"] != "busy":
                        break
                    # Waiting never launches another worker or increases the slot cap.
                    job.cancelled.wait(min(.05, remaining))
                job.result = result
            except Exception:
                job.result = self._result("error", job.request_id, error="媒体库读取失败，请重试", error_code="provider_error")
            finally:
                with self._lock:
                    job.done.set()
                    if self._shared.get(key) is job:
                        self._shared.pop(key)

        try:
            if leader:
                try:
                    threading.Thread(target=run_shared, daemon=True, name="playlist-directory-read").start()
                except Exception:
                    with self._lock:
                        job.result = self._result("error", job.request_id, error="媒体库读取失败，请重试", error_code="provider_error")
                        job.done.set()
                        self._shared.pop(key, None)
            while not job.done.wait(min(.05, max(0, deadline - time.monotonic()))):
                if cancelled.is_set() or self._stopped.is_set():
                    return self._result("cancelled", request_id, error="请求已取消")
                if time.monotonic() >= deadline:
                    return self._result("timeout", request_id, error="媒体库读取超时，请稍后重试")
            if cancelled.is_set() or self._stopped.is_set():
                return self._result("cancelled", request_id, error="请求已取消")
            return {**job.result, "request_id": request_id}
        finally:
            with self._lock:
                self._active.pop(request_id, None)
                job.waiters -= 1
                if not job.waiters and not job.done.is_set():
                    job.cancelled.set()
                    active = self._active.get(job.request_id)
                    if active:
                        active.set()
                    if self._shared.get(key) is job:
                        self._shared.pop(key)

    def call(self, fn: Callable[[], Any], *, request_id: str = "", provider: str = "metadata", timeout: float | None = None) -> dict[str, Any]:
        request_id = request_id or uuid4().hex
        if self._stopped.is_set():
            return self._result("cancelled", request_id, error="插件已停止")
        with self._lock:
            remaining = self._cooldowns.get(provider, 0) - time.monotonic()
            if remaining > 0:
                retry_after = max(1, math.ceil(remaining))
                service = "订阅请求" if provider == "subscribe" else "外部服务请求"
                return self._result("cooldown", request_id,
                    error=f"上一次{service}未完成，插件暂停重试；请等待约 {retry_after} 秒后再试。",
                    retry_after=retry_after)
            if request_id in self._active:
                return self._result("busy", request_id, error="请求正在进行")
            if not self._slots.acquire(blocking=False):
                return self._result("busy", request_id, error="服务正在处理其他请求，请稍后重试")
            cancelled = threading.Event()
            self._active[request_id] = cancelled
        done = threading.Event()
        output: dict[str, Any] = {}

        def run():
            try:
                output["value"] = fn()
            except Exception as error:
                # Do not return exception strings containing configured URLs/tokens.
                output["error"] = str(error) if isinstance(error, JellyfinAuthError) else "外部服务请求失败，请检查连接和设置"
                allowed = {"unsupported", "timeout", "server_error", "invalid_input", "provider_error", "missing_output", "permission_denied"}
                output["error_code"] = error.code if isinstance(error, JellyfinAuthError) else getattr(error, "code", None) if getattr(error, "code", None) in allowed else "provider_error"
            finally:
                self._slots.release()
                done.set()

        threading.Thread(target=run, daemon=True, name="playlist-provider").start()
        deadline = time.monotonic() + (self.timeout if timeout is None else timeout)
        try:
            while not done.wait(min(0.05, max(0, deadline - time.monotonic()))):
                if cancelled.is_set() or self._stopped.is_set():
                    return self._result("cancelled", request_id, error="请求已取消")
                if time.monotonic() >= deadline:
                    with self._lock:
                        self._cooldowns[provider] = time.monotonic() + self.cooldown
                    return self._result("timeout", request_id, error="请求超时，请稍后重试")
            if cancelled.is_set() or self._stopped.is_set():
                return self._result("cancelled", request_id, error="请求已取消")
            if "error" in output:
                with self._lock:
                    self._cooldowns[provider] = time.monotonic() + self.cooldown
                return self._result("error", request_id, error=output["error"], error_code=output["error_code"])
            return self._result("success", request_id, value=output.get("value"))
        finally:
            with self._lock:
                self._active.pop(request_id, None)

    @staticmethod
    def _result(status: str, request_id: str, **values) -> dict[str, Any]:
        return {"success": status in {"success", "empty"}, "status": status, "request_id": request_id, **values}
