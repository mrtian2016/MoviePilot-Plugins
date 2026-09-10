"""Magnet 搜索渠道共用的候选规范化。"""

from typing import Any, Dict, Iterable, List

from .matching import unique_texts
from ..utils.magnet import parse_magnet_metadata


def clear_cache(target: Any) -> int:
    operation = getattr(target, "clear_cache", None)
    if not callable(operation):
        return 0
    result = operation()
    if isinstance(result, dict):
        return sum(int(value or 0) for value in result.values())
    return int(result or 0)


def media_titles(mediainfo: Any) -> List[str]:
    return unique_texts((
        getattr(mediainfo, "title", ""),
        getattr(mediainfo, "original_title", ""),
        getattr(mediainfo, "original_name", ""),
    ))


def normalize_magnets(
        resources: Iterable[Dict[str, Any]], source: str
) -> List[Dict[str, Any]]:
    normalized = []
    seen = set()
    for resource in resources or []:
        url = str(resource.get("url") or "").strip()
        if not url.casefold().startswith("magnet:?"):
            normalized.append(resource)
            continue
        provider_text = " ".join(
            str(resource.get(key) or "").strip()
            for key in (
                "title", "description", "name", "raw_title",
                "release_name", "quality",
            )
            if str(resource.get(key) or "").strip()
        )
        default_season = (
            int(resource.get("target_season"))
            if resource.get("identity_verified") and resource.get("target_season")
            else None
        )
        metadata = parse_magnet_metadata(
            url, provider_text, default_season=default_season
        )
        if not metadata:
            continue
        info_hash = str(metadata.get("info_hash") or "").upper()
        if not info_hash or info_hash in seen:
            continue
        seen.add(info_hash)
        item = dict(resource)
        item.update({
            "source": source,
            "resource_type": "magnet",
            "magnet_metadata": metadata,
            "info_hash": info_hash,
        })
        if metadata.get("display_name"):
            item["magnet_name"] = metadata["display_name"]
        if metadata.get("size") and not item.get("size"):
            item["size"] = metadata["size"]
        if metadata.get("preview_episodes"):
            item["preview_episodes"] = metadata["preview_episodes"]
        normalized.append(item)
    return normalized
