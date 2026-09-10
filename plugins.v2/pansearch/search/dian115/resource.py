"""Dian115 资源详情缓存与分享解锁协议。"""

import threading
import time
from typing import Any, Dict, Optional

from app.log import logger

from .client import Dian115Client, Dian115Error
from .security import resource_path, share_path
from ...utils.cache import (
    cached_resource_call,
    create_platform_ttl_cache,
    normalize_platform_cache_key,
)


class Dian115ResourceService:
    """负责资源接口及缓存，复用唯一的 Dian115 认证客户端。"""

    _DETAIL_CACHE_TTL = 10 * 60
    _DETAIL_CACHE_LIMIT = 512

    def __init__(self, client: Dian115Client):
        self._client = client
        self._detail_cache = create_platform_ttl_cache(
            "dian115:resource_details",
            getattr(client, "_email", id(client)),
            maxsize=self._DETAIL_CACHE_LIMIT,
            ttl=self._DETAIL_CACHE_TTL,
        )
        self._detail_locks = tuple(threading.Lock() for _ in range(32))
        self._lock = threading.RLock()

    def matches_client(self, client: Dian115Client) -> bool:
        return self._client is client

    def clear_cache(self) -> int:
        with self._lock:
            count = len(list(self._detail_cache.items()))
            self._detail_cache.clear()
            return count

    def resource_detail(
            self,
            tmdb_id: int,
            media_type: str,
            season: int = 0,
            force_refresh: bool = False,
    ) -> Dict[str, Any]:
        """按 TMDB 媒体标识读取资源及分享列表。"""
        normalized_id = int(tmdb_id)
        normalized_type = str(media_type or "").strip().lower()
        normalized_season = int(season or 0)
        cache_key = normalize_platform_cache_key(
            (normalized_id, normalized_type, normalized_season)
        )

        def load_detail() -> Dict[str, Any]:
            path = resource_path(
                normalized_type, normalized_id, normalized_season
            )
            key = path.rsplit("/", 1)[-1]
            payload = self._client.request_json(
                "GET",
                "/api/portal/resource-detail",
                path,
                params={"key": key},
            )
            payload["resource_key"] = key
            payload["resource_path"] = path
            return payload

        def log_cache_hit(waited: bool = False) -> None:
            stage = "等待后" if waited else ""
            logger.debug(
                f"Dian115 详情{stage}命中缓存：tmdb={normalized_id}，"
                f"type={normalized_type}，season={normalized_season}"
            )

        return cached_resource_call(
            self._detail_cache,
            cache_key,
            load_detail,
            locks=self._detail_locks,
            access_lock=self._lock,
            force_refresh=force_refresh,
            on_hit=lambda _: log_cache_hit(),
            on_wait_hit=lambda _: log_cache_hit(waited=True),
        )

    def unlock_share(
            self,
            share_id: int,
            resource_id: int = 0,
            max_unlock_points: Optional[int] = None,
            tmdb_id: int = 0,
            media_type: str = "",
            season: int = 0,
    ) -> Dict[str, Any]:
        """解锁分享；调用方预算已确认后直接提交解锁请求。"""
        normalized_share_id = int(share_id or 0)
        if normalized_share_id <= 0:
            raise Dian115Error("Dian115 分享 ID 无效")
        started = time.monotonic()
        logger.debug(
            f"Dian115 准备获取分享：share_id={normalized_share_id}，"
            f"resource_id={int(resource_id or 0)}，"
            f"预算={max_unlock_points if max_unlock_points is not None else '未限制'}"
        )
        current_path = share_path(normalized_share_id)
        body = {"share_id": normalized_share_id}
        if int(resource_id or 0):
            body["resource_id"] = int(resource_id)
        with self._client._lock:
            self._client._check_cooldown()
            payload = self._client._unlock_gate.run(lambda: self._client._request_json(
                "POST", "/api/portal/unlock", current_path, json=body
            ))
        unlock = payload.get("unlock") or {}
        already = bool(
            payload.get("already") or payload.get("owner")
            or unlock.get("already") or unlock.get("is_unlocked")
        )
        try:
            # 已解锁响应仍可能携带历史扣费记录，本次读取不再计费。
            actual_points = 0 if already else max(0, int(unlock.get("cost_points") or 0))
        except (TypeError, ValueError):
            actual_points = 0
        if max_unlock_points is not None and actual_points > int(max_unlock_points):
            logger.error(
                "Dian115 实际扣费超过调用方预算："
                f"share_id={normalized_share_id}，实际={actual_points}，"
                f"预算={int(max_unlock_points)}"
            )
        payload["actual_points"] = actual_points
        logger.debug(
            f"Dian115 分享获取完成：share_id={normalized_share_id}，"
            f"actual_points={actual_points}，"
            f"already={already}，"
            f"耗时={time.monotonic() - started:.2f}s"
        )
        with self._lock:
            normalized_type = str(media_type or "").strip().lower()
            if int(tmdb_id or 0) > 0 and normalized_type in {"movie", "tv"}:
                self._detail_cache.delete(
                    normalize_platform_cache_key(
                        (int(tmdb_id), normalized_type, int(season or 0))
                    )
                )
            else:
                self._detail_cache.clear()
        return payload
