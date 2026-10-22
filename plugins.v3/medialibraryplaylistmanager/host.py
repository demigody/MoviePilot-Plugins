"""V3 host capabilities; no torrent search, download or organizer implementation."""
from __future__ import annotations

import asyncio
from functools import lru_cache
from datetime import datetime
from typing import Any


@lru_cache(maxsize=1)
def _native_search_chain(chain_type: type) -> Any:
    """保留MoviePilot完整搜索流程，只将提供方调用切换为严格错误传播。"""
    class StrictSearchChain(chain_type):
        def search_medias(self, meta, media_source=None):
            """提供方失败须反馈错误，不能被MoviePilot普通入口折叠为空结果。"""
            medias = self.run_module_strict("search_medias", meta=meta, media_source=media_source)
            if medias is None:
                raise RuntimeError("MoviePilot搜索提供方没有返回结果")
            return medias

    return StrictSearchChain()


def object_dict(value: Any) -> dict:
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        return value.model_dump()
    return vars(value) if hasattr(value, "__dict__") else {}


def normalize_type(value: Any) -> str:
    text = str(getattr(value, "value", value) or "").casefold()
    if text in {"movie", "电影"}:
        return "movie"
    if text in {"tv", "series", "电视剧"}:
        return "tv"
    raise ValueError("仅支持电影和电视剧")


def media_record(value: Any, media_type: str | None = None) -> dict:
    raw = object_dict(value)
    kind = normalize_type(raw.get("media_type", raw.get("type", media_type)))
    tmdb_id = int(raw.get("tmdb_id") or raw.get("id") or 0)
    if tmdb_id <= 0:
        raise ValueError("缺少 TMDB ID")
    title = str(raw.get("title") or raw.get("name") or raw.get("en_title") or "").strip()
    year = raw.get("year") or str(raw.get("release_date") or raw.get("first_air_date") or "")[:4]
    poster = raw.get("poster") or raw.get("poster_path") or None
    if poster and str(poster).startswith("/"):
        poster = "https://image.tmdb.org/t/p/w300" + poster
    return {"tmdb_id": tmdb_id, "media_type": kind, "title": title,
            "year": int(year) if str(year).isdigit() else None, "poster": poster}


class Host:
    # Keep foreign-ID conversion concurrent enough for cold multi-provider
    # searches while bounding TMDB/provider traffic per search request.
    _search_identity_concurrency = 12

    @staticmethod
    def search(query: str, media_type: str) -> list[dict]:
        """跟随 MP 搜索来源，使用MoviePilot身份转换合并作品并保留未关联结果。"""
        from app.chain.media import MediaChain
        from app.schemas.types import MediaSource
        chain = _native_search_chain(MediaChain)
        _, medias = chain.search(title=query, media_source=None)
        order = str(getattr(getattr(chain, "runtime_config", None), "search_source", "") or "").split(",")
        rank = {source: index for index, source in enumerate(order)}
        raws = [object_dict(media) for media in medias or []]
        raws.sort(key=lambda raw: rank.get(str(raw.get("media_source") or "themoviedb"), len(rank)))
        candidates, native_seen = [], set()
        for raw in raws:
            try:
                kind = normalize_type(raw.get("media_type") or raw.get("type"))
                source = str(getattr(raw.get("media_source"), "value", raw.get("media_source")) or
                    ("themoviedb" if raw.get("tmdb_id") else ""))
                if not source:
                    continue
                source_id = str(raw.get("media_id") or raw.get(source + "_id") or
                    (raw.get("tmdb_id") or raw.get("id") if source == "themoviedb" else "") or "").strip()
                native_key = (kind, source, source_id)
                if not source_id or native_key in native_seen:
                    continue
                native_seen.add(native_key)
                if media_type != "all" and kind != media_type:
                    continue
                title = str(raw.get("title") or raw.get("name") or "").strip()
                if not title:
                    continue
                year = raw.get("year")
                item = {"media_source": source, "media_id": source_id, "media_type": kind, "title": title,
                    "year": int(year) if str(year).isdigit() else None, "poster": raw.get("poster") or raw.get("poster_path")}
                candidates.append((raw, source, source_id, native_key, kind, item))
            except (TypeError, ValueError):
                continue

        async def map_candidates():
            semaphore = asyncio.Semaphore(Host._search_identity_concurrency)

            async def map_candidate(candidate):
                raw, source, source_id, native_key, kind, item = candidate
                try:
                    # Native IDs remain paired with their source; only a host conversion
                    # to TMDB may interpret a foreign result's numeric `id` as TMDB.
                    if raw.get("tmdb_id") or source == "themoviedb":
                        mapped = media_record({**raw, "tmdb_id": raw.get("tmdb_id") or source_id}, kind)
                    else:
                        async with semaphore:
                            async_converter = getattr(chain, "async_convert_media_identity", None)
                            if callable(async_converter):
                                converted = await async_converter(target_source=MediaSource.TMDB,
                                    media_source=MediaSource(source), media_id=source_id)
                            else:
                                converted = await asyncio.to_thread(chain.convert_media_identity,
                                    target_source=MediaSource.TMDB, media_source=MediaSource(source), media_id=source_id)
                        if not converted:
                            raise ValueError("没有确认的对应关系")
                        converted = object_dict(converted)
                        converted_source = converted.get("media_source")
                        if converted_source and str(getattr(converted_source, "value", converted_source)) != MediaSource.TMDB.value:
                            raise ValueError("转换返回了其他来源身份")
                        mapped = media_record(converted, kind)
                    item.update({"tmdb_id": mapped["tmdb_id"], "media_type": mapped["media_type"],
                        "poster": item.get("poster") or mapped.get("poster"), "year": item.get("year") or mapped.get("year")})
                except Exception:
                    item["mapping_error"] = "MoviePilot暂未确认对应的 TMDB 媒体，暂不可加入片单。"
                item["source_refs"] = [{"media_source": source, "media_id": source_id}]
                return item, native_key

            return await asyncio.gather(*(map_candidate(candidate) for candidate in candidates))

        mapped_candidates = asyncio.run(map_candidates())
        result, identities = [], {}
        for item, native_key in mapped_candidates:
            key = (item["media_type"], "themoviedb", str(item["tmdb_id"])) if item.get("tmdb_id") else native_key
            existing = identities.get(key)
            if existing is None:
                identities[key] = item
                result.append(item)
            else:
                existing["source_refs"].extend(ref for ref in item["source_refs"] if ref not in existing["source_refs"])
                if any("\u3400" <= char <= "\u9fff" for char in item["title"]) and not any("\u3400" <= char <= "\u9fff" for char in existing["title"]):
                    existing["title"] = item["title"]
        return result

    @staticmethod
    def resolve(tmdb_id: int, media_type: str) -> dict:
        from app.chain.media import MediaChain
        from app.schemas.types import MediaSource, MediaType
        media = MediaChain().recognize_media(mtype=MediaType.TV if media_type == "tv" else MediaType.MOVIE,
                                              media_source=MediaSource.TMDB, media_id=str(tmdb_id))
        if media is None:
            raise ValueError("TMDB ID 未找到")
        record = media_record(media, media_type)
        if record["media_type"] != media_type or record["tmdb_id"] != tmdb_id or not record["title"]:
            raise ValueError("媒体解析结果身份或片名无效")
        return record

    @staticmethod
    def collection(tmdb_id: int) -> list[dict]:
        from app.chain.tmdb import TmdbChain
        chain = TmdbChain()
        strict = getattr(chain, "tmdb_collection_strict", None)
        medias = strict(tmdb_id) if callable(strict) else chain.tmdb_collection(tmdb_id)
        if medias is None or (not callable(strict) and not medias):
            # The host's public chain collapses provider failures to []. Do
            # not persist an apparent successful import from that ambiguity.
            raise ValueError("MoviePilot未返回可确认的 TMDB 合集数据")
        records = [media_record(media, "movie") for media in medias]
        if any(record["media_type"] != "movie" for record in records):
            raise ValueError("TMDB 合集包含无效的非电影身份")
        return records

    @staticmethod
    def subscribe(member: dict) -> tuple[Any, Any]:
        from app.chain.subscribe import SubscribeChain
        from app.schemas.types import MediaSource, MediaType
        options = {"season": member["season"]} if "season" in member else {}
        return SubscribeChain().add(title=member["title"], year=str(member.get("year") or ""),
                                    mtype=MediaType.TV if member["media_type"] == "tv" else MediaType.MOVIE,
                                    exist_ok=True, media_source=MediaSource.TMDB, media_id=str(member["tmdb_id"]), **options)

    @staticmethod
    def seasons(tmdb_id: int) -> list[dict]:
        from app.chain.tmdb import TmdbChain
        values = TmdbChain().run_module_strict("tmdb_seasons", tmdbid=tmdb_id)
        if not isinstance(values, list) or not values:
            raise ValueError("未取得这部电视剧的季信息，请稍后重试")
        result, seen = [], set()
        for value in values:
            raw = object_dict(value)
            number, count = raw.get("season_number"), raw.get("episode_count")
            if (isinstance(number, bool) or not isinstance(number, int) or number < 0 or number in seen
                    or isinstance(count, bool) or not isinstance(count, int) or count < 0):
                raise ValueError("电视剧季信息不完整，请稍后重试")
            seen.add(number)
            result.append({"season_number": number, "episode_count": count, "name": str(raw.get("name") or "")})
        return sorted(result, key=lambda item: item["season_number"])

    @staticmethod
    def subscription_exists(subscription_id: int, member: dict) -> bool:
        """以MoviePilot当前订阅及媒体身份校验缓存，避免删除或 ID 复用造成永久跳过。"""
        from app.sdk.queries import get_subscription
        from app.schemas.types import MediaSource
        subscription = get_subscription(int(subscription_id))
        if subscription is None:
            return False
        source = getattr(subscription, "media_source", None)
        return (str(getattr(source, "value", source)).casefold() == MediaSource.TMDB.value.casefold()
            and str(subscription.media_id) == str(member["tmdb_id"])
            and normalize_type(subscription.type) == member["media_type"]
            and ("season" not in member or getattr(subscription, "season", None) == member["season"]))

    @staticmethod
    def download_state(member: dict, submitted_at: str | None = None) -> str | None:
        """判断媒体是否已进入下载链路但尚未重新出现在媒体库。

        返回 ``downloading`` 表示下载任务仍在运行，返回 ``downloaded``
        表示已经进入下载历史或订阅完成历史，等待整理/扫描入库。

        MP 在下载任务提交后会把普通订阅转入完成历史并删除当前订阅行。
        因此这里只查当前订阅会把“下载中”误判成“未订阅”。下载器任务、
        下载历史和本次提交之后的订阅完成历史都属于只读查询；任一处能按
        TMDB 身份命中，就保留插件的提交声明，等待媒体库扫描完成。
        """
        tmdb_id = str(member.get("tmdb_id") or "")
        media_type = str(member.get("media_type") or "").casefold()
        season = member.get("season")
        if not tmdb_id or media_type not in {"movie", "tv"}:
            return None

        def matches(media: Any, *, history: bool = False) -> bool:
            raw = object_dict(media)
            source = raw.get("media_source")
            source = str(getattr(source, "value", source) or "").casefold()
            if source and source != "tmdb":
                return False
            if str(raw.get("media_id") or "") != tmdb_id:
                return False
            kind = str(raw.get("type") or raw.get("media_type") or "").casefold()
            if kind and normalize_type(kind) != media_type:
                return False
            if media_type != "tv" or season is None:
                return True
            value = raw.get("season") if not history else raw.get("seasons")
            if value is None:
                return False
            values = value if isinstance(value, (list, tuple, set)) else str(value).replace(",", " ").split()
            return str(season) in {str(item).lstrip("S").lstrip("s") for item in values}

        try:
            from app.chain.download import DownloadChain
            if any(matches(getattr(torrent, "media", None)) for torrent in DownloadChain().downloading()):
                return "downloading"
        except Exception:
            # A downloader may be temporarily unavailable. The caller still
            # applies its short claim window, so a transient query failure is
            # safe and must not turn the whole publish job into an error.
            pass

        try:
            from app.sdk.queries import list_download_history
            from app.schemas.types import MediaSource
            page = list_download_history(
                {"media_source": MediaSource.TMDB, "media_id": tmdb_id},
                {"page": 1, "count": 100},
            )
            items = getattr(page, "items", None)
            if items is None and isinstance(page, dict):
                items = page.get("items") or []
            for item in items or []:
                raw = object_dict(item)
                if submitted_at:
                    if not raw.get("date"):
                        continue
                    try:
                        created = datetime.fromisoformat(str(raw["date"]).replace("Z", "+00:00"))
                        submitted = datetime.fromisoformat(str(submitted_at).replace("Z", "+00:00"))
                        if created < submitted:
                            continue
                    except (TypeError, ValueError):
                        continue
                if matches(raw, history=True):
                    return "downloaded"
        except Exception:
            pass

        try:
            from app.sdk.queries import list_subscription_history
            from app.schemas.types import MediaSource
            filters = {"media_source": MediaSource.TMDB, "media_id": tmdb_id}
            if media_type == "tv" and season is not None:
                filters["season"] = int(season)
            page = list_subscription_history(filters, {"page": 1, "count": 100})
            items = getattr(page, "items", None)
            if items is None and isinstance(page, dict):
                items = page.get("items") or []
            for item in items or []:
                raw = object_dict(item)
                if submitted_at:
                    if not raw.get("date"):
                        continue
                    try:
                        created = datetime.fromisoformat(str(raw["date"]).replace("Z", "+00:00"))
                        submitted = datetime.fromisoformat(str(submitted_at).replace("Z", "+00:00"))
                        if created < submitted:
                            continue
                    except (TypeError, ValueError):
                        continue
                source = raw.get("media_source")
                source = str(getattr(source, "value", source) or "").casefold()
                if source and source != "tmdb":
                    continue
                if str(raw.get("media_id") or "") != tmdb_id:
                    continue
                kind = str(raw.get("type") or raw.get("media_type") or "").casefold()
                if kind and normalize_type(kind) != media_type:
                    continue
                if media_type == "tv" and season is not None and raw.get("season") != int(season):
                    continue
                return "downloaded"
        except Exception:
            pass
        return None

    @staticmethod
    def download_pending(member: dict, submitted_at: str | None = None) -> bool:
        """兼容旧MoviePilot调用方的布尔状态接口。"""
        return bool(Host.download_state(member, submitted_at))

    @staticmethod
    def servers() -> dict[str, dict]:
        from app.sdk.services import MediaServerHelper
        helper = MediaServerHelper()
        configs = helper.get_configs(include_disabled=True)
        services = {}
        for kind in ("emby", "jellyfin"):
            services.update(helper.get_services(type_filter=kind) or {})
        result = {}
        for name, value in (configs or {}).items():
            raw = object_dict(value)
            kind = str(getattr(raw.get("type"), "value", raw.get("type")) or "").casefold()
            conf = dict(raw.get("config") or {})
            conf.update({"name": str(raw.get("name") or name), "kind": kind, "enabled": raw.get("enabled", True)})
            service = services.get(name)
            instance = getattr(service, "instance", None)
            if instance:
                discovered = getattr(instance, "user", None) or getattr(instance, "_user", None)
                if kind == "jellyfin":
                    # MP may select SUPERUSER here. A user token must instead
                    # use its own authenticated owner, not this fallback.
                    conf["default_user_id"] = discovered
                else:
                    conf["user_id"] = conf.get("user_id") or discovered
            result[str(name)] = conf
        return result
