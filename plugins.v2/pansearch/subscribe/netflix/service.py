"""Netflix Top10 表格解析。"""
import json
import re
from typing import Iterator

from ...core.subscribe import MediaCandidate


class NetflixService:
    SEASON = re.compile(r"Season\s*(\d+)", re.I)
    GRAPHQL_MARKER = "reactContext.models.graphql = JSON.parse('"
    JS_ESCAPE = re.compile(r"\\(u[0-9a-fA-F]{4}|x[0-9a-fA-F]{2}|.)", re.S)

    @staticmethod
    def latest_week(rows: list[dict]) -> list[dict]:
        weeks = [row.get("week") for row in rows if row.get("week")]
        if not weeks:
            return rows
        latest = max(weeks)
        return [row for row in rows if row.get("week") == latest]

    @staticmethod
    def rank(row: dict) -> int:
        try:
            return int(row.get("rank") or row.get("weekly_rank") or 10 ** 6)
        except (TypeError, ValueError):
            return 10 ** 6

    @classmethod
    def candidates(
            cls, rows: list[dict], category: str, scope: str, limit: int
    ) -> Iterator[MediaCandidate]:
        selected = sorted(
            (row for row in rows if row.get("category") == category),
            key=cls.rank,
        )[:limit]
        for row in selected:
            title = str(row.get("show_title") or "").strip()
            if not title:
                continue
            media_type = "movie" if category.startswith("Films") else "tv"
            season = None
            if media_type == "tv":
                match = cls.SEASON.search(str(row.get("season_title") or ""))
                season = int(match.group(1)) if match else 1
            yield MediaCandidate(
                title=title,
                media_type=media_type,
                season=season,
                source="netflix",
                source_meta={
                    "scope": scope,
                    "category": category,
                    "rank": cls.rank(row),
                    "week": row.get("week"),
                },
                unique_seed=f"{media_type}:{title}:{season or ''}",
            )

    @classmethod
    def rich_entries(cls, html: str) -> list[dict]:
        start = html.rfind(cls.GRAPHQL_MARKER)
        if start < 0:
            raise RuntimeError("Netflix 页面未找到内嵌 GraphQL 数据")
        index = start + len(cls.GRAPHQL_MARKER)
        end = index
        while end < len(html):
            if html[end] == "\\":
                end += 2
                continue
            if html[end] == "'":
                break
            end += 1
        if end >= len(html):
            raise RuntimeError("Netflix GraphQL 数据未正确闭合")
        raw = html[index:end]
        decoded = cls.JS_ESCAPE.sub(cls._unescape_js, raw)
        payload = json.loads(decoded)
        store = payload.get("data", payload) if isinstance(payload, dict) else {}
        entries = []
        seen = set()
        for value in store.values() if isinstance(store, dict) else []:
            if not isinstance(value, dict):
                continue
            video, rank = value.get("top10Video"), value.get("top10")
            if not isinstance(video, dict) or not isinstance(rank, dict):
                continue
            video_id = video.get("videoId")
            identity = video_id or video.get("title")
            if not identity or identity in seen:
                continue
            seen.add(identity)
            parent = video.get("parentShow")
            entries.append({
                "rank": cls._optional_int(rank.get("weeklyRank")) or 10 ** 6,
                "title": video.get("title"),
                "clean_title": parent.get("title") if isinstance(parent, dict) else None,
                "year": cls._optional_int(video.get("releaseYear")),
                "video_id": video_id,
                "week": rank.get("weekEndDate"),
            })
        return entries

    @classmethod
    def rich_candidates(
            cls, entries: list[dict], kind: str, scope: str, limit: int
    ) -> Iterator[MediaCandidate]:
        is_movie = kind == "films"
        for entry in sorted(entries, key=lambda item: item.get("rank") or 10 ** 6)[:limit]:
            raw_title = str(entry.get("title") or "").strip()
            title = str(entry.get("clean_title") or raw_title).strip()
            if not title:
                continue
            season = None if is_movie else cls._season_number(raw_title)
            yield MediaCandidate(
                title=title,
                year=str(entry.get("year")) if entry.get("year") else None,
                media_type="movie" if is_movie else "tv",
                season=season,
                source="netflix",
                source_meta={
                    "scope": scope,
                    "category": kind,
                    "rank": entry.get("rank"),
                    "week": entry.get("week"),
                    "video_id": entry.get("video_id"),
                    "source": "rich",
                },
                unique_seed=f"{'movie' if is_movie else 'tv'}:{title}:{season if season is not None else ''}",
            )

    @classmethod
    def _season_number(cls, title: str) -> int:
        match = cls.SEASON.search(str(title or ""))
        return int(match.group(1)) if match else 1

    @staticmethod
    def _optional_int(value):
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _unescape_js(match: re.Match) -> str:
        value = match.group(1)
        if value.startswith(("u", "x")):
            return chr(int(value[1:], 16))
        return {"n": "\n", "r": "\r", "t": "\t", "b": "\b", "f": "\f"}.get(value, value)
