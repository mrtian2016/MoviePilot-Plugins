"""光鸭离线下载能力。"""

from __future__ import annotations

import re
import time
from threading import RLock
from typing import Any, Dict, List, Optional
from urllib.parse import unquote

from app.log import logger

from ..common import safe_int
from ...utils.magnet import parse_magnet_metadata

_ED2K_RE = re.compile(
    r"ed2k://\|file\|([^|]+)\|(\d+)\|([0-9A-Fa-f]{32})\|/?", re.I
)

_STATE_BY_STATUS = {
    0: "queued",
    1: "running",
    2: "completed",
    3: "failed",
    4: "retrying",
}
_STATUS_TEXT = {
    0: "等待下载",
    1: "下载中",
    2: "已完成",
    3: "下载失败",
    4: "重试中",
}


class GuangyaOfflineService:
    """封装光鸭离线接口、链接解析和任务提交。"""

    CACHE_TTL = 60

    def __init__(self, client: Any, files: Any):
        self.client = client
        self._files = files
        self._lock = RLock()
        self._tasks: List[Dict[str, Any]] = []
        self._updated_at = 0.0
        self._refresh_ok = False

    def create_cloud_task(self, url: str, parent_id: str = "") -> Dict[str, Any]:
        return self.client.request(
            "POST",
            f"{self.client.API_BASE_URL}/nd.bizcloudcollection.s/v1/create_task",
            json_data={"url": url, "parentId": parent_id or ""},
        )

    def resolve_cloud_url(self, url: str) -> Dict[str, Any]:
        return self.client.request(
            "POST",
            f"{self.client.API_BASE_URL}/nd.bizcloudcollection.s/v1/resolve_res",
            json_data={"url": url},
        )

    def cloud_task_list(
            self,
            page: int = 0,
            page_size: int = 50,
            status: Optional[List[int]] = None,
    ) -> Dict[str, Any]:
        return self.client.request(
            "POST",
            f"{self.client.API_BASE_URL}/nd.bizcloudcollection.s/v1/list_task",
            json_data={
                "page": page,
                "pageSize": page_size,
                "status": status if status is not None else [0, 1, 3, 4],
            },
        )

    def get_task_status(self, task_id: str) -> Dict[str, Any]:
        return self.client.request(
            "POST",
            f"{self.client.API_BASE_URL}/nd.bizuserres.s/v1/get_task_status",
            json_data={"taskId": task_id},
        )

    @staticmethod
    def is_ed2k_url(url: str) -> bool:
        return isinstance(url, str) and url.lstrip().lower().startswith("ed2k://")

    @staticmethod
    def is_magnet_url(url: str) -> bool:
        return isinstance(url, str) and url.lstrip().lower().startswith("magnet:?")

    @classmethod
    def is_offline_url(cls, url: str) -> bool:
        return cls.is_ed2k_url(url) or cls.is_magnet_url(url)

    @staticmethod
    def parse_ed2k_link(url: str) -> Dict[str, Any]:
        normalized = str(url or "").replace("｜", "|").strip()
        match = _ED2K_RE.fullmatch(normalized)
        if not match:
            return {}
        return {
            "url": normalized,
            "name": unquote(match.group(1)),
            "size": safe_int(match.group(2)),
            "hash": match.group(3).upper(),
        }

    def parse_magnet_link(
            self, url: str, fetch_metadata: bool = False
    ) -> Dict[str, Any]:
        metadata = parse_magnet_metadata(
            url,
            fetch_info=fetch_metadata,
        )
        if not metadata:
            return {}
        return {
            "url": str(url).strip(),
            "name": metadata.get("display_name") or metadata["info_hash"],
            "size": safe_int(metadata.get("size")),
            "hash": metadata["info_hash"],
            "metadata": metadata,
        }

    @staticmethod
    def _format_task(task: Dict[str, Any]) -> Dict[str, Any]:
        status = safe_int(task.get("status"))
        state = _STATE_BY_STATUS.get(status, "processing")
        native_id = str(
            task.get("taskId") or task.get("task_id") or task.get("id") or ""
        ).strip()
        percent = float(
            task.get("percent") or task.get("progress")
            or task.get("percentDone") or 0
        )
        if 0 < percent <= 1:
            percent *= 100
        add_time = safe_int(
            task.get("createTime") or task.get("create_time")
            or task.get("addTime") or task.get("add_time")
        )
        if add_time > 10_000_000_000:
            add_time //= 1000
        return {
            "id": native_id,
            "native_id": native_id,
            "name": str(
                task.get("fileName") or task.get("file_name")
                or task.get("name") or task.get("title") or "未命名任务"
            ),
            "size": safe_int(task.get("size") or task.get("fileSize")),
            "state": state,
            "completed": state == "completed",
            "failed": state == "failed",
            "status_text": _STATUS_TEXT.get(status, "处理中"),
            "percent": max(0.0, min(percent, 100.0)),
            "add_time": add_time,
        }

    @classmethod
    def _task_rows(cls, response: Dict[str, Any]) -> List[Dict[str, Any]]:
        data = response.get("data") or response.get("result") or {}
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        if not isinstance(data, dict):
            return []
        for key in ("list", "taskList", "task_list", "tasks", "items"):
            value = data.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        return []

    def _load_tasks(self, force: bool = False) -> List[Dict[str, Any]]:
        with self._lock:
            if (not force and self._updated_at
                    and time.time() - self._updated_at < self.CACHE_TTL):
                return [dict(task) for task in self._tasks]
        try:
            if not self.client.access_token:
                raise RuntimeError("光鸭账号未登录")
            response = self.cloud_task_list()
            if not self.client.is_success(response):
                raise RuntimeError(
                    response.get("msg") or response.get("message")
                    or "读取光鸭离线任务失败"
                )
            tasks = [
                self._format_task(item) for item in self._task_rows(response)
            ]
            with self._lock:
                self._tasks = tasks
                self._updated_at = time.time()
                self._refresh_ok = True
                return [dict(task) for task in tasks]
        except Exception as error:
            logger.warning(f"读取光鸭离线任务失败，继续使用缓存：{error}")
            with self._lock:
                self._refresh_ok = False
                return [dict(task) for task in self._tasks]

    def get_offline_tasks(self, force: bool = False) -> List[Dict[str, Any]]:
        """读取离线任务列表；60 秒缓存，接口异常时降级返回缓存值。"""
        return self._load_tasks(force=force)

    def add_offline_download(self, url: str, save_path: str, **kwargs: Any) -> bool:
        if not self.is_offline_url(url):
            return False
        if not self.client.access_token:
            logger.error("添加光鸭离线下载失败：账号未登录")
            return False
        resolved = self.resolve_cloud_url(url)
        if not self.client.is_success(resolved):
            logger.error(
                "添加光鸭离线下载失败："
                f"{resolved.get('msg') or resolved.get('message') or resolved.get('error') or '链接解析失败'}"
            )
            return False
        lookup = self._files.resolve_directory(save_path, create=True)
        if not lookup.checked or lookup.directory_id is None:
            logger.error(f"添加光鸭离线下载失败：无法获取或创建目标目录 {save_path}")
            return False
        created = self.create_cloud_task(url, lookup.directory_id)
        if self.client.is_success(created):
            return True
        logger.error(
            "添加光鸭离线下载失败："
            f"{created.get('msg') or created.get('message') or created.get('error') or '任务创建失败'}"
        )
        return False
