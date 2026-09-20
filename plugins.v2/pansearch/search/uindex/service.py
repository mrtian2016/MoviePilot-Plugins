"""UIndex 搜索服务实现。"""

from typing import Any, Dict, List, Optional

from app.schemas.types import MediaType

from .client import UIndexClient, UIndexError
from ..magnet import clear_cache, media_titles, normalize_magnets
from ..matching import extract_season, extract_year, unique_texts
from ...core.search import SearchQuery


class UIndexSearchService:
    def __init__(self, client: UIndexClient, result_limit: int = 20):
        self._client = client
        self._result_limit = result_limit

    @staticmethod
    def _keywords(mediainfo: Any, media_type: MediaType, season: Optional[int]) -> List[str]:
        titles = media_titles(mediainfo)
        year = extract_year(getattr(mediainfo, "year", None))
        keywords = []
        for t in titles:
            if media_type == MediaType.TV and season:
                keywords.append(f"{t} S{season:02d}")
                keywords.append(f"{t} 第{season}季")
            elif year:
                keywords.append(f"{t} {year}")
            keywords.append(t)
        return unique_texts(keywords)

    def search(self, query: SearchQuery) -> List[Dict[str, Any]]:
        mediainfo = query.mediainfo
        if not mediainfo:
            return []

        keywords = self._keywords(mediainfo, query.media_type, query.season)
        if not keywords:
            return []

        limit = query.result_limit or self._result_limit
        collected = []
        seen_hashes = set()

        for kw in keywords[:3]:
            try:
                entries = self._client.search(kw)
            except UIndexError:
                continue

            for item in entries:
                h = item.get("info_hash")
                if not h or h in seen_hashes:
                    continue

                # 季号匹配
                if query.media_type == MediaType.TV and query.season:
                    cand_season = extract_season(item.get("title"))
                    if cand_season is not None and cand_season != query.season:
                        continue

                # 电影年份检查
                if query.media_type == MediaType.MOVIE:
                    expected_year = extract_year(getattr(mediainfo, "year", None))
                    cand_year = extract_year(item.get("title"))
                    if expected_year and cand_year and cand_year != expected_year:
                        continue

                seen_hashes.add(h)
                collected.append(item)
                if len(collected) >= limit:
                    break

            if len(collected) >= limit:
                break

        return normalize_magnets(collected, "uindex")

    def clear_cache(self) -> int:
        return clear_cache(self._client)
