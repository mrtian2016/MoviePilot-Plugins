"""
搜索处理模块
负责所有搜索相关逻辑：HDHive、Nullbr、PanSou
"""
from typing import Optional, List, Dict, Any

from app.core.config import settings
from app.log import logger
from app.schemas import MediaInfo
from app.schemas.types import MediaType

from ..utils import convert_nullbr_to_pansou_format


class SearchHandler:
    """搜索处理器"""

    def __init__(
        self,
        pansou_client,
        nullbr_client,
        hdhive_client,
        pansou_enabled: bool = False,
        nullbr_enabled: bool = False,
        hdhive_enabled: bool = False,
        hdhive_query_mode: str = "playwright",
        hdhive_username: str = "",
        hdhive_password: str = "",
        hdhive_cookie: str = "",
        only_115: bool = True,
        pansou_channels: str = ""
    ):
        """
        初始化搜索处理器

        :param pansou_client: PanSou 客户端实例
        :param nullbr_client: Nullbr 客户端实例
        :param hdhive_client: HDHive 客户端实例
        :param pansou_enabled: 是否启用 PanSou
        :param nullbr_enabled: 是否启用 Nullbr
        :param hdhive_enabled: 是否启用 HDHive
        :param hdhive_query_mode: HDHive 查询模式 (playwright/api)
        :param hdhive_username: HDHive 用户名
        :param hdhive_password: HDHive 密码
        :param hdhive_cookie: HDHive Cookie
        :param only_115: 是否只搜索115网盘资源
        :param pansou_channels: PanSou 搜索频道
        """
        self._pansou_client = pansou_client
        self._nullbr_client = nullbr_client
        self._hdhive_client = hdhive_client
        self._pansou_enabled = pansou_enabled
        self._nullbr_enabled = nullbr_enabled
        self._hdhive_enabled = hdhive_enabled
        self._hdhive_query_mode = hdhive_query_mode
        self._hdhive_username = hdhive_username
        self._hdhive_password = hdhive_password
        self._hdhive_cookie = hdhive_cookie
        self._only_115 = only_115
        self._pansou_channels = pansou_channels

    def get_enabled_sources(self) -> List[str]:
        """
        获取已启用且可用的搜索源列表，按优先级排序

        :return: 搜索源名称列表，按 Nullbr > HDHive > PanSou 排序
        """
        sources = []

        # Nullbr
        if self._nullbr_enabled and self._nullbr_client:
            sources.append("nullbr")

        # HDHive
        if self._hdhive_enabled:
            # HDHive 可以有同步客户端或仅配置用户名密码（用于 Playwright 模式）
            if self._hdhive_client or (self._hdhive_username and self._hdhive_password):
                sources.append("hdhive")

        # PanSou
        if self._pansou_enabled and self._pansou_client:
            sources.append("pansou")

        return sources

    def search_resources(
        self,
        mediainfo: MediaInfo,
        media_type: MediaType,
        season: Optional[int] = None
    ) -> List[Dict]:
        """
        统一的资源搜索方法，支持电影和电视剧
        按优先级尝试所有启用的搜索源，第一个有结果的就返回
        搜索优先级: Nullbr > HDHive > PanSou

        注意：此方法主要供电影订阅使用。电视剧订阅使用 search_single_source 进行逐源搜索。

        :param mediainfo: 媒体信息
        :param media_type: 媒体类型（MOVIE 或 TV）
        :param season: 季号（电视剧必需）
        :return: 115网盘资源列表
        """
        sources = self.get_enabled_sources()

        for source in sources:
            results = self.search_single_source(source, mediainfo, media_type, season)
            if results:
                return results
            else:
                # 打印回退日志
                remaining = sources[sources.index(source) + 1:]
                if remaining:
                    logger.info(f"{source.capitalize()} 未找到资源，将回退到 {'/'.join([s.capitalize() for s in remaining])} 搜索")

        return []

    def search_single_source(
        self,
        source: str,
        mediainfo: MediaInfo,
        media_type: MediaType,
        season: Optional[int] = None
    ) -> List[Dict]:
        """
        使用指定的单一搜索源查询资源

        :param source: 搜索源名称 ("nullbr", "hdhive", "pansou")
        :param mediainfo: 媒体信息
        :param media_type: 媒体类型
        :param season: 季号（电视剧时使用）
        :return: 115网盘资源列表
        """
        if source == "nullbr":
            return self._search_nullbr(mediainfo, media_type, season)
        elif source == "hdhive":
            return self._search_hdhive(mediainfo, media_type, season)
        elif source == "pansou":
            if media_type == MediaType.MOVIE:
                search_keyword = f"{mediainfo.title} {mediainfo.year}" if mediainfo.year else mediainfo.title
                logger.info(f"使用 PanSou 搜索电影资源: {mediainfo.title}")
                return self._pansou_search(search_keyword)
            else:
                return self._search_pansou_tv(mediainfo, season)
        else:
            logger.warning(f"未知的搜索源: {source}")
            return []

    def _pansou_search(self, keyword: str) -> List[Dict]:
        """
        PanSou 搜索的通用逻辑

        :param keyword: 搜索关键词
        :return: 115网盘资源列表
        """
        cloud_types = ["115"] if self._only_115 else None

        channels = None
        if self._pansou_channels and self._pansou_channels.strip():
            channels = [ch.strip() for ch in self._pansou_channels.split(',') if ch.strip()]

        search_results = self._pansou_client.search(
            keyword=keyword,
            cloud_types=cloud_types,
            channels=channels,
            limit=20
        )

        results = search_results.get("results", {}) if search_results and not search_results.get("error") else {}
        return results.get("115网盘", [])

    def _search_nullbr(
        self,
        mediainfo: MediaInfo,
        media_type: MediaType,
        season: Optional[int] = None
    ) -> List[Dict]:
        """
        仅使用 Nullbr 搜索资源

        :param mediainfo: 媒体信息
        :param media_type: 媒体类型（MOVIE 或 TV）
        :param season: 季号（电视剧时使用）
        :return: 115网盘资源列表
        """
        if not self._nullbr_client:
            logger.warning(f"Nullbr 客户端未初始化，跳过 Nullbr 查询")
            return []

        if not mediainfo.tmdb_id:
            logger.warning(f"{mediainfo.title} 缺少 TMDB ID，无法使用 Nullbr 查询")
            return []

        if media_type == MediaType.MOVIE:
            logger.info(f"使用 Nullbr 查询电影资源: {mediainfo.title} (TMDB ID: {mediainfo.tmdb_id})")
            nullbr_resources = self._nullbr_client.get_movie_resources(mediainfo.tmdb_id)
        else:  # MediaType.TV
            logger.info(f"使用 Nullbr 查询电视剧资源: {mediainfo.title} S{season} (TMDB ID: {mediainfo.tmdb_id})")
            nullbr_resources = self._nullbr_client.get_tv_resources(mediainfo.tmdb_id, season)

        if nullbr_resources:
            results = convert_nullbr_to_pansou_format(nullbr_resources)
            logger.info(f"Nullbr 找到 {len(results)} 个资源")
            return results

        logger.info(f"Nullbr 未找到资源")
        return []

    def _search_pansou_tv(
        self,
        mediainfo: MediaInfo,
        season: int
    ) -> List[Dict]:
        """
        仅使用 PanSou 搜索电视剧资源（带降级关键词策略）

        :param mediainfo: 媒体信息
        :param season: 季号
        :return: 115网盘资源列表
        """
        if not self._pansou_client:
            logger.warning(f"PanSou 客户端未初始化，跳过 PanSou 查询")
            return []

        # 电视剧使用降级搜索策略
        search_keywords = [
            f"{mediainfo.title}{season}",  # 中文季号格式
            mediainfo.title
        ]

        for keyword in search_keywords:
            logger.info(f"使用 PanSou 搜索电视剧资源: {mediainfo.title} S{season}，关键词: '{keyword}'")
            results = self._pansou_search(keyword)
            if results:
                logger.info(f"PanSou 关键词 '{keyword}' 搜索到 {len(results)} 个结果")
                return results
            else:
                logger.info(f"PanSou 关键词 '{keyword}' 无结果，尝试下一个降级关键词")

        logger.info(f"PanSou 未找到资源")
        return []

    def _search_hdhive(
        self,
        mediainfo: MediaInfo,
        media_type: MediaType,
        season: Optional[int] = None
    ) -> List[Dict]:
        """
        使用 HDHive 搜索资源
        根据配置的查询模式选择:
        - playwright: 使用 Playwright 浏览器模拟获取分享链接
        - api: 使用 Cookie 直接请求 API

        :param mediainfo: 媒体信息
        :param media_type: 媒体类型（MOVIE 或 TV）
        :param season: 季号（电视剧时使用）
        :return: 115网盘资源列表（统一格式）
        """
        from ..lib.hdhive import MediaType as HDHiveMediaType
        if not mediainfo.tmdb_id:
            logger.warning(f"{mediainfo.title} 缺少 TMDB ID，无法使用 HDHive 查询")
            return []

        hdhive_media_type = HDHiveMediaType.MOVIE if media_type == MediaType.MOVIE else HDHiveMediaType.TV

        # 根据配置的查询模式选择
        if self._hdhive_query_mode == "playwright":
            return self._search_hdhive_playwright(mediainfo, hdhive_media_type)
        else:  # api 模式
            return self._search_hdhive_api(mediainfo, hdhive_media_type)

    def _search_hdhive_playwright(self, mediainfo: MediaInfo, hdhive_media_type) -> List[Dict]:
        """
        使用 Playwright 浏览器模拟模式查询 HDHive 资源
        需要用户名和密码进行登录
        """
        if not self._hdhive_username or not self._hdhive_password:
            logger.warning("HDHive Playwright 模式需要配置用户名和密码")
            return []

        try:
            import asyncio
            from ..lib.hdhive import create_async_client as create_hdhive_async_client

            proxy = settings.PROXY

            logger.info(f"使用 HDHive (Playwright) 查询: {mediainfo.title} (TMDB ID: {mediainfo.tmdb_id})，代理：{proxy}")

            async def async_search():
                async with create_hdhive_async_client(
                    username=self._hdhive_username,
                    password=self._hdhive_password,
                    cookie=self._hdhive_cookie,
                    browser_type="chromium",
                    headless=True,
                    proxy=proxy
                ) as client:
                    # 获取媒体信息
                    media = await client.get_media_by_tmdb_id(mediainfo.tmdb_id, hdhive_media_type)
                    if not media:
                        return []

                    # 获取资源列表
                    resources_result = await client.get_resources(media.slug, hdhive_media_type, media_id=media.id)
                    if not resources_result or not resources_result.success:
                        return []

                    # 过滤免费的 115 资源并获取分享链接
                    free_115_resources = []
                    for res in resources_result.resources:
                        if hasattr(res, 'website') and res.website.value == '115' and res.is_free:
                            share_result = await client.get_share_url_by_click(res.slug)
                            if share_result and share_result.url:
                                free_115_resources.append({
                                    "url": share_result.url,
                                    "title": res.title,
                                    "update_time": ""
                                })

                    return free_115_resources

            # 运行异步任务
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                results = loop.run_until_complete(async_search())
            finally:
                loop.close()

            if results:
                logger.info(f"HDHive (Playwright) 找到 {len(results)} 个免费 115 资源")
            else:
                logger.info(f"HDHive (Playwright) 未找到免费 115 资源")
            return results

        except Exception as e:
            logger.error(f"HDHive (Playwright) 查询失败: {e}")
            return []

    def _search_hdhive_api(self, mediainfo: MediaInfo, hdhive_media_type) -> List[Dict]:
        """
        使用 API 模式（Cookie 直接请求）查询 HDHive 资源
        需要有效的 Cookie
        """
        if not self._hdhive_client:
            logger.warning("HDHive API 模式需要有效的 Cookie")
            return []

        try:
            logger.info(f"使用 HDHive (API) 查询: {mediainfo.title} (TMDB ID: {mediainfo.tmdb_id})")

            # 获取媒体信息
            with self._hdhive_client as client:
                media = client.get_media_by_tmdb_id(mediainfo.tmdb_id, hdhive_media_type)
                if not media:
                    logger.info(f"HDHive (API) 未找到媒体: {mediainfo.title}")
                    return []

                # 获取资源列表
                resources_result = client.get_resources(media.slug, hdhive_media_type, media_id=media.id)
                if not resources_result or not resources_result.success:
                    logger.info(f"HDHive (API) 获取资源列表失败: {mediainfo.title}")
                    return []

                # 过滤免费的 115 资源
                free_115_resources = []
                for res in resources_result.resources:
                    if hasattr(res, 'website') and res.website.value == '115' and res.is_free:
                        # 获取分享链接
                        share_result = client.get_share_url(res.slug)
                        if share_result and share_result.url:
                            free_115_resources.append({
                                "url": share_result.url,
                                "title": res.title,
                                "update_time": ""
                            })

                if free_115_resources:
                    logger.info(f"HDHive (API) 找到 {len(free_115_resources)} 个免费 115 资源")
                    return free_115_resources
                else:
                    logger.info(f"HDHive (API) 未找到免费 115 资源")
                    return []

        except Exception as e:
            logger.error(f"HDHive (API) 查询失败: {e}")
            return []
