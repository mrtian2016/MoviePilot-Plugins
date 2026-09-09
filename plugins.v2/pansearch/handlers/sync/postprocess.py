"""离线任务完成检测与文件后处理。"""

import copy
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Set, Tuple

from app.db import SessionFactory
from app.db.models.subscribe import Subscribe
from app.db.subscribe_oper import SubscribeOper
from app.log import logger
from app.schemas.types import MediaType

from ...core import OwnerDelegator
from ...utils import MediaFileParser


class PostprocessService(OwnerDelegator):
    """监控待处理文件并完成重命名、STRM和历史状态更新。"""

    _POSTPROCESS_STEPS = (
        ("locate", "检查并定位文件"),
        ("organize", "重命名与移动"),
        ("strm", "生成 STRM"),
        ("subtitle", "处理字幕"),
        ("metadata", "刮削元数据"),
        ("commit", "登记完成状态"),
        ("notify", "消息通知"),
    )

    _FINALIZE_MAX_FAILURES = 5
    _FINALIZE_DEAD_LOG = "文件后处理连续失败 {} 次，已终止重试：{}"
    _FINALIZE_DEAD_REASON = "文件后处理连续失败 {} 次，已停止自动重试"
    _FINALIZE_DEAD_TITLE = "115 文件后处理失败"
    _FINALIZE_DEAD_TEXT = (
        "{}\n\n连续 {} 次后处理失败，已停止自动重试，"
        "请检查网盘文件与媒体目录状态"
    )

    @staticmethod
    def _postprocess_task_id(item: Dict[str, Any]) -> str:
        subscribe_id = int(item.get("subscribe_id") or 0)
        if subscribe_id > 0:
            return f"subscribe:{subscribe_id}"
        sub_key = str(item.get("sub_key") or "").strip()
        return f"media:{sub_key}" if sub_key else ""

    def _postprocess_steps(
            self, item: Optional[Dict[str, Any]] = None
    ) -> List[Dict[str, str]]:
        has_subtitles = bool((item or {}).get("subtitles"))
        strm_enabled = bool(
            self._strm_generate_enabled
            and self._strm_generator
            and self._local_resource_path
        )
        metadata_enabled = bool(
            self._metadata_scraper
            and self._local_resource_path
            and (self._nfo_scrape_enabled or self._image_scrape_enabled)
        )
        return [
            {"key": key, "label": label}
            for key, label in self._POSTPROCESS_STEPS
            if not (key == "strm" and not strm_enabled)
               and not (key == "subtitle" and not has_subtitles)
               and not (key == "metadata" and not metadata_enabled)
               and not (key == "notify" and not self._notify)
        ]

    def _update_postprocess_progress(
            self,
            item: Dict[str, Any],
            step: str,
            file_index: int,
            file_total: int,
            detail: str = "",
    ) -> None:
        """通过现有任务运行态推送当前文件和处理步骤。"""
        if not self._task_update:
            return
        task_id = self._postprocess_task_id(item)
        if not task_id:
            return
        steps = self._postprocess_steps(item)
        step_index = next(
            (
                index for index, value in enumerate(steps)
                if value["key"] == step
            ),
            None,
        )
        if step_index is None:
            return
        normalized_index = max(1, min(int(file_index or 1), int(file_total or 1)))
        normalized_total = max(1, int(file_total or 1))
        file_progress = (
                                (normalized_index - 1)
                                + (step_index + 1) / max(1, len(steps))
                        ) / normalized_total
        self._task_update(
            task_id,
            current_file=str(item.get("file_name") or "").strip(),
            postprocess_active=True,
            postprocess_detail=str(detail or "").strip(),
            postprocess_step=step,
            postprocess_step_index=step_index,
            postprocess_step_total=len(steps),
            postprocess_steps=steps,
            postprocess_file_index=normalized_index,
            postprocess_file_total=normalized_total,
            postprocess_progress=round(file_progress * 100, 2),
            progress=min(99, 95 + int(file_progress * 4)),
        )

    def _cleanup_failed_offline_task(
            self, item: Dict[str, Any], reason: str
    ) -> None:
        """失败后删除对应离线任务及其源文件，不清空共享隔离目录。"""
        task_id = str(item.get("task_id") or "").strip().upper()
        if not task_id or not self._offline_tasks:
            return
        try:
            deleted = self._offline_tasks.delete_offline_task(
                task_id, delete_source_file=True
            )
            if deleted:
                logger.debug(
                    f"Magnet 匹配失败，已删除离线任务及下载文件：{task_id}，原因：{reason}"
                )
        except Exception as error:
            logger.warning(
                f"Magnet 匹配失败后清理下载文件失败：{task_id}，{error}"
            )

    def _persist_offline_progress(
            self, item: Dict[str, Any], task: Dict[str, Any]
    ) -> None:
        """任务仍在下载时持久化轻量进度快照，供前端展示非空状态。"""
        try:
            status_text = str(task.get("status_text") or "").strip() or "处理中"
            percent = max(0.0, min(float(task.get("percent") or 0.0), 100.0))
            snapshot = {
                "status_text": status_text,
                "percent": round(percent, 1),
                "state": str(task.get("state") or "processing"),
                "updated_at": time.time(),
            }
            if item.get("offline_progress") == snapshot:
                return
            item["offline_progress"] = snapshot
            self._sync_pending_history_status(item, f"{status_text} {percent:.0f}%")
        except Exception as error:
            logger.debug(f"记录离线下载进度快照失败：{error}")

    def _sync_pending_history_status(
            self, item: Dict[str, Any], status: str
    ) -> None:
        """把进度快照同步到等待该 pending 键的历史记录，避免状态为空。"""
        if not self._get_data or not self._save_data or not status:
            return
        try:
            with self._offline_pending_lock:
                history = self._get_data("history") or []
                changed = False
                for record in history:
                    if str(record.get("finalize_key") or "") != str(
                            item.get("pending_key") or ""):
                        continue
                    if str(record.get("status") or "") in {"成功", "失败"}:
                        continue
                    record["status"] = status
                    changed = True
                if changed:
                    self._save_data("history", history)
        except Exception as error:
            logger.debug(f"同步离线进度到历史记录失败：{error}")

    def _offline_timeout_file_verdict(
            self,
            item: Dict[str, Any],
            now: float,
            directory_snapshot,
            subscribe_cache: Optional[Dict[int, Any]] = None,
    ) -> Optional[str]:
        """超时判失败前的文件已存在终审。

        复用分享分支的反查机制（staging 目录与最终目录按文件名和
        source_sha1 匹配）：文件已就绪返回 "ready"（转成功终态流程）；
        目录列举失败返回 "defer"（安排重试）；未找到返回 "missing"。
        """
        staging_dir = str(
            item.get("staging_dir") or item.get("cloud_dir") or "/"
        ).rstrip("/") or "/"
        staging_name = str(
            item.get("staging_name") or item.get("file_name") or ""
        )
        source_sha1 = str(item.get("source_sha1") or "").upper()
        final_dir = str(item.get("cloud_dir") or "/").rstrip("/") or "/"
        directories = [staging_dir]
        if final_dir != staging_dir:
            directories.append(final_dir)
        for cloud_dir in directories:
            directory_valid, file_index = directory_snapshot(cloud_dir)
            if not directory_valid:
                return "defer"
            if not file_index:
                continue
            candidate = file_index.get(staging_name)
            if not candidate and source_sha1:
                candidate = next(
                    (
                        value for value in file_index.values()
                        if str(value.sha1 or "").upper() == source_sha1
                    ),
                    None,
                )
            if candidate:
                logger.info(
                    f"离线任务超时但文件已在网盘就绪，转成功终态："
                    f"{cloud_dir}/{getattr(candidate, 'name', '') or staging_name}"
                )
                return "ready"
        return "missing"

    @staticmethod
    def _offline_timeout_should_defer(
            tasks_valid: bool, verdict: Optional[str]
    ) -> bool:
        """任务列表快照不可用时，仅凭“超时/未找到”不足以下失败终态。

        快照刷新失败（tasks_valid False）且文件终审未判 ready 时，
        超时分支必须暂缓判定，等待接口恢复后再下结论。
        """
        return (not tasks_valid) and verdict != "ready"

    @staticmethod
    def _upgrade_backup_name(file_name: str, task_id: str) -> str:
        """仅在原文件名后追加短任务 ID，避免隐藏文件和冗长标记。"""
        source = Path(str(file_name or ""))
        short_id = "".join(
            value for value in str(task_id or "") if value.isalnum()
        )[:10]
        if not short_id:
            short_id = uuid.uuid4().hex[:10]
        return f"{source.stem}-{short_id}{source.suffix}"

    def _activate_persisted_pending_tasks(
            self, pending: Dict[str, Dict[str, Any]], now: float
    ) -> int:
        """恢复历史已落盘但未激活的后处理任务。"""
        inactive_keys = {
            key for key, item in pending.items()
            if not bool(item.get("history_ready", True))
        }
        if not inactive_keys:
            return 0
        history = self._get_data("history") or []
        persisted_keys = {
            str(record.get("finalize_key") or "")
            for record in history
            if isinstance(record, dict) and record.get("finalize_key")
        }
        activated_keys = inactive_keys & persisted_keys
        for key in activated_keys:
            pending[key]["history_ready"] = True
            pending[key]["next_check_at"] = min(
                float(pending[key].get("next_check_at") or now), now
            )
        if activated_keys:
            self._save_offline_pending(pending)
            logger.info(
                f"已恢复 {len(activated_keys)} 个未激活的115文件后处理任务"
            )
        return len(activated_keys)

    @staticmethod
    def _due_pending_keys(
            pending: Dict[str, Dict[str, Any]],
            now: float,
            force: bool = False,
            pending_keys: Optional[Set[str]] = None,
    ) -> List[str]:
        selected = set(pending_keys or [])
        return [
            key
            for key, item in pending.items()
            if (not selected or key in selected)
               and bool(item.get("history_ready", True))
               and now >= float(item.get("_monitor_until") or 0)
               and (force or now >= float(item.get("next_check_at") or 0))
        ]

    @staticmethod
    def _media_context_key(item: Dict[str, Any]) -> Optional[Tuple[Any, ...]]:
        subscribe_id = int(item.get("subscribe_id") or 0)
        if subscribe_id > 0:
            return "subscribe", subscribe_id
        sub_key = str(item.get("sub_key") or "").strip()
        if sub_key:
            return "sub_key", sub_key
        media_data = item.get("mediainfo") or {}
        media_id = (
                media_data.get("tmdb_id")
                or media_data.get("douban_id")
                or media_data.get("media_id")
        )
        if not media_id:
            return None
        return (
            "media",
            str(media_data.get("type") or ""),
            str(media_id),
            int(item.get("season") or 0),
        )

    @staticmethod
    def _offline_media_group_key(
            item: Dict[str, Any], pending_key: str
    ) -> Tuple[Any, ...]:
        media_data = item.get("mediainfo") or {}
        media_type = str(media_data.get("type") or item.get("type") or "")
        media_id = (
                media_data.get("tmdb_id")
                or media_data.get("douban_id")
                or media_data.get("media_id")
        )
        if media_id:
            return "media", media_type, str(media_id)
        title = str(media_data.get("title") or item.get("title") or "").strip()
        if title:
            return (
                "title",
                media_type,
                title.casefold(),
                str(media_data.get("year") or item.get("year") or ""),
            )
        subscribe_id = int(item.get("subscribe_id") or 0)
        if subscribe_id > 0:
            return "subscribe", subscribe_id
        sub_key = str(item.get("sub_key") or "").strip()
        if sub_key:
            return "sub_key", sub_key
        return "pending", pending_key

    def get_due_offline_task_groups(
            self,
            force: bool = False,
            pending_keys: Optional[Set[str]] = None,
    ) -> List[Dict[str, Any]]:
        """按媒体聚合当前到期任务；同一媒体由单个工作线程顺序处理。"""
        if not self._get_data:
            return []
        with self._offline_pending_lock:
            pending = self._get_data(self._OFFLINE_PENDING_KEY) or {}
            self._activate_persisted_pending_tasks(pending, time.time())
        due_keys = self._due_pending_keys(
            pending, time.time(), force=force, pending_keys=pending_keys
        )
        groups: Dict[Tuple[Any, ...], Dict[str, Any]] = {}
        for pending_key in due_keys:
            item = pending[pending_key]
            group_key = self._offline_media_group_key(item, pending_key)
            group = groups.setdefault(group_key, {
                "pending_keys": set(),
                "needs_offline": False,
                "task_ids": set(),
            })
            group["pending_keys"].add(pending_key)
            group["needs_offline"] = group["needs_offline"] or str(
                item.get("task_type") or "share"
            ) in {"ed2k", "magnet"}
            task_id = str(item.get("task_id") or "").strip().upper()
            if task_id:
                group["task_ids"].add(task_id)
        return list(groups.values())

    @staticmethod
    def _strm_file_ready(strm_path: Optional[Path]) -> bool:
        try:
            return bool(
                strm_path
                and strm_path.is_file()
                and strm_path.stat().st_size > 0
            )
        except OSError:
            return False

    def _delete_upgrade_old_strm(
            self,
            item: Dict[str, Any],
            replacement_path: Optional[Path] = None,
    ) -> None:
        """新 STRM 就绪后清理路径不同的旧版本 STRM。"""
        if not self._strm_generator or not self._local_resource_path:
            return
        old_dir = str(item.get("upgrade_old_cloud_dir") or "").strip()
        old_name = str(item.get("upgrade_old_file_name") or "").strip()
        if not old_dir or not old_name:
            return
        try:
            strm_path = self._strm_generator.local_path(
                local_root=self._local_resource_path,
                cloud_root=self._CLOUD_MEDIA_ROOT,
                cloud_dir=old_dir,
                file_name=old_name,
            )
            if replacement_path and (
                    strm_path.resolve(strict=False)
                    == replacement_path.resolve(strict=False)
            ):
                return
            if strm_path.is_file():
                strm_path.unlink()
                logger.info(f"洗版清理旧 STRM：{strm_path}")
        except (OSError, ValueError) as error:
            logger.warning(f"洗版清理旧 STRM 失败：{old_dir}/{old_name}，{error}")

    def _replace_upgrade_file(
            self,
            item: Dict[str, Any],
            pending_key: str,
            target_file: Any,
            staging_dir: str,
            file_name: str,
            now: float,
            directory_snapshot,
    ) -> Optional[Any]:
        """将新文件移入目标位置；旧文件仅临时避让，等待最终提交删除。"""
        final_dir = str(item.get("cloud_dir") or "/").rstrip("/") or "/"
        old_dir = str(item.get("upgrade_old_cloud_dir") or final_dir).rstrip("/") or "/"
        old_name = str(item.get("upgrade_old_file_name") or "").strip()
        old_id = str(item.get("upgrade_old_file_id") or "").strip()
        backup_name = str(item.get("upgrade_backup_name") or "").strip()
        old_file = None
        if old_id or old_name:
            old_valid, old_index = directory_snapshot(old_dir)
            if old_valid:
                old_file = next(
                    (value for value in old_index.values()
                     if old_id and str(getattr(value, "id", "")) == old_id),
                    None,
                )
                old_file = old_file or old_index.get(old_name)

        if not item.get("upgrade_old_backed_up") and old_file and (
                str(getattr(old_file, "id", "")) != str(getattr(target_file, "id", ""))
        ):
            backup_name = backup_name or (
                self._upgrade_backup_name(
                    old_name, item.get("task_id") or pending_key
                )
            )
            logger.info(
                f"洗版临时备份旧文件：{old_dir}/{old_name} -> {backup_name}"
            )
            if not self._cloud_mutations.rename_file(old_dir, old_file, backup_name):
                logger.warning(f"洗版替换无法备份旧文件：{old_dir}/{old_name}")
                return None
            item["upgrade_old_backed_up"] = True
            item["upgrade_backup_name"] = backup_name
            item["upgrade_old_file_id"] = str(getattr(old_file, "id", "") or old_id)

        if not item.get("moved_at"):
            if target_file.name != file_name:
                if not self._cloud_mutations.rename_file(staging_dir, target_file, file_name):
                    if item.get("upgrade_old_backed_up") and old_file:
                        if self._cloud_mutations.rename_file(old_dir, old_file, old_name):
                            item.pop("upgrade_old_backed_up", None)
                        else:
                            logger.error(
                                f"洗版新文件重命名失败且旧文件恢复失败：{old_dir}/{backup_name}"
                            )
                    self._schedule_finalize_retry(item, now)
                    return None
                item["staging_name"] = file_name
                target_file = self._cloud_query.get_cached_file(staging_dir, file_name)
                if not target_file:
                    self._schedule_finalize_retry(item, now)
                    return None
            moved_file = (
                target_file if staging_dir == final_dir
                else self._cloud_mutations.move_file(target_file, final_dir, file_name)
            )
            if not moved_file:
                if item.get("upgrade_old_backed_up") and old_file:
                    if self._cloud_mutations.rename_file(old_dir, old_file, old_name):
                        item.pop("upgrade_old_backed_up", None)
                    else:
                        logger.error(
                            f"洗版新文件移动失败且旧文件恢复失败：{old_dir}/{backup_name}"
                        )
                self._schedule_finalize_retry(item, now)
                return None
            item["moved_at"] = now
            target_file = moved_file

        return target_file

    def _upgrade_old_file_id(
            self,
            item: Dict[str, Any],
            directory_snapshot,
    ) -> str:
        """解析已备份旧文件的真实 ID，供单个或批量回收复用。"""
        if item.get("upgrade_old_deleted") or not item.get("upgrade_old_backed_up"):
            return ""
        old_dir = str(
            item.get("upgrade_old_cloud_dir") or item.get("cloud_dir") or "/"
        ).rstrip("/") or "/"
        backup_name = str(item.get("upgrade_backup_name") or "").strip()
        backup_id = str(item.get("upgrade_old_file_id") or "").strip()
        if backup_id:
            return backup_id
        if not backup_name:
            return ""
        _, backup_index = directory_snapshot(old_dir)
        backup_file = backup_index.get(backup_name)
        return str(getattr(backup_file, "id", "") or "") if backup_file else ""

    def _delete_upgrade_old_file(
            self,
            item: Dict[str, Any],
            directory_snapshot,
    ) -> bool:
        """新文件及 STRM 就绪后，提交洗版并删除临时避让的旧文件。"""
        if item.get("upgrade_old_deleted") or not item.get("upgrade_old_backed_up"):
            return True
        old_dir = str(
            item.get("upgrade_old_cloud_dir") or item.get("cloud_dir") or "/"
        ).rstrip("/") or "/"
        backup_name = str(item.get("upgrade_backup_name") or "").strip()
        backup_id = self._upgrade_old_file_id(item, directory_snapshot)
        if backup_id and self._cloud_mutations.delete_file(backup_id):
            item["upgrade_old_deleted"] = True
        if item.get("upgrade_old_deleted"):
            logger.info(f"洗版完成，已删除旧文件：{old_dir}/{backup_name}")
            return True
        logger.warning(f"洗版新文件已就绪，旧文件删除待重试：{old_dir}/{backup_name}")
        return False

    def monitor_offline_strm_tasks(
            self,
            force: bool = False,
            pending_keys: Optional[Set[str]] = None,
            offline_tasks: Optional[List[Dict[str, Any]]] = None,
            offline_tasks_valid: Optional[bool] = None,
    ) -> Dict[str, int]:
        """检查离线下载和网盘文件后处理；手动刷新可立即重试指定任务。"""
        if (
                not self._get_data
                or not self._cloud_directories
                or not self._cloud_query
                or not self._cloud_mutations
        ):
            return {"checked": 0, "completed": 0, "failed": 0, "pending": 0}
        with self._offline_pending_lock:
            pending = self._get_data(self._OFFLINE_PENDING_KEY) or {}
            if not pending:
                return {"checked": 0, "completed": 0, "failed": 0, "pending": 0}

            now = time.time()
            due_keys = self._due_pending_keys(
                pending, now, force=force, pending_keys=pending_keys
            )
            if not due_keys:
                return {"checked": 0, "completed": 0, "failed": 0, "pending": len(pending)}

            monitor_token = uuid.uuid4().hex
            for key in due_keys:
                item = pending[key]
                item["_monitor_token"] = monitor_token
                item["_monitor_until"] = now + self._OFFLINE_MONITOR_LEASE_SECONDS
            self._save_offline_pending(pending)
            pending_snapshot = copy.deepcopy(pending)
        completed = 0
        failed = 0
        finalized_details: List[Dict[str, Any]] = []
        notification_contexts: List[Tuple[Dict[str, Any], str]] = []
        subscription_batches: Dict[Tuple[Any, ...], Dict[str, Any]] = {}
        media_context_cache: Dict[
            Tuple[Any, ...], Tuple[Any, Dict[str, Any]]
        ] = {}
        subscribe_ids = {
            int((pending.get(key) or {}).get("subscribe_id") or 0)
            for key in due_keys
            if int((pending.get(key) or {}).get("subscribe_id") or 0) > 0
        }
        subscribe_cache: Dict[int, Any] = {}
        if subscribe_ids:
            try:
                with SessionFactory() as db:
                    subscribes = db.query(Subscribe).filter(
                        Subscribe.id.in_(sorted(subscribe_ids))
                    ).all()
                    subscribe_cache = {
                        int(subscribe.id): subscribe for subscribe in subscribes
                    }
                for subscribe_id in subscribe_ids - set(subscribe_cache):
                    subscribe_cache[subscribe_id] = None
            except Exception as error:
                logger.debug(f"批量读取后处理订阅失败，将按需查询：{error}")

        def queue_subscription_completion(
                item: Dict[str, Any], media, media_data: Dict[str, Any]
        ) -> None:
            if item.get("transient_target"):
                return
            episode_values = (
                    item.get("success_episodes")
                    or item.get("notification_episodes")
                    or ([item.get("episode")] if item.get("episode") else [])
            )
            episodes = set()
            for value in episode_values:
                try:
                    episode = int(value or 0)
                except (TypeError, ValueError):
                    continue
                if episode > 0:
                    episodes.add(episode)
            if not media or not episodes:
                return
            key = (
                int(item.get("subscribe_id") or 0),
                int(getattr(media, "tmdb_id", 0) or 0),
                int(item.get("season") or 0),
                str(item.get("task_type") or "share").strip().lower(),
            )
            batch = subscription_batches.setdefault(key, {
                "item": copy.deepcopy(item),
                "mediainfo": media,
                "media_data": dict(media_data or {}),
                "episodes": set(),
            })
            batch["episodes"].update(episodes)

        if due_keys:

            needs_offline = any(
                str((pending.get(key) or {}).get("task_type") or "share")
                in {"ed2k", "magnet"}
                for key in due_keys
            )
            tasks = offline_tasks
            if needs_offline and tasks is None and self._offline_tasks:
                snapshot = self._offline_tasks.get_offline_task_list_snapshot(
                    force=True,
                )
                tasks = snapshot.get("tasks") or []
                offline_tasks_valid = bool(snapshot.get("refresh_ok"))
            tasks_valid = bool(offline_tasks_valid)
            task_map = {
                str(task.get("id") or "").upper(): task
                for task in (tasks or [])
                if task.get("id")
            }
            directory_snapshots: Dict[str, Tuple[bool, Dict[str, Any]]] = {}
            due_positions = {
                pending_key: index
                for index, pending_key in enumerate(due_keys, 1)
            }

            def update_progress(
                    item: Dict[str, Any],
                    pending_key: str,
                    step: str,
                    detail: str = "",
            ) -> None:
                self._update_postprocess_progress(
                    item,
                    step,
                    due_positions.get(pending_key, 1),
                    len(due_keys),
                    detail,
                )

            def directory_snapshot(cloud_dir: str) -> Tuple[bool, Dict[str, Any]]:
                normalized_dir = str(cloud_dir or "").rstrip("/")
                if normalized_dir in directory_snapshots:
                    return directory_snapshots[normalized_dir]
                lookup = self._cloud_directories.resolve_directory(normalized_dir)
                if not lookup.checked:
                    result = (False, {})
                elif lookup.directory_id is None:
                    result = (True, {})
                else:
                    listing = self._cloud_directories.list_directory(
                        lookup.directory_id
                    )
                    if not listing.checked:
                        result = (False, {})
                    else:
                        file_index = {}
                        for file_item in listing.files:
                            if file_item.name:
                                file_index[file_item.name] = file_item
                        result = (True, file_index)
                directory_snapshots[normalized_dir] = result
                return result

            upgrade_delete_batch: Dict[str, Dict[str, Any]] = {}

            def finish_finalized_item(
                    item: Dict[str, Any],
                    pending_key: str,
                    strm_path,
                    media,
                    media_data: Dict[str, Any],
            ) -> None:
                nonlocal completed
                update_progress(
                    item, pending_key, "commit", "更新历史和订阅进度"
                )
                if item.get("upgrade") and str(
                        item.get("upgrade_mode") or self._upgrade_mode
                ) != "coexist":
                    self._delete_upgrade_old_strm(
                        item, replacement_path=strm_path
                    )
                queue_subscription_completion(item, media, media_data)
                detail = self._notify_pending_file_finalized(
                    item,
                    pending_key,
                    strm_path,
                    mediainfo=media,
                    media_data=media_data,
                    finish_subscription=media is None,
                    subscribe_cache=subscribe_cache,
                )
                # 单项后处理完成后立即提交历史终态并将通知入队，不能等本轮
                # pending 扫描结束，否则中途停止会丢失已完成项的通知。
                self._mark_offline_history_status(pending_key, "成功")
                if detail:
                    finalized_details.append(detail)
                    notification_contexts.append((item, pending_key))
                logger.debug(
                    f"文件后处理完成"
                    f"{'并生成 STRM' if strm_path else ''}："
                    f"{strm_path or item.get('file_name') or pending_key}"
                )
                pending.pop(pending_key, None)
                completed += 1

            def finalize_ready_item(
                    item: Dict[str, Any],
                    pending_key: str,
                    strm_path,
                    media,
                    media_data: Dict[str, Any],
            ) -> None:
                is_replacement = item.get("upgrade") and str(
                    item.get("upgrade_mode") or self._upgrade_mode
                ) != "coexist"
                if is_replacement and self._cloud_batch_mutations:
                    backup_id = self._upgrade_old_file_id(item, directory_snapshot)
                    if backup_id:
                        upgrade_delete_batch[pending_key] = {
                            "item": item,
                            "file_id": backup_id,
                            "strm_path": strm_path,
                            "media": media,
                            "media_data": media_data,
                        }
                        return
                if is_replacement and not self._delete_upgrade_old_file(
                        item, directory_snapshot
                ):
                    if self._finalize_failure(item, pending_key):
                        return
                    self._schedule_finalize_retry(item, now)
                    return
                finish_finalized_item(
                    item, pending_key, strm_path, media, media_data
                )

            prepared_files: Dict[str, Any] = {}
            moved_files: Dict[str, Any] = {}
            if self._cloud_batch_mutations:
                # 洗版先批量避让旧文件，再让新文件进入统一重命名、移动批次。
                # 单项失败仍由后续逐项流程恢复旧文件并安排重试。
                upgrade_backup_groups: Dict[str, Dict[str, Dict[str, Any]]] = {}
                for pending_key in due_keys:
                    item = pending.get(pending_key) or {}
                    task_type = str(item.get("task_type") or "share")
                    task = task_map.get(
                        str(item.get("task_id") or pending_key).upper()
                    )
                    if (
                            task_type == "magnet"
                            or (task_type == "ed2k" and not bool(
                        task and task.get("completed")
                    ))
                            or not item.get("upgrade")
                            or str(item.get("upgrade_mode") or self._upgrade_mode)
                            == "coexist"
                            or item.get("upgrade_old_backed_up")
                    ):
                        continue
                    old_dir = str(
                        item.get("upgrade_old_cloud_dir")
                        or item.get("cloud_dir") or "/"
                    ).rstrip("/") or "/"
                    old_name = str(item.get("upgrade_old_file_name") or "").strip()
                    old_id = str(item.get("upgrade_old_file_id") or "").strip()
                    directory_valid, old_index = directory_snapshot(old_dir)
                    if not directory_valid:
                        continue
                    old_file = next(
                        (
                            value for value in old_index.values()
                            if old_id and str(getattr(value, "id", "")) == old_id
                        ),
                        None,
                    ) or old_index.get(old_name)
                    if not old_file:
                        continue
                    backup_name = self._upgrade_backup_name(
                        old_name, item.get("task_id") or pending_key
                    )
                    upgrade_backup_groups.setdefault(old_dir, {})[pending_key] = {
                        "item": old_file,
                        "target_name": backup_name,
                    }
                    item["upgrade_backup_name"] = backup_name

                for old_dir, rename_items in upgrade_backup_groups.items():
                    renamed = self._cloud_batch_mutations.rename_files(
                        old_dir, rename_items
                    )
                    for pending_key, backup_file in renamed.items():
                        item = pending.get(pending_key)
                        if not item:
                            continue
                        item["upgrade_old_backed_up"] = True
                        item["upgrade_old_file_id"] = str(
                            getattr(backup_file, "id", "")
                            or item.get("upgrade_old_file_id") or ""
                        )
                    if renamed:
                        directory_snapshots.pop(old_dir, None)

                rename_groups: Dict[str, Dict[str, Dict[str, Any]]] = {}
                for pending_key in due_keys:
                    item = pending.get(pending_key) or {}
                    task_type = str(item.get("task_type") or "share")
                    is_replacement = item.get("upgrade") and str(
                        item.get("upgrade_mode") or self._upgrade_mode
                    ) != "coexist"
                    if (
                            task_type == "magnet"
                            or item.get("moved_at")
                            or (is_replacement and not item.get("upgrade_old_backed_up"))
                    ):
                        continue
                    task = task_map.get(
                        str(item.get("task_id") or pending_key).upper()
                    )
                    if task_type == "ed2k" and not bool(
                            task and task.get("completed")
                    ):
                        continue
                    staging_dir = str(
                        item.get("staging_dir") or item.get("cloud_dir") or "/"
                    ).rstrip("/") or "/"
                    directory_valid, file_index = directory_snapshot(staging_dir)
                    if not directory_valid:
                        continue
                    staging_name = str(
                        item.get("staging_name") or item.get("file_name") or ""
                    )
                    task_name = str((task or {}).get("name") or "").strip()
                    source_file = file_index.get(staging_name) or file_index.get(task_name)
                    source_sha1 = str(item.get("source_sha1") or "").upper()
                    if not source_file and len(source_sha1) == 40:
                        source_file = next(
                            (
                                candidate for candidate in file_index.values()
                                if str(candidate.sha1 or "").upper() == source_sha1
                            ),
                            None,
                        )
                    if source_file:
                        rename_groups.setdefault(staging_dir, {})[pending_key] = {
                            "item": source_file,
                            "target_name": str(item.get("file_name") or pending_key),
                        }

                for staging_dir, rename_items in rename_groups.items():
                    first_key = next(iter(rename_items), "")
                    first_item = pending.get(first_key) or {}
                    if first_item:
                        update_progress(
                            first_item,
                            first_key,
                            "organize",
                            f"批量重命名 {len(rename_items)} 个文件",
                        )
                    renamed = self._cloud_batch_mutations.rename_files(
                        staging_dir, rename_items
                    )
                    for pending_key, target_file in renamed.items():
                        item = pending.get(pending_key)
                        if not item:
                            continue
                        item["staging_name"] = item["file_name"]
                        prepared_files[pending_key] = target_file

                move_groups: Dict[str, Dict[str, Any]] = {}
                for pending_key, target_file in prepared_files.items():
                    item = pending.get(pending_key) or {}
                    staging_dir = str(item.get("staging_dir") or "/").rstrip("/") or "/"
                    final_dir = str(item.get("cloud_dir") or "/").rstrip("/") or "/"
                    if staging_dir == final_dir:
                        item["moved_at"] = now
                        moved_files[pending_key] = target_file
                        continue
                    move_groups.setdefault(final_dir, {})[pending_key] = target_file

                for final_dir, move_items in move_groups.items():
                    first_key = next(iter(move_items), "")
                    first_item = pending.get(first_key) or {}
                    if first_item:
                        update_progress(
                            first_item,
                            first_key,
                            "organize",
                            f"批量移动 {len(move_items)} 个文件",
                        )
                    moved = self._cloud_batch_mutations.move_files(
                        move_items, final_dir
                    )
                    for pending_key, target_file in moved.items():
                        item = pending.get(pending_key)
                        if not item:
                            continue
                        item["moved_at"] = now
                        moved_files[pending_key] = target_file

            def finalize_after_metadata(
                    item: Dict[str, Any],
                    pending_key: str,
                    file_name: str,
                    strm_path,
                    media,
                    media_data: Dict[str, Any],
            ) -> None:
                if (
                        media
                        and self._organize_after_transfer
                        and self._metadata_scraper
                        and self._local_resource_path
                        and (self._nfo_scrape_enabled or self._image_scrape_enabled)
                ):
                    update_progress(
                        item, pending_key, "metadata", "刮削当前文件元数据"
                    )
                    self._scrape_metadata_batch([{
                        "cloud_dir": item["cloud_dir"],
                        "file_name": file_name,
                        "notification_episodes": (
                            [item.get("episode")] if item.get("episode") else []
                        ),
                    }], media, season=item.get("season"))
                finalize_ready_item(
                    item, pending_key, strm_path, media, media_data
                )

            for pending_key in due_keys:
                item = pending.get(pending_key)
                if not item:
                    continue
                if item.get("finalize_dead"):
                    fail_count = int(item.get("fail_count") or 0)
                    reason = self._FINALIZE_DEAD_REASON.format(fail_count)
                    logger.error(
                        "{}：{}".format(
                            reason,
                            str(item.get("file_name") or pending_key),
                        )
                    )
                    self._mark_offline_history_status(pending_key, "失败", reason)
                    self._notify_finalize_dead(item, pending_key)
                    pending.pop(pending_key, None)
                    failed += 1
                    continue
                task_type = str(item.get("task_type") or "share")
                file_name = str(item.get("file_name") or pending_key)
                created_at = float(item.get("created_at") or now)
                target_file = None
                update_progress(
                    item, pending_key, "locate", "检查下载和文件就绪状态"
                )
                if task_type == "magnet":
                    task = task_map.get(str(item.get("task_id") or "").upper())
                    task_done = bool(task and task.get("completed"))
                    if task and not task_done:
                        self._persist_offline_progress(item, task)
                    if task and bool(task.get("failed")):
                        reason = "Magnet 离线下载失败"
                        self._add_offline_blacklist(item.get("share_url") or item.get("task_id"), reason)
                        self._cleanup_failed_offline_task(item, reason)
                        self._mark_offline_history_status(pending_key, "失败", reason)
                        pending.pop(pending_key, None)
                        failed += 1
                        continue
                    if not task_done:
                        if now - created_at >= self._OFFLINE_TIMEOUT:
                            verdict = self._offline_timeout_file_verdict(
                                item, now, directory_snapshot,
                                subscribe_cache=subscribe_cache,
                            )
                            if verdict == "defer":
                                self._schedule_finalize_retry(item, now)
                                continue
                            if verdict == "ready":
                                task_done = True
                            elif self._offline_timeout_should_defer(
                                tasks_valid, verdict
                            ):
                                logger.warning(
                                    f"接口异常，暂缓判定：{file_name}"
                                )
                                self._schedule_finalize_retry(item, now)
                                continue
                            else:
                                reason = "Magnet 离线下载超过 30 分钟未完成，已退出"
                                self._add_offline_blacklist(item.get("share_url") or item.get("task_id"), reason)
                                self._cleanup_failed_offline_task(item, reason)
                                self._mark_offline_history_status(pending_key, "失败", reason)
                                pending.pop(pending_key, None)
                                failed += 1
                                continue
                        else:
                            self._schedule_finalize_retry(item, now)
                        continue
                    update_progress(
                        item, pending_key, "organize", "整理 Magnet 下载文件"
                    )
                    finalized = self._finalize_magnet_package(
                        item, pending_key, subscribe_cache=subscribe_cache
                    )
                    if finalized is None:
                        if self._finalize_failure(item, pending_key):
                            continue
                        self._schedule_finalize_retry(item, now)
                        continue
                    pending.pop(pending_key, None)
                    if finalized:
                        # Magnet 一个离线任务可能匹配多个真实文件；该任务的
                        # 历史已在 _finalize_magnet_package 中持久化，立即入队。
                        update_progress(
                            item, pending_key, "commit", "登记文件和通知结果"
                        )
                        finalized_details.extend(finalized)
                        notification_contexts.append((item, pending_key))
                        completed += len(finalized)
                    else:
                        self._add_offline_blacklist(item.get("share_url") or item.get("task_id"),
                                                    "Magnet 下载完成但未匹配到目标媒体文件")
                        failed += 1
                    continue
                if task_type == "ed2k":
                    task = task_map.get(str(item.get("task_id") or pending_key).upper())
                    task_done = bool(
                        item.get("moved_at") or (task and task.get("completed"))
                    )
                    if task and bool(task.get("failed")):
                        reason = "离线下载失败"
                        logger.error(f"{reason}：{file_name}")
                        self._add_offline_blacklist(item.get("share_url") or item.get("task_id"), reason)
                        self._mark_offline_history_status(pending_key, "失败", reason)
                        pending.pop(pending_key, None)
                        failed += 1
                        continue
                    if task is not None and not task_done:
                        if now - created_at >= self._OFFLINE_TIMEOUT:
                            verdict = self._offline_timeout_file_verdict(
                                item, now, directory_snapshot,
                                subscribe_cache=subscribe_cache,
                            )
                            if verdict == "defer":
                                self._schedule_finalize_retry(item, now)
                                continue
                            if verdict == "ready":
                                task_done = True
                                item.setdefault("download_completed_at", now)
                            elif self._offline_timeout_should_defer(
                                tasks_valid, verdict
                            ):
                                logger.warning(
                                    f"接口异常，暂缓判定：{file_name}"
                                )
                                self._schedule_finalize_retry(item, now)
                                continue
                            else:
                                reason = "115 离线下载超过 30 分钟未完成，已退出"
                                logger.error(f"{reason}：{file_name}")
                                self._add_offline_blacklist(item.get("share_url") or item.get("task_id"), reason)
                                self._mark_offline_history_status(pending_key, "失败", reason)
                                pending.pop(pending_key, None)
                                failed += 1
                                continue
                            continue
                        self._persist_offline_progress(item, task)
                        self._schedule_finalize_retry(item, now)
                        continue
                    if not task_done and task is None and tasks_valid:
                        staging_dir = str(
                            item.get("staging_dir") or item.get("cloud_dir") or "/"
                        )
                        directory_valid, file_index = directory_snapshot(staging_dir)
                        if directory_valid and not file_index:
                            reason = "离线任务及目标文件均不存在"
                            logger.warning(f"{reason}：{file_name}")
                            self._add_offline_blacklist(item.get("share_url") or item.get("task_id"), reason)
                            self._mark_offline_history_status(pending_key, "失败", reason)
                            pending.pop(pending_key, None)
                            failed += 1
                            continue
                    if not task_done:
                        if now - created_at >= self._OFFLINE_TIMEOUT:
                            verdict = self._offline_timeout_file_verdict(
                                item, now, directory_snapshot,
                                subscribe_cache=subscribe_cache,
                            )
                            if verdict == "defer":
                                self._schedule_finalize_retry(item, now)
                                continue
                            if verdict == "ready":
                                task_done = True
                                item.setdefault("download_completed_at", now)
                            elif self._offline_timeout_should_defer(
                                tasks_valid, verdict
                            ):
                                logger.warning(
                                    f"接口异常，暂缓判定：{file_name}"
                                )
                                self._schedule_finalize_retry(item, now)
                                continue
                            else:
                                reason = "115 离线下载超过 30 分钟未完成，已退出"
                                logger.error(f"{reason}：{file_name}")
                                self._add_offline_blacklist(item.get("share_url") or item.get("task_id"), reason)
                                self._mark_offline_history_status(pending_key, "失败", reason)
                                pending.pop(pending_key, None)
                                failed += 1
                                continue
                            continue
                        self._schedule_finalize_retry(item, now)
                        continue
                    item.setdefault("download_completed_at", now)

                already_moved = bool(item.get("moved_at"))
                staging_dir = str(
                    item.get("cloud_dir") if already_moved
                    else item.get("staging_dir") or item.get("cloud_dir") or "/"
                ).rstrip("/") or "/"
                staging_name = (
                    file_name if already_moved
                    else str(item.get("staging_name") or file_name)
                )
                update_progress(
                    item,
                    pending_key,
                    "locate",
                    f"在 {staging_dir} 定位 {staging_name}",
                )
                target_file = moved_files.get(pending_key) or prepared_files.get(pending_key)
                if target_file:
                    file_index = {}
                else:
                    directory_valid, file_index = directory_snapshot(staging_dir)
                    if not directory_valid:
                        if self._finalize_failure(item, pending_key):
                            continue
                        self._schedule_finalize_retry(item, now)
                        continue
                task_name = (
                    str((task or {}).get("name") or "").strip()
                    if task_type == "ed2k"
                    else ""
                )
                target_file = (
                        target_file
                        or file_index.get(staging_name)
                        or file_index.get(task_name)
                )
                source_sha1 = str(item.get("source_sha1") or "").upper()
                if not target_file and source_sha1:
                    target_file = next(
                        (
                            candidate for candidate in file_index.values()
                            if str(candidate.sha1 or "").upper() == source_sha1
                        ),
                        None,
                    )
                if not target_file and not already_moved:
                    final_dir = str(item.get("cloud_dir") or "/").rstrip("/") or "/"
                    if final_dir != staging_dir:
                        final_valid, final_index = directory_snapshot(final_dir)
                        if final_valid:
                            final_candidate = final_index.get(file_name)
                            candidate_sha1 = str(
                                getattr(final_candidate, "sha1", "") or ""
                            ).upper()
                            if (
                                    final_candidate
                                    and source_sha1
                                    and candidate_sha1 != source_sha1
                            ):
                                final_candidate = None
                            if not final_candidate and source_sha1:
                                final_candidate = next(
                                    (
                                        candidate for candidate in final_index.values()
                                        if str(candidate.sha1 or "").upper() == source_sha1
                                    ),
                                    None,
                                )
                            if final_candidate:
                                target_file = final_candidate
                                item["moved_at"] = now
                                already_moved = True
                                staging_dir = final_dir
                                logger.info(
                                    f"后处理在最终目录找到已移动文件，继续生成STRM："
                                    f"{final_dir}/{file_name}"
                                )
                if not target_file:
                    ready_at = float(item.get("download_completed_at") or created_at)
                    if now - ready_at >= self._FILE_FINALIZE_TIMEOUT:
                        reason = "网盘文件已保存但30分钟内仍无法在转存路径定位"
                        self._mark_offline_history_status(pending_key, "失败", reason)
                        pending.pop(pending_key, None)
                        failed += 1
                    else:
                        self._schedule_finalize_retry(item, now)
                    continue

                if item.get("upgrade") and str(
                        item.get("upgrade_mode") or self._upgrade_mode
                ) != "coexist":
                    update_progress(
                        item, pending_key, "organize", "替换旧版本文件"
                    )
                    replaced_file = self._replace_upgrade_file(
                        item=item,
                        pending_key=pending_key,
                        target_file=target_file,
                        staging_dir=staging_dir,
                        file_name=file_name,
                        now=now,
                        directory_snapshot=directory_snapshot,
                    )
                    if not replaced_file:
                        continue
                    target_file = replaced_file
                    already_moved = True

                if not already_moved and not self._organize_after_transfer:
                    # "转存后整理"关闭：文件停在转存目录即视为完成。
                    item["cloud_dir"] = staging_dir
                    item["moved_at"] = now
                    already_moved = True
                    logger.info(
                        f"转存后整理已关闭，文件保留在转存目录："
                        f"{staging_dir}/{Path(staging_name).name}"
                    )

                if not already_moved:
                    update_progress(
                        item, pending_key, "organize", "重命名并移动到媒体目录"
                    )
                    if target_file.name != file_name:
                        if not self._cloud_mutations.rename_file(
                                staging_dir, target_file, file_name
                        ):
                            self._schedule_finalize_retry(item, now)
                            continue
                        item["staging_name"] = file_name
                        target_file = self._cloud_query.get_cached_file(
                            staging_dir, file_name
                        )
                        if not target_file:
                            self._schedule_finalize_retry(item, now)
                            continue
                    final_dir = str(item["cloud_dir"]).rstrip("/") or "/"
                    if staging_dir != final_dir:
                        moved_file = self._cloud_mutations.move_file(
                            target_file, item["cloud_dir"], file_name
                        )
                    else:
                        moved_file = target_file
                    if not moved_file:
                        self._schedule_finalize_retry(item, now)
                        continue
                    target_file = moved_file
                    item["moved_at"] = now

                context_key = self._media_context_key(item)
                cached_context = (
                    media_context_cache.get(context_key) if context_key else None
                )
                if cached_context:
                    media, media_data = cached_context
                    item["mediainfo"] = media_data
                else:
                    media, media_data = self._restore_pending_media_context(
                        item, pending_key
                    )
                    resolved_key = context_key or self._media_context_key(item)
                    if resolved_key and media:
                        media_context_cache[resolved_key] = (media, media_data)
                if (
                        not self._strm_generate_enabled
                        or not self._strm_generator
                        or not self._local_resource_path
                        or not self._organize_after_transfer
                ):
                    if item.get("subtitles"):
                        update_progress(
                            item, pending_key, "subtitle", "检查并整理伴随字幕"
                        )
                        if not self._finalize_subtitle_files(
                                item, directory_snapshot
                        ):
                            self._schedule_finalize_retry(item, now)
                            continue
                    finalize_after_metadata(
                        item, pending_key, file_name, None, media, media_data
                    )
                    continue

                update_progress(
                    item, pending_key, "strm", "生成并校验 STRM 文件"
                )
                strm_path = self._generate_strm(
                    item["cloud_dir"], file_name, target_file=target_file
                )
                if strm_path and not self._strm_file_ready(strm_path):
                    logger.error(f"STRM 生成后文件不存在或为空：{strm_path}")
                    strm_path = None
                if strm_path:
                    if item.get("subtitles"):
                        update_progress(
                            item, pending_key, "subtitle", "检查并整理伴随字幕"
                        )
                        if not self._finalize_subtitle_files(
                                item, directory_snapshot, strm_path=strm_path
                        ):
                            self._schedule_finalize_retry(item, now)
                            continue
                    if not self._strm_file_ready(strm_path):
                        logger.error(f"洗版后 STRM 文件不存在或为空：{strm_path}")
                        self._schedule_finalize_retry(item, now)
                        continue
                    finalize_after_metadata(
                        item,
                        pending_key,
                        file_name,
                        strm_path,
                        media,
                        media_data,
                    )
                    continue

                ready_at = float(item.get("download_completed_at") or created_at)
                if now - ready_at >= self._FILE_FINALIZE_TIMEOUT:
                    reason = "文件已下载但30分钟内仍无法生成 STRM"
                    self._mark_offline_history_status(pending_key, "失败", reason)
                    pending.pop(pending_key, None)
                    failed += 1
                else:
                    self._schedule_finalize_retry(item, now)

            if upgrade_delete_batch:
                delete_ids = list(dict.fromkeys(
                    value["file_id"] for value in upgrade_delete_batch.values()
                ))
                deleted_ids = {
                    str(file_id) for file_id in
                    self._cloud_batch_mutations.delete_files(delete_ids)
                }
                for pending_key, value in upgrade_delete_batch.items():
                    item = value["item"]
                    if str(value["file_id"]) not in deleted_ids:
                        self._schedule_finalize_retry(item, now)
                        continue
                    item["upgrade_old_deleted"] = True
                    finish_finalized_item(
                        item,
                        pending_key,
                        value["strm_path"],
                        value["media"],
                        value["media_data"],
                    )
                success_count = len(deleted_ids & set(map(str, delete_ids)))
                total_count = len(delete_ids)
                logger.info(
                    f"洗版旧文件批量回收完成：成功 {success_count}/{total_count} 个"
                )
                if success_count < total_count:
                    logger.warning(
                        f"洗版旧文件有 {total_count - success_count} 个回收失败，"
                        "已保留后处理任务等待重试"
                    )

            for batch in subscription_batches.values():
                completion_item = batch["item"]
                completion_item["success_episodes"] = sorted(batch["episodes"])
                completion_item["notification_episodes"] = sorted(
                    batch["episodes"]
                )
                self._finish_pending_subscription(
                    completion_item,
                    batch["media_data"],
                    mediainfo=batch["mediainfo"],
                )

            if finalized_details:
                if self._notify and notification_contexts:
                    progress_item, progress_key = notification_contexts[-1]
                    update_progress(
                        progress_item,
                        progress_key,
                        "notify",
                        f"汇总发送 {len(finalized_details)} 个文件的完成通知",
                    )
                self._send_finalized_batch(finalized_details)

        with self._offline_pending_lock:
            current_pending = self._get_data(self._OFFLINE_PENDING_KEY) or {}
            for pending_key in due_keys:
                original_item = pending_snapshot.get(pending_key)
                processed_item = pending.get(pending_key)
                current_item = current_pending.get(pending_key)
                if current_item is None or original_item is None:
                    continue
                if current_item.get("_monitor_token") != monitor_token:
                    continue
                generation = (
                    original_item.get("created_at"),
                    original_item.get("share_url"),
                    original_item.get("file_name"),
                    original_item.get("task_type"),
                )
                current_generation = (
                    current_item.get("created_at"),
                    current_item.get("share_url"),
                    current_item.get("file_name"),
                    current_item.get("task_type"),
                )
                if generation != current_generation:
                    continue
                if processed_item is None:
                    current_pending.pop(pending_key, None)
                    continue
                processed_item.pop("_monitor_token", None)
                processed_item.pop("_monitor_until", None)
                for field in set(original_item) | set(processed_item):
                    if original_item.get(field) == processed_item.get(field):
                        continue
                    if field in processed_item:
                        current_item[field] = copy.deepcopy(processed_item[field])
                    else:
                        current_item.pop(field, None)
            self._save_offline_pending(current_pending)
            pending_count = len(current_pending)
        result = {
            "checked": len(due_keys),
            "completed": completed,
            "failed": failed,
            "pending": pending_count,
        }
        task_ids = {
            self._postprocess_task_id(item)
            for pending_key in due_keys
            if (item := pending_snapshot.get(pending_key))
               and self._postprocess_task_id(item)
        }
        if self._task_update:
            for task_id in task_ids:
                self._task_update(
                    task_id,
                    postprocess_active=False,
                    postprocess_detail="",
                )
        self._notify_offline_pending_changed(result["pending"])
        return result

    def _finalize_magnet_package(
            self,
            item: Dict[str, Any],
            pending_key: str,
            subscribe_cache: Optional[Dict[int, Any]] = None,
    ) -> Optional[List[Dict[str, Any]]]:
        """读取完成后的真实文件树，只移动实际匹配的媒体文件。"""
        mediainfo, media_data = self._restore_pending_media_context(item, pending_key)
        if not mediainfo:
            self._cleanup_failed_offline_task(item, "媒体元数据不存在")
            return []
        subscribe_id = int(item.get("subscribe_id") or 0)
        subscribe = (
            subscribe_cache.get(subscribe_id)
            if subscribe_cache is not None and subscribe_id in subscribe_cache
            else None
        )
        if subscribe_id and (
                subscribe_cache is None or subscribe_id not in subscribe_cache
        ):
            with SessionFactory() as db:
                subscribe = SubscribeOper(db=db).get(subscribe_id)
        if not subscribe and item.get("transient_target"):
            subscribe = SimpleNamespace(**(item.get("target_subscribe") or {}))
        if not subscribe:
            self._cleanup_failed_offline_task(item, "订阅已不存在")
            self._mark_offline_history_status(
                pending_key, "失败", "Magnet 下载完成时订阅已不存在"
            )
            return []

        files = self._cloud_query.list_files_recursive(item.get("cloud_dir"), max_depth=6)
        video_files = [
            file_item for file_item in files
            if MediaFileParser.is_video(str(file_item.get("name") or ""))
        ]
        if not video_files:
            logger.debug(f"Magnet 已完成但真实文件树尚未就绪：{item.get('file_name')}")
            return None

        def directory_snapshot(cloud_dir: str) -> Tuple[bool, Dict[str, Any]]:
            lookup = self._cloud_directories.resolve_directory(cloud_dir)
            if not lookup.checked:
                return False, {}
            if lookup.directory_id is None:
                return True, {}
            listing = self._cloud_directories.list_directory(lookup.directory_id)
            if not listing.checked:
                return False, {}
            return True, {value.name: value for value in listing.files if value.name}

        matched: List[Tuple[Optional[int], Any, int]] = []
        season = item.get("season")
        if mediainfo.type == MediaType.TV:
            target_episodes = []
            for value in item.get("target_episodes") or []:
                try:
                    episode = int(str(value or "0"))
                except ValueError:
                    continue
                if episode > 0:
                    target_episodes.append(episode)
            episode_files = self._match_episode_files(
                video_files,
                mediainfo,
                subscribe,
                max(1, int(season or 1)),
                target_episodes,
            )
            matched = [
                (episode, episode_files[episode][0], episode_files[episode][1])
                for episode in target_episodes
                if episode_files.get(episode, (None, 0))[0]
            ]
        else:
            movie_file, movie_score = self._match_movie_file(
                video_files, mediainfo, subscribe
            )
            if movie_file:
                matched = [(None, movie_file, movie_score)]

        if not matched:
            reason = "Magnet 下载完成，但真实文件名未匹配当前订阅"
            logger.warning(f"{reason}：{item.get('file_name')}")
            self._cleanup_failed_offline_task(item, reason)
            self._mark_offline_history_status(pending_key, "失败", reason)
            return []

        history_records = []
        details = []
        success_episodes = []
        resource = item.get("resource") or {}
        share_url = str(item.get("share_url") or "")
        upgrade_baseline = item.get("upgrade_baseline") or {}
        for episode, source_file, current_score in matched:
            source_name = str(source_file.get("name") or "")
            baseline_key = str(episode) if episode else "movie"
            old_baseline = upgrade_baseline.get(baseline_key) or {}
            is_upgrade = bool(old_baseline)
            source_size = self._resource_size_bytes(source_file.get("size"))
            if is_upgrade:
                should_upgrade, reason = self._should_upgrade_candidate(
                    int(old_baseline.get("score") or 0),
                    current_score,
                    int(old_baseline.get("size") or 0),
                    source_size,
                )
                if not should_upgrade:
                    label = f"E{int(episode):02d}" if episode else mediainfo.title
                    logger.info(f"Magnet 下载后洗版候选跳过 {label}：{reason}")
                    continue
            cloud_dir, target_name = self._platform_target(
                self._CLOUD_MEDIA_ROOT,
                subscribe,
                mediainfo,
                source_name,
                season=max(1, int(season or 1)) if episode else None,
                episode=episode,
            )
            mode = str(item.get("upgrade_mode") or self._upgrade_mode)
            organize_enabled = self._organize_after_transfer
            if is_upgrade and mode == "coexist":
                target_name = self._coexist_target_name(
                    target_name, source_name, source_size, source_file.get("sha1") or ""
                )
            if is_upgrade and mode != "coexist":
                old_dir = str(old_baseline.get("cloud_dir") or "").strip()
                old_name = str(old_baseline.get("file_name") or "").strip()
                old_file_id = str(old_baseline.get("file_id") or "").strip()
                if not old_dir or not old_name:
                    old_dir, old_name = self._platform_target(
                        self._CLOUD_MEDIA_ROOT,
                        subscribe,
                        mediainfo,
                        old_name or source_name,
                        season=max(1, int(season or 1)) if episode else None,
                        episode=episode,
                    )
                if not old_file_id:
                    old_file = self._cloud_query.get_cached_file(old_dir, old_name)
                    old_file_id = str(getattr(old_file, "id", "") or "")
                replace_item = {
                    **item,
                    "cloud_dir": cloud_dir,
                    "file_name": target_name,
                    "upgrade_old_cloud_dir": old_dir,
                    "upgrade_old_file_name": old_name,
                    "upgrade_old_file_id": old_file_id,
                }
                source_dir = str(
                    (getattr(source_file, "native", None) or {}).get("_cloud_dir")
                    or item.get("cloud_dir") or "/"
                ).rstrip("/") or "/"
                moved = self._replace_upgrade_file(
                    replace_item,
                    f"{pending_key}:{baseline_key}",
                    source_file,
                    source_dir,
                    target_name,
                    time.time(),
                    directory_snapshot,
                )
            else:
                source_dir = str(
                    (getattr(source_file, "native", None) or {}).get("_cloud_dir")
                    or item.get("cloud_dir") or "/"
                ).rstrip("/") or "/"
                if organize_enabled:
                    moved = self._cloud_mutations.move_file(
                        source_file, cloud_dir, target_name
                    )
                else:
                    moved = source_file
                    if moved.name != target_name:
                        if not self._cloud_mutations.rename_file(
                                source_dir, moved, target_name
                        ):
                            continue
                        moved = self._cloud_query.get_cached_file(
                            source_dir, target_name
                        ) or moved
            if not moved:
                continue
            if organize_enabled:
                self._scrape_metadata(
                    cloud_dir,
                    target_name,
                    mediainfo,
                    season=season,
                    episode=episode,
                )
            strm_path = None
            if self._strm_generate_enabled and self._strm_generator and self._local_resource_path:
                strm_path = self._generate_strm(
                    cloud_dir, target_name, target_file=moved, lookup_target=False
                )
                if strm_path:
                    if is_upgrade and mode != "coexist":
                        if not self._delete_upgrade_old_file(
                                replace_item, directory_snapshot
                        ):
                            logger.warning(
                                f"Magnet 洗版旧文件删除失败：{target_name}"
                            )
                            continue
                        self._delete_upgrade_old_strm(
                            replace_item, replacement_path=strm_path
                        )
                    self._media_server_notifier.notify(
                        path=strm_path, mediainfo=mediainfo, file_name=target_name
                    )
            elif self._local_resource_path:
                if is_upgrade and mode != "coexist":
                    if not self._delete_upgrade_old_file(
                            replace_item, directory_snapshot
                    ):
                        logger.warning(
                            f"Magnet 洗版旧文件删除失败：{target_name}"
                        )
                        continue
                    self._delete_upgrade_old_strm(replace_item)
                notify_path = self._resolve_resource_season_dir(
                    self._local_resource_path,
                    subscribe,
                    mediainfo,
                    max(1, int(season or 1)),
                )
                if notify_path:
                    self._media_server_notifier.notify(
                        path=notify_path, mediainfo=mediainfo, file_name=target_name
                    )
            if episode:
                success_episodes.append(int(episode))
            else:
                success_episodes.append(1)
            episode_fields = (
                {"season": int(season or 1), "episode": int(episode)}
                if episode else {}
            )
            record = self._build_transfer_history_item(
                mediainfo=mediainfo,
                subscribe=subscribe,
                status="成功",
                share_url=share_url,
                file_name=target_name,
                source_file_name=source_name,
                cloud_dir=cloud_dir,
                resource=resource,
                file_size=source_size,
                source_sha1=str(source_file.get("sha1") or ""),
                rule_score=current_score,
                upgrade=is_upgrade,
                **episode_fields,
            )
            history_records.append(record)
            detail = {
                "type": record["type"],
                "title": mediainfo.title,
                "year": mediainfo.year,
                "image": mediainfo.get_poster_image(),
                "file_name": target_name,
            }
            if episode:
                detail.update({"season": int(season or 1), "episodes": [int(episode)]})
            details.append(detail)

        if not history_records:
            return None
        persisted_records = [
            record for record in history_records
            if not record.get("skip_history")
        ]
        with self._offline_pending_lock:
            history = [
                record for record in (self._get_data("history") or [])
                if str(record.get("finalize_key") or "") != pending_key
            ]
            history.extend(persisted_records)
            self._save_data("history", history)
        self._record_platform_transfer_histories(persisted_records)
        item["success_episodes"] = (
            [] if item.get("transient_target") else success_episodes
        )
        item["notification_episodes"] = success_episodes if mediainfo.type == MediaType.TV else []
        if not item.get("transient_target"):
            self._finish_pending_subscription(
                item, media_data, mediainfo=mediainfo
            )
        logger.info(
            f"Magnet 下载后文件匹配完成：移动 {len(history_records)} 个文件，"
            f"未匹配内容保留在隔离目录"
        )
        return details

    def _finalize_failure(self, item: Dict[str, Any], pending_key: str) -> bool:
        """Record one real finalize failure (rename/move/locate misses only).

        Returns True once consecutive failures reach the limit: the item is
        flagged dead and the next monitor scan terminates it (history +
        notify + dequeue) instead of retrying forever.
        """
        fail_count = int(item.get("fail_count") or 0) + 1
        item["fail_count"] = fail_count
        if fail_count < self._FINALIZE_MAX_FAILURES:
            return False
        item["finalize_dead"] = True
        item["next_check_at"] = 0.0
        logger.error(
            self._FINALIZE_DEAD_LOG.format(
                fail_count, str(item.get("file_name") or pending_key)
            )
        )
        return True

    def _notify_finalize_dead(
            self, item: Dict[str, Any], pending_key: str
    ) -> None:
        if not self._post_message or not self._notify:
            return
        try:
            self._post_message(
                mtype=self._notification_type,
                title=self._FINALIZE_DEAD_TITLE,
                text=self._FINALIZE_DEAD_TEXT.format(
                    str(item.get("file_name") or pending_key),
                    int(item.get("fail_count") or 0),
                ),
            )
        except Exception as error:
            logger.warning(f"后处理失败通知发送异常：{error}")

    def _schedule_finalize_retry(self, item: Dict[str, Any], now: float) -> None:
        check_index = min(
            int(item.get("check_index") or 0) + 1,
            len(self._OFFLINE_CHECK_DELAYS) - 1,
        )
        item["check_index"] = check_index
        retry_at = now + self._OFFLINE_CHECK_DELAYS[check_index]
        if str(item.get("task_type") or "share") in {"ed2k", "magnet"}:
            created_at = float(item.get("created_at") or now)
            retry_at = min(retry_at, created_at + self._OFFLINE_TIMEOUT)
        item["next_check_at"] = retry_at
        retry_minutes = max(1, int(max(0, retry_at - now) + 59) // 60)
        logger.debug(
            f"文件后处理尚未完成：{item.get('file_name')}，"
            f"{retry_minutes} 分钟后复查"
        )
