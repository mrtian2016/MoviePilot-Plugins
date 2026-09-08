"""在线文档配置、匹配与候选构造。"""

import re
from typing import Any, Dict, Iterable, List

from app.log import logger

from .client import OnlineDocumentClient, is_online_document_url
from ..magnet import clear_cache
from ..types import SUPPORTED_RESOURCE_TYPES, normalize_resource_type
from ...core.search import SearchQuery, format_search_log_prefix


class OnlineDocumentSearchService:
    _CONTEXT_WINDOW = 8000

    def __init__(self, client: OnlineDocumentClient):
        self._client = client
        self._documents = self._normalize_documents(
            client.documents, client.resource_types
        )

    @staticmethod
    def _normalize_documents(
            documents: Iterable[Any], default_resource_types: Any
    ) -> List[Dict[str, Any]]:
        normalized = []
        for value in documents or []:
            item = value if isinstance(value, dict) else {"url": value}
            url = str(item.get("url") or "").strip()
            if not is_online_document_url(url):
                continue
            resource_types = (
                    item.get("resource_types") or default_resource_types or []
            )
            if isinstance(resource_types, str):
                resource_types = re.split(r"[,，\s]+", resource_types)
            normalized.append({
                "url": url,
                "resource_types": list(dict.fromkeys(
                    normalize_resource_type(kind) for kind in resource_types
                    if normalize_resource_type(kind) in SUPPORTED_RESOURCE_TYPES
                )),
            })
        return normalized

    @classmethod
    def _matching_links(
            cls, parsed: Dict[str, Any], keywords: Iterable[str]
    ) -> List[Dict[str, Any]]:
        links = list(parsed.get("links") or [])
        needles = list(dict.fromkeys(
            str(keyword or "").casefold().strip()
            for keyword in keywords
            if str(keyword or "").strip()
        ))
        if not needles:
            return links
        lowered = str(parsed.get("text") or "").casefold()
        positions = []
        for needle in needles:
            offset = 0
            while True:
                position = lowered.find(needle, offset)
                if position < 0:
                    break
                positions.append(position)
                offset = position + max(1, len(needle))
        if not positions:
            return []
        ranked = []
        for index, item in enumerate(links):
            link_position = int(item.get("_position") or 0)
            distance = min(abs(link_position - position) for position in positions)
            if distance <= cls._CONTEXT_WINDOW:
                ranked.append((distance, index, item))
        ranked.sort(key=lambda value: (value[0], value[1]))
        return [item for _, _, item in ranked]

    def search(self, query: SearchQuery) -> List[Dict[str, Any]]:
        prefix = format_search_log_prefix(query, "online_docs")
        titles = []
        for value in (
                getattr(query.mediainfo, "title", ""),
                getattr(query.mediainfo, "original_title", ""),
                getattr(query.mediainfo, "original_name", ""),
        ):
            text = str(value or "").strip()
            if text and text not in titles:
                titles.append(text)
        keyword = titles[0] if titles else ""
        limit = max(1, min(int(query.result_limit or 20), 100))
        results = []
        for document in self._documents:
            document_url = document["url"]
            parsed = self._client.read(document_url)
            if parsed.get("error"):
                logger.warning(
                    f"{prefix} 文档读取失败：{document_url}，"
                    f"{parsed['error']}"
                )
                continue
            matched_links = self._matching_links(parsed, titles)
            logger.debug(
                f"{prefix} 文档匹配：文档={document_url}，"
                f"关键词={keyword}，正文字符={len(parsed.get('text') or '')}，"
                f"资源链接={len(parsed.get('links') or [])}，"
                f"相邻候选={len(matched_links)}"
            )
            for item in matched_links:
                allowed_types = document["resource_types"]
                if (
                        not query.resource_list_mode
                        and allowed_types
                        and item.get("resource_type") not in allowed_types
                ):
                    continue
                result = {
                    key: value
                    for key, value in item.items()
                    if not key.startswith("_")
                }
                result.update({
                    "title": keyword,
                    "source_url": document_url,
                    "document_type": parsed.get("document_type"),
                })
                results.append(result)
                if len(results) >= limit:
                    return results
        return results

    def clear_cache(self) -> int:
        return clear_cache(self._client)
