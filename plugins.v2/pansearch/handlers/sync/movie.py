"""电影订阅搜索、匹配与转存流程。"""

import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.log import logger
from app.schemas import MediaInfo
from app.schemas.types import MediaType

from ...core import OwnerDelegator
from ...core.media import tmdb_id_of
from ...drive.common import format_size


class MovieSyncProcessor(OwnerDelegator):
    """处理电影订阅同步。"""

    def process_movie_subscribe(
            self,
            subscribe,
            history: List[dict],
            transfer_details: List[Dict[str, Any]],
            transferred_count: int,
            manual_resources: Optional[List[Dict[str, Any]]] = None,
            manual_upgrade: bool = False,
            transient_target: bool = False,
    ) -> int:
        """
        处理单个电影订阅

        :param subscribe: 订阅对象
        :param history: 历史记录列表
        :param transfer_details: 转存详情列表
        :param transferred_count: 当前已转存数量
        :return: 更新后的转存数量
        """
        track_points = not manual_resources
        try:
            if self._stop_requested():
                return transferred_count
            logger.debug(f"处理电影订阅：{subscribe.name} ({subscribe.year})")

            # 加载该订阅的历史积分花费（用 tmdb_id 作为唯一标识）
            sub_key = self.subscription_budget_key(subscribe, MediaType.MOVIE)
            if track_points and self._search_handler:
                self._search_handler.reset_subscription_budgets(sub_key)

            # 检查历史记录是否已成功转存
            movie_history_score = -1  # -1 表示未转存过
            movie_history_size = 0
            subscribe_tmdb_id = str(tmdb_id_of(subscribe) or "")
            subscribe_year = str(getattr(subscribe, "year", None) or "").strip()
            for h in history:
                if h.get("type") != "电影" or h.get("status") != "成功":
                    continue
                history_tmdb_id = str(h.get("tmdb_id") or "").strip()
                if subscribe_tmdb_id and history_tmdb_id:
                    same_movie = subscribe_tmdb_id == history_tmdb_id
                else:
                    history_year = str(h.get("year") or "").strip()
                    same_movie = (
                            h.get("title") == subscribe.name
                            and bool(subscribe_year and history_year)
                            and subscribe_year == history_year
                    )
                if not same_movie:
                    continue
                score = int(h.get("rule_score") or 0)
                if score > movie_history_score:
                    movie_history_score = score
                    movie_history_size = self._resource_size_bytes(
                        h.get("file_size") or h.get("size")
                    )

            # 原生 best_version 决定是否为洗版订阅，插件范围进一步限制处理对象。
            is_best_version = manual_upgrade or self._is_cloud_upgrade_subscribe(subscribe)

            mediainfo: MediaInfo = self._subscribe_mediainfo(
                subscribe, MediaType.MOVIE
            )
            if not mediainfo:
                logger.warning(f"无法识别媒体信息：{subscribe.name}")
                return transferred_count

            # 洗版电影需要先建立现有版本基线，否则同名文件只能被当作普通订阅完成。
            existing_movie = None
            upgrade_target_exists = False
            if is_best_version:
                emby_has_size = False
                manual_movie = (
                        (getattr(subscribe, "_manual_media_baseline", {}) or {}).get("movie")
                        or {}
                )
                if manual_movie:
                    manual_name = str(manual_movie.get("file_name") or "").strip()
                    manual_size = self._resource_size_bytes(manual_movie.get("file_size"))
                    manual_score = self._get_mp_rule_score(
                        manual_name, manual_size, subscribe, 0, mediainfo
                    )
                    movie_history_score = max(movie_history_score, manual_score)
                    if manual_size:
                        movie_history_size = manual_size
                    logger.info(
                        f"电影 {subscribe.name} 洗版基线采用所选媒体库内容："
                        f"{manual_name}，评分 {manual_score}，{format_size(manual_size)}"
                    )
                for media_item in self._emby_media_resolver.movie_media(
                        chain=self._chain, mediainfo=mediainfo
                ):
                    media_file = Path(str(media_item.get("path") or ""))
                    media_size = self._resource_size_bytes(media_item.get("size"))
                    rule_title = str(
                        media_item.get("rule_title") or media_file.name
                    ).strip()
                    emby_has_size = emby_has_size or media_size > 0
                    emby_score = self._get_mp_rule_score(
                        rule_title, media_size, subscribe, 0, mediainfo
                    )
                    if media_size:
                        movie_history_size = media_size
                    if emby_score > movie_history_score:
                        movie_history_score = emby_score
                    if media_size or emby_score > 0:
                        logger.info(
                            f"电影 {subscribe.name} 洗版基线采用 Emby 媒体："
                            f"{media_file.name}，媒体详情 {rule_title}，"
                            f"评分 {emby_score}，{format_size(media_size)}"
                        )
                existing_movie = self._timed_sync_call(
                    "cloud_scan",
                    self._find_cloud_movie_file,
                    subscribe,
                    mediainfo,
                )
                if existing_movie:
                    upgrade_target_exists = True
                    existing_dir, existing_name, existing_file = existing_movie
                    if self._strm_generate_enabled:
                        existing_strm = self._generate_strm(
                            existing_dir,
                            existing_name,
                            target_file=existing_file,
                        )
                        if not existing_strm:
                            logger.warning(
                                f"电影 {subscribe.name} 真实网盘文件已存在，"
                                "但 STRM 修复尚未完成"
                            )
                    if not emby_has_size:
                        file_name, target_file = existing_name, existing_file
                        existing_size = int(getattr(target_file, "size", 0) or 0)
                        existing_score = self._get_mp_rule_score(
                            file_name, existing_size, subscribe, 0, mediainfo
                        )
                        if existing_score >= movie_history_score:
                            movie_history_score = existing_score
                            movie_history_size = existing_size
                        logger.info(
                            f"电影 {subscribe.name} Emby 未提供有效大小，"
                            f"网盘回退基线：{movie_history_score} "
                            f"（{file_name}，{format_size(existing_size)}）"
                        )
                    logger.info(
                        f"电影 {subscribe.name} 洗版中，"
                        f"现有平台优先级 {movie_history_score}"
                    )
                else:
                    movie_history_score = -1
                    movie_history_size = 0
                    logger.info(
                        f"电影 {subscribe.name} 未找到真实网盘旧文件，"
                        "本轮无法建立洗版基线"
                    )
                    if manual_upgrade:
                        return transferred_count

            if not manual_resources:
                release_date = self._calendar_date(mediainfo.release_date)
                if release_date and release_date > datetime.date.today():
                    self.defer_subscribe_until(
                        subscribe,
                        release_date,
                        f"电影上映日期为 {release_date.isoformat()}",
                    )
                    logger.debug(
                        f"电影 {mediainfo.title_year} 尚未上映，"
                        f"延期至 {release_date.isoformat()} 后检查"
                    )
                    return transferred_count
            self._set_task_phase(subscribe, "检查网盘内容", 25)

            if not is_best_version:
                existing_movie = self._timed_sync_call(
                    "cloud_scan",
                    self._find_cloud_movie_file,
                    subscribe,
                    mediainfo,
                )
                if existing_movie:
                    cloud_dir, file_name, _ = existing_movie
                    logger.info(
                        f"目标电影已存在，结束订阅：{cloud_dir.rstrip('/')}/{file_name}"
                    )
                    self._generate_strm(cloud_dir, file_name)
                    self._scrape_metadata(cloud_dir, file_name, mediainfo)
                    if not transient_target:
                        self._subscribe_handler.check_and_finish_subscribe(
                            subscribe=subscribe,
                            mediainfo=mediainfo,
                            success_episodes=[1],
                        )
                    if track_points and self._search_handler:
                        self._search_handler.clear_subscription_budgets(sub_key)
                    return transferred_count

            # 手动资源直接进入现有匹配转存链，否则查询搜索源。
            self._set_task_phase(
                subscribe,
                "处理手动网盘资源" if manual_resources else "搜索候选资源",
                45,
            )
            if manual_resources:
                source_order = ["manual"]
                source_results = {
                    "manual": [dict(resource) for resource in manual_resources]
                }
                logger.info(
                    f"手动处理电影 {mediainfo.title}："
                    f"收到 {len(source_results['manual'])} 个资源链接"
                )
            else:
                source_order = self._search_handler.get_enabled_sources()
                source_results = self._search_handler.search_sources(
                    sources=source_order,
                    mediainfo=mediainfo,
                    media_type=MediaType.MOVIE,
                    subscribe=subscribe,
                )
            resource_batches = self._build_transfer_resource_batches(
                source_order, source_results
            )
            candidate_resources = [
                resource
                for _, resources, _ in resource_batches
                for resource in resources
            ]

            if not candidate_resources:
                if self._stop_requested():
                    return transferred_count
                logger.info(f"未找到电影 {mediainfo.title} 的可处理资源")
                return transferred_count

            self._set_task_phase(subscribe, "筛选候选资源", 60)
            search_label = self._search_handler._search_label(
                mediainfo, MediaType.MOVIE
            )
            result_sources = "/".join(dict.fromkeys(
                str(resource.get("source") or "unknown").upper()
                for resource in candidate_resources
            ))
            logger.debug(
                f"[{search_label}][{result_sources}] 找到候选资源："
                f"{self._format_resource_summary(candidate_resources)}"
            )

            # 遍历搜索结果，尝试找到并转存电影
            movie_transferred = False
            for resource_index, resource in enumerate(candidate_resources):
                if movie_transferred or self._stop_requested():
                    break
                self._set_task_phase(
                    subscribe,
                    f"检查候选资源 {resource_index + 1}/{len(candidate_resources)}",
                    60 + int((resource_index + 1) / len(candidate_resources) * 22),
                )

                share_url = resource.get("url", "")
                resource_title = resource.get("title", "")

                # 检查是否是刚搜索出尚未真正解锁的延期解锁资源
                if (resource.get("need_unlock") or resource.get("need_access")) and not share_url:
                    resource_ref = resource.get("resource_ref")
                    if resource_ref and upgrade_target_exists and movie_history_score >= 0:
                        preview_name = str(
                            getattr(self._search_handler, "_resource_filter_title", lambda value: "")(
                                resource
                            ) or resource_title
                        ).strip()
                        preview_matched, preview_score = self._search_handler.select_file_candidate(
                            [{
                                "name": preview_name,
                                "size": self._resource_size_bytes(resource.get("size")),
                            }],
                            mediainfo,
                            subscribe,
                        )
                        preview_size = self._resource_size_bytes(resource.get("size"))
                        preview_decision = self._should_upgrade_candidate(
                            movie_history_score,
                            preview_score,
                            movie_history_size,
                            preview_size,
                        )
                        if preview_matched and preview_size and not preview_decision[0]:
                            logger.info(
                                f"电影 {mediainfo.title} 候选解锁前跳过："
                                f"{preview_decision[1]}"
                            )
                            continue
                share_url = self._resolve_candidate_resource_url(
                    candidate_resources,
                    resource_index,
                    resource,
                    search_label,
                    log_prefix=f"[{search_label}][HDHIVE]",
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

                cloud_resource = self._is_cloud_resource_url(share_url)
                direct_cloud_resource = (
                        cloud_resource and self._is_direct_cloud_resource_url(share_url)
                )
                logger.info(
                    f"检查{self._resource_input_label(share_url)}：{resource_title} - "
                    f"{self._resource_log_reference(share_url)}"
                )

                try:
                    if self._is_offline_url(share_url) or self._is_magnet_url(share_url):
                        if self._is_offline_blacklisted(resource, share_url):
                            logger.info(
                                f"🚫 离线任务命中黑名单（1天内失败或超时），跳过该资源并选择其他候选：{resource_title}"
                            )
                            continue

                    if self._is_magnet_url(share_url):
                        provider_name = self._prepare_magnet_resource(
                            resource, share_url
                        )
                        if not self._validate_resource_url(
                                share_url, resource_label="Magnet 链接"
                        ):
                            continue
                        provider_name = provider_name or resource_title
                        matched, current_score = self._search_handler.select_file_candidate(
                            [{"name": provider_name, "size": resource.get("size") or 0}],
                            mediainfo,
                            subscribe,
                        )
                        if not matched:
                            logger.debug(f"Magnet 元数据未通过平台优先级规则：{provider_name}")
                            continue
                        magnet_size = self._resource_size_bytes(
                            resource.get("size")
                            or (resource.get("magnet_metadata") or {}).get("size")
                        )
                        magnet_upgrade = False
                        if upgrade_target_exists and movie_history_score >= 0:
                            magnet_upgrade, magnet_reason = self._should_upgrade_candidate(
                                movie_history_score,
                                current_score,
                                movie_history_size,
                                magnet_size,
                            )
                            if not magnet_upgrade:
                                logger.info(
                                    f"电影 {mediainfo.title} Magnet 洗版候选跳过：{magnet_reason}"
                                )
                                continue
                        self._set_task_phase(subscribe, "提交离线下载", 90)
                        pending_key = self._queue_magnet_package(
                            resource, share_url, subscribe, mediainfo,
                            sub_key=sub_key if track_points else "",
                            upgrade=magnet_upgrade,
                            upgrade_mode=self._upgrade_mode,
                            upgrade_baseline={
                                "movie": {
                                    "score": movie_history_score,
                                    "size": movie_history_size,
                                }
                            } if magnet_upgrade else {},
                            transient_target=transient_target,
                        )
                        if not pending_key:
                            continue
                        self._append_magnet_pending_history(
                            history=history,
                            mediainfo=mediainfo,
                            subscribe=subscribe,
                            share_url=share_url,
                            cloud_dir=self._cloud_transfer_path.rstrip('/') or "/",
                            resource=resource,
                            rule_score=current_score,
                            upgrade=magnet_upgrade,
                            finalize_key=pending_key,
                        )
                        movie_transferred = True
                        logger.info(f"Magnet 已进入下载后真实文件匹配：{provider_name}")
                        continue

                    share_files = self._validated_resource_files(
                        share_url,
                        resource_title=resource_title,
                    )
                    if not share_files:
                        continue

                    require_media_match = self._is_cross_drive_resource(
                        resource, share_url
                    )
                    matched_file, current_score = self._match_movie_file(
                        share_files,
                        mediainfo,
                        subscribe,
                        resource_title,
                        require_media_match=require_media_match,
                    )

                    if not matched_file:
                        if require_media_match:
                            logger.debug(
                                f"跨盘分享内容未被平台识别为目标电影："
                                f"{mediainfo.title_year}，已跳过该资源"
                            )
                        else:
                            logger.debug(
                                f"目标网盘分享未找到符合规则的电影文件："
                                f"{mediainfo.title_year}，已跳过该资源"
                            )
                        continue

                    if matched_file:
                        file_name = matched_file.get('name', '')
                        logger.debug(f"找到匹配文件：{file_name}")

                        is_upgrade = False
                        # 洗版模式下检查是否需要升级资源
                        upgrade_old_size = movie_history_size
                        if upgrade_target_exists and movie_history_score >= 0:
                            candidate_size = self._resource_size_bytes(
                                matched_file.get("size")
                            )
                            should_upgrade, reason = self._should_upgrade_candidate(
                                movie_history_score,
                                current_score,
                                movie_history_size,
                                candidate_size,
                            )
                            if not should_upgrade:
                                logger.info(
                                    f"电影 {mediainfo.title} 洗版候选跳过：{reason}"
                                )
                                continue
                            is_upgrade = True
                            logger.info(
                                f"电影 {mediainfo.title} 洗版：{reason}"
                            )

                        save_dir, target_name = self._platform_target(
                            self._CLOUD_MEDIA_ROOT, subscribe, mediainfo, file_name
                        )
                        if is_upgrade and self._upgrade_mode == "coexist":
                            target_name = self._coexist_target_name(
                                target_name,
                                file_name,
                                self._resource_size_bytes(matched_file.get("size")),
                                matched_file.get("sha1") or "",
                            )
                        staging_dir = self._resource_staging_dir(
                            share_url, matched_file
                        )
                        logger.info(
                            f"网盘源文件: {staging_dir}/{file_name}，"
                            f"整理到: {save_dir}/{target_name}"
                            if direct_cloud_resource
                            else f"跨盘源文件: {self._cloud_resource_path(share_url)}/{file_name}，"
                                 f"转存后整理到: {save_dir}/{target_name}"
                            if cloud_resource
                            else f"网盘转存暂存: {staging_dir}/{file_name}，"
                                 f"完成后移动到: {save_dir}/{target_name}"
                        )

                        if self._stop_requested():
                            break
                        self._set_task_phase(
                            subscribe,
                            "登记网盘文件整理" if direct_cloud_resource else "转存匹配文件",
                            90,
                        )
                        success = True
                        if not direct_cloud_resource:
                            success = self._timed_sync_call(
                                "share_transfer",
                                self._transfer_file,
                                share_url,
                                matched_file,
                                self._cloud_transfer_path,
                                None if self._is_offline_url(share_url) else target_name,
                                matched_file.get("sha1"),
                                media_type="movie",
                            )
                        if not success:
                            if self._stop_requested():
                                logger.info(
                                    f"用户已停止转存：{mediainfo.title}"
                                )
                                break

                        subtitles = []
                        if success and not self._is_offline_url(share_url):
                            subtitles = self._transfer_companion_subtitles(
                                share_url=share_url,
                                files=share_files,
                                video_file=matched_file,
                                target_video_name=target_name,
                                media_type="movie",
                            )

                        # 记录历史
                        history_item = self._build_transfer_history_item(
                            mediainfo=mediainfo,
                            subscribe=subscribe,
                            status=self._transfer_history_status(success, share_url),
                            share_url=share_url,
                            file_name=target_name,
                            source_file_name=file_name,
                            cloud_dir=save_dir,
                            resource=resource,
                            file_size=self._resource_size_bytes(matched_file.get("size")),
                            source_sha1=matched_file.get("sha1") or "",
                            source_md5=matched_file.get("md5") or "",
                            rule_score=current_score,
                            upgrade=is_upgrade,
                        )
                        history.append(history_item)

                        if success:
                            transferred_count += 1
                            movie_transferred = True
                            movie_history_score = current_score
                            self._set_task_phase(subscribe, "登记文件后处理", 95)
                            strm_path, pending_key = self._generate_or_queue_strm(
                                share_url,
                                save_dir,
                                target_name,
                                mediainfo,
                                source_sha1=matched_file.get("sha1"),
                                file_size=self._resource_size_bytes(matched_file.get("size")),
                                subscribe_id=(
                                    None if transient_target else getattr(subscribe, "id", None)
                                ),
                                success_episodes=[] if transient_target else [1],
                                sub_key=sub_key,
                                transient_target=transient_target,
                                target_subscribe=(
                                    self._serialize_pending_target_subscribe(subscribe)
                                    if transient_target else None
                                ),
                                skip_history=bool(resource.get("skip_history")),
                                staging_dir=staging_dir,
                                staging_name=(
                                        matched_file.get("staging_name") or file_name
                                ),
                                upgrade=is_upgrade,
                                upgrade_mode=self._upgrade_mode,
                                upgrade_old_cloud_dir=(
                                    existing_movie[0] if existing_movie else ""
                                ),
                                upgrade_old_file_name=(
                                    existing_movie[1] if existing_movie else ""
                                ),
                                upgrade_old_file_id=(
                                    getattr(existing_movie[2], "id", "")
                                    if existing_movie else ""
                                ),
                                upgrade_old_size=upgrade_old_size,
                                subtitles=subtitles,
                            )
                            if not strm_path and not pending_key:
                                history_item["status"] = "失败"
                                history_item["failure_reason"] = (
                                    "文件已转存但后处理任务登记失败"
                                )
                                transferred_count = max(0, transferred_count - 1)
                                movie_transferred = False
                                movie_history_score = 0
                                logger.error(
                                    f"文件已转存但后处理任务登记失败：{target_name}"
                                )
                                continue
                            if pending_key:
                                history_item["finalize_key"] = pending_key
                                history_item["status"] = (
                                    "下载中" if self._is_offline_url(share_url)
                                    else "处理中"
                                )
                            if strm_path:
                                self._media_server_notifier.notify(
                                    path=strm_path,
                                    mediainfo=mediainfo,
                                    file_name=target_name,
                                )
                            logger.info(
                                f"{'已登记网盘电影整理' if direct_cloud_resource else '成功转存电影'}："
                                f"{mediainfo.title} (平台优先级:{current_score})"
                            )

                            # 收集转存详情用于通知
                            if not pending_key:
                                transfer_details.append({
                                    "type": "电影",
                                    "title": mediainfo.title,
                                    "year": mediainfo.year,
                                    "image": mediainfo.get_poster_image(),
                                    "file_name": target_name,
                                    "notification_kind": (
                                        "upgrade"
                                        if is_upgrade
                                        else "cross_transfer"
                                        if history_item.get("transfer_mode") == "cross"
                                        else "transfer"
                                    ),
                                })

                            self._record_download_history(
                                mediainfo=mediainfo,
                                subscribe=subscribe,
                                path=save_dir,
                                download_hash=matched_file.get("id"),
                                torrent_name=resource_title,
                                share_url=share_url,
                                torrent_description=file_name,
                            )

                            if not pending_key and not transient_target:
                                self._subscribe_handler.check_and_finish_subscribe(
                                    subscribe=subscribe,
                                    mediainfo=mediainfo,
                                    success_episodes=[1],
                                )
                                if track_points and self._search_handler:
                                    self._search_handler.clear_subscription_budgets(sub_key)
                        else:
                            logger.error(f"转存失败：{mediainfo.title}")

                except Exception as e:
                    logger.error(
                        f"处理分享链接出错：{self._resource_log_reference(share_url)}，"
                        f"错误：{str(e)}"
                    )
                    continue

        except Exception as e:
            logger.error(f"处理电影订阅 {subscribe.name} 出错：{str(e)}")
        return transferred_count
