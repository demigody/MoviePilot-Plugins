"""Backend-only Jellyfin user sessions obtained with MP's application API key.

Official Quick Connect: Enabled -> Initiate -> Authorize(userId, code) ->
Users/AuthenticateWithQuickConnect(secret). No account password is needed.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import threading
import time
from uuid import uuid4

from .emby import EmbyAPIError, _value
from .jellyfin import JellyfinAuthError, same_user


class JellyfinSessions:
    def __init__(self, load, save, *, cancelled=lambda: False, timeout=45):
        self._load, self._save = load, save
        self._cancelled = cancelled
        self._timeout = timeout
        self._closed = threading.Event()
        self._lock = threading.Lock()
        self._entries = None
        self._verified = set()

    def stop(self):
        # Never wait for a network worker while the host holds its reload lock.
        self._closed.set()

    def _guard(self):
        if self._closed.is_set() or self._cancelled():
            raise JellyfinAuthError("Jellyfin 授权已取消，请在插件重新加载后重试。", code="cancelled")

    @staticmethod
    def _scope(server_id, client):
        values = [str(server_id), client.base_url, client.api_key, client.user_id.replace('-', '').casefold()]
        return hashlib.sha256(json.dumps(values, ensure_ascii=True).encode()).hexdigest()

    def _read(self):
        if self._entries is not None:
            return
        try:
            stored = self._load()
        except Exception as error:
            raise JellyfinAuthError("Jellyfin 授权缓存读取失败，请检查 MoviePilot 的插件数据存储。", code="authorization_storage_error") from error
        entries = stored.get('entries') if isinstance(stored, dict) and stored.get('version') == 1 else None
        self._entries = copy.deepcopy(entries) if isinstance(entries, dict) else {}

    def _commit(self, scope, entry):
        self._guard()
        entries = {key: value for key, value in self._entries.items() if key != scope}
        entries[scope] = entry
        entries = dict(list(entries.items())[-32:])
        try:
            if self._save({'version': 1, 'entries': copy.deepcopy(entries)}) is False:
                raise RuntimeError('存储未接受本次保存')
        except JellyfinAuthError:
            raise
        except Exception as error:
            raise JellyfinAuthError("Jellyfin 用户授权未能保存，请检查 MoviePilot 的插件数据存储。", code="authorization_storage_error") from error
        self._entries = entries

    def _step(self, client, method, path, *, stage, deadline, params=None, body=None):
        self._guard()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise JellyfinAuthError("Jellyfin 自动授权超时，请检查服务器连接后重试。", code="timeout")
        try:
            result = client._request(method, path, params, body, timeout=remaining)
        except EmbyAPIError as error:
            status = error.status
            if error.code == 'timeout':
                message, code = "Jellyfin 自动授权超时，请检查服务器连接后重试。", 'timeout'
            elif status == 401:
                message, code = "MoviePilot 中的 Jellyfin 访问凭据无效或已撤销，请检查 API 密钥。", 'authorization_credentials_invalid'
                if stage in {'initiate', 'authorize', 'exchange'}:
                    message, code = "Jellyfin 拒绝了 Quick Connect 授权（HTTP 401）；请检查该功能是否被关闭，以及 MP 的 API 密钥是否仍然有效。", 'quick_connect_disabled'
            elif status == 403:
                message, code = "Jellyfin 拒绝为所选用户授权，请检查 MP 使用的是后台创建的 API 密钥，并确认该用户可用。", 'authorization_denied'
            elif status == 404:
                message, code = (("MP 配置的 Jellyfin 用户不存在，请检查媒体服务器用户设置。", 'authorization_user_missing')
                    if stage == 'user' else ("Jellyfin 未提供所需的 Quick Connect 接口，请检查服务器版本或反向代理路径。", 'authorization_unsupported'))
                if stage in {'authorize', 'exchange'}:
                    message, code = "Jellyfin Quick Connect 授权请求已失效，请重新同步。", 'authorization_expired'
            elif status == 400:
                message, code = "Jellyfin 未接受自动授权请求，请检查服务器版本和 Quick Connect 接口兼容性。", 'authorization_invalid_response'
            elif status and status >= 500:
                message, code = "Jellyfin 自动授权时服务器发生错误，请检查服务器日志后重试。", 'server_error'
            else:
                message, code = "无法连接 Jellyfin 完成自动授权，请检查 MP 配置的地址、网络和反向代理。", 'server_error'
            raise JellyfinAuthError(message, code=code, status=status) from error
        self._guard()
        return result

    def _profile(self, client, deadline):
        raw = self._step(client, 'GET', f'Users/{client._id(client.user_id)}', stage='user', deadline=deadline)
        if not isinstance(raw, dict) or not same_user(str(_value(raw, 'Id') or ''), client.user_id):
            raise JellyfinAuthError("Jellyfin 返回的用户身份与 MP 配置不一致，请检查媒体服务器用户设置。", code="authorization_user_mismatch")
        if _value(_value(raw, 'Policy', default={}), 'IsDisabled', default=False):
            raise JellyfinAuthError("MP 配置的 Jellyfin 用户已被停用，请启用该用户或更改媒体服务器用户设置。", code="authorization_user_disabled")

    def _available(self, client, deadline):
        self._profile(client, deadline)
        enabled = self._step(client, 'GET', 'QuickConnect/Enabled', stage='enabled', deadline=deadline)
        if enabled is False:
            raise JellyfinAuthError("Jellyfin 未开启 Quick Connect，无法自动获取播放列表用户令牌。请在 Jellyfin 控制台开启 Quick Connect 后重新同步。", code="quick_connect_disabled")
        if enabled is not True:
            raise JellyfinAuthError("Jellyfin 返回的 Quick Connect 状态无效，请检查服务器版本或代理配置。", code="authorization_invalid_response")

    def _token_user(self, client, deadline, *, cached=False):
        self._guard()
        remaining = deadline-time.monotonic()
        if remaining <= 0:
            raise JellyfinAuthError("Jellyfin 自动授权超时，请检查服务器连接后重试。", code="timeout")
        try:
            raw = client._request('GET', 'Users/Me', timeout=remaining)
        except EmbyAPIError as error:
            if error.status == 400 or (cached and error.status == 401):
                return None
            # Reuse the sanitized status mapping without repeating a request.
            if error.status == 401:
                raise JellyfinAuthError("MoviePilot 中的 Jellyfin 访问凭据无效或已撤销，请检查 API 密钥。", code="authorization_credentials_invalid", status=401) from error
            raise JellyfinAuthError("Jellyfin 用户身份检查失败，请检查服务器连接和用户状态。", code='timeout' if error.code == 'timeout' else 'server_error', status=error.status) from error
        self._guard()
        user = str(_value(raw, 'Id') or '')
        if not same_user(user, client.user_id):
            raise JellyfinAuthError("Jellyfin 用户令牌与 MP 配置的用户不一致，请检查媒体服务器用户设置。", code="authorization_user_mismatch")
        return client.api_key

    def check(self, server_id, client):
        """GET-only preview. Do not create, persist, or renew user sessions."""
        self._guard()
        deadline = time.monotonic() + self._timeout
        while not self._lock.acquire(timeout=0.05):
            self._guard()
            if time.monotonic() >= deadline:
                raise JellyfinAuthError("Jellyfin 授权正在处理，请稍后重试。", code="busy")
        try:
            self._read()
            entry = self._entries.get(self._scope(server_id, client))
            token = entry.get('token') if isinstance(entry, dict) else None
            if isinstance(token, str) and token and self._token_user(client.with_token(token), deadline, cached=True):
                return
        finally:
            self._lock.release()
        if self._token_user(client, deadline):
            return
        self._available(client, deadline)

    def get(self, server_id, client, *, rejected_token=None):
        deadline = time.monotonic() + self._timeout
        while not self._lock.acquire(timeout=0.05):
            self._guard()
            if time.monotonic() >= deadline:
                raise JellyfinAuthError("Jellyfin 授权正在处理，请稍后重试。", code="busy")
        try:
            self._guard()
            self._read()
            scope = self._scope(server_id, client)
            entry = self._entries.get(scope)
            entry = dict(entry) if isinstance(entry, dict) else {}
            token = entry.get('token')
            if isinstance(token, str) and token and token != rejected_token:
                if (scope, token) in self._verified or self._token_user(client.with_token(token), deadline, cached=True):
                    self._verified.add((scope, token))
                    return token
            self._verified = {item for item in self._verified if item[0] != scope}
            if token == rejected_token and rejected_token == client.api_key:
                raise JellyfinAuthError("Jellyfin 用户令牌已失效；MP 当前填入的是用户令牌，请改用后台 API 密钥以便自动授权。", code="authorization_credentials_invalid")
            original = self._token_user(client, deadline)
            if original:
                return original
            self._available(client, deadline)
            device = entry.get('device_id')
            if not isinstance(device, str) or not re.fullmatch(r'[a-f0-9]{32}', device):
                device = uuid4().hex
            # Persist the device before login so interrupted attempts don't
            # create a new device identity on every future synchronization.
            self._commit(scope, {'device_id':device})
            grant = client.with_token(client.api_key)
            grant.device_id = device
            raw = self._step(grant, 'POST', 'QuickConnect/Initiate', stage='initiate', deadline=deadline)
            secret, code = _value(raw, 'Secret'), _value(raw, 'Code')
            if (not isinstance(secret, str) or not 1 <= len(secret) <= 512 or any(ord(c) < 32 for c in secret)
                    or not isinstance(code, str) or not re.fullmatch(r'[A-Za-z0-9]{6}', code)):
                raise JellyfinAuthError("Jellyfin 返回的 Quick Connect 授权信息无效。", code="authorization_invalid_response")
            authorized = self._step(grant, 'POST', 'QuickConnect/Authorize', stage='authorize', deadline=deadline,
                params={'Code':code, 'UserId':client.user_id})
            if authorized is not True:
                raise JellyfinAuthError("Jellyfin 未批准所选用户的 Quick Connect 授权，请检查用户状态后重试。", code="authorization_denied")
            result = self._step(grant, 'POST', 'Users/AuthenticateWithQuickConnect', stage='exchange', deadline=deadline,
                body={'Secret':secret})
            token = _value(result, 'AccessToken')
            owner = _value(_value(result, 'User', default={}), 'Id')
            if (not isinstance(token, str) or not token or len(token) > 4096
                    or not isinstance(owner, str) or not same_user(owner, client.user_id)):
                raise JellyfinAuthError("Jellyfin 未返回所选用户的有效登录令牌。", code="authorization_user_mismatch")
            if not self._token_user(client.with_token(token), deadline):
                raise JellyfinAuthError("Jellyfin 返回的令牌不包含所选用户身份。", code="authorization_invalid_response")
            self._guard()
            self._commit(scope, {'device_id':device, 'token':token})
            self._verified.add((scope, token))
            return token
        finally:
            self._lock.release()
