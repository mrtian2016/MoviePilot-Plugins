"""AniList 自动订阅渠道（对接 MoviePilot 平台 AniList 功能）。"""
from __future__ import annotations

from typing import Iterator, Optional

from app.chain.anilist import AniListChain
from app.log import logger

from ...core.subscribe import MediaCandidate, SubscribeContext, SubscribeProvider
from ...core.subscribe.provider import ranking_scan_limit
from ...core.subscribe.registry import register

ANILIST_METHODS: dict[str, tuple[str, str]] = {
    "popular": ("popular_this_season", "AniList本季热门"),
    "popular_this_season": ("popular_this_season", "AniList本季热门"),
    "trending": ("trending", "AniList流行趋势"),
}


@register
class AnilistSubscribeProvider(SubscribeProvider):
    provider_id = "anilist"
    provider_name = "AniList榜单"

    def __init__(self, chain: Optional[AniListChain] = None) -> None:
        self._chain = chain or AniListChain()

    def spec(self) -> dict:
        return {
            "id": self.provider_id,
            "name": self.provider_name,
            "default_cron": "0 8 * * *",
        }

    def has_listening(self, options: dict) -> bool:
        return bool(options.get("ranks"))

    def fetch(self, options: dict, context: SubscribeContext) -> Iterator[MediaCandidate]:
        ranks = list(options.get("ranks") or ["popular_this_season", "trending"])
        min_vote = float(options.get("min_vote") or 0.0)
        min_year = int(options.get("min_year") or 0)
        limit = int(options.get("limit") or 30)
        scan_limit = ranking_scan_limit(options)

        logger.info(f"开始抓取AniList榜单：选中榜单={ranks}")
        seen_keys: set[str] = set()

        for rank_key in ranks:
            if context.stopped():
                return
            if rank_key not in ANILIST_METHODS:
                continue

            method_name, rank_name = ANILIST_METHODS[rank_key]
            method = getattr(self._chain, method_name, None)
            if not method:
                logger.warning(f"MoviePilot平台链中不存在方法：AniListChain.{method_name}")
                continue

            try:
                items = []
                page = 1
                seen_pages = set()
                while len(items) < scan_limit:
                    if context.stopped():
                        return
                    try:
                        page_items = method(page=page, count=limit) or []
                    except Exception as error:
                        if not items:
                            raise
                        logger.warning(f"AniList榜单[{rank_name}]第 {page} 页抓取失败，保留已抓取内容：{error}")
                        break
                    if not page_items:
                        break
                    page_keys = tuple(
                        str(getattr(item, "anilist_id", None) or getattr(item, "title", None) or item)
                        for item in page_items
                    )
                    if page_keys in seen_pages:
                        break
                    seen_pages.add(page_keys)
                    items.extend(page_items)
                    page += 1
                items = items[:scan_limit]
            except Exception as error:
                logger.error(f"抓取AniList榜单[{rank_name}]失败：{error}")
                continue

            logger.info(f"AniList榜单[{rank_name}]抓取成功：返回 {len(items)} 条数据")

            for item in items:
                if context.stopped():
                    return
                data = item.to_dict() if hasattr(item, "to_dict") else dict(item or {})
                title = str(data.get("title") or data.get("name") or "").strip()
                if not title:
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

                anilist_id = data.get("anilist_id")
                try:
                    anilist_id = int(anilist_id) if anilist_id else None
                except (TypeError, ValueError):
                    anilist_id = None

                tmdb_id = data.get("tmdb_id")
                try:
                    tmdb_id = int(tmdb_id) if tmdb_id else None
                except (TypeError, ValueError):
                    tmdb_id = None

                unique_key = f"{anilist_id or tmdb_id or title}:{year}"
                if unique_key in seen_keys:
                    continue
                seen_keys.add(unique_key)

                yield MediaCandidate(
                    title=title,
                    year=year or None,
                    media_type="tv",
                    tmdb_id=tmdb_id,
                    source=self.provider_id,
                    source_meta={
                        "rank": rank_name,
                        "rank_key": rank_key,
                        "anilist_id": anilist_id,
                        "release_date": data.get("release_date"),
                        "first_air_date": data.get("first_air_date"),
                    },
                    vote_average=vote_float,
                    unique_seed=unique_key,
                )


def create_anilist_provider() -> AnilistSubscribeProvider:
    return AnilistSubscribeProvider()
