"""SeedHub 作品匹配与资源候选构造。"""

import re
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, Iterable, List, Optional

from app.schemas.types import MediaType

from .client import SeedHubClient, SeedHubError
from ..magnet import clear_cache, media_titles, normalize_magnets
from ..matching import (
    extract_season,
    extract_year,
    title_matches,
    unique_texts,
)
from ..types import resource_type_from_url
from ...core.search import SearchQuery


class SeedHubSearchService:
    def __init__(self, client: SeedHubClient, result_limit: int):
        self._client = client
        self._result_limit = result_limit

    @staticmethod
    def _keywords(
            titles: List[str], year: Any, media_type: MediaType, season: Any
    ) -> List[str]:
        keywords = []
        for title in titles:
            if media_type == MediaType.TV and season:
                keywords.extend((f"{title} S{season:02d}", f"{title} 第{season}季"))
            if year:
                keywords.append(f"{title} {year}")
            keywords.append(title)
        return unique_texts(keywords)

    @staticmethod
    def _type_matches(value: object, media_type: str) -> bool:
        text = str(value or "").strip().casefold()
        if not text:
            return False
        if media_type == "tv":
            return any(marker in text for marker in ("剧集", "电视剧", "tv", "series"))
        return any(marker in text for marker in ("电影", "movie"))

    @classmethod
    def _select_candidate(
            cls,
            candidates: Iterable[Dict[str, str]],
            expected_titles: List[str],
            expected_year: str,
            media_type: str,
            season: Optional[int],
            douban_id: Optional[object],
    ) -> Optional[Dict[str, str]]:
        expected_douban_id = str(douban_id or "").strip()
        best_id_match = None
        best_title_match = None
        for index, candidate in enumerate(candidates):
            if not cls._type_matches(candidate.get("media_type"), media_type):
                continue
            candidate_titles = [candidate.get("title"), candidate.get("anchor_title")]
            exact_douban = bool(
                expected_douban_id
                and str(candidate.get("douban_id") or "").strip()
                == expected_douban_id
            )
            matched_title = any(
                title_matches(value, expected_titles) for value in candidate_titles
            )
            if not exact_douban and not matched_title:
                continue
            candidate_year = str(candidate.get("year") or "")
            if (
                    not exact_douban and expected_year and candidate_year
                    and candidate_year != expected_year
            ):
                continue
            if (
                    not exact_douban and media_type == "movie"
                    and expected_year and not candidate_year
            ):
                continue
            candidate_season = next(
                (value for value in map(extract_season, candidate_titles) if value),
                None,
            )
            if (
                    not exact_douban and season and candidate_season
                    and candidate_season != season
            ):
                continue
            score = 1000 if exact_douban else 100
            if expected_year and candidate_year == expected_year:
                score += 40
            if season and candidate_season == season:
                score += 60
            ranked = (score, -index, candidate)
            if exact_douban:
                if best_id_match is None or (score, -index) > best_id_match[:2]:
                    best_id_match = ranked
            elif best_title_match is None or (score, -index) > best_title_match[:2]:
                best_title_match = ranked
        best = best_id_match or best_title_match
        return best[2] if best is not None else None

    def _pending_entries(
            self, movie_id: str, entries: List[Dict[str, str]], limit: int
    ) -> List[Dict[str, Any]]:
        results = []
        seen = set()
        for item in entries:
            kind = str(item.get("kind") or "").strip().lower()
            if kind == "magnet":
                seed_id = str(item.get("seed_id") or "").strip()
                if not re.fullmatch(r"\d{1,32}", seed_id):
                    continue
                identity = f"magnet:{seed_id}"
                resource_type = "magnet"
            elif kind == "pan":
                path = str(item.get("href") or "").strip()
                host = str(item.get("host") or "").strip().lower()
                resource_type = resource_type_from_url(
                    f"https://{host}"
                ) if host else ""
                if not path or not resource_type:
                    continue
                identity = f"pan:{path}"
            else:
                continue
            if identity in seen:
                continue
            seen.add(identity)
            resource_id = item.get("seed_id") or item.get("href") or ""
            results.append({
                "url": "",
                "title": item.get("title") or f"SeedHub 资源 {resource_id}",
                "size": item.get("size") or 0,
                "update_time": item.get("updated_at") or "",
                "resource_type": resource_type,
                "source_url": f"{self._client.base_url}/movies/{movie_id}/",
                "pending_resolution": True,
                "provider_data": {
                    "kind": kind,
                    "seed_id": str(item.get("seed_id") or ""),
                    "path": str(item.get("href") or ""),
                    "host": str(item.get("host") or ""),
                },
            })
            if len(results) >= limit:
                break
        return results

    def _resolve_entries(
            self, movie_id: str, entries: List[Dict[str, str]], limit: int
    ) -> List[Dict[str, Any]]:
        results = []
        seen = set()
        concurrency = min(6, len(entries) or 1)
        with ThreadPoolExecutor(
                max_workers=concurrency, thread_name_prefix="seedhub"
        ) as executor:
            for offset in range(0, len(entries), concurrency):
                batch = entries[offset:offset + concurrency]
                futures = [
                    (item, executor.submit(self._client.resolve_entry, item))
                    for item in batch
                ]
                for item, future in futures:
                    try:
                        url, resource_type = future.result()
                    except SeedHubError:
                        continue
                    key = url.casefold()
                    if not url or not resource_type or key in seen:
                        continue
                    seen.add(key)
                    identity = item.get("seed_id") or item.get("href") or ""
                    results.append({
                        "url": url,
                        "title": item.get("title") or f"SeedHub 资源 {identity}",
                        "size": item.get("size") or 0,
                        "update_time": item.get("updated_at") or "",
                        "resource_type": resource_type,
                        "source_url": f"{self._client.base_url}/movies/{movie_id}/",
                        "provider_data": {
                            "seed_id": str(item.get("seed_id") or "")
                        },
                    })
                    if len(results) >= limit:
                        break
                if len(results) >= limit:
                    break
        return results

    def _search(
            self,
            keywords: Iterable[str],
            titles: List[str],
            expected_year: object,
            media_type: str,
            season: Optional[int],
            douban_id: Optional[object],
            limit: int,
            resource_list_mode: bool,
    ) -> List[Dict[str, Any]]:
        normalized_keywords = unique_texts(keywords)
        if not titles or not normalized_keywords:
            return []
        year = extract_year(expected_year)
        selected = None
        for keyword in normalized_keywords:
            selected = self._select_candidate(
                self._client.search_candidates(keyword), titles, year,
                media_type, season, douban_id,
            )
            if selected:
                break
        if not selected:
            return []
        identity_verified = bool(
            douban_id
            and str(selected.get("douban_id") or "").strip()
            == str(douban_id).strip()
        )
        movie_id = selected["movie_id"]
        entries = self._client.detail_entries(movie_id)
        if not resource_list_mode and media_type == "tv" and season:
            entries = [
                item for item in entries
                if extract_season(item.get("title")) in (None, season)
            ]
        normalized_limit = max(1, min(int(limit or 20), 80))
        results = (
            self._pending_entries(movie_id, entries, normalized_limit)
            if resource_list_mode else
            self._resolve_entries(movie_id, entries, normalized_limit)
        )
        selected_douban_id = str(selected.get("douban_id") or "").strip()
        if selected_douban_id:
            for result in results:
                result.setdefault("provider_data", {})["douban_id"] = (
                    selected_douban_id
                )
                result["identity_verified"] = identity_verified
                result["target_season"] = (
                    int(season) if identity_verified and season else None
                )
        return results

    def search(self, query: SearchQuery):
        mediainfo = query.mediainfo
        titles = media_titles(mediainfo)
        resources = self._search(
            keywords=self._keywords(
                titles,
                getattr(mediainfo, "year", None),
                query.media_type,
                query.season,
            ),
            titles=titles,
            expected_year=getattr(mediainfo, "year", None),
            media_type=(
                "tv" if query.media_type == MediaType.TV else "movie"
            ),
            season=query.season,
            douban_id=getattr(mediainfo, "douban_id", None),
            limit=(
                query.result_limit or self._result_limit
                if query.resource_list_mode else self._result_limit
            ),
            resource_list_mode=query.resource_list_mode,
        )
        return normalize_magnets(resources, "seedhub")

    def resolve(self, **kwargs):
        return self._client.resolve_resource(**kwargs)

    def clear_cache(self) -> int:
        return clear_cache(self._client)
