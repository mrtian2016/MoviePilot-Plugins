"""HDHive WebAPI 资源查询、详情解析与解锁。"""

import copy
import random
import re
import threading
import time
from collections import deque
from typing import Any, Callable, Dict, List, Optional

from app.log import logger

from .action import ServerActionResponse
from .client import HDHiveClient, HDHiveWebError
from .parser import (
    ED2K_URL_RE,
    HDHIVE_DETAIL_RESOURCE_TYPES,
    HDHIVE_RESOURCE_TYPES,
    build_resource_detail_path,
    deduplicate,
    earliest_target_air_time,
    file_preview_capability_from_row,
    flatten_group_data,
    is_free_resource,
    normalize_languages,
    preview_episodes_from_files,
    preview_episodes_from_row,
    resource_slug as row_slug,
    resource_timestamp,
    resource_type as row_resource_type,
    resource_update_time,
    resolve_resource_detail_path,
    share_url_from_values,
    torrentclaw_rows,
    unlock_points,
    valid_share_url,
)
from .parser import (
    file_preview_capability,
    is_challenge_page,
    resource_detail_path,
    resource_group_data,
    response_text,
)
from ...matching import positive_ints
from ....utils.cache import create_platform_ttl_cache


class _UnlockLimiter:
    """资源服务级解锁节流与账号串行化。"""

    WINDOW_SECONDS = 60.0
    _STATE_LOCK = threading.RLock()
    _HISTORIES: Dict[str, deque] = {}
    _LOCKS: Dict[str, threading.RLock] = {}
    _READY_ATS: Dict[str, float] = {}

    def __init__(self, session_key: str, per_window: int):
        self._key = str(session_key or "")
        self._per_window = max(1, min(int(per_window or 2), 3))

    def lock(self) -> threading.RLock:
        with self._STATE_LOCK:
            return self._LOCKS.setdefault(self._key, threading.RLock())

    @property
    def per_window(self) -> int:
        return self._per_window

    def record(self) -> None:
        with self._STATE_LOCK:
            history = self._HISTORIES.setdefault(self._key, deque())
            now = time.monotonic()
            self._evict(history, now)
            history.append(now)

    def wait_for_slot(self, stop_requested, cooldown_remaining) -> None:
        def ensure_runnable():
            if stop_requested():
                raise HDHiveWebError("HDHive 解锁等待已停止", code="stopped")
            remaining = cooldown_remaining()
            if remaining > 0:
                raise HDHiveWebError(
                    "HDHive WebAPI 处于风控冷却期，跳过解锁"
                    f"（剩余 {int(remaining + 0.999)} 秒）",
                    code="rate_limited",
                )

        while True:
            ensure_runnable()
            wait_seconds = self._next_wait_seconds()
            if wait_seconds <= 0:
                return
            logger.debug(f"HDHive 解锁接口按节奏等待 {wait_seconds:.1f} 秒")
            deadline = time.monotonic() + wait_seconds
            while deadline > time.monotonic():
                ensure_runnable()
                time.sleep(min(deadline - time.monotonic(), 0.25))

    def _next_wait_seconds(self) -> float:
        interval = self.WINDOW_SECONDS / self._per_window + random.uniform(1.0, 4.0)
        with self._STATE_LOCK:
            history = self._HISTORIES.setdefault(self._key, deque())
            now = time.monotonic()
            self._evict(history, now)
            ready_at = self._READY_ATS.setdefault(
                self._key, now + random.uniform(3.0, 8.0)
            )
            wait_seconds = max(ready_at - now, 0.0)
            if history:
                wait_seconds = max(wait_seconds, interval - (now - history[-1]))
            if len(history) >= self._per_window:
                wait_seconds = max(
                    wait_seconds, self.WINDOW_SECONDS - (now - history[0])
                )
            return wait_seconds

    @classmethod
    def _evict(cls, history: deque, now: float) -> None:
        while history and now - history[0] >= cls.WINDOW_SECONDS:
            history.popleft()


class HDHiveResourceService:
    """负责 HDHive 资源查询、解析、缓存和解锁。"""

    BASE_URL = "https://re0.me"
    _RESOURCE_CACHE_TTL = 5 * 60
    _RESOURCE_CACHE_LIMIT = 128
    _RESOURCE_LOCKS = tuple(threading.Lock() for _ in range(64))
    _PREVIEW_CACHE_TTL = 10 * 60
    _PREVIEW_CACHE_LIMIT = 256
    _ACCESSIBLE_URL_CACHE_TTL = 10 * 60
    _ACCESSIBLE_URL_CACHE_LIMIT = 256
    _ACCESSIBLE_URL_LOCKS = tuple(threading.Lock() for _ in range(64))
    _PREVIEW_INTERVAL_SECONDS = 12.0
    _PREVIEW_WINDOW_SECONDS = 60.0
    _PREVIEWS_PER_WINDOW = 4
    _PREVIEW_STATE_LOCK = threading.RLock()
    _PREVIEW_HISTORIES: Dict[str, deque] = {}
    _PREVIEW_LOCKS: Dict[str, threading.RLock] = {}
    _PREVIEW_ACCOUNT_LOCKS: Dict[str, threading.Lock] = {}
    _PREVIEW_LOCK_BUCKETS = 32
    _SCHEMA_FAILURE_TTL = 60
    _PREVIEW_UNAVAILABLE_TTL = 15
    _TORRENTCLAW_CACHE_TTL = 30 * 60
    _TORRENTCLAW_CACHE_LIMIT = 128
    _TORRENTCLAW_REQUEST_INTERVAL = 60.0
    _TORRENTCLAW_STATE_LOCK = threading.RLock()
    _TORRENTCLAW_LAST_REQUEST_AT: Dict[str, float] = {}
    _TORRENTCLAW_RETRY_UNTIL: Dict[str, float] = {}

    def __init__(
            self,
            client: HDHiveClient,
            torrentclaw_enabled: bool = False,
            torrentclaw_subtitle_languages: Optional[List[str]] = None,
            unlocks_per_minute: int = 2,
    ):
        self._client = client
        self._torrentclaw_enabled = bool(torrentclaw_enabled)
        self._torrentclaw_subtitle_languages = normalize_languages(
            torrentclaw_subtitle_languages or ["zh"]
        )
        session_key = client.cache_namespace
        self._session_key = session_key
        self._unlock_limiter = _UnlockLimiter(session_key, unlocks_per_minute)
        self._resource_cache = create_platform_ttl_cache(
            "hdhive:web:rows",
            session_key,
            maxsize=self._RESOURCE_CACHE_LIMIT,
            ttl=self._RESOURCE_CACHE_TTL,
        )
        self._schema_failure_cache = create_platform_ttl_cache(
            "hdhive:web:schema_failures",
            session_key,
            maxsize=self._RESOURCE_CACHE_LIMIT,
            ttl=self._SCHEMA_FAILURE_TTL,
        )
        self._preview_cache = create_platform_ttl_cache(
            "hdhive:web:file_preview",
            session_key,
            maxsize=self._PREVIEW_CACHE_LIMIT,
            ttl=self._PREVIEW_CACHE_TTL,
        )
        self._preview_unavailable_cache = create_platform_ttl_cache(
            "hdhive:web:preview_unavailable",
            session_key,
            maxsize=self._PREVIEW_CACHE_LIMIT,
            ttl=self._PREVIEW_UNAVAILABLE_TTL,
        )
        self._preview_capability_cache = create_platform_ttl_cache(
            "hdhive:web:preview_capability",
            session_key,
            maxsize=self._PREVIEW_CACHE_LIMIT,
            ttl=self._PREVIEW_CACHE_TTL,
        )
        self._accessible_url_cache = create_platform_ttl_cache(
            "hdhive:web:accessible_url",
            session_key,
            maxsize=self._ACCESSIBLE_URL_CACHE_LIMIT,
            ttl=self._ACCESSIBLE_URL_CACHE_TTL,
        )
        self._torrentclaw_cache = create_platform_ttl_cache(
            "hdhive:web:torrentclaw",
            session_key,
            maxsize=self._TORRENTCLAW_CACHE_LIMIT,
            ttl=self._TORRENTCLAW_CACHE_TTL,
        )
        self._lock = threading.RLock()

    def matches_config(
            self,
            client: HDHiveClient,
            torrentclaw_enabled: bool,
            torrentclaw_subtitle_languages: Any,
            unlocks_per_minute: int = 2,
    ) -> bool:
        return (
                self._client is client
                and self._torrentclaw_enabled == bool(torrentclaw_enabled)
                and self._torrentclaw_subtitle_languages
                == normalize_languages(torrentclaw_subtitle_languages or ["zh"])
                and self._unlock_limiter.per_window
                == max(1, min(int(unlocks_per_minute or 2), 3))
        )

    def clear_cache(self) -> Dict[str, int]:
        with self._lock:
            counts = {
                "resources": len(list(self._resource_cache.items())),
                "previews": len(list(self._preview_cache.items())),
                "accessible_urls": len(list(self._accessible_url_cache.items())),
                "torrentclaw": len(list(self._torrentclaw_cache.items())),
            }
            self._resource_cache.clear()
            self._schema_failure_cache.clear()
            self._preview_cache.clear()
            self._preview_unavailable_cache.clear()
            self._preview_capability_cache.clear()
            self._accessible_url_cache.clear()
            self._torrentclaw_cache.clear()
            return counts

    def _preview_lock(self, preview_key: str) -> threading.RLock:
        """合并同一账号、同一资源的并发预览，不阻塞其他缓存命中。"""
        bucket = hash(preview_key) % self._PREVIEW_LOCK_BUCKETS
        lock_key = f"{self._client.cache_namespace}:{bucket}"
        with self._PREVIEW_STATE_LOCK:
            return self._PREVIEW_LOCKS.setdefault(lock_key, threading.RLock())

    def _preview_account_lock(self) -> threading.Lock:
        with self._PREVIEW_STATE_LOCK:
            return self._PREVIEW_ACCOUNT_LOCKS.setdefault(
                self._client.cache_namespace, threading.Lock()
            )

    def _wait_for_preview_slot(self, log_prefix: str) -> None:
        """限制详情页 file-list 的账号级调用频率。"""
        logged = False
        while True:
            if self._client.cooldown_remaining > 0:
                raise HDHiveWebError(
                    "HDHive WebAPI 处于风控冷却期，跳过文件预览",
                    code="rate_limited",
                )
            with self._PREVIEW_STATE_LOCK:
                history = self._PREVIEW_HISTORIES.setdefault(
                    self._client.cache_namespace, deque()
                )
                now = time.monotonic()
                while history and now - history[0] >= self._PREVIEW_WINDOW_SECONDS:
                    history.popleft()
                wait_seconds = 0.0
                if history:
                    wait_seconds = max(
                        wait_seconds,
                        self._PREVIEW_INTERVAL_SECONDS - (now - history[-1]),
                    )
                if len(history) >= self._PREVIEWS_PER_WINDOW:
                    wait_seconds = max(
                        wait_seconds,
                        self._PREVIEW_WINDOW_SECONDS - (now - history[0]),
                    )
                if wait_seconds <= 0:
                    history.append(now)
                    return
            if not logged:
                logger.debug(
                    f"{log_prefix} file-list 预览限频等待 {wait_seconds:.1f} 秒"
                )
                logged = True
            time.sleep(min(wait_seconds, 0.25))

    def _load_file_preview(
            self,
            slug: str,
            resource_type: str,
            log_prefix: str,
            detail_path: str = "",
    ) -> Dict[str, Any]:
        """限频读取并缓存 file-list，不访问详情页且绝不触发解锁。"""
        preview_key = f"{resource_type}:{slug}"
        preview = self._preview_cache.get(preview_key)
        if isinstance(preview, dict):
            logger.debug(f"{log_prefix} 命中 file-list 预览缓存：slug={slug}")
            return copy.deepcopy(preview)
        failure_key = f"preview:{preview_key}"
        unavailable = self._preview_unavailable_cache.get(preview_key)
        if unavailable:
            raise HDHiveWebError(str(unavailable), code="preview_unavailable")
        cached_failure = self._schema_failure_cache.get(failure_key)
        if cached_failure:
            raise HDHiveWebError(str(cached_failure), code="preview_invalid")
        with self._preview_lock(preview_key):
            preview = self._preview_cache.get(preview_key)
            if isinstance(preview, dict):
                return copy.deepcopy(preview)
            unavailable = self._preview_unavailable_cache.get(preview_key)
            if unavailable:
                raise HDHiveWebError(
                    str(unavailable), code="preview_unavailable"
                )
            cached_failure = self._schema_failure_cache.get(failure_key)
            if cached_failure:
                raise HDHiveWebError(str(cached_failure), code="preview_invalid")
            account_lock = self._preview_account_lock()
            if not account_lock.acquire(blocking=False):
                raise HDHiveWebError(
                    "HDHive 已有资源预览正在进行，请稍后再试",
                    code="preview_busy",
                )
            try:
                preview_started = time.monotonic()
                self._wait_for_preview_slot(log_prefix)
                requested_at = time.monotonic()
                resolved_detail_path = self._resolve_detail_path(
                    resource_type, slug, detail_path
                )
                response = self._client.signed_request(
                    "GET",
                    f"/api/customer/resources/{slug}/file-list",
                    headers={
                        "accept": "application/json",
                        "referer": f"{self.BASE_URL}{resolved_detail_path}",
                    },
                )
                logger.debug(
                    f"{log_prefix} file-list 请求完成：slug={slug}，"
                    f"限频等待={requested_at - preview_started:.2f}s，"
                    f"请求链={time.monotonic() - requested_at:.2f}s"
                )
                try:
                    payload = response.json()
                except ValueError as error:
                    self._schema_failure_cache.set(
                        failure_key, "HDHive file-list 响应格式异常"
                    )
                    raise HDHiveWebError(
                        "HDHive file-list 响应格式异常", code="preview_invalid"
                    ) from error
                preview = payload.get("data") if isinstance(payload, dict) else None
                if not isinstance(preview, dict):
                    error_value = payload.get("error") if isinstance(payload, dict) else None
                    message = str(
                        payload.get("message")
                        or (
                            error_value.get("message")
                            if isinstance(error_value, dict) else error_value
                        )
                        or ""
                        if isinstance(payload, dict) else ""
                    ).strip()
                    business_failure = isinstance(payload, dict) and (
                            payload.get("success") is False
                            or bool(message)
                            or isinstance(error_value, dict)
                    )
                    if business_failure:
                        failure_message = "HDHive 暂时无法提供该资源文件列表"
                        self._preview_unavailable_cache.set(
                            preview_key, failure_message
                        )
                        logger.debug(
                            f"{log_prefix} file-list 业务失败：slug={slug}，"
                            f"原因={message or '站点未返回文件列表'}"
                        )
                        raise HDHiveWebError(
                            failure_message, code="preview_unavailable"
                        )
                    failure_message = (
                        f"HDHive file-list 缺少 data：{message}"
                        if message else "HDHive file-list 缺少 data"
                    )
                    self._schema_failure_cache.set(failure_key, failure_message)
                    raise HDHiveWebError(
                        failure_message, code="preview_invalid"
                    )
                self._preview_cache.set(preview_key, copy.deepcopy(preview))
                return copy.deepcopy(preview)
            finally:
                account_lock.release()

    def preview_resource(
            self,
            slug: str,
            resource_type: str,
            target_season: Optional[int] = None,
            target_episodes: Optional[List[int]] = None,
            supports_file_preview: Optional[bool] = None,
            detail_path: str = "",
            log_prefix: str = "[HDHIVE]",
    ) -> Dict[str, Any]:
        """只读预览未解锁资源，返回文件和集数信息。"""
        normalized_slug = str(slug or "").strip()
        normalized_type = str(resource_type or "").strip().lower()
        if (
                not normalized_slug
                or normalized_type not in HDHIVE_DETAIL_RESOURCE_TYPES
        ):
            raise HDHiveWebError(
                "HDHive 资源标识或类型无效", code="invalid_resource"
            )
        can_preview = supports_file_preview
        if can_preview is None:
            can_preview = self._resolve_file_preview_capability(
                normalized_slug, normalized_type, log_prefix, detail_path
            )
        if not can_preview:
            raise HDHiveWebError(
                f"HDHive {normalized_type.upper()} 资源不支持文件列表预览",
                code="preview_unsupported",
            )
        preview = self._load_file_preview(
            normalized_slug, normalized_type, log_prefix, detail_path
        )
        files = preview.get("files") or []
        preview_episodes = preview_episodes_from_files(
            files, target_season
        )
        targets = positive_ints(target_episodes)
        available = positive_ints(preview_episodes.get(
            str(max(1, int(target_season or 1)))
        ))
        return {
            "files": copy.deepcopy(files),
            "preview_episodes": preview_episodes,
            "resource_validate_status": str(
                preview.get("resource_validate_status") or ""
            ),
            "resource_validate_message": str(
                preview.get("resource_validate_message") or ""
            ),
            "covers_target": bool(targets & available) if targets else None,
        }

    def _http_access_resource(
            self,
            slug: str,
            resource_type: str,
            is_unlocked: bool,
            target_season: Optional[int],
            target_episodes: Optional[List[int]],
            supports_file_preview: Optional[bool],
            detail_path: str,
            log_prefix: str,
    ) -> Dict[str, Any]:
        """复用搜索阶段的 file-list 校验后提交解锁页面请求。"""
        detail_path = self._resolve_detail_path(
            resource_type, slug, detail_path
        )
        if is_unlocked:
            return self._access_unlocked_resource(
                slug, resource_type, detail_path, log_prefix
            )

        # file-list 是解锁前的必要资源覆盖校验，避免对缺集或已失效资源
        can_preview = supports_file_preview
        if can_preview is None:
            can_preview = self._resolve_file_preview_capability(
                slug, resource_type, log_prefix, detail_path
            )
        preview_episodes: Dict[str, List[int]] = {}
        if can_preview:
            preview = self._load_file_preview(
                slug, resource_type, log_prefix, detail_path
            )
            files = preview.get("files") or []
            preview_episodes = preview_episodes_from_files(files, target_season)
            targets = positive_ints(target_episodes)
            season_key = str(max(1, int(target_season or 1)))
            available = positive_ints(preview_episodes.get(season_key))
            if targets and (not available or not (targets & available)):
                logger.debug(
                    f"{log_prefix} file-list 未覆盖当前缺集，跳过资源：slug={slug}"
                )
                return {
                    "url": "",
                    "preview_episodes": preview_episodes,
                    "is_unlocked": False,
                    "skip_reason": "target_not_covered",
                }
            if str(preview.get("resource_validate_status") or "").lower() == "invalid":
                logger.debug(f"{log_prefix} file-list 标记资源失效，跳过资源：slug={slug}")
                return {
                    "url": "",
                    "preview_episodes": preview_episodes,
                    "is_unlocked": False,
                    "skip_reason": "resource_invalid",
                }
        else:
            logger.debug(
                f"{log_prefix} {resource_type.upper()} 不支持 file-list，"
                f"已按资源卡片 remark 预筛结果继续：slug={slug}"
            )
        normalized_path = str(detail_path or "").strip()
        if not normalized_path:
            raise HDHiveWebError(
                "HDHive 资源页路径或标识无效", code="invalid_resource"
            )
        with self._unlock_limiter.lock():
            self._unlock_limiter.wait_for_slot(
                self._client.stop_requested,
                lambda: self._client.cooldown_remaining,
            )
            response = self._client.web_unlock_request(
                normalized_path,
                slug,
                page_headers={
                    "accept": "text/html,application/xhtml+xml",
                    "referer": f"{self.BASE_URL}/",
                },
                on_submit=self._unlock_limiter.record,
            )
        result = self._unlock_response(response, resource_type)
        return {
            **result,
            "preview_episodes": preview_episodes,
            "is_unlocked": bool(result.get("url")),
        }

    def _access_unlocked_resource(
            self,
            slug: str,
            resource_type: str,
            detail_path: str,
            log_prefix: str,
    ) -> Dict[str, Any]:
        """读取已解锁资源详情页并解析分享链接（带链接缓存与并发合并）。"""

        def cached_url(access_key: str) -> str:
            share_url = self._accessible_url_cache.get(access_key) or ""
            if share_url and not valid_share_url(share_url, resource_type):
                self._accessible_url_cache.delete(access_key)
                return ""
            return share_url

        access_key = f"{resource_type}:{slug}"
        share_url = cached_url(access_key)
        if share_url:
            logger.debug(f"{log_prefix} 命中已解锁资源链接缓存：slug={slug}")
        else:
            lock_key = f"{self._session_key}:{access_key}"
            access_lock = self._ACCESSIBLE_URL_LOCKS[
                hash(lock_key) % len(self._ACCESSIBLE_URL_LOCKS)
                ]
            with access_lock:
                share_url = cached_url(access_key)
                if not share_url:
                    share_url = self._parse_unlocked_share_url(
                        slug, resource_type, detail_path, log_prefix
                    )
        if not share_url:
            logger.warning(
                f"{log_prefix} 已解锁资源详情页未解析到链接：slug={slug}"
            )
        return {
            "url": share_url,
            "preview_episodes": {},
            "is_unlocked": bool(share_url),
            "already_owned": True,
        }

    def _parse_unlocked_share_url(
            self,
            slug: str,
            resource_type: str,
            detail_path: str,
            log_prefix: str,
    ) -> str:
        """请求详情页并解析分享链接，成功后写入链接缓存。"""
        access_key = f"{resource_type}:{slug}"
        page_started = time.monotonic()
        page_response = self._client.request(
            "GET",
            detail_path,
            headers={
                "accept": "text/html,application/xhtml+xml",
                "referer": f"{self.BASE_URL}/",
            },
        )
        page_text = getattr(page_response, "text", "") or ""
        parse_started = time.monotonic()
        share_url = share_url_from_values(page_text, resource_type)
        logger.debug(
            f"{log_prefix} 已解锁资源详情读取完成：slug={slug}，"
            f"请求={parse_started - page_started:.2f}s，"
            f"解析={time.monotonic() - parse_started:.3f}s"
        )
        if share_url:
            self._accessible_url_cache.set(access_key, share_url)
        return share_url

    def _load_resource_rows(
            self,
            media_type: str,
            tmdb_id: int,
            resource_types: List[str],
            force_refresh: bool = False,
            log_prefix: str = "[HDHIVE]",
    ) -> List[Dict[str, Any]]:
        normalized_type = str(media_type or "").strip().lower()
        if normalized_type not in {"movie", "tv"}:
            raise HDHiveWebError("HDHive 媒体类型无效", code="invalid_media_type")
        enabled_types = tuple(dict.fromkeys(
            str(value or "").strip().lower()
            for value in (resource_types or [])
            if str(value or "").strip().lower() in HDHIVE_RESOURCE_TYPES
        ))
        if not enabled_types:
            return []
        cache_key = f"v2:{normalized_type}:{int(tmdb_id)}"
        requested = set(enabled_types)

        def select_rows(raw_rows: Any) -> List[Dict[str, Any]]:
            rows = [
                dict(row) for row in (raw_rows or [])
                if isinstance(row, dict)
                   and row_resource_type(row) in requested
            ]
            if self._torrentclaw_enabled and "magnet" in requested:
                rows.extend(self._load_torrentclaw_rows(
                    normalized_type, int(tmdb_id), log_prefix
                ))
            return deduplicate(rows)

        if not force_refresh:
            cached = self._resource_cache.get(cache_key)
            if isinstance(cached, list):
                logger.debug(
                    f"{log_prefix} WebAPI 命中资源页缓存：{len(cached)} 条原始资源"
                )
                return select_rows(cached)
            if self._schema_failure_cache.get(cache_key):
                logger.debug(f"{log_prefix} WebAPI 命中详情解析失败短缓存，跳过重复请求")
                return []
        lock_key = f"{self._session_key}:{cache_key}"
        cache_lock = self._RESOURCE_LOCKS[
            hash(lock_key) % len(self._RESOURCE_LOCKS)
            ]
        with cache_lock:
            if not force_refresh:
                cached = self._resource_cache.get(cache_key)
                if isinstance(cached, list):
                    logger.debug(
                        f"{log_prefix} WebAPI 等待并命中资源页缓存："
                        f"{len(cached)} 条原始资源"
                    )
                    return select_rows(cached)
                if self._schema_failure_cache.get(cache_key):
                    logger.debug(
                        f"{log_prefix} WebAPI 等待并命中详情解析失败短缓存，"
                        "跳过重复请求"
                    )
                    return []
            try:
                route_started = time.monotonic()
                with self._client.related_requests(2):
                    detail_response = self._client.request(
                        "GET",
                        f"/tmdb/{normalized_type}/{int(tmdb_id)}",
                        headers={
                            "accept": (
                                "text/html,application/xhtml+xml,application/xml;q=0.9,"
                                "*/*;q=0.8"
                            ),
                            "cache-control": "no-cache",
                            "referer": f"{self._client.BASE_URL}/",
                        },
                    )
                    detail_path = resource_detail_path(detail_response)
                    if not detail_path:
                        raise HDHiveWebError(
                            "HDHive TMDB 入口未返回资源详情路径",
                            code="schema_changed",
                        )
                    detail_started = time.monotonic()
                    group_response = self._client.request(
                        "GET",
                        detail_path,
                        headers={
                            "accept": (
                                "text/html,application/xhtml+xml,application/xml;q=0.9,"
                                "*/*;q=0.8"
                            ),
                            "cache-control": "no-cache",
                            "referer": f"{self._client.BASE_URL}/",
                        },
                    )
                    group_data = self._group_data_from_response(group_response)
                parsed_at = time.monotonic()
                logger.debug(
                    f"{log_prefix} WebAPI 页面阶段耗时："
                    f"媒体页={detail_started - route_started:.2f}s，"
                    f"资源页={parsed_at - detail_started:.2f}s"
                )
            except HDHiveWebError as error:
                if error.code == "schema_changed":
                    self._schema_failure_cache.set(cache_key, True)
                    logger.debug(f"{log_prefix} WebAPI 详情结构未识别，写入 60 秒短缓存")
                    return []
                raise
            rows = flatten_group_data(group_data)
            logger.debug(
                f"{log_prefix} WebAPI 分组解析耗时："
                f"{time.monotonic() - parsed_at:.3f}s"
            )
            if not group_data:
                logger.debug(f"{log_prefix} WebAPI 详情页无资源分组，按正常空结果处理")
            self._resource_cache.set(cache_key, copy.deepcopy(rows))
            return select_rows(rows)

    def _group_data_from_response(self, response) -> Dict[str, Any]:
        page_text = response_text(response)
        group_data = resource_group_data(page_text)
        if group_data is not None:
            return group_data
        if is_challenge_page(page_text):
            self._client.activate_risk_cooldown("详情页挑战保护")
            raise HDHiveWebError(
                "HDHive 详情页触发安全验证，已进入 600 秒风险保护冷却",
                code="rate_limited",
            )
        raise HDHiveWebError(
            "HDHive 详情页未解析到 groupData", code="schema_changed"
        )

    def _load_torrentclaw_rows(
            self, media_type: str, tmdb_id: int, log_prefix: str
    ) -> List[Dict[str, Any]]:
        cache_key = f"{media_type}:{tmdb_id}"
        state_key = self._client.cache_namespace
        cached = self._torrentclaw_cache.get(cache_key)
        if isinstance(cached, dict):
            return torrentclaw_rows(cached, self.BASE_URL)
        with self._lock:
            with self._TORRENTCLAW_STATE_LOCK:
                now = time.monotonic()
                last_request_at = self._TORRENTCLAW_LAST_REQUEST_AT.get(
                    state_key, 0.0
                )
                retry_until = self._TORRENTCLAW_RETRY_UNTIL.get(
                    state_key, 0.0
                )
                wait_seconds = max(
                    retry_until - now,
                    self._TORRENTCLAW_REQUEST_INTERVAL
                    - (now - last_request_at),
                    0.0,
                )
            if wait_seconds > 0:
                logger.debug(
                    f"{log_prefix} TorrentClaw 等待限速 {wait_seconds:.1f}s"
                )
                time.sleep(wait_seconds)
            started = time.monotonic()
            response = self._client.request(
                "GET",
                "/api/torrentclaw/torrents",
                params={"tmdbId": tmdb_id, "type": media_type},
                headers={
                    "accept": "application/json",
                    "referer": f"{self._client.BASE_URL}/",
                },
            )
            with self._TORRENTCLAW_STATE_LOCK:
                self._TORRENTCLAW_LAST_REQUEST_AT[state_key] = time.monotonic()
            try:
                payload = response.json()
            except ValueError as error:
                raise HDHiveWebError(
                    "TorrentClaw 返回数据格式异常", code="schema_changed"
                ) from error
            message = str(payload.get("message") or "") if isinstance(payload, dict) else ""
            if "获取过于频繁" in message:
                retry_match = re.search(r"(\d+)\s*秒后重试", message)
                retry_seconds = int(retry_match.group(1)) if retry_match else 60
                with self._TORRENTCLAW_STATE_LOCK:
                    self._TORRENTCLAW_RETRY_UNTIL[state_key] = (
                            time.monotonic() + retry_seconds + 1
                    )
                logger.debug(
                    f"{log_prefix} TorrentClaw 限流，{retry_seconds}s 后可重试"
                )
                return []
            if not isinstance(payload, dict):
                return []
            self._torrentclaw_cache.set(cache_key, payload)
            rows = torrentclaw_rows(payload, self.BASE_URL)
            preferred = self._torrentclaw_subtitle_languages
            if preferred:
                matched = [
                    row for row in rows
                    if any(
                        str(actual or "").lower() == expected
                        or str(actual or "").lower().startswith(f"{expected}-")
                        for actual in (row.get("subtitle_languages") or [])
                        for expected in preferred
                    )
                ]
                if matched:
                    rows = matched
            logger.debug(
                f"{log_prefix} TorrentClaw 返回 {len(rows)} 条，"
                f"耗时={time.monotonic() - started:.2f}s"
            )
            return rows

    def search_test_resources(
            self,
            tmdb_id: int,
            media_type: str,
            candidate_limit: int = 20,
            log_prefix: str = "[HDHIVE]",
    ) -> List[Dict[str, Any]]:
        """只读取资源页原始候选，不访问详情、不套预算或订阅规则。"""
        return self.search_resources(
            tmdb_id=tmdb_id,
            media_type=media_type,
            include_paid=True,
            resource_types=list(HDHIVE_RESOURCE_TYPES),
            candidate_limit=max(1, int(candidate_limit or 20)),
            log_prefix=log_prefix,
        )

    def search_resources(
            self,
            tmdb_id: int,
            media_type: str,
            include_paid: bool,
            target_season: Optional[int] = None,
            target_episodes: Optional[List[int]] = None,
            target_episode_air_dates: Optional[Dict[int, str]] = None,
            resource_types: Optional[List[str]] = None,
            magnet_filter: Optional[
                Callable[[List[Dict[str, Any]]], List[Dict[str, Any]]]
            ] = None,
            candidate_limit: Optional[int] = 4,
            log_prefix: str = "[HDHIVE]",
    ) -> List[Dict[str, Any]]:
        enabled_types = list(resource_types or HDHIVE_RESOURCE_TYPES)
        rows = self._load_resource_rows(
            media_type, int(tmdb_id), enabled_types, log_prefix=log_prefix
        )
        detail_rows = [
            row for row in rows
            if row_resource_type(row) != "magnet"
        ]
        magnet_rows = [
            row for row in rows
            if row_resource_type(row) == "magnet"
        ]
        preview_count = 0
        for row in detail_rows:
            preview_episodes = preview_episodes_from_row(
                row, target_season=target_season
            )
            if preview_episodes:
                row["preview_episodes"] = preview_episodes
                preview_count += 1
        target_episode_set = positive_ints(target_episodes)
        target_season_key = str(max(1, int(target_season or 1)))
        coverage_order = {}
        matched_rows = []
        remark_filtered_count = 0
        for row in detail_rows:
            preview = row.get("preview_episodes") or {}
            if not target_episode_set or not preview:
                coverage = (2, 0) if target_episode_set else (0, 0)
            else:
                covered = target_episode_set & positive_ints(
                    preview.get(target_season_key)
                )
                if not covered:
                    remark_filtered_count += 1
                    continue
                coverage = (
                    (0, -len(covered))
                    if covered == target_episode_set
                    else (1, -len(covered))
                )
            coverage_order[id(row)] = coverage
            matched_rows.append(row)
        type_order = {
            resource_type: index
            for index, resource_type in enumerate(enabled_types)
        }
        detail_rows = sorted(
            matched_rows,
            key=lambda row: (
                -resource_timestamp(resource_update_time(row)),
                type_order.get(row_resource_type(row), len(type_order)),
                not is_free_resource(row),
                row.get("is_official") is not True,
                *coverage_order[id(row)],
                unlock_points(row),
            ),
        )
        if magnet_filter:
            filtered = magnet_filter(magnet_rows)
            if filtered:
                magnet_rows = filtered
        earliest_air_time = earliest_target_air_time(
            target_episode_set, target_episode_air_dates or {}
        )
        media_page_url = (
            f"{self._client.BASE_URL}/tmdb/{media_type}/{int(tmdb_id)}"
        )
        results: List[Dict[str, Any]] = []
        accepted_groups = set()
        stale_count = 0
        limit = (
            None if candidate_limit is None
            else max(1, min(int(candidate_limit or 4), 50))
        )
        for row in detail_rows:
            if limit is not None and len(accepted_groups) >= limit:
                break
            resource_type = row_resource_type(row)
            slug = row_slug(row)
            if not resource_type or not slug:
                continue
            try:
                detail_path = build_resource_detail_path(
                    resource_type, slug, row.get("website")
                )
            except ValueError as error:
                raise HDHiveWebError(
                    str(error), code="invalid_resource"
                ) from error
            update_time = resource_update_time(row)
            resource_time = resource_timestamp(update_time)
            if (
                    earliest_air_time and resource_time
                    and resource_time < earliest_air_time - 24 * 60 * 60
            ):
                stale_count += 1
                continue
            points = unlock_points(row)
            is_unlocked = bool(row.get("is_unlocked"))
            is_free = points == 0
            share_url = (
                share_url_from_values(row, resource_type)
                if is_unlocked else ""
            )
            common = {
                "title": str(row.get("title") or f"HDHive {resource_type.upper()}资源"),
                "description": str(row.get("remark") or ""),
                "resolution": row.get("video_resolution") or "",
                "quality": "",
                "subtitle": row.get("subtitle_language") or "",
                "size": row.get("share_size") or 0,
                "update_time": update_time,
                "resource_ref": slug,
                "unlock_group": f"{resource_type}:{slug}",
                "resource_type": resource_type,
                "listed_unlock_points": points,
                "is_free": is_free,
                "is_unlocked": is_unlocked,
                "url": share_url,
                "is_official": bool(row.get("is_official")),
                "source_url": f"{self.BASE_URL}{detail_path}",
                "provider_data": {"detail_path": detail_path},
                "media_page_url": media_page_url,
                "target_season": int(target_season or 0),
                "target_episodes": sorted(target_episode_set),
                "preview_episodes": copy.deepcopy(
                    row.get("preview_episodes") or {}
                ),
                "supports_file_preview": file_preview_capability_from_row(row),
            }
            if is_unlocked:
                results.append({
                    **common,
                    "need_access": False,
                    "need_unlock": False,
                    "unlock_points": points,
                })
            elif is_free:
                results.append({
                    **common,
                    "url": "",
                    "need_access": False,
                    "need_unlock": True,
                    "unlock_points": 0,
                })
            elif include_paid:
                results.append({
                    **common,
                    "url": "",
                    "need_unlock": True,
                    "need_access": False,
                    "unlock_points": points,
                })
            else:
                continue
            accepted_groups.add(f"{resource_type}:{slug}")

        for row in magnet_rows:
            if limit is not None and len(accepted_groups) >= limit:
                break
            magnet_url = str(row.get("url") or "").strip()
            if not magnet_url.lower().startswith("magnet:?"):
                continue
            slug = row_slug(row)
            group = f"magnet:{slug or magnet_url}"
            if group in accepted_groups:
                continue
            results.append({
                "title": str(row.get("title") or "HDHive Magnet资源"),
                "description": row.get("description", ""),
                "resource_type": "magnet",
                "url": magnet_url,
                "resource_ref": slug,
                "unlock_group": group,
                "need_unlock": False,
                "need_access": False,
                "unlock_points": 0,
                "listed_unlock_points": 0,
                "is_free": True,
                "is_unlocked": True,
                "size": row.get("size") or 0,
                "update_time": row.get("created_at") or "",
                "source_url": str(row.get("source_url") or ""),
                "media_page_url": media_page_url,
            })
            accepted_groups.add(group)
        logger.debug(
            f"{log_prefix} WebAPI 候选整理完成：原始={len(rows)}，"
            f"集数已识别={preview_count}，remark过滤={remark_filtered_count}，"
            f"时间过滤={stale_count}，"
            f"资源页={len(accepted_groups)}，候选={len(results)}"
        )
        return results

    def unlock_resource(
            self,
            slug: str,
            resource_type: str,
            media_page_url: str = "",
            is_unlocked: bool = False,
            target_season: Optional[int] = None,
            target_episodes: Optional[List[int]] = None,
            supports_file_preview: Optional[bool] = None,
            detail_path: str = "",
            log_prefix: str = "[HDHIVE]",
    ) -> Dict[str, Any]:
        """按详情页、file-list 预览和 HTTP 解锁接口顺序获取链接。"""
        normalized_slug = str(slug or "").strip()
        normalized_type = str(resource_type or "").strip().lower()
        if not normalized_slug or normalized_type not in HDHIVE_DETAIL_RESOURCE_TYPES:
            raise HDHiveWebError("HDHive 资源标识或类型无效", code="invalid_resource")
        return self._http_access_resource(
            normalized_slug,
            normalized_type,
            bool(is_unlocked),
            target_season,
            target_episodes,
            supports_file_preview,
            detail_path,
            log_prefix,
        )

    def _unlock_response(
            self,
            response: ServerActionResponse,
            normalized_type: str,
    ) -> Dict[str, Any]:
        """解析统一的 Server Action 解锁响应。"""
        status_code = response.status_code
        payload = response.payload
        if payload is None:
            redirect_url = str(getattr(response, "redirect_url", "") or "").strip()
            if status_code < 400 and valid_share_url(
                    redirect_url, normalized_type
            ):
                self._resource_cache.clear()
                return {
                    "url": redirect_url,
                    "success": True,
                }
            raise HDHiveWebError(
                "HDHive 解锁 Server Action 响应格式异常",
                code="unlock_invalid_response",
                status_code=status_code,
            )
        data = payload.get("data") if isinstance(payload, dict) else None
        if not (
                status_code < 400
                and isinstance(payload, dict)
                and payload.get("success")
                and isinstance(data, dict)
        ):
            error_value = payload.get("error") if isinstance(payload, dict) else None
            error_message = (
                error_value.get("message") if isinstance(error_value, dict) else ""
            )
            message = str(
                payload.get("message") or error_message
                if isinstance(payload, dict) else ""
            ).strip()
            if any(marker in message for marker in ("页面过期", "请刷新页面")):
                self._resource_cache.clear()
                raise HDHiveWebError(
                    f"HDHive 资源页上下文刷新后仍已过期，停止本次解锁：{message}",
                    code="page_expired",
                    status_code=status_code,
                )
            if any(marker in message for marker in (
                    "高频", "人机验证", "安全验证", "访问频繁", "操作频繁",
            )):
                self._client.activate_risk_cooldown(
                    "网页解锁要求人机验证但未返回 challenge"
                )
                raise HDHiveWebError(
                    f"HDHive 网页解锁要求人机验证但未返回可处理的 challenge：{message}",
                    code="captcha_required",
                    status_code=status_code,
                )
            if status_code == 429:
                raise HDHiveWebError(
                    f"HDHive 获取资源触发 HTTP 429 风控：{message or '请求过于频繁'}",
                    code="rate_limited",
                    status_code=status_code,
                )
            raise HDHiveWebError(
                f"HDHive 获取资源失败：{message or f'HTTP {status_code}'}",
                code="unlock_failed",
                status_code=status_code,
            )
        value = str(data.get("full_url") or data.get("url") or "").strip()
        urls = (
            list(dict.fromkeys(
                candidate
                for candidate in (
                    match.group(0) for match in ED2K_URL_RE.finditer(value)
                )
                if valid_share_url(candidate, normalized_type)
            ))
            if normalized_type == "ed2k"
            else (
                [value]
                if valid_share_url(value, normalized_type)
                else []
            )
        )
        if urls:
            self._resource_cache.clear()
        return {
            "url": urls if len(urls) > 1 else (urls[0] if urls else ""),
            "success": True,
            "already_owned": bool(data.get("already_owned")),
        }

    def _resolve_file_preview_capability(
            self, slug: str, resource_type: str, log_prefix: str,
            detail_path: str = "",
    ) -> bool:
        """从资源详情页实际渲染参数确认 file-list 能力并短期缓存。"""
        capability_key = f"{resource_type}:{slug}"
        cached = self._preview_capability_cache.get(capability_key)
        if isinstance(cached, bool):
            return cached
        detail_path = self._resolve_detail_path(
            resource_type, slug, detail_path
        )
        response = self._client.request(
            "GET",
            detail_path,
            headers={
                "accept": "text/html,application/xhtml+xml",
                "referer": f"{self.BASE_URL}/",
            },
        )
        supported = file_preview_capability(response)
        if supported is None:
            raise HDHiveWebError(
                "HDHive 资源页未声明文件列表预览能力，已停止探测",
                code="preview_capability_unknown",
            )
        self._preview_capability_cache.set(capability_key, supported)
        logger.debug(
            f"{log_prefix} 资源页 file-list 能力：slug={slug}，"
            f"支持={supported}"
        )
        return supported

    def _resolve_detail_path(
            self, resource_type: str, slug: str, detail_path: str = ""
    ) -> str:
        try:
            return resolve_resource_detail_path(
                resource_type,
                slug,
                detail_path,
                base_url=self.BASE_URL,
            )
        except ValueError as error:
            raise HDHiveWebError(
                str(error), code="invalid_resource"
            ) from error
