"""HDHive OpenAPI/Web 资源查询与积分解锁。"""

import re
import time
from typing import Any, Dict, List, Mapping, Optional
from urllib.parse import urljoin, urlparse

from app.log import logger
from app.schemas import MediaInfo
from app.schemas.types import MediaType

from .web import (
    HDHIVE_DETAIL_RESOURCE_TYPES,
    HDHIVE_RESOURCE_TYPES,
    HDHiveClient,
    HDHiveResourceService,
    HDHiveWebError,
    valid_share_url,
)
from ..budget import PointBudgetLedger
from ...core import OwnerDelegator, SearchQuery, format_search_label
from ...core.media import tmdb_id_of
from ...utils.cache import create_platform_ttl_cache
from ...utils.cache import normalize_platform_cache_key


class HDHiveSearchService(OwnerDelegator):
    """提供 HDHive WebAPI 与开放 API 搜索能力。"""

    _HISTORY_KEY = "hdhive_sub_points_history"

    def __init__(self, owner):
        super().__init__(owner)
        cache_identity = f"{owner._hdhive_query_mode}:{owner._hdhive_username}"
        unlocked_cache = create_platform_ttl_cache(
            "search:hdhive_unlocked_urls",
            cache_identity,
            maxsize=256,
            ttl=30 * 60,
        )
        object.__setattr__(self, "_budget", PointBudgetLedger(
            self._HISTORY_KEY,
            owner._hdhive_max_unlock_points,
            owner._hdhive_max_points_per_sub,
            unlocked_cache=unlocked_cache,
        ))
    @property
    def _hdhive_budget(self):
        return self._budget

    @property
    def available(self) -> bool:
        return bool(self._hdhive_enabled and (
                (
                        self._hdhive_query_mode == "web"
                        and self._hdhive_username
                        and self._hdhive_password
                )
                or (
                        self._hdhive_query_mode == "api"
                        and self._hdhive_client
                        and self._hdhive_client.is_ready
                )
        ))

    @property
    def resource_types(self):
        return frozenset(self._resource_type_order_config)

    @property
    def cache_context(self) -> Dict[str, Any]:
        return {
            "version": 1,
            "mode": self._hdhive_query_mode,
            "torrentclaw_enabled": self._hdhive_torrentclaw_enabled,
            "torrentclaw_subtitle_languages": list(
                self._hdhive_torrentclaw_subtitle_languages
            ),
        }

    @staticmethod
    def _hdhive_source_url(resource: Dict[str, Any]) -> str:
        """校验 OpenAPI 返回的 HDHive 资源链接。"""
        candidate = str(resource.get("source_url") or "").strip()
        if not candidate:
            return ""
        resolved = urljoin("https://re0.me/", candidate)
        parsed = urlparse(resolved)
        if parsed.scheme not in {"http", "https"} or parsed.netloc.lower() != "re0.me":
            return ""
        if not re.fullmatch(
                r"/resource/(?:[A-Za-z0-9_-]+/)?[A-Za-z0-9_-]+",
                parsed.path.rstrip("/"),
                re.I,
        ):
            return ""
        return resolved

    @staticmethod
    def _openapi_resource_type(resource: Dict[str, Any]) -> str:
        return str(resource.get("pan_type") or "").strip().lower()

    def _openapi_candidate(
            self,
            resource: Dict[str, Any],
            media_page_url: str,
            *,
            url: str = "",
            unlock_points: int = 0,
            need_unlock: bool = False,
    ) -> Dict[str, Any]:
        return {
            "url": url,
            "title": resource.get("title") or "HDHive 资源",
            "description": resource.get("description") or "",
            "resolution": resource.get("resolution") or "",
            "quality": resource.get("quality") or "",
            "size": resource.get("size") or 0,
            "update_time": resource.get("created_at") or "",
            "resource_ref": str(resource.get("slug") or "").strip(),
            "resource_type": self._openapi_resource_type(resource),
            "need_unlock": need_unlock,
            "need_access": not url and not need_unlock,
            "unlock_points": unlock_points,
            "is_free": not need_unlock,
            "is_unlocked": bool(resource.get("is_unlocked")),
            "is_official": bool(resource.get("is_official")),
            "source_url": self._hdhive_source_url(resource),
            "media_page_url": media_page_url,
        }

    @staticmethod
    def _hdhive_media_page_url(media_type: str, tmdb_id: Any) -> str:
        """返回资源卡片所在的 HDHive TMDB 媒体页。"""
        normalized_type = str(media_type or "").strip().lower()
        try:
            normalized_tmdb_id = int(tmdb_id or 0)
        except (TypeError, ValueError):
            return ""
        if normalized_type not in {"movie", "tv"} or normalized_tmdb_id <= 0:
            return ""
        return f"https://re0.me/tmdb/{normalized_type}/{normalized_tmdb_id}"

    @staticmethod
    def _valid_share_value(value: Any, resource_type: str) -> bool:
        values = value if isinstance(value, (list, tuple, set)) else [value]
        return bool(values) and all(
            valid_share_url(item, resource_type)
            for item in values
        )

    def _get_hdhive_web_resources(self) -> HDHiveResourceService:
        """复用唯一认证客户端，返回独立的资源服务。"""
        proxy = self._search_proxy
        with self._hdhive_web_lock:
            client = self._hdhive_web_client
            if client is None or not client.matches_config(
                    self._hdhive_username,
                    self._hdhive_password,
                    proxy,
                    self._hdhive_request_interval,
            ):
                if client and self._hdhive_web_client_owned:
                    client.close()
                client = HDHiveClient(
                    username=self._hdhive_username,
                    password=self._hdhive_password,
                    proxy=proxy,
                    request_interval=self._hdhive_request_interval,
                    should_stop=self._stop_requested,
                )
                self._hdhive_web_client = client
                self._hdhive_web_client_owned = True
                self._hdhive_web_resources = None
            resources = self._hdhive_web_resources
            if resources is None or not resources.matches_config(
                    client,
                    self._hdhive_torrentclaw_enabled,
                    self._hdhive_torrentclaw_subtitle_languages,
                    self._hdhive_unlocks_per_minute,
            ):
                resources = HDHiveResourceService(
                    client=client,
                    torrentclaw_enabled=self._hdhive_torrentclaw_enabled,
                    torrentclaw_subtitle_languages=(
                        self._hdhive_torrentclaw_subtitle_languages
                    ),
                    unlocks_per_minute=self._hdhive_unlocks_per_minute,
                )
                self._hdhive_web_resources = resources
            return resources

    def get_client(self) -> Any:
        """返回当前查询模式复用的账户客户端。"""
        if self._hdhive_query_mode == "api":
            return self._hdhive_client
        self._get_hdhive_web_resources()
        return self._hdhive_web_client

    @property
    def budget(self):
        return self._budget

    def close(self) -> None:
        """释放 HDHive Web 客户端资源。"""
        with self._hdhive_web_lock:
            web_client = self._hdhive_web_client
            web_client_owned = self._hdhive_web_client_owned
            self._hdhive_web_client = None
            self._hdhive_web_client_owned = True
            self._hdhive_web_resources = None
        if web_client and web_client_owned:
            web_client.close()

    def clear_cache(self) -> int:
        total = 0
        with self._hdhive_web_lock:
            resources = self._hdhive_web_resources
        for target in (resources, self._hdhive_client):
            if not target or not callable(getattr(target, "clear_cache", None)):
                continue
            result = target.clear_cache()
            total += (
                sum(int(value or 0) for value in result.values())
                if isinstance(result, dict)
                else int(result or 0)
            )
        return total

    def search(self, query: SearchQuery) -> Optional[List[Dict]]:
        """
        使用 HDHive 搜索资源
        根据配置的查询模式选择 Web 或 OpenAPI。

        :param mediainfo: 媒体信息
        :param media_type: 媒体类型（MOVIE 或 TV）
        :param season: 季号（电视剧时使用）
        :return: 当前转存网盘可处理的资源列表（统一格式）
        """
        mediainfo = query.mediainfo
        media_type = query.media_type
        season = query.season
        target_episodes = list(query.target_episodes)
        target_episode_air_dates = dict(query.target_episode_air_dates)
        subscribe = query.subscribe
        resource_list_mode = query.resource_list_mode
        result_limit = query.result_limit
        tmdb_id = mediainfo.tmdb_id or tmdb_id_of(subscribe)
        search_prefix = (
            f"[{format_search_label(mediainfo, media_type, season)}][HDHIVE]"
        )
        if not tmdb_id:
            logger.debug(f"{search_prefix} 缺少 TMDB ID，跳过查询")
            return []

        hdhive_media_type = "movie" if media_type == MediaType.MOVIE else "tv"

        if self._hdhive_query_mode == "web":
            results = self._search_web(
                mediainfo,
                hdhive_media_type,
                tmdb_id=tmdb_id,
                season=season,
                target_episodes=target_episodes,
                target_episode_air_dates=target_episode_air_dates,
                subscribe=subscribe,
                resource_list_mode=resource_list_mode,
                result_limit=result_limit,
            )
        else:
            results = self._search_openapi(
                mediainfo,
                hdhive_media_type,
                tmdb_id=tmdb_id,
                season=season,
                resource_list_mode=resource_list_mode,
                result_limit=result_limit,
            )
        if results is not None:
            for item in results:
                item["identity_verified"] = True
                item["target_season"] = (
                    int(season) if season is not None else None
                )
        return results

    def _search_web(
            self,
            mediainfo: MediaInfo,
            hdhive_media_type: str,
            tmdb_id: Optional[int] = None,
            season: Optional[int] = None,
            target_episodes: Optional[List[int]] = None,
            target_episode_air_dates: Optional[Dict[int, str]] = None,
            subscribe: Any = None,
            resource_list_mode: bool = False,
            result_limit: Optional[int] = None,
    ) -> Optional[List[Dict]]:
        """
        使用 WebAPI 模式查询 HDHive 资源。
        """
        search_label = format_search_label(
            mediainfo,
            MediaType.MOVIE if hdhive_media_type == "movie" else MediaType.TV,
            season,
        )
        search_prefix = f"[{search_label}][HDHIVE]"
        if not self._hdhive_username or not self._hdhive_password:
            logger.warning(f"{search_prefix} WebAPI 需要配置用户名和密码")
            return []

        try:
            started = time.monotonic()
            resources = self._get_hdhive_web_resources()
            if resource_list_mode:
                results = resources.search_test_resources(
                    tmdb_id=int(tmdb_id),
                    media_type=hdhive_media_type,
                    candidate_limit=result_limit or self._hdhive_candidate_limit,
                    log_prefix=search_prefix,
                )
            else:
                results = resources.search_resources(
                    tmdb_id=tmdb_id,
                    media_type=hdhive_media_type,
                    include_paid=self._hdhive_auto_unlock,
                    target_season=season,
                    target_episodes=target_episodes,
                    target_episode_air_dates=target_episode_air_dates,
                    resource_types=[
                        value for value in self._resource_type_order_config
                        if value in HDHIVE_RESOURCE_TYPES
                    ],
                    magnet_filter=lambda rows: self._filter_by_platform_rules(
                        rows,
                        mediainfo,
                        subscribe,
                        season=season,
                        target_episodes=target_episodes,
                    ),
                    candidate_limit=self._hdhive_candidate_limit,
                    log_prefix=search_prefix,
                )

            results = list(results)
            for item in results:
                item["preview_episodes_authoritative"] = bool(
                    item.get("preview_episodes")
                )

            if results:
                free_count = sum(1 for item in results if not item.get("need_unlock"))
                paid_count = len(results) - free_count
                resource_page_count = len({
                    str(item.get("unlock_group") or item.get("url") or index)
                    for index, item in enumerate(results)
                })
                type_counts: Dict[str, int] = {}
                for item in results:
                    resource_type = str(
                        item.get("resource_type") or "unknown"
                    ).strip().lower()
                    type_counts[resource_type] = type_counts.get(resource_type, 0) + 1
                type_summary = "，".join(
                    f"{key.upper()}={value}"
                    for key, value in type_counts.items()
                )
                logger.debug(
                    f"{search_prefix} WebAPI 渠道统计："
                    f"资源页={resource_page_count}，候选={len(results)}"
                    f"（免费/已解锁: {free_count}，待积分解锁: {paid_count}，"
                    f"类型: {type_summary or '无'}）"
                )
            else:
                logger.debug(
                    f"{search_prefix} WebAPI 渠道统计："
                    "站点无资源或均未通过渠道预筛"
                )
            return results

        except HDHiveWebError as e:
            message = (
                f"{locals().get('search_prefix', f'[{mediainfo.title}][HDHIVE]')} "
                f"WebAPI 查询失败：{e}，"
                f"耗时={time.monotonic() - locals().get('started', time.monotonic()):.2f}s"
            )
            if e.code in {"rate_limited", "server_cooldown", "stopped"}:
                logger.debug(message)
            else:
                logger.error(message)
            return None
        except Exception as e:
            logger.error(
                f"{locals().get('search_prefix', f'[{mediainfo.title}][HDHIVE]')} "
                f"WebAPI 查询失败：{e}，"
                f"耗时={time.monotonic() - locals().get('started', time.monotonic()):.2f}s"
            )
            # 暂态失败不能伪装成正常空结果，否则上层会写入负缓存。
            return None

    def _search_openapi(
            self, mediainfo: MediaInfo, hdhive_media_type: str,
            tmdb_id: Optional[int] = None, season: Optional[int] = None,
            resource_list_mode: bool = False,
            result_limit: Optional[int] = None,
    ) -> Optional[List[Dict]]:
        """
        使用 API 模式查询 HDHive 资源
        需要应用 Secret + 用户授权（OpenAPI 客户端）
        """
        from .open import HDHiveOpenAPIError
        search_label = format_search_label(
            mediainfo,
            MediaType.MOVIE if hdhive_media_type == "movie" else MediaType.TV,
            season,
        )
        search_prefix = f"[{search_label}][HDHIVE]"

        if not self._hdhive_client or not self._hdhive_client.is_ready:
            logger.warning(
                f"{search_prefix} API 模式需要配置应用 Secret "
                "并完成用户授权"
            )
            return []

        try:
            started = time.monotonic()
            media_page_url = self._hdhive_media_page_url(
                hdhive_media_type, tmdb_id
            )

            # 1. 获取资源列表
            try:
                data = self._hdhive_client.query_resources(hdhive_media_type, tmdb_id)
            except HDHiveOpenAPIError as e:
                logger.error(
                    f"{search_prefix} API 获取资源失败: "
                    f"[{e.code}] {e.message} {e.description}"
                )
                return None

            if not data.get("success") or not data.get("data"):
                logger.debug(
                    f"{search_prefix} API 渠道统计：站点资源=0"
                )
                return []

            if resource_list_mode:
                raw_candidates = []
                for resource in data.get("data", []):
                    resource_type = self._openapi_resource_type(resource)
                    if resource_type not in HDHIVE_RESOURCE_TYPES:
                        continue
                    raw_points = resource.get("unlock_points")
                    is_unlocked = bool(resource.get("is_unlocked"))
                    is_free = is_unlocked or raw_points == 0
                    raw_candidates.append(self._openapi_candidate(
                        resource,
                        media_page_url,
                        unlock_points=(
                                self._hdhive_budget.normalize_points(raw_points) or 0
                        ),
                        need_unlock=not is_free,
                    ))
                    if len(raw_candidates) >= max(
                            1, int(result_limit or self._hdhive_candidate_limit)
                    ):
                        break
                logger.debug(
                    f"{search_prefix} API 渠道统计："
                    f"站点资源={len(data.get('data') or [])}，模式=只读测试"
                )
                return raw_candidates

            enabled_types = set(self._resource_type_order_config) & set(
                HDHIVE_DETAIL_RESOURCE_TYPES
            )
            enabled_resources = [
                resource for resource in data.get("data", [])
                if self._openapi_resource_type(resource) in enabled_types
            ]
            candidates = []
            for resource in enabled_resources:
                candidate = dict(resource)
                candidate["update_time"] = (
                        resource.get("updated_at")
                        or resource.get("posted_at")
                        or resource.get("created_at")
                        or ""
                )
                candidate["is_official"] = bool(resource.get("is_official"))
                candidate["need_unlock"] = not bool(resource.get("is_unlocked")) and resource.get("unlock_points") != 0
                candidates.append(candidate)
            # 更新时间决定首选候选；同时间再沿用类型、可用性、官组和积分顺序。
            resources = sorted(
                self._prefilter_resource_order(candidates),
                key=self._hdhive_update_sort_key,
            )[: self._hdhive_candidate_limit]
            available_resources = []
            request_failures = 0
            missing_slug_count = 0
            unknown_points_count = 0
            auto_unlock_skipped = 0
            for resource in resources:
                slug = str(resource.get("slug") or "").strip()
                if not slug:
                    missing_slug_count += 1
                    continue

                raw_points = resource.get("unlock_points")
                is_free = bool(resource.get("is_unlocked")) or raw_points == 0
                unlock_points = 0
                if not is_free:
                    if raw_points is None:
                        try:
                            detail_response = self._hdhive_client.get_share_details(slug)
                            detail = detail_response.get("data") or {}
                        except HDHiveOpenAPIError as e:
                            request_failures += 1
                            logger.warning(
                                f"{search_prefix} API 查询资源实际积分失败: "
                                f"[{e.code}] "
                                f"{e.message} {e.description}"
                            )
                            continue
                        is_free = bool(detail.get("is_unlocked") or detail.get("is_free_for_user"))
                        raw_points = detail.get("actual_unlock_points")
                    unlock_points = (
                            self._hdhive_budget.normalize_points(raw_points) or 0
                    )
                    if not is_free and unlock_points <= 0:
                        unknown_points_count += 1
                        continue

                if is_free:
                    try:
                        unlock_data = self._hdhive_client.unlock_resource(
                            slug, max_unlock_points=0
                        )
                    except HDHiveOpenAPIError as e:
                        request_failures += 1
                        logger.warning(
                            f"{search_prefix} API 获取免费或已解锁资源链接失败: "
                            f"[{e.code}] {e.message} {e.description}"
                        )
                        continue
                    result_data = unlock_data.get("data") or {}
                    share_url = str(result_data.get("full_url") or result_data.get("url") or "").strip()
                    if not share_url:
                        request_failures += 1
                        logger.warning(
                            f"{search_prefix} API 免费或已解锁资源"
                            "未返回分享链接"
                        )
                        continue
                    available_resources.append(self._openapi_candidate(
                        resource,
                        media_page_url,
                        url=share_url,
                        unlock_points=unlock_points,
                    ))
                    continue

                if not self._hdhive_auto_unlock:
                    auto_unlock_skipped += 1
                    continue
                available_resources.append(self._openapi_candidate(
                    resource,
                    media_page_url,
                    unlock_points=unlock_points,
                    need_unlock=True,
                ))

            if available_resources:
                free_count = sum(1 for r in available_resources if not r.get("need_unlock"))
                unlock_count = len(available_resources) - free_count
                logger.debug(
                    f"{search_prefix} API 渠道统计：站点资源={len(data.get('data') or [])}，"
                    f"启用类型={len(enabled_resources)}，预筛={len(resources)}，"
                    f"免费/已解锁={free_count}，待积分解锁={unlock_count}，"
                    f"跳过（缺少标识={missing_slug_count}，积分未知={unknown_points_count}）"
                )
                return available_resources
            else:
                if request_failures:
                    logger.warning(
                        f"{search_prefix} API 请求暂态失败，空结果不写入搜索缓存"
                    )
                    return None
                logger.debug(
                    f"{search_prefix} API 渠道统计：站点资源={len(data.get('data') or [])}，"
                    f"启用类型={len(enabled_resources)}，预筛={len(resources)}，"
                    f"跳过（缺少标识={missing_slug_count}，积分未知={unknown_points_count}）"
                )
                return []

        except Exception as e:
            logger.error(f"{search_prefix} API 查询失败: {e}")
            return None

    def preview(self, candidate: Mapping[str, Any]) -> Dict[str, Any]:
        """只读预览 HDHive file-list，不触发积分解锁。"""
        if self._hdhive_query_mode != "web":
            raise HDHiveWebError(
                "HDHive API 模式不支持未解锁资源预览",
                code="preview_unsupported",
            )
        search_label = str(candidate.get("search_label") or "")
        search_prefix = f"[{search_label}][HDHIVE]" if search_label else "[HDHIVE]"
        return self._get_hdhive_web_resources().preview_resource(
            slug=str(candidate.get("resource_ref") or ""),
            resource_type=str(candidate.get("resource_type") or ""),
            target_season=candidate.get("target_season"),
            target_episodes=candidate.get("target_episodes"),
            supports_file_preview=candidate.get("supports_file_preview"),
            detail_path=str(
                (candidate.get("provider_data") or {}).get("detail_path") or ""
            ),
            log_prefix=search_prefix,
        )

    def unlock(
            self,
            candidate: Mapping[str, Any],
            search_label: str = "",
    ) -> Any:
        """串行执行付费解锁，保证全局和单订阅积分预算原子扣减。"""
        while not self._hdhive_unlock_operation_lock.acquire(timeout=0.25):
            if self._stop_requested():
                logger.info(
                    f"[{search_label}][HDHIVE] 已停止任务，跳过积分解锁"
                    if search_label else
                    "[HDHIVE] 已停止任务，跳过积分解锁"
                )
                return None
        try:
            return self._unlock(
                str(candidate.get("resource_ref") or ""),
                int(candidate.get("unlock_points") or 0),
                str(candidate.get("resource_type") or ""),
                str(candidate.get("media_page_url") or ""),
                search_label,
                bool(candidate.get("is_unlocked")),
                candidate.get("target_season"),
                candidate.get("target_episodes"),
                candidate.get("supports_file_preview"),
                str(
                    (candidate.get("provider_data") or {}).get("detail_path")
                    or ""
                ),
            )
        finally:
            self._hdhive_unlock_operation_lock.release()

    def _unlock(
            self,
            slug: str,
            unlock_points: int,
            resource_type: str,
            media_page_url: str = "",
            search_label: str = "",
            is_unlocked: bool = False,
            target_season: Optional[int] = None,
            target_episodes: Optional[List[int]] = None,
            supports_file_preview: Optional[bool] = None,
            detail_path: str = "",
    ) -> Any:
        """
        供 SyncHandler 调用的按需积分解锁，支持 OpenAPI 和 Web。
        :param slug: 资源的标识符
        :param unlock_points: 本次需消耗的积分
        :return: 成功返回真实的 url；多文件ED2K资源返回URL列表，失败返回 None
        """
        search_prefix = (
            f"[{search_label}][HDHIVE]" if search_label else "[HDHIVE]"
        )
        if self._stop_requested():
            logger.info(f"{search_prefix} 已停止任务，跳过积分解锁")
            return None

        if self._hdhive_query_mode == "web":
            with self._hdhive_web_lock:
                web_client = self._hdhive_web_client
            cooldown_remaining = (
                web_client.cooldown_remaining if web_client else 0.0
            )
            if cooldown_remaining > 0:
                logger.debug(
                    f"{search_prefix} HDHive WebAPI 仍在风控冷却，"
                    "跳过本次解锁"
                    f"（剩余 {int(cooldown_remaining + 0.999)} 秒）"
                )
                return None

        if str(resource_type or "").strip().lower() not in HDHIVE_DETAIL_RESOURCE_TYPES:
            logger.warning(
                f"{search_prefix} 拒绝解锁不支持或未标注类型的资源："
                f"slug={slug or '<empty>'}，"
                f"resource_type={resource_type or '<empty>'}"
            )
            return None

        normalized_slug = str(slug or "").strip()
        normalized_type = str(resource_type or "").strip().lower()
        cache_key = (
            "web" if self._hdhive_query_mode == "web" else "api",
            normalized_type,
            normalized_slug,
        )
        cache_key = normalize_platform_cache_key(cache_key)
        cached_url = self._hdhive_budget.cached_url(cache_key)
        if cached_url and self._valid_share_value(cached_url, normalized_type):
            logger.debug(
                f"{search_prefix} 复用已解锁 HDHive 资源链接："
                f"slug={normalized_slug}，跳过重复解锁"
            )
            return cached_url
        if cached_url:
            logger.warning(
                f"{search_prefix} 已缓存 HDHive 资源链接格式无效，"
                f"忽略缓存并重新读取：slug={normalized_slug}"
            )
            self._hdhive_budget.discard_cached_url(cache_key)

        budget_status = self._hdhive_budget.status(unlock_points)
        if budget_status is None:
            logger.warning(
                f"{search_prefix} 解锁积分无效：slug={normalized_slug}，"
                f"points={unlock_points}"
            )
            return None
        unlock_points = budget_status.requested
        if not budget_status.task_allowed:
            logger.warning(
                f"{search_prefix} 全局积分预算不足："
                f"已花费 {budget_status.task_spent}，需 {unlock_points}，"
                f"总预算 {budget_status.task_limit}"
            )
            return None

        if not budget_status.subscribe_allowed:
            logger.warning(
                f"{search_prefix} 单订阅积分预算不足："
                f"已花费 {budget_status.subscribe_spent}，需 {unlock_points}，"
                f"预算 {budget_status.subscribe_limit}"
            )
            return None

        try:
            share_url = ""
            deducted_points = 0
            mode_label = "WebAPI" if self._hdhive_query_mode == "web" else "OpenAPI"
            action_label = (
                "读取已解锁资源链接"
                if is_unlocked
                else "获取零积分资源链接"
                if unlock_points <= 0
                else "按需解锁资源"
            )
            media_page_suffix = (
                f"，媒体页={media_page_url}" if media_page_url else ""
            )
            logger.debug(
                f"{search_prefix} {mode_label} {action_label}："
                f"slug={slug}，积分={unlock_points}"
                f"{media_page_suffix}"
            )

            if self._hdhive_query_mode == "web":
                if not self._hdhive_username or not self._hdhive_password:
                    logger.warning("HDHive WebAPI 缺少用户名或密码，无法积分解锁")
                    return None
                resources = self._get_hdhive_web_resources()
                unlock_result = resources.unlock_resource(
                    slug,
                    resource_type=resource_type,
                    media_page_url=media_page_url,
                    is_unlocked=is_unlocked,
                    target_season=target_season,
                    target_episodes=target_episodes,
                    supports_file_preview=supports_file_preview,
                    detail_path=detail_path,
                    log_prefix=search_prefix,
                )
                share_url = unlock_result.get("url") or ""
                deducted_points = (
                    0
                    if is_unlocked or unlock_result.get("already_owned")
                    else unlock_points
                )
                skip_reason = str(unlock_result.get("skip_reason") or "")
                if skip_reason:
                    reason_label = {
                        "target_not_covered": "file-list 未覆盖当前缺集",
                        "resource_invalid": "file-list 标记资源失效",
                    }.get(skip_reason, skip_reason)
                    logger.debug(
                        f"{search_prefix} {mode_label} 预览后跳过资源："
                        f"{reason_label}"
                    )
                    return None
            else:
                from .open import HDHiveOpenAPIError
                if not self._hdhive_client or not self._hdhive_client.is_ready:
                    logger.warning("HDHive API 模式需要应用 Secret 和有效用户 Token 才能解锁")
                    return None
                try:
                    unlock_data = self._hdhive_client.unlock_resource(
                        slug, max_unlock_points=unlock_points
                    )
                except HDHiveOpenAPIError as e:
                    logger.error(f"HDHive (API) 解锁请求失败: [{e.code}] {e.message} {e.description}")
                    return None
                if unlock_data.get("success") and unlock_data.get("data"):
                    result_data = unlock_data["data"]
                    share_url = result_data.get("full_url") or result_data.get("url") or ""
                actual_points = self._hdhive_budget.normalize_points(
                    unlock_data.get("actual_points")
                )
                if (
                        actual_points is None
                        and share_url
                        and not is_unlocked
                ):
                    actual_points = unlock_points
                deducted_points = actual_points or 0

            if share_url and not self._valid_share_value(
                    share_url, normalized_type
            ):
                logger.error(
                    f"{search_prefix} {mode_label} 返回的资源链接格式无效，"
                    f"已拒绝使用：slug={normalized_slug}"
                )
                share_url = ""
            recorded_points, _, _ = self._hdhive_budget.record_result(
                cache_key, share_url, deducted_points
            )
            if not share_url:
                if recorded_points <= 0:
                    logger.error(
                        f"{search_prefix} {mode_label} 获取后未获得资源链接"
                    )
                    return None
                logger.error(
                    f"{search_prefix} {mode_label} 未返回资源链接；"
                    f"积分账本已记录 {recorded_points} 积分"
                )
                return None

            if recorded_points <= 0:
                logger.debug(
                    f"{search_prefix} {mode_label} "
                    f"{'已读取已解锁资源链接' if is_unlocked else '已取得零积分资源链接'}，"
                    "未消耗积分"
                )
            else:
                remaining_task, remaining_subscribe = (
                    self._hdhive_budget.remaining()
                )
                logger.debug(
                    f"{search_prefix} {mode_label} 成功解锁并记录 "
                    f"{recorded_points} 积分；"
                    f"全局剩余 {remaining_task}，"
                    f"当前订阅剩余 {remaining_subscribe}"
                )
            return share_url

        except HDHiveWebError as e:
            message = (
                f"{search_prefix} {self._hdhive_query_mode} 解锁异常: {e}，"
                f"code={e.code or '-'}，status={e.status_code or 0}"
            )
            if e.code in {"rate_limited", "server_cooldown", "stopped"}:
                logger.debug(message)
            else:
                logger.error(message)
            return None
        except Exception as e:
            logger.error(f"{search_prefix} {self._hdhive_query_mode} 解锁异常: {e}")
            return None
