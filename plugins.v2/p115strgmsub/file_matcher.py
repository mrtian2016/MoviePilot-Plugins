"""
文件匹配模块
负责剧集文件的匹配和网盘已存在集数的检查
"""
import re
from pathlib import Path
from typing import List, Optional, Set
from app.core.metainfo import MetaInfo
from app.schemas import MediaInfo
from app.log import logger


class FileMatcher:
    """文件匹配器类"""
    
    # 视频文件扩展名
    VIDEO_EXTENSIONS = {'.mkv', '.mp4', '.avi', '.rmvb', '.wmv', '.flv', '.ts', '.m2ts'}
    
    @staticmethod
    def _contains_other_season(file_name: str, target_season: int) -> bool:
        """
        检查文件名是否明确包含其他季的标识

        :param file_name: 文件名
        :param target_season: 目标季号
        :return: 是否包含其他季标识
        """
        # 匹配 S01、S02 等格式，检查是否为其他季
        season_match = re.search(r'[Ss](\d{1,2})[Ee]', file_name)
        if season_match:
            found_season = int(season_match.group(1))
            if found_season != target_season:
                return True

        # 匹配 "第X季" 格式
        cn_season_match = re.search(r'第\s*(\d{1,2})\s*季', file_name)
        if cn_season_match:
            found_season = int(cn_season_match.group(1))
            if found_season != target_season:
                return True

        # 匹配 Season X 格式
        en_season_match = re.search(r'[Ss]eason\s*(\d{1,2})', file_name, re.IGNORECASE)
        if en_season_match:
            found_season = int(en_season_match.group(1))
            if found_season != target_season:
                return True

        return False

    @staticmethod
    def _matches_target_season(file_name: str, target_season: int) -> bool:
        """
        检查文件名是否明确匹配目标季

        :param file_name: 文件名
        :param target_season: 目标季号
        :return: 是否匹配目标季
        """
        # 匹配 S01、S02 等格式
        season_match = re.search(r'[Ss](\d{1,2})[Ee]', file_name)
        if season_match:
            found_season = int(season_match.group(1))
            return found_season == target_season

        # 匹配 "第X季" 格式
        cn_season_match = re.search(r'第\s*(\d{1,2})\s*季', file_name)
        if cn_season_match:
            found_season = int(cn_season_match.group(1))
            return found_season == target_season

        # 匹配 Season X 格式
        en_season_match = re.search(r'[Ss]eason\s*(\d{1,2})', file_name, re.IGNORECASE)
        if en_season_match:
            found_season = int(en_season_match.group(1))
            return found_season == target_season

        return False

    @staticmethod
    def match_episode_file(
        files: List[dict],
        title: str,
        season: int,
        episode: int
    ) -> Optional[dict]:
        """
        匹配剧集文件

        :param files: 文件列表
        :param title: 剧集标题
        :param season: 季号
        :param episode: 集号
        :return: 匹配的文件信息
        """
        # 严格模式：必须包含季号的匹配模式
        strict_patterns = [
            # S01E01 格式 (最常见且准确)
            rf'[Ss]0?{season}[Ee]0?{episode}(?!\d)',
        ]

        # 宽松模式：不包含季号的匹配模式（需要额外验证）
        loose_patterns = [
            # 第1集 格式
            rf'第\s*0?{episode}\s*集',
            # EP01 格式
            rf'[Ee][Pp]0?{episode}(?!\d)',
            # E01格式（开头或特定位置）
            rf'[\[\(\s\.\-_][Ee]0?{episode}[\]\)\s\.\-_]',
        ]

        # 最宽松模式：纯数字匹配（风险较高，仅作为最后手段）
        # 仅当文件名明确匹配目标季或无季号标识时使用
        loosest_patterns = [
            # .01. 格式
            rf'[\.\s\-_]0?{episode}[\.\s\-_]',
        ]

        # 收集候选文件，按匹配优先级排序
        strict_matches = []
        loose_matches = []
        loosest_matches = []

        for file in files:
            file_name = file.get("name", "")
            is_dir = file.get("is_dir", False)

            # 跳过目录（但可以递归处理子文件）
            if is_dir:
                sub_files = file.get("children", [])
                if sub_files:
                    matched = FileMatcher.match_episode_file(sub_files, title, season, episode)
                    if matched:
                        return matched
                continue

            # 检查文件扩展名
            ext = Path(file_name).suffix.lower()
            if ext not in FileMatcher.VIDEO_EXTENSIONS:
                continue

            # 如果明确包含其他季的标识，直接跳过
            if FileMatcher._contains_other_season(file_name, season):
                continue

            # 严格模式匹配
            for pattern in strict_patterns:
                if re.search(pattern, file_name, re.IGNORECASE):
                    strict_matches.append(file)
                    break
            else:
                # 宽松模式匹配：需要确保没有其他季标识
                for pattern in loose_patterns:
                    if re.search(pattern, file_name, re.IGNORECASE):
                        # 额外检查：如果是第一季，或者文件名明确匹配目标季
                        if season == 1 or FileMatcher._matches_target_season(file_name, season):
                            loose_matches.append(file)
                        # 如果文件名没有任何季号标识，也接受（可能是单季剧）
                        elif not re.search(r'[Ss]\d+[Ee]|第\s*\d+\s*季|[Ss]eason\s*\d+', file_name, re.IGNORECASE):
                            loose_matches.append(file)
                        break
                else:
                    # 最宽松模式：仅当文件名明确匹配目标季时使用
                    if FileMatcher._matches_target_season(file_name, season):
                        for pattern in loosest_patterns:
                            if re.search(pattern, file_name, re.IGNORECASE):
                                loosest_matches.append(file)
                                break

        # 按优先级返回匹配结果
        if strict_matches:
            return strict_matches[0]
        if loose_matches:
            return loose_matches[0]
        if loosest_matches:
            return loosest_matches[0]

        return None

    @staticmethod
    def match_movie_file(
        files: List[dict],
        title: str,
        min_size_mb: int = 500
    ) -> Optional[dict]:
        """
        匹配电影文件（查找最大的视频文件）

        :param files: 文件列表
        :param title: 电影标题
        :param min_size_mb: 最小文件大小（MB），用于过滤小文件
        :return: 匹配的文件信息
        """
        candidates = []
        min_size_bytes = min_size_mb * 1024 * 1024

        def collect_video_files(file_list: List[dict]):
            """递归收集所有视频文件"""
            for file in file_list:
                file_name = file.get("name", "")
                is_dir = file.get("is_dir", False)

                if is_dir:
                    sub_files = file.get("children", [])
                    if sub_files:
                        collect_video_files(sub_files)
                    continue

                # 检查文件扩展名
                ext = Path(file_name).suffix.lower()
                if ext not in FileMatcher.VIDEO_EXTENSIONS:
                    continue

                # 检查文件大小
                file_size = file.get("size", 0)
                if file_size < min_size_bytes:
                    continue

                candidates.append(file)

        collect_video_files(files)

        if not candidates:
            return None

        # 按文件大小降序排序，返回最大的文件
        candidates.sort(key=lambda x: x.get("size", 0), reverse=True)
        return candidates[0]

    @staticmethod
    def check_existing_episodes(
        p115_manager,
        mediainfo: MediaInfo,
        season: int,
        save_dir: str
    ) -> Set[int]:
        """
        检查115网盘目录中已存在的剧集集数

        :param p115_manager: 115客户端管理器
        :param mediainfo: 媒体信息
        :param season: 季号
        :param save_dir: 网盘保存目录
        :return: 已存在的集数集合
        """
        existing_episodes = set()

        if not p115_manager:
            return existing_episodes

        try:
            # 列出网盘目录中的文件
            files = p115_manager.list_files(save_dir)
            if not files:
                logger.debug(f"网盘目录为空或不存在: {save_dir}")
                return existing_episodes

            logger.info(f"检查网盘目录 {save_dir}，共 {len(files)} 个文件")

            # 使用MetaInfo识别每个文件的集数
            for file_info in files:
                file_name = file_info.get("name", "")
                is_dir = file_info.get("fid", 0) == 0  # fid=0表示目录

                # 跳过目录
                if is_dir:
                    continue

                # 检查是否为视频文件
                file_ext = Path(file_name).suffix.lower()
                if file_ext not in FileMatcher.VIDEO_EXTENSIONS:
                    continue

                # 检查是否包含其他季的标识，如果是则跳过
                if FileMatcher._contains_other_season(file_name, season):
                    logger.debug(f"跳过其他季文件: {file_name}")
                    continue

                # 使用MetaInfo识别文件信息
                meta = MetaInfo(file_name)

                # 检查季号是否匹配
                # 情况1: 文件名包含季号且匹配目标季
                # 情况2: 文件名无季号（meta.begin_season 为 None），视为当前目录对应的季
                #        因为 save_dir 已经是 Season X 目录，文件应该属于该季
                season_matches = (
                    (meta.begin_season is not None and meta.begin_season == season) or
                    (meta.begin_season is None and not FileMatcher._contains_other_season(file_name, season))
                )

                if season_matches and meta.begin_episode:
                    existing_episodes.add(meta.begin_episode)
                    logger.debug(f"识别到已存在集数: {file_name} -> S{season:02d}E{meta.begin_episode:02d}")

                    # 如果是剧集范围（如E01-E03），添加所有集数
                    if meta.end_episode and meta.end_episode != meta.begin_episode:
                        for ep in range(meta.begin_episode, meta.end_episode + 1):
                            existing_episodes.add(ep)

            if existing_episodes:
                logger.info(f"{mediainfo.title} S{season} 网盘已存在 {len(existing_episodes)} 集: {sorted(existing_episodes)}")
            else:
                logger.info(f"{mediainfo.title} S{season} 网盘目录中未找到该季剧集")

        except Exception as e:
            logger.error(f"检查网盘目录失败: {e}")

        return existing_episodes
