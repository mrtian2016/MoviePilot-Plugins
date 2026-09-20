"""
搜索处理模块
负责所有搜索相关逻辑：Dian115、PanSou、KDocs

v1.8.0 起：
    * 新增 Dian115（癫影）搜索源，仅返回 115 分享链接；
    * Nullbr 站点已失效，从可用搜索源中剔除（配置项保留但不再参与调度）。
"""
from typing import Optional, List, Dict, Any

from app.core.config import settings
from app.log import logger
from app.schemas import MediaInfo
from app.schemas.types import MediaType

from ..utils import convert_nullbr_to_pansou_format, SimpleTTLCache
from ..clients.dian115 import is_115_share_url


class SearchHandler:
    """搜索处理器"""

    def __init__(
        self,
        pansou_client,
        nullbr_client,
        pansou_enabled: bool = False,
        nullbr_enabled: bool = False,
        only_115: bool = True,
        pansou_channels: str = "",
        search_source_order: Optional[List[str]] = None,
        kdocs_client=None,
        kdocs_enabled: bool = False,
        dian115_client=None,
        dian115_enabled: bool = False,
        dian115_auto_unlock: bool = False,
        dian115_max_unlock_points: int = 50,
        dian115_max_points_per_sub: int = 20,
        pinglian_client=None,
        pinglian_enabled: bool = False
    ):
        """
        初始化搜索处理器

        :param pansou_client: PanSou 客户端实例
        :param nullbr_client: Nullbr 客户端实例（站点已失效，不再参与调度）
        :param pansou_enabled: 是否启用 PanSou
        :param nullbr_enabled: 是否启用 Nullbr（已失效，保留兼容）
        :param only_115: 是否只搜索115网盘资源
        :param pansou_channels: PanSou 搜索频道
        :param search_source_order: 自定义搜索源优先级列表，如 ["pansou", "dian115"]；
                                    为空时使用默认优先级 Dian115 > PanSou > KDocs
        :param kdocs_client: KDocs 在线文档库客户端实例
        :param kdocs_enabled: 是否启用 KDocs 在线文档库
        :param dian115_client: Dian115（癫影）客户端实例
        :param dian115_enabled: 是否启用 Dian115 搜索源
        :param dian115_auto_unlock: 是否自动消耗积分解锁 Dian115 资源
        :param dian115_max_unlock_points: 单次任务 Dian115 积分解锁总预算
        :param dian115_max_points_per_sub: 单个订阅 Dian115 积分解锁预算
        """
        self._pansou_client = pansou_client
        self._nullbr_client = nullbr_client
        self._pansou_enabled = pansou_enabled
        self._nullbr_enabled = nullbr_enabled
        self._current_sub_key = ""
        self._get_data_func = None
        self._save_data_func = None
        self._only_115 = only_115
        self._pansou_channels = pansou_channels
        self._search_source_order = search_source_order or []
        self._kdocs_client = kdocs_client
        self._kdocs_enabled = kdocs_enabled
        self._dian115_client = dian115_client
        self._dian115_enabled = dian115_enabled
        self._dian115_auto_unlock = dian115_auto_unlock
        self._dian115_max_unlock_points = dian115_max_unlock_points
        self._dian115_max_points_per_sub = dian115_max_points_per_sub
        self._pinglian_client = pinglian_client
        self._pinglian_enabled = pinglian_enabled
        # Dian115 的积分账本独立维护，避免跨渠道互相挤占预算
        self._dian115_spent_points = 0
        self._dian115_sub_spent_points = 0
        # 已解锁链接幂等缓存：同一分享在同一 TTL 内只扣一次分
        self._dian115_unlocked_cache = SimpleTTLCache(ttl=3600, maxsize=512)

    def get_enabled_sources(self) -> List[str]:
        """
        获取已启用且可用的搜索源列表，按优先级排序
        （v1.7.3 渠道级回退：电影分支由上层逐渠道调用 search_single_source 消费此列表）

        优先级规则：
        1. 用户配置了自定义优先级（search_source_order）时按其顺序排列；
           未出现在自定义列表中的已启用源按默认顺序追加在末尾
        2. 未配置时使用默认优先级 Dian115 > PanSou > KDocs

        v1.8.0：Nullbr 站点已停止服务，**硬屏蔽**（即使配置里仍勾选
        也不参与调度，避免订阅流程白白等待超时）。其配置项保留仅作兼容。

        :return: 搜索源名称列表
        """
        # 按默认优先级收集已启用且可用的源
        available = []

        # Nullbr —— 站点已失效，永久剔除
        # if self._nullbr_enabled and self._nullbr_client:
        #     available.append("nullbr")

        # Dian115（癫影）
        if self._dian115_enabled and self._dian115_client:
            available.append("dian115")

        # PanSou
        if self._pansou_enabled and self._pansou_client:
            available.append("pansou")

        # KDocs
        if self._kdocs_enabled and self._kdocs_client and self._kdocs_client.is_ready:
            available.append("kdocs")
        if self._pinglian_enabled and self._pinglian_client:
            available.append("pinglian")

        # 应用用户自定义优先级
        if self._search_source_order:
            sources = [s for s in self._search_source_order if s in available]
            sources += [s for s in available if s not in sources]
            return sources

        return available

    def search_resources(
        self,
        mediainfo: MediaInfo,
        media_type: MediaType,
        season: Optional[int] = None
    ) -> List[Dict]:
        """
        统一的聚合资源搜索方法，支持电影和电视剧
        按优先级遍历所有启用的搜索源，聚合各源结果返回（每个结果带 _source 标签）

        v1.7.3 起不再"第一个有结果的源就返回"——渠道是否成功由上层按
        真实转存结果判定，全失败时需继续消费后续渠道的结果。

        注意：此方法主要供电影订阅使用。电视剧订阅使用 search_single_source 进行逐源搜索。

        :param mediainfo: 媒体信息
        :param media_type: 媒体类型（MOVIE 或 TV）
        :param season: 季号（电视剧必需）
        :return: 115网盘资源列表（按源优先级拼接，各结果带 _source 字段）
        """
        sources = self.get_enabled_sources()

        aggregated: List[Dict] = []
        for source in sources:
            results = self.search_single_source(source, mediainfo, media_type, season, tag_source=True)
            if results:
                aggregated.extend(results)
            else:
                # 打印回退日志
                remaining = sources[sources.index(source) + 1:]
                if remaining:
                    logger.info(f"{source.capitalize()} 未找到资源，将回退到 {'/'.join([s.capitalize() for s in remaining])} 搜索")

        return aggregated

    def search_single_source(
        self,
        source: str,
        mediainfo: MediaInfo,
        media_type: MediaType,
        season: Optional[int] = None,
        tag_source: bool = False
    ) -> List[Dict]:
        """
        使用指定的单一搜索源查询资源

        :param source: 搜索源名称 ("dian115", "pansou", "kdocs")
        :param mediainfo: 媒体信息
        :param media_type: 媒体类型
        :param season: 季号（电视剧时使用）
        :param tag_source: 是否给每个结果打上 _source 渠道标签（渠道级回退统计用）
        :return: 115网盘资源列表
        """
        if source == "dian115":
            results = self._search_dian115(mediainfo, media_type, season)
        elif source == "pansou":
            if media_type == MediaType.MOVIE:
                results = self._search_pansou_movie(mediainfo)
            else:
                results = self._search_pansou_tv(mediainfo, season)
        elif source == "kdocs":
            results = self._search_kdocs(mediainfo)
        elif source == "pinglian":
            results = self._search_pinglian(mediainfo, media_type, season)
        else:
            logger.warning(f"未知的搜索源: {source}")
            return []

        if tag_source:
            for item in results or []:
                if isinstance(item, dict):
                    item.setdefault("_source", source)
        return results or []

    def _search_pinglian(self, mediainfo: MediaInfo, media_type: MediaType, season: Optional[int] = None) -> List[Dict]:
        if not self._pinglian_client:
            return []
        keyword = mediainfo.title
        if media_type != MediaType.MOVIE and season:
            keyword = f"{keyword} S{season}"
        try:
            return self._pinglian_client.search(keyword)
        except Exception as exc:
            logger.warning(f"Pinglian 查询失败，跳过该搜索源: {exc}")
            return []

    def _search_dian115(
        self,
        mediainfo: MediaInfo,
        media_type: MediaType,
        season: Optional[int] = None
    ) -> List[Dict]:
        """
        使用 Dian115（癫影）按 TMDB ID 查询资源

        Dian115 是按 TMDB 维度组织资源的，因此必须要有 TMDB ID。
        客户端内部已完成「只保留 115 分享链接」过滤，这里无需二次过滤。

        :param mediainfo: 媒体信息
        :param media_type: 媒体类型（MOVIE 或 TV）
        :param season: 季号（电视剧时使用）
        :return: 115网盘资源列表（统一格式）
        """
        if not self._dian115_client:
            logger.warning("Dian115 客户端未初始化，跳过 Dian115 查询")
            return []

        if not getattr(self._dian115_client, "is_configured", False):
            logger.warning("Dian115 未配置认证信息，跳过 Dian115 查询")
            return []

        if not mediainfo.tmdb_id:
            logger.warning(f"{mediainfo.title} 缺少 TMDB ID，无法使用 Dian115 查询")
            return []

        dian_media_type = "movie" if media_type == MediaType.MOVIE else "tv"
        dian_season = int(season or 0) if dian_media_type == "tv" else 0
        label = (
            mediainfo.title if media_type == MediaType.MOVIE
            else f"{mediainfo.title} S{season}"
        )
        logger.info(f"使用 Dian115 查询资源: {label} (TMDB ID: {mediainfo.tmdb_id})")

        try:
            results = self._dian115_client.search_resources(
                tmdb_id=mediainfo.tmdb_id,
                media_type=dian_media_type,
                season=dian_season,
                limit=20
            )
        except Exception as e:
            logger.error(f"Dian115 查询 {label} 失败: {e}")
            return []

        if not results:
            logger.info("Dian115 未找到资源")
            return []

        # 两级预算过滤：
        #   * 单条解锁成本高于任一预算上限的，直接丢弃（无论如何都解锁不了）
        #   * 免费 / 已解锁条目直接采用
        #   * 收费条目：开启自动解锁时标记为待解锁，交给 SyncHandler 按需真正扣分
        #   * 收费条目且未开自动解锁：丢弃并计数
        selected: List[Dict] = []
        skipped_over_budget = 0
        skipped_need_unlock = 0
        for resource in results:
            unlock_points = int(resource.get("unlock_points") or 0)
            if unlock_points > 0:
                if unlock_points > self._dian115_max_points_per_sub:
                    skipped_over_budget += 1
                    continue
                if unlock_points > self._dian115_max_unlock_points:
                    skipped_over_budget += 1
                    continue
                if not self._dian115_auto_unlock:
                    skipped_need_unlock += 1
                    continue
                resource["need_unlock"] = True
            selected.append(resource)

        free_count = sum(1 for item in selected if not item.get("need_unlock"))
        unlock_count = len(selected) - free_count
        logger.info(
            f"Dian115 共得到 {len(selected)} 个 115 资源"
            f"（免费/已解锁: {free_count}, 待积分解锁: {unlock_count}）；"
            f"跳过（未开自动解锁: {skipped_need_unlock}, 超出预算: {skipped_over_budget}）"
        )
        return selected

    def unlock_dian115_resource(self, share_id: int, unlock_points: int) -> Optional[str]:
        """
        供 SyncHandler 调用的 Dian115 手动解锁（扣积分）API

        采用两级预算：单次任务总预算 + 单订阅预算；
        服务端实际扣分与预估不一致时以服务端为准入账。

        :param share_id: Dian115 分享 ID（资源条目的 resource_ref）
        :param unlock_points: 搜索阶段给出的预估积分
        :return: 成功返回真实的 115 分享链接，失败返回 None
        """
        if not self._dian115_client:
            logger.warning("Dian115 客户端未初始化，无法解锁")
            return None

        normalized_share_id = int(share_id or 0)
        if normalized_share_id <= 0:
            logger.warning("Dian115 解锁失败：分享 ID 无效")
            return None

        # 复用已解锁缓存，避免同一轮任务内重复扣分
        cached = self._dian115_unlocked_cache.get(str(normalized_share_id))
        if cached:
            logger.info(f"Dian115 复用本任务已解锁链接: share_id={normalized_share_id}")
            return str(cached)

        estimate = max(0, int(unlock_points or 0))
        if (self._dian115_spent_points + estimate) > self._dian115_max_unlock_points:
            logger.warning(
                f"Dian115 全局积分预算不足：已花费 {self._dian115_spent_points}，"
                f"需 {estimate}，任务总预算 {self._dian115_max_unlock_points}"
            )
            return None
        if (self._dian115_sub_spent_points + estimate) > self._dian115_max_points_per_sub:
            logger.warning(
                f"Dian115 单订阅积分预算不足：本订阅已花费 {self._dian115_sub_spent_points}，"
                f"需 {estimate}，单订阅预算 {self._dian115_max_points_per_sub}"
            )
            return None

        logger.info(f"Dian115 触发按需积分解锁: share_id={normalized_share_id}，预估 {estimate} 积分")
        try:
            payload = self._dian115_client.unlock_share(normalized_share_id)
        except Exception as e:
            code = str(getattr(e, "code", "") or "")
            if code in ("turnstile_failed", "turnstile_required"):
                logger.warning(
                    f"Dian115 解锁需要 Cloudflare 人机验证（{code}），本次跳过且未扣积分。"
                    f"如需使用癫影资源，请在浏览器完成验证后改用其它方案；"
                    f"当前建议以「签到/转盘」为主、搜索走盘搜。"
                )
            else:
                logger.error(f"Dian115 解锁 {normalized_share_id} 失败：{e}")
            return None

        share_url = self._dian115_extract_unlock_url(payload)
        if not share_url:
            logger.error(f"Dian115 解锁响应未返回可用链接：share_id={normalized_share_id}")
            return None

        actual_points = max(0, int(payload.get("actual_points") or estimate))
        self._dian115_spent_points += actual_points
        self._dian115_sub_spent_points += actual_points
        self._dian115_unlocked_cache.set(str(normalized_share_id), share_url)
        if self._current_sub_key:
            history = self._load_sub_points_history()
            history[f"dian115:{self._current_sub_key}"] = self._dian115_sub_spent_points
            self._save_sub_points_history(history)
        logger.info(
            f"Dian115 解锁成功，扣除 {actual_points} 积分，链接: {share_url}。"
            f"任务剩余 {max(0, self._dian115_max_unlock_points - self._dian115_spent_points)}，"
            f"订阅剩余 {max(0, self._dian115_max_points_per_sub - self._dian115_sub_spent_points)}"
        )
        return share_url

    @staticmethod
    def _dian115_extract_unlock_url(payload: Dict) -> str:
        """从解锁响应中取链接：优先 115 分享链接，其次离线链接（magnet/ed2k）。

        癫影存在 ``share_kind=offline`` 的资源，解锁后**只给 magnet / ed2k**，
        旧实现无条件取第一个非空字段，会把 magnet 当成 115 分享链接返回，
        上层解析 share_code 必然失败（还白扣积分）。现在两轮取值：
        先找真正的 115 分享链接，找不到才回退离线链接（由上层走云下载）。
        """
        if not isinstance(payload, dict):
            return ""
        data = payload.get("payload")
        if not isinstance(data, dict):
            data = payload
        keys = ("url", "share_url", "full_url", "link", "share_link")
        for key in keys:
            value = str(data.get(key) or "").strip()
            if value and is_115_share_url(value):
                return value
        for key in keys:
            value = str(data.get(key) or "").strip()
            if value:
                return value
        share_code = str(data.get("share_code") or "").strip()
        if share_code:
            receive_code = str(data.get("receive_code") or "").strip()
            return (
                f"https://115.com/s/{share_code}?password={receive_code}"
                if receive_code else f"https://115.com/s/{share_code}"
            )
        return ""

    def reset_dian115_sub_spent_points(self, sub_key: str = ""):
        """供 SyncHandler 在处理每个新订阅前调用，载入该订阅的历史积分花费。"""
        if sub_key:
            history = self._load_sub_points_history()
            self._dian115_sub_spent_points = max(
                0, int(history.get(f"dian115:{sub_key}") or 0)
            )
        else:
            self._dian115_sub_spent_points = 0

    def clear_dian115_sub_points(self, sub_key: str):
        """订阅完成后清除该订阅的 Dian115 历史积分记录。"""
        history = self._load_sub_points_history()
        key = f"dian115:{sub_key}"
        if key in history:
            del history[key]
            self._save_sub_points_history(history)
            logger.info(f"Dian115 已清除订阅 {sub_key} 的历史积分记录")

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
                return results
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
                return results
            else:
                logger.info(f"PanSou 关键词 '{keyword}' 无结果，尝试下一个降级关键词")

        logger.info(f"PanSou 未找到资源")
        return []


    def _search_kdocs(
        self,
        mediainfo: MediaInfo
    ) -> List[Dict]:
        """
        使用 KDocs 在线文档库搜索资源

        :param mediainfo: 媒体信息
        :return: 115网盘资源列表（统一格式）
        """
        if not self._kdocs_client or not self._kdocs_client.is_ready:
            logger.warning("KDocs: 客户端未初始化或未配置 Token，跳过查询")
            return []

        try:
            logger.info(f"使用 KDocs 在线文档库查询: {mediainfo.title}")
            results = self._kdocs_client.search(mediainfo.title, only_115=self._only_115)
            if results:
                logger.info(f"KDocs 找到 {len(results)} 个资源")
            else:
                logger.info("KDocs 未找到资源")
            return results
        except Exception as e:
            logger.error(f"KDocs 查询失败: {e}")
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
        清空当前订阅标识（各渠道积分账本由各渠道自行维护）
        """
        self._current_sub_key = ""

    def reset_sub_spent_points(self, sub_key: str = ""):
        """
        供 SyncHandler 在开始处理每一个新的订阅时调用
        记录当前订阅标识，供渠道积分账本持久化使用
        :param sub_key: 订阅唯一标识，如 "逐玉_S1"
        """
        self._current_sub_key = sub_key

