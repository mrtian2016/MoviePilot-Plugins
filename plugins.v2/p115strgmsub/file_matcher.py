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
        # 常见的剧集命名模式
        patterns = [
            # S01E01 格式
            rf'[Ss]0?{season}[Ee]0?{episode}(?!\d)',
            # 第1集 格式
            rf'第\s*0?{episode}\s*集',
            # EP01 格式
            rf'[Ee][Pp]?0?{episode}(?!\d)',
            # .01. 格式 (需要更严格匹配)
            rf'[\.\s\-_]0?{episode}[\.\s\-_]',
            # E01格式（开头或特定位置）
            rf'[\[\(\s\.\-_][Ee]0?{episode}[\]\)\s\.\-_]',
        ]
        
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
            
            # 匹配集数
            for pattern in patterns:
                if re.search(pattern, file_name, re.IGNORECASE):
                    return file
        
        return None
    
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
                
                # 使用MetaInfo识别文件信息
                meta = MetaInfo(file_name)
                
                # 检查季号是否匹配
                if meta.begin_season and meta.begin_season == season:
                    # 获取集数
                    if meta.begin_episode:
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
