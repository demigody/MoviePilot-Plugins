import asyncio
import sys
from enum import Enum
from types import ModuleType, SimpleNamespace

import pytest

from app.plugins.embycollectionsync.host import Host


class MediaSource(str, Enum):
    TMDB = "themoviedb"
    Douban = "douban"
    Bangumi = "bangumi"


class MediaType(str, Enum):
    MOVIE = "电影"
    TV = "电视剧"


def test_host_discovers_emby_and_jellyfin_with_their_own_users(monkeypatch):
    for name in ('app', 'app.sdk'):
        package = ModuleType(name)
        package.__path__ = []
        monkeypatch.setitem(sys.modules, name, package)
    calls = []
    class Helper:
        def get_configs(self, include_disabled):
            assert include_disabled is True
            return {name:{'type':kind,'name':name,'config':{'host':'http://local','apikey':'private'}}
                for name,kind in [('客厅','emby'),('书房','jellyfin'),('其他','plex')]}
        def get_services(self, *, type_filter):
            calls.append(type_filter)
            return {'客厅' if type_filter=='emby' else '书房': SimpleNamespace(instance=SimpleNamespace(user=type_filter+'-user'))}
    services = ModuleType('app.sdk.services')
    services.MediaServerHelper = Helper
    monkeypatch.setitem(sys.modules,'app.sdk.services',services)
    result = Host.servers()
    assert calls == ['emby','jellyfin']
    assert result['客厅']['user_id'] == 'emby-user'
    assert result['书房']['kind'] == 'jellyfin' and result['书房']['default_user_id'] == 'jellyfin-user'
    assert 'user_id' not in result['书房']
    assert result['其他']['kind'] == 'plex' and 'user_id' not in result['其他']


def mock_search_host(monkeypatch, chain_type):
    for name in ("app", "app.chain", "app.schemas", "app.sdk"):
        package = ModuleType(name)
        package.__path__ = []
        monkeypatch.setitem(sys.modules, name, package)
    chain_module = ModuleType("app.chain.media")
    chain_module.MediaChain = chain_type
    types = ModuleType("app.schemas.types")
    types.MediaSource, types.MediaType = MediaSource, MediaType
    sdk_media = ModuleType("app.sdk.media")
    sdk_media.MetaInfo = lambda query: SimpleNamespace(name=query, type="未知")
    monkeypatch.setitem(sys.modules, "app.chain.media", chain_module)
    monkeypatch.setitem(sys.modules, "app.schemas.types", types)
    monkeypatch.setitem(sys.modules, "app.sdk.media", sdk_media)


def test_host_passes_explicit_season_to_native_subscription_and_checks_cache_season(monkeypatch):
    mock_search_host(monkeypatch, object)
    calls = []
    module = ModuleType('app.chain.subscribe')
    module.SubscribeChain = lambda: SimpleNamespace(add=lambda **kwargs: (calls.append(kwargs) or (10, None)))
    monkeypatch.setitem(sys.modules, 'app.chain.subscribe', module)
    assert Host.subscribe({'tmdb_id': 211089, 'media_type': 'tv', 'title': '剧', 'season': 4}) == (10, None)
    assert calls[0]['season'] == 4 and calls[0]['media_id'] == '211089'
    queries = ModuleType('app.sdk.queries')
    queries.get_subscription = lambda _: SimpleNamespace(media_source=MediaSource.TMDB, media_id='211089', type='电视剧', season=3)
    monkeypatch.setitem(sys.modules, 'app.sdk.queries', queries)
    assert not Host.subscription_exists(10, {'tmdb_id': 211089, 'media_type': 'tv', 'season': 4})
    assert Host.subscription_exists(10, {'tmdb_id': 211089, 'media_type': 'tv', 'season': 3})


def test_host_season_choices_use_moviepilot_metadata_chain(monkeypatch):
    mock_search_host(monkeypatch, object)
    calls = []
    module = ModuleType('app.chain.tmdb')
    def query(*args, **kwargs):
        calls.append((args, kwargs))
        return [SimpleNamespace(season_number=3, episode_count=24, name='三'), {'season_number': 1, 'episode_count': 20}]
    module.TmdbChain = lambda: SimpleNamespace(run_module_strict=query)
    monkeypatch.setitem(sys.modules, 'app.chain.tmdb', module)
    assert Host.seasons(211089) == [{'season_number':1, 'episode_count':20, 'name':''}, {'season_number':3, 'episode_count':24, 'name':'三'}]
    assert calls == [(('tmdb_seasons',), {'tmdbid':211089})]


@pytest.mark.parametrize("kind,ids", [("all", [634649, 961651, 10]), ("movie", [634649, 961651]), ("tv", [10])])
def test_search_uses_native_keyword_flow_then_filters_without_forcing_provider_type(monkeypatch, kind, ids):
    calls = []
    query = "英雄无归"
    meta = SimpleNamespace(name=query, type="未知")
    medias = [
        {"tmdb_id": 634649, "type": "电影", "title": "Spider-Man: No Way Home", "year": "2021"},
        {"tmdb_id": 961651, "type": "电影", "title": "幕后特辑"},
        {"tmdb_id": 10, "type": "电视剧", "title": "剧集"},
        {"id": 99, "type": "Person", "name": "人物"},
    ]

    class NativeChain:
        def search(self, *, title, media_source):
            calls.append(("native_search", title, media_source))
            return meta, self.search_medias(meta=meta, media_source=media_source)

        def run_module_strict(self, method, **kwargs):
            calls.append(("provider", method, kwargs))
            return medias

    mock_search_host(monkeypatch, NativeChain)
    result = Host.search(query, kind)
    assert [item["tmdb_id"] for item in result] == ids
    assert calls == [
        ("native_search", query, None),
        ("provider", "search_medias", {"meta": meta, "media_source": None}),
    ]
    assert meta.type == "未知"


@pytest.mark.parametrize("failure", [None, RuntimeError("private-provider-url")])
def test_native_search_keeps_strict_provider_failures_distinct_from_empty(monkeypatch, failure):
    class NativeChain:
        def search(self, **kwargs):
            return None, self.search_medias(meta=object(), media_source=kwargs["media_source"])

        def run_module_strict(self, *args, **kwargs):
            if isinstance(failure, Exception):
                raise failure
            return failure

    mock_search_host(monkeypatch, NativeChain)
    with pytest.raises(RuntimeError):
        Host.search("missing", "all")


def test_native_search_accepts_confirmed_empty_result(monkeypatch):
    class NativeChain:
        def search(self, **kwargs):
            return None, self.search_medias(meta=object(), media_source=kwargs["media_source"])

        def run_module_strict(self, *args, **kwargs):
            return []

    mock_search_host(monkeypatch, NativeChain)
    assert Host.search("missing", "all") == []


def test_search_follows_host_sources_converts_and_merges_same_work_without_dropping_unmapped(monkeypatch):
    conversions = []

    class NativeChain:
        runtime_config = SimpleNamespace(search_source="douban,themoviedb,bangumi")

        def search(self, **kwargs):
            assert kwargs == {"title": "英雄无归", "media_source": None}
            return None, [
                {"tmdb_id": 634649, "type": "电影", "title": "Spider-Man: No Way Home", "year": "2021"},
                {"media_source": "douban", "media_id": "26933210", "type": "电影", "title": "蜘蛛侠：英雄无归", "year": "2021"},
                {"media_source": "douban", "media_id": "37436600", "type": "电影", "title": "英雄无归", "year": "2025"},
                {"media_source": "bangumi", "media_id": "9", "type": "电视剧", "title": "其他电视剧"},
                {"media_source": "douban", "media_id": "37436600", "type": "电影", "title": "英雄无归", "year": "2025"},
            ]

        def convert_media_identity(self, **kwargs):
            conversions.append(kwargs)
            if kwargs["media_id"] == "26933210":
                return {"id": 634649, "media_type": "movie", "title": "Spider-Man: No Way Home", "release_date": "2021-12-15"}
            return None

        async def async_convert_media_identity(self, **kwargs):
            return self.convert_media_identity(**kwargs)

    mock_search_host(monkeypatch, NativeChain)
    result = Host.search("英雄无归", "all")
    assert len(result) == 3
    feature = next(item for item in result if item.get("tmdb_id") == 634649)
    assert feature["title"] == "蜘蛛侠：英雄无归"
    assert [(source["media_source"], source["media_id"]) for source in feature["source_refs"]] == [("douban", "26933210"), ("themoviedb", "634649")]
    missing = next(item for item in result if item.get("media_id") == "37436600")
    assert not missing.get("tmdb_id") and missing["mapping_error"]
    assert len(conversions) == 3
    assert all(call["target_source"] == MediaSource.TMDB for call in conversions)


def test_search_maps_many_foreign_candidates_concurrently(monkeypatch):
    active = 0
    peak = 0
    candidates = [
        {"media_source": "douban", "media_id": str(index), "type": "电影", "title": f"影片 {index}"}
        for index in range(1, 13)
    ]

    class NativeChain:
        def search(self, **kwargs):
            return None, candidates

        def run_module_strict(self, *args, **kwargs):
            return candidates

        def convert_media_identity(self, **kwargs):
            raise AssertionError("搜索映射应使用异步转换")

        async def async_convert_media_identity(self, **kwargs):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.01)
            active -= 1
            return {"id": int(kwargs["media_id"]) + 100, "type": "电影", "title": kwargs["media_id"]}

    mock_search_host(monkeypatch, NativeChain)
    result = Host.search("影片", "all")
    assert len(result) == len(candidates)
    assert {item["tmdb_id"] for item in result} == set(range(101, 113))
    assert 1 < peak <= 12


@pytest.mark.parametrize("conversion", [RuntimeError("private-provider-url"), {"media_source": "douban", "id": 10, "type": "movie", "title": "错误来源"}])
def test_search_never_uses_foreign_native_id_as_tmdb_id_or_merges_different_typed_ids(monkeypatch, conversion):
    class NativeChain:
        def search(self, **kwargs):
            return None, [
                {"tmdb_id": 10, "type": "电影", "title": "同名", "year": "2020"},
                {"tmdb_id": 10, "type": "电视剧", "title": "同名", "year": "2020"},
                {"tmdb_id": 11, "type": "电影", "title": "同名", "year": "2020"},
                {"media_source": "douban", "media_id": "10", "id": 10, "type": "电影", "title": "同名", "year": "2020"},
                {"id": 99, "type": "电影", "title": "缺少来源身份"},
            ]

        def convert_media_identity(self, **kwargs):
            if isinstance(conversion, Exception):
                raise conversion
            return conversion

        async def async_convert_media_identity(self, **kwargs):
            return self.convert_media_identity(**kwargs)

    mock_search_host(monkeypatch, NativeChain)
    result = Host.search("同名", "all")
    assert len(result) == 4
    foreign = next(item for item in result if item.get("media_source") == "douban")
    assert not foreign.get("tmdb_id")
    assert "private" not in str(result)


@pytest.mark.parametrize("source,media_id,kind,expected", [
    (MediaSource.TMDB, "1", "电影", True),
    ("themoviedb", "1", "电影", True),
    (MediaSource.Douban, "1", "电影", False),
    (MediaSource.TMDB, "2", "电影", False),
    (MediaSource.TMDB, "1", "电视剧", False),
])
def test_subscription_cache_checks_canonical_source_and_typed_identity(monkeypatch, source, media_id, kind, expected):
    # 替换查询边界，使用宿主实际的枚举值，避免真实数据库或网络访问。
    for name in ("app", "app.sdk", "app.schemas"):
        package = ModuleType(name)
        package.__path__ = []
        monkeypatch.setitem(sys.modules, name, package)
    queries = ModuleType("app.sdk.queries")
    queries.get_subscription = lambda sid: SimpleNamespace(media_source=source, media_id=media_id, type=kind)
    types = ModuleType("app.schemas.types")
    types.MediaSource = MediaSource
    monkeypatch.setitem(sys.modules, "app.sdk.queries", queries)
    monkeypatch.setitem(sys.modules, "app.schemas.types", types)
    assert Host.subscription_exists(10, {"tmdb_id": 1, "media_type": "movie"}) is expected
