"""按媒体结构匹配实际文件候选。"""

import re
from typing import Any, Dict, List

from app.core.metainfo import MetaInfo
from app.log import logger

from .file_parser import MediaFileParser
from ..search.matching import normalize_title, title_matches


class FileMatcher:
    """电影或剧集候选"""

    _TITLE_NOISE_RE = re.compile(r"[丨｜|¦]+")
    _COMPACT_YEAR_RE = re.compile(r"(?<!\s)(\((?:19|20)\d{2}\))")
    _TITLELESS_SEASON_EPISODE = re.compile(
        r"^[Ss]0*(\d{1,3})[ ._~-]*[Ee](?:[Pp])?0*(\d{1,4})(?=[ ._~-]|$)",
        re.IGNORECASE,
    )
    _TITLELESS_EPISODE = re.compile(
        r"^(?:第\s*)?(?:[Ee](?:[Pp])?)?0*(\d{1,4})(?:\s*集)?(?=[ ._~-]|$)",
        re.IGNORECASE,
    )

    @staticmethod
    def _matches_target_media(meta: Any, mediainfo: Any, season: int | None = None) -> bool:
        if mediainfo is None:
            return True
        expected_titles = [
            getattr(mediainfo, key, None)
            for key in ("title", "en_title", "original_title")
        ]
        candidate_titles = [
            getattr(meta, key, None)
            for key in ("cn_name", "en_name", "original_name")
        ]
        if not any(
                title_matches(candidate, expected_titles)
                for candidate in candidate_titles
                if candidate
        ):
            return False

        expected_years = {
            str(value).strip()
            for value in (
                getattr(mediainfo, "year", None),
                (
                    (getattr(mediainfo, "season_years", {}) or {}).get(season)
                    if season is not None else None
                ),
            )
            if str(value or "").strip()
        }
        candidate_year = str(getattr(meta, "year", None) or "").strip()
        if candidate_year and expected_years and candidate_year not in expected_years:
            return False
        candidate_type = getattr(meta, "type", None)
        expected_type = getattr(mediainfo, "type", None)
        if (
                candidate_type and expected_type
                and str(getattr(candidate_type, "value", candidate_type)) not in {"未知", "unknown"}
                and candidate_type != expected_type
        ):
            return False
        return True

    @staticmethod
    def _platform_meta(name: str) -> Any:
        try:
            # 部分分享用竖线类字符拆散中文片名，先去除这类装饰噪声再交给平台识别。
            normalized = FileMatcher._TITLE_NOISE_RE.sub("", name)
            normalized = FileMatcher._COMPACT_YEAR_RE.sub(r" \1", normalized)
            return MetaInfo(normalized)
        except Exception as error:
            logger.debug(f"平台媒体解析失败，跳过文件：{name} - {error}")
            return None

    @staticmethod
    def _has_explicit_media_title(meta: Any) -> bool:
        """数字、E01、S01E01 等只有季集标记的名称不算作品标题。"""
        for key in ("cn_name", "en_name", "original_name"):
            normalized = normalize_title(getattr(meta, key, None))
            if not normalized:
                continue
            residue = re.sub(r"(?i)(?:s\d{1,3})?(?:e|ep)?\d{1,4}", "", normalized)
            if residue:
                return True
        return False

    @classmethod
    def _titleless_episode_number(
            cls, name: str, meta: Any, season: int
    ) -> int | None:
        """仅为无作品名前缀的文件补全当前媒体和季号。"""
        if cls._has_explicit_media_title(meta):
            return None
        parsed_season = getattr(meta, "begin_season", None)
        parsed_episode = getattr(meta, "begin_episode", None)
        if parsed_episode is not None:
            try:
                if parsed_season is None or int(parsed_season) == int(season):
                    return int(parsed_episode)
            except (TypeError, ValueError):
                return None
        season_episode = cls._TITLELESS_SEASON_EPISODE.match(name)
        if season_episode:
            if int(season_episode.group(1)) == int(season):
                return int(season_episode.group(2))
            return None
        episode = cls._TITLELESS_EPISODE.match(name)
        return int(episode.group(1)) if episode else None

    @classmethod
    def _meta_with_target_defaults(
            cls, name: str, meta: Any, mediainfo: Any, season: int
    ) -> tuple[Any, int | None]:
        episode = cls._titleless_episode_number(name, meta, season)
        if episode is None or episode <= 0 or mediainfo is None:
            return meta, episode
        title = str(
            getattr(mediainfo, "title", None)
            or getattr(mediainfo, "en_title", None)
            or ""
        ).strip()
        if not title:
            return meta, None
        season_years = getattr(mediainfo, "season_years", {}) or {}
        year = str(
            season_years.get(season) or getattr(mediainfo, "year", None) or ""
        ).strip()
        synthetic = f"{title} {year} S{int(season):02d}E{episode:02d}".strip()
        return cls._platform_meta(synthetic), episode

    @classmethod
    def media_name_matches(cls, name: str, mediainfo: Any) -> bool:
        """确认文本经识别后属于目标媒体。"""
        normalized = str(name or "").strip()
        if not normalized:
            return False
        meta = cls._platform_meta(normalized)
        return bool(meta and cls._matches_target_media(meta, mediainfo))

    @classmethod
    def media_matches(cls, item: Any, mediainfo: Any) -> bool:
        """确认分享文件经识别后属于目标媒体。"""
        name = str((item or {}).get("name") or "").strip()
        return bool(
            name
            and MediaFileParser.is_video(name)
            and cls.media_name_matches(name, mediainfo)
        )

    @classmethod
    def episode_from_file(
            cls, item: Any, season: int, mediainfo: Any = None
    ) -> int | None:
        """使用元数据解析文件，并确认它属于目标媒体。"""
        name = str((item or {}).get("name") or "").strip()
        if not name or not MediaFileParser.is_video(name):
            return None
        if MediaFileParser.contains_other_season(name, season):
            return None
        meta = cls._platform_meta(name)
        if not meta:
            return None

        if not cls._has_explicit_media_title(meta):
            meta, default_episode = cls._meta_with_target_defaults(
                name, meta, mediainfo, season
            )
            if not meta:
                return None
        else:
            default_episode = None

        parsed_season = getattr(meta, "begin_season", None)
        episode = getattr(meta, "begin_episode", None) or default_episode
        if parsed_season is None or episode is None:
            return None
        try:
            parsed_season, episode = int(parsed_season), int(episode)
        except (TypeError, ValueError):
            return None
        if parsed_season != int(season) or episode <= 0:
            return None
        if not cls._matches_target_media(meta, mediainfo, season):
            return None
        return episode

    @staticmethod
    def episode_candidates(
            files: list,
            season: int,
            episodes: List[int],
            mediainfo: Any = None,
    ) -> Dict[int, List[Any]]:
        episode_list = list(dict.fromkeys(int(value) for value in episodes))
        if not episode_list:
            return {}

        target_episodes = set(episode_list)
        strict_matches = {episode: [] for episode in episode_list}
        loose_matches = {episode: [] for episode in episode_list}
        loosest_matches = {episode: [] for episode in episode_list}
        loose_patterns = {
            episode: (
                re.compile(rf"第\s*{episode}\s*集", re.IGNORECASE),
                re.compile(rf"[Ee][Pp]{episode}(?!\d)", re.IGNORECASE),
                re.compile(
                    rf"[\[\(\s\.\-_][Ee]0?{episode}[\]\)\s\.\-_]",
                    re.IGNORECASE,
                ),
            )
            for episode in episode_list
        }
        loosest_patterns = {
            episode: re.compile(
                rf"[\.\s\-_]0?{episode}[\.\s\-_]", re.IGNORECASE
            )
            for episode in episode_list
        }
        total_files = 0

        for item in MediaFileParser.iter_files(files):
            file_name = str(item.get("name") or "")
            total_files += 1
            if not MediaFileParser.is_video(file_name):
                continue
            found_episode = FileMatcher.episode_from_file(item, season, mediainfo)
            if found_episode is not None:
                if found_episode in target_episodes:
                    strict_matches[found_episode].append(item)
                continue
            # 有目标媒体时，平台无法识别标题或季集的文件不能进入转存。
            if mediainfo is not None:
                continue
            matches_season = MediaFileParser.matches_target_season(file_name, season)
            has_season_marker = bool(MediaFileParser.ANY_SEASON_PATTERN.search(file_name))
            for episode in episode_list:
                if any(pattern.search(file_name) for pattern in loose_patterns[episode]):
                    if season == 1 or matches_season or not has_season_marker:
                        loose_matches[episode].append(item)
                    continue
                if matches_season and loosest_patterns[episode].search(file_name):
                    loosest_matches[episode].append(item)

        results = {}
        for episode in episode_list:
            results[episode] = (
                    strict_matches[episode]
                    or loose_matches[episode]
                    or loosest_matches[episode]
            )
            if not results[episode] and total_files:
                logger.debug(f"S{season:02d}E{episode:02d} 未匹配到实际媒体文件")
        return results

    @staticmethod
    def movie_candidates(
            files: list, min_size_mb: int = 500, mediainfo: Any = None
    ) -> List[Any]:
        min_size = max(0, int(min_size_mb or 0)) * 1024 * 1024
        candidates = [
            item
            for item in MediaFileParser.iter_files(files)
            if MediaFileParser.is_video(str(item.get("name") or ""))
               and int(item.get("size") or 0) >= min_size
               and (mediainfo is None or FileMatcher.media_matches(item, mediainfo))
        ]
        candidates.sort(key=lambda item: int(item.get("size") or 0), reverse=True)
        return candidates
