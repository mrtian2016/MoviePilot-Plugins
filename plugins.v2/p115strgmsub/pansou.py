"""
PanSou 网盘搜索客户端
用于搜索各类网盘资源
"""
import re
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any

import requests

from app.log import logger


class PanSouClient:
    """网盘搜索客户端"""

    # 网盘类型中文名映射
    TYPE_NAMES = {
        "baidu": "百度网盘",
        "aliyun": "阿里云盘",
        "quark": "夸克网盘",
        "tianyi": "天翼云盘",
        "uc": "UC网盘",
        "mobile": "移动云盘",
        "115": "115网盘",
        "pikpak": "PikPak",
        "xunlei": "迅雷云盘",
        "123": "123云盘",
        "magnet": "磁力链接",
        "ed2k": "电驴链接"
    }

    def __init__(
            self,
            base_url: str,
            username: str = "",
            password: str = "",
            auth_enabled: bool = True
    ):
        """
        初始化 PanSou 客户端

        :param base_url: API 基础地址
        :param username: 用户名
        :param password: 密码
        :param auth_enabled: 是否启用认证
        """
        self.base_url = base_url.rstrip("/") if base_url else ""
        self.username = username
        self.password = password
        self.auth_enabled = auth_enabled
        self._token: Optional[str] = None
        self._token_expires: Optional[datetime] = None

    def _get_token(self) -> Optional[str]:
        """获取或刷新 Token"""
        if not self.base_url:
            return None

        if not self.auth_enabled:
            return None

        if not all([self.username, self.password]):
            logger.warning("PanSou 认证已启用但未配置用户名密码")
            return None

        # 检查 Token 是否有效（提前 5 分钟刷新）
        now = datetime.now()
        if self._token and self._token_expires:
            if now < self._token_expires - timedelta(minutes=5):
                return self._token

        # 登录获取新 Token
        try:
            login_url = f"{self.base_url}/api/auth/login"
            response = requests.post(
                login_url,
                json={"username": self.username, "password": self.password},
                timeout=10
            )

            if response.status_code == 200:
                data = response.json()
                self._token = data.get("token")
                expires_at = data.get("expires_at")
                if expires_at:
                    self._token_expires = datetime.fromtimestamp(expires_at)
                else:
                    self._token_expires = now + timedelta(hours=24)
                logger.debug("PanSou Token 获取成功")
                return self._token
            else:
                logger.error(f"PanSou 登录失败: HTTP {response.status_code}")
                return None

        except Exception as e:
            logger.error(f"PanSou 登录失败: {e}")
            return None

    def search(
            self,
            keyword: str,
            cloud_types: List[str] = None,
            channels: List[str] = None,
            limit: int = 10
    ) -> Dict[str, Any]:
        """
        搜索网盘资源

        :param keyword: 搜索关键词
        :param cloud_types: 网盘类型列表，如 ["115", "quark"]
        :param channels: TG搜索频道列表 
        :param limit: 每种网盘类型返回的结果数量限制
        :return: 搜索结果
        """
        if not keyword or not keyword.strip():
            return {
                "error": "搜索关键词不能为空",
                "keyword": keyword
            }

        keyword = keyword.strip()

        if not self.base_url:
            return {
                "error": "未配置 PanSou API 地址",
                "keyword": keyword
            }

        try:
            limit = min(max(int(limit) if limit else 10, 1), 20)
        except (ValueError, TypeError):
            limit = 10

        try:
            headers = {"Content-Type": "application/json"}

            # 如果启用认证，获取 Token
            if self.auth_enabled:
                token = self._get_token()
                if not token:
                    return {
                        "error": "PanSou API 认证失败，请检查用户名和密码配置",
                        "keyword": keyword
                    }
                headers["Authorization"] = f"Bearer {token}"

            # 构建请求参数
            search_url = f"{self.base_url}/api/search"
            payload = {
                "kw": keyword,
                "res": "results"
            }
            if channels:
                payload["channels"] = channels

            if cloud_types:
                payload["cloud_types"] = cloud_types

            logger.info(f"PanSou 搜索: {payload}")
            response = requests.post(search_url, json=payload, headers=headers, timeout=120)

            # Token 失效重试
            if response.status_code == 401 and self.auth_enabled:
                self._token = None
                self._token_expires = None

                token = self._get_token()
                if token:
                    headers["Authorization"] = f"Bearer {token}"
                    response = requests.post(search_url, json=payload, headers=headers, timeout=30)

            if response.status_code != 200:
                return {
                    "error": f"搜索请求失败: HTTP {response.status_code}",
                    "keyword": keyword
                }

            resp_data = response.json()

            # 检查响应状态码
            if resp_data.get("code") != 0:
                return {
                    "error": resp_data.get("message", "搜索失败"),
                    "keyword": keyword
                }

            # 获取 data 字段
            data = resp_data.get("data", {})
            total = data.get("total", 0)
            results_list = data.get("results", [])

            # 按网盘类型分组
            grouped_results = {}

            for item in results_list:
                title = item.get("title", "")
                # 清理 title 中的 HTML 标签
                title = re.sub(r'<[^>]+>', '', title)

                links = item.get("links", [])
                update_time = item.get("datetime", "")

                for link in links:
                    pan_type = link.get("type", "unknown")
                    type_display = self.TYPE_NAMES.get(pan_type, pan_type)

                    if type_display not in grouped_results:
                        grouped_results[type_display] = []

                    # 限制每种类型的数量
                    if len(grouped_results[type_display]) >= limit:
                        continue

                    link_item = {
                        "url": link.get("url", ""),
                        "title": title,
                        "update_time": update_time
                    }

                    # 如果有密码，添加密码字段
                    pwd = link.get("password", "")
                    if pwd:
                        link_item["password"] = pwd

                    grouped_results[type_display].append(link_item)

            # 按时间倒序排序
            for pan_type in grouped_results:
                grouped_results[pan_type].sort(
                    key=lambda x: x.get("update_time", ""),
                    reverse=True
                )

            # 计算总数
            total_count = sum(len(v) for v in grouped_results.values())

            return {
                "keyword": keyword,
                "total": total,
                "count": total_count,
                "results": grouped_results
            }

        except requests.exceptions.Timeout:
            return {
                "error": "搜索请求超时，请稍后重试",
                "keyword": keyword
            }
        except Exception as e:
            logger.error(f"搜索网盘资源失败: {str(e)}")
            return {
                "error": f"搜索网盘资源失败: {str(e)}",
                "keyword": keyword
            }

    def search_115(self, keyword: str, limit: int = 10) -> List[Dict[str, Any]]:
        """
        专门搜索 115 网盘资源

        :param keyword: 搜索关键词
        :param limit: 结果数量限制
        :return: 115 网盘资源列表
        """
        result = self.search(keyword=keyword, cloud_types=["115"], limit=limit)

        if result.get("error"):
            logger.error(f"搜索 115 资源失败: {result.get('error')}")
            return []

        return result.get("results", {}).get("115网盘", [])
