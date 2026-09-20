"""TMDB 自动订阅渠道（对接 MoviePilot 平台推荐功能）。"""
from __future__ import annotations

from typing import Iterator, Optional

from app.chain.recommend import RecommendChain
from app.log import logger
from app.schemas.types import MediaType

from ...core.subscribe import MediaCandidate, SubscribeContext, SubscribeProvider
from ...core.subscribe.provider import ranking_scan_limit
from ...core.subscribe.registry import register

TMDB_METHODS: dict[str, tuple[str, str, str]] = {
    "trending": ("tmdb_trending", "TMDB流行趋势", "movie"),
    "movies": ("tmdb_movies", "TMDB热门电影", "movie"),
    "tvs": ("tmdb_tvs", "TMDB热门电视剧", "tv"),
}


@register
class TmdbSubscribeProvider(SubscribeProvider):
    provider_id = "tmdb"
    provider_name = "TMDB榜单"

    def __init__(self, chain: Optional[RecommendChain] = None) -> None:
        self._chain = chain or RecommendChain()

    def spec(self) -> dict:
        return {
            "id": self.provider_id,
            "name": self.provider_name,
            "default_cron": "0 8 * * *",
        }

    def has_listening(self, options: dict) -> bool:
        return bool(options.get("ranks"))

    def fetch(self, options: dict, context: SubscribeContext) -> Iterator[MediaCandidate]:
        ranks = list(options.get("ranks") or ["trending", "movies", "tvs"])
        if not ranks:
            return

        min_vote = float(options.get("min_vote") or 0.0)
        min_year = int(options.get("min_year") or 0)
        limit = int(options.get("limit") or 20)
        scan_limit = ranking_scan_limit(options)
        limit_type = str(options.get("media_type") or "").strip().lower()

        logger.info(f"开始抓取TMDB榜单：选中榜单={ranks}")
        seen_keys: set[str] = set()

        for rank_key in ranks:
            if context.stopped():
                return
            if rank_key not in TMDB_METHODS:
                logger.warning(f"未知TMDB榜单键：{rank_key}")
                continue

            method_name, rank_name, default_type = TMDB_METHODS[rank_key]
            method = getattr(self._chain, method_name, None)
            if not method:
                logger.warning(f"MoviePilot平台推荐链中不存在方法：RecommendChain.{method_name}")
                continue

            try:
                items = []
                page = 1
                seen_pages = set()
                while len(items) < scan_limit:
                    if context.stopped():
                        return
                    try:
                        page_items = method(page=page) or []
                    except Exception as error:
                        if not items:
                            raise
                        logger.warning(f"TMDB榜单[{rank_name}]第 {page} 页抓取失败，保留已抓取内容：{error}")
                        break
                    if not page_items:
                        break
                    page_keys = tuple(
                        str(getattr(item, "tmdb_id", None) or getattr(item, "title", None) or item)
                        for item in page_items
                    )
                    if page_keys in seen_pages:
                        break
                    seen_pages.add(page_keys)
                    items.extend(page_items)
                    page += 1
                items = items[:scan_limit]
            except Exception as error:
                logger.error(f"抓取TMDB榜单[{rank_name}]失败：{error}")
                continue

            logger.info(f"TMDB榜单[{rank_name}]抓取成功：返回 {len(items)} 条数据")

            for item in items:
                if context.stopped():
                    return
                data = item.to_dict() if hasattr(item, "to_dict") else dict(item or {})
                title = str(data.get("title") or data.get("name") or "").strip()
                if not title:
                    continue

                raw_type = str(data.get("type") or default_type or "").strip().lower()
                movie_types = {"movie", "电影", "film"}
                tv_types = {"tv", "电视剧", "剧集", "teleplay", "series"}
                if hasattr(MediaType, "MOVIE"):
                    movie_types.add(str(getattr(MediaType.MOVIE, "value", "")).lower())
                if hasattr(MediaType, "TV"):
                    tv_types.add(str(getattr(MediaType.TV, "value", "")).lower())

                mtype = "movie" if raw_type in movie_types else "tv" if raw_type in tv_types else default_type
                if limit_type and limit_type not in ("all", "全部", "any") and limit_type != mtype:
                    continue

                year = str(data.get("year") or "").strip()
                if min_year and year:
                    try:
                        if int(year) < min_year:
                            continue
                    except ValueError:
                        pass

                vote = data.get("vote_average") or data.get("vote") or 0.0
                try:
                    vote_float = float(vote)
                    if min_vote and vote_float < min_vote:
                        continue
                except (TypeError, ValueError):
                    vote_float = 0.0

                tmdb_id = data.get("tmdb_id")
                try:
                    tmdb_id = int(tmdb_id) if tmdb_id else None
                except (TypeError, ValueError):
                    tmdb_id = None

                unique_key = f"{tmdb_id or title}:{year}"
                if unique_key in seen_keys:
                    continue
                seen_keys.add(unique_key)

                yield MediaCandidate(
                    title=title,
                    year=year or None,
                    media_type=mtype,
                    tmdb_id=tmdb_id,
                    source=self.provider_id,
                    source_meta={
                        "rank": rank_name,
                        "rank_key": rank_key,
                        "release_date": data.get("release_date"),
                        "first_air_date": data.get("first_air_date"),
                    },
                    vote_average=vote_float,
                    unique_seed=unique_key,
                )


def create_tmdb_provider() -> TmdbSubscribeProvider:
    return TmdbSubscribeProvider()
