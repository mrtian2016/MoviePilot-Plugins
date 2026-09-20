"""PanSou 搜索结果匹配与候选构造。"""

import re
from typing import Any, Dict, List

import unicodedata
from app.log import logger
from app.schemas import MediaInfo
from app.schemas.types import MediaType

from ..magnet import clear_cache, normalize_magnets
from ..types import PANSOU_RESOURCE_TYPES, resource_type_name
from ...core import OwnerDelegator, SearchQuery, format_search_log_prefix


class PanSouSearchService(OwnerDelegator):
    """将 PanSou 协议响应转换为统一搜索候选。"""

    _PUNCT_GAP_RE = re.compile(
        r"[\s\u3000:：·•.,，。!！?？（）【】\[\]/／\\＼-]+"
    )

    @staticmethod
    def _normalize_for_match(text: str) -> str:
        value = unicodedata.normalize("NFKC", str(text or ""))
        for old, new in (
                ("：", ":"), ("，", ","), ("（", "("), ("）", ")"),
                ("【", "["), ("】", "]"), ("！", "!"), ("？", "?"),
                ("–", "-"), ("—", "-"), ("…", "..."),
        ):
            value = value.replace(old, new)
        return re.sub(r"[\s\u3000]+", " ", value).strip().casefold()

    @classmethod
    def _compact_for_match(cls, text: str) -> str:
        return cls._PUNCT_GAP_RE.sub("", cls._normalize_for_match(text))

    @classmethod
    def _title_matches_search_key(cls, key: str, title: str) -> bool:
        if not key:
            return True
        if key in (title or ""):
            return True
        normalized_key = cls._normalize_for_match(key)
        normalized_title = cls._normalize_for_match(title)
        if normalized_key and normalized_key in normalized_title:
            return True
        compact_key = cls._compact_for_match(key)
        return bool(
            len(compact_key) >= 2
            and compact_key in cls._compact_for_match(title)
        )

    @staticmethod
    def _is_word_char(value: str) -> bool:
        return bool(value) and (value.isalnum() or "\u3400" <= value <= "\u9fff")

    @classmethod
    def _title_matches_media(
            cls,
            media_titles: List[str],
            media_year: Any,
            resource_title: str,
    ) -> bool:
        normalized_resource = cls._normalize_for_match(resource_title)
        if not normalized_resource:
            return False
        comparable_resource = cls._PUNCT_GAP_RE.sub(
            " ", normalized_resource
        ).strip()
        expected_year = str(media_year or "").strip()
        resource_years = set(
            re.findall(r"(?<!\d)((?:19|20)\d{2})(?!\d)", normalized_resource)
        )
        if expected_year and resource_years and expected_year not in resource_years:
            return False
        for media_title in media_titles:
            comparable_title = cls._PUNCT_GAP_RE.sub(
                " ", cls._normalize_for_match(media_title)
            ).strip()
            if not comparable_title:
                continue
            start = 0
            while True:
                index = comparable_resource.find(comparable_title, start)
                if index < 0:
                    break
                previous = comparable_resource[index - 1] if index else ""
                prefix = comparable_resource[:index].rstrip()
                if (
                        not cls._is_word_char(previous)
                        or bool(expected_year and prefix.endswith(expected_year))
                ):
                    return True
                start = index + 1
        return False

    @staticmethod
    def _media_titles(mediainfo: MediaInfo) -> List[str]:
        return list(dict.fromkeys(
            value for value in (
                str(getattr(mediainfo, "title", "") or "").strip(),
                str(
                    getattr(mediainfo, "original_title", "")
                    or getattr(mediainfo, "original_name", "")
                    or ""
                ).strip(),
            ) if value
        ))

    @staticmethod
    def _resource_type(resource: Dict[str, Any]) -> str:
        value = str(resource.get("resource_type") or "").strip().lower()
        return "alipan" if value == "aliyun" else value

    @classmethod
    def _normalize_results(
            cls,
            rows: Any,
            keyword: str,
            media_titles: List[str],
            media_year: Any,
            allowed_types: List[str],
            limit: int,
    ) -> Dict[str, List[Dict[str, Any]]]:
        groups: Dict[str, List[Dict[str, Any]]] = {}
        allowed = set(allowed_types)
        for item in rows if isinstance(rows, list) else []:
            if not isinstance(item, dict):
                continue
            item_title = re.sub(r"<[^>]+>", "", str(item.get("title") or ""))
            raw_tags = item.get("tags") or []
            if not isinstance(raw_tags, list):
                raw_tags = [raw_tags]
            tags = [str(tag).strip() for tag in raw_tags if str(tag or "").strip()]
            for link in item.get("links") or []:
                if not isinstance(link, dict):
                    continue
                title = re.sub(
                    r"<[^>]+>", "", str(link.get("work_title") or item_title)
                ).strip()
                if (
                        not cls._title_matches_search_key(keyword, title)
                        and not any(
                    cls._title_matches_search_key(value, title)
                    for value in media_titles
                )
                ):
                    continue
                if media_titles and not cls._title_matches_media(
                        media_titles, media_year, title
                ):
                    continue
                resource_type = str(link.get("type") or "unknown").strip().lower()
                if allowed and resource_type not in allowed:
                    continue
                group = groups.setdefault(
                    resource_type_name(resource_type, resource_type), []
                )
                candidate = {
                    "url": link.get("url") or "",
                    "title": title,
                    "update_time": item.get("datetime") or "",
                    "resource_type": resource_type,
                    "tags": tags,
                }
                description = re.sub(
                    r"<[^>]+>", "", str(
                        link.get("description")
                        or item.get("description")
                        or item.get("content")
                        or item.get("message")
                        or ""
                    )
                ).strip()
                if description and description != title:
                    candidate["description"] = description
                source_url = str(
                    item.get("source_url") or item.get("message_url") or ""
                ).strip()
                if source_url.startswith(("http://", "https://")):
                    candidate["source_url"] = source_url
                password = str(link.get("password") or "").strip()
                if password:
                    candidate["password"] = password
                group.append(candidate)
        for group in groups.values():
            group.sort(key=lambda row: row.get("update_time", ""), reverse=True)
        return groups

    @staticmethod
    def _round_robin(
            groups: List[List[Dict[str, Any]]], limit: int
    ) -> List[Dict[str, Any]]:
        results = []
        offsets = [0] * len(groups)
        while groups and len(results) < limit:
            for index in range(len(groups) - 1, -1, -1):
                group = groups[index]
                offset = offsets[index]
                if offset >= len(group):
                    groups.pop(index)
                    offsets.pop(index)
                    continue
                results.append(group[offset])
                offsets[index] += 1
                if len(results) >= limit:
                    break
        return results

    def search(self, query: SearchQuery) -> List[Dict[str, Any]]:
        mediainfo = query.mediainfo
        media_type = query.media_type
        season = max(1, int(query.season or 1)) if media_type == MediaType.TV else None
        keyword = (
            str(mediainfo.title or "").strip()
            if media_type == MediaType.TV else
            f"{mediainfo.title} {mediainfo.year or ''}".strip()
        )
        prefix = format_search_log_prefix(query, "pansou")
        if not self._pansou_client:
            logger.warning(f"{prefix} 客户端未初始化，跳过查询")
            return []
        titles = self._media_titles(mediainfo)
        limit = (
            max(1, int(query.result_limit or self._pansou_result_limit))
            if query.resource_list_mode else self._pansou_result_limit
        )
        allowed_types = (
            [
                "aliyun" if value == "alipan" else value
                for value in PANSOU_RESOURCE_TYPES
            ] if query.resource_list_mode else
            [
                "aliyun" if value == "alipan" else value
                for value in self._resource_type_order_config
            ]
        )
        if not query.resource_list_mode and query.subscribe is not None:
            target_drive = str(getattr(self, "_cloud_drive_key", "") or "").strip().lower()
            if target_drive:
                allowed_types = [
                    "aliyun" if target_drive == "alipan" else target_drive
                ]
        response = self._pansou_client.request_search(
            keyword=keyword,
            cloud_types=allowed_types,
            channels=[] if query.resource_list_mode else self._pansou_channels,
            plugins=[] if query.resource_list_mode else self._pansou_plugins,
            filter_config={} if query.resource_list_mode else self._pansou_filter,
            refresh=self._pansou_refresh,
            concurrency=self._pansou_concurrency,
            response_mode="merge" if query.resource_list_mode else "results",
        )
        raw_items = (response or {}).get("results") or []
        # 如果带年份搜索结果为空，尝试以纯标题降级检索
        pure_title = str(mediainfo.title or "").strip()
        if (not raw_items) and keyword != pure_title and pure_title:
            logger.debug(f"{prefix} 带年份关键词 '{keyword}' 无结果，降级尝试纯标题 '{pure_title}'")
            fallback_res = self._pansou_client.request_search(
                keyword=pure_title,
                cloud_types=allowed_types,
                channels=[] if query.resource_list_mode else self._pansou_channels,
                plugins=[] if query.resource_list_mode else self._pansou_plugins,
                filter_config={} if query.resource_list_mode else self._pansou_filter,
                refresh=self._pansou_refresh,
                concurrency=self._pansou_concurrency,
                response_mode="merge" if query.resource_list_mode else "results",
            )
            if fallback_res and fallback_res.get("results"):
                response = fallback_res
                keyword = pure_title

        if not response or response.get("error"):
            reason = response.get("error") if response else "接口未返回结果"
            logger.warning(f"{prefix} 搜索失败：关键词 '{keyword}'，原因：{reason}")
            return []
        groups = self._normalize_results(
            response.get("results"), keyword, titles,
            None if query.resource_list_mode else getattr(mediainfo, "year", None),
            allowed_types, limit,
        )
        grouped = [
            group for resource_type, group in groups.items()
            if query.resource_list_mode or self._resource_type(group[0]) in self._resource_type_order_config
            if group
        ]
        candidates = self._round_robin(grouped, limit)
        candidates = normalize_magnets(candidates, "pansou")
        usable = [
            resource for resource in candidates
            if (
                       query.resource_list_mode
                       or resource.get("resource_type") != "magnet"
                       or resource.get("magnet_metadata")
               )
               and self._media_type_matches(resource, media_type)
        ]
        logger.debug(
            f"{prefix} 渠道统计：原始条目={int(response.get('raw_count') or 0)}，"
            f"匹配链接={sum(len(group) for group in groups.values())}，"
            f"有效返回={len(usable)}"
        )
        return usable

    @staticmethod
    def _media_type_matches(
            resource: Dict[str, Any], media_type: MediaType
    ) -> bool:
        tags = " ".join(str(tag) for tag in (resource.get("tags") or [])).lower()
        if not tags:
            return True
        has_movie = any(marker in tags for marker in ("电影", "影片", "movie"))
        has_tv = any(
            marker in tags for marker in ("电视剧", "剧集", "连续剧", "tv series", "tv")
        )
        if media_type == MediaType.MOVIE:
            return not (has_tv and not has_movie)
        return not (has_movie and not has_tv)

    def clear_cache(self) -> int:
        return clear_cache(self._pansou_client)
