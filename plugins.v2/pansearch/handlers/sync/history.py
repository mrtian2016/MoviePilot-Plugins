"""转存历史记录生命周期与重试。"""

import copy
import hashlib
import re
import shutil
import time
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import urljoin, urlsplit, urlunsplit

from app.chain.mediaserver import MediaServerChain
from app.core.context import MediaInfo
from app.core.metainfo import MetaInfo
from app.db import SessionFactory
from app.db.downloadhistory_oper import DownloadHistoryOper
from app.db.models.downloadhistory import DownloadHistory
from app.db.models.mediaserver import MediaServerItem
from app.db.subscribe_oper import SubscribeOper
from app.helper.mediaserver import MediaServerHelper
from app.log import logger
from app.schemas.types import MediaType
from sqlalchemy import func, or_

from ...core import CloudDriveCapability, CloudFile, OwnerDelegator
from ...core.history import history_group_key
from ...core.media import (
    download_history_identity_payload,
    get_download_history_last_by,
    list_subscribes_by_tmdb_id,
    media_identity,
    media_server_tmdb_filters,
    recognize_media,
    tmdb_id_of,
)
from ...search.types import normalize_resource_type, resource_type_from_url


class HistoryService(OwnerDelegator):
    """管理转存历史记录、关联文件与重试流程。"""

    _METADATA_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}
    _SEASON_DIRECTORY_PATTERN = re.compile(r"^season[ ._-]*\d+$", re.IGNORECASE)
    _PLATFORM_HISTORY_STORAGE = "pansearch"
    # 存量核对自动回填：每轮最多核对条数与同键复核间隔（内存缓存）。
    _OFFLINE_BACKFILL_STATUSES = {"失败", "处理中"}
    _OFFLINE_BACKFILL_RECHECK_SECONDS = 3600
    _OFFLINE_BACKFILL_CACHE_MAXSIZE = 500

    def _build_transfer_history_item(
            self,
            mediainfo: MediaInfo,
            subscribe,
            status: str,
            share_url: str,
            file_name: str,
            source_file_name: str,
            cloud_dir: str,
            resource: Optional[Dict[str, Any]] = None,
            **fields: Any,
    ) -> Dict[str, Any]:
        """集中生成插件转存历史，确保平台整理历史所需字段完整。"""
        media_data = self._serialize_mediainfo(mediainfo)
        media_source, media_id = media_identity(mediainfo)
        effective_media = self._effective_mediainfo(subscribe, mediainfo)
        cloud_drive_name = str(
            getattr(self._cloud_drive, "name", "网盘") or "网盘"
        ).strip()
        item = {
            "title": effective_media.title,
            "year": effective_media.year,
            "tmdb_id": mediainfo.tmdb_id,
            "imdb_id": mediainfo.imdb_id,
            "tvdb_id": mediainfo.tvdb_id,
            "douban_id": mediainfo.douban_id,
            "bangumi_id": getattr(mediainfo, "bangumi_id", None),
            "anilist_id": getattr(mediainfo, "anilist_id", None),
            "media_source": media_source,
            "media_id": media_id or media_data.get("media_id"),
            "subscribe_id": (
                getattr(subscribe, "id", None) if subscribe is not None else None
            ),
            "category": getattr(mediainfo, "category", None),
            "episode_group": getattr(mediainfo, "episode_group", None),
            "image": mediainfo.get_poster_image(),
            "type": mediainfo.type.value,
            "status": status,
            "share_url": share_url,
            "file_name": file_name,
            "source_file_name": source_file_name,
            "cloud_dir": cloud_dir,
            "cloud_drive_name": cloud_drive_name,
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            **fields,
        }
        item.update(self._resource_history_meta(resource or {}, share_url))
        source_provider = self._resource_provider_for_url(share_url)
        if (
                source_provider and self._cloud_drive
                and source_provider.key != self._cloud_drive.key
        ):
            source_file = CloudFile(
                id="",
                name=str(source_file_name or file_name or ""),
                is_directory=False,
                size=int(fields.get("file_size") or 0),
                sha1=str(fields.get("source_sha1") or ""),
                md5=str(fields.get("source_md5") or ""),
            )
            item.update({
                "transfer_mode": "cross",
                "source_drive_key": source_provider.key,
                "source_drive_name": source_provider.name,
                "target_drive_key": self._cloud_drive.key,
                "target_drive_name": self._cloud_drive.name,
            })
            if self._cross_transfer_manager:
                item.update(self._cross_transfer_manager.cache_info(
                    source_provider.key,
                    source_file,
                    verify_checksum=False,
                ))
        item["task_types"] = self._history_task_types(item)
        return item

    def _record_download_history(
            self,
            mediainfo: MediaInfo,
            subscribe,
            path: str,
            download_hash: Any,
            torrent_name: str,
            share_url: str,
            torrent_description: str = "",
            seasons: str = "",
            episodes: str = "",
    ) -> bool:
        """通过下载历史能力登记一次网盘转存。"""
        effective_media = self._effective_mediainfo(subscribe, mediainfo)
        provider_name = str(
            getattr(self._cloud_drive, "name", "网盘") or "网盘"
        )
        payload = {
            "path": path,
            "type": mediainfo.type.value,
            "title": effective_media.title,
            "year": effective_media.year,
            "image": mediainfo.get_poster_image(),
            "downloader": provider_name,
            "download_hash": download_hash,
            "torrent_name": torrent_name,
            "torrent_site": provider_name,
            "username": "PanSearch",
            "date": time.strftime("%Y-%m-%d %H:%M:%S"),
            "note": {
                "source": f"Subscribe|{getattr(subscribe, 'name', '')}",
                "share_url": share_url,
            },
        }
        payload.update(download_history_identity_payload(
            mediainfo, DownloadHistory
        ))
        if torrent_description:
            payload["torrent_description"] = torrent_description
        if seasons:
            payload["seasons"] = seasons
        if episodes:
            payload["episodes"] = episodes
        try:
            DownloadHistoryOper().add(**payload)
            logger.debug(f"已记录 {mediainfo.title_year} 下载历史")
            return True
        except Exception as error:
            logger.warning(f"记录下载历史失败：{error}")
            return False

    @staticmethod
    def _append_tv_transfer_detail(
            transfer_details: List[Dict[str, Any]],
            mediainfo: MediaInfo,
            season: int,
            episodes: List[int],
            notification_kind: str = "transfer",
    ) -> None:
        """按媒体季批量合并通知详情，避免逐集重复扫描列表。"""
        normalized = sorted({
            int(episode) for episode in episodes if int(episode) > 0
        })
        if not normalized:
            return
        detail = next(
            (
                item for item in transfer_details
                if item.get("title") == mediainfo.title
                   and int(item.get("season") or 0) == int(season)
                   and item.get("notification_kind", "transfer") == notification_kind
            ),
            None,
        )
        if detail:
            detail["episodes"] = sorted(
                set(detail.get("episodes") or []) | set(normalized)
            )
            return
        transfer_details.append({
            "type": MediaType.TV.value,
            "title": mediainfo.title,
            "year": mediainfo.year,
            "season": season,
            "episodes": normalized,
            "image": mediainfo.get_poster_image(),
            "notification_kind": notification_kind,
        })

    @staticmethod
    def _history_record_identity(
            record: Dict[str, Any]
    ) -> Optional[Tuple[str, ...]]:
        """使用持久字段标识同一条历史，避免旧快照覆盖监控更新。"""
        identity = tuple(
            str(record.get(key) or "")
            for key in (
                "share_url",
                "file_name",
                "tmdb_id",
                "season",
                "episode",
            )
        )
        return identity if identity[0] and identity[1] else None

    @classmethod
    def _upgrade_scope_identity(
            cls, record: Dict[str, Any]
    ) -> Optional[Tuple[str, ...]]:
        """标识同一媒体季集，洗版写入时据此替换旧版本记录。"""
        media_type = str(record.get("type") or "").strip()
        tmdb_id = str(record.get("tmdb_id") or "").strip()
        media_key = (
            f"tmdb:{tmdb_id}"
            if tmdb_id
            else "legacy:"
                 f"{str(record.get('title') or '').strip()}:"
                 f"{str(record.get('year') or '').strip()}"
        )
        if not media_type or media_key == "legacy::":
            return None
        if media_type == MediaType.MOVIE.value:
            return media_type, media_key
        season = cls._positive_int(record.get("season"))
        episode = cls._positive_int(record.get("episode"))
        if not season or not episode:
            return None
        return media_type, media_key, str(season), str(episode)

    @classmethod
    def _ensure_history_record_id(cls, record: Dict[str, Any]) -> str:
        record_id = str(record.get("record_id") or "").strip()
        if record_id:
            return record_id
        identity = cls._history_record_identity(record)
        if not identity:
            return ""
        record_id = hashlib.sha1("\0".join(identity).encode("utf-8")).hexdigest()
        record["record_id"] = record_id
        return record_id

    @staticmethod
    def _positive_int(value: Any) -> Optional[int]:
        try:
            number = int(value or 0)
        except (TypeError, ValueError):
            return None
        return number if number > 0 else None

    @staticmethod
    def _is_upgrade_history(record: Dict[str, Any]) -> bool:
        value = record.get("upgrade")
        if isinstance(value, bool):
            return value
        return str(value or "").strip().casefold() in {"1", "true", "yes", "on"}

    @classmethod
    def _history_task_types(cls, record: Dict[str, Any]) -> List[str]:
        raw_types = record.get("task_types") or []
        if isinstance(raw_types, str):
            raw_types = [raw_types]
        task_types = {
            str(value or "").strip()
            for value in raw_types
            if str(value or "").strip()
        }
        if str(record.get("transfer_mode") or "") == "cross":
            task_types.add("cross_transfer")
        if cls._is_upgrade_history(record):
            task_types.add("upgrade")
        return sorted(task_types)

    @classmethod
    def _is_workflow_history(cls, record: Dict[str, Any]) -> bool:
        return bool(cls._history_task_types(record))

    @staticmethod
    def _platform_episode_numbers(record: Dict[str, Any]) -> List[int]:
        values = (
            record.get("success_episodes")
            or record.get("episode")
            or record.get("episodes")
            or record.get("target_episodes")
        )
        candidates = values if isinstance(values, (list, tuple, set)) else [values]
        episodes = {
            int(number)
            for value in candidates
            for number in re.findall(r"\d+", str(value or ""))
            if int(number) > 0
        }
        if (
                not isinstance(values, (list, tuple, set))
                and "-" in str(values or "")
                and len(episodes) >= 2
        ):
            episodes.update(range(min(episodes), max(episodes) + 1))
        return sorted(episodes)

    @staticmethod
    def _platform_source_label(record: Dict[str, Any]) -> str:
        source = str(record.get("source") or "").strip().lower()
        labels = {
            "hdhive": "HDHive",
            "dian115": "Dian115",
            "pansou": "PanSou",
            "manual": "手动添加",
        }
        if source in labels:
            return labels[source]
        resource_type = str(record.get("resource_type") or "").strip().lower()
        return {
            "115": "115资源",
            "cloud": "网盘路径",
            "ed2k": "ED2K",
            "magnet": "Magnet",
        }.get(resource_type, "网盘搜索助手")

    @classmethod
    def _platform_source_path(
            cls,
            record: Dict[str, Any],
            season: Optional[int] = None,
            episode: Optional[int] = None,
    ) -> str:
        """生成可读且稳定的整理来源标识，避免界面显示内部协议路径。"""
        title = re.sub(r"[\\/]+", "-", str(record.get("title") or "").strip())
        scope = ""
        if season:
            scope = f" S{season:02d}"
            if episode:
                scope += f"E{episode:02d}"
        record_id = cls._ensure_history_record_id(record)
        suffix = f" #{record_id[:10]}" if record_id else ""
        return f"{cls._platform_source_label(record)} · {title}{scope}{suffix}"

    def _platform_history_entries(
            self, record: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        if (
                str(record.get("status") or "") != "成功"
                or self._is_upgrade_history(record)
        ):
            return []
        media_type = str(record.get("type") or "")
        if media_type not in {MediaType.MOVIE.value, MediaType.TV.value}:
            return []
        title = str(record.get("title") or "").strip()
        if not title:
            return []

        tmdb_id = self._positive_int(record.get("tmdb_id"))

        file_name = str(record.get("file_name") or "").strip()
        source_name = str(record.get("source_file_name") or file_name).strip()
        cloud_dir = str(record.get("cloud_dir") or "/").strip() or "/"
        destination = (
            str(PurePosixPath(cloud_dir) / file_name) if file_name else cloud_dir
        )
        try:
            file_size = max(0, int(record.get("file_size") or 0))
        except (TypeError, ValueError):
            file_size = 0
        source_storage = self._platform_source_label(record)
        dest_storage = str(
            record.get("cloud_drive_name")
            or getattr(self._cloud_drive, "name", "网盘")
            or "网盘"
        ).strip()
        common = {
            "src_storage": source_storage,
            "src_fileitem": {
                "name": source_name,
                "path": source_name,
                "size": file_size,
                "storage": source_storage,
            },
            "dest": destination,
            "dest_storage": dest_storage,
            "dest_fileitem": {
                "name": file_name,
                "path": destination,
                "size": file_size,
                "storage": dest_storage,
            },
            "mode": "copy",
            "type": media_type,
            "title": title,
            "year": str(record.get("year") or "") or None,
            "tmdbid": tmdb_id,
            "imdbid": str(record.get("imdb_id") or "").strip() or None,
            "tvdbid": self._positive_int(record.get("tvdb_id")),
            "doubanid": str(record.get("douban_id") or "").strip() or None,
            "bangumiid": self._positive_int(record.get("bangumi_id")),
            "anilistid": self._positive_int(record.get("anilist_id")),
            "media_source": str(record.get("media_source") or "").strip() or None,
            "media_id": str(record.get("media_id") or "").strip() or None,
            "category": str(record.get("category") or "").strip() or None,
            "episode_group": str(record.get("episode_group") or "").strip() or None,
            "image": str(record.get("image") or "").strip() or None,
            "status": True,
            "files": [destination] if destination else [],
            "downloader": "网盘搜索助手",
            "date": str(record.get("time") or "").strip() or None,
        }
        if media_type == MediaType.MOVIE.value:
            return [{
                **common,
                "src": self._platform_source_path(record),
            }]

        season = self._positive_int(record.get("season")) or 1
        episodes = self._platform_episode_numbers(record)
        if not episodes:
            if not self._ensure_history_record_id(record):
                return []
            return [{
                **common,
                "src": self._platform_source_path(record, season=season),
                "seasons": f"S{season:02d}",
            }]
        return [
            {
                **common,
                "src": self._platform_source_path(
                    record, season=season, episode=episode
                ),
                "seasons": f"S{season:02d}",
                "episodes": f"E{episode:02d}",
            }
            for episode in episodes
        ]

    def _record_platform_transfer_histories(
            self,
            records: List[Dict[str, Any]],
            reconcile: bool = False,
    ) -> int:
        if not self._platform_transfer_history_enabled and not reconcile:
            return 0
        entries_by_src = {
            entry["src"]: entry
            for record in (records if self._platform_transfer_history_enabled else [])
            if isinstance(record, dict)
            for entry in self._platform_history_entries(record)
        }
        entries = list(entries_by_src.values())
        if not entries and not reconcile:
            return 0
        try:
            from app.db import SessionFactory
            from app.db.models.transferhistory import TransferHistory
            from sqlalchemy import or_

            if not self._platform_history_lock.acquire(timeout=1.0):
                logger.debug("MoviePilot 整理历史写入仍在执行，本批次已跳过")
                return 0
            added = 0
            updated = 0
            removed = 0
            try:
                with SessionFactory() as db:
                    columns = set(TransferHistory.__table__.columns.keys())
                    desired_sources = {entry["src"] for entry in entries}
                    if reconcile:
                        managed = db.query(TransferHistory).filter(or_(
                            TransferHistory.src.like("pansearch://%"),
                            TransferHistory.downloader == "网盘搜索助手",
                        )).all()
                        existing_by_src = {item.src: item for item in managed}
                        for item in managed:
                            if item.src not in desired_sources:
                                db.delete(item)
                                existing_by_src.pop(item.src, None)
                                removed += 1
                    else:
                        existing = (
                            db.query(TransferHistory).filter(
                                TransferHistory.src.in_(sorted(desired_sources))
                            ).all()
                            if desired_sources else []
                        )
                        existing_by_src = {item.src: item for item in existing}
                    for entry in entries:
                        existing = existing_by_src.get(entry["src"])
                        if existing:
                            changed_fields = {
                                key: value
                                for key, value in entry.items()
                                if key in columns and key != "src"
                                   and getattr(existing, key, None) != value
                            }
                            if changed_fields:
                                for key, value in changed_fields.items():
                                    setattr(existing, key, value)
                                updated += 1
                            continue
                        db.add(TransferHistory(**{
                            key: value for key, value in entry.items()
                            if key in columns
                        }))
                        added += 1
                    if added or updated or removed:
                        db.commit()
            finally:
                self._platform_history_lock.release()
            if added or updated or removed:
                logger.debug(
                    f"MoviePilot 成功整理历史已同步：新增 {added} 条，"
                    f"更新 {updated} 条，清理 {removed} 条"
                )
            return added + updated + removed
        except Exception as error:
            logger.error(f"登记成功整理历史失败：{error}")
            return 0

    @staticmethod
    def _history_image_from_download(record: Dict[str, Any]) -> Optional[str]:
        """从下载历史恢复旧版插件记录缺失的海报。"""
        media_type = str(record.get("type") or "").strip()
        tmdb_id = HistoryService._positive_int(record.get("tmdb_id"))
        title = str(record.get("title") or "").strip()
        year = str(record.get("year") or "").strip() or None
        if media_type not in {MediaType.MOVIE.value, MediaType.TV.value}:
            return None
        if not tmdb_id and not title:
            return None
        try:
            histories = get_download_history_last_by(
                DownloadHistoryOper(),
                mtype=media_type,
                title=title or None,
                year=year,
                tmdb_id=tmdb_id,
            )
        except Exception as error:
            logger.debug(f"从下载历史恢复整理海报失败：{title}，{error}")
            return None
        return next(
            (
                str(getattr(item, "image", "") or "").strip()
                for item in (histories or [])
                if str(getattr(item, "image", "") or "").strip()
            ),
            None,
        )

    def _delete_platform_transfer_histories(
            self,
            records: Optional[List[Dict[str, Any]]] = None,
            all_managed: bool = False,
    ) -> int:
        """增量删除插件托管的整理历史，不触碰关联媒体文件。"""
        sources = {
            entry["src"]
            for record in (records or [])
            if isinstance(record, dict)
            for entry in self._platform_history_entries(record)
        }
        if not all_managed and not sources:
            return 0
        try:
            from app.db import SessionFactory
            from app.db.models.transferhistory import TransferHistory
            from sqlalchemy import or_

            if not self._platform_history_lock.acquire(timeout=1.0):
                logger.debug("MoviePilot 整理历史删除仍在执行，本批次已跳过")
                return 0
            try:
                with SessionFactory() as db:
                    query = db.query(TransferHistory).filter(or_(
                        TransferHistory.src.like("pansearch://%"),
                        TransferHistory.downloader == "网盘搜索助手",
                    ))
                    if not all_managed:
                        query = query.filter(TransferHistory.src.in_(sources))
                    deleted = query.delete(synchronize_session=False)
                    if deleted:
                        db.commit()
                    return int(deleted or 0)
            finally:
                self._platform_history_lock.release()
        except Exception as error:
            logger.error(f"清理整理历史失败：{error}")
            return 0

    def sync_platform_transfer_history(self) -> int:
        """让整理历史完整镜像插件历史；关闭开关时清理镜像。"""
        if not self._get_data:
            return 0
        with self._offline_pending_lock:
            history = self._get_data("history") or []
            image_cache: Dict[Tuple[Any, ...], Optional[str]] = {}
            restored = 0
            for record in history:
                if (
                        not isinstance(record, dict)
                        or str(record.get("status") or "") != "成功"
                        or str(record.get("image") or "").strip()
                ):
                    continue
                cache_key = (
                    record.get("type"),
                    record.get("tmdb_id"),
                    record.get("title"),
                    record.get("year"),
                )
                if cache_key not in image_cache:
                    image_cache[cache_key] = self._history_image_from_download(record)
                image = image_cache[cache_key]
                if image:
                    record["image"] = image
                    restored += 1
            if restored and self._save_data:
                self._save_data("history", history)
                logger.info(f"已从下载历史恢复 {restored} 条插件整理记录海报")
            records = [
                copy.deepcopy(record)
                for record in history
                if isinstance(record, dict)
            ]
        return self._record_platform_transfer_histories(records, reconcile=True)

    def append_history_records(
            self,
            records: List[Dict[str, Any]],
            reopen_terminal: bool = False,
    ) -> int:
        """合并新增历史，并原子激活对应的网盘文件后处理任务。"""
        if not records or not self._get_data or not self._save_data:
            return 0
        activated_pending_count = 0
        platform_records = []
        with self._offline_pending_lock:
            history = self._get_data("history") or []
            for record in history:
                self._ensure_history_record_id(record)
            record_index = {
                identity: index
                for index, record in enumerate(history)
                if (identity := self._history_record_identity(record))
            }
            upgrade_scope_index = {
                scope: index
                for index, record in enumerate(history)
                if (scope := self._upgrade_scope_identity(record))
            }
            workflow_scope_index = {
                scope: index
                for index, record in enumerate(history)
                if self._is_workflow_history(record)
                if (scope := self._upgrade_scope_identity(record))
            }
            failed_scope_index = {
                scope: index
                for index, record in enumerate(history)
                if str(record.get("status") or "") == "失败"
                if (scope := self._upgrade_scope_identity(record))
            }
            # v1.5.4 T3：同媒体季集已有成功记录时，失败写入不得制造矛盾终态。
            success_scope_index = {
                scope: index
                for index, record in enumerate(history)
                if str(record.get("status") or "") == "成功"
                if (scope := self._upgrade_scope_identity(record))
            }
            warned_failures: Set[str] = set()
            for record in records:
                incoming = copy.deepcopy(record)
                self._ensure_history_record_id(incoming)
                identity = self._history_record_identity(incoming)
                index = record_index.get(identity) if identity else None
                scope = self._upgrade_scope_identity(incoming)
                incoming_status = str(incoming.get("status") or "")
                incoming_is_upgrade = self._is_upgrade_history(incoming)
                if index is None and incoming_is_upgrade and scope:
                    index = upgrade_scope_index.get(scope)
                if index is None and scope and self._is_workflow_history(incoming):
                    index = workflow_scope_index.get(scope)
                # 换源重试可能改变分享链接和源文件名；只复用同媒体季集的失败记录，
                # 成功记录与普通多版本记录仍按精确身份隔离。
                if index is None and scope and not incoming_is_upgrade:
                    # v1.5.4 T3：失败优先并入同季集成功记录（同轮其他源已成功）。
                    if (
                            incoming_status == "失败"
                            and scope in success_scope_index
                            and not reopen_terminal
                    ):
                        index = success_scope_index[scope]
                    else:
                        index = failed_scope_index.get(scope)
                if index is None:
                    if incoming_status == "失败":
                        self._ensure_history_failure_reason(incoming)
                        self._warn_history_failure(incoming, warned_failures)
                    history.append(incoming)
                    platform_records.append(copy.deepcopy(incoming))
                    if identity:
                        record_index[identity] = len(history) - 1
                    if scope:
                        upgrade_scope_index[scope] = len(history) - 1
                        if self._is_workflow_history(incoming):
                            workflow_scope_index[scope] = len(history) - 1
                    if incoming_status == "成功":
                        if scope:
                            success_scope_index[scope] = len(history) - 1
                    elif incoming_status == "失败" and scope:
                        failed_scope_index[scope] = len(history) - 1
                    continue

                current = history[index]
                current_status = str(current.get("status") or "")
                merged = {**current, **incoming}
                merged["task_types"] = sorted(
                    set(self._history_task_types(current))
                    | set(self._history_task_types(incoming))
                )
                if incoming_is_upgrade:
                    merged["upgrade"] = True
                    merged["upgrade_count"] = max(
                        1, int(current.get("upgrade_count") or 0) + 1
                    )
                    merged["previous_file_name"] = str(
                        current.get("file_name") or ""
                    )
                    merged["previous_file_size"] = current.get("file_size")
                    merged["previous_rule_score"] = current.get("rule_score")
                if (
                        current_status in {"成功", "失败"}
                        and incoming_status not in {"成功", "失败"}
                        and not reopen_terminal
                ):
                    merged["status"] = current_status
                    for state_key in ("finalize_key", "failure_reason"):
                        if state_key in current:
                            merged[state_key] = current[state_key]
                        else:
                            merged.pop(state_key, None)
                # 成功优先于失败：该媒体季集此前已成功时，不因新的失败降级。
                if (
                        not incoming_is_upgrade
                        and not reopen_terminal
                        and current_status == "成功"
                        and incoming_status == "失败"
                ):
                    merged["status"] = "成功"
                    logger.warning(
                        f"忽略矛盾失败记录，该媒体季集已成功："
                        f"{self._history_failure_label(merged)}，"
                        f"原因：{str(incoming.get('failure_reason') or '').strip() or '未提供失败原因'}"
                    )
                merged_status = str(merged.get("status") or "")
                # 失败终态必带原因并告警；非失败状态清除历史原因残留。
                if merged_status == "失败":
                    self._ensure_history_failure_reason(merged)
                    self._warn_history_failure(merged, warned_failures)
                else:
                    merged.pop("failure_reason", None)
                history[index] = merged
                platform_records.append(copy.deepcopy(merged))
                if scope:
                    upgrade_scope_index[scope] = index
                    if self._is_workflow_history(merged):
                        workflow_scope_index[scope] = index
                    if merged_status == "成功":
                        success_scope_index[scope] = index
                        if failed_scope_index.get(scope) == index:
                            failed_scope_index.pop(scope, None)
                    elif merged_status == "失败":
                        failed_scope_index[scope] = index
                    elif failed_scope_index.get(scope) == index:
                        failed_scope_index.pop(scope, None)
            self._save_data("history", history)
            finalize_keys = {
                str(record.get("finalize_key") or "")
                for record in records
                if str(record.get("finalize_key") or "")
            }
            if finalize_keys:
                pending = self._get_data(self._OFFLINE_PENDING_KEY) or {}
                now = time.time()
                activated = False
                for pending_key in finalize_keys:
                    item = pending.get(pending_key)
                    if not item:
                        continue
                    item["history_ready"] = True
                    item["next_check_at"] = min(
                        float(item.get("next_check_at") or now), now
                    )
                    activated = True
                if activated:
                    self._save_offline_pending(pending)
                    activated_pending_count = len(pending)
        if activated_pending_count:
            self._notify_offline_pending_changed(activated_pending_count)
        self._record_platform_transfer_histories(platform_records)
        return len(records)

    @staticmethod
    def _ensure_history_failure_reason(record: Dict[str, Any]) -> str:
        """失败记录必带原因；旧记录缺 failure_reason 键时补占位，绝不崩溃。"""
        reason = str(record.get("failure_reason") or "").strip()
        if not reason:
            reason = "未提供失败原因"
            record["failure_reason"] = reason
        return reason

    @staticmethod
    def _history_failure_label(record: Dict[str, Any]) -> str:
        """失败告警标识：订阅/媒体 + 季集，便于定位具体条目。"""
        try:
            season = int(record.get("season") or 0)
        except (TypeError, ValueError):
            season = 0
        try:
            episode = int(record.get("episode") or 0)
        except (TypeError, ValueError):
            episode = 0
        subscribe_id = str(record.get("subscribe_id") or "").strip()
        title = str(
            record.get("title") or record.get("file_name") or "未知媒体"
        ).strip()
        parts = [f"订阅#{subscribe_id}"] if subscribe_id else []
        parts.append(title)
        if season > 0:
            parts.append(f"S{season:02d}")
        if episode > 0:
            parts.append(f"E{episode:02d}")
        return " ".join(parts)

    def _warn_history_failure(
            self, record: Dict[str, Any], warned: Set[str]
    ) -> None:
        """任何失败写入至少产生一条 WARNING（含订阅/集数/原因）。"""
        reason = self._ensure_history_failure_reason(record)
        label = self._history_failure_label(record)
        key = f"{label}|{reason}"
        if key in warned:
            return
        warned.add(key)
        logger.warning(f"历史记录写入失败状态：{label}，原因：{reason}")

    def compact_workflow_history(self) -> List[Dict[str, Any]]:
        """合并同一媒体季集的跨盘、洗版工作流记录并持久化。"""
        if not self._get_data or not self._save_data:
            return []
        with self._offline_pending_lock:
            history = [
                copy.deepcopy(record)
                for record in (self._get_data("history") or [])
                if isinstance(record, dict)
            ]
            compacted: List[Dict[str, Any]] = []
            scope_indexes: Dict[Tuple[str, ...], int] = {}
            upgrade_scopes = {
                scope
                for record in history
                if self._is_upgrade_history(record)
                if (scope := self._upgrade_scope_identity(record))
            }
            for record in history:
                self._ensure_history_record_id(record)
                scope = self._upgrade_scope_identity(record)
                index = scope_indexes.get(scope) if scope else None
                should_merge = bool(
                    index is not None
                    and (
                            scope in upgrade_scopes
                            or (
                                    self._is_workflow_history(record)
                                    and self._is_workflow_history(compacted[index])
                            )
                    )
                )
                if not should_merge:
                    compacted.append(record)
                    if scope:
                        scope_indexes[scope] = len(compacted) - 1
                    continue
                current = compacted[index]
                latest, previous = (
                    (record, current)
                    if str(record.get("time") or "") >= str(current.get("time") or "")
                    else (current, record)
                )
                is_upgrade = (
                        self._is_upgrade_history(previous)
                        or self._is_upgrade_history(latest)
                )
                merged = {**previous, **latest, "upgrade": is_upgrade}
                merged["task_types"] = sorted(
                    set(self._history_task_types(previous))
                    | set(self._history_task_types(latest))
                    | ({"upgrade"} if is_upgrade else set())
                )
                if is_upgrade:
                    merged["upgrade_count"] = max(
                        1,
                        int(current.get("upgrade_count") or int(
                            self._is_upgrade_history(current)
                        )) + int(self._is_upgrade_history(record)),
                    )
                    merged["previous_file_name"] = str(
                        previous.get("file_name") or ""
                    )
                    merged["previous_file_size"] = previous.get("file_size")
                    merged["previous_rule_score"] = previous.get("rule_score")
                compacted[index] = merged
            if compacted != history:
                self._save_data("history", compacted)
                logger.info(
                    f"跨盘/洗版历史已合并：{len(history)} 条 -> {len(compacted)} 条"
                )
            return compacted

    @staticmethod
    def _format_history_size(value: Any) -> str:
        try:
            size = max(0, int(value or 0))
        except (TypeError, ValueError):
            return "-"
        if not size:
            return "-"
        amount = float(size)
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if amount < 1024 or unit == "TB":
                digits = 0 if amount >= 100 else 1
                return f"{amount:.{digits}f} {unit}"
            amount /= 1024
        return "-"

    def _history_page_fields(self, record: Dict[str, Any]) -> Dict[str, str]:
        """生成历史页面所需的稳定标识、名称和可点击链接。"""
        media_type = str(record.get("type") or "未知类型")

        file_name = str(
            record.get("file_name") or record.get("source_file_name") or ""
        ).strip() or "-"
        extension = Path(file_name).suffix.removeprefix(".").upper() or "-"
        if media_type == "电影":
            display_name = str(record.get("title") or "").strip()
            if not display_name:
                display_name = Path(file_name).stem or "-"
        else:
            try:
                season = max(0, int(record.get("season") or 0))
            except (TypeError, ValueError):
                season = 0
            target_episodes = record.get("target_episodes")
            values = (
                target_episodes
                if isinstance(target_episodes, (list, tuple, set))
                else re.findall(r"\d+", str(target_episodes or ""))
            )
            episodes = set()
            for value in values:
                try:
                    episode = int(value)
                except (TypeError, ValueError):
                    continue
                if episode > 0:
                    episodes.add(episode)
            ordered = sorted(episodes)
            season_label = f"S{season:02d}"
            if len(ordered) > 1:
                display_name = (
                    f"{season_label}E{ordered[0]:02d}-E{ordered[-1]:02d}"
                )
            else:
                try:
                    episode = ordered[0] if ordered else int(
                        record.get("episode") or 0
                    )
                except (TypeError, ValueError):
                    episode = 0
                display_name = (
                    f"{season_label}E{episode:02d}"
                    if episode > 0 else season_label
                )

        source = str(record.get("source") or "").strip().casefold()
        source_url = str(
            record.get("source_url") or record.get("media_page_url") or ""
        ).strip()
        if source == "hdhive" and source_url:
            parsed = urlsplit(source_url)
            if parsed.scheme and parsed.netloc:
                source_url = urlunsplit(("", "", parsed.path, parsed.query, ""))
            base_url = str(
                getattr(self, "_hdhive_base_url", "https://re0.me")
                or "https://re0.me"
            ).strip().rstrip("/")
            source_url = urljoin(f"{base_url}/", source_url.lstrip("/"))
        has_source_link = (
                source not in {"manual", "手动添加", "手动资源"}
                and bool(re.match(r"^https?://", source_url, re.IGNORECASE))
        )
        source_link = source_url if has_source_link else ""
        share_url = str(record.get("share_url") or "").strip()
        resource_link = (
            share_url
            if re.match(r"^(?:https?|ed2k|magnet):", share_url, re.IGNORECASE)
            else ""
        )
        return {
            "history_group_key": history_group_key(record),
            "display_name": display_name,
            "display_file_name": file_name,
            "file_extension": extension,
            "source_link": source_link,
            "resource_link": resource_link,
        }

    @classmethod
    def _history_retry_state(cls, record: Dict[str, Any]) -> Tuple[bool, str]:
        status = str(record.get("status") or "")
        is_cross = str(record.get("transfer_mode") or "") == "cross"
        if status == "成功" and is_cross:
            return False, "跨盘任务已成功完成，无需重试"
        if not str(record.get("share_url") or "").strip():
            return False, "历史记录缺少资源链接，无法重试"
        if status == "失败":
            if is_cross:
                cache_status = str(record.get("cache_status") or "")
                title = (
                    "恢复跨盘转存"
                    if cache_status in {"complete", "partial"}
                    else "重新执行跨盘转存"
                )
                return True, title
            return True, "重试此记录"
        if (
                status == "成功"
                and str(record.get("source_file_name") or "")
                == str(record.get("file_name") or "")
        ):
            return True, "修复此记录"
        return False, "当前记录无需重试"

    def prepare_history_records(
            self, records: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """为历史页面生成业务展示字段，前端不再重复推导状态。"""
        prepared = []
        for source in records or []:
            record = copy.deepcopy(source)
            record["resource_type"] = (
                    normalize_resource_type(
                        record.get("resource_type") or record.get("pan_type") or ""
                    )
                    or resource_type_from_url(record.get("share_url"))
                    or "unknown"
            )
            record.update(self._history_page_fields(record))
            is_cross = str(record.get("transfer_mode") or "") == "cross"
            record["is_cross_transfer"] = is_cross
            record["task_types"] = self._history_task_types(record)
            if is_cross:
                source_name = str(
                    record.get("source_drive_name")
                    or record.get("source_drive_key")
                    or "源网盘"
                )
                target_name = str(
                    record.get("target_drive_name")
                    or record.get("target_drive_key")
                    or "目标网盘"
                )
                cache_labels = {
                    "complete": "缓存完整",
                    "partial": "可断点续传",
                    "missing": "缓存已清理" if record.get("status") == "成功" else "无可用缓存",
                }
                cache_label = cache_labels.get(
                    str(record.get("cache_status") or ""), cache_labels["missing"]
                )
                record["cross_transfer_title"] = (
                    f"{source_name} → {target_name} · {cache_label}"
                )
            can_retry, retry_title = self._history_retry_state(record)
            record["can_retry"] = can_retry
            record["retry_title"] = retry_title
            if self._is_upgrade_history(record):
                parts = []
                previous_name = str(record.get("previous_file_name") or "").strip()
                if previous_name:
                    parts.append(previous_name)
                previous_size = self._format_history_size(record.get("previous_file_size"))
                current_size = self._format_history_size(record.get("file_size"))
                if previous_size != "-":
                    parts.append(
                        f"{previous_size} → {current_size}"
                        if current_size != "-" else previous_size
                    )
                if record.get("previous_rule_score") is not None:
                    parts.append(
                        f"评分 {int(record.get('previous_rule_score') or 0)}"
                        f" → {int(record.get('rule_score') or 0)}"
                    )
                count = max(1, int(record.get("upgrade_count") or 1))
                count_label = f"（已洗版 {count} 次）" if count > 1 else ""
                record["upgrade_version_info"] = (
                    f"原版{count_label}：{' · '.join(parts) or '版本信息缺失'}"
                )
            prepared.append(record)
        return prepared

    @staticmethod
    def prepare_history_group(group: Dict[str, Any]) -> Dict[str, Any]:
        """补充单个数据库媒体组的展示字段，不在应用层重新分组。"""
        prepared = copy.deepcopy(group)
        records = prepared.get("records") or []
        first = records[0] if records else {}
        prepared.update({
            "tmdb_id": first.get("tmdb_id"),
            "title": first.get("title") or "未知媒体",
            "year": first.get("year") or "",
            "type": first.get("type") or "未知类型",
            "resource_types": list(dict.fromkeys(
                str(record.get("resource_type") or "unknown")
                for record in records
                if str(record.get("resource_type") or "").strip()
            )),
        })
        prepared["sources"] = list(dict.fromkeys(
            "manual"
            if str(record.get("source") or "").strip().casefold()
               in {"manual", "手动添加", "手动资源"}
            else str(record.get("source") or "unknown").strip().casefold()
            for record in records
        ))
        prepared["source_items"] = [
            {"value": source} for source in prepared["sources"]
        ]
        prepared["seasons"] = sorted({
            int(record.get("season") or 0)
            for record in records
            if str(record.get("season") or "").isdigit()
               and int(record.get("season") or 0) > 0
        })
        prepared["resource_link_count"] = len({
            str(record.get("resource_link") or "").strip()
            for record in records
            if str(record.get("resource_link") or "").strip()
        })
        prepared["notification_record"] = max(
            (
                record for record in records
                if record.get("status") == "成功" and not record.get("finalize_key")
            ),
            key=lambda record: str(record.get("time") or ""),
            default=None,
        )
        prepared["selectable"] = bool(records)
        prepared["deletable"] = bool(records) and all(
            record.get("status") in {"成功", "失败"}
            or bool(record.get("finalize_key"))
            for record in records
        )
        return prepared

    def reconcile_orphaned_history(self) -> int:
        """将 pending 已消失但 STRM 已存在的假下载中记录纠正为成功。"""
        if (
                not self._get_data
                or not self._save_data
                or not self._strm_generator
                or not self._local_resource_path
        ):
            return 0
        repaired = 0
        repaired_records = []
        with self._offline_pending_lock:
            pending = self._get_data(self._OFFLINE_PENDING_KEY) or {}
            history = self._get_data("history") or []
            for record in history:
                if str(record.get("status") or "") not in {"下载中", "处理中"}:
                    continue
                pending_key = str(record.get("finalize_key") or "")
                if not pending_key:
                    pending_key = self._offline_hash(str(record.get("share_url") or ""))
                if pending_key and pending_key in pending:
                    continue
                cloud_dir = str(record.get("cloud_dir") or "").strip()
                file_name = str(record.get("file_name") or "").strip()
                if not cloud_dir or not file_name:
                    continue
                try:
                    strm_path = self._strm_generator.local_path(
                        local_root=self._local_resource_path,
                        cloud_root=self._CLOUD_MEDIA_ROOT,
                        cloud_dir=cloud_dir,
                        file_name=file_name,
                    )
                except Exception as error:
                    logger.debug(f"检查遗留历史 STRM 失败：{file_name}，{error}")
                    continue
                if not strm_path.is_file():
                    continue
                record["status"] = "成功"
                record.pop("finalize_key", None)
                record.pop("failure_reason", None)
                repaired_records.append(copy.deepcopy(record))
                repaired += 1
            if repaired:
                self._save_data("history", history)
        if repaired:
            logger.info(f"已自动修复 {repaired} 条 STRM 已存在但状态未完成的历史记录")
            self._record_platform_transfer_histories(repaired_records)
            if self._history_changed:
                self._history_changed()
        return repaired

    @staticmethod
    def _offline_backfill_record_key(record: Dict[str, Any]) -> str:
        """存量核对的记录键：分享链接 + 目录 + 文件名。"""
        share_url = str(record.get("share_url") or "").strip()
        file_name = str(
            record.get("source_file_name") or record.get("file_name") or ""
        ).strip()
        cloud_dir = str(
            record.get("staging_dir") or record.get("cloud_dir") or ""
        ).strip()
        if not file_name and not share_url:
            return ""
        return f"{share_url}|{cloud_dir}|{file_name}"

    @staticmethod
    def _offline_backfill_record_label(record: Dict[str, Any]) -> str:
        """回填 INFO 日志的记录标识：标题（剧集带季集，缺失退回文件名）。"""
        title = str(record.get("title") or "").strip()
        if not title:
            return (
                str(
                    record.get("source_file_name")
                    or record.get("file_name") or ""
                ).strip()
                or "未知记录"
            )
        if str(record.get("type") or "") == "电视剧":
            try:
                season = max(1, int(record.get("season") or 1))
                episode = int(record.get("episode") or 0)
            except (TypeError, ValueError):
                season, episode = 1, 0
            if episode > 0:
                return f"{title} S{season:02d}E{episode:02d}"
        return title

    @staticmethod
    def _offline_record_locate_fields(
            record: Dict[str, Any]
    ) -> Tuple[List[str], str]:
        """从历史记录提取定位所需字段：候选文件名列表与 source_sha1。"""
        names: List[str] = []
        for name in (
                record.get("staging_name"),
                record.get("file_name"),
                record.get("source_file_name"),
        ):
            text = str(name or "").strip()
            if text and text not in names:
                names.append(text)
        source_sha1 = str(record.get("source_sha1") or "").upper()
        return names, source_sha1

    def _full_pan_search_available(self) -> bool:
        """当前网盘与宿主是否具备全盘兜底检索能力（不可用时不得判死）。"""
        if not callable(getattr(self, "_full_pan_locate_file", None)):
            return False
        try:
            query = getattr(self, "_cloud_query", None)
        except Exception:
            return False
        return callable(getattr(query, "list_files_recursive", None))

    def _offline_backfill_verdict(
            self,
            record: Dict[str, Any],
            directory_snapshot,
            purpose: str = "backfill",
    ) -> str:
        """与超时终审同款的反查：按文件名或源哈希在中转与最终目录找文件。

        目录列举全部有效但未命中时，再复用终审 T1 的全盘检索能力兜底，
        覆盖文件已被整理/移动/改名到其他目录的场景。返回 "ready"（文件
        已就绪）、"defer"（目录接口瞬态错误，本轮跳过）或 "missing"
        （确实不存在）。
        """

        def entry_name(entry: Any) -> str:
            value = getattr(entry, "name", None)
            if not value and isinstance(entry, dict):
                value = entry.get("name") or entry.get("file_name")
            return str(value or "").strip()

        staging_dir = str(
            record.get("staging_dir") or record.get("cloud_dir") or "/"
        ).rstrip("/") or "/"
        final_dir = str(record.get("cloud_dir") or "/").rstrip("/") or "/"
        names: List[str] = []
        for name in (
                record.get("staging_name"),
                record.get("file_name"),
                record.get("source_file_name"),
        ):
            text = str(name or "").strip()
            if text and text not in names:
                names.append(text)
        source_sha1 = str(record.get("source_sha1") or "").upper()
        if not names and not source_sha1:
            return "missing"
        directories = [staging_dir]
        if final_dir != staging_dir:
            directories.append(final_dir)
        for cloud_dir in directories:
            directory_valid, file_index = directory_snapshot(cloud_dir)
            if not directory_valid:
                return "defer"
            if not file_index:
                continue
            candidate = next(
                (file_index[name] for name in names if name in file_index),
                None,
            )
            if not candidate and source_sha1:
                candidate = next(
                    (
                        value for value in file_index.values()
                        if str(getattr(value, "sha1", "") or "").upper()
                        == source_sha1
                    ),
                    None,
                )
            if candidate:
                logger.info(
                    f"存量核对发现文件已在网盘就绪，"
                    f"{'确认历史成功状态' if purpose == 'audit' else '自动回填为成功'}："
                    f"{self._offline_backfill_record_label(record)} -> "
                    f"{cloud_dir}/{entry_name(candidate) or (names[0] if names else '')}"
                )
                return "ready"
        # 目录内未命中：走全盘检索兜底（仅当宿主具备递归检索能力）。
        locate = getattr(self, "_full_pan_locate_file", None)
        if callable(locate):
            located = locate(
                record,
                file_name=names[0] if names else "",
                source_sha1=source_sha1,
            )
            if located:
                target, actual_dir = located
                logger.info(
                    f"存量核对全盘检索命中文件，"
                    f"{'确认历史成功状态' if purpose == 'audit' else '自动回填为成功'}："
                    f"{self._offline_backfill_record_label(record)} -> "
                    f"{str(actual_dir or '/').rstrip('/') or '/'}/"
                    f"{entry_name(target) or (names[0] if names else '')}"
                )
                return "ready"
        return "missing"

    def _offline_backfill_notification_detail(
            self, record: Dict[str, Any]
    ) -> Dict[str, Any]:
        """从历史记录构造补发通知所需的结构，复用既有完成通知链路。"""
        is_tv = str(record.get("type") or "") == "电视剧"
        detail = {
            "type": "电视剧" if is_tv else "电影",
            "title": record.get("title"),
            "year": record.get("year"),
            "image": record.get("image"),
            "file_name": record.get("file_name") or record.get("source_file_name"),
            "notification_kind": (
                "upgrade" if self._is_upgrade_history(record)
                else "cross_transfer" if record.get("transfer_mode") == "cross"
                else "transfer"
            ),
        }
        if is_tv:
            detail["season"] = max(1, int(record.get("season") or 1))
            try:
                episode = int(record.get("episode") or 0)
            except (TypeError, ValueError):
                episode = 0
            detail["episodes"] = [episode] if episode > 0 else []
        return detail

    _OFFLINE_SUCCESS_AUDIT_RESOURCE_TYPES = {"magnet", "ed2k", "offline"}

    @classmethod
    def _offline_success_audit_candidate(cls, record: Dict[str, Any]) -> bool:
        """判定成功记录是否属于需要反向回查的离线"提交即成功"高危项。

        仅对确实可能"提交成功≠下载成功"的记录回查：残留 finalize_key
        （成功终态本应清除）、资源类型为磁力/ED2K，或分享链接本身就是
        磁力/ED2K。普通分享转存成功不做全盘回查，避免误降级。
        """
        if str(record.get("status") or "").strip() != "成功":
            return False
        if str(record.get("finalize_key") or "").strip():
            return True
        resource_type = str(record.get("resource_type") or "").strip().lower()
        if resource_type in cls._OFFLINE_SUCCESS_AUDIT_RESOURCE_TYPES:
            return True
        share_url = str(record.get("share_url") or "").strip().lower()
        return share_url.startswith("magnet:") or share_url.startswith("ed2k://")

    def _offline_success_reverse_verdict(
            self,
            record: Dict[str, Any],
            directory_snapshot,
    ) -> str:
        """T2 修复前假成功记录的反向回查结论。

        返回 "confirmed"（文件确实在网盘，成功无误）、"missing"
        （经目录与全盘检索均未找到，确证假成功）或 "defer"（检索能力
        不足或接口瞬态错误，绝不在证据不足时误降级）。
        """
        names, source_sha1 = self._offline_record_locate_fields(record)
        if not names and not source_sha1:
            return "defer"
        verdict = self._offline_backfill_verdict(
            record, directory_snapshot, purpose="audit"
        )
        if verdict == "ready":
            return "confirmed"
        if verdict == "defer":
            return "defer"
        # 目录内未命中且全盘检索能力不可用：证据不足，暂不降级。
        if not self._full_pan_search_available():
            return "defer"
        return "missing"

    @staticmethod
    def _offline_fake_success_reason() -> str:
        return (
            "存量核对反向回查：成功记录在网盘目录与全盘检索均未找到实体"
            "文件，确认属于提交成功但实际未落盘的假成功，已降级为失败"
        )

    def _downgrade_fake_success_record(
            self, record: Dict[str, Any], reason: str
    ) -> None:
        """把确认的假成功记录降级为失败并记录 INFO（T4 存量核对升级）。"""
        record["status"] = "失败"
        record["failure_reason"] = reason
        record.pop("finalize_key", None)
        record["offline_downgraded_at"] = time.time()
        logger.info(
            f"存量核对反向回查降级："
            f"{self._offline_backfill_record_label(record)}，原因：{reason}"
        )

    def reconcile_offline_history_backfill(self, limit: int = 10) -> int:
        """存量核对：失败/空/处理中回填成功 + 离线假成功反向回查降级。

        每轮同步调用两类核对：
        1) 回填：状态为失败、空值或处理中的历史记录，按文件名或源哈希在
           中转与最终目录反查，未命中再走全盘检索兜底；就绪则回填为成功
           并补发通知。
        2) 假成功反查（v1.5.4 T4）：离线（磁力/ED2K）成功记录做反向回查，
           目录与全盘检索均未找到实体文件时确认属于"提交即成功"的假成功，
           降级为失败并修正订阅进度 note，防止永久污染；检索能力不可用或
           接口瞬态错误一律 defer，绝不在证据不足时误降级。
        每轮最多核对 limit 条，近期核对过的键做内存缓存避免全量列目录。
        """
        if (
                not self._get_data
                or not self._save_data
                or not self._cloud_directories
        ):
            return 0
        now = time.time()
        checked_at = getattr(self, "_offline_backfill_checked_at", None)
        if checked_at is None:
            checked_at = {}
            self._offline_backfill_checked_at = checked_at
        if len(checked_at) > self._OFFLINE_BACKFILL_CACHE_MAXSIZE:
            for key, value in list(checked_at.items()):
                if now - value >= self._OFFLINE_BACKFILL_RECHECK_SECONDS:
                    checked_at.pop(key, None)
        directory_snapshots: Dict[str, Tuple[bool, Dict[str, Any]]] = {}

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
                    result = (True, {
                        file_item.name: file_item
                        for file_item in listing.files
                        if file_item.name
                    })
            directory_snapshots[normalized_dir] = result
            return result

        max_checks = max(1, int(limit or 10))
        repaired_records: List[Dict[str, Any]] = []
        notification_details: List[Dict[str, Any]] = []
        downgraded_records: List[Dict[str, Any]] = []
        with self._offline_pending_lock:
            pending = self._get_data(self._OFFLINE_PENDING_KEY) or {}
            history = self._get_data("history") or []
            checked = 0
            for record in history:
                if checked >= max_checks:
                    break
                if not isinstance(record, dict):
                    continue
                status = str(record.get("status") or "").strip()
                # 存量回填候选：失败/空/处理中；假成功反查候选：成功但
                # 属离线提交高危项（v1.5.4 T4）。
                is_backfill = (
                    not status or status in self._OFFLINE_BACKFILL_STATUSES
                )
                audit_check = getattr(
                    self, "_offline_success_audit_candidate", None
                )
                is_audit = (
                    bool(audit_check(record)) if callable(audit_check) else False
                )
                if not is_backfill and not is_audit:
                    continue
                if is_audit and record.get("offline_verified_at"):
                    # 已确认无误的成功记录不再重复全盘检索。
                    continue
                finalize_key = str(record.get("finalize_key") or "")
                if finalize_key and finalize_key in pending:
                    # 仍有活跃后处理任务，交由离线监视器收敛，不重复回填。
                    continue
                key = self._offline_backfill_record_key(record)
                if not key:
                    continue
                if now - checked_at.get(key, 0) < (
                        self._OFFLINE_BACKFILL_RECHECK_SECONDS
                ):
                    continue
                checked += 1
                try:
                    reverse = getattr(
                        self, "_offline_success_reverse_verdict", None
                    )
                    if is_audit and callable(reverse):
                        verdict = reverse(record, directory_snapshot)
                    elif is_audit:
                        # 反查能力不可用：证据不足，绝不误降级。
                        verdict = "defer"
                    else:
                        verdict = self._offline_backfill_verdict(
                            record, directory_snapshot
                        )
                except Exception as error:
                    logger.debug(
                        f"存量核对反查网盘文件异常，本轮跳过："
                        f"{record.get('file_name') or key}，{error}"
                    )
                    verdict = "defer"
                if verdict == "defer":
                    # 瞬态接口错误/证据不足：本轮跳过且不记缓存，下一轮重试。
                    continue
                checked_at[key] = now
                if is_audit:
                    downgrade = getattr(
                        self, "_downgrade_fake_success_record", None
                    )
                    reason_fn = getattr(
                        self, "_offline_fake_success_reason", None
                    )
                    if verdict == "confirmed":
                        record["offline_verified_at"] = now
                    elif callable(downgrade) and callable(reason_fn):
                        downgrade(record, reason_fn())
                        downgraded_records.append(record)
                    continue
                if verdict != "ready":
                    continue
                record["status"] = "成功"
                record.pop("failure_reason", None)
                record.pop("finalize_key", None)
                repaired_records.append(copy.deepcopy(record))
                notification_details.append(
                    self._offline_backfill_notification_detail(record)
                )
            if repaired_records or downgraded_records:
                self._save_data("history", history)
        if downgraded_records:
            # 修正订阅进度 note：把被降级集数从 note 移除并重算缺集数，
            # 防止假成功永久污染订阅进度（v1.5.4 T4）。
            refresh_notes = getattr(
                self, "_refresh_deleted_subscribe_notes", None
            )
            if callable(refresh_notes):
                try:
                    refresh_notes(
                        downgraded_records,
                        history,
                        log_context="假成功反向回查降级后",
                    )
                except Exception as error:
                    logger.warning(f"假成功降级后修正订阅进度失败：{error}")
            labels = "、".join(
                self._offline_backfill_record_label(item)
                for item in downgraded_records
            )
            logger.info(
                f"存量核对反向回查降级：{len(downgraded_records)} 条假成功记录"
                f"在网盘目录与全盘检索均未找到实体文件，已降级为失败"
                f"并修正订阅进度：{labels}"
            )
            if self._history_changed:
                self._history_changed()
        if repaired_records:
            labels = "、".join(
                self._offline_backfill_record_label(item)
                for item in repaired_records
            )
            logger.info(
                f"存量核对自动回填：{len(repaired_records)} 条失败/处理中记录"
                f"已在网盘就绪，已回填为成功并补发通知：{labels}"
            )
            self._record_platform_transfer_histories(repaired_records)
            self._send_finalized_batch(notification_details)
            if self._history_changed:
                self._history_changed()
        return len(repaired_records)

    def _pending_history_record(self, pending_key: str) -> Optional[Dict[str, Any]]:
        history = (self._get_data("history") or []) if self._get_data else []
        return next(
            (
                record
                for record in history
                if str(record.get("finalize_key") or "") == pending_key
                   or pending_key.upper()
                   in str(record.get("share_url") or "").upper()
            ),
            None,
        )

    def _restore_pending_media_context(
            self, item: Dict[str, Any], pending_key: str
    ) -> Tuple[Optional[MediaInfo], Dict[str, Any]]:
        media_data = item.get("mediainfo") or {}
        if media_data:
            try:
                return self._deserialize_mediainfo(media_data), media_data
            except Exception as error:
                logger.warning(f"网盘文件后处理媒体信息已失效，将重新识别：{error}")

        record = self._pending_history_record(pending_key)
        if not record:
            logger.warning(f"网盘文件后处理缺少对应历史记录：{pending_key}")
            return None, {}
        try:
            source_name = str(
                record.get("source_file_name") or record.get("file_name") or ""
            )
            context = self._resolve_history_retry_context(record, source_name)
            mediainfo = context["mediainfo"]
            subscribe = context.get("subscribe")
            episode = context.get("episode")
            item["subscribe_id"] = getattr(subscribe, "id", None)
            item["success_episodes"] = (
                [episode]
                if mediainfo.type == MediaType.TV and episode
                else [1]
            )
            if mediainfo.type == MediaType.TV:
                item["season"] = max(1, int(context.get("season") or 1))
                item["notification_episodes"] = [episode] if episode else []
            media_data = self._serialize_mediainfo(mediainfo)
            item["mediainfo"] = media_data
            logger.debug(
                f"已从历史记录恢复网盘文件后处理媒体信息：{mediainfo.title_year}"
            )
            return mediainfo, media_data
        except Exception as error:
            logger.warning(f"恢复网盘文件后处理媒体信息失败：{error}")
            return None, {}

    def _notify_pending_file_finalized(
            self,
            item: Dict[str, Any],
            pending_key: str,
            strm_path: Optional[Path],
            mediainfo: Optional[MediaInfo] = None,
            media_data: Optional[Dict[str, Any]] = None,
            finish_subscription: bool = True,
            subscribe_cache: Optional[Dict[int, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        if mediainfo is None:
            mediainfo, restored_data = self._restore_pending_media_context(
                item, pending_key
            )
            media_data = media_data or restored_data
        else:
            media_data = media_data or self._serialize_mediainfo(mediainfo)
        notify_path = strm_path
        if not notify_path and mediainfo and self._local_resource_path:
            subscribe = None
            subscribe_id = int(item.get("subscribe_id") or 0)
            if subscribe_id:
                if subscribe_cache is not None and subscribe_id in subscribe_cache:
                    subscribe = subscribe_cache[subscribe_id]
                else:
                    try:
                        subscribe = SubscribeOper().get(subscribe_id)
                    except Exception as error:
                        logger.debug(f"读取后处理订阅失败：{subscribe_id}，{error}")
            target_subscribe = subscribe or SimpleNamespace(
                name=mediainfo.title,
                year=mediainfo.year,
                media_category=None,
            )
            notify_path = self._resolve_resource_season_dir(
                self._local_resource_path,
                target_subscribe,
                mediainfo,
                max(1, int(item.get("season") or 1)),
            )
        if notify_path and mediainfo:
            scheduled = self._media_server_notifier.notify(
                path=notify_path,
                mediainfo=mediainfo,
                file_name=str(item.get("file_name") or ""),
            )
            if scheduled:
                logger.debug(
                    f"已加入媒体目录通知批次：{item.get('file_name') or Path(notify_path).name}"
                )
        elif strm_path:
            logger.warning(
                f"STRM 已生成但缺少媒体信息，无法发送入库通知：{strm_path}"
            )

        if finish_subscription and not item.get("transient_target"):
            self._finish_pending_subscription(
                item, media_data or {}, mediainfo=mediainfo
            )
        if not mediainfo:
            logger.warning("文件已完成，但缺少媒体信息，无法发送完成通知和Webhook")
            return None
        history_record = self._pending_history_record(pending_key) or {}
        detail = {
            "type": "电视剧" if mediainfo.type == MediaType.TV else "电影",
            "title": mediainfo.title,
            "year": mediainfo.year,
            "image": getattr(mediainfo, "get_poster_image", lambda: None)(),
            "file_name": item.get("file_name"),
            "notification_kind": (
                "upgrade"
                if self._is_upgrade_history(history_record)
                else "cross_transfer"
                if history_record.get("transfer_mode") == "cross"
                else "transfer"
            ),
        }
        episodes = []
        episode_values = (
                item.get("notification_episodes")
                or item.get("success_episodes")
                or (
                    [history_record.get("episode")]
                    if history_record.get("episode")
                    else []
                )
        )
        for value in episode_values:
            try:
                episode = int(str(value or "0"))
            except (TypeError, ValueError):
                continue
            if episode > 0:
                episodes.append(episode)
        if mediainfo.type == MediaType.TV:
            detail["season"] = max(
                1,
                int(
                    item.get("season")
                    or history_record.get("season")
                    or getattr(mediainfo, "season", 0)
                    or 1
                ),
            )
            detail["episodes"] = episodes
        return detail

    @staticmethod
    def _aggregate_transfer_details(
            transfer_details: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """按电视剧标题、年份和季聚合延迟完成的文件。"""
        aggregated: List[Dict[str, Any]] = []
        tv_index: Dict[Tuple[str, str, int, str], Dict[str, Any]] = {}
        episode_sets: Dict[Tuple[str, str, int, str], Set[int]] = {}
        file_name_sets: Dict[Tuple[str, str, int, str], Set[str]] = {}
        for raw_detail in transfer_details:
            detail = copy.deepcopy(raw_detail)
            if detail.get("type") != "电视剧":
                aggregated.append(detail)
                continue
            season = max(1, int(detail.get("season") or 1))
            detail["season"] = season
            detail["episodes"] = sorted(
                {
                    int(episode)
                    for episode in (detail.get("episodes") or [])
                    if int(episode) > 0
                }
            )
            key = (
                str(detail.get("title") or ""),
                str(detail.get("year") or ""),
                season,
                str(detail.get("notification_kind") or "transfer"),
            )
            current = tv_index.get(key)
            if current is None:
                file_name = str(detail.get("file_name") or "").strip()
                if file_name:
                    detail["file_names"] = [file_name]
                    file_name_sets[key] = {file_name}
                episode_sets[key] = set(detail["episodes"])
                tv_index[key] = detail
                aggregated.append(detail)
                continue
            episode_sets[key].update(detail["episodes"])
            file_name = str(detail.get("file_name") or "").strip()
            if file_name:
                file_names = current.setdefault("file_names", [])
                known_names = file_name_sets.setdefault(key, set(file_names))
                if file_name not in known_names:
                    known_names.add(file_name)
                    file_names.append(file_name)
            if not current.get("image") and detail.get("image"):
                current["image"] = detail["image"]
        for key, detail in tv_index.items():
            detail["episodes"] = sorted(episode_sets[key])
        return aggregated

    def _send_finalized_batch(
            self, transfer_details: List[Dict[str, Any]]
    ) -> None:
        if not transfer_details:
            return
        aggregated = self._aggregate_transfer_details(transfer_details)
        total_count = len(transfer_details)
        if self._notify:
            try:
                self.send_transfer_notification(aggregated, total_count)
            except Exception as error:
                logger.warning(f"文件完成通知入队失败：{error}")
        if self._file_finalized:
            try:
                self._file_finalized(aggregated, total_count)
            except Exception as error:
                logger.warning(f"文件完成汇总 Webhook 发送失败：{error}")
        logger.debug(
            f"网盘文件后处理完成通知已入队：{total_count} 个文件，"
            f"{len(aggregated)} 个媒体项"
        )

    def _mark_offline_history_status(
            self, pending_key: str, status: str, reason: str = ""
    ) -> None:
        self._mark_offline_history_status_batch({pending_key}, status, reason)

    def _mark_offline_history_status_batch(
            self, pending_keys: Set[str], status: str, reason: str = ""
    ) -> None:
        """按 pending_key / share_url 更新对应历史记录的状态。

        v1.5.14：新增**定向 UPDATE 快路径**。历史实现是
        ``_get_data("history")`` + ``_save_data("history", ...)`` —— 一次状态
        变更要拉出全部历史行、逐字段深比较、再整体写回；而本方法由后处理监控
        在 ``_offline_pending_lock`` **锁内**调用，历史行数一多就把监控器、
        前端刷新 API、后续调度 tick 全部堵在锁上，表现为「后处理长期卡住
        + 页面很慢」。数据层支持定向更新时走快路径，否则回退原语义。
        """
        if not self._get_data or not self._save_data:
            return
        normalized_keys = {
            str(value or "").strip() for value in pending_keys
            if str(value or "").strip()
        }
        if not normalized_keys:
            return

        # 快路径：数据层支持定向更新时，只 UPDATE 命中行（O(命中行数)）。
        store = None
        getter = getattr(self, "_get_data_store", None)
        if callable(getter):
            try:
                store = getter()
            except Exception:
                store = None
        if store is None:
            store = getattr(self, "_data_store", None)
        updater = getattr(store, "update_history_status_by_finalize_keys", None)
        if callable(updater):
            try:
                platform_records = updater(normalized_keys, status, reason)
            except Exception as error:
                logger.warning(
                    f"历史状态定向更新失败，回退全量重写：{error}"
                )
                platform_records = None
            if platform_records is not None:
                self._record_platform_transfer_histories(platform_records)
                if platform_records and self._history_changed:
                    self._history_changed()
                return

        uppercase_keys = {value.upper() for value in normalized_keys}
        platform_records = []
        with self._offline_pending_lock:
            history = self._get_data("history") or []
            changed = False
            for item in history:
                link = str(item.get("share_url") or "").upper()
                item_key = str(item.get("finalize_key") or "")
                if (
                        item_key not in normalized_keys
                        and not any(value in link for value in uppercase_keys)
                ):
                    continue
                item["status"] = status
                item.pop("finalize_key", None)
                if status == "失败":
                    # 失败终态必须携带原因：优先本次原因，退回历史已有原因，
                    # 兜底占位文案，绝不允许失败记录无原因（v1.5.3 T3）。
                    item["failure_reason"] = (
                            str(reason or "").strip()
                            or str(item.get("failure_reason") or "").strip()
                            or "未提供失败原因"
                    )
                elif reason:
                    item["failure_reason"] = reason
                else:
                    item.pop("failure_reason", None)
                if status == "成功":
                    platform_records.append(copy.deepcopy(item))
                changed = True
            if changed:
                self._save_data("history", history)
        self._record_platform_transfer_histories(platform_records)
        if changed and self._history_changed:
            self._history_changed()

    def get_pending_finalize_tasks(self) -> List[Dict[str, Any]]:
        """返回等待115文件就绪、重命名或生成STRM的持久任务。"""
        if not self._get_data:
            return []
        with self._offline_pending_lock:
            pending = self._get_data(self._OFFLINE_PENDING_KEY) or {}
            return [copy.deepcopy({**item, "pending_key": key}) for key, item in pending.items()]

    def count_pending_finalize_tasks(self, fallback: int = 0) -> int:
        """非阻塞地统计待后处理任务数量（v1.5.14）。

        ``get_pending_finalize_tasks`` 需要 ``_offline_pending_lock``，而后处理
        监控可能长时间持有该锁（含网盘 IO）。监控器与前端 API 在拿不到锁时
        若仍走该路径，就会把线程一起挂住、直至 APScheduler 线程池耗尽。
        这里改为「拿不到锁就返回调用方已知的计数」，只用于日志与展示。
        """
        if not self._get_data:
            return 0
        if not self._offline_pending_lock.acquire(blocking=False):
            return int(fallback or 0)
        try:
            pending = self._get_data(self._OFFLINE_PENDING_KEY) or {}
            return len(pending)
        finally:
            self._offline_pending_lock.release()

    def delete_pending_finalize_tasks(
            self,
            pending_keys: Set[str],
            task_ids: Optional[Set[str]] = None,
    ) -> int:
        """删除用户明确取消的待后处理任务，并结束对应历史状态。"""
        removed_keys, pending_count = self._remove_pending_finalize_tasks(
            pending_keys,
            task_ids=task_ids,
        )
        if not removed_keys:
            return 0
        reason = "后处理任务已由用户删除"
        logger.warning(
            f"离线后处理任务被用户删除，对应历史标记失败：{len(removed_keys)} 项，原因：{reason}"
        )
        self._mark_offline_history_status_batch(removed_keys, "失败", reason)
        self._notify_offline_pending_changed(pending_count)
        return len(removed_keys)

    def stop_pending_finalize_tasks(self, pending_keys: Set[str]) -> int:
        """安全停止后移除尚未提交的后处理任务，并记录准确的停止原因。"""
        removed_keys, pending_count = self._remove_pending_finalize_tasks(
            pending_keys
        )
        if not removed_keys:
            return 0
        reason = "后处理任务已由用户停止"
        logger.warning(
            f"离线后处理任务被用户停止，对应历史标记失败：{len(removed_keys)} 项，原因：{reason}"
        )
        self._mark_offline_history_status_batch(removed_keys, "失败", reason)
        self._notify_offline_pending_changed(pending_count)
        return len(removed_keys)

    def _remove_pending_finalize_tasks(
            self,
            pending_keys: Set[str],
            task_ids: Optional[Set[str]] = None,
    ) -> Tuple[Set[str], int]:
        """仅移除待后处理状态；调用方负责处理历史记录和状态通知。"""
        keys = {str(value or "").strip() for value in pending_keys if str(value or "").strip()}
        normalized_task_ids = {
            str(value or "").strip().upper()
            for value in (task_ids or set())
            if str(value or "").strip()
        }
        if (not keys and not normalized_task_ids) or not self._get_data:
            return set(), 0
        with self._offline_pending_lock:
            pending = self._get_data(self._OFFLINE_PENDING_KEY) or {}
            removed_keys = {
                key for key, item in pending.items()
                if key in keys
                   or str((item or {}).get("task_id") or "").strip().upper()
                   in normalized_task_ids
            }
            if not removed_keys:
                return set(), len(pending)
            for key in removed_keys:
                pending.pop(key, None)
            self._save_offline_pending(pending)
            return removed_keys, len(pending)

    @staticmethod
    def _history_record_matches(
            record: Dict[str, Any], identity: Dict[str, Any]
    ) -> bool:
        for key in (
                "time",
                "share_url",
                "file_name",
                "tmdb_id",
                "season",
                "episode",
        ):
            expected = identity.get(key)
            if expected in (None, "") and key not in {"time", "file_name"}:
                continue
            if str(record.get(key) or "") != str(expected or ""):
                return False
        return True

    @staticmethod
    def _history_record_deletable(record: Dict[str, Any]) -> bool:
        return (
                str(record.get("status") or "") in {"成功", "失败"}
                or bool(record.get("finalize_key"))
        )

    @staticmethod
    def _transient_target_defaults() -> Dict[str, Any]:
        return {
            "id": 0,
            "doubanid": None,
            "bangumiid": None,
            "anilistid": None,
            "media_source": None,
            "media_id": None,
            "media_category": None,
            "episode_group": None,
            "filter_groups": None,
            "sites": None,
            "include": None,
            "exclude": None,
            "note": [],
            "episode_priority": {},
            "best_version": False,
            "state": "N",
        }

    def build_transient_media_target(
            self,
            media: Dict[str, Any],
            target_id: int = -1,
            episodes: Optional[Set[int]] = None,
            manual_upgrade: bool = False,
            manual_baseline: Optional[Dict[str, Any]] = None,
    ) -> Any:
        """将订阅外媒体转换为现有同步链可消费的临时目标。"""
        media_type = (
            MediaType.TV
            if str(media.get("media_type") or media.get("type") or "").strip().lower()
               in {"tv", MediaType.TV.value.lower()}
            else MediaType.MOVIE
        )
        title = str(media.get("title") or media.get("name") or "").strip()
        tmdb_id = self._positive_int(media.get("tmdb_id") or media.get("tmdbid"))
        if not title or not tmdb_id:
            raise ValueError("媒体目标缺少标题或 TMDB ID")

        season = max(1, int(media.get("season") or 1)) if media_type == MediaType.TV else None
        selected_episodes = sorted({
            int(value)
            for value in (
                episodes if episodes is not None else media.get("episodes") or []
            )
            if int(value) > 0
        })

        target_data = self._transient_target_defaults()
        target_data.update({
            "id": int(target_id),
            "name": title,
            "year": media.get("year"),
            "type": media_type.value,
            "tmdbid": tmdb_id,
            "doubanid": media.get("douban_id") or media.get("doubanid"),
            "bangumiid": media.get("bangumi_id") or media.get("bangumiid"),
            "season": season,
            "start_episode": min(selected_episodes) if selected_episodes else 1,
            "total_episode": max(selected_episodes) if selected_episodes else 0,
            "lack_episode": len(selected_episodes) if media_type == MediaType.TV else 0,
            "episode_group": media.get("episode_group"),
            "_transient_target": True,
            "_manual_upgrade": bool(manual_upgrade),
            "_target_episodes": set(selected_episodes),
            "_manual_media_baseline": dict(manual_baseline or {}),
        })
        return SimpleNamespace(**target_data)

    def _history_upgrade_records(
            self, identities: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        if not self._get_data:
            raise RuntimeError("历史记录存储未初始化")
        records = self._get_data("history") or []
        target_ids = {
            str(identity.get("record_id") or "").strip()
            for identity in identities
            if str(identity.get("record_id") or "").strip()
        }
        selected = []
        for record in records:
            record_id = self._ensure_history_record_id(record)
            if record_id in target_ids or any(
                    self._history_record_matches(record, identity)
                    for identity in identities
            ):
                if str(record.get("status") or "") == "成功" and not record.get("finalize_key"):
                    selected.append(copy.deepcopy(record))
        if not selected:
            raise LookupError("所选历史记录不存在或没有可洗版的成功内容")
        return selected

    def resolve_history_upgrade_targets(
            self, identities: List[Dict[str, Any]]
    ) -> List[Any]:
        records = self._history_upgrade_records(identities)
        grouped: Dict[Tuple[str, str, int], List[Dict[str, Any]]] = {}
        for record in records:
            media_type = str(record.get("type") or "")
            if media_type not in {MediaType.TV.value, MediaType.MOVIE.value}:
                continue
            media_key = str(record.get("tmdb_id") or "").strip() or (
                f"{record.get('title') or ''}|{record.get('year') or ''}"
            )
            season = max(1, int(record.get("season") or 1)) if media_type == MediaType.TV.value else 0
            grouped.setdefault((media_type, media_key, season), []).append(record)

        targets = []
        for index, ((media_type, _, season), group) in enumerate(grouped.items(), start=1):
            sample = group[0]
            episodes = {
                episode
                for record in group
                for episode in self._platform_episode_numbers(record)
            }
            target = self.build_transient_media_target(
                {
                    "title": sample.get("title"),
                    "year": sample.get("year"),
                    "media_type": media_type,
                    "tmdb_id": sample.get("tmdb_id"),
                    "douban_id": sample.get("douban_id"),
                    "season": season or None,
                    "episode_group": sample.get("episode_group"),
                },
                target_id=-index,
                episodes=episodes,
                manual_upgrade=True,
            )
            targets.append(target)
        if not targets:
            raise ValueError("所选记录缺少可识别的电影或电视剧媒体信息")
        return targets

    def _media_server_services(self) -> Dict[str, Any]:
        services = MediaServerHelper().get_services() or {}
        return {
            name: service for name, service in services.items()
            if service and not service.instance.is_inactive()
        }

    @staticmethod
    def _media_server_storage_name(name: str, service: Any) -> str:
        """返回媒体库同步表使用的服务器类型标识。"""
        return str(getattr(service, "type", "") or name or "").strip().casefold()

    def _configured_media_path(self, value: Any) -> Tuple[Path, str]:
        """校验媒体服务器实际路径属于插件配置的 STRM 媒体根目录。"""
        if not self._local_resource_path:
            raise RuntimeError("未配置本地或挂载媒体根路径")
        root = Path(self._local_resource_path).expanduser().resolve(strict=False)
        if not root.is_dir():
            raise RuntimeError("插件配置的媒体根路径不存在或不可访问")
        path = Path(str(value or "").strip()).expanduser().resolve(strict=False)
        if not str(value or "").strip() or (path != root and root not in path.parents):
            raise ValueError("媒体服务器条目路径不在插件配置的媒体根路径中")
        if not path.exists():
            raise LookupError(f"媒体服务器条目路径不存在或不可访问：{path}")
        return path, path.relative_to(root).as_posix()

    @staticmethod
    def _media_server_item_type(value: Any) -> Optional[MediaType]:
        text = str(value or "").strip().lower()
        if text in {"电影", "movie"}:
            return MediaType.MOVIE
        if text in {"电视剧", "tv", "series", "show"}:
            return MediaType.TV
        return None

    def list_media_server_content(
            self,
            server: str = "",
            keyword: str = "",
            tmdb_id: Any = None,
            media_type: str = "",
            limit: int = 500,
    ) -> Dict[str, Any]:
        """在指定媒体服务器内按标题或 TMDB ID 搜索可洗版内容。"""
        services = self._media_server_services()
        server_options = [
            {
                "title": f"{name}（{str(getattr(service, 'type', '') or '媒体服务器').upper()}）",
                "value": name,
            }
            for name, service in sorted(services.items(), key=lambda item: item[0].casefold())
        ]
        selected_server = str(server or "").strip()
        query_text = str(keyword or "").strip()
        target_tmdb_id = self._positive_int(tmdb_id)
        target_media_type = str(media_type or "").strip().lower()
        if not selected_server:
            return {"servers": server_options, "items": []}
        if selected_server not in services:
            raise ValueError("所选媒体服务器不存在或未启用")
        if not query_text and not target_tmdb_id:
            return {"servers": server_options, "items": []}

        storage_server = self._media_server_storage_name(
            selected_server, services[selected_server]
        )
        row_limit = max(1, min(int(limit or 500), 1000))
        with SessionFactory() as db:
            query = db.query(MediaServerItem).filter(
                func.lower(MediaServerItem.server) == storage_server
            )
            if target_tmdb_id:
                query = query.filter(*media_server_tmdb_filters(
                    MediaServerItem, [target_tmdb_id]
                ))
            else:
                query = query.filter(or_(
                    MediaServerItem.title.ilike(f"%{query_text}%"),
                    MediaServerItem.original_title.ilike(f"%{query_text}%"),
                ))
            rows = query.order_by(
                MediaServerItem.title, MediaServerItem.year
            ).limit(100).all()

            items = []
            for row in rows:
                media_type = self._media_server_item_type(row.item_type)
                row_tmdb_id = tmdb_id_of(row)
                if not media_type or not row_tmdb_id or not row.path:
                    continue
                if target_media_type in {"movie", "tv"} and (
                        (target_media_type == "movie") != (media_type == MediaType.MOVIE)
                ):
                    continue
                try:
                    _, relative = self._configured_media_path(row.path)
                except (ValueError, LookupError, RuntimeError):
                    continue
                base = {
                    "server": selected_server,
                    "item_id": str(row.item_id),
                    "title": str(row.title or "未知媒体"),
                    "year": row.year,
                    "tmdb_id": row_tmdb_id,
                    "media_type": "movie" if media_type == MediaType.MOVIE else "tv",
                    "path": relative,
                }
                if media_type == MediaType.MOVIE:
                    items.append({
                        **base,
                        "kind": "movie",
                        "label": f"{base['title']}{f' ({row.year})' if row.year else ''} · 电影 · {relative}",
                    })
                    continue
                seasoninfo = row.seasoninfo or {}
                for season_value, episode_values in seasoninfo.items():
                    try:
                        season = int(season_value)
                    except (TypeError, ValueError):
                        continue
                    episodes = sorted({
                        episode for value in (episode_values or [])
                        if (episode := self._positive_int(value))
                    })
                    if not episodes:
                        continue
                    items.append({
                        **base,
                        "kind": "season",
                        "season": season,
                        "label": f"{base['title']} S{season:02d} · 整季（{len(episodes)} 集）· {relative}",
                    })
                    for episode in episodes:
                        items.append({
                            **base,
                            "kind": "episode",
                            "season": season,
                            "episode": episode,
                            "label": f"{base['title']} S{season:02d}E{episode:02d} · {relative}",
                        })
                    if len(items) >= row_limit:
                        break
                if len(items) >= row_limit:
                    break
        return {"servers": server_options, "items": items[:row_limit]}

    def _media_server_episode_files(
            self, server: str, item_id: str, season: int
    ) -> Dict[int, Dict[str, Any]]:
        chain = MediaServerChain()
        episode_ids = chain.get_season_episode_ids(
            server=server, item_id=item_id, season=season
        )
        result = {}
        for episode, episode_id in (episode_ids or {}).items():
            info = chain.iteminfo(server=server, item_id=episode_id)
            if not info:
                raise LookupError(
                    f"无法读取媒体服务器剧集详情：{server} S{season:02d}E{int(episode):02d}"
                )
            path, relative = self._configured_media_path(getattr(info, "path", ""))
            result[int(episode)] = {
                "file_name": path.name,
                "file_size": path.stat().st_size if path.is_file() and path.suffix.lower() != ".strm" else 0,
                "path": relative,
                "source": f"媒体服务器 {server}",
            }
        return result

    def resolve_media_server_upgrade_targets(
            self, selections: List[Dict[str, Any]]
    ) -> List[Any]:
        """二次校验媒体服务器条目和实际路径，并按媒体、季聚合洗版目标。"""
        services = self._media_server_services()
        grouped: Dict[Tuple[str, int, int], Dict[str, Any]] = {}
        episode_cache: Dict[Tuple[str, str, int], Dict[int, Dict[str, Any]]] = {}
        seen = set()
        with SessionFactory() as db:
            normalized_selections = []
            rows_by_server: Dict[str, set[str]] = {}
            for selection in selections[:200]:
                server = str(selection.get("server") or "").strip()
                item_id = str(selection.get("item_id") or "").strip()
                kind = str(selection.get("kind") or "").strip().lower()
                identity = (server, item_id, kind, selection.get("season"), selection.get("episode"))
                if identity in seen:
                    continue
                seen.add(identity)
                if server not in services or not item_id:
                    raise ValueError("所选媒体服务器不存在、未启用或条目无效")
                storage_server = self._media_server_storage_name(
                    server, services[server]
                )
                normalized_selections.append((
                    selection, server, item_id, kind, storage_server,
                ))
                rows_by_server.setdefault(storage_server, set()).add(item_id)

            rows_by_key = {}
            for storage_server, item_ids in rows_by_server.items():
                rows = db.query(MediaServerItem).filter(
                    func.lower(MediaServerItem.server) == storage_server,
                    MediaServerItem.item_id.in_(item_ids),
                ).all()
                rows_by_key.update({
                    (storage_server, str(row.item_id)): row for row in rows
                })

            for selection, server, item_id, kind, storage_server in normalized_selections:
                row = rows_by_key.get((storage_server, item_id))
                row_tmdb_id = tmdb_id_of(row)
                if not row or not row_tmdb_id:
                    raise LookupError("所选媒体库内容已不存在或缺少 TMDB ID")
                media_type = self._media_server_item_type(row.item_type)
                if not media_type:
                    raise ValueError("所选媒体库内容不是电影或电视剧")
                media_path, relative = self._configured_media_path(row.path)
                if media_type == MediaType.MOVIE:
                    if kind != "movie":
                        raise ValueError("电影媒体库条目类型错误")
                    if not media_path.is_file():
                        raise LookupError("所选电影的媒体服务器路径不是可访问文件")
                    key = (media_type.value, row_tmdb_id, 0)
                    grouped.setdefault(key, {
                        "row": row,
                        "season": 0,
                        "episodes": set(),
                        "baseline": {"movie": {
                            "file_name": media_path.name,
                            "file_size": media_path.stat().st_size if media_path.is_file() and media_path.suffix.lower() != ".strm" else 0,
                            "path": relative,
                            "source": f"媒体服务器 {server}",
                        }},
                    })
                    continue

                try:
                    season = max(1, int(selection.get("season") or 0))
                except (TypeError, ValueError):
                    raise ValueError("电视剧季号无效")
                cache_key = (server, item_id, season)
                if cache_key not in episode_cache:
                    episode_cache[cache_key] = self._media_server_episode_files(
                        server, item_id, season
                    )
                available = episode_cache[cache_key]
                if kind == "season":
                    selected_episodes = set(available)
                elif kind == "episode":
                    episode = int(selection.get("episode") or 0)
                    if episode <= 0 or episode not in available:
                        raise LookupError("所选媒体库剧集已不存在")
                    selected_episodes = {episode}
                else:
                    raise ValueError("电视剧媒体库条目类型错误")
                if not selected_episodes:
                    raise LookupError("所选媒体库季没有可洗版剧集")
                key = (media_type.value, row_tmdb_id, season)
                group = grouped.setdefault(key, {
                    "row": row,
                    "season": season,
                    "episodes": set(),
                    "baseline": {"episodes": {}},
                })
                group["episodes"].update(selected_episodes)
                group["baseline"]["episodes"].update({
                    episode: available[episode] for episode in selected_episodes
                })

        targets = []
        for index, group in enumerate(grouped.values(), start=1):
            row = group["row"]
            media_type = self._media_server_item_type(row.item_type)
            targets.append(self.build_transient_media_target(
                {
                    "title": row.title,
                    "year": row.year,
                    "media_type": media_type.value,
                    "tmdb_id": tmdb_id_of(row),
                    "season": group["season"] or None,
                },
                target_id=-index,
                episodes=group["episodes"],
                manual_upgrade=True,
                manual_baseline=group["baseline"],
            ))
        if not targets:
            raise ValueError("请选择至少一个符合插件媒体路径配置的媒体库内容")
        return targets

    def delete_history_record(
            self, identity: Dict[str, Any], delete_linked_files: bool = False
    ) -> Dict[str, Any]:
        """删除一条终态或后处理历史；显式请求时联动删除115文件和STRM。"""
        if not self._get_data or not self._save_data:
            raise RuntimeError("历史记录存储未初始化")
        if not str(identity.get("time") or "").strip() or not str(
                identity.get("file_name") or ""
        ).strip():
            raise ValueError("历史记录标识不完整")
        pending_count = 0
        pending_removed = False
        deleted = None
        with self._offline_pending_lock:
            history = self._get_data("history") or []
            for index, record in enumerate(history):
                if not self._history_record_matches(record, identity):
                    continue
                if not self._history_record_deletable(record):
                    raise RuntimeError(
                        f"{record.get('status') or '当前'}状态仍在处理，不能删除历史记录"
                    )
                deleted = copy.deepcopy(record)
                finalize_key = str(deleted.get("finalize_key") or "").strip()
                if finalize_key:
                    removed_keys, pending_count = self._remove_pending_finalize_tasks(
                        {finalize_key}
                    )
                    pending_removed = bool(removed_keys)
                linked_result = None
                if delete_linked_files:
                    linked_result = self._delete_history_linked_files(deleted)
                    self._refresh_deleted_media([deleted])
                deleted["cache_deleted"] = self._delete_history_cache(deleted)
                history.pop(index)
                if delete_linked_files:
                    self._refresh_deleted_subscribe_notes([deleted], history)
                self._save_data("history", history)
                if linked_result:
                    deleted["linked_delete"] = linked_result
                break
        if deleted is None:
            raise LookupError("历史记录不存在或状态已发生变化，请刷新后重试")
        if pending_removed:
            self._notify_offline_pending_changed(pending_count)
        self._delete_platform_transfer_histories([deleted])
        return deleted

    def delete_history_records(
            self,
            identities: List[Dict[str, Any]],
            delete_linked_files: bool = False,
    ) -> Dict[str, int]:
        """批量删除终态或后处理历史，仅进行一次读取和持久化。"""
        if not self._get_data or not self._save_data:
            raise RuntimeError("历史记录存储未初始化")
        target_ids = {
            str(identity.get("record_id") or "").strip()
            for identity in identities
            if str(identity.get("record_id") or "").strip()
        }
        target_identities = {
            identity_key
            for identity in identities
            if (identity_key := self._history_record_identity(identity))
        }
        if not target_ids and not target_identities:
            raise ValueError("未提供有效的历史记录标识")

        pending_count = 0
        pending_removed = False
        linked_stats: Dict[str, int] = {}
        with self._offline_pending_lock:
            history = self._get_data("history") or []
            selected_indexes = []
            for index, record in enumerate(history):
                record_id = self._ensure_history_record_id(record)
                identity_key = self._history_record_identity(record)
                if record_id in target_ids or identity_key in target_identities:
                    selected_indexes.append(index)

            if not selected_indexes:
                raise LookupError("所选历史记录不存在或状态已发生变化，请刷新后重试")

            deleted_indexes = set()
            linked_deleted = 0
            skipped = 0
            deletable_records = []
            for index in selected_indexes:
                record = history[index]
                if not self._history_record_deletable(record):
                    skipped += 1
                    continue
                deletable_records.append(record)
                deleted_indexes.add(index)

            finalize_keys = {
                str(record.get("finalize_key") or "").strip()
                for record in deletable_records
                if str(record.get("finalize_key") or "").strip()
            }
            if finalize_keys:
                removed_keys, pending_count = self._remove_pending_finalize_tasks(
                    finalize_keys
                )
                pending_removed = bool(removed_keys)

            if delete_linked_files and deletable_records:
                linked_stats = self._delete_history_linked_files_batch(
                    deletable_records
                )
                self._refresh_deleted_media(deletable_records)
                linked_deleted = len(deletable_records)

            cache_deleted = sum(
                self._delete_history_cache(record)
                for record in deletable_records
            )

            if deleted_indexes:
                history = [
                    record
                    for index, record in enumerate(history)
                    if index not in deleted_indexes
                ]
                if delete_linked_files and deletable_records:
                    self._refresh_deleted_subscribe_notes(
                        deletable_records, history
                    )
                self._save_data("history", history)

        if pending_removed:
            self._notify_offline_pending_changed(pending_count)
        if deleted_indexes:
            self._delete_platform_transfer_histories(deletable_records)

        return {
            "deleted": len(deleted_indexes),
            "linked_deleted": linked_deleted,
            "cache_deleted": cache_deleted,
            "skipped": skipped,
            **linked_stats,
        }

    def _refresh_deleted_media(self, records: List[Dict[str, Any]]) -> None:
        """删除关联文件后，按受影响 STRM 路径刷新媒体库。"""
        for record in records:
            cloud_dir = str(record.get("cloud_dir") or "").strip()
            file_name = str(record.get("file_name") or "").strip()
            if not cloud_dir or not file_name or not self._local_resource_path:
                continue
            try:
                local_path = self._path_mapper.local_path(
                    local_root=self._local_resource_path,
                    cloud_root=self._CLOUD_MEDIA_ROOT,
                    cloud_dir=cloud_dir,
                    file_name=file_name,
                )
                if local_path.exists():
                    logger.warning(
                        f"关联 STRM 仍然存在，跳过媒体库删除通知：{local_path}"
                    )
                    continue
                self._media_server_notifier.notify_deleted_path(local_path, record)
            except Exception as error:
                logger.warning(
                    f"删除历史后刷新媒体库失败：{file_name} - {error}"
                )

    @staticmethod
    def _history_episodes(record: Dict[str, Any]) -> Set[int]:
        values = record.get("episodes") or record.get("notification_episodes")
        if values is None:
            values = [record.get("episode")]
        return {
            int(value) for value in values
            if str(value or "").isdigit() and int(value) > 0
        }

    def _refresh_deleted_subscribe_notes(
            self,
            deleted_records: List[Dict[str, Any]],
            remaining_history: List[Dict[str, Any]],
            log_context: str = "历史删除后",
    ) -> None:
        """按失效记录后的剩余历史修正电视剧订阅 note 和缺集数。"""
        targets: Dict[Tuple[str, int], Set[int]] = {}
        for record in deleted_records:
            tmdb_id = str(record.get("tmdb_id") or "").strip()
            season = int(record.get("season") or 0)
            episodes = self._history_episodes(record)
            if tmdb_id and season > 0 and episodes:
                targets.setdefault((tmdb_id, season), set()).update(episodes)
        if not targets:
            return
        for (tmdb_id, season), deleted_episodes in targets.items():
            remaining_episodes = {
                episode
                for record in remaining_history
                if str(record.get("tmdb_id") or "").strip() == tmdb_id
                   and int(record.get("season") or 0) == season
                   and str(record.get("status") or "") == "成功"
                for episode in self._history_episodes(record)
            }
            for subscribe in list_subscribes_by_tmdb_id(
                    SubscribeOper(), int(tmdb_id), season):
                if str(getattr(subscribe, "type", "")) != MediaType.TV.value:
                    continue
                current_note = {
                    int(value) for value in (getattr(subscribe, "note", None) or [])
                    if str(value).isdigit()
                }
                new_note = sorted(
                    (current_note - deleted_episodes) | remaining_episodes
                )
                if new_note == sorted(current_note):
                    continue
                start = int(getattr(subscribe, "start_episode", 1) or 1)
                total = int(getattr(subscribe, "total_episode", 0) or 0)
                expected = max(0, total - start + 1)
                lack = len(set(range(start, total + 1)) - set(new_note)) if expected else 0
                SubscribeOper().update(
                    subscribe.id,
                    {"note": new_note, "lack_episode": lack},
                )
                logger.info(
                    f"{log_context}更新订阅 note：{subscribe.name}，"
                    f"{sorted(current_note)} -> {new_note}"
                )

    def delete_by_media_server_paths(self, paths: List[str]) -> Dict[str, int]:
        """按媒体服务器 STRM 路径精确匹配并联动删除终态历史。"""
        normalized_paths = set()
        for path in paths:
            normalized = self._normalize_media_server_path(path)
            if normalized:
                normalized_paths.add(normalized)
        if not normalized_paths or not self._get_data:
            return {"matched": 0, "deleted": 0, "linked_deleted": 0,
                    "cache_deleted": 0, "skipped": 0}
        matched = []
        for record in self._get_data("history") or []:
            cloud_dir = str(record.get("cloud_dir") or "").strip()
            file_name = str(record.get("file_name") or "").strip()
            if not cloud_dir or not file_name or not self._local_resource_path:
                continue
            try:
                local_path = self._path_mapper.local_path(
                    local_root=self._local_resource_path,
                    cloud_root=self._CLOUD_MEDIA_ROOT,
                    cloud_dir=cloud_dir,
                    file_name=file_name,
                )
                media_server_path = self._media_server_notifier.media_server_path(
                    local_path
                )
            except Exception as error:
                logger.debug(f"计算深度删除匹配路径失败：{file_name} - {error}")
                continue
            if self._normalize_media_server_path(media_server_path) in normalized_paths:
                matched.append(record)
        if not matched:
            return {"matched": 0, "deleted": 0, "linked_deleted": 0,
                    "cache_deleted": 0, "skipped": 0}
        result = self.delete_history_records(matched, delete_linked_files=True)
        return {"matched": len(matched), **result}

    @staticmethod
    def _normalize_media_server_path(path: Any) -> str:
        """标准化用于精确比较的媒体服务器路径。"""
        value = str(path or "").strip().replace("\\", "/")
        while "//" in value:
            value = value.replace("//", "/")
        return value.rstrip("/")

    def _delete_history_cache(self, record: Dict[str, Any]) -> int:
        if not self._cross_transfer_manager:
            return 0
        cache_key = str(record.get("cache_key") or "").strip()
        if not cache_key:
            return 0
        return self._cross_transfer_manager.delete_cache(cache_key)

    def _delete_history_linked_files_batch(
            self, records: List[Dict[str, Any]]
    ) -> Dict[str, int]:
        """按网盘目录聚合删除；目录内容全部命中时直接删除目录。"""
        preflight = self._preflight_linked_media_directories(records)
        local_handled = preflight["local_handled"]
        cloud_handled = preflight["cloud_handled"]
        grouped: Dict[str, List[Dict[str, Any]]] = {}
        strm_deleted = preflight["strm_deleted"]
        local_directories_deleted = preflight["local_directories_deleted"]
        local_cleanup_targets: Dict[Path, Path] = {}
        for record in records:
            cloud_dir = str(record.get("cloud_dir") or "").strip()
            file_name = str(record.get("file_name") or "").strip()
            record_key = id(record)
            if cloud_dir and file_name and record_key not in cloud_handled:
                grouped.setdefault(cloud_dir, []).append(record)
            if (
                    record_key in local_handled
                    or not self._local_resource_path
                    or not cloud_dir
                    or not file_name
            ):
                continue
            try:
                strm_path = self._path_mapper.local_path(
                    local_root=self._local_resource_path,
                    cloud_root=self._CLOUD_MEDIA_ROOT,
                    cloud_dir=cloud_dir,
                    file_name=file_name,
                )
                if strm_path.is_file():
                    strm_path.unlink()
                    strm_deleted += 1
                self._delete_local_metadata_for_stem(strm_path)
                local_directory = strm_path.parent
                media_directory = (
                    local_directory.parent
                    if self._is_season_directory(cloud_dir, record)
                    else local_directory
                )
                local_cleanup_targets[local_directory] = media_directory
            except Exception as error:
                logger.warning(f"批量删除关联 STRM 失败：{file_name} - {error}")

        for local_directory, media_directory in sorted(
                local_cleanup_targets.items(),
                key=lambda item: len(item[0].parts),
                reverse=True,
        ):
            local_directories_deleted += self._cleanup_local_metadata_directory(
                local_directory
            )
            if media_directory != local_directory:
                local_directories_deleted += self._cleanup_local_metadata_directory(
                    media_directory
                )

        cloud_deleted = preflight["cloud_files_deleted"]
        directories_deleted = preflight["cloud_directories_deleted"]
        for cloud_dir, directory_records in grouped.items():
            try:
                lookup = self._cloud_directories.resolve_directory(cloud_dir)
                if not lookup.checked:
                    raise RuntimeError("无法确认目录状态")
                if not lookup.directory_id:
                    continue
                listing = self._cloud_directories.list_directory(lookup.directory_id)
                if not listing.checked:
                    raise RuntimeError("无法读取目录内容")
                target_names = {
                    str(record.get("file_name") or "").strip()
                    for record in directory_records
                }
                target_stems = {Path(name).stem.lower() for name in target_names}
                target_files = [
                    item for item in listing.files if item.name in target_names
                ]
                remaining_files = [
                    item for item in listing.files if item.name not in target_names
                ]
                if self._cloud_items_are_generated_metadata(
                        remaining_files, target_stems
                ):
                    if self._cloud_mutations.delete_file(lookup.directory_id):
                        cloud_deleted += len(target_files)
                        directories_deleted += 1
                        if self._is_season_directory(
                                cloud_dir, directory_records[0]
                        ):
                            directories_deleted += self._cleanup_cloud_media_parent(
                                cloud_dir
                            )
                    continue
                file_ids = [item.id for item in target_files]
                if not file_ids:
                    continue
                deleted_ids = self._cloud_batch_mutations.delete_files(file_ids)
                cloud_deleted += len(deleted_ids)
            except Exception as error:
                logger.warning(f"批量删除关联网盘内容失败：{cloud_dir} - {error}")

        logger.info(
            f"历史联动批量删除完成：历史记录={len(records)} 条，"
            f"网盘文件={cloud_deleted} 个，网盘目录={directories_deleted} 个，"
            f"STRM={strm_deleted} 个，本地目录={local_directories_deleted} 个"
        )
        return {
            "cloud_files_deleted": cloud_deleted,
            "cloud_directories_deleted": directories_deleted,
            "strm_deleted": strm_deleted,
            "local_directories_deleted": local_directories_deleted,
        }

    def _preflight_linked_media_directories(
            self, records: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """删除前扫描一次STRM；整季或整部命中时直接回收目录。"""
        result = {
            "local_handled": set(),
            "cloud_handled": set(),
            "cloud_files_deleted": 0,
            "cloud_directories_deleted": 0,
            "strm_deleted": 0,
            "local_directories_deleted": 0,
        }
        if not self._local_resource_path:
            return result
        media_groups: Dict[Path, List[Dict[str, Any]]] = {}
        for record in records:
            cloud_dir = str(record.get("cloud_dir") or "").strip()
            file_name = str(record.get("file_name") or "").strip()
            if not cloud_dir or not file_name:
                continue
            try:
                strm_path = self._path_mapper.local_path(
                    local_root=self._local_resource_path,
                    cloud_root=self._CLOUD_MEDIA_ROOT,
                    cloud_dir=cloud_dir,
                    file_name=file_name,
                )
            except Exception:
                continue
            is_season = self._is_season_directory(cloud_dir, record)
            media_directory = strm_path.parent.parent if is_season else strm_path.parent
            cloud_media_dir = str(PurePosixPath(cloud_dir).parent) if is_season else cloud_dir
            media_groups.setdefault(media_directory, []).append({
                "record": record,
                "strm_path": strm_path,
                "season_directory": strm_path.parent if is_season else None,
                "cloud_directory": cloud_dir,
                "cloud_media_directory": cloud_media_dir or "/",
            })

        for media_directory, entries in media_groups.items():
            if not self._is_safe_local_media_directory(media_directory):
                continue
            target_paths = {
                entry["strm_path"].resolve(strict=False) for entry in entries
            }
            current_strms = {
                path.resolve(strict=False) for path in media_directory.rglob("*.strm")
            } if media_directory.is_dir() else set()
            if media_directory.is_dir() and current_strms.issubset(target_paths):
                result["strm_deleted"] += len(current_strms & target_paths)
                cloud_path = entries[0]["cloud_media_directory"]
                if self._delete_cloud_directory_direct(cloud_path):
                    result["cloud_handled"].update(id(entry["record"]) for entry in entries)
                    result["cloud_files_deleted"] += len(entries)
                    result["cloud_directories_deleted"] += 1
                shutil.rmtree(media_directory)
                result["local_handled"].update(id(entry["record"]) for entry in entries)
                result["local_directories_deleted"] += 1
                continue

            season_groups: Dict[Path, List[Dict[str, Any]]] = {}
            for entry in entries:
                season_directory = entry["season_directory"]
                if season_directory:
                    season_groups.setdefault(season_directory, []).append(entry)
            for season_directory, season_entries in season_groups.items():
                if not self._is_safe_local_media_directory(season_directory):
                    continue
                season_targets = {
                    entry["strm_path"].resolve(strict=False) for entry in season_entries
                }
                season_strms = {
                    path.resolve(strict=False) for path in season_directory.glob("*.strm")
                } if season_directory.is_dir() else set()
                if not season_directory.is_dir() or not season_strms.issubset(season_targets):
                    continue
                result["strm_deleted"] += len(season_strms & season_targets)
                cloud_path = season_entries[0]["cloud_directory"]
                if self._delete_cloud_directory_direct(cloud_path):
                    result["cloud_handled"].update(
                        id(entry["record"]) for entry in season_entries
                    )
                    result["cloud_files_deleted"] += len(season_entries)
                    result["cloud_directories_deleted"] += 1
                shutil.rmtree(season_directory)
                result["local_handled"].update(
                    id(entry["record"]) for entry in season_entries
                )
                result["local_directories_deleted"] += 1
        return result

    def _is_safe_local_media_directory(self, directory: Path) -> bool:
        local_root = Path(self._local_resource_path).expanduser().resolve(strict=False)
        resolved = directory.resolve(strict=False)
        if local_root not in resolved.parents:
            return False
        try:
            relative = resolved.relative_to(local_root)
        except ValueError:
            return False
        return len(relative.parts) >= 2

    def _delete_cloud_directory_direct(self, cloud_dir: str) -> bool:
        if len(self._cloud_media_relative_parts(cloud_dir)) < 2:
            return False
        try:
            lookup = self._cloud_directories.resolve_directory(cloud_dir)
            return bool(
                lookup.checked
                and lookup.directory_id
                and self._cloud_mutations.delete_file(lookup.directory_id)
            )
        except Exception as error:
            logger.warning(f"直接删除关联115目录失败，将回退逐文件删除：{cloud_dir} - {error}")
            return False

    @classmethod
    def _is_generated_metadata_name(
            cls, name: str, target_stems: Optional[Set[str]] = None
    ) -> bool:
        path = Path(str(name or ""))
        stem = path.stem.lower()
        suffix = path.suffix.lower()
        target_stems = target_stems or set()
        if suffix == ".nfo":
            return stem in {"tvshow", "season"} or stem in target_stems
        if suffix not in cls._METADATA_IMAGE_SUFFIXES:
            return False
        return bool(
            stem in {"poster", "fanart"}
            or re.fullmatch(r"season\d{2}-poster", stem)
            or stem == "season-specials-poster"
            or any(stem == f"{target_stem}-thumb" for target_stem in target_stems)
        )

    @classmethod
    def _cloud_items_are_generated_metadata(
            cls, items: List[Any], target_stems: Optional[Set[str]] = None
    ) -> bool:
        return all(
            not item.is_directory
            and cls._is_generated_metadata_name(item.name, target_stems)
            for item in items
        )

    @classmethod
    def _is_season_directory(
            cls, cloud_dir: str, record: Dict[str, Any]
    ) -> bool:
        directory_name = PurePosixPath(str(cloud_dir or "/")).name
        return bool(
            cls._SEASON_DIRECTORY_PATTERN.fullmatch(directory_name)
            or record.get("season") not in (None, "")
        )

    @classmethod
    def _delete_local_metadata_for_stem(cls, strm_path: Path) -> int:
        deleted = 0
        candidates = [strm_path.with_suffix(".nfo")]
        candidates.extend(
            item
            for item in strm_path.parent.glob(f"{strm_path.stem}-thumb.*")
            if item.suffix.lower() in cls._METADATA_IMAGE_SUFFIXES
        )
        for candidate in candidates:
            if candidate.is_file():
                candidate.unlink()
                deleted += 1
        return deleted

    def _cleanup_local_metadata_directory(self, directory: Path) -> int:
        if not directory.is_dir():
            return 0
        local_root = Path(self._local_resource_path).expanduser().resolve(strict=False)
        resolved = directory.resolve(strict=False)
        if local_root not in resolved.parents:
            return 0
        try:
            relative = resolved.relative_to(local_root)
        except ValueError:
            return 0
        # 至少保留“分类/媒体”两级边界，绝不清理本地资源根或分类目录。
        if len(relative.parts) < 2:
            return 0
        entries = list(directory.iterdir())
        if any(
                item.is_dir() or not self._is_generated_metadata_name(item.name)
                for item in entries
        ):
            return 0
        for item in entries:
            item.unlink()
        directory.rmdir()
        return 1

    def _cleanup_cloud_media_parent(self, cloud_dir: str) -> int:
        child_path = PurePosixPath(cloud_dir)
        parent_path = str(child_path.parent) or "/"
        if len(self._cloud_media_relative_parts(parent_path)) < 2:
            return 0
        lookup = self._cloud_directories.resolve_directory(parent_path)
        if not lookup.checked or not lookup.directory_id:
            return 0
        listing = self._cloud_directories.list_directory(lookup.directory_id)
        if not listing.checked:
            return 0
        remaining = [
            item for item in listing.files
            if not (item.is_directory and item.name == child_path.name)
        ]
        if not self._cloud_items_are_generated_metadata(remaining):
            return 0
        return int(bool(self._cloud_mutations.delete_file(lookup.directory_id)))

    def _cloud_media_relative_parts(self, path: str) -> Tuple[str, ...]:
        root = PurePosixPath(self._CLOUD_MEDIA_ROOT)
        candidate = PurePosixPath(str(path or "/"))
        try:
            relative = candidate.relative_to(root)
        except ValueError:
            return ()
        return tuple(part for part in relative.parts if part not in {"", ".", "/"})

    def _delete_history_linked_files(
            self, record: Dict[str, Any]
    ) -> Dict[str, Any]:
        """尽力删除115目标文件及本地STRM，任何失败均不阻止历史删除。"""
        file_name = str(record.get("file_name") or "").strip()
        result = self._delete_history_linked_files_batch([record])
        cloud_deleted = (
                result["cloud_files_deleted"] > 0
                or result["cloud_directories_deleted"] > 0
        )
        strm_deleted = result["strm_deleted"] > 0
        return {
            "cloud_file_deleted": cloud_deleted,
            "cloud_file_error": "",
            "strm_deleted": strm_deleted,
            "strm_error": "",
            "strm_path": "",
            "cloud_directories_deleted": result["cloud_directories_deleted"],
            "local_directories_deleted": result["local_directories_deleted"],
        }

    def notify_history_record(self, identity: Dict[str, Any]) -> Dict[str, Any]:
        """按成功历史补发入库和 Webhook 通知，不重复发送转存完成消息。"""
        if not self._get_data:
            raise RuntimeError("历史记录存储未初始化")
        with self._offline_pending_lock:
            history = self._get_data("history") or []
            record = next(
                (
                    item
                    for item in history
                    if self._history_record_matches(item, identity)
                ),
                None,
            )
            if not record:
                raise LookupError("历史记录不存在或状态已发生变化，请刷新后重试")
            if str(record.get("status") or "") != "成功" or record.get(
                    "finalize_key"
            ):
                raise RuntimeError("只有已成功完成的记录才能手动通知")
            record = copy.deepcopy(record)

        title = str(record.get("title") or "").strip()
        year = str(record.get("year") or "").strip()
        summary_title = f"{title}（{year}）" if title and year else title

        source_name = str(
            record.get("source_file_name") or record.get("file_name") or ""
        )
        context = self._resolve_history_retry_context(record, source_name)
        target_subscribe = context["target_subscribe"]
        mediainfo = context["mediainfo"]
        cloud_dir = str(record.get("cloud_dir") or context["cloud_dir"])
        file_name = str(record.get("file_name") or context["target_name"])
        if not self._local_resource_path:
            raise RuntimeError("未配置本地STRM或媒体根目录，无法确定入库通知路径")
        notify_path = self._resolve_resource_season_dir(
            self._local_resource_path,
            target_subscribe,
            mediainfo,
            max(1, int(record.get("season") or 1)),
        )
        if not notify_path:
            raise RuntimeError("无法按媒体分类规则确定入库通知目录")

        notification_name = summary_title or file_name
        scheduled = self._media_server_notifier.notify(
            path=notify_path,
            mediainfo=mediainfo,
            file_name=notification_name,
        )
        if not scheduled:
            raise RuntimeError("入库通知未启用或媒体服务器配置无效")
        detail = {
            "type": "电视剧" if mediainfo.type == MediaType.TV else "电影",
            "title": mediainfo.title,
            "year": mediainfo.year,
            "image": mediainfo.get_poster_image(),
            "file_name": notification_name,
        }
        if mediainfo.type == MediaType.TV:
            detail["season"] = max(1, int(record.get("season") or 1))
            detail["episodes"] = [int(record.get("episode") or 0)]
        if self._file_finalized:
            self._file_finalized([detail], 1)
        logger.info(
            f"已手动补发汇总通知：{notification_name}，入库目录：{notify_path}"
        )
        return {
            "file_name": file_name,
            "summary_title": notification_name,
            "notify_path": str(notify_path),
        }

    def clear_deletable_history(self, force: bool = False) -> Dict[str, int]:
        """默认只清理终态历史；显式强制时清空全部历史展示。"""
        if not self._get_data or not self._save_data:
            raise RuntimeError("历史记录存储未初始化")
        with self._offline_pending_lock:
            history = self._get_data("history") or []
            retained = [] if force else [
                record for record in history
                if not self._history_record_deletable(record)
            ]
            deleted_records = [
                record for record in history if record not in retained
            ]
            deleted_count = len(history) - len(retained)
            if deleted_count:
                self._save_data("history", retained)
        if deleted_count:
            for record in deleted_records:
                self._delete_history_cache(record)
            self._delete_platform_transfer_histories(deleted_records)
        return {"deleted": deleted_count, "retained": len(retained)}

    @staticmethod
    def _find_share_file_for_history(files: List[dict], source_sha1: str, source_name: str) -> Optional[dict]:
        source_hash = re.sub(r"[^0-9A-Fa-f]", "", str(source_sha1 or "")).upper()
        source_name = str(source_name or "").strip()
        leaf_files = []

        def collect(items: List[dict]) -> None:
            for item in items or []:
                if item.get("is_dir"):
                    collect(item.get("children") or [])
                else:
                    leaf_files.append(item)

        collect(files)
        for item in leaf_files:
            item_hash = re.sub(
                r"[^0-9A-Fa-f]", "", str(item.get("sha1") or "")
            ).upper()
            if source_hash and item_hash == source_hash:
                return item
        if source_name:
            matched = next(
                (
                    item
                    for item in leaf_files
                    if str(item.get("name") or "").strip() == source_name
                ),
                None,
            )
            if matched:
                return matched
        return leaf_files[0] if len(leaf_files) == 1 else None

    def retry_history_record(self, record_time: str, share_url: str, file_name: str) -> Dict[str, Any]:
        """按持久化历史中的精确记录重新执行平台命名和完整后处理。"""
        history = (self._get_data("history") or []) if self._get_data else []
        record = next(
            (
                item for item in history
                if str(item.get("time") or "") == str(record_time or "")
                   and str(item.get("share_url") or "") == str(share_url or "")
                   and str(item.get("file_name") or "") == str(file_name or "")
            ),
            None,
        )
        if not record:
            raise ValueError("未找到对应的转存历史记录")
        can_retry, retry_title = self._history_retry_state(record)
        if not can_retry:
            raise ValueError(retry_title)

        source_sha1 = str(record.get("source_sha1") or "").strip()
        source_name = str(
            record.get("source_file_name") or record.get("file_name") or ""
        ).strip()
        canonical_url = str(record.get("share_url") or "").strip()
        if not source_name or not canonical_url:
            raise ValueError("历史记录缺少文件名或资源链接，无法重试")

        retry_context = self._resolve_history_retry_context(record, source_name)
        subscribe = retry_context["subscribe"]
        mediainfo = retry_context["mediainfo"]
        season = retry_context["season"]
        episode = retry_context["episode"]
        cloud_dir = retry_context["cloud_dir"]
        target_name = retry_context["target_name"]

        final_file_exists = bool(
            self._cloud_query.find_file(cloud_dir, target_name, attempts=1)
        )
        success = final_file_exists
        retry_staging_name = source_name
        if not success:
            expected_size = int(record.get("file_size") or 0)
            expected_sha1 = source_sha1.upper()
            for candidate_name in dict.fromkeys((source_name, target_name)):
                staging_file = self._cloud_query.find_file(
                    self._cloud_transfer_path, candidate_name, attempts=1
                )
                if not staging_file:
                    continue
                actual_size = int(getattr(staging_file, "size", 0) or 0)
                actual_sha1 = str(
                    getattr(staging_file, "sha1", "") or ""
                ).upper()
                if expected_sha1 and actual_sha1 != expected_sha1:
                    continue
                if expected_size > 0 and actual_size != expected_size:
                    continue
                retry_staging_name = str(
                    getattr(staging_file, "name", "") or candidate_name
                )
                if not source_sha1 and actual_sha1:
                    source_sha1 = actual_sha1
                    record["source_sha1"] = actual_sha1
                success = True
                logger.info(
                    f"历史恢复复用目标盘暂存文件："
                    f"{self._cloud_transfer_path.rstrip('/')}/{retry_staging_name}"
                )
                break
        cross_source = None
        cached_source = None
        if record.get("transfer_mode") == "cross":
            source_key = str(record.get("source_drive_key") or "").strip()
            target_key = str(record.get("target_drive_key") or "").strip()
            if not source_key or not target_key:
                raise ValueError("跨盘历史缺少源网盘或目标网盘信息")
            if not self._cloud_drive or target_key != self._cloud_drive.key:
                raise ValueError("跨盘历史的目标网盘与当前转存网盘不一致")
            cached_source = CloudFile(
                id="",
                name=source_name,
                is_directory=False,
                size=int(record.get("file_size") or 0),
                sha1=source_sha1,
                md5=str(record.get("source_md5") or ""),
            )
            cache_info = self._cross_transfer_manager.cache_info(
                source_key, cached_source, verify_checksum=True
            ) if self._cross_transfer_manager else {}
            record.update(cache_info)
            if self._save_data:
                self._save_data("history", history)
            if not success and cache_info.get("cache_status") == "complete":
                task = self._cross_transfer_manager.create_from_cloud_file(
                    source_key,
                    cached_source,
                    target_key,
                    self._cloud_transfer_path,
                    source_name,
                )
                success = self._cross_transfer_manager.wait(task["id"])
                if not success:
                    record.update(self._cross_transfer_manager.cache_info(
                        source_key, cached_source, verify_checksum=False,
                    ))
                    record["failure_reason"] = "缓存恢复上传失败"
                    logger.warning(
                        f"缓存恢复上传到目标网盘失败："
                        f"{self._history_failure_label(record)}，"
                        "原因：缓存恢复上传失败"
                    )
                    self._save_data("history", history)
                    raise RuntimeError("缓存完整，但恢复上传到目标网盘失败")
            if not success:
                try:
                    cross_source = self._cloud_drive_registry.get(source_key)
                except KeyError as error:
                    raise ValueError(
                        f"跨盘历史源网盘未就绪：{source_key}"
                    ) from error

        if not success:
            file_id = ""
            if not self._is_ed2k_url(canonical_url):
                share_transfer = (
                    cross_source.require(CloudDriveCapability.SHARE_TRANSFER)
                    if cross_source else self._share_transfer
                )
                status = share_transfer.check_share_status(canonical_url)
                if not status.is_valid:
                    provider_name = cross_source.name if cross_source else "网盘"
                    raise ValueError(f"{provider_name}分享链接无效：{status.status_text}")
                share_files = share_transfer.list_share_files(canonical_url)
                source_file = self._find_share_file_for_history(
                    share_files, source_sha1, source_name
                )
                if not source_file:
                    raise ValueError("原分享中未找到历史记录对应的源文件")
                file_id = str(source_file.get("id") or "")
                if not file_id:
                    raise ValueError("原分享文件缺少文件ID")
                if not source_sha1:
                    source_sha1 = str(source_file.get("sha1") or "")
                source_md5 = str(
                    source_file.get("md5") or record.get("source_md5") or ""
                )
                source_name = str(source_file.get("name") or source_name)
                record["source_file_name"] = source_name
                record["source_sha1"] = source_sha1
                record["source_md5"] = source_md5
                record["file_size"] = int(source_file.get("size") or 0)
                if cached_source:
                    cached_source = CloudFile(
                        id=file_id,
                        name=source_name,
                        is_directory=False,
                        size=int(record.get("file_size") or 0),
                        sha1=source_sha1,
                        md5=source_md5,
                    )
            success = self._transfer_file(
                canonical_url,
                {"id": file_id, "name": source_name,
                 "size": record.get("file_size") or 0,
                 "sha1": source_sha1,
                 "md5": record.get("source_md5") or ""},
                self._cloud_transfer_path, None, source_sha1,
            )

        if not success:
            if cached_source and self._cross_transfer_manager:
                record.update(self._cross_transfer_manager.cache_info(
                    str(record.get("source_drive_key") or ""),
                    cached_source,
                    verify_checksum=False,
                ))
            record["failure_reason"] = "重试转存失败"
            logger.warning(
                f"历史重试转存失败：{self._history_failure_label(record)}，"
                "原因：重试转存失败"
            )
            self._save_data("history", history)
            raise RuntimeError("重试转存失败")

        strm_path, pending_key = self._generate_or_queue_strm(
            canonical_url,
            cloud_dir,
            target_name,
            mediainfo,
            source_sha1=source_sha1,
            file_size=int(record.get("file_size") or 0),
            subscribe_id=getattr(subscribe, "id", None),
            success_episodes=(
                [episode] if mediainfo.type == MediaType.TV and episode else [1]
            ),
            season=season if mediainfo.type == MediaType.TV else None,
            notification_episodes=(
                [episode] if mediainfo.type == MediaType.TV and episode else None
            ),
            staging_dir="" if final_file_exists else self._cloud_transfer_path,
            staging_name=retry_staging_name,
        )
        if not strm_path and not pending_key:
            record["status"] = "失败"
            record["failure_reason"] = "文件已转存但后处理任务登记失败"
            logger.warning(
                f"文件已转存但无法登记后处理任务，历史标记失败："
                f"{self._history_failure_label(record)}，"
                "原因：文件已转存但后处理任务登记失败"
            )
            self._save_data("history", history)
            raise RuntimeError("文件已转存，但无法登记后处理任务")
        record["file_name"] = target_name
        record["cloud_dir"] = cloud_dir
        record["source_file_name"] = source_name
        record["source_sha1"] = source_sha1
        record["tmdb_id"] = mediainfo.tmdb_id
        record.pop("failure_reason", None)
        if cached_source and self._cross_transfer_manager:
            record.update(self._cross_transfer_manager.cache_info(
                str(record.get("source_drive_key") or ""),
                cached_source,
                verify_checksum=False,
            ))
        if pending_key:
            record["finalize_key"] = pending_key
            record["status"] = (
                "下载中" if self._is_ed2k_url(canonical_url) else "处理中"
            )
        else:
            record.pop("finalize_key", None)
            record["status"] = "成功"
            self._media_server_notifier.notify(
                path=strm_path,
                mediainfo=mediainfo,
                file_name=target_name,
            )

        if subscribe and not pending_key:
            success_episodes = [1]
            if mediainfo.type == MediaType.TV:
                success_episodes = [episode] if episode else []
            if success_episodes:
                self._subscribe_handler.check_and_finish_subscribe(
                    subscribe=subscribe,
                    mediainfo=mediainfo,
                    success_episodes=success_episodes,
                )
        if pending_key:
            self.append_history_records([record], reopen_terminal=True)
        else:
            self._save_data("history", history)
        if record["status"] == "成功":
            self._record_platform_transfer_histories([record])
        logger.info(
            f"历史记录后处理完成：{cloud_dir.rstrip('/')}/{target_name}，"
            f"状态：{record['status']}"
        )
        return {
            "status": record["status"],
            "pending_key": pending_key,
            "strm_path": str(strm_path or ""),
            "cloud_dir": cloud_dir,
            "file_name": target_name,
        }

    def _resolve_history_retry_context(
            self, record: Dict[str, Any], source_name: str
    ) -> Dict[str, Any]:
        """按当前订阅和规则还原历史记录的最终处理上下文。"""
        title = str(record.get("title") or "").strip()
        if not title or not source_name:
            raise ValueError("历史记录缺少媒体名称或源文件名")

        media_type = (
            MediaType.TV
            if str(record.get("type") or "") == "电视剧"
            else MediaType.MOVIE
        )
        season = int(record.get("season") or 1) if media_type == MediaType.TV else None
        episode = (
            int(record.get("episode") or 0) or None
            if media_type == MediaType.TV
            else None
        )
        meta = MetaInfo(title)
        meta.type = media_type
        meta.year = record.get("year")
        if season is not None:
            meta.begin_season = season
        if episode is not None:
            meta.begin_episode = episode
        mediainfo = recognize_media(
            self._chain,
            meta=meta,
            mtype=media_type,
            tmdb_id=record.get("tmdb_id"),
            cache=True,
        )
        if not mediainfo:
            raise ValueError(f"无法识别历史记录媒体：{title}")

        subscribe = None
        tmdb_id = mediainfo.tmdb_id or record.get("tmdb_id")
        if tmdb_id:
            try:
                candidates = list_subscribes_by_tmdb_id(
                    SubscribeOper(), tmdb_id, season
                )
                if not candidates and media_type == MediaType.MOVIE:
                    candidates = [
                        item
                        for item in (SubscribeOper().list() or [])
                        if tmdb_id_of(item) == int(tmdb_id)
                    ]
                subscribe = next(
                    (
                        item
                        for item in candidates
                        if str(getattr(item, "type", "")) == media_type.value
                           and (
                                   media_type != MediaType.TV
                                   or int(getattr(item, "season", 1) or 1) == season
                           )
                    ),
                    None,
                )
            except Exception as error:
                logger.warning(f"查询历史记录对应订阅失败：{title}，{error}")

        target_subscribe = subscribe or SimpleNamespace(
            name=title,
            year=record.get("year"),
            media_category=None,
        )
        cloud_dir, target_name = self._platform_target(
            root_path=self._CLOUD_MEDIA_ROOT,
            subscribe=target_subscribe,
            mediainfo=mediainfo,
            source_name=source_name,
            season=season,
            episode=episode,
        )
        return {
            "subscribe": subscribe,
            "target_subscribe": target_subscribe,
            "mediainfo": mediainfo,
            "season": season,
            "episode": episode,
            "cloud_dir": cloud_dir,
            "target_name": target_name,
        }
