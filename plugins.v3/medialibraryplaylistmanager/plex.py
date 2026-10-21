"""Plex PMS adapter. Canonical list identities remain typed TMDB works.

Wire contracts and known server limitations: docs/MEDIA_SERVER_PLEX_RESEARCH.md.
Plex ratingKey identifies media; playlistItemID identifies a particular occurrence.
"""
from __future__ import annotations

import inspect
import hashlib
import json
import re
import time
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .emby import EmbyAPIError, EmbyClient, EmbyEpisode, EmbyItem, EmbySeason
from .models import positive_id, text


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward a user's token to a redirect destination.
        return None


def _id(value: Any) -> str:
    try:
        return str(positive_id(value))
    except ValueError as error:
        raise EmbyAPIError('Plex 返回了无效的媒体或条目 ID') from error


def _number(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise EmbyAPIError('Plex 返回的数量、季号或集号无效')
    return value


def _tmdb(row: dict) -> int | None:
    guids = row.get('Guid', [])
    if not isinstance(guids, list) or any(not isinstance(guid, dict) for guid in guids):
        raise EmbyAPIError('Plex 返回的媒体标识无效')
    values = set()
    for raw in [guid.get('id') for guid in guids] + [row.get('guid')]:
        if raw is None:
            continue
        value = text(raw)
        match = re.fullmatch(r'(?:tmdb|com\.plexapp\.agents\.themoviedb)://([1-9][0-9]*)(?:\?[^#]*)?', value)
        if match:
            values.add(int(match[1]))
        elif value.startswith(('tmdb://', 'com.plexapp.agents.themoviedb://')):
            raise EmbyAPIError('Plex 返回了无效的 TMDB 标识')
    if len(values) > 1:
        raise EmbyAPIError('Plex 同一条媒体存在冲突的 TMDB 标识')
    return next(iter(values), None)


class PlexClient:
    kind = 'plex'
    capabilities = {'supported': True, 'kinds': ['plex'], 'media_types': ['movie', 'tv'],
        'collections': True, 'playlists': True, 'reorder': True, 'movie_playlist': True,
        'tv_playlist': True, 'playlist_media_types': ['movie', 'tv', 'episode'],
        'playlist_grouping': 'member', 'mixed_collection': False, 'season_collection': False,
        'cross_library_collection': False, 'collection_batch_size': 100, 'removal_batch_size': 1}

    def __init__(self, base_url: str, api_key: str, user_id: str = '', transport=None,
                 timeout: float = 10, *, page_size: int = 200, scan_timeout: float = 90, max_pages: int = 1000):
        url = text(base_url).rstrip('/')
        parsed = urlsplit(url)
        if parsed.scheme not in {'http', 'https'} or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError('Plex 地址必须为不含凭据和查询参数的 HTTP(S) 地址')
        self.base_url, self.api_key, self.transport = url + '/', text(api_key), transport
        self.timeout = min(max(float(timeout), .1), 30)
        self.page_size = min(max(int(page_size), 1), 1000)
        self.scan_timeout = min(max(float(scan_timeout), .1), 300)
        self.max_pages = min(max(int(max_pages), 1), 5000)
        self._metadata: dict[str, dict] = {}
        self._machine = None
        self._publication_fingerprint = None

    def _request(self, method, path, params=None, *, timeout=None, with_headers=False):
        params = {key: value for key, value in (params or {}).items() if value is not None}
        headers = {'Accept': 'application/json', 'X-Plex-Token': self.api_key,
                   'X-Plex-Client-Identifier': 'moviepilot-media-list-sync', 'X-Plex-Product': 'MoviePilot Media Lists'}
        timeout = min(self.timeout, timeout) if timeout is not None else self.timeout
        if timeout <= 0:
            raise EmbyAPIError('Plex 请求超时，请稍后重试', code='timeout')
        try:
            if self.transport is not None:
                operation = getattr(self.transport, 'request', self.transport)
                kwargs = {'params': params, 'json': None, 'headers': headers, 'timeout': timeout}
                signature = inspect.signature(operation)
                if not any(p.kind == inspect.Parameter.VAR_KEYWORD for p in signature.parameters.values()):
                    kwargs = {key: value for key, value in kwargs.items() if key in signature.parameters}
                signature.bind(method, path, **kwargs)
                response = operation(method, path, **kwargs)
                response_headers = {str(key).lower(): value for key, value in getattr(response, 'headers', {}).items()}
                decoded = EmbyClient._decode_response(response)
                return (decoded, response_headers) if with_headers else decoded
            url = self.base_url + path
            if params:
                url += '?' + urlencode(params)
            request = Request(url, headers=headers, method=method)
            with build_opener(_NoRedirect()).open(request, timeout=timeout) as response:
                response_headers = {str(key).lower(): value for key, value in response.headers.items()}
                decoded = EmbyClient._decode_response(response.read())
                return (decoded, response_headers) if with_headers else decoded
        except (HTTPError, EmbyAPIError) as error:
            status = error.code if isinstance(error, HTTPError) else error.status
            if status == 401:
                raise EmbyAPIError('Plex 令牌无效或已失效，请检查 MoviePilot 中的 Plex 配置', code='authorization_credentials_invalid', status=status) from error
            if status == 403:
                raise EmbyAPIError('Plex 拒绝操作，请确认 MP 中的令牌所属用户有权管理此媒体库或播放列表', code='authorization_denied', status=status) from error
            if status == 404:
                raise EmbyAPIError('Plex 对象不存在或当前用户无权访问；请检查 MP 的地址、令牌和远端对象', code='object_unavailable', status=status) from error
            if isinstance(error, EmbyAPIError):
                raise
            raise EmbyAPIError(f'Plex 返回 HTTP {status}，请检查服务器地址和操作权限', status=status) from error
        except (TimeoutError, URLError) as error:
            timeout_error = isinstance(error, TimeoutError) or isinstance(getattr(error, 'reason', None), TimeoutError)
            raise EmbyAPIError('Plex 请求超时' if timeout_error else 'Plex 连接失败，请检查 MP 中的服务器地址和网络', code='timeout' if timeout_error else 'server_error') from error
        except Exception as error:
            raise EmbyAPIError('Plex 请求失败，请检查服务器连接和返回数据') from error

    @staticmethod
    def _container(raw):
        if not isinstance(raw, dict) or not isinstance(raw.get('MediaContainer'), dict):
            raise EmbyAPIError('Plex 返回数据不完整')
        return raw['MediaContainer']

    def _pages(self, path, params=None, *, tag='Metadata', entry=False):
        result, seen, total = [], set(), None
        deadline = time.monotonic() + self.scan_timeout
        for _ in range(self.max_pages):
            raw, response_headers = self._request('GET', path, {**(params or {}),
                'X-Plex-Container-Start': len(result), 'X-Plex-Container-Size': self.page_size}, timeout=deadline - time.monotonic(), with_headers=True)
            page = self._container(raw)
            size = _number(page.get('size'))
            rows = page.get(tag, []) if size == 0 else page.get(tag)
            if not isinstance(rows, list) or len(rows) != size or any(not isinstance(row, dict) for row in rows):
                raise EmbyAPIError('Plex 分页内容与数量不一致')
            def paging_value(body_key, header_key):
                body = _number(page[body_key]) if body_key in page else None
                header = response_headers.get(header_key)
                if header is not None:
                    if not re.fullmatch(r'[0-9]+', str(header)):
                        raise EmbyAPIError('Plex 分页响应头无效')
                    header = int(header)
                if body is not None and header is not None and body != header:
                    raise EmbyAPIError('Plex 分页响应头与内容不一致')
                return body if body is not None else header
            offset = paging_value('offset', 'x-plex-container-start')
            count = paging_value('totalSize', 'x-plex-container-total-size')
            # PMS explicitly permits endpoints to ignore paging and return a
            # complete size-only container. Never infer completeness from a
            # short page when the response does declare pagination.
            unpaged = offset is None and count is None
            if (unpaged and result) or (offset is not None and offset != len(result)):
                raise EmbyAPIError('Plex 分页位置不连续')
            if total is None and count is not None:
                total = count
            if (total is not None and (count != total or len(result) + size > total or (not size and len(result) < total))):
                raise EmbyAPIError('Plex 分页总数变化或读取不完整')
            for row in rows:
                identity = _id(row.get('playlistItemID') if entry else row.get('key') if tag == 'Directory' else row.get('ratingKey'))
                if identity in seen:
                    raise EmbyAPIError('Plex 返回了重复的分页条目')
                seen.add(identity)
            result.extend(rows)
            if unpaged or len(result) == total or (total is None and not size):
                return result
        raise EmbyAPIError('Plex 分页超过读取上限', code='timeout')

    def _detail(self, key, *, playlist=False):
        key = _id(key)
        container = self._container(self._request('GET', f'playlists/{key}' if playlist else f'library/metadata/{key}', {'includeGuids': 1}))
        rows = container.get('Metadata')
        if container.get('size') != 1 or not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict) or _id(rows[0].get('ratingKey')) != key:
            raise EmbyAPIError('Plex 对象详情的身份不完整')
        return rows[0]

    def _output(self, key, kind):
        row = self._detail(key, playlist=kind == 'playlist')
        if row.get('type') != kind:
            raise EmbyAPIError('Plex 绑定对象类型与设置不一致，请检查对应的合集或播放列表', code='unsupported')
        # PMS omits smart on ordinary collections; PlexAPI also defaults it to 0.
        if row.get('smart', 0) not in (False, 0, '0'):
            raise EmbyAPIError('Plex 智能合集和智能播放列表由筛选规则管理，不支持同步片单内容', code='unsupported')
        if kind == 'playlist' and row.get('playlistType') != 'video':
            raise EmbyAPIError('Plex 绑定的播放列表不是视频列表', code='unsupported')
        if kind == 'collection' and (row.get('subtype') not in ('movie', 'show') or not row.get('librarySectionID')):
            raise EmbyAPIError('Plex 合集的类型或所属媒体库不明确', code='unsupported')
        return row

    def _item(self, row, *, entry=False):
        key = _id(row.get('ratingKey'))
        kind = row.get('type')
        if kind not in ('movie', 'show', 'episode', 'season', 'collection', 'playlist'):
            raise EmbyAPIError('Plex 返回了不支持的媒体类型', code='unsupported')
        item = EmbyItem(key, text(row.get('title')), row.get('year'), None,
                        _tmdb(row) if kind in ('movie', 'show') else None,
                        _id(row.get('playlistItemID')) if entry else None,
                        {'movie': 'movie', 'show': 'tv', 'episode': 'episode', 'season': 'season'}.get(kind))
        self._metadata[key] = row
        if kind in ('episode', 'season'):
            parent = _id(row.get('grandparentRatingKey') if kind == 'episode' else row.get('parentRatingKey'))
            parent_row = self._metadata.get(parent)
            if parent_row is None:
                parent_row = self._detail(parent)
                self._metadata[parent] = parent_row
            if parent_row.get('type') != 'show':
                raise EmbyAPIError('Plex 季或集的所属电视剧身份不符')
            if kind == 'season':
                return EmbySeason(**vars(item), series_id=parent, series_tmdb_id=_tmdb(parent_row),
                                  season_number=_number(row.get('index')), series_name=text(parent_row.get('title')))
            media = row.get('Media')
            if not isinstance(media, list) or not any(isinstance(m, dict) and isinstance(m.get('Part'), list) and any(isinstance(p, dict) and text(p.get('file')) for p in m['Part']) for m in media):
                raise EmbyAPIError('Plex 剧集缺少已入库文件信息')
            return EmbyEpisode(**vars(item), series_id=parent, series_tmdb_id=_tmdb(parent_row),
                               season_number=_number(row.get('parentIndex')), episode_number=_number(row.get('index')))
        return item

    def list_libraries(self):
        return [{'id': _id(row.get('key')), 'name': text(row.get('title')) or f"媒体库 {row.get('key')}",
                 'media_type': 'movie' if row['type'] == 'movie' else 'tv'}
                for row in self._pages('library/sections', tag='Directory') if row.get('type') in ('movie', 'show')]

    def list_media(self, library_ids: Iterable[str] | None = None, media_type='all'):
        if media_type not in ('all', 'movie', 'tv'):
            raise ValueError('请选择全部、电影或电视剧')
        self._metadata.clear()
        libraries = self.list_libraries()
        selected = {_id(value) for value in library_ids or []}
        if selected - {library['id'] for library in libraries}:
            raise EmbyAPIError('Plex 所选媒体库不存在或当前用户无权访问，请刷新媒体库后检查范围')
        result = []
        for library in libraries:
            if (selected and library['id'] not in selected) or media_type not in ('all', library['media_type']):
                continue
            for row in self._pages(f"library/sections/{library['id']}/all", {'type': 1 if library['media_type'] == 'movie' else 2, 'includeGuids': 1}):
                if row.get('type') != ('movie' if library['media_type'] == 'movie' else 'show'):
                    raise EmbyAPIError('Plex 媒体类型与所属媒体库不符')
                if row.get('librarySectionID') is not None and _id(row['librarySectionID']) != library['id']:
                    raise EmbyAPIError('Plex 媒体的所属媒体库不一致')
                result.append(self._item({**row, 'librarySectionID': library['id']}))
        return result

    def get_series_episodes(self, series):
        rows = [self._item(row) for row in self._pages(f'library/metadata/{_id(series.id)}/allLeaves', {'includeGuids': 1})]
        if any(not isinstance(row, EmbyEpisode) or row.series_id != series.id or row.parent_key != series.key for row in rows):
            raise EmbyAPIError('Plex 返回的剧集不属于选定电视剧')
        return sorted(rows, key=lambda row: (row.season_number, row.episode_number, row.id))

    def get_series_seasons(self, series):
        rows = [self._item(row) for row in self._pages(f'library/metadata/{_id(series.id)}/children', {'excludeAllLeaves': 1, 'includeGuids': 1})]
        if any(not isinstance(row, EmbySeason) or row.series_id != series.id or row.parent_key != series.key for row in rows):
            raise EmbyAPIError('Plex 返回的季不属于选定电视剧')
        return sorted(rows, key=lambda row: (row.season_number, row.id))

    def list_collections(self):
        result = []
        for library in self.list_libraries():
            for row in self._pages(f"library/sections/{library['id']}/collections"):
                if row.get('type') != 'collection':
                    raise EmbyAPIError('Plex 合集列表返回了其他类型')
                result.append(self._item(row))
        return result

    def list_playlists(self):
        rows = self._pages('playlists', {'type': 15, 'playlistType': 'video'})
        if any(row.get('type') != 'playlist' or row.get('playlistType') != 'video' for row in rows):
            raise EmbyAPIError('Plex 播放列表读取不完整')
        return [self._item(row) for row in rows]

    def get_collection_items(self, collection_id):
        self._output(collection_id, 'collection')
        return [self._item(row) for row in self._pages(f'library/metadata/{_id(collection_id)}/children', {'includeGuids': 1})]

    def get_playlist_items(self, playlist_id):
        self._output(playlist_id, 'playlist')
        return [self._item(row, entry=True) for row in self._pages(f'playlists/{_id(playlist_id)}/items', {'includeGuids': 1}, entry=True)]

    def _collection_scope(self, item_ids, collection_id=None):
        scope = set()
        for key in dict.fromkeys(_id(key) for key in item_ids):
            row = self._metadata.get(key)
            if not row or not row.get('librarySectionID'):
                row = self._detail(key)
                self._metadata[key] = row
            if row.get('type') not in ('movie', 'show'):
                raise EmbyAPIError('Plex 合集暂不支持指定季，请关闭合集同步并使用播放列表', code='unsupported')
            scope.add((_id(row.get('librarySectionID')), row['type']))
        if collection_id:
            row = self._output(collection_id, 'collection')
            scope.add((_id(row.get('librarySectionID')), row['subtype']))
        if len(scope) > 1:
            raise EmbyAPIError('Plex 合集只能包含同一媒体库中的同类作品；请关闭合集同步，播放列表可照常使用', code='unsupported')
        return next(iter(scope), None)

    def validate_collection(self, item_ids, collection_id=None):
        self._collection_scope(item_ids, collection_id)

    def _uri(self, item_ids):
        ids = list(dict.fromkeys(_id(key) for key in item_ids))
        if not ids:
            raise EmbyAPIError('Plex 新建或添加内容需要至少一项已入库媒体')
        if self._machine is None:
            machine = text(self._container(self._request('GET', 'identity')).get('machineIdentifier'))
            if not re.fullmatch(r'[A-Za-z0-9_-]+', machine):
                raise EmbyAPIError('Plex 未返回有效服务器标识，请检查 MP 中的服务器地址')
            self._machine = machine
        return f"server://{self._machine}/com.plexapp.plugins.library/library/metadata/{','.join(ids)}"

    def connection_fingerprint(self):
        machine = text(self._container(self._request('GET', 'identity')).get('machineIdentifier'))
        if not re.fullmatch(r'[A-Za-z0-9_-]+', machine):
            raise EmbyAPIError('Plex 未返回有效服务器标识，请检查 MP 中的服务器地址')
        self._machine = machine
        return hashlib.sha256(json.dumps([self.base_url, machine, self.api_key], ensure_ascii=True).encode()).hexdigest()

    def validate_target(self, target):
        current = self.connection_fingerprint()
        bound = target.collection.remote_id or any(playlist.remote_id for playlist in target.playlists)
        if bound and target.connection_fingerprint != current:
            raise EmbyAPIError('Plex 连接或令牌已变化，或旧绑定未经验证；请移除此同步设置后重新添加', code='connection_changed')
        self._publication_fingerprint = current

    def bind_target(self, target):
        if not self._publication_fingerprint:
            raise EmbyAPIError('Plex 连接尚未验证，绑定未保存')
        target.connection_fingerprint = self._publication_fingerprint

    def _created(self, raw, kind):
        rows = self._container(raw).get('Metadata')
        if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict) or rows[0].get('type') != kind:
            raise EmbyAPIError('Plex 新建操作未返回有效的绑定 ID，请检查远端结果后重试')
        return _id(rows[0].get('ratingKey'))

    def create_collection(self, name, item_ids=()):
        ids = list(item_ids)
        scope = self._collection_scope(ids)
        if not scope:
            raise EmbyAPIError('Plex 不创建空合集')
        section, subtype = scope
        return self._created(self._request('POST', 'library/collections', {'title': name, 'sectionId': section,
            'type': 1 if subtype == 'movie' else 2, 'smart': 0, 'uri': self._uri(ids)}), 'collection')

    def add_collection_items(self, collection_id, item_ids):
        ids = list(item_ids)
        self._collection_scope(ids, collection_id)
        self._request('PUT', f'library/collections/{_id(collection_id)}/items', {'uri': self._uri(ids)})

    def remove_collection_items(self, collection_id, item_ids):
        self._output(collection_id, 'collection')
        for key in dict.fromkeys(_id(key) for key in item_ids):
            self._request('DELETE', f'library/collections/{_id(collection_id)}/items/{key}')

    def create_playlist(self, name, item_ids=()):
        return self._created(self._request('POST', 'playlists', {'title': name, 'type': 'video', 'smart': 0, 'uri': self._uri(item_ids)}), 'playlist')

    def add_playlist_items(self, playlist_id, item_ids):
        self._output(playlist_id, 'playlist')
        self._request('PUT', f'playlists/{_id(playlist_id)}/items', {'uri': self._uri(item_ids)})

    def remove_playlist_entries(self, playlist_id, entry_ids):
        self._output(playlist_id, 'playlist')
        for entry in dict.fromkeys(_id(key) for key in entry_ids):
            self._request('DELETE', f'playlists/{_id(playlist_id)}/items/{entry}')

    def move_playlist_entry(self, playlist_id, entry_id, new_index):
        rows = self.get_playlist_items(playlist_id)
        entry_id = _id(entry_id)
        if isinstance(new_index, bool) or not isinstance(new_index, int) or not 0 <= new_index < len(rows) or entry_id not in {row.playlist_item_id for row in rows}:
            raise EmbyAPIError('Plex 播放列表位置或条目已变化，请重新同步')
        remaining = [row for row in rows if row.playlist_item_id != entry_id]
        params = {'after': remaining[new_index - 1].playlist_item_id} if new_index else {}
        self._request('PUT', f'playlists/{_id(playlist_id)}/items/{entry_id}/move', params)

    def ensure_playlist_access(self):
        # The MP token already identifies the user. Actual reads/writes verify access.
        pass

    def check_playlist_access(self):
        pass
