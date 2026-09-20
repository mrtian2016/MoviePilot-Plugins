"""平台入口共享的聚合与业务调用。"""

import copy
import datetime
import re
import time
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlparse
from uuid import uuid4

import pytz
from app.core.config import settings
from app.core.metainfo import MetaInfo
from app.db import SessionFactory
from app.db.models.subscribe import Subscribe
from app.db.subscribe_oper import SubscribeOper
from app.log import logger
from app.schemas.types import MediaType
from app.utils.string import StringUtils

from ...core import CloudDriveCapability, OwnerDelegator
from ...core.media import (
    get_subscribe_by_media,
    legacy_media_ids,
    media_identity,
    tmdb_id_of,
)
from ...search.types import (
    PREVIEW_PROVIDER_KEYS,
    resource_type_from_url,
    resource_type_name,
)
from ...utils.cache import create_platform_ttl_cache


class PlatformIntegrationService(OwnerDelegator):
    """统一服务于页面、仪表盘、命令、工作流与智能体。"""

    _OVERVIEW_TTL_SECONDS = 3.0
    _RESOURCE_LINK_PATTERN = re.compile(
        r"ed2k://\|file\|.*?\|/|magnet:\?\S+|https?://\S+",
        re.IGNORECASE,
    )

    def __init__(self, owner):
        super().__init__(owner)
        self._overview_cache = create_platform_ttl_cache(
            "platform:overview",
            owner,
            maxsize=4,
            ttl=int(self._OVERVIEW_TTL_SECONDS),
        )
        self._agent_resource_cache = create_platform_ttl_cache(
            "platform:agent_resources",
            owner,
            maxsize=256,
            ttl=30 * 60,
        )
        self._link_selection_cache = create_platform_ttl_cache(
            "platform:link_selections",
            owner,
            maxsize=256,
            ttl=30 * 60,
        )

    @staticmethod
    def extract_resource_links(value: Any) -> List[str]:
        if isinstance(value, (list, tuple, set)):
            candidates: Iterable[Any] = value
        else:
            candidates = PlatformIntegrationService._RESOURCE_LINK_PATTERN.findall(
                str(value or "")
            )
        links = []
        seen = set()
        for candidate in candidates:
            link = str(candidate or "").strip().rstrip(",，。;；)")
            if link and link not in seen:
                seen.add(link)
                links.append(link)
        return links[:50]

    def _cache_stats(self) -> Dict[str, Any]:
        stats: Dict[str, Any] = {}
        if self._search_handler:
            stats["search"] = self._search_handler.get_cache_stats()
        if self._cloud_drive and self._cloud_drive.supports(
                CloudDriveCapability.CACHE_MAINTENANCE
        ):
            cache_service = self._cloud_drive.require(
                CloudDriveCapability.CACHE_MAINTENANCE
            )
            stats[self._cloud_drive.key] = cache_service.get_cache_stats()
        return stats

    def get_platform_overview(
            self, recent_limit: int = 5, include_runtime: bool = True
    ) -> Dict[str, Any]:
        limit = max(0, min(int(recent_limit or 0), 20))
        cache_key = f"overview:{int(bool(include_runtime))}"
        cached = self._overview_cache.get(cache_key)
        if isinstance(cached, dict):
            result = copy.deepcopy(cached)
            result["recent_history"] = list(cached.get("recent_history") or [])[:limit]
            return result

        today = datetime.datetime.now(pytz.timezone(settings.TZ)).strftime("%Y-%m-%d")
        database_overview = self._get_data_store().history_overview(
            today, recent_limit=20
        )
        history_count = int(database_overview.get("total") or 0)
        transferred_today = int(database_overview.get("today") or 0)
        success = int(database_overview.get("success") or 0)
        failed = int(database_overview.get("failed") or 0)
        recent_history = list(database_overview.get("recent") or [])
        provider = self._cloud_drive
        overview = {
            "stats": [
                {"title": "总转存", "value": history_count, "color": "primary", "icon": "mdi-cloud-upload-outline"},
                {"title": "今日转存", "value": transferred_today, "color": "info", "icon": "mdi-calendar-today"},
                {"title": "成功", "value": success, "color": "success", "icon": "mdi-check-circle-outline"},
                {"title": "失败", "value": failed, "color": "error", "icon": "mdi-alert-circle-outline"},
            ],
            "history_count": history_count,
            "recent_history": recent_history,
            "cache": self._cache_stats(),
            "provider": {
                "key": provider.key if provider else "",
                "name": provider.name if provider else "未配置",
                "capabilities": sorted(
                    capability.value for capability in provider.capabilities
                ) if provider else [],
            },
        }
        if include_runtime:
            overview["runtime"] = self._runtime_snapshot()
        self._overview_cache[cache_key] = copy.deepcopy(overview)
        result = copy.deepcopy(overview)
        result["recent_history"] = recent_history[:limit]
        return result

    def clear_platform_cache(self) -> Dict[str, int]:
        """清理平台聚合与智能体候选缓存。"""
        counts = {
            "platform_overview": len(list(self._overview_cache.items())),
            "agent_resources": len(list(self._agent_resource_cache.items())),
        }
        self._overview_cache.clear()
        self._agent_resource_cache.clear()
        return counts

    def close(self) -> None:
        pass

    @staticmethod
    def _agent_cache_key(session_id: str, search_id: str) -> str:
        return f"{str(session_id or 'unknown')}:{str(search_id or '').strip()}"

    @staticmethod
    def _normalize_agent_title(value: Any) -> str:
        return re.sub(r"[\W_]+", "", str(value or "").casefold())

    @staticmethod
    def _agent_media_type(value: Any) -> Optional[MediaType]:
        normalized = str(getattr(value, "value", value) or "").strip().lower()
        if normalized in {"movie", MediaType.MOVIE.value.lower()}:
            return MediaType.MOVIE
        if normalized in {"tv", MediaType.TV.value.lower()}:
            return MediaType.TV
        return None

    @staticmethod
    def _subscribe_identity(subscribe: Any) -> Tuple[Any, ...]:
        source = str(getattr(subscribe, "media_source", "") or "").strip()
        media_id = str(getattr(subscribe, "media_id", "") or "").strip()
        if source and media_id:
            return "source", source, media_id
        for field in ("tmdbid", "doubanid", "bangumiid", "anilistid"):
            value = getattr(subscribe, field, None)
            if value not in (None, ""):
                return field, str(value)
        return (
            "title",
            PlatformIntegrationService._normalize_agent_title(
                getattr(subscribe, "name", "")
            ),
            str(getattr(subscribe, "year", "") or ""),
        )

    def _match_agent_subscribe(
            self,
            subscribe_id: Optional[int],
            title: str,
            media_type: Optional[MediaType],
            season: Optional[int],
            latest_season: bool,
    ) -> Tuple[Any, Optional[str]]:
        oper = SubscribeOper()
        subscribe = oper.get(int(subscribe_id)) if subscribe_id else None
        if subscribe_id and not subscribe:
            return None, f"未找到订阅 ID {subscribe_id}"

        normalized_title = self._normalize_agent_title(title)
        if subscribe and normalized_title and normalized_title != self._normalize_agent_title(
                getattr(subscribe, "name", "")
        ):
            return None, "媒体名称与指定订阅不一致"
        if subscribe and media_type and self._agent_media_type(
                getattr(subscribe, "type", None)
        ) != media_type:
            return None, "媒体类型与指定订阅不一致"
        if subscribe and season is not None:
            if int(getattr(subscribe, "season", 0) or 0) == int(season):
                return subscribe, None
            return None, f"指定订阅不对应第 {season} 季"
        if subscribe:
            return subscribe, None

        subscribes = []
        if normalized_title:
            subscribes = [
                item for item in (oper.list() or [])
                if self._normalize_agent_title(getattr(item, "name", ""))
                   == normalized_title
                   and (
                           not media_type
                           or self._agent_media_type(getattr(item, "type", None)) == media_type
                   )
            ]
            identities = {self._subscribe_identity(item) for item in subscribes}
            if len(identities) > 1:
                subscribes = []

        if not subscribes:
            return subscribe, None
        if season is not None:
            matched = [
                item for item in subscribes
                if int(getattr(item, "season", 0) or 0) == int(season)
            ]
            if matched:
                return matched[0], None
            return None, None
        return subscribes[0], None

    @staticmethod
    def _latest_media_season(mediainfo: Any) -> int:
        seasons = []
        for value in (getattr(mediainfo, "seasons", None) or {}).keys():
            try:
                season = int(value)
            except (TypeError, ValueError):
                continue
            if season > 0:
                seasons.append(season)
        if seasons:
            return max(seasons)
        return max(1, int(getattr(mediainfo, "number_of_seasons", 0) or 1))

    def _match_recognized_subscribe(
            self,
            mediainfo: Any,
            media_type: MediaType,
            season: Optional[int],
    ) -> Any:
        return get_subscribe_by_media(
            SubscribeOper(),
            media_type=media_type.value,
            season=season if media_type == MediaType.TV else None,
            media=mediainfo,
        )

    def _recognize_subscribe_media(self, subscribe: Any):
        media_type = (
            MediaType.MOVIE
            if str(getattr(subscribe, "type", "")) == MediaType.MOVIE.value
            else MediaType.TV
        )
        season = int(getattr(subscribe, "season", 0) or 1) if media_type == MediaType.TV else None
        meta = MetaInfo(str(getattr(subscribe, "name", "") or ""))
        meta.year = getattr(subscribe, "year", None)
        meta.type = media_type
        source, media_id = media_identity(subscribe)
        legacy_ids = legacy_media_ids(subscribe)
        mediainfo = self._sync_handler._recognize_media_once(
            (
                "agent",
                media_type.value,
                source,
                media_id,
                getattr(subscribe, "name", None),
                getattr(subscribe, "year", None),
                season or 0,
            ),
            meta=meta,
            mtype=media_type,
            media_source=source,
            media_id=media_id,
            **legacy_ids,
            cache=True,
        )
        return mediainfo, media_type, season

    def _recognize_agent_media(
            self,
            subscribe: Any,
            title: str,
            media_type: Optional[MediaType],
            season: Optional[int],
            latest_season: bool,
    ) -> Tuple[Any, Optional[MediaType], Optional[int]]:
        if subscribe:
            mediainfo, resolved_type, resolved_season = (
                self._recognize_subscribe_media(subscribe)
            )
            if mediainfo and resolved_type == MediaType.TV:
                if latest_season:
                    resolved_season = self._latest_media_season(mediainfo)
                elif season is not None:
                    resolved_season = season
            return mediainfo, resolved_type, resolved_season

        meta = MetaInfo(title)
        if media_type:
            meta.type = media_type
        if season is not None:
            meta.begin_season = season
        mediainfo = self._sync_handler._recognize_media_once(
            ("agent", media_type.value if media_type else "", title, season or 0),
            meta=meta,
            mtype=media_type,
            cache=True,
        )
        if not mediainfo:
            return None, media_type, season
        resolved_type = self._agent_media_type(getattr(mediainfo, "type", None))
        if not resolved_type:
            return mediainfo, None, season
        if media_type and resolved_type != media_type:
            return None, resolved_type, season
        if resolved_type == MediaType.MOVIE:
            return mediainfo, resolved_type, None
        resolved_season = season
        if latest_season:
            resolved_season = self._latest_media_season(mediainfo)
        if resolved_season is None:
            resolved_season = int(
                getattr(meta, "begin_season", 0)
                or getattr(mediainfo, "season", 0)
                or 1
            )
        return mediainfo, resolved_type, resolved_season

    @staticmethod
    def _candidate_reason(resource: Dict[str, Any], index: int) -> List[str]:
        reasons = []
        if index == 0:
            reasons.append("当前平台规则排序第一")
        source_priority = int(resource.get("source_priority") or 0)
        if source_priority:
            reasons.append(f"搜索源优先级第 {source_priority} 位")
        if resource.get("is_official"):
            reasons.append("官方或官组资源")
        priority = int(resource.get("platform_priority") or 0)
        if priority:
            reasons.append(f"规则优先级 {priority}")
        if not resource.get("need_unlock"):
            reasons.append("无需积分解锁")
        elif int(resource.get("unlock_points") or 0) > 0:
            reasons.append(f"需要 {int(resource.get('unlock_points') or 0)} 积分解锁")
        definition = str(
            resource.get("resolution") or resource.get("quality") or ""
        ).strip()
        if definition:
            reasons.append(f"清晰度 {definition}")
        if resource.get("update_time"):
            reasons.append(f"更新时间 {resource.get('update_time')}")
        return reasons or ["已通过订阅规则筛选"]

    def search_platform_resources(
            self,
            session_id: str,
            subscribe_id: Optional[int] = None,
            title: str = "",
            media_type: str = "",
            season: Optional[int] = None,
            latest_season: bool = False,
            limit: int = 20,
    ) -> Dict[str, Any]:
        """按订阅或媒体名称搜索候选，并保存完整结果供智能体分步选择。"""
        started = time.monotonic()
        title = str(title or "").strip()
        suffix = re.search(r"\s*最新(?:一)?季\s*$", title)
        if suffix:
            latest_season = True
            title = title[:suffix.start()].strip()
        if not subscribe_id and not title:
            return {"success": False, "message": "请提供媒体名称或订阅 ID"}
        if season is not None and latest_season:
            return {"success": False, "message": "指定季号与最新季不能同时使用"}
        requested_type = self._agent_media_type(media_type)
        if media_type and not requested_type:
            return {
                "success": False,
                "message": "媒体类型仅支持 movie（电影）或 tv（电视剧）",
            }
        if not requested_type and (season is not None or latest_season):
            requested_type = MediaType.TV
        try:
            if not self._search_handler:
                return {"success": False, "message": "搜索服务尚未就绪"}
            subscribe, match_error = self._match_agent_subscribe(
                subscribe_id=subscribe_id,
                title=title,
                media_type=requested_type,
                season=season,
                latest_season=latest_season,
            )
            if match_error:
                return {"success": False, "message": match_error}
            if subscribe and not title:
                title = str(getattr(subscribe, "name", "") or "").strip()
            mediainfo, resolved_type, resolved_season = self._recognize_agent_media(
                subscribe=subscribe,
                title=title,
                media_type=requested_type,
                season=season,
                latest_season=latest_season,
            )
            if not mediainfo:
                return {"success": False, "message": f"无法识别媒体：{title}"}
            if resolved_type not in {MediaType.MOVIE, MediaType.TV}:
                return {"success": False, "message": "仅支持电影或电视剧资源搜索"}
            if resolved_type == MediaType.MOVIE and (season or latest_season):
                return {"success": False, "message": "电影不支持季号或最新季参数"}
            subscribe_type = self._agent_media_type(
                getattr(subscribe, "type", None)
            ) if subscribe else None
            subscribe_season = int(
                getattr(subscribe, "season", 0) or 0
            ) if subscribe_type == MediaType.TV else None
            if (
                    subscribe_type != resolved_type
                    or subscribe_season != resolved_season
            ):
                subscribe = self._match_recognized_subscribe(
                    mediainfo,
                    resolved_type,
                    resolved_season,
                )
            sources = self._search_handler.get_enabled_sources()
            if not sources:
                return {"success": False, "message": "没有可用的搜索源"}
            source_results = self._search_handler.search_sources(
                sources=sources,
                mediainfo=mediainfo,
                media_type=resolved_type,
                season=resolved_season,
                subscribe=subscribe,
            )
            resources = []
            for source_priority, source in enumerate(sources, start=1):
                for source_position, resource in enumerate(
                        source_results.get(source) or [], start=1
                ):
                    item = dict(resource)
                    item.setdefault("source", source)
                    item["source_priority"] = source_priority
                    item["source_position"] = source_position
                    resources.append(item)
            resources = resources[: max(1, min(int(limit or 20), 50))]
        except Exception as error:
            logger.error(f"智能体搜索网盘资源失败：{error}")
            return {"success": False, "message": "搜索资源失败，请检查媒体信息和搜索源配置"}

        candidates = []
        cached_resources = {}
        bound_subscribe_id = int(getattr(subscribe, "id", 0) or 0) or None
        available_count = 0
        transferable_count = 0
        unlock_count = 0
        official_count = 0
        free_count = 0
        total_size = 0
        latest_update_time = ""
        source_counts: Counter = Counter()
        resource_type_counts: Counter = Counter()
        for index, resource in enumerate(resources):
            candidate_id = f"r{index + 1:03d}"
            item = dict(resource)
            item["candidate_id"] = candidate_id
            cached_resources[candidate_id] = item
            available = bool(
                str(item.get("url") or "").strip()
                or (
                        item.get("need_unlock")
                        and item.get("resource_ref")
                )
            )
            candidate = {
                "candidate_id": candidate_id,
                "title": str(item.get("title") or "未知资源"),
                "source": str(item.get("source") or "unknown"),
                "rank": index + 1,
                "source_priority": int(item.get("source_priority") or 0),
                "source_position": int(item.get("source_position") or 0),
                "resource_type": str(
                    item.get("resource_type") or item.get("pan_type") or "unknown"
                ).lower(),
                "size": item.get("size") or 0,
                "resolution": item.get("resolution") or "",
                "quality": item.get("quality") or "",
                "update_time": item.get("update_time") or "",
                "platform_priority": int(item.get("platform_priority") or 0),
                "is_official": bool(item.get("is_official")),
                "need_unlock": bool(item.get("need_unlock")),
                "unlock_points": int(item.get("unlock_points") or 0),
                "available": available,
                "transferable": bool(bound_subscribe_id and available),
                "recommendation_reasons": self._candidate_reason(item, index),
            }
            candidates.append(candidate)
            available_count += available
            transferable_count += candidate["transferable"]
            unlock_count += candidate["need_unlock"]
            official_count += candidate["is_official"]
            free_count += not candidate["need_unlock"]
            total_size += max(
                0, int(StringUtils.num_filesize(candidate["size"]) or 0)
            )
            latest_update_time = max(
                latest_update_time, str(candidate.get("update_time") or "")
            )
            source_counts[candidate["source"]] += 1
            resource_type_counts[candidate["resource_type"]] += 1

        search_id = uuid4().hex[:12]
        media = {
            "subscribe_id": bound_subscribe_id,
            "title": str(getattr(mediainfo, "title", "") or title),
            "year": getattr(mediainfo, "year", None),
            "type": resolved_type.value,
            "season": resolved_season,
        }
        cache_key = self._agent_cache_key(session_id, search_id)
        self._agent_resource_cache[cache_key] = {
            "subscribe_id": bound_subscribe_id,
            "media": media,
            "resources": cached_resources,
        }
        summary = {
            "total": len(candidates),
            "available": available_count,
            "transferable": transferable_count,
            "need_unlock": unlock_count,
            "official": official_count,
            "free": free_count,
            "total_size_bytes": total_size,
            "average_size_bytes": total_size // len(candidates) if candidates else 0,
            "latest_update_time": latest_update_time,
            "by_source": dict(source_counts),
            "by_resource_type": dict(resource_type_counts),
        }
        logger.info(
            f"智能体资源搜索完成：{media['title']}"
            f"{f' S{resolved_season:02d}' if resolved_season else ''}，"
            f"订阅 {bound_subscribe_id or '未绑定'}，候选 {len(candidates)} 个，"
            f"耗时 {int((time.monotonic() - started) * 1000)} ms"
        )
        return {
            "success": True,
            "message": "资源搜索完成",
            "search_id": search_id,
            "media": media,
            "summary": summary,
            "source_priority_order": sources,
            "sort_rule": (
                "先按配置的搜索源优先级，再按资源类型优先级、MoviePilot规则优先级、"
                "官组、可直接访问状态、更新时间和解锁积分保持稳定顺序"
            ),
            "recommended_candidate_ids": [
                item["candidate_id"] for item in available[:3]
            ],
            "candidates": candidates,
            "next_step": (
                    "请使用中文汇总候选，并结合规则优先级、官组、清晰度、"
                    "资源大小、更新时间和解锁成本说明推荐理由。"
                    + (
                        "用户需要手动选择时，优先调用 ask_user_choice 展示候选 ID；"
                        "收到选择后使用 search_id 调用 pansearch_select_resources。"
                        if bound_subscribe_id else
                        "本次搜索未绑定现有订阅，只能展示和推荐；需要转存时先创建或选择订阅，"
                        "再使用订阅 ID 重新搜索。"
                    )
                    + "不要自行构造或改写资源链接。"
            ),
        }

    def select_platform_resources(
            self,
            session_id: str,
            search_id: str,
            candidate_ids: List[str],
    ) -> Dict[str, Any]:
        """只从当前会话缓存取回已搜索链接并提交，禁止模型直接拼接链接。"""
        cache_key = self._agent_cache_key(session_id, search_id)
        cached = self._agent_resource_cache.get(cache_key)
        if not isinstance(cached, dict):
            return {"success": False, "message": "候选资源已过期，请重新搜索"}
        subscribe_id = int(cached.get("subscribe_id") or 0)
        if subscribe_id <= 0:
            return {
                "success": False,
                "message": "本次搜索未关联现有订阅，请先创建或选择订阅后重新搜索",
            }
        resources = cached.get("resources") or {}
        selected = []
        invalid = []
        for candidate_id in dict.fromkeys(str(value).strip() for value in candidate_ids):
            resource = resources.get(candidate_id)
            usable = bool(
                str((resource or {}).get("url") or "").strip()
                or (
                        (resource or {}).get("need_unlock")
                        and (resource or {}).get("resource_ref")
                )
            )
            if not usable:
                invalid.append(candidate_id)
                continue
            selected.append(dict(resource))
        if invalid:
            return {
                "success": False,
                "message": f"候选不可用或不可直接提交：{', '.join(invalid)}",
            }
        if not selected:
            return {"success": False, "message": "没有选择可提交的候选资源"}
        result = dict(self.start_selected_resources(int(subscribe_id), selected))
        data = dict(result.get("data") or {})
        data["candidate_ids"] = list(dict.fromkeys(candidate_ids))
        result["data"] = data
        return result

    def get_runtime_performance(self, include_tasks: bool = True) -> Dict[str, Any]:
        """汇总当前任务、搜索缓存和同步阶段性能指标。"""
        now = time.time()
        tasks = self._serialize_runtime_tasks() if include_tasks else []
        search_metrics = (
            self._search_handler.get_search_metrics()
            if self._search_handler else {}
        )
        sync_metrics = (
            self._sync_handler.get_sync_metrics()
            if self._sync_handler else {}
        )
        queue = {"pending": 0, "active": 0}
        if self._subscribe_search_queue_lock is not None:
            with self._subscribe_search_queue_lock:
                queue = {
                    "pending": len(self._subscribe_search_pending),
                    "active": len(self._subscribe_search_active),
                }
        external_calls = 0
        external_elapsed_ms = 0
        positive_hits = 0
        negative_hits = 0
        for metric in search_metrics.values():
            external_calls += int(metric.get("external_calls") or 0)
            external_elapsed_ms += int(
                metric.get("external_elapsed_ms") or 0
            )
            positive_hits += int(metric.get("positive_cache_hits") or 0)
            negative_hits += int(metric.get("negative_cache_hits") or 0)
        run_elapsed_ms = (
            int(max(0.0, now - self._sync_run_started_at) * 1000)
            if self._sync_running and self._sync_run_started_at else 0
        )
        return {
            "success": True,
            "message": "运行性能数据已汇总",
            "runtime": {
                "status": self._sync_status,
                "task": self._sync_task_text,
                "progress": self._sync_progress,
                "running": self._sync_running,
                "elapsed_ms": run_elapsed_ms,
                "last_elapsed_ms": int(self._sync_last_elapsed_ms or 0),
                "last_finished_at": float(self._sync_last_finished_at or 0),
                "transferred": int(self._sync_context.get("transferred") or 0),
                "configured_concurrency": int(self._subscription_concurrency or 1),
            },
            "queue": queue,
            "tasks": tasks,
            "search": {
                "summary": {
                    "external_calls": external_calls,
                    "external_elapsed_ms": external_elapsed_ms,
                    "positive_cache_hits": positive_hits,
                    "negative_cache_hits": negative_hits,
                },
                "sources": search_metrics,
                "cache": self._search_handler.get_cache_stats()
                if self._search_handler else {},
            },
            "sync_stages": sync_metrics,
            "next_step": (
                "请用中文说明当前任务是否正常推进、最耗时的搜索源或同步阶段、"
                "缓存命中效果，以及是否需要调整并发或缓存配置。"
            ),
        }

    def api_platform_overview(self, include_runtime: bool = True) -> Dict[str, Any]:
        return {
            "success": True,
            "data": self.get_platform_overview(
                6, include_runtime=bool(include_runtime)
            ),
        }

    def start_platform_sync(self) -> Dict[str, Any]:
        return self.api_vue_start_sync()

    def api_vue_resolve_manual_links(self, payload: Dict[str, Any]) -> dict:
        """只读识别手动资源，统一返回订阅、TMDB 候选与资源季。"""
        payload = dict(payload or {})
        links = self.extract_resource_links(payload.get("resource_links"))
        cloud_path = str(payload.get("cloud_path") or "").strip()
        if not links and not cloud_path:
            return {"success": False, "message": "请提供有效资源链接或网盘路径"}
        title = str(payload.get("title") or "").strip()
        requested_type = str(payload.get("media_type") or "").strip().lower()
        try:
            requested_tmdb_id = int(payload.get("tmdb_id") or 0)
        except (TypeError, ValueError):
            return {"success": False, "message": "TMDB ID 格式错误"}
        preview = self._preview_link_media(links) if links else {}
        recognized_title = (
                title
                or str(preview.get("title") or "").strip()
                or self._link_media_title(links)
        )
        if not recognized_title:
            return {"success": False, "message": "未能从分享内容识别媒体名称"}
        media_type = requested_type or str(preview.get("media_type") or "").strip().lower()
        if media_type and media_type not in {"movie", "tv"}:
            return {"success": False, "message": "媒体类型仅支持 movie 或 tv"}
        season_values = set(self._link_title_seasons(recognized_title))
        season_values.update({
            int(value) for value in preview.get("seasons") or []
            if int(value) > 0
        })
        for link in links:
            season_values.update(self._link_title_seasons(self._link_media_title([link])))
        seasons = sorted(season_values)
        matched = (
            self._find_link_subscribe(
                recognized_title,
                media_type,
                seasons[0] if len(seasons) == 1 else None,
                requested_tmdb_id or None,
            )
            if len(seasons) <= 1 else None
        )
        candidates = []
        if not matched:
            search_result = self.api_vue_search_tmdb_candidates({
                "title": recognized_title,
                "tmdb_id": requested_tmdb_id or None,
                "media_type": media_type or None,
            })
            if not search_result.get("success"):
                return search_result
            candidates = list((search_result.get("data") or {}).get("items") or [])
            if media_type:
                candidates = [item for item in candidates if item.get("media_type") == media_type]
        resolved_media = (
            self._link_subscribe_media(matched)
            if matched else candidates[0] if len(candidates) == 1 else None
        )
        available_seasons = []
        if resolved_media and resolved_media.get("media_type") == "tv":
            detail_result = self.api_vue_search_tmdb_candidates({
                "title": resolved_media.get("title") or recognized_title,
                "original_title": resolved_media.get("original_title") or "",
                "year": resolved_media.get("year"),
                "tmdb_id": resolved_media.get("tmdb_id"),
                "media_type": "tv",
            })
            detail_data = detail_result.get("data") or {}
            available_seasons = list(detail_data.get("seasons") or [])
            if not matched and detail_data.get("items"):
                candidates = list(detail_data["items"])
        selected_seasons = (
            [value for value in seasons if value in set(available_seasons)]
            if available_seasons else seasons
        )
        return {
            "success": True,
            "message": "已定位订阅" if matched else "已定位 TMDB 候选",
            "data": {
                "title": recognized_title,
                "media_type": media_type,
                "seasons": selected_seasons,
                "detected_seasons": seasons,
                "available_seasons": available_seasons,
                "subscribe_id": int(getattr(matched, "id", 0) or 0) if matched else None,
                "candidates": candidates,
            },
        }

    def submit_platform_links(
            self,
            subscribe_id: Optional[int] = None,
            resource_links: Any = None,
            wait: bool = False,
            title: str = "",
            media_type: str = "",
            season: Optional[int] = None,
            seasons: Optional[List[int]] = None,
            selection_id: str = "",
            tmdb_id: Optional[int] = None,
            selection_scope: str = "",
            detect_multiple: bool = True,
    ) -> Dict[str, Any]:
        """提交链接；无有效订阅时先完成 TMDB 快速识别与选择。"""
        try:
            normalized_seasons = sorted({
                int(value) for value in (seasons or [])
                if int(value) > 0
            })
            if season is not None and not normalized_seasons:
                normalized_seasons = [int(season)]
        except (TypeError, ValueError):
            return {"success": False, "message": "季数格式错误"}
        season = normalized_seasons[0] if normalized_seasons else season
        try:
            normalized_subscribe_id = int(subscribe_id or 0)
        except (TypeError, ValueError):
            normalized_subscribe_id = 0
        if normalized_subscribe_id > 0:
            subscribe = SubscribeOper().get(normalized_subscribe_id)
            if subscribe:
                return self.api_vue_start_manual_sync({
                    "subscribe_id": normalized_subscribe_id,
                    "resource_links": self.extract_resource_links(resource_links),
                }, wait=wait)
            if not str(title or "").strip():
                title = str(normalized_subscribe_id)

        normalized_selection_id = str(selection_id or "").strip()
        requested_selection_type = str(media_type or "").strip().lower()
        selected_candidate = None
        if normalized_selection_id:
            cached = self._link_selection_cache.get(normalized_selection_id)
            if not isinstance(cached, dict):
                return {"success": False, "message": "TMDB 候选已过期，请重新提交链接识别"}
            cached_scope = str(cached.get("scope") or "")
            if cached_scope and cached_scope != str(selection_scope or ""):
                return {"success": False, "message": "TMDB 候选不属于当前会话"}
            try:
                selected_tmdb_id = int(tmdb_id or 0)
            except (TypeError, ValueError):
                selected_tmdb_id = 0
            if selected_tmdb_id <= 0:
                return {"success": False, "message": "请选择有效的 TMDB ID"}
            matched_candidates = [
                item for item in cached.get("candidates", [])
                if int(item.get("tmdb_id") or 0) == selected_tmdb_id
                   and (
                           not requested_selection_type
                           or item.get("media_type") == requested_selection_type
                   )
            ]
            if len(matched_candidates) > 1:
                return {"success": False, "message": "TMDB ID 同时匹配电影和电视剧，请指定媒体类型"}
            selected_candidate = matched_candidates[0] if matched_candidates else None
            if selected_candidate is None:
                return {"success": False, "message": "所选 TMDB ID 不在待选候选中"}
            links = list(cached.get("links") or [])
            title = str(cached.get("title") or "")
            media_type = str(cached.get("media_type") or "")
            season = cached.get("season")
            normalized_seasons = [
                int(value) for value in cached.get("seasons") or []
            ]
            if season is not None and not normalized_seasons:
                normalized_seasons = [int(season)]
        else:
            links = self.extract_resource_links(resource_links)
        if not links:
            return {"success": False, "message": "请至少提供一个有效资源链接"}

        # 一个分享可能包含多个媒体。无显式标题时按文件名分组，分别走现有
        # TMDB/订阅匹配和整理流程，避免把整包错误归到出现次数最多的媒体。
        detected = (
            self._preview_link_media_candidates(links)
            if selected_candidate is None and detect_multiple else []
        )
        title_key = self._normalize_agent_title(title)
        detected_keys = {
            self._normalize_agent_title(item.get("title")) for item in detected
        }
        if (
                selected_candidate is None
                and len(detected) > 1
                and (not title_key or title_key not in detected_keys)
        ):
            results = []
            for item in detected:
                item_result = self.submit_platform_links(
                    subscribe_id=subscribe_id,
                    resource_links=links,
                    wait=wait,
                    title=str(item.get("title") or "").strip(),
                    media_type=str(item.get("media_type") or "").strip(),
                    seasons=list(item.get("seasons") or []),
                    selection_scope=selection_scope,
                    detect_multiple=False,
                )
                results.append(item_result)
            success = all(bool(item.get("success")) for item in results)
            messages = [str(item.get("message") or "") for item in results]
            return {
                "success": success,
                "message": "；".join(value for value in messages if value)
                           or ("多媒体资源已提交" if success else "部分媒体提交失败"),
                "data": {
                    "media": [
                        dict((item.get("data") or {}).get("media") or {})
                        for item in results
                    ],
                    "results": results,
                },
            }

        if selected_candidate is None:
            preview = self._preview_link_media(links) if not str(title or "").strip() else {}
            recognized_title = str(title or "").strip() or str(preview.get("title") or "").strip()
            if not str(title or "").strip() and recognized_title:
                if preview.get("year") and str(preview["year"]) not in recognized_title:
                    recognized_title = f"{recognized_title} ({preview['year']})"
                if not media_type:
                    media_type = str(preview.get("media_type") or "")
                if season is None:
                    season = preview.get("season")
            else:
                recognized_title = recognized_title or self._link_media_title(links)

            if not recognized_title:
                return {
                    "success": False,
                    "message": "未能从分享内容识别媒体名称，请在链接前补充名称后重试",
                }
            title = recognized_title
            normalized_type = str(media_type or "").strip().lower()
            if normalized_type and normalized_type not in {"movie", "tv"}:
                return {"success": False, "message": "媒体类型仅支持 movie 或 tv"}
            matched_subscribe = (
                self._find_link_subscribe(
                    recognized_title,
                    normalized_type,
                    season,
                )
                if len(normalized_seasons) <= 1 else None
            )
            if matched_subscribe:
                result = self.api_vue_start_manual_sync({
                    "subscribe_id": int(getattr(matched_subscribe, "id", 0) or 0),
                    "resource_links": links,
                }, wait=wait)
                data = dict(result.get("data") or {})
                data["matched_subscribe_id"] = int(
                    getattr(matched_subscribe, "id", 0) or 0
                )
                data["media"] = self._link_subscribe_media(matched_subscribe)
                result["data"] = data
                logger.info(
                    f"分享内容已定位订阅：标题={recognized_title}，"
                    f"订阅={getattr(matched_subscribe, 'name', '')}，"
                    f"季={getattr(matched_subscribe, 'season', '') or '电影'}"
                )
                return result
            search_result = self.api_vue_search_tmdb_candidates({"title": recognized_title})
            if not search_result.get("success"):
                return search_result
            candidates = list((search_result.get("data") or {}).get("items") or [])
            if normalized_type:
                candidates = [
                    item for item in candidates
                    if item.get("media_type") == normalized_type
                ]
            if not candidates:
                return {"success": False, "message": f"未识别到“{recognized_title}”的 TMDB 媒体"}
            logger.info(
                f"分享内容 TMDB 匹配：标题={recognized_title}，"
                f"类型={normalized_type or '不限'}，候选={len(candidates)}"
            )
            try:
                selected_tmdb_id = int(tmdb_id or 0)
            except (TypeError, ValueError):
                selected_tmdb_id = 0
            if selected_tmdb_id:
                selected_candidate = next(
                    (
                        item for item in candidates
                        if int(item.get("tmdb_id") or 0) == selected_tmdb_id
                    ),
                    None,
                )
                if not selected_candidate:
                    return {"success": False, "message": "指定 TMDB ID 与识别结果不匹配"}
            elif len(candidates) == 1:
                selected_candidate = candidates[0]
            else:
                next_selection_id = uuid4().hex[:12]
                self._link_selection_cache[next_selection_id] = {
                    "scope": str(selection_scope or ""),
                    "links": links,
                    "title": recognized_title,
                    "media_type": normalized_type,
                    "season": season,
                    "seasons": normalized_seasons,
                    "candidates": candidates,
                }
                return {
                    "success": False,
                    "message": f"“{recognized_title}”匹配到多个 TMDB 媒体，请先选择",
                    "data": {
                        "selection_required": True,
                        "selection_id": next_selection_id,
                        "candidates": candidates,
                        "next_step": "请选择候选的 media_type 与 tmdb_id 后继续提交",
                    },
                }

        matched_subscribe = (
            self._find_link_subscribe(
                str(selected_candidate.get("title") or title),
                str(selected_candidate.get("media_type") or media_type),
                season,
                int(selected_candidate.get("tmdb_id") or 0),
            )
            if not normalized_subscribe_id and len(normalized_seasons) <= 1
            else None
        )
        if matched_subscribe:
            result = self.api_vue_start_manual_sync({
                "subscribe_id": int(getattr(matched_subscribe, "id", 0) or 0),
                "resource_links": links,
            }, wait=wait)
            data = dict(result.get("data") or {})
            data["matched_subscribe_id"] = int(
                getattr(matched_subscribe, "id", 0) or 0
            )
            data["media"] = self._link_subscribe_media(matched_subscribe)
            result["data"] = data
            return result
        media = self._link_media_payload(
            selected_candidate,
            source_title=title,
            season=season,
            seasons=normalized_seasons,
        )
        result = self.api_vue_start_manual_sync({
            "resource_links": links,
            "media": media,
        }, wait=wait)
        logger.info(
            f"分享资源转存提交：媒体={media.get('title') or title}，"
            f"类型={media.get('media_type') or '未知'}，链接数={len(links)}，"
            f"成功={bool(result.get('success'))}"
        )
        if result.get("success") and normalized_selection_id:
            try:
                del self._link_selection_cache[normalized_selection_id]
            except KeyError:
                pass
        data = dict(result.get("data") or {})
        data.setdefault("media", media)
        result["data"] = data
        return result

    @staticmethod
    def _link_media_title(links: List[str]) -> str:
        """从 ED2K 文件名或 Magnet dn 中提取可供平台识别的标题。"""
        for link in links:
            lowered = str(link or "").lower()
            if lowered.startswith("ed2k://|file|"):
                parts = str(link).split("|")
                if len(parts) > 2:
                    return unquote(parts[2]).strip()
            if lowered.startswith("magnet:?"):
                values = parse_qs(urlparse(str(link)).query).get("dn") or []
                if values and str(values[0]).strip():
                    return unquote(str(values[0])).strip()
        return ""

    @staticmethod
    def _link_title_seasons(value: Any) -> List[int]:
        """提取资源标题中的明确季范围，供 TMDB 季列表自动预选。"""
        seasons = set()
        pattern = re.compile(
            r"(?<![A-Za-z0-9])(?:S(?:eason)?\s*|第\s*)0*(\d{1,3})"
            r"(?:\s*(?:[-~～至到])\s*(?:S(?:eason)?\s*|第\s*)?0*(\d{1,3}))?"
            r"(?:\s*季)?(?=$|[\s._/\\\-\[\]()Eｅ集])",
            re.IGNORECASE,
        )
        for match in pattern.finditer(str(value or "")):
            start = int(match.group(1))
            end = int(match.group(2) or start)
            if 0 < start <= end <= 999 and end - start <= 100:
                seasons.update(range(start, end + 1))
        return sorted(seasons)

    def _find_link_subscribe(
            self,
            title: str,
            media_type: str = "",
            season: Optional[int] = None,
            tmdb_id: Optional[int] = None,
    ) -> Any:
        """按分享识别结果只复用唯一明确的订阅，避免多季误配。"""
        normalized_title = self._normalize_agent_title(title)
        if not normalized_title and not tmdb_id:
            return None
        normalized_type = str(media_type or "").strip().lower()
        candidates = []
        for subscribe in SubscribeOper().list() or []:
            subscribe_type = self._agent_media_type(getattr(subscribe, "type", None))
            if normalized_type and self._agent_media_type(normalized_type) != subscribe_type:
                continue
            if tmdb_id:
                try:
                    if tmdb_id_of(subscribe) != int(tmdb_id):
                        continue
                except (TypeError, ValueError):
                    continue
            elif self._normalize_agent_title(getattr(subscribe, "name", "")) != normalized_title:
                continue
            if season is not None and subscribe_type == MediaType.TV:
                if int(getattr(subscribe, "season", 0) or 0) != int(season):
                    continue
            candidates.append(subscribe)
        return candidates[0] if len(candidates) == 1 else None

    @staticmethod
    def _link_subscribe_media(subscribe: Any) -> Dict[str, Any]:
        media_type = (
            "movie"
            if str(getattr(subscribe, "type", "")) == MediaType.MOVIE.value
            else "tv"
        )
        media = {
            "tmdb_id": tmdb_id_of(subscribe) or 0,
            "media_type": media_type,
            "title": str(getattr(subscribe, "name", "") or "").strip(),
            "year": getattr(subscribe, "year", None),
        }
        if media_type == "tv":
            media["seasons"] = [int(getattr(subscribe, "season", 1) or 1)]
        return media

    def _preview_link_media(self, links: List[str]) -> Dict[str, Any]:
        """从已配置网盘的分享文件名推断媒体名称和季集范围。"""
        candidates = self._preview_link_media_candidates(links)
        return candidates[0] if candidates else {}

    def _preview_link_media_candidates(self, links: List[str]) -> List[Dict[str, Any]]:
        """从分享文件名识别全部媒体，按文件出现次数降序返回。"""
        if not self._sync_handler:
            return []
        for link in links:
            if resource_type_from_url(link) not in PREVIEW_PROVIDER_KEYS:
                continue
            try:
                files = self._sync_handler.preview_resource_files(link)
            except Exception as error:
                logger.warning(f"分享内容快速识别失败：{error}")
                continue
            resource_type = resource_type_from_url(link)
            logger.info(
                f"分享内容预览：类型={resource_type_name(resource_type, '未知')}，"
                f"文件数={len(files)}"
            )
            inferred = self._infer_link_media_candidates(files)
            if inferred:
                for item in inferred:
                    logger.info(
                        f"分享内容识别完成：标题={item.get('title')}，"
                        f"类型={item.get('media_type') or '待 TMDB 判断'}，"
                        f"季={','.join(str(value) for value in item.get('seasons') or []) or '未指定'}"
                    )
                return inferred
            logger.warning(
                f"分享内容未识别到媒体文件名："
                f"类型={resource_type_name(resource_type, '未知')}"
            )
        return []

    @staticmethod
    def _infer_link_media(files: List[Dict[str, Any]]) -> Dict[str, Any]:
        """兼容旧调用：返回分享中出现频率最高的媒体。"""
        candidates = PlatformIntegrationService._infer_link_media_candidates(files)
        return candidates[0] if candidates else {}

    @staticmethod
    def _infer_link_media_candidates(files: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """聚合分享中的视频文件名，保留全部不同媒体元数据。"""
        scores: Counter = Counter()
        candidates: Dict[str, Dict[str, Any]] = {}
        media_extensions = {
            str(value).lower() for value in (settings.RMT_MEDIAEXT or [])
        }
        for item in files or []:
            if not isinstance(item, dict):
                continue
            name = str(
                item.get("name")
                or item.get("file_name")
                or item.get("filename")
                or item.get("path")
                or ""
            ).strip()
            if not name:
                continue
            file_name = Path(name).name
            if media_extensions and Path(file_name).suffix.lower() not in media_extensions:
                continue
            try:
                meta = MetaInfo(file_name)
            except Exception as error:
                logger.debug(f"分享文件名解析失败：{file_name}，原因：{error}")
                continue
            parsed_title = str(getattr(meta, "name", "") or "").strip()
            if not parsed_title:
                continue
            key = PlatformIntegrationService._normalize_agent_title(parsed_title)
            if not key:
                continue
            scores[key] += 1
            candidate = candidates.setdefault(key, {
                "title": parsed_title,
                "year": getattr(meta, "year", None),
                "media_type": (
                    "tv"
                    if getattr(meta, "begin_season", None) is not None
                       or getattr(meta, "begin_episode", None) is not None
                    else ""
                ),
                "seasons": set(),
            })
            parsed_season = getattr(meta, "begin_season", None)
            if parsed_season is not None and int(parsed_season) > 0:
                candidate["seasons"].add(int(parsed_season))
            for season_context in (
                    item.get("_relative_path"),
                    item.get("path"),
                    item.get("_cloud_dir"),
            ):
                candidate["seasons"].update(
                    PlatformIntegrationService._link_title_seasons(
                        season_context
                    )
                )
        result = []
        for selected_key, _score in scores.most_common():
            selected = dict(candidates[selected_key])
            selected["seasons"] = sorted(selected.get("seasons") or [])
            result.append(selected)
        return result

    @staticmethod
    def _link_media_payload(
            candidate: Dict[str, Any],
            source_title: str = "",
            season: Optional[int] = None,
            seasons: Optional[List[int]] = None,
    ) -> Dict[str, Any]:
        media_type = str(candidate.get("media_type") or "").lower()
        media = {
            "tmdb_id": int(candidate.get("tmdb_id") or 0),
            "media_type": media_type,
        }
        if media_type != "tv":
            return media
        resolved_seasons = sorted({
            int(value) for value in (seasons or [])
            if int(value) > 0
        })
        if not resolved_seasons:
            resolved_season = season
            if resolved_season:
                resolved_seasons = [max(1, int(resolved_season))]
        media["seasons"] = resolved_seasons
        return media

    @staticmethod
    def _set_workflow_output(context: Any, name: str, result: Dict[str, Any]) -> Any:
        outputs = dict(getattr(context, "node_outputs", None) or {})
        outputs[name] = dict(result)
        context.node_outputs = outputs
        return context

    def workflow_start_sync(
            self,
            context: Any,
            subscribe_id: int = 0,
            subscribe_ids: Optional[List[int]] = None,
            subscribe_states: Optional[str] = None,
            **kwargs,
    ) -> tuple[bool, Any]:
        requested_ids: List[int] = []
        raw_ids: Any = subscribe_ids
        if raw_ids is None and subscribe_id:
            raw_ids = [subscribe_id]
        if raw_ids is None:
            context_subscribes = list(getattr(context, "subscribes", None) or [])
            raw_ids = [getattr(item, "id", 0) for item in context_subscribes] or None
        if raw_ids is not None:
            if not isinstance(raw_ids, (list, tuple, set)):
                raw_ids = [raw_ids]
            try:
                normalized_ids = {int(value) for value in raw_ids}
            except (TypeError, ValueError):
                result = {"success": False, "message": "订阅 ID 参数格式错误"}
                return False, self._set_workflow_output(
                    context, "pansearch_sync", result
                )
            requested_ids = sorted(value for value in normalized_ids if value > 0)
            if not requested_ids:
                result = {"success": False, "message": "请提供有效的订阅 ID"}
                return False, self._set_workflow_output(
                    context, "pansearch_sync", result
                )
            with SessionFactory() as db:
                existing_ids = {
                    int(item.id)
                    for item in db.query(Subscribe).filter(
                        Subscribe.id.in_(requested_ids)
                    ).all()
                    if getattr(item, "id", None) is not None
                }
            missing_ids = [value for value in requested_ids if value not in existing_ids]
            if missing_ids:
                result = {
                    "success": False,
                    "message": f"订阅不存在：{', '.join(map(str, missing_ids))}",
                }
                return False, self._set_workflow_output(
                    context, "pansearch_sync", result
                )
        result: Dict[str, Any] = {}
        self.sync_subscribes(
            subscribe_ids=requested_ids or None,
            subscribe_states=subscribe_states,
            result=result,
        )
        data = dict(result.get("data") or {})
        data.update({
            "scope": (
                "selected" if requested_ids
                else "states" if subscribe_states
                else "all"
            ),
            "subscribe_ids": requested_ids,
            "subscribe_count": len(requested_ids),
            "subscribe_states": subscribe_states,
        })
        result["data"] = data
        return bool(result.get("success")), self._set_workflow_output(
            context, "pansearch_sync", result
        )

    def workflow_process_links(
            self,
            context: Any,
            subscribe_id: int = 0,
            resource_links: Any = None,
            title: str = "",
            media_type: str = "",
            season: Optional[int] = None,
            seasons: Optional[List[int]] = None,
            selection_id: str = "",
            tmdb_id: Optional[int] = None,
            **kwargs,
    ) -> tuple[bool, Any]:
        if not subscribe_id:
            subscribes = list(getattr(context, "subscribes", None) or [])
            if len(subscribes) == 1:
                subscribe_id = int(getattr(subscribes[0], "id", 0) or 0)
        raw_content = (
            resource_links
            if resource_links is not None else getattr(context, "content", "")
        )
        links = self.extract_resource_links(raw_content)
        recognized_title = str(title or "").strip()
        if not recognized_title and resource_links is None:
            recognized_title = str(raw_content or "")
            for link in links:
                recognized_title = recognized_title.replace(link, " ")
            recognized_title = " ".join(recognized_title.split())
        result = self.submit_platform_links(
            subscribe_id=subscribe_id,
            resource_links=links,
            wait=True,
            title=recognized_title,
            media_type=media_type,
            season=season,
            seasons=seasons,
            selection_id=selection_id,
            tmdb_id=tmdb_id,
            selection_scope="workflow",
        )
        return bool(result.get("success")), self._set_workflow_output(
            context, "pansearch_links", result
        )
