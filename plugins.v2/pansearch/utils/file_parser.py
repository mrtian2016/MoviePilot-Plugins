"""媒体文件树展开与季集标记解析。"""

import re
from pathlib import Path
from typing import Any, Iterator, Optional, Tuple


class MediaFileParser:
    """文件结构和命名解析"""

    VIDEO_EXTENSIONS = {".mkv", ".mp4", "avi", ".iso"}
    SUBTITLE_EXTENSIONS = {
        ".srt", ".ass", ".ssa", ".vtt", ".sub", ".sup", ".idx", ".smi", ".mks",
    }
    _SXEX_PATTERN = re.compile(r"[Ss](\d{1,2})[Ee](\d{1,4})")
    _SEASON_EPISODE_PATTERN = re.compile(r"[Ss](\d{1,2})[Ee]")
    _CN_SEASON_PATTERN = re.compile(r"第\s*(\d{1,2})\s*季")
    _EN_SEASON_PATTERN = re.compile(r"[Ss]eason\s*(\d{1,2})", re.IGNORECASE)
    ANY_SEASON_PATTERN = re.compile(
        r"[Ss]\d+[Ee]|第\s*\d+\s*季|[Ss]eason\s*\d+", re.IGNORECASE
    )

    @classmethod
    def is_video(cls, file_name: str) -> bool:
        return Path(str(file_name or "")).suffix.lower() in cls.VIDEO_EXTENSIONS

    @classmethod
    def is_subtitle(cls, file_name: str) -> bool:
        return Path(str(file_name or "")).suffix.lower() in cls.SUBTITLE_EXTENSIONS

    @classmethod
    def contains_other_season(cls, file_name: str, target_season: int) -> bool:
        for pattern in (
                cls._SEASON_EPISODE_PATTERN,
                cls._CN_SEASON_PATTERN,
                cls._EN_SEASON_PATTERN,
        ):
            match = pattern.search(file_name)
            if match:
                return int(match.group(1)) != target_season
        return False

    @classmethod
    def matches_target_season(cls, file_name: str, target_season: int) -> bool:
        for pattern in (
                cls._SEASON_EPISODE_PATTERN,
                cls._CN_SEASON_PATTERN,
                cls._EN_SEASON_PATTERN,
        ):
            match = pattern.search(file_name)
            if match:
                return int(match.group(1)) == target_season
        return False

    @classmethod
    def extract_season_episode(
            cls, file_name: str
    ) -> Optional[Tuple[int, int]]:
        match = cls._SXEX_PATTERN.search(file_name)
        if not match:
            return None
        return int(match.group(1)), int(match.group(2))

    @classmethod
    def iter_files(cls, files: list, parent_path: str = "") -> Iterator[Any]:
        for item in files or []:
            name = str(item.get("name") or item.get("file_name") or "").strip()
            relative_path = "/".join(
                value for value in (parent_path.strip("/"), name) if value
            )
            if item.get("is_dir"):
                yield from cls.iter_files(
                    item.get("children") or [], relative_path
                )
            else:
                if relative_path and isinstance(item, dict):
                    item.setdefault("_relative_path", relative_path)
                    yield item
                elif relative_path:
                    # CloudFile 等 frozen Mapping 无法原地写入；产出可变副本，
                    # 让下游 _relative_path 消费方拿到一致的数据视图。
                    enriched = dict(item)
                    enriched["_relative_path"] = relative_path
                    yield enriched
                else:
                    yield item
