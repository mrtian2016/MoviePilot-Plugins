"""Bangumi 自动订阅渠道（对接 MoviePilot 平台推荐功能）。"""
from __future__ import annotations

from typing import Iterator, Optional

from app.chain.recommend import RecommendChain
from app.log import logger

from ...core.subscribe import MediaCandidate, SubscribeContext, SubscribeProvider
from ...core.subscribe.provider import ranking_scan_limit
from ...core.subscribe.registry import register


@register
class BangumiSubscribeProvider(SubscribeProvider):
    provider_id = "bangumi"
    provider_name = "Bangumi榜单"

    def __init__(self, chain: Optional[RecommendChain] = None) -> None:
        self._chain = chain or RecommendChain()

    def spec(self) -> dict:
        return {
            "id": self.provider_id,
            "name": self.provider_name,
            "default_cron": "0 8 * * *",
        }

    def has_listening(self, options: dict) -> bool:
        return bool(options.get("enabled", True))

    def fetch(self, options: dict, context: SubscribeContext) -> Iterator[MediaCandidate]:
        min_vote = float(options.get("min_vote") or 0.0)
        min_year = int(options.get("min_year") or 0)
        limit = int(options.get("limit") or 50)
        scan_limit = ranking_scan_limit(options)

        logger.info("开始抓取Bangumi每日放送...")
        method = getattr(self._chain, "bangumi_calendar", None)
        if not method:
            logger.warning("MoviePilot平台推荐链中不存在方法：RecommendChain.bangumi_calendar")
            return

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
                    logger.warning(f"Bangumi每日放送第 {page} 页抓取失败，保留已抓取内容：{error}")
                    break
                if not page_items:
                    break
                page_keys = tuple(
                    str(getattr(item, "bangumi_id", None) or getattr(item, "title", None) or item)
                    for item in page_items
                )
                if page_keys in seen_pages:
                    break
                seen_pages.add(page_keys)
                items.extend(page_items)
                page += 1
            items = items[:scan_limit]
        except Exception as error:
            logger.error(f"抓取Bangumi每日放送失败：{error}")
            return

        logger.info(f"Bangumi每日放送抓取成功：返回 {len(items)} 条数据")
        seen_keys: set[str] = set()

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

            bangumi_id = data.get("bangumi_id")
            try:
                bangumi_id = int(bangumi_id) if bangumi_id else None
            except (TypeError, ValueError):
                bangumi_id = None

            unique_key = f"{bangumi_id or title}:{year}"
            if unique_key in seen_keys:
                continue
            seen_keys.add(unique_key)

            yield MediaCandidate(
                title=title,
                year=year or None,
                media_type="tv",
                bangumi_id=bangumi_id,
                source=self.provider_id,
                source_meta={
                    "rank": "每日放送",
                    "release_date": data.get("release_date"),
                    "first_air_date": data.get("first_air_date"),
                    "air_date": data.get("air_date"),
                },
                vote_average=vote_float,
                unique_seed=unique_key,
            )


def create_bangumi_provider() -> BangumiSubscribeProvider:
    return BangumiSubscribeProvider()
