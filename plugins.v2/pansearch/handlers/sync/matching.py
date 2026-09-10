"""网盘分享文件的电影/剧集匹配。"""

from pathlib import Path
from typing import Dict, List

from app.log import logger
from app.schemas import MediaInfo

from ...core import OwnerDelegator
from ...utils import FileMatcher


class FileMatchingService(OwnerDelegator):
    """只负责从已读取的文件树中选择媒体文件。"""

    def _match_episode_files(
            self,
            files: list,
            mediainfo: MediaInfo,
            subscribe,
            season: int,
            episodes: List[int],
            require_media_match: bool = True,
    ) -> Dict[int, tuple]:
        """按结构收集剧集候选，再使用规则组选择文件。"""
        episode_list = list(dict.fromkeys(int(value) for value in episodes))
        candidates = FileMatcher.episode_candidates(
            files,
            season,
            episode_list,
            mediainfo=mediainfo if require_media_match else None,
        )
        results = {}
        for episode in episode_list:
            episode_candidates = candidates.get(episode) or []
            selected, priority = self._search_handler.select_file_candidate(
                episode_candidates, mediainfo, subscribe
            )
            results[episode] = selected, priority
            if selected and len(episode_candidates) > 1:
                selected_path = str(
                    selected.get("_relative_path")
                    or selected.get("name")
                    or ""
                ).strip()
                logger.debug(
                    f"{mediainfo.title_year} S{season:02d}E{episode:02d} "
                    f"存在 {len(episode_candidates)} 个版本，平台规则选择："
                    f"{selected_path}（优先级 {priority}）"
                )
        return results

    def _match_movie_file(
            self,
            files: list,
            mediainfo: MediaInfo,
            subscribe,
            resource_title: str = "",
            require_media_match: bool = True,
    ) -> tuple:
        """按媒体文件结构收集电影候选，再使用规则组选择。"""
        matched = self._search_handler.select_file_candidate(
            FileMatcher.movie_candidates(
                files,
                mediainfo=mediainfo if require_media_match else None,
            ),
            mediainfo,
            subscribe,
        )
        if matched[0] or not require_media_match:
            return matched if matched[0] else (None, 0)

        # 跨盘文件名被网盘混淆时，仅允许“资源标题匹配 + 唯一大视频”兜底。
        fallback = FileMatcher.movie_candidates(files)
        if (
                len(fallback) != 1
                or not FileMatcher.media_name_matches(resource_title, mediainfo)
        ):
            return None, 0
        actual = fallback[0]
        scoring_item = dict(actual)
        scoring_item["name"] = (
            f"{str(resource_title).strip()}{Path(str(actual.get('name') or '')).suffix}"
        )
        selected, score = self._search_handler.select_file_candidate(
            [scoring_item], mediainfo, subscribe
        )
        return (actual, score) if selected else (None, 0)
