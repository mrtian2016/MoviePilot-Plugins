"""电视剧订阅搜索、匹配与转存流程。"""

import datetime
from typing import Any, Dict, List, Optional, Set

from app.log import logger
from app.schemas import MediaInfo
from app.schemas.types import MediaType
from app.utils.string import StringUtils

from ..notification import EmbyMediaResolver
from ...core import OwnerDelegator


class TelevisionSyncProcessor(OwnerDelegator):
    """处理电视剧订阅同步。"""

    def process_tv_subscribe(
            self,
            subscribe,
            history: List[dict],
            transfer_details: List[Dict[str, Any]],
            transferred_count: int,
            exclude_ids: Set[int],
            manual_resources: Optional[List[Dict[str, Any]]] = None,
            allow_upgrade: bool = True,
            manual_upgrade: bool = False,
            target_episodes: Optional[Set[int]] = None,
            transient_target: bool = False,
    ) -> int:
        """
        处理单个电视剧订阅

        :param subscribe: 订阅对象
        :param history: 历史记录列表
        :param transfer_details: 转存详情列表
        :param transferred_count: 当前已转存数量
        :param exclude_ids: 排除的订阅ID集合
        :return: 更新后的转存数量
        """
        track_points = not manual_resources
        try:
            if self._stop_requested():
                return transferred_count
            # 原生 best_version 决定洗版订阅，插件范围进一步限制处理对象。
            if (
                    allow_upgrade
                    and (manual_upgrade or self._is_cloud_upgrade_subscribe(subscribe))
            ):
                return self._process_tv_subscribe_upgrade(
                    subscribe=subscribe,
                    history=history,
                    transfer_details=transfer_details,
                    transferred_count=transferred_count,
                    exclude_ids=exclude_ids,
                    target_episodes=target_episodes,
                    manual_upgrade=manual_upgrade,
                    manual_resources=manual_resources,
                )

            logger.debug(
                f"📺 处理订阅：{subscribe.name} S{subscribe.season or 1:02d}，"
                f"范围 E{subscribe.start_episode or 1:02d}-E{subscribe.total_episode or 0:02d}，"
                f"订阅记录缺失 {subscribe.lack_episode} 集"
            )

            # 加载该订阅的历史积分花费（用 tmdb_id + 季数作为唯一标识）
            sub_key = self.subscription_budget_key(subscribe, MediaType.TV)
            if track_points and self._search_handler:
                self._search_handler.reset_subscription_budgets(sub_key)

            mediainfo: MediaInfo = self._subscribe_mediainfo(
                subscribe, MediaType.TV, cache=False
            )

            if not mediainfo:
                logger.warn(f"无法识别媒体信息：{subscribe.name}")
                return transferred_count

            self._set_task_phase(subscribe, "核对播出范围", 20)
            season = subscribe.season or 1
            total_ep = subscribe.total_episode or 0
            start_ep = subscribe.start_episode or 1
            expected_episodes = set(range(start_ep, total_ep + 1)) if total_ep >= start_ep else set()
            discover_manual_episodes = bool(
                manual_resources and transient_target and not expected_episodes
            )
            missing_episodes: List[int] = []
            discovered_manual_episodes: Set[int] = set()
            target_episode_air_dates: Dict[int, str] = {}
            calendar_entry: Optional[Dict[str, Any]] = None
            # 收集阶段已读取 TMDB 季网页；这里复用结果，避免重复请求网页。
            if expected_episodes and mediainfo.tmdb_id and not manual_resources:
                preparation = getattr(
                    subscribe, "_pansearch_preparation", {}
                ) or {}
                calendar_entry = preparation.get("calendar")
                if not calendar_entry:
                    calendar_entry = self.get_tv_subscribe_calendar(
                        subscribe, tmdb_id=mediainfo.tmdb_id
                    )
                if calendar_entry:
                    target_episode_air_dates = {
                        int(episode): str(air_date)
                        for episode, value in (
                                calendar_entry.get("aired_episode_air_dates") or {}
                        ).items()
                        if (air_date := str(value or "").strip())
                    }
                    if calendar_entry.get("all_targets_future"):
                        logger.debug(
                            f"{mediainfo.title_year} S{season:02d} "
                            "所有目标集均未播出，跳过物理校验"
                        )
                        return transferred_count
                    if calendar_entry.get("unknown_episodes"):
                        boundary = int(
                            calendar_entry.get("unreleased_boundary_episode") or 0
                        )
                        boundary_reason = str(
                            calendar_entry.get("unreleased_boundary_reason") or ""
                        )
                        boundary_text = ""
                        if boundary:
                            boundary_text = (
                                f"，TMDB 网页仅返回至 E{boundary - 1:02d}，"
                                f"按 E{boundary:02d} 未播边界过滤"
                                if boundary_reason == "unknown_tail"
                                else f"，按 E{boundary:02d} 未播边界过滤"
                            )
                        logger.debug(
                            f"{mediainfo.title_year} S{season:02d} "
                            f"目标集播出日期不完整"
                            f"{boundary_text}，"
                            "继续物理校验"
                        )
                else:
                    logger.info(
                        f"{mediainfo.title_year} S{season:02d} "
                        "TMDB 季网页未返回剧集信息，跳过播出过滤"
                    )
            # 1. 先读取 Emby 实际剧集，不混入订阅 note。
            self._set_task_phase(subscribe, "检查媒体库内容", 30)
            emby_valid, emby_episodes = self._timed_sync_call(
                "emby_scan",
                EmbyMediaResolver.episode_numbers,
                self._chain,
                mediainfo,
                season,
            )
            existing_episodes_in_resources: Set[int] = (
                    emby_episodes & expected_episodes
            )
            if not emby_valid:
                if transient_target:
                    emby_episodes = set()
                    logger.debug(
                        f"{mediainfo.title_year} S{season:02d} 未读取到 Emby 数据，"
                        "临时媒体目标继续按网盘实际内容检查"
                    )
                else:
                    logger.warning(
                        f"{mediainfo.title_year} S{season:02d} 无法读取 Emby 实际数据，"
                        "本轮跳过且不访问115，不修改订阅进度"
                    )
                    return transferred_count
            logger.debug(
                f"Emby 实际存在剧集："
                f"{self._format_episode_ranges(emby_episodes & expected_episodes)}"
            )

            # 2. 再读取115目标目录；不扫描本地 STRM 路径。
            self._set_task_phase(subscribe, "检查网盘内容", 40)
            cloud_valid, cloud_episodes, cloud_label = self._timed_sync_call(
                "cloud_scan",
                self._scan_cloud_resource_episodes,
                subscribe=subscribe,
                mediainfo=mediainfo,
                season=season,
                start_episode=start_ep,
                total_episode=total_ep,
            )
            if not cloud_valid:
                logger.warning(
                    f"{mediainfo.title_year} S{season:02d} 无法读取115实际数据，"
                    "本轮跳过，不修改订阅进度"
                )
                return transferred_count
            existing_episodes_in_resources.update(cloud_episodes & expected_episodes)
            logger.debug(
                f"115 实际存在剧集：{cloud_label}，"
                f"{self._format_episode_ranges(cloud_episodes & expected_episodes)}"
            )

            if expected_episodes:
                current_note = {
                                   int(episode) for episode in (subscribe.note or [])
                                   if str(episode).isdigit()
                               } & expected_episodes
                missing_episodes = sorted(expected_episodes - existing_episodes_in_resources)
                restored_missing = set(missing_episodes) & current_note
                if not transient_target:
                    self._reconcile_subscribe_physical_episodes(
                        subscribe=subscribe,
                        episodes=existing_episodes_in_resources,
                        start_episode=start_ep,
                        total_episode=total_ep,
                    )
                logger.debug(
                    f"Emby 与115合并后已存在 "
                    f"{self._format_episode_ranges(existing_episodes_in_resources)}，缺失 "
                    f"{self._format_episode_ranges(set(missing_episodes))}"
                )
                if restored_missing:
                    logger.warning(
                        "Emby 与115均不存在，已删除订阅误标并恢复缺集："
                        f"{self._format_episode_ranges(restored_missing)}"
                    )

            if not missing_episodes and not discover_manual_episodes:
                logger.info(f"{mediainfo.title_year} S{season:02d} Emby 与115已完整存在")
                if not transient_target:
                    self._subscribe_handler.check_and_finish_subscribe(
                        subscribe=subscribe,
                        mediainfo=mediainfo,
                        success_episodes=sorted(existing_episodes_in_resources),
                    )
                if track_points and self._search_handler:
                    self._search_handler.clear_subscription_budgets(sub_key)
                return transferred_count

            # 过滤掉小于开始集数的剧集。
            if subscribe.start_episode:
                missing_episodes = [
                    episode for episode in missing_episodes
                    if episode >= subscribe.start_episode
                ]

            # 物理校验完成后，仅对已播出且实际缺失的集数继续搜索。
            if calendar_entry:
                unreleased_episodes = {
                    int(episode)
                    for episode in (
                            calendar_entry.get("unreleased_episodes") or []
                    )
                }
                not_aired = [
                    episode
                    for episode in missing_episodes
                    if episode in unreleased_episodes
                ]
                if not_aired:
                    not_aired_set = set(not_aired)
                    missing_episodes = [
                        episode
                        for episode in missing_episodes
                        if episode not in not_aired_set
                    ]
                    logger.debug(
                        f"{mediainfo.title_year} S{season:02d} 跳过未播出剧集："
                        f"{self._format_episode_ranges(not_aired_set)}"
                    )
                    if not missing_episodes:
                        defer_until = self._calendar_date(
                            calendar_entry.get("next_air_date")
                        )
                        if defer_until and defer_until > datetime.date.today():
                            self.defer_subscribe_until(
                                subscribe,
                                defer_until,
                                f"缺失剧集最早于 {defer_until.isoformat()} 播出",
                            )
                        logger.debug(
                            f"{mediainfo.title_year} S{season} "
                            "所有缺失剧集均未播出，跳过"
                        )
                        return transferred_count

            logger.debug(
                f"{mediainfo.title_year} S{season:02d} 待转存剧集："
                f"{self._format_episode_ranges(set(missing_episodes))}"
            )

            # 成功转存的集数列表
            success_episodes = []

            # 手动资源作为单一来源进入现有匹配转存链。
            enabled_sources = (
                ["manual"] if manual_resources
                else self._search_handler.get_enabled_sources()
            )

            if not enabled_sources:
                logger.warning(f"没有可用的搜索源，跳过 {mediainfo.title} S{season} 的搜索")
                return transferred_count

            prefetched_results = (
                {"manual": [dict(resource) for resource in manual_resources]}
                if manual_resources else {}
            )
            self._set_task_phase(
                subscribe,
                "处理手动网盘资源" if manual_resources else "搜索缺失剧集",
                55,
            )
            if not manual_resources:
                prefetched_results = self._search_handler.search_sources(
                    sources=enabled_sources,
                    mediainfo=mediainfo,
                    media_type=MediaType.TV,
                    season=season,
                    target_episodes=missing_episodes,
                    target_episode_air_dates=target_episode_air_dates,
                    subscribe=subscribe,
                )
            resource_batches = self._build_transfer_resource_batches(
                enabled_sources, prefetched_results
            )
            seen_share_urls = set()
            search_label = self._search_handler._search_label(
                mediainfo, MediaType.TV, season
            )
            for source_index, (source, candidate_resources, is_cross_batch) in enumerate(
                    resource_batches
            ):
                search_prefix = f"[{search_label}][{source.upper()}]"
                if self._stop_requested():
                    break
                if not missing_episodes and not discover_manual_episodes:
                    logger.debug(f"{mediainfo.title_year} S{season} 所有缺失剧集已转存完成，不再查询后续源")
                    break

                logger.debug(
                    f"🔎 {search_prefix} 开始处理"
                    f"{'跨盘' if is_cross_batch else '目标网盘'}候选"
                    f"（当前缺失: {len(missing_episodes)} 集）"
                )
                self._set_task_phase(
                    subscribe,
                    f"处理 {source.upper()} "
                    f"{'跨盘' if is_cross_batch else '目标网盘'}候选",
                    55 + int((source_index + 1) / len(resource_batches) * 15),
                )

                logger.info(
                    f"{search_prefix} 找到候选资源："
                    f"{self._format_resource_summary(candidate_resources)}"
                )

                # 遍历搜索结果
                for resource_index, resource in enumerate(candidate_resources):
                    if self._stop_requested():
                        break
                    self._set_task_phase(
                        subscribe,
                        f"检查候选资源 {resource_index + 1}/{len(candidate_resources)}",
                        72 + int((resource_index + 1) / len(candidate_resources) * 16),
                    )

                    share_url = resource.get("url", "")
                    resource_title = resource.get("title", "")

                    share_url = self._resolve_candidate_resource_url(
                        candidate_resources,
                        resource_index,
                        resource,
                        search_label,
                        log_prefix=search_prefix,
                    )
                    if self._stop_requested():
                        break

                    if not share_url:
                        continue

                    share_url = share_url.strip()
                    if not self._is_supported_resource(resource, share_url):
                        logger.warning(
                            f"跳过当前同步链不支持的资源类型 "
                            f"{self._supported_resource_type(resource, share_url)}：{resource_title}"
                        )
                        continue
                    resource_urls = [share_url]
                    if self._is_ed2k_url(share_url):
                        for grouped_resource in candidate_resources:
                            grouped_url = str(grouped_resource.get("url") or "").strip()
                            if (
                                    self._is_ed2k_url(grouped_url)
                                    and grouped_url not in resource_urls
                            ):
                                resource_urls.append(grouped_url)
                    resource_by_url = {
                        str(item.get("url") or "").strip(): item
                        for item in candidate_resources
                        if str(item.get("url") or "").strip()
                    }
                    resource_urls = [
                        url for url in resource_urls if url not in seen_share_urls
                    ]
                    if not resource_urls:
                        logger.debug(f"跳过重复分享链接：{resource_title}")
                        continue
                    seen_share_urls.update(resource_urls)

                    resource_input_label = self._resource_input_label(share_url)
                    if len(resource_urls) > 1:
                        logger.debug(
                            f"合并检查 {len(resource_urls)} 条互补ED2K资源：{resource_title}"
                        )
                    else:
                        logger.debug(
                            f"检查{resource_input_label}：{resource_title} - "
                            f"{self._resource_log_reference(share_url)}"
                        )

                    try:
                        missing_episode_set = set(missing_episodes)
                        if self._is_offline_url(share_url) or self._is_magnet_url(share_url):
                            if self._is_offline_blacklisted(resource, share_url):
                                logger.info(
                                    f"🚫 离线任务命中黑名单（1天内失败或超时），跳过该资源并选择其他候选：{resource_title}"
                                )
                                continue

                        if self._is_magnet_url(share_url):
                            magnet_title = self._prepare_magnet_resource(
                                resource, share_url
                            )
                            title_seasons = self._magnet_title_seasons(resource)
                            if title_seasons and season not in title_seasons:
                                logger.debug(
                                    f"Magnet 标题预过滤排除：标题季数="
                                    f"{','.join(f'S{value:02d}' for value in sorted(title_seasons))}，"
                                    f"目标季数=S{season:02d}，"
                                    f"标题={magnet_title or resource_title}"
                                )
                                continue
                            title_episodes = self._magnet_title_episodes(
                                resource, season
                            )
                            if title_episodes:
                                target_episode_set = (
                                    title_episodes - discovered_manual_episodes
                                    if discover_manual_episodes
                                    else missing_episode_set & title_episodes
                                )
                                if not target_episode_set:
                                    logger.debug(
                                        f"Magnet 标题预过滤排除：标题集数="
                                        f"{self._format_episode_ranges(title_episodes)}，"
                                        f"当前缺集={self._format_episode_ranges(missing_episode_set)}，"
                                        f"标题={magnet_title or resource_title}"
                                    )
                                    continue
                                target_episodes = sorted(target_episode_set)
                            else:
                                preview_episodes = self._resource_preview_episodes(
                                    resource, season
                                )
                                target_episode_set = (
                                    preview_episodes - discovered_manual_episodes
                                    if discover_manual_episodes
                                    else missing_episode_set & preview_episodes
                                    if preview_episodes
                                    else missing_episode_set
                                )
                                target_episodes = sorted(target_episode_set)

                            if not self._validate_resource_url(
                                    share_url,
                                    resource_label="Magnet 链接",
                                    log_prefix=search_prefix,
                            ):
                                continue
                            if not target_episodes:
                                logger.debug(
                                    f"Magnet 预览集数未覆盖当前缺集：预览="
                                    f"{self._format_episode_ranges(preview_episodes)}，"
                                    f"当前缺集={self._format_episode_ranges(missing_episode_set)}，"
                                    f"标题={magnet_title or resource_title}"
                                )
                                continue
                            pending_key = self._queue_magnet_package(
                                resource,
                                share_url,
                                subscribe,
                                mediainfo,
                                season=season,
                                target_episodes=target_episodes,
                                sub_key=sub_key if track_points else "",
                                transient_target=transient_target,
                            )
                            if not pending_key:
                                continue
                            if discover_manual_episodes:
                                discovered_manual_episodes.update(target_episodes)
                            provider_name = str(
                                (resource.get("magnet_metadata") or {}).get("display_name")
                                or resource_title
                            ).strip()
                            self._append_magnet_pending_history(
                                history=history,
                                mediainfo=mediainfo,
                                subscribe=subscribe,
                                share_url=share_url,
                                cloud_dir=self._cloud_transfer_path.rstrip('/') or "/",
                                resource=resource,
                                season=season,
                                target_episodes=target_episodes,
                                finalize_key=pending_key,
                            )
                            logger.info(
                                f"Magnet 已进入下载后真实文件匹配：{provider_name}，"
                                f"目标 {self._format_episode_ranges(set(target_episodes))}"
                            )
                            continue
                        share_files = []
                        for current_url in resource_urls:
                            share_files.extend(self._validated_resource_files(
                                current_url,
                                resource_title=resource_title,
                                target_season=(season if self._skip_other_season_dirs else None),
                                log_prefix=search_prefix,
                            ))
                        if not share_files:
                            continue

                        video_count, share_episodes = self._summarize_share_episodes(
                            share_files,
                            season,
                            mediainfo if is_cross_batch else None,
                        )
                        matched_episode_numbers = (
                            share_episodes - discovered_manual_episodes
                            if discover_manual_episodes
                            else missing_episode_set & share_episodes
                        )
                        absent_episode_numbers = (
                            set()
                            if discover_manual_episodes
                            else missing_episode_set - share_episodes
                        )
                        logger.debug(
                            f"{resource_input_label}实际包含 {video_count} 个视频，"
                            f"S{season:02d} 可识别集数："
                            f"{self._format_episode_ranges(share_episodes)}；"
                            f"当前缺失中可用：{self._format_episode_ranges(matched_episode_numbers)}"
                        )
                        if video_count and not share_episodes:
                            reason = (
                                "跨盘分享内容未被平台识别为目标媒体"
                                if is_cross_batch
                                else "目标网盘分享未识别到目标季集数"
                            )
                            logger.debug(
                                f"{reason}：{mediainfo.title_year} "
                                f"S{season:02d}，已跳过该资源"
                            )
                            continue
                        if absent_episode_numbers:
                            logger.debug(
                                f"{resource_input_label}未包含当前缺失集数："
                                f"{self._format_episode_ranges(absent_episode_numbers)}"
                            )

                        # 收集该分享中所有匹配的文件
                        matched_items = []
                        episodes_to_match = (
                            sorted(matched_episode_numbers)
                            if discover_manual_episodes
                            else [
                                episode for episode in missing_episodes
                                if not share_episodes or episode in share_episodes
                            ]
                        )
                        matched_files = self._match_episode_files(
                            share_files,
                            mediainfo,
                            subscribe,
                            season,
                            episodes_to_match,
                            require_media_match=is_cross_batch,
                        )

                        for episode in episodes_to_match:
                            matched_file, current_score = matched_files.get(
                                episode, (None, 0)
                            )

                            if matched_file:
                                file_name = matched_file.get('name', '')
                                logger.debug(f"找到匹配文件：{file_name} -> E{episode:02d}")

                                is_upgrade = False

                                target_dir, target_name = self._platform_target(
                                    self._CLOUD_MEDIA_ROOT, subscribe, mediainfo,
                                    file_name, season, episode
                                )
                                matched_items.append({
                                    "file": matched_file,
                                    "resource": resource_by_url.get(
                                        str(matched_file.get("url") or "").strip(), resource
                                    ),
                                    "episode": episode,
                                    "score": current_score,
                                    "is_upgrade": is_upgrade,
                                    "target_dir": target_dir,
                                    "target_name": target_name,
                                    "subtitle_files": self._companion_subtitle_files(
                                        share_files,
                                        matched_file,
                                        season=season,
                                        episode=episode,
                                    ),
                                })

                        if not matched_items:
                            logger.debug(
                                f"该{resource_input_label}未匹配到 S{season} 的任何缺失剧集，"
                                "可能是季数不匹配或文件名无法识别"
                            )
                            continue

                        cloud_resource = self._is_cloud_resource_url(share_url)
                        direct_cloud_resource = (
                                cloud_resource
                                and self._is_direct_cloud_resource_url(share_url)
                        )
                        self._set_task_phase(
                            subscribe,
                            "登记网盘剧集整理"
                            if direct_cloud_resource else "转存匹配剧集",
                            92,
                        )
                        logger.debug(
                            f"准备批量整理：{mediainfo.title_year} S{season:02d}，"
                            f"{len(matched_items)} 个网盘内文件直接进入目标目录"
                            if direct_cloud_resource
                            else f"准备跨盘转存：{mediainfo.title_year} S{season:02d}，"
                                 f"{len(matched_items)} 个文件进入目标网盘后整理"
                            if cloud_resource
                            else f"准备批量转存：{mediainfo.title_year} S{season:02d}，"
                                 f"{len(matched_items)} 个文件到 {self._cloud_transfer_path}"
                        )
                        transfer_results = self._transfer_episode_items(
                            matched_items,
                            share_url,
                            mediainfo,
                            subscribe,
                            season,
                            sub_key,
                            track_subscription=not transient_target,
                            transient_target=transient_target,
                        )
                        if not transfer_results:
                            break
                        self._set_task_phase(subscribe, "登记文件后处理", 95)
                        batch_success_episodes = []
                        batch_detail_episodes = {}
                        completed_missing_episodes = set()

                        # 处理结果
                        for transfer_result in transfer_results:
                            item = transfer_result["item"]
                            file_id = transfer_result["file_id"]
                            episode = item["episode"]
                            file_name = item["file"]["name"]
                            current_score = item["score"]
                            is_upgrade = item["is_upgrade"]
                            success = transfer_result["success"]
                            pending_key = transfer_result["pending_key"]
                            item_share_url = item["file"].get("url") or share_url

                            history_item = self._build_transfer_history_item(
                                mediainfo=mediainfo,
                                subscribe=subscribe,
                                status=self._transfer_history_status(success, item_share_url),
                                share_url=item_share_url,
                                file_name=item["target_name"],
                                source_file_name=file_name,
                                cloud_dir=item["target_dir"],
                                resource=item["resource"],
                                season=season,
                                episode=episode,
                                file_size=int(item["file"].get("size") or 0),
                                source_sha1=item["file"].get("sha1") or "",
                                source_md5=item["file"].get("md5") or "",
                                rule_score=current_score,
                                upgrade=is_upgrade,
                            )
                            history.append(history_item)

                            if success:
                                transferred_count += 1
                                if pending_key:
                                    history_item["finalize_key"] = pending_key
                                    history_item["status"] = (
                                        "下载中" if self._is_ed2k_url(item_share_url)
                                        else "处理中"
                                    )
                                completed_missing_episodes.add(episode)

                                if not is_upgrade and not pending_key:
                                    success_episodes.append(episode)

                                score_info = f"(平台优先级:{current_score})"
                                upgrade_info = " [洗版升级]" if is_upgrade else ""
                                logger.debug(
                                    f"成功转存：{mediainfo.title} S{season:02d}E{episode:02d} {score_info}{upgrade_info}")

                                if not pending_key:
                                    notification_kind = (
                                        "upgrade"
                                        if is_upgrade
                                        else "cross_transfer"
                                        if history_item.get("transfer_mode") == "cross"
                                        else "transfer"
                                    )
                                    batch_detail_episodes.setdefault(
                                        notification_kind, []
                                    ).append(episode)

                                batch_success_episodes.append(episode)
                            else:
                                logger.error(f"转存失败：{mediainfo.title} S{season:02d}E{episode:02d}")

                        if completed_missing_episodes:
                            if discover_manual_episodes:
                                discovered_manual_episodes.update(
                                    completed_missing_episodes
                                )
                            missing_episodes = [
                                episode for episode in missing_episodes
                                if episode not in completed_missing_episodes
                            ]
                        for notification_kind, episodes in batch_detail_episodes.items():
                            self._append_tv_transfer_detail(
                                transfer_details, mediainfo, season, episodes,
                                notification_kind=notification_kind,
                            )

                        # 记录下载历史
                        if batch_success_episodes:
                            episodes_str = StringUtils.format_ep(batch_success_episodes)
                            self._record_download_history(
                                mediainfo=mediainfo,
                                subscribe=subscribe,
                                path=matched_items[0]["target_dir"],
                                download_hash=share_url,
                                torrent_name=resource_title,
                                share_url=share_url,
                                seasons=f"S{season:02d}",
                                episodes=episodes_str,
                            )

                        if (
                                self._stop_requested()
                                or not discover_manual_episodes and not missing_episodes
                        ):
                            break

                    except Exception as e:
                        logger.error(
                            f"处理分享链接出错：{self._resource_log_reference(share_url)}，"
                            f"错误：{str(e)}"
                        )
                        continue

                # 当前源处理完成
                if missing_episodes:
                    remaining_batches = resource_batches[source_index + 1:]
                    if remaining_batches:
                        next_source, _, next_is_cross = remaining_batches[0]
                        logger.debug(
                            f"{source.upper()} 处理完成，仍缺失 {len(missing_episodes)} 集，"
                            f"继续处理 {next_source.upper()} "
                            f"{'跨盘' if next_is_cross else '目标网盘'}候选"
                        )
                    else:
                        logger.debug(
                            f"{source.upper()} 处理完成，仍缺失 {len(missing_episodes)} 集，"
                            "候选资源已用尽"
                        )

            # 更新订阅状态
            # 将媒体路径已存在的集数和本次成功转存的集数合并
            self._set_task_phase(subscribe, "更新订阅进度", 96)
            all_success_episodes = list(set(success_episodes) | existing_episodes_in_resources)
            if all_success_episodes and not transient_target:
                remaining_lack = self._subscribe_handler.check_and_finish_subscribe(
                    subscribe=subscribe,
                    mediainfo=mediainfo,
                    success_episodes=all_success_episodes
                )
                if track_points and remaining_lack == 0 and hasattr(
                        self._search_handler, "clear_subscription_budgets"
                ):
                    self._search_handler.clear_subscription_budgets(sub_key)

        except Exception as e:
            logger.error(f"处理订阅 {subscribe.name} 出错：{str(e)}")
        return transferred_count
