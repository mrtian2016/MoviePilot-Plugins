"""运行状态、离线任务与历史操作 API。"""

import asyncio
import json
import time
from typing import Any, Dict, Optional

from app.core.config import global_vars, settings
from app.log import logger
from fastapi import Request
from fastapi.responses import StreamingResponse

from .page import clear_ui_options_cache
from .. import CloudDriveCapability, OwnerDelegator
from ...utils import clear_magnet_metadata_cache


class RuntimeApi(OwnerDelegator):
    def api_vue_stop_sync(self) -> dict:
        return self.api_stop_sync(settings.API_TOKEN)

    def api_vue_stop_sync_task(self, payload: Dict[str, Any]) -> dict:
        return self.api_stop_sync_task(
            settings.API_TOKEN,
            str((payload or {}).get("task_id") or ""),
        )

    def api_vue_runtime_status(self) -> dict:
        return self.api_runtime_status(settings.API_TOKEN)

    async def api_vue_runtime_stream(self, request: Request) -> StreamingResponse:
        """以单条 SSE 连接推送运行态，合并高频进度变化。"""

        async def event_generator():
            last_revision = -1
            last_heartbeat = time.monotonic()
            yield "retry: 3000\n\n"
            try:
                while not global_vars.is_system_stopped:
                    if await request.is_disconnected():
                        break
                    revision = self._runtime_revision_value()
                    now = time.monotonic()
                    if revision != last_revision:
                        snapshot = self._runtime_snapshot()
                        last_revision = int(snapshot.get("revision") or 0)
                        payload = json.dumps(
                            snapshot, ensure_ascii=False, separators=(",", ":")
                        )
                        yield f"data: {payload}\n\n"
                        last_heartbeat = now
                    elif now - last_heartbeat >= 15:
                        yield ": keepalive\n\n"
                        last_heartbeat = now
                    await asyncio.sleep(0.5)
            except asyncio.CancelledError:
                return

        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    def api_vue_offline_tasks(self, refresh: bool = False) -> dict:
        return self.api_offline_tasks(settings.API_TOKEN, refresh)

    def api_vue_refresh_offline_tasks(self) -> dict:
        """显式刷新离线任务；周期读取保持只读缓存语义。"""
        return self.api_offline_tasks(settings.API_TOKEN, refresh=True)

    def api_vue_delete_offline_task(self, payload: Dict[str, Any]) -> dict:
        return self.api_delete_offline_task(
            settings.API_TOKEN,
            str((payload or {}).get("task_id") or ""),
            str((payload or {}).get("pending_key") or ""),
        )

    def api_vue_delete_offline_tasks(self, payload: Dict[str, Any]) -> dict:
        task_ids = [
            str(value).strip()
            for value in ((payload or {}).get("task_ids") or [])
            if str(value).strip()
        ]
        pending_keys = [
            str(value).strip()
            for value in ((payload or {}).get("pending_keys") or [])
            if str(value).strip()
        ]
        return self.api_delete_offline_tasks(
            settings.API_TOKEN, task_ids, pending_keys
        )

    def api_vue_retry_offline_tasks(self, payload: Dict[str, Any]) -> dict:
        pending_keys = [
            str(value).strip()
            for value in ((payload or {}).get("pending_keys") or [])
            if str(value).strip()
        ]
        task_ids = [
            str(value).strip()
            for value in ((payload or {}).get("task_ids") or [])
            if str(value).strip()
        ]
        return self.api_retry_offline_tasks(
            settings.API_TOKEN,
            pending_keys,
            task_ids,
        )

    def api_vue_clear_history(self, payload: Optional[Dict[str, Any]] = None) -> dict:
        return self.api_clear_history(
            settings.API_TOKEN,
            force=(payload or {}).get("force") is True,
            clear_points_history=(payload or {}).get("clear_points_history") is True,
        )

    def api_vue_delete_history(self, payload: Dict[str, Any]) -> dict:
        return self.api_delete_history(settings.API_TOKEN, payload or {})

    def api_vue_delete_history_batch(self, payload: Dict[str, Any]) -> dict:
        data = payload or {}
        return self.api_delete_history_batch(
            settings.API_TOKEN,
            identities=data.get("records") or [],
            delete_linked_files=data.get("delete_linked_files") is True,
        )

    def api_vue_upgrade_history(self, payload: Dict[str, Any]) -> dict:
        return self.api_upgrade_history(settings.API_TOKEN, payload or {})

    def api_vue_notify_history(self, payload: Dict[str, Any]) -> dict:
        return self.api_notify_history(settings.API_TOKEN, payload or {})

    def api_vue_retry_history(self, payload: Dict[str, Any]) -> dict:
        if not self._sync_handler:
            return {"success": False, "message": "同步处理器未初始化"}
        try:
            result = self._sync_handler.retry_history_record(
                record_time=str((payload or {}).get("time") or ""),
                share_url=str((payload or {}).get("share_url") or ""),
                file_name=str((payload or {}).get("file_name") or ""),
            )
            return {"success": True, "message": "历史记录已重新处理", "data": result}
        except Exception as error:
            logger.error(f"重新处理历史记录异常：{error}")
            return {"success": False, "message": str(error)}

    _CACHE_CATEGORIES = frozenset({
        "search", "cloud", "sync", "interface", "platform",
    })

    def api_vue_clear_cache(
            self, payload: Optional[Dict[str, Any]] = None
    ) -> dict:
        requested = (payload or {}).get("categories")
        categories = (
            self._CACHE_CATEGORIES
            if requested is None
            else {
                str(value).strip().lower()
                for value in requested
                if str(value).strip().lower() in self._CACHE_CATEGORIES
            }
        )
        if not categories:
            return {"success": False, "message": "请至少选择一项缓存内容"}
        counts: Dict[str, int] = {}
        try:
            if "search" in categories:
                if self._search_handler:
                    counts.update(self._search_handler.clear_search_cache())
                counts["magnet_metadata"] = clear_magnet_metadata_cache()
            if "cloud" in categories and (
                    self._cloud_drive and self._cloud_drive.supports(
                    CloudDriveCapability.CACHE_MAINTENANCE
            )
            ):
                cache_service = self._cloud_drive.require(
                    CloudDriveCapability.CACHE_MAINTENANCE
                )
                counts.update(cache_service.clear_cache())
            if "sync" in categories and self._sync_handler:
                counts.update(self._sync_handler.clear_runtime_cache())
            if "interface" in categories:
                counts["ui_options"] = clear_ui_options_cache()
            if "platform" in categories:
                counts.update(self.clear_platform_cache())
            total = sum(int(value or 0) for value in counts.values())
            logger.info(
                f"插件缓存已清理：{total} 项，分类={','.join(sorted(categories))}"
            )
            return {
                "success": True,
                "message": f"缓存已清理，共移除 {total} 项",
                "data": {
                    "categories": sorted(categories),
                    "counts": counts,
                },
            }
        except Exception as error:
            logger.error(f"清理插件缓存失败：{error}")
            return {"success": False, "message": str(error)}
