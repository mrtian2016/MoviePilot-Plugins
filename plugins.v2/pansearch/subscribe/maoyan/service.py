"""猫眼榜单数据解析。"""
import re
from datetime import date, timedelta
from typing import Iterator

from ...core.subscribe import MediaCandidate


class MaoyanService:
    @staticmethod
    def _year(release_info) -> str | None:
        digits = "".join(re.findall(r"\d", str(release_info or "")))
        if not digits:
            return None
        try:
            return str((date.today() - timedelta(days=int(digits))).year)
        except (OverflowError, TypeError, ValueError):
            return None

    def movie_box(self, payload: dict, limit: int) -> Iterator[MediaCandidate]:
        rows = ((payload or {}).get("movieList") or {}).get("list") or []
        for row in rows[:limit]:
            info = row.get("movieInfo") or {}
            title = str(info.get("movieName") or "").strip()
            if title:
                year = self._year(info.get("releaseInfo"))
                yield MediaCandidate(
                    title=title, year=year, media_type="movie", source="maoyan",
                    source_meta=info, unique_seed=f"movie:{title}:{year or ''}",
                )

    def web_heat(self, payload: dict, limit: int, platform: str) -> Iterator[MediaCandidate]:
        rows = ((payload or {}).get("dataList") or {}).get("list") or []
        for row in rows[:limit]:
            info = row.get("seriesInfo") or {}
            title = str(info.get("name") or "").strip()
            if title:
                year = self._year(info.get("releaseInfo"))
                yield MediaCandidate(
                    title=title, year=year, media_type="tv", season=1, source="maoyan",
                    source_meta={**info, "platform": platform},
                    unique_seed=f"tv:{title}:{year or ''}",
                )
