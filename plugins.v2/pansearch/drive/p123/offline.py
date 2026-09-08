"""123 网盘离线下载与任务管理能力。"""

from __future__ import annotations

import re
import time
from threading import RLock
from typing import Any, Dict, Optional, Set
from urllib.parse import unquote

from app.log import logger

from .client import is_success
from ..common import safe_int
from ...utils.magnet import parse_magnet_metadata

_ED2K_RE = re.compile(
    r"ed2k://\|file\|([^|]+)\|(\d+)\|([0-9A-Fa-f]{32})\|/?", re.I
)
_MAGNET_HASH_RE = re.compile(r"(?:^|[?&])xt=urn:btih:([0-9A-Fa-f]{40})(?:&|$)", re.I)


class P123OfflineService:
    """使用 123 网页端接口提交离线任务，并提供统一任务快照。"""

    CACHE_TTL = 60

    def __init__(self, client: Any, files: Any):
        self.client = client
        self._files = files
        self._lock = RLock()
        self._tasks: list[Dict[str, Any]] = []
        self._updated_at = 0.0
        self._refresh_ok = False
        self._native_task_ids: Dict[str, str] = {}

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
            "hash": str(metadata["info_hash"]).upper(),
            "metadata": metadata,
        }

    @staticmethod
    def _task_list(response: Dict[str, Any]) -> list:
        data = response.get("data") or {}
        if isinstance(data, list):
            return data
        if not isinstance(data, dict):
            return []
        for key in ("list", "taskList", "task_list", "tasks", "items", "infoList"):
            value = data.get(key)
            if isinstance(value, list):
                return value
        return []

    @staticmethod
    def _task_hash(task: Dict[str, Any]) -> str:
        value = str(
            task.get("info_hash") or task.get("infoHash")
            or task.get("file_hash") or task.get("hash") or ""
        ).strip()
        if value:
            return value.upper()
        url = str(task.get("url") or task.get("download_url") or "").strip()
        ed2k = _ED2K_RE.fullmatch(url)
        if ed2k:
            return ed2k.group(3).upper()
        magnet = _MAGNET_HASH_RE.search(url)
        return magnet.group(1).upper() if magnet else ""

    def _format_task(self, task: Dict[str, Any]) -> Dict[str, Any]:
        status = safe_int(task.get("status"))
        state = {
            0: "running",
            1: "failed",
            2: "completed",
            3: "retrying",
            4: "queued",
        }.get(status, "processing")
        native_id = str(
            task.get("task_id") or task.get("taskId") or task.get("id") or ""
        ).strip()
        task_id = self._task_hash(task) or native_id
        if task_id and native_id:
            self._native_task_ids[task_id.upper()] = native_id
        percent = float(
            task.get("percent") or task.get("progress")
            or task.get("percentDone") or 0
        )
        if 0 < percent <= 1:
            percent *= 100
        add_time = safe_int(
            task.get("create_time") or task.get("createTime")
            or task.get("add_time") or task.get("createdAt")
        )
        if add_time > 10_000_000_000:
            add_time //= 1000
        return {
            "id": task_id,
            "native_id": native_id,
            "name": str(
                task.get("file_name") or task.get("fileName")
                or task.get("name") or task.get("title") or "未命名任务"
            ),
            "size": safe_int(task.get("size") or task.get("fileSize")),
            "state": state,
            "completed": state == "completed",
            "failed": state == "failed",
            "status_text": {
                0: "下载中",
                1: "下载失败",
                2: "已完成",
                3: "重试中",
                4: "等待下载",
            }.get(status, "处理中"),
            "percent": max(0.0, min(percent, 100.0)),
            "add_time": add_time,
        }

    def _load_tasks(self, force: bool = False) -> list[Dict[str, Any]]:
        with self._lock:
            if not force and self._updated_at and time.time() - self._updated_at < self.CACHE_TTL:
                return [dict(task) for task in self._tasks]
        try:
            response = self.client.offline_task_list({
                "current_page": 1,
                "page_size": 100,
                "status_arr": [0, 1, 2, 3, 4],
            })
            if not is_success(response):
                raise RuntimeError(response.get("message") or "读取123离线任务失败")
            self._native_task_ids = {}
            tasks = [
                self._format_task(item)
                for item in self._task_list(response)
                if isinstance(item, dict)
            ]
            with self._lock:
                self._tasks = tasks
                self._updated_at = time.time()
                self._refresh_ok = True
                return [dict(task) for task in tasks]
        except Exception as error:
            logger.warning(f"读取123离线任务失败，继续使用缓存：{error}")
            with self._lock:
                self._refresh_ok = False
                return [dict(task) for task in self._tasks]

    def get_offline_task_list_snapshot(
            self,
            force: bool = False,
            task_ids: Optional[Set[str]] = None,
    ) -> Dict[str, Any]:
        tasks = self._load_tasks(force=force)
        normalized_ids = {
            str(value or "").strip().upper()
            for value in (task_ids or set())
            if str(value or "").strip()
        }
        if normalized_ids:
            tasks = [
                task for task in tasks
                if str(task.get("id") or "").strip().upper() in normalized_ids
            ]
        with self._lock:
            return {
                "tasks": tasks,
                "updated_at": self._updated_at,
                "cache_ttl": self.CACHE_TTL,
                "refresh_ok": self._refresh_ok,
            }

    def get_offline_tasks_snapshot(self, force: bool = False) -> Dict[str, Any]:
        snapshot = self.get_offline_task_list_snapshot(force=force)
        snapshot["quota"] = {}
        return snapshot

    def _native_id(self, task_id: str) -> str:
        normalized = str(task_id or "").strip()
        if not normalized:
            raise ValueError("离线任务ID不能为空")
        with self._lock:
            return self._native_task_ids.get(normalized.upper(), normalized)

    def restart_offline_task(self, task_id: str) -> bool:
        native_id = self._native_id(task_id)
        response = self.client.offline_task_abort({
            "task_ids": [int(native_id) if native_id.isdigit() else native_id],
            "is_abort": False,
            "all": False,
        })
        if not is_success(response):
            raise RuntimeError(response.get("message") or "重试123离线任务失败")
        with self._lock:
            self._updated_at = 0
        return True

    def delete_offline_task(
            self, task_id: str, delete_source_file: bool = False
    ) -> bool:
        native_id = self._native_id(task_id)
        response = self.client.offline_task_delete(
            int(native_id) if native_id.isdigit() else native_id
        )
        if not is_success(response):
            raise RuntimeError(response.get("message") or "删除123离线任务失败")
        with self._lock:
            normalized = str(task_id).upper()
            self._tasks = [
                task for task in self._tasks
                if str(task.get("id") or "").upper() != normalized
            ]
            self._native_task_ids.pop(normalized, None)
            self._updated_at = time.time()
        return True

    def delete_offline_tasks(
            self, task_ids: list[str], delete_source_file: bool = False
    ) -> int:
        normalized = list(dict.fromkeys(
            str(value or "").strip() for value in task_ids
            if str(value or "").strip()
        ))
        if not normalized:
            raise ValueError("请选择需要删除的离线任务")
        native_ids = [self._native_id(value) for value in normalized]
        response = self.client.offline_task_delete([
            int(value) if value.isdigit() else value for value in native_ids
        ])
        if not is_success(response):
            raise RuntimeError(response.get("message") or "批量删除123离线任务失败")
        removed = {value.upper() for value in normalized}
        with self._lock:
            self._tasks = [
                task for task in self._tasks
                if str(task.get("id") or "").upper() not in removed
            ]
            for value in removed:
                self._native_task_ids.pop(value, None)
            self._updated_at = time.time()
        return len(normalized)

    def add_offline_download(self, url: str, save_path: str, **kwargs: Any) -> bool:
        if not self.is_offline_url(url):
            return False
        lookup = self._files.resolve_directory(save_path, create=True)
        if not lookup.checked or lookup.directory_id is None:
            logger.error(f"添加123离线下载失败：无法获取或创建目标目录 {save_path}")
            return False
        try:
            response = self.client.offline_add(
                str(url).strip(), upload_dir=int(lookup.directory_id or 0)
            )
            if not is_success(response):
                logger.error(
                    f"添加123离线下载失败：{response.get('message') or '任务创建失败'}"
                )
                return False
            with self._lock:
                self._updated_at = 0
            return True
        except Exception as error:
            logger.error(f"添加123离线下载异常：{error}")
            return False
