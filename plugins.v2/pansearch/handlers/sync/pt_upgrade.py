"""PT 整理完成后的网盘洗版上传。"""

import hashlib
import time
from pathlib import Path
from threading import Event as ThreadEvent
from typing import Any, Dict, Optional, Tuple

from app.db.subscribe_oper import SubscribeOper
from app.log import logger
from app.schemas.types import MediaType

from ...core import OwnerDelegator
from ...core.media import list_subscribes_by_tmdb_id
from ...utils import MediaFileParser


class _PtUploadCancelled(RuntimeError):
    """PT 洗版任务收到停止请求。"""


class PtUpgradeService(OwnerDelegator):
    """将符合洗版条件的本地整理文件上传并交给现有后处理链。"""

    @staticmethod
    def _file_sha1(path: Path, stop_event: Optional[ThreadEvent] = None) -> str:
        digest = hashlib.sha1()
        with path.open("rb") as file:
            while chunk := file.read(8 * 1024 * 1024):
                if stop_event and stop_event.is_set():
                    raise _PtUploadCancelled("用户停止 PT 洗版上传")
                digest.update(chunk)
        return digest.hexdigest().upper()

    @staticmethod
    def _staging_name(path: Path, checksum: str) -> str:
        suffix = path.suffix
        stem = path.stem[:160]
        return f"PanSearch_PT_{checksum[:12]}_{stem}{suffix}"

    def _pt_upgrade_subscribe(self, mediainfo, meta):
        tmdb_id = int(getattr(mediainfo, "tmdb_id", 0) or 0)
        if not tmdb_id:
            return None
        media_type = getattr(
            getattr(mediainfo, "type", None), "value", getattr(mediainfo, "type", None)
        )
        season = None
        if media_type == MediaType.TV.value:
            season_list = list(getattr(meta, "season_list", None) or [])
            season = int(
                getattr(meta, "begin_season", 0)
                or (season_list[0] if season_list else 1)
                or 1
            )
        subscribes = list_subscribes_by_tmdb_id(
            SubscribeOper(), tmdb_id, season
        )
        selected_ids = {str(value) for value in (self._upgrade_subscribe_ids or [])}
        return next(
            (
                subscribe for subscribe in subscribes
                if bool(getattr(subscribe, "best_version", False))
                   and (
                           not selected_ids
                           or str(getattr(subscribe, "id", "")) in selected_ids
                   )
            ),
            None,
        )

    @staticmethod
    def _local_target(event_data: Dict[str, Any]) -> Optional[Path]:
        transferinfo = event_data.get("transferinfo")
        target_item = getattr(transferinfo, "target_item", None) if transferinfo else None
        if not target_item or str(getattr(target_item, "storage", "local") or "local") != "local":
            return None
        path = Path(str(getattr(target_item, "path", "") or ""))
        return path if path.is_file() else None

    def _tv_target(
            self, subscribe, mediainfo, meta, local_path: Path, file_size: int,
    ) -> Optional[Tuple[int, int, str, str, Any, str, int, str]]:
        season_list = list(getattr(meta, "season_list", None) or [])
        season = int(
            getattr(meta, "begin_season", 0)
            or (season_list[0] if season_list else getattr(subscribe, "season", 1))
            or 1
        )
        episodes = sorted({
            int(value) for value in (getattr(meta, "episode_list", None) or [])
            if int(value) > 0
        })
        if not episodes:
            parsed = MediaFileParser.extract_season_episode(local_path.name)
            if parsed and int(parsed[0]) == season:
                episodes = [int(parsed[1])]
        if len(episodes) != 1:
            logger.warning(f"PT洗版仅处理可确定单集的电视剧文件：{local_path.name}")
            return None
        episode = episodes[0]
        valid, episode_files, cloud_dir = self._scan_cloud_resource_episode_files(
            subscribe=subscribe,
            mediainfo=mediainfo,
            season=season,
            start_episode=max(1, int(getattr(subscribe, "start_episode", 1) or 1)),
            total_episode=max(0, int(getattr(subscribe, "total_episode", 0) or 0)),
        )
        if not valid:
            logger.warning(f"PT洗版无法确认网盘目标目录：{local_path.name}")
            return None
        old_file = episode_files.get(episode)
        old_score = self._get_mp_rule_score(
            getattr(old_file, "name", ""),
            int(getattr(old_file, "size", 0) or 0),
            subscribe,
            season,
            mediainfo,
        ) if old_file else 0
        new_score = self._get_mp_rule_score(
            local_path.name, file_size, subscribe, season, mediainfo
        )
        should_upload, reason = self._should_upgrade_candidate(
            old_score,
            new_score,
            int(getattr(old_file, "size", 0) or 0),
            file_size,
            has_existing=bool(old_file),
        )
        if not should_upload:
            logger.info(f"PT洗版跳过 {local_path.name}：{reason}")
            return None
        target_dir, target_name = self._platform_target(
            self._CLOUD_MEDIA_ROOT,
            subscribe,
            mediainfo,
            local_path.name,
            season,
            episode,
        )
        return (
            season, episode, target_dir, target_name, old_file,
            cloud_dir if old_file else "", new_score, reason,
        )

    def _movie_target(
            self, subscribe, mediainfo, local_path: Path, file_size: int,
    ) -> Optional[Tuple[None, None, str, str, Any, str, int, str]]:
        existing = self._find_cloud_movie_file(subscribe, mediainfo)
        old_file = existing[2] if existing else None
        old_score = self._get_mp_rule_score(
            getattr(old_file, "name", ""),
            int(getattr(old_file, "size", 0) or 0),
            subscribe,
            1,
            mediainfo,
        ) if old_file else 0
        new_score = self._get_mp_rule_score(
            local_path.name, file_size, subscribe, 1, mediainfo
        )
        should_upload, reason = self._should_upgrade_candidate(
            old_score,
            new_score,
            int(getattr(old_file, "size", 0) or 0),
            file_size,
            has_existing=bool(old_file),
        )
        if not should_upload:
            logger.info(f"PT洗版跳过 {local_path.name}：{reason}")
            return None
        target_dir, target_name = self._platform_target(
            self._CLOUD_MEDIA_ROOT, subscribe, mediainfo, local_path.name
        )
        return (
            None, None, target_dir, target_name, old_file,
            existing[0] if existing else "", new_score, reason,
        )

    def process_pt_upgrade(self, event_data: Dict[str, Any]) -> bool:
        """处理单个整理完成事件。"""
        if not self._enable_pt_upgrade or not self._cloud_upload:
            return False
        if not event_data.get("downloader") or not event_data.get("download_hash"):
            return False
        local_path = self._local_target(event_data)
        mediainfo = event_data.get("mediainfo")
        meta = event_data.get("meta")
        if not local_path or not mediainfo or not meta:
            return False
        active_key = str(local_path.resolve())
        with self._pt_upgrade_lock:
            if active_key in self._pt_upgrade_active:
                return False
            self._pt_upgrade_active.add(active_key)
        task_id = f"pt:{hashlib.sha1(active_key.encode('utf-8')).hexdigest()[:20]}"
        stop_event = ThreadEvent()
        self._update_pt_task(
            task_id,
            title=str(getattr(mediainfo, "title", "PT 洗版") or "PT 洗版"),
            media_type="电视剧" if getattr(getattr(mediainfo, "type", None), "value",
                                           getattr(mediainfo, "type", None)) == MediaType.TV.value else "电影",
            status="running",
            phase="分析 PT 整理文件",
            progress=0,
            transferred=0,
            total=0,
            upload_speed=0,
            stop_event=stop_event,
        )
        try:
            subscribe = self._pt_upgrade_subscribe(mediainfo, meta)
            if not subscribe:
                return False
            file_size = local_path.stat().st_size
            self._update_pt_task(task_id, total=file_size, phase="计算文件校验和")
            checksum = self._file_sha1(local_path, stop_event)
            if any(
                    str(item.get("source_sha1") or "").upper() == checksum
                    for item in self.get_pending_finalize_tasks()
            ):
                logger.debug(f"PT洗版文件已在网盘后处理队列：{local_path.name}")
                return False
            media_type = getattr(
                getattr(mediainfo, "type", None), "value", getattr(mediainfo, "type", None)
            )
            target = (
                self._tv_target(subscribe, mediainfo, meta, local_path, file_size)
                if media_type == MediaType.TV.value
                else self._movie_target(subscribe, mediainfo, local_path, file_size)
            )
            if not target:
                return False
            (
                season, episode, target_dir, target_name, old_file,
                old_cloud_dir, new_score, reason,
            ) = target
            if old_file and self._upgrade_mode == "coexist":
                target_name = self._coexist_target_name(
                    target_name, local_path.name, file_size, checksum
                )
            staging_name = self._staging_name(local_path, checksum)
            progress_state = {"at": time.monotonic(), "bytes": 0}

            def upload_progress(transferred: int, total: int) -> None:
                if stop_event.is_set():
                    raise _PtUploadCancelled("用户停止 PT 洗版上传")
                now = time.monotonic()
                transferred = max(
                    progress_state["bytes"], min(max(0, int(transferred)), max(0, int(total)))
                )
                elapsed = max(0.001, now - progress_state["at"])
                speed = max(0, int((transferred - progress_state["bytes"]) / elapsed))
                progress_state.update(at=now, bytes=transferred)
                self._update_pt_task(
                    task_id,
                    phase="上传 PT 文件",
                    progress=round(min(100, transferred * 100 / max(1, total)), 1),
                    transferred=transferred,
                    total=total,
                    upload_speed=speed,
                )

            self._update_pt_task(task_id, phase="上传 PT 文件")
            if not self._cloud_upload.upload_file(
                    str(local_path), self._cloud_transfer_path, staging_name, checksum,
                    progress_callback=upload_progress,
            ):
                if stop_event.is_set():
                    raise _PtUploadCancelled("用户停止 PT 洗版上传")
                self._update_pt_task(
                    task_id,
                    status="failed",
                    phase="PT 文件上传失败",
                    message="当前网盘未完成本地文件上传",
                    upload_speed=0,
                    finished_at=time.time(),
                )
                return False
            share_url = f"pt://{event_data.get('download_hash')}"
            _, pending_key = self._generate_or_queue_strm(
                share_url=share_url,
                cloud_dir=target_dir,
                file_name=target_name,
                mediainfo=mediainfo,
                source_sha1=checksum,
                file_size=file_size,
                subscribe_id=getattr(subscribe, "id", None),
                success_episodes=[],
                season=season,
                notification_episodes=[episode] if episode else [],
                sub_key=f"pt-upgrade:{getattr(subscribe, 'id', '')}",
                staging_dir=self._cloud_transfer_path,
                staging_name=staging_name,
                upgrade=bool(old_file),
                upgrade_mode=self._upgrade_mode,
                upgrade_old_cloud_dir=old_cloud_dir,
                upgrade_old_file_name=(getattr(old_file, "name", "") if old_file else ""),
                upgrade_old_file_id=(getattr(old_file, "id", "") if old_file else ""),
                upgrade_old_size=(int(getattr(old_file, "size", 0) or 0) if old_file else 0),
            )
            if not pending_key:
                logger.error(f"PT洗版上传后未能登记网盘后处理：{local_path.name}")
                self._update_pt_task(
                    task_id,
                    status="failed",
                    phase="网盘后处理登记失败",
                    message="上传已完成，但未能登记后处理任务",
                    upload_speed=0,
                    finished_at=time.time(),
                )
                return False
            self._update_pt_task(
                task_id,
                status="postprocessing",
                phase="上传完成，等待网盘后处理",
                progress=100,
                transferred=file_size,
                total=file_size,
                upload_speed=0,
                pending_key=pending_key,
            )
            history_item = self._build_transfer_history_item(
                mediainfo=mediainfo,
                subscribe=subscribe,
                status="处理中",
                share_url=share_url,
                file_name=target_name,
                source_file_name=local_path.name,
                cloud_dir=target_dir,
                resource={"resource_type": "pt", "source": "moviepilot"},
                season=season,
                **({"episode": episode} if episode else {}),
                file_size=file_size,
                source_sha1=checksum,
                rule_score=new_score,
                upgrade=bool(old_file),
                finalize_key=pending_key,
            )
            self.append_history_records([history_item])
            logger.info(f"PT洗版已提交网盘后处理：{local_path.name}，{reason}")
            return True
        except _PtUploadCancelled:
            logger.info(f"PT洗版上传已停止：{local_path.name}")
            self._update_pt_task(
                task_id,
                status="stopped",
                phase="PT 上传已停止",
                upload_speed=0,
                finished_at=time.time(),
            )
            return False
        except Exception as error:
            logger.error(f"PT洗版处理失败：{local_path.name}，{error}")
            self._update_pt_task(
                task_id,
                status="failed",
                phase="PT 洗版失败",
                message=str(error),
                finished_at=time.time(),
            )
            return False
        finally:
            with self._pt_upgrade_lock:
                self._pt_upgrade_active.discard(active_key)
            lock = getattr(self, "_sync_tasks_lock", None)
            if lock is not None:
                with lock:
                    task = self._sync_tasks.get(task_id)
                    if task and task.get("status") == "running":
                        task.update({
                            "status": "completed",
                            "phase": "无需 PT 洗版",
                            "progress": 100,
                            "upload_speed": 0,
                            "finished_at": time.time(),
                        })

    def _update_pt_task(self, task_id: str, **values: Any) -> None:
        """将 PT 洗版上传状态复用到统一任务列表。"""
        lock = getattr(self, "_sync_tasks_lock", None)
        if lock is None:
            return
        with lock:
            task = self._sync_tasks.get(task_id)
            if task is None:
                now = time.time()
                task = {
                    "id": task_id,
                    "task_kind": "pt_upgrade",
                    "queued_at": now,
                    "started_at": now,
                    "finished_at": None,
                    "stop_event": ThreadEvent(),
                    "message": "",
                }
                self._sync_tasks[task_id] = task
            task.update(values)
