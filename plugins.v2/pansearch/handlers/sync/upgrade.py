"""洗版基线、评分与自动升级。"""

import datetime
from typing import Any, Dict, List, Optional, Set

from app.db import SessionFactory
from app.log import logger
from app.schemas import MediaInfo
from app.schemas.types import MediaType
from app.utils.string import StringUtils

from ...core import OwnerDelegator


class UpgradeService(OwnerDelegator):
    def _set_upgrade_phase(
            self, subscribe, phase: str, progress: int
    ) -> None:
        self._set_task_phase(subscribe, phase, progress)

    def _process_tv_subscribe_upgrade(
            self,
            subscribe,
            history: List[dict],
            transfer_details: List[Dict[str, Any]],
            transferred_count: int,
            exclude_ids: Set[int],
            target_episodes: Optional[Set[int]] = None,
            manual_upgrade: bool = False,
            manual_resources: Optional[List[Dict[str, Any]]] = None,
    ) -> int:
        """
        洗版模式专用转存逻辑（独立于普通转存）

        流程：
        1. 合并整理历史、插件转存历史和 Emby 路径建立基线
        2. 搜索全部集数（含已存在的）
        3. 仅转存画质提升达到层级的集数
        4. 不调用 check_and_finish_subscribe，保持订阅活跃

        :param subscribe: 订阅对象
        :param history: 历史记录列表
        :param transfer_details: 转存详情列表
        :param transferred_count: 当前已转存数量
        :param exclude_ids: 排除的订阅ID集合
        :return: 更新后的转存数量
        """
        from app.db.subscribe_oper import SubscribeOper

        try:
            transient_target = bool(getattr(subscribe, "_transient_target", False))
            season = subscribe.season or 1
            sub_key = self.subscription_budget_key(subscribe, MediaType.TV)
            if self._search_handler:
                self._search_handler.reset_subscription_budgets(sub_key)
            logger.debug(f"开始洗版：{subscribe.name} S{season:02d}")

            mediainfo: MediaInfo = self._subscribe_mediainfo(
                subscribe, MediaType.TV
            )
            if not mediainfo:
                logger.warning(f"【洗版转存】无法识别媒体信息 {subscribe.name}")
                return transferred_count

            # 读取已有评分，并以整季整理历史、插件历史和 Emby 构建统一基线。
            self._set_upgrade_phase(subscribe, "建立洗版基线", 20)
            existing_ep_pri = self._read_ep_priority(subscribe)
            baseline = self._build_episode_baseline(
                subscribe, mediainfo, season, include_saved=True
            )
            cloud_valid, cloud_episode_files, cloud_dir = self._timed_sync_call(
                "cloud_scan",
                self._scan_cloud_resource_episode_files,
                subscribe=subscribe,
                mediainfo=mediainfo,
                season=season,
                start_episode=max(1, int(subscribe.start_episode or 1)),
                total_episode=max(0, int(subscribe.total_episode or 0)),
            )
            if not cloud_valid:
                logger.warning(
                    f"【洗版转存】{mediainfo.title} S{season:02d} "
                    "无法确认真实网盘文件，本轮跳过洗版"
                )
                return transferred_count

            verified_baseline = {}
            for episode, cloud_file in cloud_episode_files.items():
                baseline_item = dict(baseline.get(episode) or {})
                cloud_name = str(getattr(cloud_file, "name", "") or "")
                cloud_size = int(getattr(cloud_file, "size", 0) or 0)
                if not baseline_item:
                    score = self._get_mp_rule_score(
                        cloud_name, cloud_size, subscribe, season, mediainfo
                    )
                    baseline_item = {
                        "file_name": cloud_name,
                        "file_size": cloud_size,
                        "source": "真实网盘文件",
                        "score": score,
                        "rule_score": score,
                    }
                elif not int(baseline_item.get("file_size") or 0):
                    baseline_item["file_size"] = cloud_size
                    baseline_item["size_source"] = "真实网盘文件"
                baseline_item.update({
                    "cloud_dir": cloud_dir,
                    "target_file_name": cloud_name,
                    "cloud_file_id": str(getattr(cloud_file, "id", "") or ""),
                })
                verified_baseline[int(episode)] = baseline_item
            baseline = verified_baseline
            if target_episodes is not None:
                normalized_targets = {
                    int(episode) for episode in target_episodes if int(episode) > 0
                }
                baseline = {
                    episode: item for episode, item in baseline.items()
                    if episode in normalized_targets
                }
            local_scores = {
                episode: int(item.get("score") or 0)
                for episode, item in baseline.items()
            }
            upgrade_log_prefix = f"【洗版转存】{mediainfo.title} S{season:02d}"

            if not local_scores:
                if manual_upgrade:
                    logger.warning(
                        f"{upgrade_log_prefix} 未找到所选内容对应的真实网盘旧文件，"
                        "无法执行洗版"
                    )
                    return transferred_count
                logger.info(
                    f"{upgrade_log_prefix} 未找到可用的现有版本基线，"
                    "回退到普通转存逻辑"
                )
                return self.process_tv_subscribe(
                    subscribe=subscribe, history=history,
                    transfer_details=transfer_details,
                    transferred_count=transferred_count,
                    exclude_ids=exclude_ids,
                    allow_upgrade=False,
                    manual_resources=manual_resources,
                    manual_upgrade=False,
                    target_episodes=target_episodes,
                )

            logger.info(f"{upgrade_log_prefix} 已建立 {len(local_scores)} 集基线")

            # 构造待搜索的集数列表
            total_ep = subscribe.total_episode or 0
            start_ep = subscribe.start_episode or 1
            all_expected_episodes = set(range(start_ep, total_ep + 1)) if total_ep > 0 else set()

            # pri_order 没有固定满分；所有已有版本均通过候选比较决定是否升级。
            episodes_to_search = set(local_scores)

            if all_expected_episodes and target_episodes is None:
                missing_eps = sorted(all_expected_episodes - set(local_scores.keys()))
                if missing_eps:
                    logger.debug(f"{upgrade_log_prefix} 缺失 {len(missing_eps)} 集：{missing_eps}")
                    episodes_to_search |= set(missing_eps)

            episodes_to_search = sorted(episodes_to_search)

            # TMDB 播出日期过滤，并保留目标集播出日期供资源筛选。
            target_episode_air_dates: Dict[int, str] = {}
            if mediainfo.tmdb_id:
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
                    unreleased_episodes = {
                        int(episode)
                        for episode in (
                                calendar_entry.get("unreleased_episodes") or []
                        )
                    }
                    not_aired = [
                        episode
                        for episode in episodes_to_search
                        if episode in unreleased_episodes
                    ]
                    if not_aired:
                        not_aired_set = set(not_aired)
                        episodes_to_search = [
                            episode
                            for episode in episodes_to_search
                            if episode not in not_aired_set
                        ]
                        logger.debug(
                            f"{upgrade_log_prefix} 跳过 {len(not_aired)} 集未播出"
                        )
                        defer_until = self._calendar_date(
                            calendar_entry.get("next_air_date")
                        )
                        if (
                                not episodes_to_search
                                and defer_until
                                and defer_until > datetime.date.today()
                                and not manual_upgrade
                        ):
                            self.defer_subscribe_until(
                                subscribe,
                                defer_until,
                                f"洗版目标最早于 {defer_until.isoformat()} 播出",
                            )

            if not episodes_to_search:
                logger.info(f"{upgrade_log_prefix} 无可搜索的集数")
                return transferred_count

            logger.debug(f"{upgrade_log_prefix} 待搜索 {len(episodes_to_search)} 集：{episodes_to_search}")

            # 搜索并转存匹配资源
            enabled_sources = (
                ["manual"]
                if manual_resources
                else self._search_handler.get_enabled_sources()
            )
            if not enabled_sources:
                logger.warning(f"{upgrade_log_prefix} 没有可用的搜索源")
                return transferred_count

            self._set_upgrade_phase(
                subscribe,
                "处理手动洗版资源" if manual_resources else "搜索候选资源",
                40,
            )
            prefetched_results = (
                {"manual": [dict(resource) for resource in manual_resources]}
                if manual_resources else {}
            )
            if not manual_resources:
                prefetched_results = self._search_handler.search_sources(
                    sources=enabled_sources,
                    mediainfo=mediainfo,
                    media_type=MediaType.TV,
                    season=season,
                    target_episodes=episodes_to_search,
                    target_episode_air_dates=target_episode_air_dates,
                    subscribe=subscribe,
                )
            resource_batches = self._build_transfer_resource_batches(
                enabled_sources, prefetched_results
            )
            new_priority = dict(existing_ep_pri)
            upgrade_downloaded = 0
            upgrade_episodes = set()  # 记录已升级的集号，用于更新 note

            for source, candidate_resources, is_cross_batch in resource_batches:
                if self._stop_requested():
                    break
                if not episodes_to_search:
                    break
                logger.debug(
                    f"{upgrade_log_prefix} 使用 {source.upper()} 处理"
                    f"{'跨盘' if is_cross_batch else '目标网盘'}候选"
                )

                for resource_index, resource in enumerate(candidate_resources):
                    if self._stop_requested():
                        break
                    if not episodes_to_search:
                        break
                    share_url = resource.get("url", "")
                    resource_title = resource.get("title", "")
                    pending_episodes = tuple(episodes_to_search)

                    if (resource.get("need_unlock") or resource.get("need_access")) and not share_url:
                        resource_ref = resource.get("resource_ref")
                        if resource_ref:
                            preview_files = resource.get("preview_files") or []
                            preview_matches = self._match_episode_files(
                                preview_files,
                                mediainfo,
                                subscribe,
                                season,
                                pending_episodes,
                                require_media_match=is_cross_batch,
                            ) if preview_files else {}
                            preview_candidates = [
                                (episode, file_item, score)
                                for episode, (file_item, score) in preview_matches.items()
                                if file_item
                            ]
                            if preview_candidates:
                                preview_worthwhile = any(
                                    self._should_upgrade_candidate(
                                        local_scores.get(episode, 0),
                                        score,
                                        int(baseline.get(episode, {}).get("file_size") or 0),
                                        self._resource_size_bytes(file_item.get("size")),
                                        has_existing=episode in baseline,
                                    )[0]
                                    for episode, file_item, score in preview_candidates
                                )
                                if not preview_worthwhile:
                                    logger.info(
                                        f"{upgrade_log_prefix} 预览中的目标集均不满足"
                                        f"{self._upgrade_mode}洗版条件，解锁前跳过：{resource_title}"
                                    )
                                    continue
                    share_url = self._resolve_candidate_resource_url(
                        candidate_resources,
                        resource_index,
                        resource,
                        self._search_handler._search_label(
                            mediainfo, MediaType.TV, season
                        ),
                        log_prefix=upgrade_log_prefix,
                    )
                    if self._stop_requested():
                        break

                    if not share_url:
                        continue

                    if not self._is_supported_resource(resource, share_url):
                        logger.warning(
                            f"跳过当前同步链不支持的资源类型 "
                            f"{self._supported_resource_type(resource, share_url)}：{resource_title}"
                        )
                        continue

                    if self._is_offline_blacklisted(resource, share_url):
                        if self._is_offline_url(share_url) or self._is_magnet_url(share_url):
                            logger.info(
                                f"🚫 洗版离线任务命中黑名单（1天内失败或超时），跳过该资源：{resource_title}"
                            )
                        else:
                            logger.info(
                                f"🚫 洗版分享链接命中死链黑名单（1天内确定性转存失败），跳过该资源：{resource_title}"
                            )
                        continue

                    if self._is_magnet_url(share_url):
                        provider_name = self._prepare_magnet_resource(
                            resource, share_url
                        )
                        if not self._validate_resource_url(
                                share_url,
                                resource_label="Magnet 链接",
                                log_prefix=upgrade_log_prefix,
                        ):
                            continue
                        provider_name = provider_name or resource_title
                        title_seasons = self._magnet_title_seasons(resource)
                        if title_seasons and season not in title_seasons:
                            logger.debug(
                                f"{upgrade_log_prefix} Magnet 标题预过滤排除："
                                f"标题季数={','.join(f'S{value:02d}' for value in sorted(title_seasons))}，"
                                f"目标季数=S{season:02d}，标题={provider_name}"
                            )
                            continue
                        target_episodes = []
                        title_episodes = self._magnet_title_episodes(resource, season)
                        if title_episodes:
                            target_episodes = sorted(
                                set(episodes_to_search) & title_episodes
                            )
                            if not target_episodes:
                                logger.debug(
                                    f"{upgrade_log_prefix} Magnet 标题预过滤排除："
                                    f"标题集数={self._format_episode_ranges(title_episodes)}，"
                                    f"目标集数={self._format_episode_ranges(set(episodes_to_search))}，"
                                    f"标题={provider_name}"
                                )
                                continue
                        _, provider_score = self._search_handler.select_file_candidate(
                            [{"name": provider_name, "size": 0}],
                            mediainfo,
                            subscribe,
                        )
                        target_episodes = target_episodes or sorted(
                            set(episodes_to_search)
                            & self._resource_preview_episodes(resource, season)
                        ) or sorted(episodes_to_search)
                        target_episode_set = set(target_episodes)
                        upgrade_baseline = {}
                        worthwhile = False
                        for episode in target_episodes:
                            baseline_item = baseline.get(episode, {})
                            old_score = local_scores.get(episode, 0)
                            episode_worthwhile = False
                            if episode not in baseline:
                                episode_worthwhile = True
                            elif provider_score > old_score:
                                episode_worthwhile = True
                            elif provider_score == old_score and self._upgrade_mode in {"largest", "smallest"}:
                                # Magnet 元数据通常只有整包大小，逐集大小留到下载完成后判断。
                                episode_worthwhile = True
                            worthwhile = worthwhile or episode_worthwhile
                            if episode_worthwhile and episode in baseline:
                                upgrade_baseline[str(episode)] = {
                                    "score": old_score,
                                    "size": int(baseline_item.get("file_size") or 0),
                                    "cloud_dir": baseline_item.get("cloud_dir") or "",
                                    "file_name": baseline_item.get("target_file_name")
                                                 or baseline_item.get("file_name") or "",
                                    "file_id": baseline_item.get("cloud_file_id") or "",
                                }
                        if not worthwhile:
                            continue
                        pending_key = self._queue_magnet_package(
                            resource,
                            share_url,
                            subscribe,
                            mediainfo,
                            season=season,
                            target_episodes=target_episodes,
                            sub_key=sub_key,
                            upgrade=bool(upgrade_baseline),
                            upgrade_mode=self._upgrade_mode,
                            upgrade_baseline=upgrade_baseline,
                            transient_target=transient_target,
                        )
                        if not pending_key:
                            continue
                        self._append_magnet_pending_history(
                            history=history,
                            mediainfo=mediainfo,
                            subscribe=subscribe,
                            share_url=share_url,
                            cloud_dir=self._cloud_transfer_path.rstrip("/") or "/",
                            resource=resource,
                            season=season,
                            target_episodes=target_episodes,
                            upgrade=bool(upgrade_baseline),
                            finalize_key=pending_key,
                        )
                        episodes_to_search = [
                            episode for episode in episodes_to_search
                            if episode not in target_episode_set
                        ]
                        continue

                    share_files = self._validated_resource_files(
                        share_url,
                        resource_title=resource_title,
                        target_season=(season if self._skip_other_season_dirs else None),
                        log_prefix=upgrade_log_prefix,
                    )
                    if not share_files:
                        continue

                    # 匹配需要升级的集数
                    self._set_upgrade_phase(subscribe, "比较版本", 65)
                    matched_items = []
                    matched_files = self._match_episode_files(
                        share_files,
                        mediainfo,
                        subscribe,
                        season,
                        pending_episodes,
                        require_media_match=is_cross_batch,
                    )
                    for episode in pending_episodes:
                        matched_file, cand_pri = matched_files.get(
                            episode, (None, 0)
                        )
                        if not matched_file:
                            continue

                        file_name = matched_file.get('name', '')
                        # 候选文件大小（115 API 搜索已自带）
                        candidate_size = self._resource_size_bytes(matched_file.get("size"))

                        # 现有文件信息（MoviePilot 规则优先级）
                        old_score = local_scores.get(episode, 0)

                        new_score = cand_pri

                        score_gap = new_score - old_score

                        should_upgrade, reason = self._should_upgrade_candidate(
                            old_score,
                            new_score,
                            int(baseline.get(episode, {}).get("file_size") or 0),
                            candidate_size,
                            has_existing=episode in baseline,
                        )
                        if not should_upgrade:
                            logger.debug(
                                f"{upgrade_log_prefix} E{episode:02d} 洗版候选跳过：{reason}"
                            )
                            continue

                        logger.debug(
                            f"{upgrade_log_prefix} E{episode:02d}：{reason}"
                        )

                        target_dir, target_name = self._platform_target(
                            self._CLOUD_MEDIA_ROOT, subscribe, mediainfo,
                            file_name, season, episode
                        )
                        if episode in baseline and self._upgrade_mode == "coexist":
                            target_name = self._coexist_target_name(
                                target_name,
                                file_name,
                                candidate_size,
                                matched_file.get("sha1") or "",
                            )
                        baseline_item = baseline.get(episode, {})
                        old_dir = str(baseline_item.get("cloud_dir") or "").strip()
                        old_name = str(
                            baseline_item.get("target_file_name")
                            or baseline_item.get("file_name") or ""
                        ).strip()
                        old_file_id = str(baseline_item.get("cloud_file_id") or "")
                        if episode in baseline and self._upgrade_mode != "coexist" and not old_file_id:
                            if not old_dir or not old_name:
                                old_dir, old_name = self._platform_target(
                                    self._CLOUD_MEDIA_ROOT,
                                    subscribe,
                                    mediainfo,
                                    old_name or file_name,
                                    season,
                                    episode,
                                )
                            old_file = self._cloud_query.get_cached_file(old_dir, old_name)
                            if old_file:
                                old_file_id = str(getattr(old_file, "id", "") or "")
                                old_name = str(getattr(old_file, "name", "") or old_name)
                        matched_items.append({
                            "file": matched_file,
                            "resource": resource,
                            "episode": episode,
                            "new_score": new_score,
                            "old_score": old_score,
                            "score_gap": score_gap,
                            "file_name": file_name,
                            "candidate_size": candidate_size,
                            "target_dir": target_dir,
                            "target_name": target_name,
                            "is_upgrade": bool(episode in baseline),
                            "success_episodes": [],
                            "upgrade_old_cloud_dir": str(
                                old_dir
                            ),
                            "upgrade_old_file_name": str(
                                old_name
                            ),
                            "upgrade_old_file_id": str(
                                old_file_id
                            ),
                            "upgrade_old_size": int(
                                baseline.get(episode, {}).get("file_size") or 0
                            ),
                            "subtitle_files": self._companion_subtitle_files(
                                share_files,
                                matched_file,
                                season=season,
                                episode=episode,
                            ),
                        })

                    if not matched_items:
                        continue

                    self._set_upgrade_phase(subscribe, "提交替换", 80)
                    transfer_results = self._transfer_episode_items(
                        matched_items,
                        share_url,
                        mediainfo,
                        subscribe,
                        season,
                        sub_key,
                        track_subscription=not manual_upgrade and not transient_target,
                        transient_target=transient_target,
                    )
                    if not transfer_results:
                        break
                    batch_detail_episodes = []
                    batch_success_episodes = []
                    for transfer_result in transfer_results:
                        item = transfer_result["item"]
                        episode = item["episode"]
                        new_score = item["new_score"]
                        old_score = item["old_score"]
                        file_name = item["file_name"]
                        success = transfer_result["success"]
                        pending_key = transfer_result["pending_key"]

                        history_item = self._build_transfer_history_item(
                            mediainfo=mediainfo,
                            subscribe=subscribe,
                            status=self._transfer_history_status(success, share_url),
                            share_url=share_url,
                            file_name=item["target_name"],
                            source_file_name=file_name,
                            cloud_dir=item["target_dir"],
                            resource=item["resource"],
                            season=season,
                            episode=episode,
                            file_size=int(item.get("candidate_size") or 0),
                            source_sha1=item["file"].get("sha1") or "",
                            source_md5=item["file"].get("md5") or "",
                            rule_score=new_score,
                            upgrade=item.get("is_upgrade", False),
                        )
                        history.append(history_item)

                        if success:
                            transferred_count += 1
                            upgrade_downloaded += 1
                            upgrade_episodes.add(episode)
                            new_priority[str(episode)] = new_score

                            if pending_key:
                                history_item["finalize_key"] = pending_key
                                history_item["status"] = (
                                    "下载中" if self._is_ed2k_url(share_url)
                                    else "处理中"
                                )
                            if not pending_key:
                                batch_detail_episodes.append(episode)
                            batch_success_episodes.append(episode)

                            logger.debug(
                                f"{upgrade_log_prefix} 转存成功 E{episode:02d}"
                                f" {old_score}→{new_score}（{file_name}）"
                            )

                    if batch_success_episodes:
                        completed_episode_set = set(batch_success_episodes)
                        episodes_to_search = [
                            episode for episode in episodes_to_search
                            if episode not in completed_episode_set
                        ]
                    self._append_tv_transfer_detail(
                        transfer_details, mediainfo, season, batch_detail_episodes,
                        notification_kind="upgrade",
                    )
                    if batch_success_episodes:
                        self._record_download_history(
                            mediainfo=mediainfo,
                            subscribe=subscribe,
                            path=transfer_results[0]["item"]["target_dir"],
                            download_hash=share_url,
                            torrent_name=resource_title,
                            share_url=share_url,
                            seasons=f"S{season:02d}",
                            episodes=StringUtils.format_ep(batch_success_episodes),
                        )

            # 洗版转存的集数同样计入已入库进度。
            if upgrade_episodes and not manual_upgrade and not transient_target:
                remaining_lack = self._subscribe_handler.update_subscribe_progress(
                    subscribe=subscribe,
                    mediainfo=mediainfo,
                    success_episodes=sorted(upgrade_episodes),
                )
                if remaining_lack is not None:
                    logger.debug(f"{upgrade_log_prefix} 已更新订阅进度（{len(upgrade_episodes)} 集）")
                else:
                    logger.warning(f"{upgrade_log_prefix} 更新订阅进度失败")

            # 更新单集优先级
            if (
                    new_priority != existing_ep_pri
                    and not manual_upgrade
                    and not transient_target
            ):
                try:
                    SubscribeOper().update(subscribe.id, {"episode_priority": new_priority})
                    logger.debug(f"{upgrade_log_prefix} 已更新 {len(new_priority)} 集评分")
                except Exception as e:
                    logger.warning(f"{upgrade_log_prefix} 更新 episode_priority 失败：{e}")

            # 不调用 check_and_finish_subscribe——保持订阅活跃以持续搜索更优资源
            if upgrade_downloaded:
                self._set_upgrade_phase(subscribe, "洗版完成", 100)
                logger.info(f"{upgrade_log_prefix} 洗版转存完成，共升级 {upgrade_downloaded} 集")
            else:
                self._set_upgrade_phase(subscribe, "无需替换", 100)
                logger.info(f"{upgrade_log_prefix} 洗版转存完成，未发现可升级资源")

        except Exception as e:
            logger.error(f"洗版失败：{subscribe.name}，{e}")
        return transferred_count

    def auto_upgrade_scan(self):
        """网盘转存后刷新已开启洗版订阅的整季评分。"""
        from app.db.subscribe_oper import SubscribeOper

        with SessionFactory() as db:
            all_subs = SubscribeOper(db=db).list() or []

        targets = [
            subscribe for subscribe in all_subs
            if subscribe.type == MediaType.TV.value
               and self._is_cloud_upgrade_subscribe(subscribe)
        ]
        if not targets:
            logger.debug("网盘洗版没有待刷新订阅")
            return

        updated = 0
        failed = 0
        for subscribe in targets:
            try:
                updated += self._upgrade_scan_single_sub(subscribe)
            except Exception as error:
                failed += 1
                logger.error(
                    f"网盘洗版评分刷新失败：{subscribe.name} "
                    f"S{subscribe.season or 1:02d}，{error}"
                )
        logger.info(
            f"网盘洗版评分刷新完成：{len(targets) - failed}/"
            f"{len(targets)} 个订阅，更新 {updated} 集"
        )

    def _upgrade_scan_single_sub(self, subscribe):
        """按统一基线刷新单个订阅的 episode_priority。"""
        from app.db.subscribe_oper import SubscribeOper

        season = int(subscribe.season or 1)
        mediainfo = self._subscribe_mediainfo(subscribe, MediaType.TV)
        if not mediainfo:
            logger.warning(f"洗版评分刷新失败：无法识别 {subscribe.name}")
            return 0

        baseline = self._build_episode_baseline(
            subscribe, mediainfo, season, include_saved=False
        )
        scores = {
            str(episode): int(item.get("score") or 0)
            for episode, item in baseline.items()
            if int(item.get("score") or 0) > 0
        }
        if not scores:
            logger.debug(f"洗版评分无可用基线：{subscribe.name} S{season:02d}")
            return 0
        if scores != self._read_ep_priority(subscribe):
            SubscribeOper().update(subscribe.id, {"episode_priority": scores})
            return len(scores)
        return 0

    def _self_heal_cleanup(self):
        """按真实整理/转存/媒体库基线刷新评分，避免依赖 STRM 文件名误删。"""
        from app.db.subscribe_oper import SubscribeOper

        try:
            with SessionFactory() as db:
                subscribes = SubscribeOper(db=db).list() or []
                subscribes = [
                    item for item in subscribes
                    if getattr(item, "episode_priority", None)
                ]
        except Exception as error:
            logger.warning(f"洗版评分自愈查询失败：{error}")
            return

        updated_count = 0
        cleaned_count = 0
        for subscribe in subscribes:
            try:
                season = int(subscribe.season or 1)
                mediainfo = self._subscribe_mediainfo(
                    subscribe, MediaType.TV
                )
                if not mediainfo:
                    continue
                baseline = self._build_episode_baseline(
                    subscribe, mediainfo, season, include_saved=False
                )
                if not baseline:
                    continue
                scores = {
                    str(episode): int(item.get("score") or 0)
                    for episode, item in baseline.items()
                    if int(item.get("score") or 0) > 0
                }
                old_scores = self._read_ep_priority(subscribe)
                if scores and scores != old_scores:
                    cleaned_count += len(set(old_scores) - set(scores))
                    SubscribeOper().update(
                        subscribe.id, {"episode_priority": scores}
                    )
                    updated_count += len(scores)
            except Exception as error:
                logger.warning(
                    f"洗版评分自愈失败：{subscribe.name} "
                    f"S{subscribe.season or 1:02d}，{error}"
                )

        if updated_count or cleaned_count:
            logger.info(
                f"洗版评分自愈完成：更新 {updated_count} 集，"
                f"清理 {cleaned_count} 条旧评分"
            )
