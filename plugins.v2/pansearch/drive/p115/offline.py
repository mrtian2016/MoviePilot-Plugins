"""115 离线任务与 ED2K 批量提交。"""

import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import unquote

from app.log import logger

from ...core import OwnerDelegator
from ...utils import parse_magnet_metadata

try:
    from p115client import check_response
    from p115client.tool.clouddownload import clouddownload_iter
except ImportError:
    pass


class OfflineDownloadService(OwnerDelegator):
    """管理115离线任务及ED2K下载。"""

    _OFFLINE_TASK_CACHE_LIMIT = 500
    ED2K_FILE_LINK_RE = re.compile(
        r"^ed2k://\|file\|([^|]+)\|(\d+)\|([0-9A-Fa-f]{32})(?:\|(?:h|p)=[^|]+)*\|/$",
        re.IGNORECASE,
    )

    @staticmethod
    def _normalize_hash(value: Any) -> str:
        return re.sub(r"[^0-9A-Fa-f]", "", str(value or "")).upper()

    @staticmethod
    def _format_offline_task(task: Dict[str, Any]) -> Dict[str, Any]:
        status = int(task.get("status", -1) or 0)
        move = int(task.get("move", 0) or 0)
        failed = move == -1
        state = (
            "failed" if failed
            else {0: "queued", 1: "running", 2: "completed", 3: "retrying"}.get(
                status, "processing"
            )
        )
        size = int(task.get("size", 0) or 0)
        percent = float(task.get("percentDone", task.get("percent", 0)) or 0)
        if 0 < percent <= 1:
            percent *= 100
        return {
            "id": str(task.get("info_hash") or "").upper(),
            "name": str(task.get("name") or "未命名任务"),
            "size": size,
            "state": state,
            "completed": state == "completed",
            "failed": failed,
            "status_text": (
                "下载失败" if failed
                else {0: "等待下载", 1: "下载中", 2: "已完成", 3: "重试中"}.get(
                    status, "处理中"
                )
            ),
            "percent": max(0.0, min(percent, 100.0)),
            "add_time": int(task.get("add_time", 0) or 0),
        }

    def get_offline_tasks(self, force: bool = False) -> List[Dict[str, Any]]:
        """读取离线任务列表；所有调用方共享同一份 10 分钟缓存。"""
        if not self.client:
            return []
        now = time.time()
        with self._offline_task_lock:
            if (not force and
                    self._offline_task_cache_time
                    and now - self._offline_task_cache_time < self.OFFLINE_TASK_CACHE_TTL
            ):
                return [dict(task) for task in self._offline_task_cache]
            while self._offline_task_refreshing:
                self._offline_task_condition.wait(timeout=30)
                if self._offline_task_cache_time > 0:
                    return [dict(task) for task in self._offline_task_cache]
            self._offline_task_refreshing = True
            refresh_revision = self._offline_task_cache_revision

        try:
            rows = self.rate_limiter.call(
                lambda: list(clouddownload_iter(
                    self.client,
                    cooldown=2,
                    type="web",
                    **self._ios_request_kwargs(app=False),
                ))
            )
            tasks = [
                self._format_offline_task(task)
                for task in rows
            ]
            tasks = tasks[:self._OFFLINE_TASK_CACHE_LIMIT]
        except Exception as error:
            with self._offline_task_lock:
                self._offline_task_refresh_ok = False
                self._offline_task_refreshing = False
                cached = [dict(task) for task in self._offline_task_cache]
                self._offline_task_condition.notify_all()
            logger.warning(f"读取 115 离线任务失败，继续使用缓存：{error}")
            return cached

        with self._offline_task_lock:
            current_status = {
                task["id"]: task["state"]
                for task in tasks
                if task["id"]
            }
            status_changes: Dict[Tuple[int, int], int] = {}
            changed_names: List[str] = []
            for task in tasks:
                task_id = task["id"]
                previous = self._offline_task_status.get(task_id)
                if previous is not None and previous != task["state"]:
                    transition = (previous, task["state"])
                    status_changes[transition] = status_changes.get(transition, 0) + 1
                    changed_names.append(str(task.get("name") or task_id))
            for (previous, current), count in status_changes.items():
                logger.debug(
                    f"115 离线任务状态批量更新："
                    f"{self._format_offline_status(previous)} -> "
                    f"{self._format_offline_status(current)} {count} 个"
                )
            if changed_names:
                logger.debug(f"115 离线任务状态变化文件：{', '.join(changed_names)}")
            if refresh_revision == self._offline_task_cache_revision:
                self._offline_task_status = current_status
                self._offline_task_cache = tasks
                self._offline_task_cache_time = time.time()
                self._offline_task_refresh_ok = True
            result = [dict(task) for task in self._offline_task_cache]
            self._offline_task_refreshing = False
            self._offline_task_condition.notify_all()
        return result

    @staticmethod
    def _format_offline_status(state: str) -> str:
        return {
            "queued": "等待下载",
            "running": "下载中",
            "completed": "已完成",
            "retrying": "重试中",
            "failed": "下载失败",
        }.get(state, "处理中")

    def get_offline_task_list_snapshot(self, force: bool = False) -> Dict[str, Any]:
        """读取供后处理共享的完整任务列表快照，不请求离线额度。"""
        tasks = self.get_offline_tasks(force=force)
        with self._offline_task_lock:
            updated_at = self._offline_task_cache_time
            refresh_ok = self._offline_task_refresh_ok
        return {
            "tasks": tasks,
            "updated_at": updated_at,
            "cache_ttl": self.OFFLINE_TASK_CACHE_TTL,
            "refresh_ok": refresh_ok,
        }

    def get_offline_tasks_snapshot(self, force: bool = False) -> Dict[str, Any]:
        """读取前端展示所需的任务列表和离线额度。"""
        snapshot = self.get_offline_task_list_snapshot(force=force)
        snapshot["quota"] = self.get_offline_quota(force=force)
        return snapshot

    def get_offline_quota(self, force: bool = False) -> Dict[str, Any]:
        """读取115开放接口返回的离线下载额度。"""
        if not self.client:
            return {}
        now = time.time()
        with self._offline_task_lock:
            if (
                    not force and self._offline_quota_cache_time
                    and now - self._offline_quota_cache_time < self.OFFLINE_TASK_CACHE_TTL
            ):
                return dict(self._offline_quota_cache)
            while self._offline_quota_refreshing:
                self._offline_task_condition.wait(timeout=30)
                if self._offline_quota_cache_time > 0:
                    return dict(self._offline_quota_cache)
            self._offline_quota_refreshing = True
        try:
            response = self._rate_limited_call(
                self.client.clouddownload_quota_info_open,
                **self._ios_request_kwargs(app=False)
            )
            check_response(response)
            data = response.get("data") or {}
            quota = {
                "total": int(data.get("count") or 0),
                "used": int(data.get("used") or 0),
                "remaining": int(data.get("surplus") or 0),
                "max_size_gb": int(data.get("max_size") or 0),
            }
        except Exception as error:
            logger.debug(f"读取115离线下载额度失败：{error}")
            quota = None
        with self._offline_task_lock:
            if quota is not None:
                self._offline_quota_cache = quota
                self._offline_quota_cache_time = time.time()
            self._offline_quota_refreshing = False
            result = dict(self._offline_quota_cache)
            self._offline_task_condition.notify_all()
        return result

    def delete_offline_task(self, task_id: str, delete_source_file: bool = False) -> bool:
        """删除单个115离线任务，默认保留已下载的源文件。"""
        normalized_hash = str(task_id or "").strip().lower()
        if not normalized_hash:
            raise ValueError("离线任务 info_hash 不能为空")
        response = self._rate_limited_call(
            self.client.clouddownload_task_del,
            {
                "hash[0]": normalized_hash,
                "flag": 1 if delete_source_file else 0,
            },
            **self._ios_request_kwargs(app=False),
        )
        check_response(response)
        with self._offline_task_lock:
            self._offline_task_cache = [
                task for task in self._offline_task_cache
                if str(task.get("id") or "").strip().lower() != normalized_hash
            ]
            self._offline_task_status.pop(normalized_hash.upper(), None)
            self._offline_task_cache_time = time.time()
            self._offline_task_cache_revision += 1
        logger.info(f"🗑️ 已删除115离线任务：{normalized_hash}")
        return True

    def delete_offline_tasks(
            self, task_ids: List[str], delete_source_file: bool = False
    ) -> int:
        """一次请求批量删除115离线任务，默认保留源文件。"""
        hashes = list(dict.fromkeys(
            str(value or "").strip().lower() for value in task_ids
            if str(value or "").strip()
        ))
        if not hashes:
            raise ValueError("请选择需要删除的离线任务")
        payload = {f"hash[{index}]": value for index, value in enumerate(hashes)}
        payload["flag"] = 1 if delete_source_file else 0
        response = self._rate_limited_call(
            self.client.clouddownload_task_del,
            payload, **self._ios_request_kwargs(app=False)
        )
        check_response(response)
        hash_set = {value.upper() for value in hashes}
        with self._offline_task_lock:
            self._offline_task_cache = [
                task for task in self._offline_task_cache
                if str(task.get("id") or "").upper() not in hash_set
            ]
            for info_hash in hash_set:
                self._offline_task_status.pop(info_hash, None)
            self._offline_task_cache_time = time.time()
            self._offline_task_cache_revision += 1
        logger.info(f"已批量删除115离线任务：{len(hashes)} 个")
        return len(hashes)

    def restart_offline_task(self, task_id: str) -> bool:
        """手动重启失败的115离线任务。"""
        normalized_hash = str(task_id or "").strip().lower()
        if not normalized_hash:
            raise ValueError("离线任务 info_hash 不能为空")
        response = self._rate_limited_call(
            self.client.clouddownload_task_restart,
            normalized_hash,
            **self._ios_request_kwargs(app=False),
        )
        check_response(response)
        with self._offline_task_lock:
            self._offline_task_cache_time = 0
            self._offline_task_status.pop(normalized_hash.upper(), None)
            self._offline_task_cache_revision += 1
        logger.info(f"115 离线任务已重启：{normalized_hash.upper()}")
        return True

    def _offline_task_exists(self, ed2k_hash: str) -> bool:
        """按 115 离线任务 info_hash 判断 ED2K 是否已提交。"""
        target_hash = self._normalize_hash(ed2k_hash)
        return any(
            self._normalize_hash(task.get("id")) == target_hash
            for task in self.get_offline_tasks()
        )

    def add_offline_download(self, offline_url: str, save_path: str, target_name: str = None) -> bool:
        """将 ED2K 或 Magnet 资源提交到 115 离线下载。"""
        success_hashes, _ = self.add_offline_downloads_batch(
            [{"url": offline_url, "target_name": target_name}],
            save_path=save_path,
            batch_size=1,
        )
        file_info = (
            self.parse_ed2k_link(offline_url)
            if self.is_ed2k_url(offline_url)
            else self.parse_magnet_link(offline_url)
        )
        return bool(file_info and file_info["hash"] in success_hashes)

    def add_offline_downloads_batch(
            self,
            items: List[Dict[str, Any]],
            save_path: str,
            batch_size: int = 20,
            batch_interval: float = 3.0,
    ) -> Tuple[List[str], List[str]]:
        """批量添加 ED2K/Magnet 离线任务，同一批只调用一次115添加接口。"""
        if not self.client:
            logger.error("添加 115 离线下载失败：客户端未初始化")
            return [], []
        if not self._login_checked and not self.check_login():
            logger.error("添加 115 离线下载失败：登录状态无效")
            return [], []
        if not self.is_vip:
            logger.warning("添加 115 离线下载失败：当前账号不是会员")
            return [], []

        prepared: List[Dict[str, Any]] = []
        invalid_hashes: List[str] = []
        for item in items or []:
            raw_url = str(item.get("url") or "")
            is_ed2k = self.is_ed2k_url(raw_url)
            file_info = (
                self.parse_ed2k_link(raw_url)
                if is_ed2k else self.parse_magnet_link(raw_url)
            )
            if not file_info:
                logger.error("添加 115 离线下载失败：ED2K/Magnet 链接格式无效")
                continue
            target_name = str(item.get("target_name") or "").strip()
            display_name = file_info["name"]
            download_url = file_info["url"]
            if target_name and is_ed2k:
                target_name = Path(target_name).name
                if Path(target_name).suffix.lower() != Path(display_name).suffix.lower():
                    logger.error(
                        f"ED2K 平台文件名扩展名不一致：{display_name} -> {target_name}"
                    )
                    invalid_hashes.append(file_info["hash"])
                    continue
            prepared.append({
                **file_info,
                "download_url": download_url,
                "display_name": display_name,
            })

        if not prepared:
            return [], invalid_hashes

        existing_hashes = {
            self._normalize_hash(task.get("id"))
            for task in self.get_offline_tasks()
            if task.get("id")
        }
        success_hashes = [
            item["hash"] for item in prepared if item["hash"] in existing_hashes
        ]
        pending_items = [
            item for item in prepared if item["hash"] not in existing_hashes
        ]
        if success_hashes:
            logger.debug(f"115 离线任务已存在，批量跳过 {len(success_hashes)} 个")
        if not pending_items:
            return success_hashes, invalid_hashes

        parent_id = self.get_pid_by_path(save_path, mkdir=True)
        if parent_id == -1:
            logger.error(f"添加 115 离线下载失败：无法获取或创建目标目录 {save_path}")
            return success_hashes, invalid_hashes + [item["hash"] for item in pending_items]

        batch_size = max(1, min(int(batch_size or 20), 20))
        total_batches = (len(pending_items) + batch_size - 1) // batch_size
        failed_hashes = list(invalid_hashes)
        synthetic_tasks: List[Dict[str, Any]] = []
        logger.debug(
            f"115 批量离线提交：共 {len(pending_items)} 个任务，"
            f"分 {total_batches} 批（每批最多 {batch_size} 个）"
        )
        for offset in range(0, len(pending_items), batch_size):
            batch = pending_items[offset:offset + batch_size]
            payload = {
                f"url[{index}]": item["download_url"]
                for index, item in enumerate(batch)
            }
            payload["wp_path_id"] = parent_id
            try:
                resp = self._rate_limited_call(
                    self.client.clouddownload_task_add_urls,
                    payload,
                    **self._ios_request_kwargs(app=False),
                )
            except Exception as error:
                logger.error(f"115 批量离线提交异常：{error}")
                failed_hashes.extend(item["hash"] for item in batch)
                continue
            if not resp.get("state"):
                error_msg = resp.get("error") or resp.get("message") or "未知错误"
                error_code = resp.get("errno", resp.get("errcode", 0))
                logger.error(
                    f"115 批量离线提交失败：{error_msg} (错误码: {error_code})"
                )
                failed_hashes.extend(item["hash"] for item in batch)
                continue

            result = (resp.get("data") or {}).get("result") or []
            result_by_hash = {
                self._normalize_hash(result_item.get("info_hash")): result_item
                for result_item in result
                if result_item.get("info_hash")
            }
            for item in batch:
                info_hash = item["hash"]
                task_data = result_by_hash.get(info_hash, {})
                if task_data.get("state") is False or task_data.get("error"):
                    failed_hashes.append(info_hash)
                    continue
                success_hashes.append(info_hash)
                synthetic_tasks.append(self._format_offline_task({
                    **task_data,
                    "info_hash": info_hash,
                    "name": item["display_name"],
                    "size": item["size"],
                    "status": task_data.get("status", 0),
                    "percentDone": task_data.get("percentDone", 0),
                    "add_time": task_data.get("add_time", int(time.time())),
                }))
            if offset + batch_size < len(pending_items):
                time.sleep(max(0.0, float(batch_interval)))

        if synthetic_tasks:
            submitted_hashes = {task["id"] for task in synthetic_tasks}
            with self._offline_task_lock:
                self._offline_task_cache = [
                    task for task in self._offline_task_cache
                    if self._normalize_hash(task.get("id")) not in submitted_hashes
                ]
                self._offline_task_cache[0:0] = synthetic_tasks
                for task in synthetic_tasks:
                    self._offline_task_status[task["id"]] = task["state"]
                self._offline_task_cache_time = time.time()
                self._offline_task_cache_revision += 1
        logger.debug(
            f"115 批量离线提交完成：成功 {len(success_hashes)} 个，"
            f"失败 {len(failed_hashes)} 个"
        )
        return success_hashes, failed_hashes

    @classmethod
    def parse_ed2k_link(cls, url: str) -> Optional[Dict[str, Any]]:
        """解析 ED2K 单文件链接，生成可复用现有文件匹配流程的文件信息。"""
        if not isinstance(url, str):
            return None
        normalized = url.replace("｜", "|").translate(
            dict.fromkeys((0x200B, 0x200C, 0x200D, 0x200E, 0x200F, 0xFEFF))
        ).strip()
        match = cls.ED2K_FILE_LINK_RE.fullmatch(normalized)
        if not match:
            return None
        return {
            "url": normalized,
            "name": unquote(match.group(1)),
            "size": int(match.group(2)),
            "hash": match.group(3).upper(),
        }

    @staticmethod
    def is_ed2k_url(url: str) -> bool:
        return isinstance(url, str) and url.lstrip().lower().startswith("ed2k://")

    @staticmethod
    def is_magnet_url(url: str) -> bool:
        return isinstance(url, str) and url.lstrip().lower().startswith("magnet:?")

    def parse_magnet_link(
            self, url: str, fetch_metadata: bool = False
    ) -> Optional[Dict[str, Any]]:
        metadata = parse_magnet_metadata(
            url,
            fetch_info=fetch_metadata,
        )
        if not metadata:
            return None
        return {
            "url": str(url).strip(),
            "name": metadata.get("display_name") or metadata["info_hash"],
            "size": int(metadata.get("size") or 0),
            "hash": metadata["info_hash"],
            "metadata": metadata,
        }

    @classmethod
    def is_offline_url(cls, url: str) -> bool:
        return cls.is_ed2k_url(url) or cls.is_magnet_url(url)
