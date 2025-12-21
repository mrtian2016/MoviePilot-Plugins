"""
Nullbr 资源查询客户端
通过 TMDB ID 获取 115 网盘资源
"""
from typing import Optional, List, Dict, Any

import requests

from app.log import logger


class NullbrClient:
    """Nullbr 资源查询客户端"""

    BASE_URL = "https://api.nullbr.eu.org"

    def __init__(self, app_id: str, api_key: str):
        """
        初始化 Nullbr 客户端

        :param app_id: Nullbr APP ID
        :param api_key: Nullbr API Key
        """
        self.app_id = app_id
        self.api_key = api_key
        self.headers = {
            "User-Agent": "MoviePilot/p115strgmsub",
            "Content-Type": "application/json",
            "X-APP-ID": app_id,
            "X-API-KEY": api_key
        }

    def get_movie_resources(self, tmdb_id: int) -> List[Dict[str, Any]]:
        """
        获取电影的 115 网盘资源

        :param tmdb_id: TMDB 电影 ID
        :return: 资源列表
        """
        if not self.api_key or not self.app_id:
            missing = []
            if not self.app_id:
                missing.append("APP ID")
            if not self.api_key:
                missing.append("API Key")
            logger.error(f"❌ Nullbr 缺少必要配置：{', '.join(missing)}，请在插件设置中配置")
            return []

        try:
            url = f"{self.BASE_URL}/movie/{tmdb_id}/115"

            response = requests.get(
                url,
                headers=self.headers,
                timeout=30
            )

            if response.status_code == 200:
                data = response.json()
                # 响应格式: {"115": [...], "id": 1396, "page": 1, "total_page": 1, "media_type": "movie"}
                resources = data.get("115", [])
                if resources:
                    logger.info(f"Nullbr 获取电影 {tmdb_id} 资源成功，共 {len(resources)} 个")
                    return resources
                else:
                    logger.info(f"Nullbr 未找到电影 {tmdb_id} 的资源")
                    return []
            elif response.status_code == 401:
                logger.error("Nullbr API Key 或 APP ID 无效或已过期")
                return []
            elif response.status_code == 404:
                logger.info(f"Nullbr 未找到电影 {tmdb_id} 的资源")
                return []
            else:
                logger.warning(f"Nullbr 请求失败: HTTP {response.status_code}")
                return []

        except requests.exceptions.Timeout:
            logger.error("Nullbr 请求超时")
            return []
        except Exception as e:
            logger.error(f"Nullbr 获取电影资源出错: {e}")
            return []

    def get_tv_resources(self, tmdb_id: int, season: int = None) -> List[Dict[str, Any]]:
        """
        获取电视剧的 115 网盘资源

        :param tmdb_id: TMDB 电视剧 ID
        :param season: 季号（可选，用于过滤返回结果）
        :return: 资源列表
        """
        if not self.api_key or not self.app_id:
            missing = []
            if not self.app_id:
                missing.append("APP ID")
            if not self.api_key:
                missing.append("API Key")
            logger.error(f"❌ Nullbr 缺少必要配置：{', '.join(missing)}，请在插件设置中配置")
            return []

        try:
            url = f"{self.BASE_URL}/tv/{tmdb_id}/115"

            response = requests.get(
                url,
                headers=self.headers,
                timeout=30
            )

            if response.status_code == 200:
                data = response.json()
                # 响应格式: {"115": [...], "id": 1396, "page": 1, "total_page": 1, "media_type": "tv"}
                all_resources = data.get("115", [])
                
                if not all_resources:
                    logger.info(f"Nullbr 未找到电视剧 {tmdb_id} 的资源")
                    return []
                
                # 如果指定了季号，过滤包含该季的资源
                if season:
                    season_str = f"S{season}"
                    filtered_resources = [
                        r for r in all_resources
                        if season_str in r.get("season_list", [])
                    ]
                    logger.info(f"Nullbr 获取电视剧 {tmdb_id} S{season} 资源成功，共 {len(filtered_resources)} 个")
                    return filtered_resources
                else:
                    logger.info(f"Nullbr 获取电视剧 {tmdb_id} 所有资源成功，共 {len(all_resources)} 个")
                    return all_resources
                    
            elif response.status_code == 401:
                logger.error("Nullbr API Key 或 APP ID 无效或已过期")
                return []
            elif response.status_code == 404:
                logger.info(f"Nullbr 未找到电视剧 {tmdb_id} 的资源")
                return []
            else:
                logger.warning(f"Nullbr 请求失败: HTTP {response.status_code}")
                return []

        except requests.exceptions.Timeout:
            logger.error("Nullbr 请求超时")
            return []
        except Exception as e:
            logger.error(f"Nullbr 获取电视剧资源出错: {e}")
            return []

    def check_connection(self) -> bool:
        """
        检查 API 连接状态

        :return: 是否连接成功
        """
        if not self.api_key or not self.app_id:
            missing = []
            if not self.app_id:
                missing.append("APP ID")
            if not self.api_key:
                missing.append("API Key")
            logger.warning(f"⚠️ Nullbr 连接检查失败：缺少{', '.join(missing)}")
            return False

        try:
            # 使用一个知名电影的 TMDB ID 来测试连接（例如：肖申克的救赎 278）
            url = f"{self.BASE_URL}/movie/278/115"

            response = requests.get(
                url,
                headers=self.headers,
                timeout=10
            )

            # 200 表示成功，404 表示未找到但连接正常
            return response.status_code in [200, 404]

        except Exception as e:
            logger.error(f"Nullbr 连接检查失败: {e}")
            return False
