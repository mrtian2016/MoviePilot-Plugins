"""
搜索处理模块
"""
import copy
import hashlib
import json
import re
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any

from app.log import logger
from app.schemas import MediaInfo
from app.schemas.types import MediaType

from .platform_rules import PlatformRuleService
from ...core import (
    SearchCapability,
    SearchQuery,
    format_search_label,
    format_search_log_prefix,
    get_component,
    resolve_component,
)
from ...core.media import tmdb_id_of
from ...search.dian115 import Dian115SearchService
from ...search.hdhive import HDHiveSearchService
from ...search.juying import JuyingResourceService
from ...search.matching import positive_ints, unique_texts
from ...search.pansou import PanSouSearchService
from ...search.registry import create_search_registry
from ...search.types import SUPPORTED_RESOURCE_TYPES, normalize_resource_type
from ...utils.cache import create_platform_ttl_cache

_COMPONENT_TYPES = (
    HDHiveSearchService,
    Dian115SearchService,
    PanSouSearchService,
    PlatformRuleService,
)


class SearchHandler:
    """搜索处理器"""

    _TEST_RESULT_LIMIT = 10

    @staticmethod
    def _normalize_pansou_values(value: Any) -> List[str]:
        if isinstance(value, str):
            value = re.split(r"[,，\n]+", value)
        return unique_texts(value)

    def __getattr__(self, name):
        return resolve_component(
            self, _COMPONENT_TYPES, name, "_search_components"
        )

    def __init__(
            self,
            pansou_client,
            hdhive_client=None,
            seedhub_client=None,
            juying_client=None,
            pinglian_client=None,
            online_docs_client=None,
            hdhive_web_client=None,
            hdhive_web_client_owned: bool = True,
            pansou_enabled: bool = False,
            hdhive_enabled: bool = False,
            dian115_enabled: bool = False,
            seedhub_enabled: bool = False,
            juying_enabled: bool = False,
            pinglian_enabled: bool = False,
            hdhive_username: str = "",
            hdhive_password: str = "",
            hdhive_query_mode: str = "web",
            hdhive_auto_unlock: bool = False,
            hdhive_max_unlock_points: int = 50,
            hdhive_max_points_per_sub: int = 20,
            dian115_email: str = "",
            dian115_password: str = "",
            dian115_auto_unlock: bool = False,
            dian115_max_unlock_points: int = 50,
            dian115_max_points_per_sub: int = 20,
            pansou_channels: Any = None,
            pansou_plugins: Any = None,
            pansou_cloud_types: Any = None,
            pansou_filter_include: Any = None,
            pansou_filter_exclude: Any = None,
            resource_type_order: Optional[List[str]] = None,
            pansou_concurrency: Optional[int] = None,
            pansou_result_limit: int = 10,
            pansou_refresh: bool = True,
            pansou_timeout: int = 30,
            seedhub_result_limit: int = 20,
            piratebay_client: Any = None,
            piratebay_enabled: bool = False,
            piratebay_result_limit: int = 20,
            uindex_client: Any = None,
            uindex_enabled: bool = False,
            uindex_result_limit: int = 20,
            juying_result_limit: int = 5,
            pinglian_result_limit: int = 20,
            search_source_order: Optional[List[str]] = None,
            search_proxy: Any = None,
            search_cache_enabled: bool = True,
            search_cache_ttl_minutes: int = 30,
            search_concurrency: int = 2,
            hdhive_candidate_limit: int = 4,
            hdhive_request_interval: float = 5.0,
            hdhive_unlocks_per_minute: int = 2,
            dian115_candidate_limit: int = 4,
            dian115_request_interval: float = 1.0,
            dian115_unlocks_per_minute: int = 6,
            hdhive_torrentclaw_enabled: bool = False,
            hdhive_torrentclaw_subtitle_languages: Any = None,
            enable_cloud_upgrade: bool = False,
            upgrade_subscribe_ids: Optional[List[int]] = None,
            should_stop: Any = None,
    ):
        """
        初始化搜索处理器

        :param pansou_client: PanSou 客户端实例
        :param pansou_enabled: 是否启用 PanSou
        :param pansou_channels: PanSou 搜索频道
        :param search_source_order: 自定义搜索源优先级列表
        """
        self._pansou_client = pansou_client
        self._hdhive_client = hdhive_client
        self._seedhub_client = seedhub_client
        self._piratebay_client = piratebay_client
        self._uindex_client = uindex_client
        self._juying_client = juying_client
        self._pinglian_client = pinglian_client
        self._online_docs_client = online_docs_client
        self._juying_resources = (
            JuyingResourceService(juying_client) if juying_client else None
        )
        self._pansou_enabled = pansou_enabled
        self._hdhive_enabled = hdhive_enabled
        self._dian115_enabled = bool(dian115_enabled)
        self._seedhub_enabled = bool(seedhub_enabled)
        self._piratebay_enabled = bool(piratebay_enabled)
        self._piratebay_result_limit = int(piratebay_result_limit or 20)
        self._uindex_enabled = bool(uindex_enabled)
        self._uindex_result_limit = int(uindex_result_limit or 20)
        self._juying_enabled = bool(juying_enabled)
        self._pinglian_enabled = bool(pinglian_enabled)
        self._online_docs_enabled = bool(online_docs_client)
        self._hdhive_username = hdhive_username
        self._hdhive_password = hdhive_password
        self._hdhive_query_mode = str(hdhive_query_mode or "web")
        if self._hdhive_query_mode not in {"api", "web"}:
            self._hdhive_query_mode = "web"
        self._hdhive_auto_unlock = hdhive_auto_unlock
        self._hdhive_web_client = hdhive_web_client
        self._hdhive_web_client_owned = bool(
            hdhive_web_client is None or hdhive_web_client_owned
        )
        self._hdhive_web_resources = None
        self._hdhive_web_lock = threading.RLock()
        self._hdhive_unlock_operation_lock = threading.Lock()
        self._dian115_email = str(dian115_email or "").strip()
        self._dian115_password = str(dian115_password or "").strip()
        self._dian115_auto_unlock = bool(dian115_auto_unlock)
        self._dian115_max_unlock_points = max(
            0, int(dian115_max_unlock_points or 0)
        )
        self._dian115_max_points_per_sub = max(
            0, int(dian115_max_points_per_sub or 0)
        )
        self._dian115_client = None
        self._dian115_resources = None
        self._dian115_client_lock = threading.RLock()
        self._hdhive_max_unlock_points = hdhive_max_unlock_points
        self._hdhive_max_points_per_sub = hdhive_max_points_per_sub
        self._pansou_channels = self._normalize_pansou_values(pansou_channels)
        self._pansou_plugins = self._normalize_pansou_values(pansou_plugins)
        self._pansou_cloud_types = [
            value.lower() for value in self._normalize_pansou_values(
                pansou_cloud_types
            )
        ]
        self._pansou_filter = {
            "include": self._normalize_pansou_values(pansou_filter_include),
            "exclude": self._normalize_pansou_values(pansou_filter_exclude),
        }
        self._resource_type_order_config = list(
            ["115", "ed2k"]
            if resource_type_order is None else resource_type_order
        )
        self._resource_type_order_map = {}
        for index, value in enumerate(self._resource_type_order_config):
            self._resource_type_order_map.setdefault(value, index)
        try:
            self._pansou_concurrency = (
                max(1, min(int(pansou_concurrency), 100))
                if pansou_concurrency else None
            )
        except (TypeError, ValueError):
            self._pansou_concurrency = None
        self._pansou_result_limit = max(1, min(int(pansou_result_limit or 10), 100))
        self._pansou_refresh = bool(pansou_refresh)
        self._pansou_timeout = max(5, min(int(pansou_timeout or 30), 120))
        self._seedhub_result_limit = max(
            1, min(int(seedhub_result_limit or 20), 80)
        )
        self._juying_result_limit = max(
            1, min(int(juying_result_limit or 5), 20)
        )
        self._pinglian_result_limit = max(
            1, min(int(pinglian_result_limit or 20), 80)
        )
        self._juying_resource_types = [
            value for value in unique_texts(
                self._resource_type_order_config, str.lower
            )
            if value in SUPPORTED_RESOURCE_TYPES
        ]
        self._search_source_order = search_source_order or []
        self._search_proxy = search_proxy
        self._search_cache_enabled = bool(search_cache_enabled)
        self._search_cache_ttl = max(60, int(search_cache_ttl_minutes or 30) * 60)
        self._search_concurrency = max(1, min(int(search_concurrency or 1), 5))
        self._hdhive_candidate_limit = max(1, min(int(hdhive_candidate_limit or 4), 20))
        self._hdhive_request_interval = max(
            2.0, min(float(hdhive_request_interval or 5.0), 10.0)
        )
        self._hdhive_unlocks_per_minute = max(
            1, min(int(hdhive_unlocks_per_minute or 2), 3)
        )
        self._dian115_candidate_limit = max(
            1, min(int(dian115_candidate_limit or 4), 20)
        )
        self._dian115_request_interval = max(
            0.2, min(float(dian115_request_interval or 1.0), 10.0)
        )
        self._dian115_unlocks_per_minute = max(
            1, min(int(dian115_unlocks_per_minute or 6), 10)
        )
        self._hdhive_torrentclaw_enabled = bool(
            hdhive_torrentclaw_enabled
            and "magnet" in self._resource_type_order_config
        )
        raw_subtitle_languages = hdhive_torrentclaw_subtitle_languages or ["zh"]
        if isinstance(raw_subtitle_languages, str):
            raw_subtitle_languages = re.split(r"[,，\s]+", raw_subtitle_languages)
        self._hdhive_torrentclaw_subtitle_languages = unique_texts(
            raw_subtitle_languages,
            lambda value: value.lower().replace("_", "-"),
        )
        self._enable_cloud_upgrade = bool(enable_cloud_upgrade)
        self._upgrade_subscribe_ids = list(upgrade_subscribe_ids or [])
        self._upgrade_subscribe_id_set = {
            str(value) for value in self._upgrade_subscribe_ids
        }
        self._search_cache_limit = 200
        self._search_negative_ttl = min(self._search_cache_ttl, 10 * 60)
        self._search_cache = create_platform_ttl_cache(
            "search:results",
            self,
            maxsize=self._search_cache_limit,
            ttl=self._search_cache_ttl,
        )
        self._search_metrics_lock = threading.RLock()
        self._search_metrics: Dict[str, Dict[str, int]] = {}
        self._platform_filter_lock = threading.RLock()
        self._platform_filter_module = None
        self._platform_filter_signature = ""
        self._platform_filter_signature_cache = create_platform_ttl_cache(
            "platform:filter_rules", maxsize=1, ttl=5
        )
        self._should_stop = should_stop
        self._search_registry = create_search_registry(
            self,
            get_component(self, PanSouSearchService, "_search_components"),
            get_component(self, HDHiveSearchService, "_search_components"),
            get_component(self, Dian115SearchService, "_search_components"),
        )

    def _is_cloud_upgrade_subscribe(self, subscribe: Any) -> bool:
        """判断订阅是否属于插件网盘洗版范围。"""
        if self._enable_cloud_upgrade and bool(
                getattr(subscribe, "_manual_upgrade", False)
        ):
            return True
        if (
                not self._enable_cloud_upgrade
                or not subscribe
                or not bool(getattr(subscribe, "best_version", False))
        ):
            return False
        selected_ids = self._upgrade_subscribe_id_set
        return not selected_ids or str(getattr(subscribe, "id", "")) in selected_ids

    def _stop_requested(self) -> bool:
        try:
            return bool(self._should_stop and self._should_stop())
        except Exception as error:
            logger.warning(f"读取搜索停止状态失败：{error}")
            return False

    def get_enabled_sources(self) -> List[str]:
        """返回用户选择且当前已启用的搜索渠道（供自动追剧与定时下载使用）。"""
        available_set = {
            provider.key for provider in self._search_registry.available()
        }
        enabled_map = {
            "pansou": getattr(self, "_pansou_enabled", True),
            "hdhive": getattr(self, "_hdhive_enabled", False),
            "seedhub": getattr(self, "_seedhub_enabled", False),
            "piratebay": getattr(self, "_piratebay_enabled", False),
            "uindex": getattr(self, "_uindex_enabled", False),
            "juying": getattr(self, "_juying_enabled", False),
            "pinglian": getattr(self, "_pinglian_enabled", False),
            "online_docs": getattr(self, "_online_docs_enabled", False),
            "dian115": True,
        }
        return [
            source for source in self._search_source_order
            if source in available_set and enabled_map.get(source, True)
        ]

    def get_all_search_sources(self) -> List[str]:
        """返回所有当前已注册的搜索渠道（供网盘资源列表实时嗅探使用，无需手动启用即可搜索）。"""
        return [provider.key for provider in self._search_registry.available()]

    @property
    def source_concurrency_enabled(self) -> bool:
        return self._search_concurrency > 1

    def _search_cache_key(
            self,
            source: str,
            mediainfo: MediaInfo,
            media_type: MediaType,
            season: Optional[int],
            target_episodes: Optional[List[int]],
            subscribe: Any,
    ) -> str:
        media_id = (
                getattr(mediainfo, "tmdb_id", None)
                or tmdb_id_of(subscribe)
        )
        context = {
            "source": source,
            "tmdb_id": media_id,
            "title": str(getattr(mediainfo, "title", "") or "").strip(),
            "year": getattr(mediainfo, "year", None),
            "type": getattr(media_type, "value", str(media_type)),
            "season": int(season or 0),
            "episodes": sorted(positive_ints(target_episodes)),
            "best_version": self._is_cloud_upgrade_subscribe(subscribe),
            "filter_groups": list(getattr(subscribe, "filter_groups", None) or []),
            "provider": dict(
                self._search_registry.get(source).policy.cache_context
            ),
        }
        encoded = json.dumps(context, ensure_ascii=False, sort_keys=True, default=str)
        return hashlib.sha1(encoded.encode("utf-8")).hexdigest()

    def reset_search_metrics(self) -> None:
        with self._search_metrics_lock:
            self._search_metrics = {}

    def _record_search_metric(self, source: str, metric: str, value: int = 1) -> None:
        with self._search_metrics_lock:
            counters = self._search_metrics.setdefault(
                str(source or "unknown"),
                {
                    "external_calls": 0,
                    "positive_cache_hits": 0,
                    "negative_cache_hits": 0,
                    "external_elapsed_ms": 0,
                },
            )
            counters[metric] = int(counters.get(metric) or 0) + int(value or 0)

    def get_search_metrics(self) -> Dict[str, Dict[str, int]]:
        with self._search_metrics_lock:
            return copy.deepcopy(self._search_metrics)

    def _get_cached_results(
            self, key: str, source: str, search_label: str
    ) -> Optional[List[Dict]]:
        if not self._search_cache_enabled:
            return None
        item = self._search_cache.get(key)
        if not isinstance(item, dict):
            return None
        cached_results = item.get("results")
        results = copy.deepcopy(cached_results) if isinstance(cached_results, list) else None
        if results is None:
            return None
        policy = self._search_registry.get(source).policy
        if not results and not policy.cache_empty_results:
            self._search_cache.pop(key, None)
            return None
        self._record_search_metric(
            source,
            "negative_cache_hits" if not results else "positive_cache_hits",
        )
        logger.debug(
            f"[{search_label}][{source.upper()}] 搜索缓存命中：候选={len(results)}"
            f"{'（空结果缓存）' if not results else ''}"
        )
        return results

    def _set_cached_results(
            self, key: str, label: str, results: List[Dict], source: str = ""
    ) -> None:
        if not self._search_cache_enabled:
            return
        policy = self._search_registry.get(source).policy
        if not results and not policy.cache_empty_results:
            return
        self._search_cache.set(
            key,
            {
                "label": label,
                "results": copy.deepcopy(list(results or [])),
                "negative": not bool(results),
            },
            ttl=self._search_negative_ttl if not results else self._search_cache_ttl,
        )

    def get_cache_stats(self) -> Dict[str, Any]:
        """返回搜索缓存占用，并顺带清理过期项。"""
        positive = 0
        negative = 0
        for _, item in self._search_cache.items():
            if not isinstance(item, dict):
                continue
            if item.get("negative"):
                negative += 1
            else:
                positive += 1
        return {
            "enabled": self._search_cache_enabled,
            "entries": positive + negative,
            "positive": positive,
            "negative": negative,
            "limit": self._search_cache_limit,
            "ttl_seconds": self._search_cache_ttl,
            "negative_ttl_seconds": self._search_negative_ttl,
        }

    def clear_search_cache(self) -> Dict[str, int]:
        """清空搜索结果及各搜索源的详情、预览和响应缓存。"""
        search_count = len(list(self._search_cache.items()))
        self._search_cache.clear()
        self._platform_filter_signature_cache.clear()
        source_counts: Dict[str, int] = {}
        for provider in self._search_registry.available():
            if provider.supports(SearchCapability.POINT_BUDGET):
                source_counts[f"{provider.key}_unlocked_urls"] = int(
                    provider.require(
                        SearchCapability.POINT_BUDGET
                    ).clear_cached_urls() or 0
                )
            if provider.supports(SearchCapability.CACHE_MAINTENANCE):
                source_counts[provider.key] = int(provider.clear_cache() or 0)
        return {
            "search_results": search_count,
            **source_counts,
        }

    def _providers_with(self, capability: SearchCapability):
        return tuple(
            provider for provider in self._search_registry.available()
            if provider.supports(capability)
        )

    def clear_point_history(self) -> Dict[str, int]:
        """清空所有积分搜索渠道的持久化消费历史。"""
        return {
            provider.key: int(provider.require(
                SearchCapability.POINT_BUDGET
            ).clear_history() or 0)
            for provider in self._providers_with(SearchCapability.POINT_BUDGET)
        }

    def has_unlock_budget(self, source: str, points: Any) -> bool:
        provider = self._search_registry.get(source)
        return bool(provider.require(
            SearchCapability.POINT_BUDGET
        ).has_budget(points))

    def source_name(self, source: str) -> str:
        return self._search_registry.get(source).name

    def supports(self, source: str, capability: SearchCapability) -> bool:
        try:
            return self._search_registry.get(source).supports(capability)
        except KeyError:
            return False

    def get_source_client(self, source: str) -> Any:
        return self._search_registry.get(source).require(
            SearchCapability.ACCOUNT
        )

    def unlock_resource(
            self,
            source: str,
            candidate: Dict[str, Any],
            search_label: str = "",
    ) -> Any:
        return self._search_registry.get(source).unlock(
            candidate, search_label=search_label
        )

    def preview_resource(
            self, source: str, candidate: Dict[str, Any]
    ) -> Dict[str, Any]:
        return self._search_registry.get(source).preview(candidate)

    def close(self, release_cache: bool = False) -> None:
        """释放搜索客户端。"""
        for provider in self._providers_with(SearchCapability.LIFECYCLE):
            provider.close()
        if (
                release_cache
                and self._hdhive_client
                and hasattr(self._hdhive_client, "close")
        ):
            self._hdhive_client.close()
        if release_cache and self._juying_client:
            self._juying_client.close()
        if release_cache and self._pinglian_client:
            self._pinglian_client.close()

    def configure_point_storage(self, get_data, save_data) -> None:
        """为所有积分搜索渠道配置持久化读写。"""
        for provider in self._providers_with(SearchCapability.POINT_BUDGET):
            provider.require(SearchCapability.POINT_BUDGET).configure_storage(
                get_data, save_data
            )

    def reset_point_budgets(self) -> None:
        """重置本轮同步的全部积分渠道任务预算。"""
        for provider in self._providers_with(SearchCapability.POINT_BUDGET):
            provider.require(SearchCapability.POINT_BUDGET).reset_task()

    def reset_subscription_budgets(self, subscription_key: str = "") -> None:
        """加载当前订阅在全部积分渠道中的历史消费。"""
        for provider in self._providers_with(SearchCapability.POINT_BUDGET):
            provider.require(SearchCapability.POINT_BUDGET).reset_subscription(
                subscription_key
            )

    def clear_subscription_budgets(self, subscription_key: str) -> None:
        """订阅完成后清理全部积分渠道的历史账本。"""
        for provider in self._providers_with(SearchCapability.POINT_BUDGET):
            provider.require(SearchCapability.POINT_BUDGET).clear_subscription(
                subscription_key
            )

    def _run_source_search(
            self,
            source: str,
            mediainfo: MediaInfo,
            media_type: MediaType,
            season: Optional[int] = None,
            target_episodes: Optional[List[int]] = None,
            target_episode_air_dates: Optional[Dict[int, str]] = None,
            subscribe: Any = None,
            resource_list_mode: bool = False,
            result_limit: Optional[int] = None,
    ) -> List[Dict]:
        try:
            provider = self._search_registry.get(source)
        except KeyError as error:
            raise ValueError("搜索渠道未配置或不可用") from error
        query = SearchQuery(
            mediainfo=mediainfo,
            media_type=media_type,
            season=season,
            target_episodes=tuple(target_episodes or ()),
            target_episode_air_dates=dict(target_episode_air_dates or {}),
            subscribe=subscribe,
            resource_list_mode=resource_list_mode,
            result_limit=result_limit,
        )
        prefix = format_search_log_prefix(query, provider.key)
        started = time.monotonic()
        try:
            results = provider.search(query)
        except Exception as error:
            logger.warning(
                f"{prefix} 搜索失败：{error}，"
                f"耗时={time.monotonic() - started:.2f}s"
            )
            raise
        logger.debug(
            f"{prefix} 搜索完成："
            f"候选={len(results)}，耗时={time.monotonic() - started:.2f}s"
        )
        return results

    def _prepare_source_results(
            self,
            results: List[Dict],
            source: str,
            mediainfo: MediaInfo,
            media_type: MediaType,
            subscribe: Any,
            season: Optional[int],
            target_episodes: Optional[List[int]],
            apply_platform_rules: bool,
    ) -> List[Dict]:
        for result in results:
            result.setdefault("source", source)
        ordered = self._prefilter_resource_order(
            results,
            season=season,
            target_episodes=target_episodes,
            log_prefix=f"[{self._search_label(mediainfo, media_type, season)}]"
                       f"[{source.upper()}]",
            filter_unsupported_types=apply_platform_rules,
        )
        if not apply_platform_rules:
            return ordered
        return self._filter_by_platform_rules(
            ordered,
            mediainfo,
            subscribe,
            season=season,
            target_episodes=target_episodes,
            prefiltered=True,
        )

    def test_source_result_limit(self) -> int:
        return self._TEST_RESULT_LIMIT

    def resolve_source_resource(self, source: str, **kwargs) -> Dict[str, Any]:
        try:
            provider = self._search_registry.get(source)
            provider.require(SearchCapability.RESOURCE_RESOLVE)
        except (KeyError, RuntimeError) as error:
            raise ValueError("搜索渠道未配置资源解析能力") from error
        return provider.resolve(**kwargs)

    def test_source(
            self,
            source: str,
            mediainfo: MediaInfo,
            media_type: MediaType,
            season: Optional[int] = None,
    ) -> List[Dict]:
        source = str(source or "").strip().lower()
        try:
            provider = self._search_registry.get(source)
        except KeyError as error:
            raise ValueError("搜索渠道未配置或不可用") from error
        cache_key = self._search_cache_key(
            source, mediainfo, media_type, season, None, None
        )
        self._search_cache.pop(cache_key, None)
        if provider.supports(SearchCapability.CACHE_MAINTENANCE):
            provider.clear_cache()
        results = self._run_source_search(
            source,
            mediainfo,
            media_type,
            season,
            resource_list_mode=True,
            result_limit=self._TEST_RESULT_LIMIT,
        )
        return list(results)[:self._TEST_RESULT_LIMIT]

    def search_single_source(
            self,
            source: str,
            mediainfo: MediaInfo,
            media_type: MediaType,
            season: Optional[int] = None,
            target_episodes: Optional[List[int]] = None,
            target_episode_air_dates: Optional[Dict[int, str]] = None,
            subscribe: Any = None,
            apply_platform_rules: bool = True,
            force_refresh: bool = False,
            result_limit: Optional[int] = None,
    ) -> List[Dict]:
        source = str(source or "").strip().lower()
        if self._stop_requested():
            return []
        try:
            provider = self._search_registry.get(source)
        except KeyError:
            search_label = self._search_label(mediainfo, media_type, season)
            logger.warning(f"[{search_label}][{source.upper()}] 未知的搜索源")
            return []
        cache_key = self._search_cache_key(
            source, mediainfo, media_type, season, target_episodes, subscribe
        )
        search_label = self._search_label(mediainfo, media_type, season)
        results = (
            self._get_cached_results(cache_key, source, search_label)
            if (provider.policy.cacheable and not force_refresh and result_limit is None) else None
        )
        if results is not None:
            return self._prepare_source_results(
                results,
                source,
                mediainfo,
                media_type,
                subscribe,
                season,
                target_episodes,
                apply_platform_rules,
            )

        external_started = time.monotonic()
        try:
            results = self._run_source_search(
                source,
                mediainfo,
                media_type,
                season,
                target_episodes,
                target_episode_air_dates,
                subscribe,
                resource_list_mode=not apply_platform_rules,
                result_limit=result_limit,
            )
        except Exception as error:
            logger.warning(
                f"[{search_label}][{source.upper()}] 外部查询抛出异常：{error}",
                exc_info=True,
            )
            return []
        finally:
            self._record_search_metric(source, "external_calls")
            self._record_search_metric(
                source,
                "external_elapsed_ms",
                int((time.monotonic() - external_started) * 1000),
            )
        if self._stop_requested():
            return []
        label = f"[{search_label}][{source.upper()}]"
        if provider.policy.cacheable and result_limit is None:
            self._set_cached_results(cache_key, label, results, source=source)
        return self._prepare_source_results(
            results,
            source,
            mediainfo,
            media_type,
            subscribe,
            season,
            target_episodes,
            apply_platform_rules,
        )

    def search_sources(
            self,
            sources: List[str],
            mediainfo: MediaInfo,
            media_type: MediaType,
            season: Optional[int] = None,
            target_episodes: Optional[List[int]] = None,
            target_episode_air_dates: Optional[Dict[int, str]] = None,
            subscribe: Any = None,
            apply_platform_rules: bool = True,
            force_refresh: bool = False,
            result_limit: Optional[int] = None,
    ) -> Dict[str, List[Dict]]:
        """并发查询相互独立的来源；各来源内部仍遵守自己的限流和串行约束。"""
        ordered_sources = list(dict.fromkeys(sources or []))
        search_label = self._search_label(mediainfo, media_type, season)
        if len(ordered_sources) <= 1 or self._search_concurrency <= 1:
            return {
                source: self.search_single_source(
                    source=source,
                    mediainfo=mediainfo,
                    media_type=media_type,
                    season=season,
                    target_episodes=target_episodes,
                    target_episode_air_dates=target_episode_air_dates,
                    subscribe=subscribe,
                    apply_platform_rules=apply_platform_rules,
                    force_refresh=force_refresh,
                    result_limit=result_limit,
                )
                for source in ordered_sources
            }

        results: Dict[str, List[Dict]] = {source: [] for source in ordered_sources}
        workers = min(self._search_concurrency, len(ordered_sources))
        executor = ThreadPoolExecutor(
            max_workers=workers,
            thread_name_prefix="pansearch-search",
        )
        stopped = False
        try:
            futures = {
                executor.submit(
                    self.search_single_source,
                    source,
                    mediainfo,
                    media_type,
                    season,
                    target_episodes,
                    target_episode_air_dates,
                    subscribe,
                    apply_platform_rules,
                    force_refresh,
                    result_limit,
                ): source
                for source in ordered_sources
            }
            pending = set(futures)
            while pending:
                if self._stop_requested():
                    stopped = True
                    for future in pending:
                        future.cancel()
                    logger.info(
                        f"⏹️ [{search_label}] 已停止等待搜索源，未开始的查询已取消"
                    )
                    break
                done, pending = wait(pending, timeout=0.25, return_when=FIRST_COMPLETED)
                for future in done:
                    source = futures[future]
                    try:
                        results[source] = future.result()
                    except Exception as error:
                        logger.error(
                            f"[{search_label}] 搜索源 {source} 并发查询失败：{error}"
                        )
        finally:
            executor.shutdown(wait=not stopped, cancel_futures=stopped)
        if not stopped:
            logger.debug(
                f"[{search_label}] 搜索源查询完成："
                + " / ".join(
                    f"{source.upper()}={len(results.get(source) or [])}"
                    for source in ordered_sources
                )
            )
        return results

    @staticmethod
    def _search_label(
            mediainfo: MediaInfo, media_type: MediaType, season: Optional[int] = None
    ) -> str:
        return format_search_label(mediainfo, media_type, season)

    @staticmethod
    def _resource_timestamp(value: Any) -> float:
        text = str(value or "").strip()
        if not text:
            return 0
        if text.isdigit():
            timestamp = float(text)
            return timestamp / 1000 if timestamp > 10_000_000_000 else timestamp
        try:
            parsed = datetime.fromisoformat(text.replace("/", "-").replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.timestamp()
        except ValueError:
            return 0

    @staticmethod
    def _resource_unlock_points(value: Any) -> int:
        try:
            return max(0, int(value or 0))
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _resource_type(resource: Dict[str, Any]) -> str:
        """读取内部规范化类型；pan_type 仅用于尚未规范化的外部来源。"""
        return normalize_resource_type(
            resource.get("resource_type") or resource.get("pan_type")
        )

    @classmethod
    def _resource_availability_order(cls, resource: Dict[str, Any]) -> int:
        """直链或已解锁优先，其次免费访问，最后才是积分解锁。"""
        if str(resource.get("url") or "").strip() or resource.get("is_unlocked") is True:
            return 0
        if (
                resource.get("is_free") is True
                or not resource.get("need_unlock")
        ):
            return 1
        return 2

    @staticmethod
    def _resource_preview_episode_set(
            resource: Dict[str, Any], season: Optional[int]
    ) -> Optional[set]:
        preview = resource.get("preview_episodes")
        if not preview:
            return None
        if not isinstance(preview, dict):
            return None
        if season is None:
            values = [
                episode
                for episodes in preview.values()
                for episode in (episodes or [])
            ]
        else:
            season_key = str(int(season))
            if season_key not in preview:
                return set()
            values = preview.get(season_key) or []
        return positive_ints(values)

    @classmethod
    def _resource_target_coverage(
            cls,
            resource: Dict[str, Any],
            season: Optional[int],
            targets: set,
    ) -> tuple:
        if not targets:
            return 0, 0
        preview = cls._resource_preview_episode_set(resource, season)
        if preview is None:
            return 2, 0
        covered = targets & preview
        if not covered:
            return 3, 0
        if covered == targets:
            return 0, -len(covered)
        return 1, -len(covered)

    def _resource_type_order(self, resource: Dict[str, Any]) -> int:
        """按配置的资源类型优先级排序。"""
        return self._resource_type_order_map.get(
            self._resource_type(resource), len(self._resource_type_order_config)
        )

    def _resource_sort_key(
            self, resource: Dict[str, Any], season: Optional[int], targets: set
    ) -> tuple:
        return (
            self._resource_type_order(resource),
            self._resource_availability_order(resource),
            resource.get("is_official") is not True,
            *self._resource_target_coverage(resource, season, targets),
            self._resource_unlock_points(resource.get("unlock_points")),
            -int(resource.get("platform_priority") or 0),
            -self._resource_timestamp(resource.get("update_time")),
        )

    def _prefilter_resource_order(
            self,
            resources: List[Dict],
            season: Optional[int] = None,
            target_episodes: Optional[List[int]] = None,
            log_prefix: str = "",
            filter_unsupported_types: bool = True,
    ) -> List[Dict]:
        """按类型、可用性、集数覆盖和积分筛选排序。"""
        targets = positive_ints(target_episodes)
        prepared = []
        unsupported_type_count = 0
        uncovered_count = 0
        for item in resources:
            resource_type = self._resource_type(item)
            type_order = self._resource_type_order_map.get(resource_type)
            if type_order is None:
                if filter_unsupported_types:
                    unsupported_type_count += 1
                    continue
                type_order = 999
            coverage = self._resource_target_coverage(item, season, targets)
            if filter_unsupported_types and coverage[0] >= 3:
                uncovered_count += 1
                continue
            sort_key = (
                type_order,
                self._resource_availability_order(item),
                item.get("is_official") is not True,
                *coverage,
                self._resource_unlock_points(item.get("unlock_points")),
                -int(item.get("platform_priority") or 0),
                -self._resource_timestamp(item.get("update_time")),
            )
            prepared.append((sort_key, item))
        prepared.sort(key=lambda pair: pair[0])
        if log_prefix and (unsupported_type_count or uncovered_count):
            details = []
            if unsupported_type_count:
                details.append(f"类型不支持={unsupported_type_count}")
            if uncovered_count:
                details.append(f"明确未覆盖目标集数={uncovered_count}")
            logger.debug(
                f"{log_prefix} 搜索候选预过滤：{len(resources)} -> {len(prepared)}，"
                + "，".join(details)
            )
        return [item for _, item in prepared]

    def _hdhive_update_sort_key(self, resource: Dict[str, Any]) -> tuple:
        """HDHive 最新更新时间优先，其余规则作为稳定的次级顺序。"""
        return (-self._resource_timestamp(resource.get("update_time")),)

    def search_resources(
            self,
            mediainfo: MediaInfo,
            media_type: MediaType,
            season: Optional[int] = None,
            subscribe: Any = None,
    ) -> List[Dict]:
        """
        统一的资源搜索方法，支持电影和电视剧
        按优先级尝试所有启用的搜索源，第一个有结果的就返回
        搜索优先级按已启用来源和用户配置确定

        注意：此方法主要供电影订阅使用。电视剧订阅使用 search_single_source 进行逐源搜索。

        :param mediainfo: 媒体信息
        :param media_type: 媒体类型（MOVIE 或 TV）
        :param season: 季号（电视剧必需）
        :return: 当前同步链可处理的网盘资源列表
        """
        sources = self.get_enabled_sources()
        search_label = self._search_label(mediainfo, media_type, season)
        if not self.source_concurrency_enabled:
            for source_index, source in enumerate(sources):
                results = self.search_single_source(
                    source=source,
                    mediainfo=mediainfo,
                    media_type=media_type,
                    season=season,
                    subscribe=subscribe,
                )
                if results:
                    return results
                remaining = sources[source_index + 1:]
                if remaining:
                    logger.debug(
                        f"[{search_label}][{source.upper()}] 未找到资源，"
                        f"将回退到 "
                        f"{'/'.join(item.capitalize() for item in remaining)} 搜索"
                    )
            return []

        source_results = self.search_sources(
            sources=sources,
            mediainfo=mediainfo,
            media_type=media_type,
            season=season,
            subscribe=subscribe,
        )
        for source in sources:
            results = source_results.get(source) or []
            if results:
                logger.debug(
                    f"[{search_label}][{source.upper()}] 并发搜索完成，"
                    f"按优先级采用 "
                    f"{len(results)} 个候选资源"
                )
                return results

        return []
