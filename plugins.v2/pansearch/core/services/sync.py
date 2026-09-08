"""订阅同步执行与并发编排。"""

import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from threading import Event as ThreadEvent, Thread
from typing import Any, Callable, Dict, List, Optional, Tuple

from app.db import SessionFactory
from app.db.models.subscribe import Subscribe
from app.db.subscribe_oper import SubscribeOper
from app.log import logger
from app.schemas.types import MediaType

from .runtime import sync_lock
from ...core import CloudDriveCapability, OwnerDelegator
from ...core.media import media_identity, tmdb_id_of
from ...utils.cache import create_platform_ttl_cache

_SUBSCRIBE_MEDIA_KEY_CACHE = create_platform_ttl_cache(
    "sync:subscribe_media_keys", maxsize=512, ttl=60
)


class SyncExecutionService(OwnerDelegator):
    """执行订阅同步并维护全局执行边界。"""

    _SUBSCRIBE_SEARCH_BATCH_SECONDS = 1.0
    # 防止平台短时间重复回调；完成后不应阻塞正常的手动重试一分钟。
    _SUBSCRIBE_SEARCH_DEBOUNCE_SECONDS = 5.0

    def _ensure_sync_operation_executor(self) -> ThreadPoolExecutor:
        """确保同步操作执行器健康可用并自动自愈。"""
        shutdown_event = getattr(self, "_subscribe_search_queue_shutdown", None)
        if shutdown_event is not None and shutdown_event.is_set():
            shutdown_event.clear()
        stop_event = getattr(self, "_stop_event", None)
        if stop_event is not None and stop_event.is_set():
            stop_event.clear()
        executor = getattr(self, "_sync_operation_executor", None)
        is_dead = executor is None or getattr(executor, "_shutdown", False)
        if is_dead:
            executor = ThreadPoolExecutor(
                max_workers=1,
                thread_name_prefix="pansearch-sync-operation",
            )
            self._sync_operation_executor = executor
        return executor

    def _run_sync_operation(
            self,
            sync_kwargs: Dict[str, Any],
            label: str,
    ) -> bool:
        logger.info(f"开始执行同步操作：{label}")
        stop_event = getattr(self, "_stop_event", None)
        if stop_event is not None and stop_event.is_set():
            stop_event.clear()
        return self.sync_subscribes(**sync_kwargs, wait_for_slot=True)

    def _submit_sync_operation(
            self,
            sync_kwargs: Dict[str, Any],
            label: str,
    ) -> Any:
        """串行提交同步操作，保证后添加资源位于现有操作之后。"""
        try:
            executor = self._ensure_sync_operation_executor()
        except Exception as error:
            logger.warning(f"自愈同步执行器异常：{error}")
            executor = getattr(self, "_sync_operation_executor", None)
        if not executor or getattr(executor, "_shutdown", False):
            raise RuntimeError("同步执行器当前不可用，请稍后重试")
        future = executor.submit(
            self._run_sync_operation,
            dict(sync_kwargs),
            str(label or "订阅任务"),
        )
        self._mark_runtime_changed()
        logger.debug(f"同步操作已提交：{label}")
        return future

    def _direct_cloud_manual_resources(
            self, resources: Optional[List[Dict[str, Any]]]
    ) -> bool:
        """判断手动资源是否全部来自目标网盘路径，可直接整理。"""
        target_provider = str(
            getattr(self._cloud_drive, "key", "") or ""
        ).strip().lower()
        return bool(resources and target_provider) and all(
            str(item.get("resource_type") or "").strip().lower() == "cloud"
            and str(item.get("cloud_provider") or "").strip().lower()
            == target_provider
            for item in resources
        )

    @staticmethod
    def _history_group_key(
            media_type: str,
            title: str,
            season: Any = None,
            tmdb_id: Any = None,
    ) -> Tuple[str, str, int]:
        normalized_type = "电影" if str(media_type or "") == MediaType.MOVIE.value else "电视剧"
        media_identity = (
            f"tmdb:{tmdb_id}"
            if str(tmdb_id or "").strip()
            else f"title:{str(title or '').strip()}"
        )
        return (
            normalized_type,
            media_identity,
            int(season or 1) if normalized_type == "电视剧" else 0,
        )

    @staticmethod
    def _media_key_from_subscribe(subscribe_id: int, subscribe: Any) -> tuple:
        if not subscribe:
            return ("ID", int(subscribe_id))
        media_type = str(getattr(subscribe, "type", "") or "")
        source, source_id = media_identity(subscribe)
        media_id = (
            f"{source}:{source_id}"
            if source and source_id
            else str(getattr(subscribe, "name", "") or "").strip()
        )
        season = (
            int(getattr(subscribe, "season", 1) or 1)
            if media_type == MediaType.TV.value else 0
        )
        return media_type, media_id, season

    @classmethod
    def _subscribe_search_media_keys(
            cls, subscribe_ids
    ) -> Dict[Optional[int], tuple]:
        """批量生成队列媒体键，同一阶段每个订阅只解析一次。"""
        values = list(dict.fromkeys(subscribe_ids or []))
        result: Dict[Optional[int], tuple] = {}
        missing: Dict[int, List[Any]] = {}
        for subscribe_id in values:
            if subscribe_id is None:
                result[subscribe_id] = ("ALL",)
                continue
            try:
                normalized_id = int(subscribe_id)
            except (TypeError, ValueError):
                result[subscribe_id] = ("ID", str(subscribe_id))
                continue
            cached = _SUBSCRIBE_MEDIA_KEY_CACHE.get(str(normalized_id))
            if isinstance(cached, (list, tuple)):
                result[subscribe_id] = tuple(cached)
            else:
                missing.setdefault(normalized_id, []).append(subscribe_id)

        if missing:
            rows_by_id: Dict[int, Any] = {}
            try:
                with SessionFactory() as db:
                    rows = db.query(Subscribe).filter(
                        Subscribe.id.in_(list(missing))
                    ).all()
                rows_by_id = {
                    int(row.id): row for row in rows
                    if getattr(row, "id", None) is not None
                }
            except Exception as error:
                logger.warning(
                    f"批量读取订阅媒体身份失败，回退按 ID 合并：{error}"
                )
            for normalized_id, original_ids in missing.items():
                media_key = cls._media_key_from_subscribe(
                    normalized_id, rows_by_id.get(normalized_id)
                )
                _SUBSCRIBE_MEDIA_KEY_CACHE.set(str(normalized_id), media_key)
                for original_id in original_ids:
                    result[original_id] = media_key
        return result

    def _prepare_searchable_subscribes(
            self, subscribes: List[Any]
    ) -> Tuple[List[Any], int]:
        """统一准备媒体身份、目标集和播出日历，供任务线程直接复用。"""
        prepared = []
        unresolved_count = 0
        for subscribe in subscribes:
            try:
                has_tmdb_id = bool(tmdb_id_of(subscribe))
            except (TypeError, ValueError):
                has_tmdb_id = False
            repaired = has_tmdb_id or bool(
                self._sync_handler
                and self._sync_handler.repair_subscribe_tmdb_id(subscribe)
            )
            if not repaired:
                unresolved_count += 1
                logger.debug(
                    "订阅缺少 TMDB ID 且自动修复失败，任务创建前跳过："
                    f"#{getattr(subscribe, 'id', '')} "
                    f"{getattr(subscribe, 'name', '')} "
                    f"({getattr(subscribe, 'year', '')})"
                )
                continue

            is_tv = getattr(subscribe, "type", "") == MediaType.TV.value
            start_episode = (
                int(getattr(subscribe, "start_episode", 1) or 1)
                if is_tv else 0
            )
            total_episode = (
                int(getattr(subscribe, "total_episode", 0) or 0)
                if is_tv else 0
            )
            expected_episodes = (
                set(range(start_episode, total_episode + 1))
                if is_tv and total_episode >= start_episode else set()
            )
            calendar_entry = (
                self._sync_handler.get_tv_subscribe_calendar(subscribe)
                if self._sync_handler and expected_episodes else None
            )
            unreleased_episodes = {
                int(episode)
                for episode in (
                        (calendar_entry or {}).get("unreleased_episodes") or []
                )
            }
            preparation = {
                "tmdb_id": int(tmdb_id_of(subscribe) or 0),
                "calendar": calendar_entry,
                "expected_episodes": sorted(expected_episodes),
                "aired_target_episodes": sorted(
                    expected_episodes - unreleased_episodes
                ),
                "unreleased_episodes": sorted(unreleased_episodes),
                "all_targets_future": bool(
                    calendar_entry
                    and calendar_entry.get("all_targets_future")
                ),
                "defer_until": str(
                    (calendar_entry or {}).get("defer_until") or ""
                ),
            }
            setattr(subscribe, "_pansearch_preparation", preparation)
            prepared.append(subscribe)
        return prepared, unresolved_count

    def _deduplicate_subscribes(
            self, subscribes: List[Any]
    ) -> Tuple[List[Any], int]:
        """按媒体身份保留最早订阅，并输出可定位的重复卡片明细。"""
        grouped: Dict[Tuple[str, str, int], List[Any]] = {}
        for subscribe in subscribes:
            grouped.setdefault(self._sync_media_key(subscribe), []).append(subscribe)

        canonical = []
        duplicate_count = 0
        duplicate_details = []
        for group in grouped.values():
            canonical_item = min(
                group,
                key=lambda item: (
                    bool(getattr(item, "_transient_target", False)),
                    int(getattr(item, "id", 0) or 0),
                ),
            )
            canonical.append(canonical_item)
            duplicates = [
                item for item in group if item is not canonical_item
            ]
            duplicate_count += len(duplicates)
            if duplicates:
                duplicate_details.append(
                    f"{getattr(canonical_item, 'name', '')}：保留 "
                    f"#{getattr(canonical_item, 'id', '')}，跳过 "
                    + ", ".join(
                        f"#{getattr(item, 'id', '')}" for item in duplicates
                    )
                )
        if duplicate_count:
            details = "；".join(duplicate_details[:10])
            if len(duplicate_details) > 10:
                details += f"；另有 {len(duplicate_details) - 10} 组"
            logger.warning(
                f"发现 {duplicate_count} 个同媒体重复订阅，本轮仅处理最早创建的订阅卡片："
                f"{details}"
            )
        return canonical, duplicate_count

    def queue_subscribe_search(
            self,
            subscribe_id: Optional[int],
            subscribe_state: Optional[str] = None,
            progress_callback: Optional[Callable[..., None]] = None,
    ) -> bool:
        """接收平台订阅搜索，并保留平台传入的状态范围。"""
        normalized_state = self._normalize_subscribe_state(subscribe_state)
        now = time.monotonic()
        queue_lock = self._subscribe_search_queue_lock
        with queue_lock:
            if self._subscribe_search_queue_shutdown.is_set():
                return False
            media_keys = self._subscribe_search_media_keys({
                subscribe_id,
                *self._subscribe_search_active,
                *self._subscribe_search_pending,
            })
            media_key = media_keys[subscribe_id]
            debounce_subject = (
                ("ALL",)
                if subscribe_id is None
                else ("SUBSCRIBE", str(subscribe_id))
            )
            debounce_key = (debounce_subject, normalized_state)
            recent = self._subscribe_search_recent
            expired_before = now - self._SUBSCRIBE_SEARCH_DEBOUNCE_SECONDS
            self._subscribe_search_recent = {
                key: completed_at
                for key, completed_at in recent.items()
                if completed_at > expired_before
            }
            active_media_keys = {
                media_keys[queued_id]
                for queued_id in self._subscribe_search_active
            }
            pending_media_id = next((
                queued_id
                for queued_id in self._subscribe_search_pending
                if media_keys[queued_id] == media_key
            ), None)
            recent_subjects = {
                key[0] for key in self._subscribe_search_recent
            }
            if (
                    debounce_key in self._subscribe_search_recent
                    or debounce_subject in recent_subjects
            ):
                queue_state = "防抖合并"
                queued = False
            elif None in self._subscribe_search_active or media_key in active_media_keys:
                queue_state = "同媒体运行中合并"
                queued = False
            elif None in self._subscribe_search_pending and subscribe_id is not None:
                queue_state = "全量队列合并"
                queued = False
            elif pending_media_id is not None:
                previous_state = self._subscribe_search_pending.get(pending_media_id)
                self._subscribe_search_pending[pending_media_id] = (
                    self._merge_subscribe_states(previous_state, normalized_state)
                )
                queue_state = "同媒体窗口合并"
                queued = False
            else:
                previous_state = self._subscribe_search_pending.get(subscribe_id)
                if subscribe_id is None and self._subscribe_search_pending:
                    self._subscribe_search_pending.clear()
                self._subscribe_search_pending[subscribe_id] = (
                    self._merge_subscribe_states(previous_state, normalized_state)
                )
                queue_state = "已排队"
                queued = True
            start_coordinator = queued and not self._subscribe_search_coordinator_running
            if start_coordinator:
                self._subscribe_search_coordinator_running = True
            pending_count = len(self._subscribe_search_pending)
        logger.debug(
            f"订阅卡片搜索{queue_state}：subscribe_id={subscribe_id or 'ALL'}，"
            f"媒体键={media_key}，待处理队列 {pending_count}"
        )

        if progress_callback:
            progress_callback(
                value=0,
                text=(
                    "订阅搜索已加入网盘搜索助手队列"
                    if queued else "订阅搜索已合并到网盘搜索助手任务"
                ),
            )
        if start_coordinator:
            Thread(
                target=self._drain_subscribe_search_queue,
                daemon=True,
                name="pansearch-search-queue",
            ).start()
        return True

    @staticmethod
    def _normalize_subscribe_state(state: Optional[str]) -> Optional[str]:
        """规范平台订阅状态，保留 N/R/P/S 的顺序并去重。"""
        if state is None:
            return None
        values = []
        for value in str(state).split(","):
            value = value.strip().upper()
            if value in {"N", "R", "P", "S"} and value not in values:
                values.append(value)
        return ",".join(values) or None

    @classmethod
    def _merge_subscribe_states(
            cls,
            first: Optional[str],
            second: Optional[str],
    ) -> Optional[str]:
        if first is None or second is None:
            return None
        return cls._normalize_subscribe_state(f"{first},{second}")

    def _drain_subscribe_search_queue(self) -> None:
        """按到达批次消费卡片搜索队列；同一批订阅交给现有线程池并发。"""
        try:
            while True:
                time.sleep(self._SUBSCRIBE_SEARCH_BATCH_SECONDS)
                with self._subscribe_search_queue_lock:
                    if self._subscribe_search_queue_shutdown.is_set():
                        return
                    if not self._subscribe_search_pending:
                        return
                    batch = self._subscribe_search_pending
                    self._subscribe_search_pending = {}
                    self._subscribe_search_active = batch
                    queue_revision = self._subscribe_search_queue_revision

                subscribe_ids = None if None in batch else list(batch)
                subscribe_states = batch.get(None) if subscribe_ids is None else None
                logger.debug(
                    f"开始消费订阅卡片搜索队列："
                    f"{'全部订阅' if subscribe_ids is None else len(subscribe_ids)}，"
                    f"订阅并发上限 {self._subscription_concurrency}"
                )
                future = self._submit_sync_operation(
                    {
                        "subscribe_ids": subscribe_ids,
                        "subscribe_states": subscribe_states,
                        "queue_revision": queue_revision,
                    },
                    "订阅卡片搜索",
                )
                future.result()
                with self._subscribe_search_queue_lock:
                    completed_at = time.monotonic()
                    for queued_id, queued_state in batch.items():
                        recent_key = (
                            ("ALL",)
                            if queued_id is None
                            else ("SUBSCRIBE", str(queued_id))
                        )
                        self._subscribe_search_recent[(recent_key, queued_state)] = completed_at
                    self._subscribe_search_active = {}
        finally:
            with self._subscribe_search_queue_lock:
                self._subscribe_search_active = {}
                self._subscribe_search_coordinator_running = False
                restart = bool(
                    self._subscribe_search_pending
                    and not self._subscribe_search_queue_shutdown.is_set()
                )
                if restart:
                    self._subscribe_search_coordinator_running = True
            if restart:
                Thread(
                    target=self._drain_subscribe_search_queue,
                    daemon=True,
                    name="pansearch-search-queue",
                ).start()

    def cancel_pending_subscribe_searches(self, shutdown: bool = False) -> None:
        with self._subscribe_search_queue_lock:
            self._subscribe_search_queue_revision += 1
            if shutdown:
                self._subscribe_search_queue_shutdown.set()
            self._subscribe_search_pending.clear()

    def _do_sync(
            self,
            subscribe_id: Optional[int] = None,
            subscribe_ids: Optional[List[int]] = None,
            subscribe_states: Optional[str] = None,
            manual_resources: Optional[List[Dict[str, Any]]] = None,
            manual_target: Optional[Dict[str, Any]] = None,
            history_search_targets: Optional[List[Dict[str, Any]]] = None,
            upgrade_request: Optional[Dict[str, Any]] = None,
            manual_upgrade: bool = False,
    ) -> bool:
        if self._stop_requested():
            logger.info("同步任务已收到停止请求，取消执行")
            return False

        # 至少启用一个搜索源
        if (
                not manual_resources
                and (
                    not self._search_handler
                    or not self._search_handler.get_enabled_sources()
                )
        ):
            logger.error("没有已启用且配置完整的搜索源，无法执行")
            if self._notify:
                self.post_message(
                    mtype=self._notification_type,
                    title="【网盘搜索助手】配置错误",
                    text="请至少启用并正确配置一个搜索源。"
                )
            return False

        if not self._cloud_drive:
            logger.error("网盘提供方未初始化，请检查网盘配置")
            return False
        required = {
            CloudDriveCapability.AUTHENTICATION,
            CloudDriveCapability.DIRECTORY_READ,
            CloudDriveCapability.FILE_QUERY,
            CloudDriveCapability.FILE_MUTATION,
        }
        cloud_path_only = bool(manual_resources) and all(
            str(resource.get("resource_type") or "").strip().lower() == "cloud"
            for resource in manual_resources
        )
        direct_cloud_path_only = self._direct_cloud_manual_resources(
            manual_resources
        )
        if not cloud_path_only:
            required.add(CloudDriveCapability.SHARE_TRANSFER)
        missing = [
            capability.value for capability in required
            if not self._cloud_drive.supports(capability)
        ]
        if missing:
            logger.error(
                f"{self._cloud_drive.name}缺少订阅同步所需能力：{', '.join(missing)}"
            )
            return False
        cloud_auth = self._cloud_drive.require(
            CloudDriveCapability.AUTHENTICATION
        )

        self._set_sync_status("running", "正在读取订阅列表", 5)

        try:
            if self._search_handler:
                if not manual_resources:
                    self._search_handler.reset_point_budgets()
                self._search_handler.reset_search_metrics()
        except Exception:
            pass
        if self._sync_handler:
            self._sync_handler.reset_sync_metrics()

        # 获取订阅或构造无需订阅卡片的临时媒体目标。
        if upgrade_request:
            source = str(upgrade_request.get("source") or "history").strip().lower()
            if source == "resolved":
                subscribes = list(upgrade_request.get("targets") or [])
            elif source == "media_server":
                subscribes = self._sync_handler.resolve_media_server_upgrade_targets(
                    upgrade_request.get("items") or []
                )
            else:
                subscribes = self._sync_handler.resolve_history_upgrade_targets(
                    upgrade_request.get("records") or []
                )
        elif manual_target:
            manual_media_type = str(
                manual_target.get("media_type") or ""
            ).strip().lower()
            manual_seasons = sorted({
                int(value) for value in manual_target.get("seasons") or []
            })
            if manual_media_type == "tv" and not manual_seasons:
                manual_seasons = [1]
            manual_targets = (
                [
                    {**manual_target, "season": season}
                    for season in manual_seasons
                ]
                if manual_media_type == "tv"
                else [manual_target]
            )
            subscribes = [
                self._sync_handler.build_transient_media_target(
                    target,
                    target_id=-index,
                    manual_upgrade=manual_upgrade,
                )
                for index, target in enumerate(manual_targets, start=1)
            ]
        else:
            with SessionFactory() as db:
                subscribe_oper = SubscribeOper(db=db)
                if subscribe_ids is not None or history_search_targets:
                    normalized_ids = []
                    seen_ids = set()
                    for queued_id in subscribe_ids or []:
                        try:
                            normalized_id = int(queued_id or 0)
                        except (TypeError, ValueError):
                            continue
                        if normalized_id <= 0 or normalized_id in seen_ids:
                            continue
                        seen_ids.add(normalized_id)
                        normalized_ids.append(normalized_id)
                    rows = (
                        db.query(Subscribe).filter(
                            Subscribe.id.in_(normalized_ids)
                        ).all()
                        if normalized_ids else []
                    )
                    rows_by_id = {int(row.id): row for row in rows}
                    subscribes = [
                        rows_by_id[subscribe_id]
                        for subscribe_id in normalized_ids
                        if subscribe_id in rows_by_id
                    ]
                elif subscribe_id:
                    subscribe = subscribe_oper.get(subscribe_id)
                    subscribes = [subscribe] if subscribe else []
                elif manual_resources:
                    # 关键安全守护：只要是手动转存且无订阅绑定，绝不能拉取全量订阅！
                    first_res = manual_resources[0] if manual_resources else {}
                    inferred_title = str(first_res.get("title") or "手动转存资源").strip()
                    fallback_target = {
                        "title": inferred_title,
                        "media_type": "tv" if any(
                            k in inferred_title.upper() for k in ["S0", "S1", "第", "季", "E0", "EP"]) else "movie",
                        "season": 1,
                        "seasons": [1],
                        "year": "",
                    }
                    subscribes = [
                        self._sync_handler.build_transient_media_target(
                            fallback_target,
                            target_id=-1,
                            manual_upgrade=manual_upgrade,
                        )
                    ]
                else:
                    subscribes = subscribe_oper.list(subscribe_states or "N,R")
            for index, target in enumerate(history_search_targets or [], start=1):
                subscribes.append(
                    self._sync_handler.build_transient_media_target(
                        target,
                        target_id=-index,
                        episodes=set(target.get("episodes") or []),
                    )
                )

        if not subscribes:
            logger.debug("当前没有可处理的订阅")
            if self._notify:
                self.post_message(
                    mtype=self._notification_type,
                    title="【网盘搜索助手】执行完成",
                    text="当前无订阅数据。"
                )
            return True

        if manual_upgrade:
            for subscribe in subscribes:
                setattr(subscribe, "_manual_upgrade", True)

        tv_subscribes = []
        movie_subscribes = []
        for subscribe in subscribes:
            if subscribe.type == MediaType.TV.value:
                tv_subscribes.append(subscribe)
            elif subscribe.type == MediaType.MOVIE.value:
                movie_subscribes.append(subscribe)

        if not tv_subscribes and not movie_subscribes:
            logger.debug("当前没有电影或电视剧订阅")
            return True

        exclude_ids = set(self._exclude_subscribes or [])
        all_subscribes = movie_subscribes + tv_subscribes
        excluded_count = 0
        deferred_count = 0
        postprocessing_count = 0
        unresolved_tmdb_count = 0
        active_subscribes = []
        transient_request = bool(manual_target or upgrade_request)
        history_target_request = bool(history_search_targets)
        if manual_resources or transient_request:
            active_subscribes, _ = self._deduplicate_subscribes(all_subscribes)
            if transient_request:
                active_subscribes, unresolved_tmdb_count = (
                    self._prepare_searchable_subscribes(active_subscribes)
                )
        else:
            pending_subscribe_ids = {
                int(item.get("subscribe_id") or 0)
                for item in (
                    self._sync_handler.get_pending_finalize_tasks()
                    if self._sync_handler else []
                )
                if int(item.get("subscribe_id") or 0) > 0
            }
            candidates = []
            for subscribe in all_subscribes:
                if history_target_request and bool(
                        getattr(subscribe, "_transient_target", False)
                ):
                    candidates.append(subscribe)
                    continue
                subscribe_id_value = int(getattr(subscribe, "id", 0) or 0)
                if self._is_subscribe_excluded(subscribe_id_value):
                    excluded_count += 1
                    continue
                if subscribe_id_value in pending_subscribe_ids:
                    postprocessing_count += 1
                    continue
                defer_entry = (
                    self._sync_handler.get_subscribe_defer(subscribe)
                    if self._sync_handler else None
                )
                if defer_entry:
                    deferred_count += 1
                    logger.debug(
                        f"订阅延期缓存命中，跳过本轮收集："
                        f"{getattr(subscribe, 'name', '')}，"
                        f"下次检查日期 {defer_entry.get('defer_until')}"
                    )
                    continue
                candidates.append(subscribe)

            candidates, _ = self._deduplicate_subscribes(candidates)
            prepared, unresolved_tmdb_count = self._prepare_searchable_subscribes(candidates)
            if prepared:
                logger.debug(
                    f"订阅批量预处理完成：{len(prepared)} 个唯一订阅"
                )
            for subscribe in prepared:
                preparation = getattr(
                    subscribe, "_pansearch_preparation", {}
                ) or {}
                if preparation.get("all_targets_future"):
                    deferred_count += 1
                    logger.debug(
                        f"订阅日历过滤，跳过本轮收集："
                        f"{getattr(subscribe, 'name', '')}，"
                        f"最早播出日期 {preparation.get('defer_until')}"
                    )
                    continue
                active_subscribes.append(subscribe)
        skipped_count = len(all_subscribes) - len(active_subscribes)
        total_subscribes = len(active_subscribes)
        if not active_subscribes:
            self._register_sync_tasks([])
            logger.debug(
                f"订阅收集完成，无需搜索：排除 {excluded_count} 个，"
                f"延期 {deferred_count} 个，后处理 {postprocessing_count} 个，"
                f"缺少 TMDB ID {unresolved_tmdb_count} 个"
            )
            return True

        if not cloud_auth.check_login():
            logger.error(f"{self._cloud_drive.name}登录状态校验失败")
            if self._notify:
                self.post_message(
                    mtype=self._notification_type,
                    title="【网盘搜索助手】登录失败",
                    text=f"{self._cloud_drive.name}登录凭证可能已过期，请更新后重试。"
                )
            return False

        history: List[dict] = self.get_data('history') or []
        history_by_media: Dict[Tuple[str, str, int], List[dict]] = {}
        for record in history:
            history_by_media.setdefault(
                self._history_group_key(
                    record.get("type"),
                    record.get("title"),
                    record.get("season"),
                    record.get("tmdb_id"),
                ),
                [],
            ).append(record)
        transfer_details: List[Dict[str, Any]] = []
        transferred_count = 0
        preparing_phase = (
            "准备处理手动转存"
            if manual_resources
            else "准备搜索历史媒体"
            if history_search_targets
            else "准备搜索资源"
        )
        status_text = (
            f"正在准备手动转存：{active_subscribes[0].name}"
            if manual_resources and active_subscribes
            else f"已加载 {total_subscribes} 个媒体目标，{preparing_phase}"
        )
        self._set_sync_status(
            "running",
            status_text,
            8,
            {
                "current": 0,
                "total": total_subscribes,
                "transferred": 0,
                "phase": preparing_phase,
            },
        )
        self._register_sync_tasks(active_subscribes)
        grouped_subscribes = {}
        for subscribe in active_subscribes:
            grouped_subscribes.setdefault(
                self._sync_media_key(subscribe), []
            ).append(subscribe)

        completed_subscribes = 0
        if grouped_subscribes:
            ordered_manual_seasons = bool(
                manual_target
                and str(manual_target.get("media_type") or "").strip().lower() == "tv"
                and len(manual_target.get("seasons") or []) > 1
            )
            worker_count = (
                1
                if ordered_manual_seasons
                else min(
                    self._subscription_concurrency,
                    len(grouped_subscribes),
                    self._cloud_drive.policy.max_concurrency,
                )
            )
            logger.debug(
                f"{'手动多季顺序调度' if ordered_manual_seasons else '订阅并发调度'}："
                f"{total_subscribes} 个媒体目标，"
                f"{len(grouped_subscribes)} 个媒体队列，并发数 {worker_count}"
            )
            executor = ThreadPoolExecutor(
                max_workers=worker_count,
                thread_name_prefix="pansearch-subscribe",
            )
            stop_waiting = False
            try:
                future_groups = {
                    executor.submit(
                        self._run_subscription_group,
                        group,
                        history_by_media.get(
                            self._history_group_key(
                                getattr(group[0], "type", ""),
                                getattr(group[0], "name", ""),
                                getattr(group[0], "season", None),
                                tmdb_id_of(group[0]),
                            ),
                            [],
                        ),
                        exclude_ids,
                        manual_resources,
                    ): group
                    for group in grouped_subscribes.values()
                }

                def collect_future_result(future) -> None:
                    nonlocal completed_subscribes, transferred_count
                    group = future_groups[future]
                    try:
                        result = future.result()
                    except Exception as error:
                        logger.error(f"订阅并发任务异常：{error}")
                        result = {
                            "history": [],
                            "transfer_details": [],
                            "transferred": 0,
                        }
                        for subscribe in group:
                            self._update_sync_task(
                                self._sync_task_id(subscribe),
                                status="failed",
                                phase="处理失败",
                                progress=100,
                                message=str(error),
                            )
                    new_history_records = result["history"]
                    history.extend(new_history_records)
                    if new_history_records:
                        self._sync_handler._timed_sync_call(
                            "history_persist",
                            self._sync_handler.append_history_records,
                            new_history_records,
                        )
                    # 每个并行订阅组完成并持久化历史后立即入队，避免停止同步时
                    # 只完成了前几个任务却因整批收尾未执行而漏发通知。
                    completed_details = result.get("transfer_details") or []
                    if completed_details:
                        completed_count = sum(
                            len(item.get("episodes") or [])
                            if item.get("type") == "电视剧"
                            else 1
                            for item in completed_details
                        )
                        if self._notify:
                            self._sync_handler.send_transfer_notification(
                                completed_details, completed_count
                            )
                        if self._webhook_handler:
                            self._webhook_handler.send_transfer_complete(
                                transfer_details=completed_details,
                                total_count=completed_count,
                            )
                    transfer_details.extend(result["transfer_details"])
                    transferred_count += int(result["transferred"] or 0)
                    completed_subscribes += len(group)
                    progress_text = (
                        f"正在按季整理媒体（{completed_subscribes}/{total_subscribes}）"
                        if direct_cloud_path_only
                        else f"正在按季处理媒体（{completed_subscribes}/{total_subscribes}）"
                        if ordered_manual_seasons
                        else f"正在并行处理订阅（{completed_subscribes}/{total_subscribes}）"
                    )
                    self._set_sync_status(
                        "running",
                        progress_text,
                        10 + int(completed_subscribes / max(total_subscribes, 1) * 85),
                        {
                            "current": completed_subscribes,
                            "total": total_subscribes,
                            "transferred": transferred_count,
                            "phase": (
                                "按季整理网盘文件"
                                if direct_cloud_path_only
                                else "按季搜索与转存"
                                if ordered_manual_seasons
                                else "并行搜索与转存"
                            ),
                            "concurrency": worker_count,
                        },
                    )

                pending_futures = set(future_groups)
                while pending_futures:
                    completed_futures, pending_futures = wait(
                        pending_futures,
                        timeout=0.5,
                        return_when=FIRST_COMPLETED,
                    )
                    if not completed_futures:
                        if self._stop_requested():
                            stop_waiting = True
                            break
                        continue

                    for future in completed_futures:
                        collect_future_result(future)

                    if self._stop_requested():
                        stop_waiting = True
                        break

                if stop_waiting:
                    if pending_futures:
                        completed_futures, pending_futures = wait(
                            pending_futures, timeout=0
                        )
                    else:
                        completed_futures = set()
                    for future in completed_futures:
                        collect_future_result(future)
                    for future in pending_futures:
                        group = future_groups[future]
                        cancelled = future.cancel()
                        for subscribe in group:
                            self._update_sync_task(
                                self._sync_task_id(subscribe),
                                status="stopped",
                                phase="已取消" if cancelled else "已停止等待当前调用",
                                progress=100,
                            )
                    logger.debug(
                        f"停止等待订阅工作线程：待返回 {len(pending_futures)} 个，"
                        "已取消尚未开始的任务"
                    )
            finally:
                executor.shutdown(
                    wait=not stop_waiting,
                    cancel_futures=stop_waiting,
                )

        if skipped_count:
            mode_label = "指定模式" if self._subscribe_filter_mode == "include" else "排除模式"
            logger.debug(f"订阅过滤（{mode_label}）：跳过 {skipped_count} 个订阅")

        action_name = "整理" if direct_cloud_path_only else "转存"
        if self._stop_requested():
            logger.info(
                f"网盘订阅同步已停止，停止前共{action_name} "
                f"{transferred_count} 个文件"
            )
            if self._notify:
                self.post_message(
                    mtype=self._notification_type,
                    title="【网盘搜索助手】任务已停止",
                    text=(
                        f"已按请求停止处理，停止前共{action_name} "
                        f"{transferred_count} 个文件。"
                    )
                )
            return False

        self._set_sync_status(
            "running",
            "正在完成本次订阅任务",
            98,
            {
                "current": total_subscribes,
                "total": total_subscribes,
                "transferred": transferred_count,
                "action_name": action_name,
                "phase": "保存结果与发送通知",
            },
        )
        logger.info(
            f"网盘订阅同步完成，共{action_name} {transferred_count} 个文件"
        )
        pending_finalize_count = 0
        if self._sync_handler:
            pending_finalize_tasks = self._sync_handler.get_pending_finalize_tasks()
            pending_finalize_count = len(pending_finalize_tasks)
            offline_pending_count = sum(
                str(item.get("task_type") or "share").strip().lower()
                in {"ed2k", "magnet"}
                for item in pending_finalize_tasks
            )
            cloud_pending_count = pending_finalize_count - offline_pending_count
            self._sync_context["pending_finalize"] = pending_finalize_count
            self._sync_context["offline_pending"] = offline_pending_count
            self._sync_context["cloud_pending"] = cloud_pending_count
            if pending_finalize_count:
                logger.debug(
                    f"本次仍有 {pending_finalize_count} 个文件等待网盘文件就绪，"
                    "暂不发送完成确认"
                )
        if self._sync_handler:
            sync_metrics = self._sync_handler.get_sync_metrics()
            if sync_metrics:
                summary = [
                    f"{name} {metric.get('calls', 0)} 次/{metric.get('elapsed_ms', 0)}ms"
                    for name, metric in sorted(sync_metrics.items())
                ]
                logger.debug(f"同步阶段耗时汇总：{'；'.join(summary)}")
        if self._search_handler:
            metrics = self._search_handler.get_search_metrics()
            if metrics:
                summary = [
                    (
                        f"{source.upper()} 外部 {counters.get('external_calls', 0)} 次/"
                        f"{counters.get('external_elapsed_ms', 0)}ms，"
                        f"正缓存 {counters.get('positive_cache_hits', 0)} 次，"
                        f"负缓存 {counters.get('negative_cache_hits', 0)} 次"
                    )
                    for source, counters in sorted(metrics.items())
                ]
                logger.debug(f"搜索性能汇总：{'；'.join(summary)}")

        if self._notify and transferred_count == 0 and not manual_resources:
            self.post_message(
                mtype=self._notification_type,
                title="【网盘搜索助手】执行完成",
                text=f"本次同步未发现需要{action_name}的新资源。"
            )

        return True

    def _apply_global_config_once(self):
        """安装确认后首次执行时，应用一次系统级洗版配置。
        放在 sync_subscribes() 开头调用，确保插件加载成功后再修改订阅状态。
        """
        if self._global_config_applied:
            return
        try:
            # 批量评分（已选独立洗版订阅且ids有变化时自动触发）
            if self._upgrade_subscribe_ids:
                current_hash = str(sorted(str(i) for i in self._upgrade_subscribe_ids))
                if current_hash != self._last_scored_ids_hash:
                    logger.info("检测到独立洗版订阅列表有变化，自动触发整理记录评分")
                    self._batch_re_score()
                    self._last_scored_ids_hash = current_hash
            self._global_config_applied = True
            logger.info("插件全局配置已应用：洗版")
        except Exception as e:
            logger.error(f"插件全局配置应用失败（下次首次执行重试）: {e}")

    def _release_sync_resources(self, notification_batch_started: bool) -> None:
        if self._search_handler:
            try:
                self._search_handler.close()
            except Exception as error:
                logger.warning(f"同步结束关闭搜索客户端失败：{error}")
        try:
            # 配置重载会关闭旧 SyncHandler；必须避开正在使用它的后处理线程。
            with self._offline_monitor_lock:
                if notification_batch_started and self._sync_handler:
                    try:
                        self._sync_handler.finish_notification_batch()
                    except Exception as error:
                        logger.warning(f"同步结束提交媒体库刷新失败：{error}")
                self._apply_pending_config()
        finally:
            sync_lock.release()

    def sync_subscribes(
            self,
            subscribe_id: Optional[int] = None,
            subscribe_ids: Optional[List[int]] = None,
            subscribe_states: Optional[str] = None,
            progress_callback: Optional[Callable[..., None]] = None,
            manual_resources: Optional[List[Dict[str, Any]]] = None,
            manual_target: Optional[Dict[str, Any]] = None,
            history_search_targets: Optional[List[Dict[str, Any]]] = None,
            upgrade_request: Optional[Dict[str, Any]] = None,
            manual_upgrade: bool = False,
            wait_for_slot: bool = False,
            queue_revision: Optional[int] = None,
            result: Optional[Dict[str, Any]] = None,
            lock_acquired: bool = False,
    ) -> bool:
        is_full_sync = (
                subscribe_id is None
                and subscribe_ids is None
                and subscribe_states is None
                and not manual_resources
                and not manual_target
                and not history_search_targets
                and not upgrade_request
        )
        if lock_acquired:
            pass
        elif wait_for_slot:
            def queue_cancelled() -> bool:
                return bool(
                    self._subscribe_search_queue_shutdown.is_set()
                    or (
                            queue_revision is not None
                            and queue_revision != self._subscribe_search_queue_revision
                    )
                )

            while not sync_lock.acquire(timeout=0.5):
                if queue_cancelled():
                    if result is not None:
                        result.update(self._sync_execution_result(
                            False, "排队任务已取消"
                        ))
                    return False
            if queue_cancelled():
                sync_lock.release()
                if result is not None:
                    result.update(self._sync_execution_result(
                        False, "排队任务已取消"
                    ))
                return False
        elif not sync_lock.acquire(blocking=False):
            logger.debug("已有订阅追更任务正在运行，跳过重复请求")
            if result is not None:
                result.update(self._sync_execution_result(
                    False, "已有订阅任务正在运行"
                ))
            return False
        notification_batch_started = False
        run_context: Dict[str, Any] = {}
        task_counts: Dict[str, int] = {}
        stop_requested = False
        try:
            with self._offline_monitor_lock:
                self._apply_pending_config()
            # 首次成功运行时才应用系统级配置（避免安装失败却污染MP配置）
            if is_full_sync:
                self._apply_global_config_once()
            if self._stop_event is None:
                self._stop_event = ThreadEvent()
            self._stop_event.clear()
            self._sync_running = True
            self._sync_run_started_at = time.time()
            self._set_sync_status("running", "正在准备订阅任务", 0, {})
            if self._sync_handler:
                notification_batch_started = self._sync_handler.begin_notification_batch()
            success = False
            try:
                if progress_callback:
                    progress_callback(value=0, text="网盘搜索助手开始处理订阅搜索")
                success = self._do_sync(
                    subscribe_id=subscribe_id,
                    subscribe_ids=subscribe_ids,
                    subscribe_states=subscribe_states,
                    manual_resources=manual_resources,
                    manual_target=manual_target,
                    history_search_targets=history_search_targets,
                    manual_upgrade=manual_upgrade,
                    upgrade_request=upgrade_request,
                )
            except Exception as e:
                logger.error(f"同步任务异常：{e}")
                success = False
            finally:
                stop_requested = self._stop_requested()
                run_context = dict(self._sync_context or {})
                with self._sync_tasks_lock:
                    current_tasks = [
                        task for task in self._sync_tasks.values()
                        if float(task.get("queued_at") or 0)
                           >= self._sync_run_started_at
                    ]
                for task in current_tasks:
                    status = str(task.get("status") or "unknown")
                    task_counts[status] = task_counts.get(status, 0) + 1
                self._sync_last_elapsed_ms = int(
                    max(0.0, time.time() - self._sync_run_started_at) * 1000
                )
                self._sync_last_finished_at = time.time()
                self._sync_running = False
                self._set_sync_status(
                    "idle",
                    "订阅任务已停止" if stop_requested else "当前没有订阅处理任务",
                    self._sync_progress if stop_requested else 100,
                    {},
                )
                if progress_callback:
                    progress_callback(
                        value=100,
                        text="订阅搜索已停止" if stop_requested else "订阅搜索完成"
                    )
                if self._sync_handler and is_full_sync and not stop_requested:
                    if self._enable_cloud_upgrade:
                        now = time.time()
                        if now - getattr(self, '_last_cloud_cleanup', 0) > 86400:
                            self._last_cloud_cleanup = now
                            self._sync_handler.auto_upgrade_scan()
                        self_heal_interval = max(0, self._self_heal_interval) * 60
                        if (
                                self_heal_interval
                                and now - getattr(self, '_last_self_heal_cleanup', 0)
                                >= self_heal_interval
                        ):
                            self._last_self_heal_cleanup = now
                            self._sync_handler._self_heal_cleanup()
            if result is not None:
                transferred = int(run_context.get("transferred") or 0)
                action_name = str(
                    run_context.get("action_name") or "转存"
                )
                if stop_requested:
                    message = "订阅搜索已停止"
                elif task_counts.get("failed"):
                    success = False
                    message = (
                        f"订阅搜索完成，但有 {task_counts['failed']} 个订阅处理失败"
                    )
                elif not success:
                    message = "订阅搜索执行失败"
                elif transferred and run_context.get("pending_finalize"):
                    pending_count = int(run_context["pending_finalize"] or 0)
                    offline_count = int(run_context.get("offline_pending") or 0)
                    cloud_count = int(run_context.get("cloud_pending") or 0)
                    if offline_count and cloud_count:
                        pending_text = (
                            f"其中 {offline_count} 个离线文件等待下载、"
                            f"{cloud_count} 个网盘文件等待就绪，"
                        )
                    elif offline_count:
                        pending_text = f"其中 {offline_count} 个离线文件等待下载，"
                    else:
                        pending_text = f"其中 {pending_count} 个网盘文件等待就绪，"
                    message = (
                        f"订阅搜索已提交{action_name} {transferred} 个文件，"
                        f"{pending_text}"
                        "完成后将再通知"
                    )
                elif transferred:
                    message = (
                        f"订阅搜索完成，共{action_name} {transferred} 个文件"
                    )
                else:
                    message = (
                        f"订阅搜索完成，未发现需要{action_name}的新资源"
                    )
                result.update(self._sync_execution_result(
                    success,
                    message,
                    context=run_context,
                    elapsed_ms=self._sync_last_elapsed_ms,
                    stopped=stop_requested,
                    task_counts=task_counts,
                ))
            return success
        finally:
            self._release_sync_resources(notification_batch_started)

    @staticmethod
    def _sync_execution_result(
            success: bool,
            message: str,
            context: Optional[Dict[str, Any]] = None,
            elapsed_ms: int = 0,
            stopped: bool = False,
            task_counts: Optional[Dict[str, int]] = None,
    ) -> Dict[str, Any]:
        context = context or {}
        return {
            "success": bool(success),
            "message": message,
            "data": {
                "processed": int(context.get("current") or 0),
                "total": int(context.get("total") or 0),
                "transferred": int(context.get("transferred") or 0),
                "elapsed_ms": max(0, int(elapsed_ms or 0)),
                "stopped": bool(stopped),
                "task_counts": dict(task_counts or {}),
            },
        }
