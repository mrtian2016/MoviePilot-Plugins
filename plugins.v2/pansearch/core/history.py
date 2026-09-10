"""转存历史的稳定媒体分组标识。"""

from typing import Any, Mapping

import unicodedata


def history_group_key(record: Mapping[str, Any]) -> str:
    """按媒体身份生成稳定分组键，避免任务时间切碎同一媒体历史。"""
    media_type = str(
        record.get("type") or record.get("media_type") or "未知类型"
    ).strip() or "未知类型"
    tmdb_id = str(record.get("tmdb_id") or "").strip()
    if tmdb_id:
        return f"tmdb:{media_type}:{tmdb_id}"

    title = " ".join(
        unicodedata.normalize(
            "NFKC", str(record.get("title") or "")
        ).casefold().split()
    )
    year = (
        str(record.get("year") or "").strip()
        if media_type == "电影" else ""
    )
    return f"legacy:{media_type}:{title}:{year}"
