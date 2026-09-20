"""盘链作品匹配与资源候选构造。"""

import re
import time
from typing import Any, Dict, Iterable, List, Optional

from app.log import logger

from .client import PinglianClient, PinglianError
from ..magnet import clear_cache, media_titles
from ..matching import extract_year, title_matches, unique_texts
from ..types import (
    RESOURCE_TYPE_ORDER,
    SUPPORTED_RESOURCE_TYPES,
    normalize_resource_type,
    resource_type_from_url,
)
from ...core.search import SearchQuery, format_search_log_prefix


class PinglianSearchService:
    def __init__(
            self,
            client: PinglianClient,
            resource_types: Iterable[str],
            result_limit: int,
    ):
        self._client = client
        self._resource_types = tuple(resource_types)
        self._result_limit = result_limit

    @staticmethod
    def _video_title(row: Dict[str, Any]) -> str:
        name = str(row.get("vod_name") or "").strip()
        row_year = extract_year(row.get("vod_year"))
        if not name or not row_year:
            return name
        normalized = re.sub(
            rf"[\s（(【\[]*{re.escape(row_year)}[）)】\]]*$", "", name
        ).strip()
        return normalized or name

    @classmethod
    def _select_video(
            cls, rows: Iterable[Dict[str, Any]], titles: List[str], year: str
    ) -> Optional[Dict[str, Any]]:
        best = None
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                continue
            name = cls._video_title(row)
            if not title_matches(name, titles):
                continue
            row_year = extract_year(row.get("vod_year"))
            if year and row_year and row_year != year:
                continue
            exact = any(name.casefold() == title.casefold() for title in titles)
            score = (200 if exact else 100) + (
                50 if year and row_year == year else 0
            )
            ranked = (score, -index, row)
            if best is None or (score, -index) > best[:2]:
                best = ranked
        return best[2] if best is not None else None

    @staticmethod
    def _round_robin(candidates: List[tuple]) -> List[tuple]:
        grouped = {}
        for candidate in candidates:
            grouped.setdefault(candidate[2], []).append(candidate)
        results = []
        offsets = {resource_type: 0 for resource_type in grouped}
        while grouped:
            for resource_type in list(grouped):
                rows = grouped[resource_type]
                offset = offsets[resource_type]
                results.append(rows[offset])
                offset += 1
                offsets[resource_type] = offset
                if offset >= len(rows):
                    grouped.pop(resource_type)
                    offsets.pop(resource_type, None)
        return results

    def _search(
            self,
            titles: List[str],
            year: Any,
            limit: int,
            resource_list_mode: bool,
            log_prefix: str,
    ) -> List[Dict[str, Any]]:
        titles = unique_texts(titles)
        if not titles:
            return []
        allowed = (
            list(RESOURCE_TYPE_ORDER)
            if resource_list_mode else list(dict.fromkeys(
                normalize_resource_type(value) for value in self._resource_types
                if normalize_resource_type(value) in SUPPORTED_RESOURCE_TYPES
            ))
        )
        if not allowed:
            return []
        expected_year = extract_year(year)
        prefix = str(log_prefix or "[PINGLIAN]")
        video = None
        selected_keyword = ""
        for keyword in titles:
            payload = self._client.request_json(
                "/api/get_videos.php", params={"wd": keyword, "pg": 1}
            )
            rows = payload.get("list") or []
            rows = rows if isinstance(rows, list) else []
            logger.debug(f"{prefix} get_videos：关键词={keyword}，条目={len(rows)}")
            video = self._select_video(rows, titles, expected_year)
            if video:
                selected_keyword = keyword
                break
        if not video:
            logger.debug(f"{prefix} 未选中作品：关键词={','.join(titles)}")
            return []
        vod_id = video.get("vod_id")
        logger.debug(
            f"{prefix} 选中作品：vod_id={vod_id}，标题={self._video_title(video)}"
        )
        payload = self._client.request_json(
            "/api/search_pan_links.php",
            params={
                "keyword": selected_keyword,
                "vod_id": vod_id,
                "_t": int(time.time() * 1000),
            },
        )
        groups = payload.get("data") if payload.get("success") else None
        if not isinstance(groups, dict):
            logger.debug(f"{prefix} search_pan_links：分组=0，原始链接=0")
            return []
        type_order = {value: index for index, value in enumerate(allowed)}
        candidates = []
        raw_link_count = 0
        type_counts: Dict[str, int] = {}
        filtered_type_counts: Dict[str, int] = {}
        for group_key, group in groups.items():
            rows = (group.get("links") or []) if isinstance(group, dict) else []
            for row in rows:
                raw_link_count += 1
                if not isinstance(row, dict):
                    continue
                direct_target = str(row.get("url") or "").strip()
                raw_type = (
                        row.get("type") or group_key
                        or (group.get("name") if isinstance(group, dict) else "")
                )
                resource_type = normalize_resource_type(raw_type)
                if not resource_type and direct_target:
                    resource_type = resource_type_from_url(direct_target)
                token = str(row.get("token") or "").strip()
                if resource_type:
                    type_counts[resource_type] = type_counts.get(resource_type, 0) + 1
                if resource_type not in type_order or (not direct_target and not token):
                    continue
                filtered_type_counts[resource_type] = (
                        filtered_type_counts.get(resource_type, 0) + 1
                )
                candidates.append((
                    type_order[resource_type], row, resource_type,
                    direct_target, token,
                ))
        logger.debug(
            f"{prefix} search_pan_links：分组={len(groups)}，"
            f"原始链接={raw_link_count}，"
            f"类型={'/'.join(f'{k}={v}' for k, v in type_counts.items()) or '无'}，"
            f"已选类型候选={'/'.join(f'{k}={v}' for k, v in filtered_type_counts.items()) or '无'}，"
            f"可用候选={len(candidates)}"
        )

        def user_tier(item: tuple) -> int:
            try:
                return int(item[1].get("user_tier") or 0)
            except (TypeError, ValueError):
                return 0

        candidates.sort(key=lambda item: (item[0], -user_tier(item)))
        if resource_list_mode:
            candidates = self._round_robin(candidates)
        results = []
        seen = set()
        direct_count = 0
        resolved_count = 0
        resolve_failed_count = 0
        normalized_limit = max(1, min(int(limit or 20), 80))
        for _, row, resource_type, direct_target, token in candidates:
            if len(results) >= normalized_limit:
                break
            key = (
                resource_type,
                str(row.get("title") or "").strip(),
                direct_target or token,
            )
            if key in seen:
                continue
            seen.add(key)
            source_url = f"{self._client.base_url}/pages/video.php?id={vod_id}"
            target = direct_target
            if target:
                if resource_type_from_url(target) != resource_type:
                    resolve_failed_count += 1
                    logger.debug(f"{prefix} 跳过类型不匹配的直链")
                    continue
                direct_count += 1
            elif resource_list_mode:
                results.append({
                    "title": str(row.get("title") or "盘链资源").strip(),
                    "description": str(row.get("source") or "").strip(),
                    "url": "",
                    "resource_type": resource_type,
                    "update_time": str(row.get("time") or ""),
                    "source_url": source_url,
                    "pending_resolution": True,
                    "provider_data": {
                        "resource_id": str(row.get("id") or ""),
                        "token": token,
                        "password": str(row.get("password") or ""),
                    },
                })
                continue
            else:
                try:
                    resolved = self._client.resolve_resource(
                        token, resource_type, str(row.get("password") or "")
                    )
                    target = resolved.get("url") or ""
                    resolved_count += 1
                except PinglianError as error:
                    resolve_failed_count += 1
                    logger.debug(f"{prefix} 跳过不可用资源：{error.code}")
                    continue
            if direct_target:
                target = self._client.apply_password(
                    resource_type, target, str(row.get("password") or "")
                )
            results.append({
                "title": str(row.get("title") or "盘链资源").strip(),
                "description": str(row.get("source") or "").strip(),
                "url": target,
                "resource_type": resource_type,
                "update_time": str(row.get("time") or ""),
                "source_url": source_url,
                "provider_data": {"resource_id": str(row.get("id") or "")},
            })
        logger.debug(
            f"{prefix} 候选解析：直链={direct_count}，"
            f"token回退={resolved_count}，跳过={resolve_failed_count}"
        )
        return results

    def search(self, query: SearchQuery):
        mediainfo = query.mediainfo
        titles = media_titles(mediainfo)
        return self._search(
            titles=titles,
            year=getattr(mediainfo, "year", None),
            limit=(
                query.result_limit or self._result_limit
                if query.resource_list_mode else self._result_limit
            ),
            resource_list_mode=query.resource_list_mode,
            log_prefix=format_search_log_prefix(query, "pinglian"),
        )

    def resolve(self, **kwargs):
        return self._client.resolve_resource(**kwargs)

    def clear_cache(self) -> int:
        return clear_cache(self._client)
