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
        hdhive_username: str = "",
        hdhive_password: str = "",
        hdhive_cookie: str = "",
        hdhive_query_mode: str = "api",
        hdhive_api_key: str = "",
        hdhive_auto_unlock: bool = False,
        hdhive_max_unlock_points: int = 50,
        hdhive_max_points_per_sub: int = 20,
        only_115: bool = True,
        pansou_channels: str = ""
    ):
        """
        初始化搜索处理器

        :param pansou_client: PanSou 客户端实例
        :param nullbr_client: Nullbr 客户端实例
        :param hdhive_client: HDHive 客户端实例（保留兼容，实际 Playwright 模式不使用）
        :param pansou_enabled: 是否启用 PanSou
        :param nullbr_enabled: 是否启用 Nullbr
        :param hdhive_enabled: 是否启用 HDHive
        :param hdhive_username: HDHive 用户名
        :param hdhive_password: HDHive 密码
        :param hdhive_cookie: HDHive Cookie
        :param hdhive_query_mode: HDHive 查询模式
        :param hdhive_api_key: HDHive API Key
        :param hdhive_auto_unlock: 是否自动解锁 HDHive 资源
        :param only_115: 是否只搜索115网盘资源
        :param pansou_channels: PanSou 搜索频道
        """
        self._pansou_client = pansou_client
        self._nullbr_client = nullbr_client
        self._hdhive_client = hdhive_client
        self._pansou_enabled = pansou_enabled
        self._nullbr_enabled = nullbr_enabled
        self._hdhive_enabled = hdhive_enabled
        self._hdhive_username = hdhive_username
        self._hdhive_password = hdhive_password
        self._hdhive_cookie = hdhive_cookie
        self._hdhive_query_mode = hdhive_query_mode
        self._hdhive_api_key = hdhive_api_key
        self._hdhive_auto_unlock = hdhive_auto_unlock
        self._hdhive_max_unlock_points = hdhive_max_unlock_points
        self._hdhive_max_points_per_sub = hdhive_max_points_per_sub
        self._current_spent_points = 0
        self._sub_spent_points = 0
        self._current_sub_key = ""
        self._get_data_func = None
        self._save_data_func = None
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
            if self._hdhive_query_mode == "playwright" and self._hdhive_username and self._hdhive_password:
                sources.append("hdhive")
            elif self._hdhive_query_mode == "api" and self._hdhive_api_key:
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
                return self._search_pansou_movie(mediainfo)
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
    def _filter_pansou_results_by_title(
        self,
        results: List[Dict],
        mediainfo: MediaInfo,
        media_type: MediaType,
        season: Optional[int] = None
    ) -> List[Dict]:
        """
        过滤 PanSou 搜索结果，确保标题、年份、季号匹配

        :param results: PanSou 搜索结果
        :param mediainfo: 媒体信息
        :param media_type: 媒体类型
        :param season: 季号（电视剧）
        :return: 过滤后的结果
        """
        if not results or not mediainfo:
            return results

        filtered = []
        for result in results:
            title = result.get("title", "") or result.get("name", "") or ""
            url = result.get("url", "") or ""

            # 合并标题和 URL 进行检查
            combined_text = f"{title} {url}".lower()

            # 检查标题是否包含媒体标题的关键字（移除空格后匹配）
            title_keywords = mediainfo.title.lower().replace(" ", "")
            if title_keywords not in combined_text.replace(" ", ""):
                logger.debug(f"PanSou 结果标题不匹配: {title} (期望: {mediainfo.title})，跳过")
                continue

            # 电影必须检查年份
            if media_type == MediaType.MOVIE and mediainfo.year:
                year_str = str(mediainfo.year)
                if year_str not in combined_text:
                    logger.debug(f"PanSou 电影结果年份不匹配: {title} (期望年份: {year_str})，跳过")
                    continue

            # 电视剧必须检查季号
            if media_type == MediaType.TV and season:
                season_patterns = [
                    f"s{season}",
                    f"s{season:02d}",
                    f"第{season}季",
                    f"第{season}辑",
                    f"{season}季",
                ]
                if not any(p in combined_text for p in season_patterns):
                    logger.debug(f"PanSou 电视剧结果季号不匹配: {title} (期望: S{season})，跳过")
                    continue

            filtered.append(result)
            logger.debug(f"PanSou 结果通过过滤: {title}")

        if len(filtered) < len(results):
            logger.info(f"PanSou 搜索过滤: {len(filtered)}/{len(results)} 条结果通过标题/年份/季号校验")

        return filtered



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

    def _search_pansou_movie(
        self,
        mediainfo: MediaInfo,
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
            f"{mediainfo.title} {mediainfo.year}",
            mediainfo.title
        ]

        for keyword in search_keywords:
            logger.info(f"使用 PanSou 搜索电影资源: {mediainfo.title}，关键词: '{keyword}'")
            results = self._pansou_search(keyword)
            if results:
                logger.info(f"PanSou 关键词 '{keyword}' 搜索到 {len(results)} 个结果")
                # 添加标题+年份过滤
                filtered = self._filter_pansou_results_by_title(
                    results, mediainfo, MediaType.MOVIE
                )
                if filtered:
                    return filtered
            else:
                logger.info(f"PanSou 关键词 '{keyword}' 无结果，尝试下一个降级关键词")

        logger.info(f"PanSou 未找到资源")
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
                # 添加标题+年份+季号过滤
                filtered = self._filter_pansou_results_by_title(
                    results, mediainfo, MediaType.TV, season
                )
                if filtered:
                    return filtered
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

        hdhive_media_type = "movie" if media_type == MediaType.MOVIE else "tv"

        if self._hdhive_query_mode == "playwright":
            return self._search_hdhive_playwright(mediainfo, HDHiveMediaType.MOVIE if media_type == MediaType.MOVIE else HDHiveMediaType.TV)
        else:
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

    def _search_hdhive_api(self, mediainfo: MediaInfo, hdhive_media_type: str) -> List[Dict]:
        """
        使用 API 模式查询 HDHive 资源
        需要有效的 API Key
        """
        if not self._hdhive_api_key:
            logger.warning("HDHive API 模式需要有效的 API Key")
            return []

        try:
            import requests
            logger.info(f"使用 HDHive (API) 查询: {mediainfo.title} (TMDB ID: {mediainfo.tmdb_id})")

            headers = {
                "X-API-Key": self._hdhive_api_key,
                "Content-Type": "application/json"
            }

            # 1. 获取资源列表
            url = f"https://hdhive.com/api/open/resources/{hdhive_media_type}/{mediainfo.tmdb_id}"
            proxy = settings.PROXY
            proxies = proxy if isinstance(proxy, dict) else {"http": proxy, "https": proxy} if proxy else None

            res = requests.get(url, headers=headers, proxies=proxies, timeout=15)
            logger.info(f"HDHive (API) GET {url} 返回状态: {res.status_code}")
            logger.info(f"HDHive (API) 响应内容: {res.text}")
            
            if res.status_code != 200:
                logger.info(f"HDHive (API) 获取资源失败, status: {res.status_code}")
                return []

            data = res.json()
            if not data.get("success") or not data.get("data"):
                logger.info(f"HDHive (API) 未找到资源: {mediainfo.title}, 返回数据: {data}")
                return []

            resources = data.get("data", [])
            
            free_115_resources = []
            resources = [ resource for resource in resources if resource.get("pan_type") == "115" ]
            logger.info(f"HDHive (API) 找到 {len(resources)} 个115网盘资源，开始过滤...")
            for resource in resources:
                # 过滤出可能是115网盘的且免费的资源 (0或者null)，或者如果开启了自动解锁，则都可以尝试
                
                # # 1. 前置过滤：根据 website 属性判断是否是 115 网盘
                # website = resource.get("website")
                # if isinstance(website, dict):
                #     website_value = str(website.get("value", ""))
                #     if website_value and website_value != "115":
                #         logger.info(f"HDHive (API) 资源 {resource.get('title')} 的 website_value 不是 115 ({website_value})，跳过")
                #         continue
                # elif isinstance(website, str):
                #     if website and website != "115":
                #         logger.info(f"HDHive (API) 资源 {resource.get('title')} 的 website 不是 115 ({website})，跳过")
                #         continue
                
                # 2. 免费策略/积分判断
                unlock_points = resource.get("unlock_points")
                is_free = unlock_points is None or unlock_points == 0 or resource.get("is_unlocked")
                
                logger.info(f"HDHive (API) 处理资源: title='{resource.get('title')}', slug='{resource.get('slug')}', unlock_points={unlock_points}, is_unlocked={resource.get('is_unlocked')}, is_free={is_free}")

                # 单个资源积分超出单订阅预算上限的，搜索阶段直接过滤掉（这些无论如何都解锁不了）
                if not is_free and unlock_points is not None:
                    if unlock_points > self._hdhive_max_points_per_sub:
                        logger.info(f"HDHive (API) 资源 {resource.get('title')} 单次解锁积分 ({unlock_points}) 超出单订阅预算上限 ({self._hdhive_max_points_per_sub})，跳过")
                        continue
                    if unlock_points > self._hdhive_max_unlock_points:
                        logger.info(f"HDHive (API) 资源 {resource.get('title')} 单次解锁积分 ({unlock_points}) 超出全局预算上限 ({self._hdhive_max_unlock_points})，跳过")
                        continue

                if is_free or self._hdhive_auto_unlock:
                    slug = resource.get("slug")
                    if not slug:
                        logger.info(f"HDHive (API) 资源缺少 slug，跳过: {resource}")
                        continue

                    # 如果免费，则直接解锁并获取链接
                    if is_free:
                        unlock_url = "https://hdhive.com/api/open/resources/unlock"
                        logger.info(f"HDHive (API) 尝试免费解锁资源: {slug}")
                        unlock_res = requests.post(unlock_url, json={"slug": slug}, headers=headers, proxies=proxies, timeout=15)
                        
                        logger.info(f"HDHive (API) 解锁 {slug} 返回状态: {unlock_res.status_code}, 内容: {unlock_res.text}")
                        
                        if unlock_res.status_code == 200:
                            unlock_data = unlock_res.json()
                            if unlock_data.get("success") and unlock_data.get("data"):
                                share_url = unlock_data["data"].get("full_url", "")
                                logger.info(f"HDHive (API) 解锁数据: {unlock_data}")
                                logger.info(f"HDHive (API) 成功解锁免费资源, 分享链接: {share_url}")
                                free_115_resources.append({
                                    "url": share_url,
                                    "title": resource.get("title", ""),
                                    "update_time": resource.get("created_at", ""),
                                    "is_official": bool(resource.get("is_official"))
                                })
                            else:
                                logger.error(f"HDHive (API) 解锁失败，返回数据异常或非成功: {unlock_data}")
                        else:
                            logger.error(f"HDHive (API) 解锁请求失败，状态码: {unlock_res.status_code}")
                    else:
                        # 对于非免费资源，延迟解锁：返回标记并携带 slug，后续供 SyncHandler 按需调用 unlock
                        logger.info(f"HDHive (API) 收费资源将其加入列表延迟解锁: {slug}")
                        free_115_resources.append({
                            "url": "",  # 此时没有真正的链接
                            "title": resource.get("title", ""),
                            "update_time": resource.get("created_at", ""),
                            "slug": slug,
                            "need_unlock": True,
                            "unlock_points": unlock_points,
                            "is_official": bool(resource.get("is_official"))
                        })
                else:
                    logger.info(f"HDHive (API) 资源 {resource.get('title')} 非免费且未开启自动解锁，已跳过")

            if free_115_resources:
                # 排序：免费优先，同组内官方(is_official)优先
                free_115_resources.sort(key=lambda r: (
                    r.get("need_unlock", False),      # False(免费) 排前面
                    not r.get("is_official", False)    # True(官方) 排前面
                ))
                free_count = sum(1 for r in free_115_resources if not r.get("need_unlock"))
                unlock_count = sum(1 for r in free_115_resources if r.get("need_unlock"))
                logger.info(f"HDHive (API) 共得到 {len(free_115_resources)} 个 115 资源（免费: {free_count}, 待自费解锁: {unlock_count}）")
                return free_115_resources
            else:
                logger.info(f"HDHive (API) 未找到可用 115 资源")
                return []

        except Exception as e:
            logger.error(f"HDHive (API) 查询失败: {e}")
            return []

    def set_data_funcs(self, get_data_func, save_data_func):
        """
        设置持久化数据读写函数
        """
        self._get_data_func = get_data_func
        self._save_data_func = save_data_func

    def _load_sub_points_history(self) -> dict:
        """加载所有订阅的历史积分花费"""
        if self._get_data_func:
            return self._get_data_func('sub_points_history') or {}
        return {}

    def _save_sub_points_history(self, data: dict):
        """保存所有订阅的历史积分花费"""
        if self._save_data_func:
            self._save_data_func('sub_points_history', data)

    def reset_task_spent_points(self):
        """
        供 SyncHandler 在每次同步任务开始时调用
        仅重置全局积分账本（单订阅的从持久化数据加载）
        """
        self._current_spent_points = 0
        self._sub_spent_points = 0
        self._current_sub_key = ""
        logger.info("HDHive (API) 任务全局积分账本已重置")

    def reset_sub_spent_points(self, sub_key: str = ""):
        """
        供 SyncHandler 在开始处理每一个新的订阅时调用
        从持久化数据中加载该订阅的历史累计花费
        :param sub_key: 订阅唯一标识，如 "逐玉_S1"
        """
        self._current_sub_key = sub_key
        if sub_key:
            history = self._load_sub_points_history()
            self._sub_spent_points = history.get(sub_key, 0)
            if self._sub_spent_points > 0:
                logger.info(f"HDHive (API) 订阅 {sub_key} 历史已花费 {self._sub_spent_points} 积分，剩余预算 {max(0, self._hdhive_max_points_per_sub - self._sub_spent_points)}")
            else:
                logger.info(f"HDHive (API) 订阅 {sub_key} 无历史积分花费")
        else:
            self._sub_spent_points = 0

    def clear_sub_points(self, sub_key: str):
        """
        订阅完成后清除该订阅的历史积分记录
        :param sub_key: 订阅唯一标识
        """
        history = self._load_sub_points_history()
        if sub_key in history:
            del history[sub_key]
            self._save_sub_points_history(history)
            logger.info(f"HDHive (API) 已清除订阅 {sub_key} 的历史积分记录")

    def unlock_hdhive_resource(self, slug: str, unlock_points: int) -> Optional[str]:
        """
        供 SyncHandler 调用的手动解锁（扣积分）API
        :param slug: 资源的标识符
        :param unlock_points: 本次需消耗的积分
        :return: 成功返回真实的 url，失败返回 None
        """
        if not self._hdhive_api_key:
            logger.warning("HDHive API 模式需要有效的 API Key 进行解锁")
            return None

        # 双层检查积分预算
        if (self._current_spent_points + unlock_points) > self._hdhive_max_unlock_points:
            logger.warning(f"HDHive (API) 全局积分预算不足！全局已花费 {self._current_spent_points}，需 {unlock_points}，全局总预算 {self._hdhive_max_unlock_points}")
            return None
            
        if (self._sub_spent_points + unlock_points) > self._hdhive_max_points_per_sub:
            logger.warning(f"HDHive (API) 单订阅积分预算不足！本订阅已花费 {self._sub_spent_points}，需 {unlock_points}，单订阅预算 {self._hdhive_max_points_per_sub}")
            return None

        try:
            import requests
            import time
            headers = {
                "X-API-Key": self._hdhive_api_key,
                "Content-Type": "application/json"
            }
            proxy = settings.PROXY
            proxies = proxy if isinstance(proxy, dict) else {"http": proxy, "https": proxy} if proxy else None

            unlock_url = "https://hdhive.com/api/open/resources/unlock"
            logger.info(f"HDHive (API) 触发后备按需积分解锁资源: {slug}")
            unlock_res = requests.post(unlock_url, json={"slug": slug}, headers=headers, proxies=proxies, timeout=15)
            
            # 为防风控拦截，解锁一个暂停 2 秒
            time.sleep(2)

            if unlock_res.status_code == 200:
                unlock_data = unlock_res.json()
                if unlock_data.get("success") and unlock_data.get("data"):
                    share_url = unlock_data["data"].get("full_url", "")
                    self._current_spent_points += unlock_points
                    self._sub_spent_points += unlock_points
                    # 持久化保存该订阅的累计花费
                    if self._current_sub_key:
                        history = self._load_sub_points_history()
                        history[self._current_sub_key] = self._sub_spent_points
                        self._save_sub_points_history(history)
                    logger.info(f"HDHive (API) 成功扣除 {unlock_points} 积分解锁, 获得新分享链接: {share_url}。全局预算剩余: {self._hdhive_max_unlock_points - self._current_spent_points}, 当前订阅预算剩余: {self._hdhive_max_points_per_sub - self._sub_spent_points}")
                    return share_url
                else:
                    logger.error(f"HDHive (API) 解锁失败，返回数据: {unlock_data}")
            else:
                logger.error(f"HDHive (API) 解锁请求失败，状态码: {unlock_res.status_code}")
                
        except Exception as e:
            logger.error(f"HDHive (API) 解锁异常: {e}")
            
        return None
