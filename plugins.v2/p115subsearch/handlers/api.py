"""
API 处理模块
负责插件的外部 API 接口
"""
from typing import Callable

from app.core.config import settings
from app.log import logger


class ApiHandler:
    """API 处理器"""

    def __init__(
        self,
        pansou_client,
        p115_manager,
        only_115: bool = True,
        save_path: str = "",
        get_data_func: Callable = None,
        save_data_func: Callable = None
    ):
        """
        初始化 API 处理器

        :param pansou_client: PanSou 客户端实例
        :param p115_manager: 115 客户端管理器
        :param only_115: 是否只搜索115网盘资源
        :param save_path: 默认转存目录
        :param get_data_func: 获取数据的函数
        :param save_data_func: 保存数据的函数
        """
        self._pansou_client = pansou_client
        self._p115_manager = p115_manager
        self._only_115 = only_115
        self._save_path = save_path
        self._get_data = get_data_func
        self._save_data = save_data_func

    def search(self, keyword: str, apikey: str) -> dict:
        """
        API: 搜索网盘资源

        :param keyword: 搜索关键词
        :param apikey: API 密钥
        :return: 搜索结果
        """
        if apikey != settings.API_TOKEN:
            return {"error": "API密钥错误"}

        if not self._pansou_client:
            return {"error": "PanSou 客户端未初始化"}

        cloud_types = ["115"] if self._only_115 else None
        return self._pansou_client.search(keyword=keyword, cloud_types=cloud_types, limit=10)

    def transfer(self, share_url: str, save_path: str, apikey: str) -> dict:
        """
        API: 转存分享链接

        :param share_url: 分享链接
        :param save_path: 转存路径
        :param apikey: API 密钥
        :return: 转存结果
        """
        if apikey != settings.API_TOKEN:
            return {"success": False, "error": "API密钥错误"}

        if not self._p115_manager:
            return {"success": False, "error": "115 客户端未初始化"}

        success = self._p115_manager.transfer_share(share_url, save_path or self._save_path)
        return {"success": success}

    def clear_history(self, apikey: str) -> dict:
        """
        API: 清空历史记录

        :param apikey: API 密钥
        :return: 操作结果
        """
        if apikey != settings.API_TOKEN:
            return {"success": False, "message": "API密钥错误"}

        if self._save_data:
            self._save_data('history', [])
        logger.info("115网盘订阅搜索历史记录已清空")
        return {"success": True, "message": "历史记录已清空"}

    def list_directories(self, path: str = "/", apikey: str = "") -> dict:
        """
        API: 列出115网盘指定路径下的目录

        :param path: 目录路径
        :param apikey: API 密钥
        :return: 目录列表
        """
        if apikey != settings.API_TOKEN:
            return {"success": False, "error": "API密钥错误"}

        if not self._p115_manager:
            return {"success": False, "error": "115客户端未初始化"}

        try:
            directories = self._p115_manager.list_directories(path)

            # 构建面包屑导航
            breadcrumbs = []
            if path and path != "/":
                parts = [p for p in path.split("/") if p]
                current_path = ""
                breadcrumbs.append({"name": "根目录", "path": "/"})
                for part in parts:
                    current_path = f"{current_path}/{part}"
                    breadcrumbs.append({"name": part, "path": current_path})
            else:
                breadcrumbs.append({"name": "根目录", "path": "/"})

            return {
                "success": True,
                "path": path,
                "breadcrumbs": breadcrumbs,
                "directories": directories
            }
        except Exception as e:
            logger.error(f"列出115目录失败: {e}")
            return {"success": False, "error": str(e)}
