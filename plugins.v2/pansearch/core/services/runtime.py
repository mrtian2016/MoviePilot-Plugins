"""同步任务运行态、停止控制与网盘文件终态后处理监控。"""

import datetime
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Event as ThreadEvent, Lock, Thread
from typing import Any, Dict, List, Optional, Set, Tuple

import pytz
from app.core.config import global_vars, settings
from app.log import logger
from app.schemas.types import MediaType
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from ...core import CloudDriveCapability
from ...core import OwnerDelegator
from ...core.media import media_identity

sync_lock = Lock()


class SyncRuntimeService(OwnerDelegator):
    """管理同步任务运行态及离线任务生命周期。"""

    def _runtime_revision_value(self) -> int:
        with self._runtime_revision_lock:
            return int(self._runtime_revision)

    def _mark_runtime_changed(self) -> None:
        """递增运行态版本，供 SSE 合并并推送状态变化。"""
        with self._runtime_revision_lock:
            self._runtime_revision += 1

    def _runtime_snapshot(self) -> Dict[str, Any]:
        with self._runtime_revision_lock:
            revision = int(self._runtime_revision)
            history_revision = int(self._history_revision)
        return {
            "status": self._sync_status,
            "task": self._sync_task_text,
            "progress": self._sync_progress,
            "context": dict(self._sync_context),
            "tasks": self._serialize_runtime_tasks(),
            "offline_supported": bool(
                self._cloud_drive
                and self._cloud_drive.supports(CloudDriveCapability.OFFLINE_TASKS)
            ),
            "revision": revision,
            "history_revision": history_revision,
        }

    def _mark_history_changed(self) -> None:
        with self._runtime_revision_lock:
            self._history_revision += 1
            self._runtime_revision += 1

    def _current_task_context(self) -> Tuple[str, Optional[ThreadEvent]]:
        """返回当前订阅线程的任务标识与停止事件。"""
        if self._task_local is None:
            return "", None
        return (
            str(getattr(self._task_local, "task_id", "") or ""),
            getattr(self._task_local, "stop_event", None),
        )

    def _stop_requested(self) -> bool:
        if self._stop_event and self._stop_event.is_set():
            return True
        task_event = (
            getattr(self._task_local, "stop_event", None)
            if self._task_local is not None
            else None
        )
        return bool(task_event and task_event.is_set())

    def _sync_task_id(self, subscribe: Any) -> str:
        if bool(getattr(subscribe, "_transient_target", False)):
            return f"media:{self._sync_handler.subscription_budget_key(subscribe)}"
        return f"subscribe:{getattr(subscribe, 'id', '')}"

    @staticmethod
    def _sync_media_key(subscribe: Any) -> Tuple[str, str, int]:
        media_type = str(getattr(subscribe, "type", "") or "")
        source, source_id = media_identity(subscribe)
        media_id = (
            f"{source}:{source_id}"
            if source and source_id
            else str(getattr(subscribe, "name", "") or "")
        )
        season = int(getattr(subscribe, "season", 1) or 1) if media_type == MediaType.TV.value else 0
        return media_type, media_id, season

    def _register_sync_tasks(self, subscribes: List[Any]) -> None:
        queued_at = time.time()
        tasks = {}
        for subscribe in subscribes:
            is_tv = getattr(subscribe, "type", "") == MediaType.TV.value
            preparation = getattr(
                subscribe, "_pansearch_preparation", {}
            ) or {}
            task_id = self._sync_task_id(subscribe)
            tasks[task_id] = {
                "id": task_id,
                "subscribe_id": getattr(subscribe, "id", None),
                "sub_key": (
                    self._sync_handler.subscription_budget_key(subscribe)
                    if bool(getattr(subscribe, "_transient_target", False))
                    else ""
                ),
                "title": str(getattr(subscribe, "name", "") or "未命名订阅"),
                "media_type": "电视剧" if is_tv else "电影",
                "year": getattr(subscribe, "year", None) or "",
                "season": int(getattr(subscribe, "season", 1) or 1) if is_tv else None,
                "target_episodes": (
                    preparation.get("aired_target_episodes", []) if is_tv else []
                ),
                "status": "queued",
                "phase": "等待调度",
                "progress": 0,
                "transferred": 0,
                "message": "",
                "queued_at": queued_at,
                "started_at": None,
                "finished_at": None,
                "stop_event": ThreadEvent(),
            }
        with self._sync_tasks_lock:
            retained = {
                task_id: task
                for task_id, task in self._sync_tasks.items()
                if task.get("status") == "postprocessing"
                   or (
                           task.get("task_kind") == "pt_upgrade"
                           and task.get("status") in {"queued", "running", "stopping"}
                   )
            }
            postprocess_fields = {
                "pending_count",
                "current_file",
                "postprocess_active",
                "postprocess_detail",
                "postprocess_step",
                "postprocess_step_index",
                "postprocess_step_total",
                "postprocess_steps",
                "postprocess_file_index",
                "postprocess_file_total",
                "postprocess_progress",
            }
            for task_id, task in tasks.items():
                previous = retained.get(task_id)
                if not previous or previous.get("status") != "postprocessing":
                    continue
                task.update({
                    key: previous[key]
                    for key in postprocess_fields
                    if key in previous
                })
            retained.update(tasks)
            self._sync_tasks = retained
        self._mark_runtime_changed()

    def _update_sync_task(self, task_id: str, **values: Any) -> None:
        changed = False
        with self._sync_tasks_lock:
            task = self._sync_tasks.get(task_id)
            if task and any(task.get(key) != value for key, value in values.items()):
                task.update(values)
                changed = True
        if changed:
            self._mark_runtime_changed()

    def _serialize_sync_tasks(self) -> List[Dict[str, Any]]:
        now = time.time()
        sync_handler = self._sync_handler
        disabled_postprocess_steps = set()
        if not (
                sync_handler
                and bool(getattr(sync_handler, "_strm_generate_enabled", False))
                and getattr(sync_handler, "_strm_generator", None)
                and getattr(sync_handler, "_local_resource_path", None)
        ):
            disabled_postprocess_steps.add("strm")
        if not (
                sync_handler
                and getattr(sync_handler, "_metadata_scraper", None)
                and getattr(sync_handler, "_local_resource_path", None)
                and (
                        bool(getattr(sync_handler, "_nfo_scrape_enabled", False))
                        or bool(getattr(sync_handler, "_image_scrape_enabled", False))
                )
        ):
            disabled_postprocess_steps.add("metadata")
        with self._sync_tasks_lock:
            tasks = []
            for task in self._sync_tasks.values():
                if task.get("status") not in {
                    "queued", "running", "stopping", "postprocessing"
                }:
                    continue
                serialized = {
                    key: value for key, value in task.items() if key != "stop_event"
                }
                serialized_steps = [
                    step for step in serialized.get("postprocess_steps") or []
                    if isinstance(step, dict)
                                     and str(step.get("key") or "")
                                     not in disabled_postprocess_steps
                ]
                serialized["postprocess_steps"] = serialized_steps
                if str(serialized.get("postprocess_step") or "") in (
                        disabled_postprocess_steps
                ):
                    serialized["postprocess_step"] = ""
                    serialized["postprocess_step_index"] = 0
                serialized["postprocess_step_total"] = len(serialized_steps)
                queued_at = float(task.get("queued_at") or now)
                started_at = float(task.get("started_at") or 0)
                serialized["queue_seconds"] = round(
                    max(0.0, (started_at or now) - queued_at), 2
                )
                serialized["elapsed_seconds"] = round(
                    max(0.0, now - started_at), 2
                ) if started_at else 0
                tasks.append(serialized)
            status_order = {
                "running": 0,
                "stopping": 1,
                "postprocessing": 2,
                "queued": 3,
            }
            tasks.sort(key=lambda task: (
                status_order.get(str(task.get("status") or ""), 9),
                -float(task.get("started_at") or task.get("queued_at") or 0),
            ))
            return tasks

    def _pending_finalize_items(self, subscribe: Any) -> List[Dict[str, Any]]:
        if not self._sync_handler:
            return []
        normalized_id = int(getattr(subscribe, "id", 0) or 0)
        sub_key = (
            self._sync_handler.subscription_budget_key(subscribe)
            if bool(getattr(subscribe, "_transient_target", False))
            else ""
        )
        return [
            item
            for item in self._sync_handler.get_pending_finalize_tasks()
            if (
                    normalized_id > 0
                    and int(item.get("subscribe_id") or 0) == normalized_id
            ) or (
                    bool(sub_key)
                    and str(item.get("sub_key") or "") == sub_key
            )
        ]

    def _pending_finalize_count(self, subscribe: Any) -> int:
        return len(self._pending_finalize_items(subscribe))

    def _postprocessing_text(
            self, items: List[Dict[str, Any]]
    ) -> Tuple[str, str]:
        """描述后处理当前等待点、目标文件和下一次检查时间。"""
        if not items:
            return "文件后处理已结束", ""
        stage_counts: Dict[str, int] = {}
        file_names = []
        next_check_at = 0.0
        for item in items:
            task_type = str(item.get("task_type") or "share").lower()
            if not item.get("download_completed_at") and not item.get("moved_at"):
                stage = {
                    "magnet": "等待 Magnet 下载完成",
                    "ed2k": "等待 115 离线下载完成",
                }.get(task_type, "等待网盘文件就绪")
            elif not item.get("moved_at"):
                stage = "整理、移动并重命名文件"
            else:
                stage = "完成字幕与本地媒体文件处理"
            stage_counts[stage] = stage_counts.get(stage, 0) + 1
            file_name = str(item.get("file_name") or "").strip()
            if file_name and file_name not in file_names:
                file_names.append(file_name)
            candidate_check = float(item.get("next_check_at") or 0)
            if candidate_check > 0:
                next_check_at = (
                    min(next_check_at, candidate_check)
                    if next_check_at else candidate_check
                )

        stage_text = "；".join(
            f"{stage} {count} 个" for stage, count in stage_counts.items()
        )
        actions = "完成文件整理与字幕处理"
        if bool(getattr(self._sync_handler, "_strm_generate_enabled", False)):
            actions += "并生成 STRM"
        phase = f"{stage_text}；就绪后将{actions}"

        visible_names = file_names[:2]
        file_text = "、".join(visible_names)
        if len(file_names) > len(visible_names):
            file_text += f" 等 {len(file_names)} 个文件"
        message_parts = [f"文件：{file_text}"] if file_text else []
        remaining = max(0, int(next_check_at - time.time() + 0.999))
        if remaining > 0:
            if remaining < 60:
                message_parts.append(f"约 {remaining} 秒后复查")
            else:
                message_parts.append(f"约 {(remaining + 59) // 60} 分钟后复查")
        return phase, "；".join(message_parts)

    @staticmethod
    def _idle_postprocess_state() -> Dict[str, Any]:
        """清理仅用于实时展示的后处理文件进度。"""
        return {
            "current_file": "",
            "postprocess_active": False,
            "postprocess_detail": "",
            "postprocess_step": "",
            "postprocess_step_index": 0,
            "postprocess_step_total": 0,
            "postprocess_steps": [],
            "postprocess_file_index": 0,
            "postprocess_file_total": 0,
            "postprocess_progress": 0,
        }

    def _refresh_postprocessing_sync_tasks(self) -> None:
        """按持久化后处理记录恢复并刷新订阅任务状态。"""
        pending_items = (
            self._sync_handler.get_pending_finalize_tasks()
            if self._sync_handler else []
        )
        groups: Dict[str, Dict[str, Any]] = {}
        for item in pending_items:
            subscribe_id = int(item.get("subscribe_id") or 0)
            sub_key = str(item.get("sub_key") or "").strip()
            task_id = (
                f"subscribe:{subscribe_id}"
                if subscribe_id > 0
                else f"media:{sub_key}" if sub_key else ""
            )
            if not task_id:
                continue
            media_data = item.get("mediainfo") or {}
            group = groups.setdefault(task_id, {
                "count": 0,
                "items": [],
                "subscribe_id": subscribe_id if subscribe_id > 0 else None,
                "sub_key": sub_key,
                "title": str(media_data.get("title") or "未命名订阅"),
                "year": media_data.get("year") or "",
                "season": item.get("season"),
                "queued_at": float(item.get("created_at") or time.time()),
                "task_kind": (
                    "pt_upgrade"
                    if str(item.get("share_url") or "").lower().startswith("pt://")
                    else "cloud_upgrade" if item.get("upgrade") else "subscribe"
                ),
            })
            group["count"] += 1
            group["items"].append(item)
            if str(item.get("share_url") or "").lower().startswith("pt://"):
                group["task_kind"] = "pt_upgrade"
            elif item.get("upgrade") and group["task_kind"] == "subscribe":
                group["task_kind"] = "cloud_upgrade"
            group["queued_at"] = min(
                float(group["queued_at"]),
                float(item.get("created_at") or group["queued_at"]),
            )

        now = time.time()
        changed = False
        with self._sync_tasks_lock:
            for task in self._sync_tasks.values():
                if task.get("status") != "postprocessing":
                    continue
                if str(task.get("id") or "") not in groups:
                    task.update({
                        "status": "completed",
                        "phase": "文件后处理已结束",
                        "progress": 100,
                        "pending_count": 0,
                        "finished_at": now,
                        **self._idle_postprocess_state(),
                    })
                    changed = True
            for task_id, group in groups.items():
                task = self._sync_tasks.get(task_id)
                if task and task.get("status") in {"queued", "running", "stopping"}:
                    continue
                pending_count = int(group["count"])
                phase, message = self._postprocessing_text(group["items"])
                values = {
                    "status": "postprocessing",
                    "task_kind": group["task_kind"],
                    "phase": phase,
                    "message": message,
                    "progress": 95,
                    "pending_count": pending_count,
                    "finished_at": None,
                }
                if not task:
                    values.update(self._idle_postprocess_state())
                elif not task.get("postprocess_active"):
                    values.update({
                        "postprocess_active": False,
                        "postprocess_detail": "",
                    })
                if task:
                    if any(task.get(key) != value for key, value in values.items()):
                        task.update(values)
                        changed = True
                    continue
                is_tv = bool(group.get("season"))
                self._sync_tasks[task_id] = {
                    "id": task_id,
                    "subscribe_id": group["subscribe_id"],
                    "sub_key": group["sub_key"],
                    "title": group["title"],
                    "media_type": "电视剧" if is_tv else "电影",
                    "year": group["year"],
                    "season": int(group["season"] or 1) if is_tv else None,
                    **values,
                    "transferred": 0,
                    "queued_at": group["queued_at"],
                    "started_at": group["queued_at"],
                    "stop_event": ThreadEvent(),
                }
                changed = True
        if changed:
            self._mark_runtime_changed()

    def _run_subscription_group(
            self,
            subscribes: List[Any],
            history_snapshot: List[dict],
            exclude_ids: set,
            manual_resources: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        local_history = list(history_snapshot)
        initial_history_count = len(local_history)
        transfer_details: List[Dict[str, Any]] = []
        transferred_count = 0
        direct_cloud_paths = self._direct_cloud_manual_resources(
            manual_resources
        )

        for subscribe in subscribes:
            task_id = self._sync_task_id(subscribe)
            with self._sync_tasks_lock:
                task = self._sync_tasks.get(task_id) or {}
                task_event = task.get("stop_event")
            if (
                    global_vars.is_system_stopped
                    or (self._stop_event and self._stop_event.is_set())
                    or (task_event and task_event.is_set())
            ):
                self._update_sync_task(
                    task_id, status="stopped", phase="已停止", progress=100
                )
                continue

            self._task_local.stop_event = task_event
            self._task_local.task_id = task_id
            manual_upgrade = bool(getattr(subscribe, "_manual_upgrade", False))
            transient_target = bool(getattr(subscribe, "_transient_target", False))
            target_episodes = getattr(subscribe, "_target_episodes", None)
            upgrade_task = manual_upgrade or self._is_cloud_upgrade_subscribe(subscribe)
            self._update_sync_task(
                task_id,
                status="running",
                phase="建立洗版基线" if upgrade_task else "检查缺失内容",
                task_kind="cloud_upgrade" if upgrade_task else "subscribe",
                progress=10,
                message="",
                started_at=time.time(),
            )
            try:
                if getattr(subscribe, "type", "") == MediaType.MOVIE.value:
                    task_count = self._sync_handler.process_movie_subscribe(
                        subscribe=subscribe,
                        history=local_history,
                        transfer_details=transfer_details,
                        transferred_count=0,
                        manual_resources=manual_resources,
                        manual_upgrade=manual_upgrade,
                        transient_target=transient_target,
                    )
                else:
                    task_count = self._sync_handler.process_tv_subscribe(
                        subscribe=subscribe,
                        history=local_history,
                        transfer_details=transfer_details,
                        transferred_count=0,
                        exclude_ids=exclude_ids,
                        manual_resources=manual_resources,
                        manual_upgrade=manual_upgrade,
                        target_episodes=target_episodes,
                        transient_target=transient_target,
                    )
                transferred_count += int(task_count or 0)
                stopped = bool(task_event and task_event.is_set())
                pending_items = self._pending_finalize_items(subscribe)
                pending_count = len(pending_items)
                postprocess_phase, postprocess_message = (
                    self._postprocessing_text(pending_items)
                )
                postprocess_state = (
                    {} if pending_count and not stopped
                    else self._idle_postprocess_state()
                )
                self._update_sync_task(
                    task_id,
                    status=(
                        "stopped" if stopped
                        else "postprocessing" if pending_count
                        else "completed"
                    ),
                    phase=(
                        "已停止" if stopped
                        else postprocess_phase
                        if pending_count
                        else (
                            f"已整理 {int(task_count or 0)} 个文件"
                            if direct_cloud_paths
                            else f"已转存 {int(task_count or 0)} 个文件"
                        )
                        if int(task_count or 0) > 0
                        else "无需转存"
                    ),
                    progress=95 if pending_count and not stopped else 100,
                    pending_count=pending_count,
                    message=postprocess_message if pending_count else "",
                    transferred=int(task_count or 0),
                    finished_at=None if pending_count and not stopped else time.time(),
                    **postprocess_state,
                )
            except Exception as error:
                logger.error(f"订阅 {getattr(subscribe, 'name', '')} 处理异常：{error}")
                self._update_sync_task(
                    task_id,
                    status="failed",
                    phase="处理失败",
                    progress=100,
                    message=str(error),
                    finished_at=time.time(),
                )
            finally:
                try:
                    del self._task_local.stop_event
                except AttributeError:
                    pass
                try:
                    del self._task_local.task_id
                except AttributeError:
                    pass

        return {
            "history": [
                record
                for record in local_history[initial_history_count:]
                if not record.get("skip_history")
            ],
            "transfer_details": transfer_details,
            "transferred": transferred_count,
        }

    def _set_sync_status(
            self,
            status: str,
            text: str,
            progress: int = None,
            context: Optional[Dict[str, Any]] = None,
    ) -> None:
        previous = (
            self._sync_status,
            self._sync_task_text,
            self._sync_progress,
            dict(self._sync_context),
        )
        self._sync_status = status
        self._sync_task_text = text
        if progress is not None:
            self._sync_progress = max(0, min(100, int(progress)))
        if context is not None:
            self._sync_context = dict(context)
        current = (
            self._sync_status,
            self._sync_task_text,
            self._sync_progress,
            dict(self._sync_context),
        )
        if current != previous:
            self._mark_runtime_changed()

    def _serialize_runtime_tasks(self) -> List[Dict[str, Any]]:
        """合并订阅任务与其跨盘子任务，避免同一操作重复展示。"""
        tasks = self._serialize_sync_tasks()
        transfer_manager = getattr(self, "_cross_transfer_manager", None)
        if not transfer_manager:
            return tasks
        tasks_by_id = {
            str(task.get("id") or ""): task for task in tasks
            if task.get("id")
        }
        grouped_transfers: Dict[str, List[Dict[str, Any]]] = {}
        active_statuses = set(getattr(transfer_manager, "ACTIVE", ()))
        for transfer in transfer_manager.list():
            parent_id = str(transfer.get("parent_task_id") or "")
            if parent_id not in tasks_by_id:
                if transfer.get("status") in active_statuses:
                    tasks.append(transfer)
                continue
            grouped_transfers.setdefault(parent_id, []).append(transfer)

        for parent_id, transfers in grouped_transfers.items():
            if not any(item.get("status") in active_statuses for item in transfers):
                continue
            parent = tasks_by_id[parent_id]
            total = 0
            transferred = 0
            speed = 0.0
            weighted_progress = 0
            unweighted_progress = 0
            stage_transferred = 0
            stage_total = 0
            messages: Dict[str, int] = {}
            for item in transfers:
                item_total = int(item.get("total") or 0)
                total += item_total
                transferred += int(item.get("transferred") or 0)
                speed += float(item.get("speed_bytes_per_second") or 0)
                progress_value = int(item.get("progress") or 0)
                weighted_progress += progress_value * item_total
                unweighted_progress += progress_value
                if (
                        item.get("status") in active_statuses
                        and int(item.get("stage_total") or 0) > 0
                ):
                    stage_transferred += int(item.get("stage_transferred") or 0)
                    stage_total += int(item.get("stage_total") or 0)
                message = str(
                    item.get("error") or item.get("message")
                    or item.get("phase") or "正在跨盘转存"
                )
                messages[message] = messages.get(message, 0) + 1
            if total > 0:
                progress = int(weighted_progress / total)
            else:
                progress = int(unweighted_progress / len(transfers))
            if len(transfers) == 1:
                phase = next(iter(messages))
            else:
                detail = " · ".join(
                    f"{message} × {count}" if count > 1 else message
                    for message, count in messages.items()
                )
                phase = f"跨盘转存 {len(transfers)} 个文件 · {detail}"
            parent.update({
                "phase": phase,
                "progress": progress,
                "transferred": transferred,
                "total": total,
                "stage_transferred": stage_transferred,
                "stage_total": stage_total,
                "speed_bytes_per_second": speed,
                "transfer_active": True,
                "transfer_file_count": len(transfers),
                "transfer_task_ids": [
                    str(item.get("id") or "") for item in transfers
                ],
            })
            if any(item.get("status") == "stopping" for item in transfers):
                parent["status"] = "stopping"
        return tasks

    def api_runtime_status(self, apikey: str) -> dict:
        if apikey != settings.API_TOKEN:
            return {"success": False, "message": "API密钥错误"}
        return {
            "success": True,
            "data": self._runtime_snapshot(),
        }

    def api_stop_sync(self, apikey: str) -> dict:
        if apikey != settings.API_TOKEN:
            return {"success": False, "message": "API密钥错误"}
        if not self._sync_running:
            transfer_manager = getattr(self, "_cross_transfer_manager", None)
            if transfer_manager:
                for task in transfer_manager.list(active_only=True):
                    transfer_manager.cancel(str(task.get("id") or ""))
            self.cancel_pending_subscribe_searches()
            return {"success": True, "message": "当前没有正在处理的任务"}
        self.cancel_pending_subscribe_searches()
        transfer_manager = getattr(self, "_cross_transfer_manager", None)
        if transfer_manager:
            for task in transfer_manager.list(active_only=True):
                transfer_manager.cancel(str(task.get("id") or ""))
        self._stop_event.set()
        with self._sync_tasks_lock:
            for task in self._sync_tasks.values():
                if task.get("status") in {"queued", "running", "stopping"}:
                    stop_event = task.get("stop_event")
                    if not stop_event:
                        continue
                    task["status"] = "stopping"
                    task["phase"] = "等待安全停止"
                    stop_event.set()
        self._set_sync_status("stopping", "已收到停止请求，等待当前操作安全结束", self._sync_progress)
        logger.info("收到快速停止请求，当前操作结束后将立即停止任务")
        return {"success": True, "message": "已发送停止请求"}

    def _postprocessing_pending_keys(
            self, task_snapshot: Dict[str, Any]
    ) -> set[str]:
        if not self._sync_handler:
            return set()
        subscribe_id = int(task_snapshot.get("subscribe_id") or 0)
        sub_key = str(task_snapshot.get("sub_key") or "").strip()
        return {
            str(item.get("pending_key") or "")
            for item in self._sync_handler.get_pending_finalize_tasks()
            if (
                       subscribe_id > 0
                       and int(item.get("subscribe_id") or 0) == subscribe_id
               ) or (
                       bool(sub_key)
                       and str(item.get("sub_key") or "") == sub_key
               )
        }

    def api_stop_sync_task(self, apikey: str, task_id: str) -> dict:
        if apikey != settings.API_TOKEN:
            return {"success": False, "message": "API密钥错误"}
        with self._sync_tasks_lock:
            task = self._sync_tasks.get(task_id)
            if not task:
                transfer_manager = getattr(self, "_cross_transfer_manager", None)
                if transfer_manager and transfer_manager.cancel(task_id):
                    return {"success": True, "message": "已发送跨盘任务取消请求"}
                return {"success": False, "message": "订阅任务不存在"}
            if (
                    task.get("status") == "stopping"
                    and task.get("postprocess_stop_token")
            ):
                return {
                    "success": True,
                    "message": "文件后处理正在安全停止，请等待当前提交完成",
                }
            if task.get("status") == "postprocessing":
                stop_token = f"{task_id}:{time.time_ns()}"
                task["postprocess_stop_token"] = stop_token
                # v1.5.12 F1：记录停止起点，供监控器按租约回收失效的停止流程
                task["postprocess_stop_started_at"] = time.time()
                task_snapshot = dict(task)
            else:
                task_snapshot = None
            if task.get("status") not in {"queued", "running", "stopping"}:
                if not task_snapshot:
                    return {"success": True, "message": "该订阅任务已经结束"}
            if task_snapshot:
                task["status"] = "stopping"
                task["phase"] = "正在停止文件后处理"
            else:
                stop_event = task.get("stop_event")
                if not stop_event:
                    return {"success": False, "message": "当前任务不支持停止"}
                task["status"] = "stopping"
                task["phase"] = "等待安全停止"
                stop_event.set()

        self._mark_runtime_changed()

        transfer_manager = getattr(self, "_cross_transfer_manager", None)
        if transfer_manager:
            transfer_manager.cancel_parent(task_id)

        if task_snapshot:
            try:
                pending_keys = self._postprocessing_pending_keys(task_snapshot)
                with self._sync_tasks_lock:
                    current = self._sync_tasks.get(task_id)
                    if (
                            not current
                            or str(current.get("postprocess_stop_token") or "")
                            != str(task_snapshot.get("postprocess_stop_token") or "")
                    ):
                        return {"success": False, "message": "任务状态已变化，请刷新后重试"}
                    current["postprocess_stop_pending_keys"] = set(pending_keys)
                Thread(
                    target=self._finish_postprocessing_stop,
                    args=(task_id, task_snapshot),
                    daemon=True,
                    name="pansearch-safe-postprocess-stop",
                ).start()
            except Exception as error:
                with self._sync_tasks_lock:
                    current = self._sync_tasks.get(task_id)
                    if current:
                        current.update({
                            "status": "postprocessing",
                            "phase": task_snapshot.get("phase")
                                     or "文件后处理中",
                        })
                        current.pop("postprocess_stop_token", None)
                        current.pop("postprocess_stop_pending_keys", None)
                logger.error(
                    f"启动文件后处理安全停止失败："
                    f"{task_snapshot.get('title')}，{error}"
                )
                return {"success": False, "message": f"停止后处理失败：{error}"}
            logger.info(
                f"文件后处理任务进入安全停止：{task_snapshot.get('title')}，"
                "等待当前文件提交完成"
            )
            return {
                "success": True,
                "message": (
                    f"正在安全停止：{task_snapshot.get('title')}，"
                    "当前文件提交完成后停止"
                ),
            }
        logger.info(f"收到单任务停止请求：{task.get('title')}（{task_id}）")
        return {"success": True, "message": f"已请求停止：{task.get('title')}"}

    # 安全停止等待后处理原子提交的最长时间（秒）。超时后不再无限等待，
    # 直接移除未开始的持久任务并标记已停止，避免「停止不了」。
    _POSTPROCESS_STOP_WAIT_SECONDS = 30
    # v1.5.12 F1：停止租约。stopping 状态会把 pending 从后处理队列中永久
    # 剔除，而负责收尾的停止线程可能因网盘请求阻塞而永不返回，表现为
    # 「后处理卡死 + 点停止也停不了」。超过该租约仍停留在 stopping，即
    # 由监控器（每轮必跑）回收标记并强制终结任务。
    _POSTPROCESS_STOP_LEASE_SECONDS = 120

    @staticmethod
    def _discard_postprocess_stop(
            task: Optional[Dict[str, Any]], stop_token: str
    ) -> None:
        """v1.5.12 F1：丢弃已失效的停止标记，避免任务永久停在 stopping。

        调用方必须已持有 _sync_tasks_lock（RLock）。
        """
        if not task:
            return
        if str(task.get("postprocess_stop_token") or "") != stop_token:
            return
        task.pop("postprocess_stop_token", None)
        task.pop("postprocess_stop_pending_keys", None)
        task.pop("postprocess_stop_started_at", None)
        if task.get("status") == "stopping":
            task.update({
                "status": "postprocessing",
                "phase": "文件后处理中",
            })

    def _collect_stopping_keys(self, now: Optional[float] = None) -> Set[str]:
        """收集「正在安全停止」的 pending 键，并回收超期 stopping 任务。

        v1.5.12 F1：原实现只按 status/stop_token 过滤，没有任何超时保护。
        历史故障：停止线程卡在网盘请求上（daemon 线程、无日志、无超时），
        任务永久停留在 stopping，其 pending_keys 被每轮剔除，于是该任务
        既不会被处理、也不会被停止，前端永远显示「处理中」。
        """
        current_time = time.time() if now is None else now
        stopping_keys: Set[str] = set()
        expired_task_ids: List[str] = []
        with self._sync_tasks_lock:
            for task_id, task in self._sync_tasks.items():
                if task.get("status") != "stopping" or not task.get(
                        "postprocess_stop_token"
                ):
                    continue
                started_at = float(task.get("postprocess_stop_started_at") or 0)
                if started_at <= 0:
                    task["postprocess_stop_started_at"] = current_time
                    started_at = current_time
                if (
                        current_time - started_at
                        < self._POSTPROCESS_STOP_LEASE_SECONDS
                ):
                    for pending_key in (
                            task.get("postprocess_stop_pending_keys") or set()
                    ):
                        stopping_keys.add(str(pending_key))
                    continue
                expired_task_ids.append(task_id)
            for task_id in expired_task_ids:
                task = self._sync_tasks[task_id]
                task.pop("postprocess_stop_token", None)
                task.pop("postprocess_stop_pending_keys", None)
                task.pop("postprocess_stop_started_at", None)
                task.update({
                    "status": "stopped",
                    "phase": "文件后处理已停止（停止超时，已自动回收）",
                    "progress": 100,
                    "pending_count": 0,
                    "finished_at": current_time,
                })
        if expired_task_ids:
            logger.warning(
                f"安全停止超过 {self._POSTPROCESS_STOP_LEASE_SECONDS}s 未完成，"
                f"已自动回收 {len(expired_task_ids)} 个停止中的任务"
            )
            self._mark_runtime_changed()
        return stopping_keys

    def _finish_postprocessing_stop(
            self,
            task_id: str,
            task_snapshot: Dict[str, Any],
    ) -> None:
        """等待当前后处理原子提交完成，再移除尚未开始的持久任务。

        v1.5.10：对 _offline_monitor_lock 的等待加入超时。历史故障中，
        监控任务长时间持有该锁（内部含网盘网络请求），导致本停止线程在
        acquire() 上永久阻塞，任务卡在 stopping，前端表现为「停止不了」。
        超时后仍继续执行清理逻辑：宁可提前移除尚未开始的任务，也不能让
        用户无法停止。
        """
        stop_token = str(task_snapshot.get("postprocess_stop_token") or "")
        acquired = self._offline_monitor_lock.acquire(
            timeout=self._POSTPROCESS_STOP_WAIT_SECONDS
        )
        if not acquired:
            logger.warning(
                f"等待后处理原子提交超时（{self._POSTPROCESS_STOP_WAIT_SECONDS}s），"
                f"强制结束停止流程：{task_snapshot.get('title')}"
            )
        try:
            with self._sync_tasks_lock:
                current = self._sync_tasks.get(task_id)
                if (
                        not current
                        or str(current.get("postprocess_stop_token") or "")
                        != stop_token
                ):
                    # v1.5.12 F1：提前 return 前必须确保本 token 不再留存，
                    # 否则 stopping 标记无人回收，任务永久卡在停止中。
                    self._discard_postprocess_stop(current, stop_token)
                    return
            pending_keys = self._postprocessing_pending_keys(task_snapshot)
            removed = self._sync_handler.stop_pending_finalize_tasks(
                pending_keys
            ) if self._sync_handler and pending_keys else 0
            with self._sync_tasks_lock:
                current = self._sync_tasks.get(task_id)
                if (
                        not current
                        or str(current.get("postprocess_stop_token") or "")
                        != stop_token
                ):
                    self._discard_postprocess_stop(current, stop_token)
                    return
                current.update({
                    "status": "stopped",
                    "phase": "文件后处理已停止，网盘文件已保留",
                    "progress": 100,
                    "pending_count": 0,
                    "finished_at": time.time(),
                })
                current.pop("postprocess_stop_token", None)
                current.pop("postprocess_stop_pending_keys", None)
            logger.info(
                f"文件后处理任务已停止：{task_snapshot.get('title')}，"
                f"已移除 {removed} 个待处理记录；"
                "网盘离线任务和现有文件均保留"
            )
        except Exception as error:
            with self._sync_tasks_lock:
                current = self._sync_tasks.get(task_id)
                if current and str(
                        current.get("postprocess_stop_token") or ""
                ) == stop_token:
                    current.update({
                        "status": "postprocessing",
                        "phase": task_snapshot.get("phase") or "文件后处理中",
                    })
                    current.pop("postprocess_stop_token", None)
                    current.pop("postprocess_stop_pending_keys", None)
            logger.error(
                f"安全停止文件后处理任务失败："
                f"{task_snapshot.get('title')}，{error}"
            )
        finally:
            if acquired:
                self._offline_monitor_lock.release()
            self._mark_runtime_changed()

    def _monitor_offline_task_groups(
            self,
            sync_handler: Any,
            offline_service: Optional[Any],
            kwargs: Dict[str, Any],
    ) -> Dict[str, Any]:
        groups = sync_handler.get_due_offline_task_groups(
            force=bool(kwargs.get("force")),
            pending_keys=kwargs.get("pending_keys"),
        )
        stopping_keys = self._collect_stopping_keys()
        if stopping_keys:
            filtered_groups = []
            for group in groups:
                active_keys = set(group["pending_keys"]) - stopping_keys
                if not active_keys:
                    continue
                filtered_group = dict(group)
                filtered_group["pending_keys"] = active_keys
                filtered_groups.append(filtered_group)
            groups = filtered_groups
        if not groups:
            return {
                "checked": 0,
                "completed": 0,
                "failed": 0,
                "pending": self._count_pending_non_blocking(sync_handler),
            }
        shared_kwargs = dict(kwargs)
        if (
                any(group["needs_offline"] for group in groups)
                and offline_service
                and shared_kwargs.get("offline_tasks") is None
        ):
            snapshot = offline_service.get_offline_task_list_snapshot(
                force=True,
            )
            shared_kwargs["offline_tasks"] = snapshot.get("tasks") or []
            shared_kwargs["offline_tasks_valid"] = bool(snapshot.get("refresh_ok"))

        if len(groups) == 1:
            shared_kwargs["pending_keys"] = set(groups[0]["pending_keys"])
            return sync_handler.monitor_offline_strm_tasks(**shared_kwargs)

        policy = getattr(getattr(self, "_cloud_drive", None), "policy", None)
        worker_count = min(
            len(groups),
            max(1, int(getattr(policy, "max_concurrency", 1) or 1)),
        )
        logger.debug(
            f"离线后处理并发调度：{len(groups)} 个媒体队列，并发数 {worker_count}"
        )
        totals = {"checked": 0, "completed": 0, "failed": 0, "pending": 0}
        with ThreadPoolExecutor(
                max_workers=worker_count,
                thread_name_prefix="pansearch-offline",
        ) as executor:
            futures = {
                executor.submit(
                    sync_handler.monitor_offline_strm_tasks,
                    **{
                        **shared_kwargs,
                        "pending_keys": set(group["pending_keys"]),
                    },
                ): group
                for group in groups
            }
            for future in as_completed(futures):
                group = futures[future]
                try:
                    result = future.result()
                except Exception as error:
                    logger.error(
                        f"媒体后处理队列执行失败："
                        f"{len(group['pending_keys'])} 个文件，{error}",
                        exc_info=True,
                    )
                    result = self._retry_group_isolated(
                        sync_handler, group, shared_kwargs
                    )
                for field in ("checked", "completed", "failed"):
                    totals[field] += int(result.get(field) or 0)
        totals["pending"] = self._count_pending_non_blocking(sync_handler)
        return totals

    def _retry_group_isolated(
            self,
            sync_handler: Any,
            group: Dict[str, Any],
            shared_kwargs: Dict[str, Any],
    ) -> Dict[str, int]:
        """整组执行失败后降级为逐条重试，隔离异常条目（v1.5.6 F2）。

        批量执行时，单条损坏数据会让整组 pending 本轮全部不推进，且连败
        计数不增长，表现为任务长期停留在"后处理中"。降级逐条执行后，健康
        条目照常推进，异常条目单独记录并带上 pending_key，便于定位清理。
        """
        keys = list(group.get("pending_keys") or [])
        totals = {"checked": 0, "completed": 0, "failed": 0}
        if len(keys) < 2:
            return totals
        logger.warning(f"后处理队列降级为逐条重试：{len(keys)} 个文件")
        for pending_key in keys:
            try:
                result = sync_handler.monitor_offline_strm_tasks(
                    **{**shared_kwargs, "pending_keys": {pending_key}}
                )
            except Exception as error:
                logger.error(
                    f"后处理条目异常已隔离：{pending_key}，{error}",
                    exc_info=True,
                )
                totals["failed"] += 1
                continue
            for field in ("checked", "completed", "failed"):
                totals[field] += int(result.get(field) or 0)
        return totals

    @staticmethod
    def _count_pending_non_blocking(sync_handler: Any, fallback: int = 0) -> int:
        """不阻塞地读取待后处理数量（v1.5.14）。

        优先使用 sync 侧的 ``count_pending_finalize_tasks``；拿不到锁时直接
        返回调用方已知/上次的计数，绝不挂起调用线程。
        """
        counter = getattr(sync_handler, "count_pending_finalize_tasks", None)
        if callable(counter):
            try:
                return int(counter(fallback) or 0)
            except Exception:
                return int(fallback or 0)
        # 旧宿主无该接口：保守走原路径但限时不可行，退回 fallback
        return int(fallback or 0)

    def _run_offline_monitor(self, **kwargs: Any) -> Dict[str, Any]:
        """按媒体队列并发执行网盘文件终态检查。"""
        sync_handler = self._sync_handler
        offline_service = self._offline_task_service()
        if not sync_handler:
            return {
                "checked": 0,
                "completed": 0,
                "failed": 0,
                "pending": 0,
            }
        if not self._offline_monitor_lock.acquire(blocking=False):
            # v1.5.14：这里绝不能阻塞等待。后处理监控可能长时间持有
            # ``_offline_pending_lock``（含网盘 IO），而
            # ``get_pending_finalize_tasks`` 需要同一把锁；本路径同时服务于
            # 调度器 tick 与前端刷新 API，一旦阻塞就会把线程堆在锁上，
            # 直至 APScheduler 线程池耗尽 —— 表现为「后处理永久卡住 + 页面很慢」。
            # 拿不到锁时退回调用方已知计数（仅用于日志与展示）。
            pending_count = self._count_pending_non_blocking(sync_handler)
            logger.debug(
                f"已有网盘文件后处理正在执行，本轮检查跳过"
                f"（待处理 {pending_count} 个）"
            )
            return {
                "checked": 0,
                "completed": 0,
                "failed": 0,
                "pending": pending_count,
                "deferred": 1,
            }
        try:
            notification_batch_started = sync_handler.begin_notification_batch()
            try:
                return self._monitor_offline_task_groups(
                    sync_handler, offline_service, kwargs
                )
            finally:
                if notification_batch_started:
                    sync_handler.finish_notification_batch()
        finally:
            self._offline_monitor_lock.release()

    def api_offline_tasks(self, apikey: str, refresh: bool = False) -> dict:
        if apikey != settings.API_TOKEN:
            return {"success": False, "message": "API密钥错误"}
        offline_tasks = self._offline_task_service()
        if not offline_tasks:
            return {"success": False, "message": "当前网盘不支持离线任务", "data": []}
        snapshot = offline_tasks.get_offline_tasks_snapshot(force=refresh)
        snapshot["provider"] = self._cloud_drive.key
        snapshot["provider_name"] = self._cloud_drive.name
        monitor_result = None
        if refresh and self._sync_handler:
            monitor_result = self._run_offline_monitor(
                force=True,
                offline_tasks=snapshot.get("tasks") or [],
                offline_tasks_valid=bool(snapshot.get("refresh_ok")),
            )
        snapshot = self._merge_pending_finalize_tasks(snapshot)
        if monitor_result is not None:
            snapshot["monitor"] = monitor_result
        return {
            "success": True,
            "message": "已手动刷新离线任务" if refresh else "已返回离线任务缓存",
            "data": snapshot,
        }

    def _merge_pending_finalize_tasks(self, snapshot: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(snapshot or {})
        tasks = [dict(item) for item in (result.get("tasks") or [])]
        pending = self._sync_handler.get_pending_finalize_tasks() if self._sync_handler else []
        task_by_id = {
            str(item.get("id") or "").upper(): item
            for item in tasks
            if item.get("id")
        }
        for item in pending:
            task_id = str(item.get("task_id") or "").upper()
            task = task_by_id.get(task_id) if task_id else None
            pending_meta = {
                "pending_key": item.get("pending_key"),
                "finalize_pending": True,
                "cloud_dir": item.get("cloud_dir"),
                "target_name": item.get("file_name"),
                "size": int(item.get("file_size") or (task or {}).get("size") or 0),
                "postprocess_text": (
                    "等待下载完成后读取真实文件并匹配"
                    if (item.get("task_type") or "share") == "magnet"
                    else "等待下载完成或文件处理"
                    if (item.get("task_type") or "share") == "ed2k"
                    else "等待系统处理、重命名和STRM"
                ),
            }
            if task:
                task.update(pending_meta)
            else:
                synthetic = {
                    "id": task_id,
                    "name": item.get("file_name") or "未命名文件",
                    "state": "processing",
                    "completed": False,
                    "failed": False,
                    "status_text": "系统处理中",
                    "percent": 100,
                    "add_time": int(item.get("created_at") or 0),
                    **pending_meta,
                }
                tasks.insert(0, synthetic)
        result["tasks"] = tasks
        result["pending_count"] = len(pending)
        return result

    def api_retry_offline_tasks(
            self,
            apikey: str,
            pending_keys: List[str],
            task_ids: Optional[List[str]] = None,
    ) -> dict:
        if apikey != settings.API_TOKEN:
            return {"success": False, "message": "API密钥错误"}
        offline_tasks = self._offline_task_service()
        if not offline_tasks:
            return {"success": False, "message": "当前网盘不支持离线任务"}
        keys = {str(value).strip() for value in pending_keys if str(value).strip()}
        normalized_task_ids = {
            str(value).strip().upper()
            for value in (task_ids or [])
            if str(value).strip()
        }
        if not keys and not normalized_task_ids:
            return {"success": False, "message": "请选择需要重试的任务"}
        restarted = 0
        restart_failed = 0
        for task_id in normalized_task_ids:
            try:
                offline_tasks.restart_offline_task(task_id)
                restarted += 1
                if self._sync_handler:
                    self._sync_handler._remove_offline_blacklist(task_id)
            except Exception as error:
                restart_failed += 1
                logger.error(f"重启离线任务失败 {task_id}：{error}")
        result = {"checked": 0, "completed": 0, "failed": 0, "pending": 0}
        if keys:
            if not self._sync_handler:
                return {"success": False, "message": "同步处理器未初始化"}
            result = self._run_offline_monitor(
                force=True,
                pending_keys=keys,
            )
        return {
            "success": restart_failed == 0,
            "message": (
                    f"已重启 {restarted} 个离线任务，复查 {result['checked']} 个后处理任务，"
                    f"完成 {result['completed']} 个，仍待处理 {result['pending']} 个"
                    + (f"，重启失败 {restart_failed} 个" if restart_failed else "")
            ),
            "data": {
                **result,
                "restarted": restarted,
                "restart_failed": restart_failed,
            },
        }

    def api_force_clear_offline_pending(
            self,
            apikey: str,
            pending_keys: Optional[List[str]] = None,
            reason: str = "",
    ) -> dict:
        """v1.5.12 F4：强制清理长期停留在「处理中」的后处理记录。

        不传 pending_keys 时清空全部待后处理队列。用于从历史故障
        （停止不了 / 永久后处理中）中一次性恢复。
        """
        if apikey != settings.API_TOKEN:
            return {"success": False, "message": "API密钥错误"}
        sync_handler = self._sync_handler
        if not sync_handler:
            return {"success": False, "message": "插件尚未初始化完成"}
        try:
            keys = (
                {str(key) for key in pending_keys if str(key).strip()}
                if pending_keys else None
            )
            removed = sync_handler.force_clear_offline_pending(
                pending_keys=keys, reason=str(reason or "")
            )
        except Exception as error:
            logger.error(f"强制清理待后处理记录失败：{error}")
            return {"success": False, "message": str(error)}
        self._mark_runtime_changed()
        return {
            "success": True,
            "message": f"已强制清理 {removed} 个待后处理记录",
            "data": {"removed": removed},
        }

    def api_delete_offline_task(
            self, apikey: str, task_id: str, pending_key: str = ""
    ) -> dict:
        if apikey != settings.API_TOKEN:
            return {"success": False, "message": "API密钥错误"}
        offline_tasks = self._offline_task_service()
        if not offline_tasks:
            return {"success": False, "message": "当前网盘不支持离线任务"}
        try:
            normalized_id = str(task_id or "").strip().upper()
            normalized_pending_key = str(pending_key or "").strip()
            monitor_result = None
            removed_pending = 0
            if self._sync_handler and (normalized_pending_key or normalized_id):
                removed_pending = self._sync_handler.delete_pending_finalize_tasks(
                    {normalized_pending_key} if normalized_pending_key else set(),
                    {normalized_id} if normalized_id else set(),
                )
            if normalized_id:
                try:
                    offline_tasks.delete_offline_task(
                        normalized_id, delete_source_file=False
                    )
                except Exception:
                    if not removed_pending:
                        raise
                    logger.debug(
                        f"{self._cloud_drive.name}离线任务已不存在，继续清理待后处理记录：{normalized_id}"
                    )
            if self._sync_handler and normalized_id and not removed_pending:
                snapshot = offline_tasks.get_offline_tasks_snapshot(force=False)
                monitor_result = self._run_offline_monitor(
                    force=True,
                    pending_keys={normalized_id},
                    offline_tasks=snapshot.get("tasks") or [],
                    # 删除接口成功已经确认该任务不存在，无需再请求一次任务列表。
                    offline_tasks_valid=True,
                )
            if normalized_id and removed_pending:
                message = "离线任务和后处理任务已删除，已下载文件保留"
            elif normalized_id:
                message = "离线任务已删除，已下载文件保留"
            else:
                message = "后处理任务已删除，已下载文件保留"
            if monitor_result and monitor_result.get("failed"):
                message += "；目标文件不存在，下载历史已结束"
            elif monitor_result and monitor_result.get("completed"):
                message += "；目标文件已完成后处理"
            return {
                "success": True,
                "message": message,
                "data": {
                    "monitor": monitor_result,
                    "pending_deleted": removed_pending,
                },
            }
        except Exception as error:
            logger.error(f"删除离线任务失败：{error}")
            return {"success": False, "message": str(error)}

    def api_delete_offline_tasks(
            self,
            apikey: str,
            task_ids: List[str],
            pending_keys: Optional[List[str]] = None,
    ) -> dict:
        if apikey != settings.API_TOKEN:
            return {"success": False, "message": "API密钥错误"}
        offline_tasks = self._offline_task_service()
        if not offline_tasks:
            return {"success": False, "message": "当前网盘不支持离线任务"}
        try:
            normalized_ids = {
                str(value or "").strip().upper() for value in task_ids
                if str(value or "").strip()
            }
            normalized_pending_keys = {
                str(value or "").strip() for value in (pending_keys or [])
                if str(value or "").strip()
            }
            removed_pending = 0
            if self._sync_handler and (normalized_pending_keys or normalized_ids):
                removed_pending = self._sync_handler.delete_pending_finalize_tasks(
                    normalized_pending_keys,
                    normalized_ids,
                )
            try:
                count = offline_tasks.delete_offline_tasks(
                    task_ids, delete_source_file=False
                ) if task_ids else 0
            except Exception:
                if not removed_pending:
                    raise
                count = 0
                logger.debug(
                    f"部分{self._cloud_drive.name}离线任务已不存在，继续批量清理待后处理记录"
                )
            monitor_result = None
            if self._sync_handler and normalized_ids and not removed_pending:
                snapshot = offline_tasks.get_offline_tasks_snapshot(force=False)
                monitor_result = self._run_offline_monitor(
                    force=True,
                    pending_keys=normalized_ids,
                    offline_tasks=snapshot.get("tasks") or [],
                    offline_tasks_valid=True,
                )
            return {
                "success": True,
                "message": (
                        f"已删除 {count} 个离线任务"
                        + (f"、{removed_pending} 个后处理任务" if removed_pending else "")
                        + "，已下载文件保留"
                ),
                "data": {
                    "deleted": count,
                    "pending_deleted": removed_pending,
                    "monitor": monitor_result,
                },
            }
        except Exception as error:
            logger.error(f"批量删除离线任务失败：{error}")
            return {"success": False, "message": str(error)}

    def _start_offline_monitor_scheduler(
            self, pending_count: int
    ) -> Optional[BackgroundScheduler]:
        """构建并启动离线监控调度器；失败返回 None。"""
        try:
            scheduler = BackgroundScheduler(timezone=settings.TZ)
            scheduler.add_job(
                func=self.monitor_offline_tasks,
                trigger=IntervalTrigger(seconds=20),
                id="PanSearch_OfflineMonitor",
                next_run_time=datetime.datetime.now(
                    tz=pytz.timezone(settings.TZ)
                ) + datetime.timedelta(seconds=3),
                max_instances=2,
                coalesce=True,
                replace_existing=True,
            )
            scheduler.start()
        except Exception as error:
            logger.error(f"启动网盘文件终态后处理监控失败：{error}")
            return None
        self._offline_scheduler = scheduler
        logger.debug(f"网盘文件终态后处理监控已启动（待处理 {pending_count} 个）")
        return scheduler

    def _update_offline_monitor(self, pending_count: int) -> None:
        """仅在存在待后处理文件时运行独立监控。

        v1.5.10：本方法必须保证「监控器一定存在且确实在跑」。
        历史故障：停机清理（stop_service）会对旧调度器执行 remove_all_jobs()
        + shutdown(wait=False)，但 shutdown 返回后 .running 仍可能短暂为 True，
        而 job 已被摘除。此时调用 modify_job 会抛 JobLookupError 并向上冒泡，
        导致监控器再也无法重建 —— 表现为待处理任务永久「后处理中」且停止不了。
        因此这里对 modify_job 做兜底，任何异常都降级为重建调度器。
        """
        self._refresh_postprocessing_sync_tasks()
        with self._offline_scheduler_lock:
            scheduler = self._offline_scheduler
            if pending_count > 0:
                if scheduler and scheduler.running:
                    try:
                        scheduler.modify_job(
                            "PanSearch_OfflineMonitor",
                            next_run_time=datetime.datetime.now(
                                tz=pytz.timezone(settings.TZ)
                            ) + datetime.timedelta(seconds=1),
                        )
                        return
                    except Exception as error:
                        # job 已不存在（或调度器处于不可用状态）：降级重建，
                        # 绝不允许异常逃逸导致监控器永久死亡。
                        logger.warning(
                            f"复用网盘文件终态监控失败，重建调度器：{error}"
                        )
                        try:
                            scheduler.remove_all_jobs()
                            if scheduler.running:
                                scheduler.shutdown(wait=False)
                        except Exception:
                            pass
                        self._offline_scheduler = None
                self._start_offline_monitor_scheduler(pending_count)
                return

            if not scheduler:
                return
            try:
                scheduler.remove_all_jobs()
                if scheduler.running:
                    scheduler.shutdown(wait=False)
            finally:
                self._offline_scheduler = None
            logger.debug("网盘文件终态后处理队列已清空，监控已停止")

    def monitor_offline_tasks(self):
        """轻量检查到期节点，实际网盘请求间隔由待处理任务控制。"""
        return self._run_offline_monitor()

    def _offline_task_service(self):
        if not self._cloud_drive or not self._cloud_drive.supports(
                CloudDriveCapability.OFFLINE_TASKS
        ):
            return None
        return self._cloud_drive.require(CloudDriveCapability.OFFLINE_TASKS)
