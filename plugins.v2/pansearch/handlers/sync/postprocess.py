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
    # 超时后连续零增长复查轮数上限，达到即判定卡死失败。
    _OFFLINE_ZERO_GROWTH_ROUNDS = 3
    # 复查时间下限：任何情况下 next_check_at 都不得早于 now + 该值，
    # 杜绝「next_check_at 被钉死在过去 → 每轮都到期」的空转（v1.5.10）。
    _OFFLINE_MIN_RETRY_SECONDS = 10
    # 绝对兜底：创建后超过该时长仍未终结的任务强制判失败，不再无限复查。
    # v1.5.12 F3：原实现只对 ed2k/magnet 生效，分享转存（share）任务在
    # 「文件被外部流程整理走」的场景下同样会无限复查，故扩展为全类型。
    _OFFLINE_ED2K_HARD_LIMIT = 24 * 60 * 60
    # v1.5.12 F2：转存目录可正常列举、但目标文件从未在该目录出现，且距
    # 登记已超过该观察期，即判定文件已被外部流程（MoviePilot 目录整理等）
    # 接管移走，直接收敛出队；不再死等到终审窗口再全盘递归定位。
    _STAGING_HANDOFF_GRACE_SECONDS = 300

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

    def _offline_timeout_fail_reason(self, prefix: str) -> str:
        """超时失败文案使用实际配置的分钟数，不再硬编码 30 分钟。"""
        minutes = max(1, int(self._OFFLINE_TIMEOUT // 60))
        return f"{prefix}离线下载超过 {minutes} 分钟未完成，已退出"

    def _finalize_timeout_minutes(self) -> int:
        """终审窗口分钟数，跟随 offline_download_timeout_minutes 配置。"""
        try:
            return max(1, int(self._FILE_FINALIZE_TIMEOUT // 60))
        except (TypeError, ValueError):
            return 120

    def _finalize_locate_fail_reason(self, rounds: int = 0) -> str:
        """定位失败文案：动态分钟数 + 全盘兜底轮数，不再硬编码 30 分钟。"""
        suffix = f"，连续 {rounds} 轮全盘检索均未找到" if rounds else ""
        return (
            f"网盘文件已保存但 {self._finalize_timeout_minutes()} 分钟内"
            f"仍无法在转存路径定位{suffix}"
        )

    def _finalize_strm_fail_reason(self) -> str:
        """STRM 生成失败文案：动态分钟数，不再硬编码 30 分钟。"""
        return (
            f"文件已下载但 {self._finalize_timeout_minutes()} 分钟内"
            f"仍无法生成 STRM"
        )

    def _finalize_full_pan_verdict(
            self,
            item: Dict[str, Any],
            file_name: str,
            now: float,
    ) -> Tuple[str, Any]:
        """终审窗口到期、常规目录定位失败时的全盘兜底判定。

        判死前先按 source_sha1、其次按文件名做网盘全盘检索：
        - ("located", (文件, 所在目录, 是否已就位))：全盘命中。文件已在
          目标目录（或当前网盘无移动能力）时按已就位继续 STRM 流程，
          后者把实际路径写入 pending 载荷 actual_path；否则交回原有
          重命名/移动流程整理到目标目录。
        - ("retry", "")：全盘未命中但零进展轮数未达上限，暂缓判定，
          保留 pending 下轮复查（基线为 download_completed_at）。
        - ("fail", 原因)：连续 _OFFLINE_ZERO_GROWTH_ROUNDS 轮全盘检索
          仍未找到文件，才下失败终态。
        旧 pending 载荷缺 finalize_zero_progress_rounds 等键时按 0 轮处理。
        """
        located = self._full_pan_locate_file(item, file_name=file_name)
        final_dir = str(item.get("cloud_dir") or "/").rstrip("/") or "/"
        if located:
            item["finalize_zero_progress_rounds"] = 0
            target_file, actual_dir = located
            actual_dir = str(actual_dir or "/").rstrip("/") or "/"
            actual_name = (
                    self._cloud_entry_name(target_file) or str(file_name or "")
            )
            if actual_dir == final_dir and actual_name == file_name:
                item["moved_at"] = item.get("moved_at") or now
                logger.info(
                    f"全盘检索确认文件已在目标目录，继续后处理："
                    f"{final_dir}/{actual_name}"
                )
                return "located", (target_file, final_dir, True)
            if getattr(self, "_cloud_mutations", None):
                item["staging_dir"] = actual_dir
                item["staging_name"] = actual_name
                logger.info(
                    f"全盘检索在 {actual_dir} 找到 {actual_name}，"
                    f"继续整理到 {final_dir}/{file_name}"
                )
                return "located", (target_file, actual_dir, False)
            actual_path = f"{actual_dir.rstrip('/')}/{actual_name}"
            item["actual_path"] = actual_path
            item["cloud_dir"] = actual_dir
            item["staging_dir"] = actual_dir
            item["staging_name"] = actual_name
            item["moved_at"] = now
            logger.info(
                f"当前网盘不支持移动文件，按全盘检索到的实际路径记录成功："
                f"{actual_path}"
            )
            return "located", (target_file, actual_dir, True)
        rounds = int(item.get("finalize_zero_progress_rounds") or 0) + 1
        item["finalize_zero_progress_rounds"] = rounds
        item.setdefault(
            "finalize_timeout_baseline",
            float(item.get("download_completed_at") or now),
        )
        if rounds >= self._OFFLINE_ZERO_GROWTH_ROUNDS:
            reason = self._finalize_locate_fail_reason(rounds)
            logger.warning(f"{reason}：{file_name}")
            return "fail", reason
        logger.info(
            f"终审窗口已到但全盘检索未找到文件（第 {rounds} 轮零进展），"
            f"继续等待：{file_name}"
        )
        return "retry", ""

    def _offline_slow_download_verdict(
            self,
            item: Dict[str, Any],
            task: Optional[Dict[str, Any]],
            prefix: str,
            file_name: str = "",
    ) -> Tuple[str, str]:
        """超时终审 "missing" 后的慢下载判定。

        任务仍在 115 离线列表且进度有增长 -> ("retry_pending", "")：
        保留 pending 记录、仅安排下一轮复查，不拉黑、不失败通知。
        任务已从列表消失（且文件终审未就绪）-> ("fail", 超时原因)。
        任务仍在但连续 _OFFLINE_ZERO_GROWTH_ROUNDS 轮复查进度零增长
        -> ("fail", 卡死原因)。进度基线与零增长轮数持久化在 pending
        载荷 JSON（timeout_progress_percent / offline_zero_growth_rounds），
        旧记录缺键时按首轮基线 / 0 轮处理。

        v1.5.10：增加「绝对兜底」——ED2K/magnet 任务创建超过
        _OFFLINE_ED2K_HARD_LIMIT 仍未终结，无论进度如何一律判失败。
        因为零增长熔断依赖基线键持久化，而历史上出现过基线键缺失导致
        计数器永远从 0 开始、熔断永不触发的无限重试（表现为永久
        「后处理中」且停止不了）。
        """
        if task is None:
            # 任务句柄不存在：网盘侧已消失且文件终审未就绪，真实失败。
            return "fail", self._offline_timeout_fail_reason(prefix)
        try:
            created_at = float(item.get("created_at") or 0)
        except (TypeError, ValueError):
            created_at = 0.0
        if created_at > 0 and (
                time.time() - created_at >= self._OFFLINE_ED2K_HARD_LIMIT
        ):
            hours = self._OFFLINE_ED2K_HARD_LIMIT // 3600
            return "fail", (
                f"115 离线下载超过 {hours} 小时仍未完成，"
                f"判定卡死退出（绝对兜底）"
            )
        try:
            percent = max(0.0, min(float(task.get("percent") or 0.0), 100.0))
        except (TypeError, ValueError):
            percent = 0.0
        baseline = item.get("timeout_progress_percent")
        if baseline is None:
            # 首次超时复查：记录进度基线，先继续等待。
            item["timeout_progress_percent"] = percent
            item["offline_zero_growth_rounds"] = 0
            logger.info(
                f"离线任务超时仍在列表（{percent:.0f}%），记录基线继续等待：{file_name}"
            )
            return "retry_pending", ""
        try:
            baseline = float(baseline)
        except (TypeError, ValueError):
            baseline = 0.0
        if percent > baseline:
            item["timeout_progress_percent"] = percent
            item["offline_zero_growth_rounds"] = 0
            logger.info(
                f"离线任务超时仍在下载（{baseline:.0f}% -> {percent:.0f}%），继续等待：{file_name}"
            )
            return "retry_pending", ""
        rounds = int(item.get("offline_zero_growth_rounds") or 0) + 1
        item["offline_zero_growth_rounds"] = rounds
        if rounds >= self._OFFLINE_ZERO_GROWTH_ROUNDS:
            reason = (
                f"115 离线下载连续 {rounds} 轮复查进度零增长"
                f"（{percent:.0f}%），判定卡死退出"
            )
            return "fail", reason
        logger.info(
            f"离线任务超时且进度未增长（{percent:.0f}%，第 {rounds} 轮），继续等待：{file_name}"
        )
        return "retry_pending", ""

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

    def _normalize_pending_item(
            self, item: Dict[str, Any], pending_key: str
    ) -> List[str]:
        """旧/异构 pending 记录缺字段时的兜底补齐。

        手动提交通道历史版本只登记了少量字段，与订阅后处理通道 schema
        不一致；轮次入口不能因为某一字段缺失就整轮跳过该记录。这里按
        "可处理即可" 原则补齐定位所需字段并保留空值兜底，同时记录补齐
        的字段名，交由调用方打一次 WARNING。返回本次补齐的字段名列表。
        """
        fixed: List[str] = []
        task_type = str(item.get("task_type") or "").strip() or "share"
        if item.get("task_type") != task_type:
            item["task_type"] = task_type
            fixed.append("task_type")
        task_id = str(item.get("task_id") or "").strip()
        if not task_id:
            task_id = str(item.get("info_hash") or pending_key).strip()
            item["task_id"] = task_id
            fixed.append("task_id")
        file_name = str(
            item.get("file_name") or item.get("staging_name")
            or item.get("source_file_name") or ""
        ).strip()
        if not file_name:
            file_name = task_id or str(pending_key)
            item["file_name"] = file_name
            fixed.append("file_name")
        cloud_dir = str(item.get("cloud_dir") or "").strip()
        if not cloud_dir:
            cloud_dir = str(item.get("staging_dir") or "/").rstrip("/") or "/"
            item["cloud_dir"] = cloud_dir
            fixed.append("cloud_dir")
        staging_dir = str(item.get("staging_dir") or "").strip()
        if not staging_dir:
            item["staging_dir"] = cloud_dir
            fixed.append("staging_dir")
        if not str(item.get("staging_name") or "").strip():
            item["staging_name"] = file_name
            fixed.append("staging_name")
        if "source_sha1" not in item:
            item["source_sha1"] = ""
            fixed.append("source_sha1")
        if "info_hash" not in item:
            item["info_hash"] = ""
            fixed.append("info_hash")
        if "subscribe_id" not in item:
            item["subscribe_id"] = 0
            fixed.append("subscribe_id")
        if "next_check_at" not in item:
            # 缺省视为立即到期：过期 pending 必须被下一轮必检。
            item["next_check_at"] = 0.0
            fixed.append("next_check_at")
        if "history_ready" not in item:
            item["history_ready"] = True
            fixed.append("history_ready")
        return fixed

    def _due_pending_keys(
            self,
            pending: Dict[str, Dict[str, Any]],
            now: float,
            force: bool = False,
            pending_keys: Optional[Set[str]] = None,
    ) -> List[str]:
        """选出本轮必检的 pending 键。

        - next_check_at 已过期（或缺失）必检必处理；
        - 上一轮异常退出残留的 _monitor_until 租约不会永久挡住已过期记录
          （租约只是并发保护，实际轮次由 _offline_monitor_lock 串行）；
        - 缺字段记录先经 _normalize_pending_item 兜底补齐，单字段缺失绝不
          整轮跳过，每个键只打一次 WARNING。
        """
        selected = set(pending_keys or [])
        warned = getattr(self, "_pending_schema_warned", None)
        if warned is None:
            warned = set()
            self._pending_schema_warned = warned
        due: List[str] = []
        for key, item in pending.items():
            if selected and key not in selected:
                continue
            fixed = self._normalize_pending_item(item, key)
            if fixed and key not in warned:
                warned.add(key)
                logger.warning(
                    f"pending 记录字段缺失，已按兜底补齐并继续处理："
                    f"{key}（补齐：{','.join(fixed)}）"
                )
            if not bool(item.get("history_ready", True)):
                continue
            next_check_at = float(item.get("next_check_at") or 0)
            expired = next_check_at <= 0 or now >= next_check_at
            if not force and not expired:
                # next_check_at 未到期：本轮跳过，等待下次复查。
                continue
            # next_check_at 已过期（或强制刷新）：必检必处理。即使上一轮
            # 异常退出残留了 _monitor_until 租约，也不得因此长期不再拾取
            # （v1.5.4 T4：奥德赛 id153 事故）。
            due.append(key)
        return due

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
            if self._cloud_entry_name(target_file) != file_name:
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

            # v1.5.10：绝对时限早筛，必须在任何网盘重活（全盘递归检索）之前。
            # 历史故障：ED2K/磁力任务一旦越过超时点，其 next_check_at 被钉在
            # 过去的固定时刻，每轮都「已到期」，于是每轮都要先做一次 115 全盘
            # 递归定位（耗时数分钟到数十分钟）才可能走到判定逻辑，监控器被
            # 长期占满、队列看似「卡死」。这里在入口直接按创建时间收割超期
            # 任务，既保证 24 小时内必定出终态，也避免为判死而空扫网盘。
            #
            # v1.5.14：锁内只做内存态变更（挑选 + 摘除 + 记录待写历史），
            # 所有落库动作（含历史定向更新）一律挪到锁外。历史实现直接在
            # 锁内调 ``_mark_offline_history_status``，而它会全量读并整体
            # 重写历史表；历史行数一多就把监控器、前端刷新 API、后续调度
            # tick 全部堵在这把锁上，表现为「后处理长期卡住 + 页面很慢」。
            expired_keys: List[str] = []
            expired_entries: List[Tuple[str, str, str]] = []
            for key in due_keys:
                item = pending.get(key) or {}
                try:
                    created_at = float(item.get("created_at") or 0)
                except (TypeError, ValueError):
                    created_at = 0.0
                if created_at <= 0:
                    continue
                if now - created_at < self._OFFLINE_ED2K_HARD_LIMIT:
                    continue
                hours = self._OFFLINE_ED2K_HARD_LIMIT // 3600
                reason = (
                    f"115 离线下载/转存后处理超过 {hours} 小时仍未完成，"
                    f"判定卡死退出（绝对兜底）"
                )
                file_name = str(item.get("file_name") or key)
                expired_entries.append((key, file_name, reason))
                pending.pop(key, None)
                expired_keys.append(key)
            if expired_keys:
                due_keys = [key for key in due_keys if key not in set(expired_keys)]
                self._save_offline_pending(pending)
                pending_count_after_expire = len(pending)
            else:
                pending_count_after_expire = -1
            expired_count = len(expired_keys)
            if not due_keys:
                pending_empty_count = len(pending)
            else:
                monitor_token_early = uuid.uuid4().hex
                for key in due_keys:
                    item = pending[key]
                    item["_monitor_token"] = monitor_token_early
                    item["_monitor_until"] = (
                            now + self._OFFLINE_MONITOR_LEASE_SECONDS
                    )
                self._save_offline_pending(pending)
                pending_empty_count = 0

        # ---- 以下全部在 _offline_pending_lock 之外 ----
        if expired_entries:
            for _key, file_name, reason in expired_entries:
                logger.warning(f"{reason}：{file_name}")
            self._mark_offline_history_status_batch(
                {key for key, _f, _r in expired_entries},
                "失败",
                expired_entries[0][2],
            )
            if pending_count_after_expire >= 0:
                self._notify_offline_pending_changed(pending_count_after_expire)
        if not due_keys:
            return {
                "checked": 0, "completed": 0, "failed": expired_count,
                "pending": pending_empty_count,
            }
        monitor_token = monitor_token_early
        pending_snapshot = copy.deepcopy(pending)
        completed = 0
        failed = expired_count
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
                            entry_name = self._cloud_entry_name(file_item)
                            if entry_name:
                                file_index[entry_name] = file_item
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

            # 本轮失败终态汇总：每项 (file_name, reason)，循环后输出一条汇总告警，
            # 避免批量超时场景逐条刷屏（v1.5.3 T3）。
            round_failure_entries: List[Tuple[str, str]] = []

            def record_round_failure(fail_name: str, fail_reason: str) -> None:
                round_failure_entries.append((fail_name, fail_reason))

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
                    record_round_failure(
                        str(item.get("file_name") or pending_key), reason
                    )
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
                        record_round_failure(file_name, reason)
                        failed += 1
                        continue
                    if not task_done:
                        finalize_now = False
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
                                finalize_now = True
                            elif self._offline_timeout_should_defer(
                                tasks_valid, verdict
                            ):
                                logger.warning(
                                    f"接口异常，暂缓判定：{file_name}"
                                )
                                self._schedule_finalize_retry(item, now)
                                continue
                            else:
                                slow_verdict, reason = (
                                    self._offline_slow_download_verdict(
                                        item, task, "Magnet ",
                                        file_name=file_name,
                                    )
                                )
                                if slow_verdict == "retry_pending":
                                    # 慢下载仍在推进：不拉黑、不失败，仅复查。
                                    self._schedule_finalize_retry(item, now)
                                    continue
                                self._add_offline_blacklist(item.get("share_url") or item.get("task_id"), reason)
                                self._cleanup_failed_offline_task(item, reason)
                                self._mark_offline_history_status(pending_key, "失败", reason)
                                pending.pop(pending_key, None)
                                record_round_failure(file_name, reason)
                                failed += 1
                                continue
                        else:
                            self._schedule_finalize_retry(item, now)
                        # 终审 "ready" 时同轮直接落入下方整理收尾，否则重试下一轮。
                        if not finalize_now:
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
                        reason = "Magnet 下载完成但未匹配到目标媒体文件"
                        logger.warning(f"{reason}：{file_name}")
                        self._add_offline_blacklist(item.get("share_url") or item.get("task_id"),
                                                    reason)
                        record_round_failure(file_name, reason)
                        failed += 1
                    continue
                if task_type == "ed2k":
                    task = task_map.get(str(item.get("task_id") or pending_key).upper())
                    task_done = bool(
                        item.get("moved_at") or (task and task.get("completed"))
                    )
                    if task and bool(task.get("failed")):
                        reason = "离线下载失败"
                        self._add_offline_blacklist(item.get("share_url") or item.get("task_id"), reason)
                        self._mark_offline_history_status(pending_key, "失败", reason)
                        pending.pop(pending_key, None)
                        record_round_failure(file_name, reason)
                        failed += 1
                        continue
                    if task is not None and not task_done:
                        finalize_now = False
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
                                finalize_now = True
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
                                slow_verdict, reason = (
                                    self._offline_slow_download_verdict(
                                        item, task, "115 ",
                                        file_name=file_name,
                                    )
                                )
                                if slow_verdict == "retry_pending":
                                    # 慢下载仍在推进：保留进度快照并复查，不失败。
                                    if task is not None:
                                        self._persist_offline_progress(item, task)
                                    self._schedule_finalize_retry(item, now)
                                    continue
                                self._add_offline_blacklist(item.get("share_url") or item.get("task_id"), reason)
                                self._mark_offline_history_status(pending_key, "失败", reason)
                                pending.pop(pending_key, None)
                                record_round_failure(file_name, reason)
                                failed += 1
                                continue
                            # 终审 "ready" 时同轮落入下方统一收尾，不再重试。
                            if not finalize_now:
                                continue
                        # 未超时或接口暂缓时保留进度快照并安排下一轮重试。
                        if not finalize_now:
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
                            record_round_failure(file_name, reason)
                            failed += 1
                            continue
                    if not task_done:
                        finalize_now = False
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
                                finalize_now = True
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
                                slow_verdict, reason = (
                                    self._offline_slow_download_verdict(
                                        item, task, "115 ",
                                        file_name=file_name,
                                    )
                                )
                                if slow_verdict == "retry_pending":
                                    # 慢下载仍在推进：保留进度快照并复查，不失败。
                                    if task is not None:
                                        self._persist_offline_progress(item, task)
                                    self._schedule_finalize_retry(item, now)
                                    continue
                                self._add_offline_blacklist(item.get("share_url") or item.get("task_id"), reason)
                                self._mark_offline_history_status(pending_key, "失败", reason)
                                pending.pop(pending_key, None)
                                record_round_failure(file_name, reason)
                                failed += 1
                                continue
                            # 终审 "ready" 时同轮落入下方统一收尾，不再重试。
                            if not finalize_now:
                                continue
                        else:
                            self._schedule_finalize_retry(item, now)
                        if not finalize_now:
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
                    # v1.5.12：保持 directory_valid 恒有定义，供下方
                    # 「外部流程接管」判定使用，避免 NameError。
                    directory_valid = True
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
                # 记录"文件曾在转存目录出现过"：转存后整理关闭时，文件可能
                # 已被 MoviePilot 等外部流程接管移走，该标记用于区分
                # "已转出"（判成功）与"从未落盘"（仍判失败）。v1.5.6 F1。
                if target_file and not item.get("staging_seen_at"):
                    item["staging_seen_at"] = now
                if not target_file and not already_moved:
                    final_dir = str(item.get("cloud_dir") or "/").rstrip("/") or "/"
                    if not self._organize_after_transfer and item.get(
                            "staging_seen_at"
                    ):
                        # 转存后整理关闭：文件已从转存目录转出，由外部流程
                        # 接管。不再等待文件出现在自己算出的媒体目录、不再
                        # 做全盘兜底检索、不再生成 STRM，直接判终。v1.5.6 F1。
                        logger.info(
                            f"转存后整理已关闭，文件已由外部流程接管，"
                            f"直接完成：{file_name}"
                        )
                        media, media_data = self._restore_pending_media_context(
                            item, pending_key
                        )
                        finalize_after_metadata(
                            item, pending_key, file_name, None, media, media_data
                        )
                        continue
                    # v1.5.12 F2：文件从未在转存目录出现过（staging_seen_at
                    # 缺失），但转存目录本身可正常列举 —— 说明文件落盘后已被
                    # 外部流程（MoviePilot 目录整理等）接走。此时既等不到
                    # staging 命中，也等不到插件自算媒体目录命中（外部命名
                    # 与插件规范名不同），历史实现只能死等到 120 分钟终审
                    # 窗口再全盘递归，表现为「后处理永久卡死且停止不了」。
                    # 这里在观察期后直接按「已由外部流程接管」收敛出队。
                    if (
                            not target_file
                            and directory_valid
                            and now - float(created_at or 0)
                            >= self._STAGING_HANDOFF_GRACE_SECONDS
                    ):
                        logger.info(
                            f"文件已离开转存目录，判定由外部流程接管，"
                            f"直接完成：{file_name}（{staging_dir}）"
                        )
                        media, media_data = self._restore_pending_media_context(
                            item, pending_key
                        )
                        finalize_after_metadata(
                            item, pending_key, file_name, None, media, media_data
                        )
                        continue
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
                        # 旧任务载荷里的 cloud_dir 可能仍是 F1 修复前的错误目录：
                        # 按 F1 规则重算期望目录并二次定位，命中即本轮直接收敛，
                        # 不再等 120 分钟终审窗口的全盘兜底。
                        recomputed_dir = self._recompute_expected_cloud_dir(item, file_name)
                        if (recomputed_dir and recomputed_dir != final_dir
                                and recomputed_dir != staging_dir):
                            recomputed_valid, recomputed_index = directory_snapshot(recomputed_dir)
                            if recomputed_valid:
                                recomputed_candidate = recomputed_index.get(file_name)
                                if recomputed_candidate:
                                    target_file = recomputed_candidate
                                    item["moved_at"] = now
                                    already_moved = True
                                    staging_dir = recomputed_dir
                                    logger.info(
                                        f"载荷目录已过期，按F1规则重算目录命中文件，"
                                        f"本轮直接收尾：{recomputed_dir}/{file_name}"
                                    )
                if not target_file:
                    ready_at = float(item.get("download_completed_at") or created_at)
                    if now - ready_at < self._FILE_FINALIZE_TIMEOUT:
                        self._schedule_finalize_retry(item, now)
                        continue
                    # 终审窗口到期不得直接判死：先做网盘全盘兜底检索
                    # （sha1 优先、文件名次之），未命中也走零进展暂缓。
                    locate_verdict, locate_payload = (
                        self._finalize_full_pan_verdict(item, file_name, now)
                    )
                    if locate_verdict == "retry":
                        self._schedule_finalize_retry(item, now)
                        continue
                    if locate_verdict != "located":
                        reason = (
                                str(locate_payload or "")
                                or self._finalize_locate_fail_reason()
                        )
                        self._mark_offline_history_status(
                            pending_key, "失败", reason
                        )
                        pending.pop(pending_key, None)
                        record_round_failure(file_name, reason)
                        failed += 1
                        continue
                    target_file, staging_dir, already_moved = locate_payload
                    staging_name = (
                            str(item.get("staging_name") or "").strip()
                            or staging_name
                    )

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
                    if self._cloud_entry_name(target_file) != file_name:
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
                    reason = self._finalize_strm_fail_reason()
                    self._mark_offline_history_status(pending_key, "失败", reason)
                    pending.pop(pending_key, None)
                    record_round_failure(file_name, reason)
                    failed += 1
                else:
                    self._schedule_finalize_retry(item, now)

            # 批量失败只输出一条汇总告警（数量 + 原因分布），避免逐条刷屏。
            if round_failure_entries:
                reason_counts: Dict[str, int] = {}
                for _, fail_reason in round_failure_entries:
                    reason_counts[fail_reason] = reason_counts.get(fail_reason, 0) + 1
                reason_summary = "；".join(
                    f"{fail_reason}（{count} 项）"
                    for fail_reason, count in reason_counts.items()
                )
                logger.warning(
                    f"本轮离线后处理判定失败 {len(round_failure_entries)} 项：{reason_summary}"
                )

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

    def force_clear_offline_pending(
            self,
            pending_keys: Optional[Set[str]] = None,
            reason: str = "",
    ) -> int:
        """v1.5.12 F4：强制将指定（或全部）待后处理记录出队。

        用于清理因历史缺陷永久停留在「处理中」的僵尸记录：出队时写入
        历史失败原因，避免其长期占用监控器与前端状态、且无法被停止。
        :return: 实际清理条数
        """
        if not self._get_data:
            return 0
        keys = {str(key) for key in (pending_keys or set()) if str(key)}
        removed = 0
        with self._offline_pending_lock:
            pending = self._get_data(self._OFFLINE_PENDING_KEY) or {}
            if not pending:
                return 0
            targets = (keys & set(pending)) if keys else set(pending)
            if not targets:
                return 0
            for key in targets:
                item = pending.get(key) or {}
                file_name = str(item.get("file_name") or key)
                self._mark_offline_history_status(
                    key,
                    "失败",
                    reason or "已手动清理：任务长期停留在后处理中，强制出队",
                )
                pending.pop(key, None)
                removed += 1
                logger.warning(f"强制清理待后处理记录：{file_name}")
            self._save_offline_pending(pending)
            pending_count = len(pending)
        self._notify_offline_pending_changed(pending_count)
        return removed

    def _recompute_expected_cloud_dir(
            self, item: Dict[str, Any], file_name: str
    ) -> Optional[str]:
        """按 F1 目录规则重算旧任务期望的最终目录。

        旧任务创建于 F1 修复前，载荷里的 cloud_dir 是错误目录；此方法用当前
        规则重算期望目录，仅作为收尾定位的二次兜底，不影响主定位逻辑。
        任何异常都按“无法重算”处理，返回 None。
        """
        try:
            media_data = item.get("mediainfo") or {}
            if not media_data:
                return None
            mediainfo = self._deserialize_mediainfo(media_data)
            if mediainfo is None:
                return None
            subscribe = SimpleNamespace(
                name=mediainfo.title or "",
                year=mediainfo.year,
                media_category=getattr(mediainfo, "category", None),
            )
            season = (
                max(1, int(item.get("season") or 1))
                if item.get("season") else None
            )
            episode = item.get("episode")
            cloud_dir, _ = self._platform_target(
                self._CLOUD_MEDIA_ROOT, subscribe, mediainfo, file_name,
                season=season, episode=episode,
            )
            return str(cloud_dir or "").rstrip("/") or "/"
        except Exception as error:
            logger.debug(f"按F1规则重算期望目录失败，跳过二次定位：{error}")
            return None

    def _finalize_magnet_package(
            self,
            item: Dict[str, Any],
            pending_key: str,
            subscribe_cache: Optional[Dict[int, Any]] = None,
    ) -> Optional[List[Dict[str, Any]]]:
        """读取完成后的真实文件树，只移动实际匹配的媒体文件。"""
        mediainfo, media_data = self._restore_pending_media_context(item, pending_key)
        if not mediainfo:
            reason = "媒体元数据不存在"
            logger.warning(
                f"Magnet 下载完成但{reason}：{item.get('file_name')}"
            )
            self._cleanup_failed_offline_task(item, reason)
            self._mark_offline_history_status(pending_key, "失败", reason)
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
            index: Dict[str, Any] = {}
            for value in listing.files:
                entry_name = self._cloud_entry_name(value)
                if entry_name:
                    index[entry_name] = value
            return True, index

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
                    if self._cloud_entry_name(moved) != target_name:
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
            # 整理开关关闭时文件停留在中转目录，由 MoviePilot 自行整理，
            # 此处不得生成 STRM（闭环：关闭=不整理、不 STRM）。
            if (self._strm_generate_enabled and self._strm_generator
                    and self._local_resource_path and self._organize_after_transfer):
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
        delay = self._OFFLINE_CHECK_DELAYS[check_index]
        retry_at = now + delay
        if str(item.get("task_type") or "share") in {"ed2k", "magnet"}:
            created_at = float(item.get("created_at") or now)
            deadline = created_at + self._OFFLINE_TIMEOUT
            # 原意是「把复查提前到超时判定点之前」，但一旦 now 已越过 deadline，
            # min() 会把 next_check_at 钉死在过去的固定时刻 —— 该记录此后每轮都
            # 判为「已到期」，形成永不退避的空转（v1.5.10 修复：ED2K 无限重试）。
            # 因此仅在 deadline 尚未到达时才允许提前；否则退化为按延迟正常退避。
            if now < deadline:
                retry_at = min(retry_at, deadline)
        # 兜底：任何情况下复查时间都不得早于当前时刻 + 最小间隔，杜绝空转。
        retry_at = max(retry_at, now + self._OFFLINE_MIN_RETRY_SECONDS)
        item["next_check_at"] = retry_at
        retry_minutes = max(1, int(max(0, retry_at - now) + 59) // 60)
        logger.debug(
            f"文件后处理尚未完成：{item.get('file_name')}，"
            f"{retry_minutes} 分钟后复查"
        )
