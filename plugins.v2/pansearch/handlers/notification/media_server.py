"""转存完成后的媒体服务器入库通知与 Emby 媒体信息提取。"""

import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from pathlib import Path
from threading import RLock, Timer
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Set

from app.chain.mediaserver import MediaServerChain
from app.helper.mediaserver import MediaServerHelper
from app.log import logger
from app.schemas import MediaInfo, RefreshMediaItem
from app.schemas.types import MediaType
from app.utils.http import RequestUtils


class EmbyMediaResolver:
    """读取 Emby 中已入库剧集的实际文件路径，供洗版建立现有版本基线。"""

    @staticmethod
    def _stream_rule_title(path: str, source: Dict[str, Any]) -> str:
        """把 Emby 媒体流详情转换为 MoviePilot 规则可识别的标题。"""
        streams = source.get("MediaStreams") or []
        video = next((
            value for value in streams
            if isinstance(value, dict) and value.get("Type") == "Video"
        ), {})
        audio = next((
            value for value in streams
            if isinstance(value, dict) and value.get("Type") == "Audio"
        ), {})
        values = [Path(str(path or "")).stem]
        values.extend((
            source.get("Container"),
            video.get("DisplayTitle") or video.get("Title"),
            video.get("Codec"),
            video.get("VideoRangeType") or video.get("VideoRange"),
            f"{video.get('BitDepth')}bit" if video.get("BitDepth") else "",
            audio.get("DisplayTitle") or audio.get("Title"),
            audio.get("Codec"),
        ))
        return " ".join(dict.fromkeys(
            str(value).strip() for value in values if str(value or "").strip()
        ))

    @staticmethod
    def _item_media(service, item_id: str) -> Dict[str, Any]:
        """直接读取 Emby 项目详情中的路径和真实媒体大小。"""
        instance = service.instance
        host = str(getattr(instance, "_host", "") or "").rstrip("/")
        api_key = str(getattr(instance, "_apikey", "") or "")
        user = str(getattr(instance, "user", "") or "")
        if not host or not api_key or not user or not item_id:
            return {}
        response = RequestUtils().get_res(
            f"{host}/emby/Users/{user}/Items/{item_id}",
            params={"api_key": api_key},
        )
        if not response or response.status_code != 200:
            return {}
        data = response.json() or {}
        media_sources = data.get("MediaSources") or []
        source = next((value for value in media_sources if isinstance(value, dict)), {})
        source = {
            **data,
            **source,
            "MediaStreams": (
                source.get("MediaStreams") or data.get("MediaStreams") or []
            ),
        }
        try:
            size = max(0, int(data.get("Size") or source.get("Size") or 0))
        except (TypeError, ValueError):
            size = 0
        path = str(data.get("Path") or source.get("Path") or "").strip()
        return {
            "path": path,
            "size": size,
            "item_id": str(item_id),
            "rule_title": EmbyMediaResolver._stream_rule_title(path, source),
            "container": str(source.get("Container") or "").strip(),
            "media_streams": list(source.get("MediaStreams") or []),
        }

    @staticmethod
    def episode_media(
            chain, mediainfo: MediaInfo, season: int
    ) -> tuple[bool, Dict[int, Dict[str, Any]]]:
        """返回 Emby 逐集路径和大小；不读取网盘。"""
        services = MediaServerHelper().get_services(type_filter="emby")
        if not services or not chain or not mediainfo:
            return False, {}

        mediaserver_chain = MediaServerChain()
        result: Dict[int, Dict[str, Any]] = {}
        checked = False
        for server_name, service in services.items():
            if service.instance.is_inactive():
                continue
            try:
                exists_media = chain.media_exists(mediainfo=mediainfo, server=server_name)
                checked = True
                if not exists_media or not exists_media.itemid:
                    continue
                episode_ids = mediaserver_chain.get_season_episode_ids(
                    server=server_name, item_id=exists_media.itemid, season=season
                )
                missing = [
                    (int(episode), str(item_id))
                    for episode, item_id in (episode_ids or {}).items()
                    if int(episode) not in result
                ]
                if missing:
                    with ThreadPoolExecutor(
                            max_workers=min(6, len(missing)),
                            thread_name_prefix="pansearch-emby-baseline",
                    ) as executor:
                        media_items = executor.map(
                            lambda value: (
                                value[0],
                                EmbyMediaResolver._item_media(service, value[1]),
                            ),
                            missing,
                        )
                        result.update(media_items)
            except Exception as error:
                logger.warning(
                    f"读取 Emby 洗版基线失败：{server_name} - "
                    f"{mediainfo.title_year} S{season:02d}，原因：{error}"
                )
        return checked, {episode: value for episode, value in result.items() if value.get("path")}

    @staticmethod
    def episode_snapshot(
            chain, mediainfo: MediaInfo, season: int
    ) -> tuple[bool, Dict[int, str]]:
        """返回是否成功检查过可用 Emby，以及实际存在的剧集路径。"""
        checked, media = EmbyMediaResolver.episode_media(chain, mediainfo, season)
        return checked, {
            episode: str(value.get("path") or "") for episode, value in media.items()
        }

    @staticmethod
    def episode_paths(chain, mediainfo: MediaInfo, season: int) -> Dict[int, str]:
        _, paths = EmbyMediaResolver.episode_snapshot(chain, mediainfo, season)
        return paths

    @staticmethod
    def movie_paths(chain, mediainfo: MediaInfo) -> list[str]:
        """读取 Emby 中已入库电影的实际文件路径，供电影洗版建立基线。"""
        services = MediaServerHelper().get_services(type_filter="emby")
        if not services or not chain or not mediainfo:
            return []

        paths = []
        for server_name, service in services.items():
            if service.instance.is_inactive():
                continue
            try:
                exists_media = chain.media_exists(
                    mediainfo=mediainfo,
                    server=server_name,
                )
                if not exists_media or not exists_media.itemid:
                    continue
                item_path = str(
                    EmbyMediaResolver._item_media(
                        service, str(exists_media.itemid)
                    ).get("path") or ""
                ).strip()
                if item_path and item_path not in paths:
                    paths.append(item_path)
            except Exception as error:
                logger.warning(
                    f"读取 Emby 电影洗版基线失败：{server_name} - "
                    f"{mediainfo.title_year}，原因：{error}"
                )
        return paths

    @staticmethod
    def movie_media(chain, mediainfo: MediaInfo) -> list[Dict[str, Any]]:
        """读取 Emby 电影路径和大小，作为网盘查询前的首选基线。"""
        services = MediaServerHelper().get_services(type_filter="emby")
        if not services or not chain or not mediainfo:
            return []
        result = []
        for server_name, service in services.items():
            if service.instance.is_inactive():
                continue
            try:
                exists_media = chain.media_exists(mediainfo=mediainfo, server=server_name)
                if not exists_media or not exists_media.itemid:
                    continue
                media = EmbyMediaResolver._item_media(service, str(exists_media.itemid))
                if media.get("path") and media not in result:
                    result.append(media)
            except Exception as error:
                logger.warning(
                    f"读取 Emby 电影洗版基线失败：{server_name} - "
                    f"{mediainfo.title_year}，原因：{error}"
                )
        return result

    @staticmethod
    def episode_numbers(
            chain, mediainfo: MediaInfo, season: int
    ) -> tuple[bool, Set[int]]:
        """只读取 Emby 季集 ID，不逐集请求详情路径。"""
        services = MediaServerHelper().get_services(type_filter="emby")
        if not services or not chain or not mediainfo:
            return False, set()

        mediaserver_chain = MediaServerChain()
        checked = False
        episodes: Set[int] = set()
        for server_name, service in services.items():
            if service.instance.is_inactive():
                continue
            try:
                exists_media = chain.media_exists(
                    mediainfo=mediainfo,
                    server=server_name,
                )
                checked = True
                if not exists_media or not exists_media.itemid:
                    continue
                episode_ids = mediaserver_chain.get_season_episode_ids(
                    server=server_name,
                    item_id=exists_media.itemid,
                    season=season,
                )
                episodes.update(int(episode) for episode in (episode_ids or {}))
            except Exception as error:
                logger.warning(
                    f"读取 Emby 剧集清单失败：{server_name} - "
                    f"{mediainfo.title_year} S{season:02d}，原因：{error}"
                )
        return checked, episodes


class MediaServerNotifier:
    """按媒体项通知所选媒体服务器，并可触发 Emby 提取媒体信息。"""

    _BATCH_WINDOW_SECONDS = 2
    _REFRESH_TIMEOUT_SECONDS = 60
    _MEDIAINFO_TIMER_LIMIT = 256
    _EMBY_REFRESH_DEDUPE_SECONDS = 15
    _EMBY_REFRESH_RETRY_DELAYS = (0, 1, 2)
    _EMBY_RETRY_STATUS_CODES = {500, 502, 503, 504}

    def __init__(
            self,
            enabled: bool = False,
            mediaservers: Optional[List[str]] = None,
            path_mappings: str = "",
            delay_seconds: int = 0,
            emby_mediainfo_enabled: bool = False,
    ):
        self.enabled = bool(enabled)
        self.mediaservers = list(mediaservers or [])
        self.path_mappings = str(path_mappings or "")
        self.delay_seconds = max(0, int(delay_seconds or 0))
        self.emby_mediainfo_enabled = bool(emby_mediainfo_enabled)
        self._batch_lock = RLock()
        self._pending: Dict[str, Dict[str, Any]] = {}
        self._batch_timer: Optional[Timer] = None
        self._task_batch_depth = 0
        self._closed = False
        self._batch_executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="pansearch-media-batch"
        )
        self._refresh_executor = ThreadPoolExecutor(
            max_workers=max(1, min(len(self.mediaservers) or 1, 4)),
            thread_name_prefix="pansearch-media-refresh",
        )
        self._emby_refresh_recent: Dict[str, float] = {}
        self._mediainfo_timers: Dict[str, Timer] = {}

    def begin_task_batch(self) -> bool:
        """记录任务批次；通知仍按延迟窗口独立提交。"""
        with self._batch_lock:
            if self._closed:
                return False
            self._task_batch_depth += 1
        return True

    def finish_task_batch(self) -> bool:
        """结束任务批次，不绕过已配置的通知延迟。"""
        with self._batch_lock:
            if self._task_batch_depth <= 0:
                return True
            self._task_batch_depth -= 1
            if self._pending and not self._batch_timer:
                self._schedule_flush_locked()
        return True

    def _schedule_flush_locked(self) -> None:
        if self._batch_timer:
            self._batch_timer.cancel()
        wait_seconds = max(self.delay_seconds, self._BATCH_WINDOW_SECONDS)
        self._batch_timer = Timer(wait_seconds, self._flush_pending)
        self._batch_timer.daemon = True
        self._batch_timer.start()
        logger.debug(f"入库通知批次已更新，静默 {wait_seconds} 秒后提交")

    @staticmethod
    def _normalize_path(path: str) -> str:
        normalized = str(path or "").replace("\\", "/")
        return normalized.rstrip("/") or "/"

    def _media_server_path(self, moviepilot_path: Path) -> Path:
        source = self._normalize_path(str(moviepilot_path))
        matches = []
        for line in self.path_mappings.splitlines():
            parts = [part.strip() for part in line.split("#", 1)]
            if len(parts) != 2 or not all(parts):
                continue
            server_root, moviepilot_root = map(self._normalize_path, parts)
            if source == moviepilot_root or source.startswith(f"{moviepilot_root}/"):
                matches.append((len(moviepilot_root), server_root, moviepilot_root))
        if not matches:
            return Path(source)

        _, server_root, moviepilot_root = max(matches, key=lambda item: item[0])
        relative = source[len(moviepilot_root):].lstrip("/")
        translated = f"{server_root}/{relative}" if relative else server_root
        return Path(translated)

    def media_server_path(self, moviepilot_path: Path) -> Path:
        """将 MoviePilot 可访问路径转换为媒体服务器路径。"""
        return self._media_server_path(moviepilot_path)

    def notify_deleted_path(self, path: Path, record: Dict[str, Any]) -> bool:
        """按已删除历史记录的路径刷新媒体服务器目录。"""
        try:
            media_type = MediaType(str(record.get("type") or ""))
        except ValueError:
            media_type = None
        media_info = SimpleNamespace(
            title=str(record.get("title") or record.get("file_name") or ""),
            year=record.get("year"),
            type=media_type,
            category=record.get("category"),
        )
        return self.notify(
            path,
            media_info,
            file_name=str(record.get("file_name") or ""),
            force=True,
            deleted=True,
        )

    def notify(
            self,
            path: Path,
            mediainfo: MediaInfo,
            file_name: str = "",
            force: bool = False,
            deleted: bool = False,
    ) -> bool:
        if not self.enabled and not force:
            return True
        if not self.mediaservers:
            logger.warning("入库通知已启用，但尚未选择媒体服务器")
            return False
        if not path or not mediainfo:
            logger.warning(f"入库通知缺少媒体路径或识别信息：{file_name}")
            return False

        target_path = self._media_server_path(path)
        target_folder = target_path.parent if target_path.suffix else target_path
        item = RefreshMediaItem(
            title=mediainfo.title,
            year=mediainfo.year,
            type=mediainfo.type,
            category=mediainfo.category,
            target_path=target_folder,
        )
        key = self._normalize_path(str(target_folder))
        with self._batch_lock:
            if self._closed:
                logger.warning("入库通知器已关闭，无法继续接收通知")
                return False
            pending = self._pending.get(key)
            if pending:
                pending["paths"].add(target_path)
                if deleted:
                    pending.setdefault("deleted_paths", set()).add(target_path)
            else:
                self._pending[key] = {
                    "item": item,
                    "folder": target_folder,
                    "paths": {target_path},
                    "deleted_paths": {target_path} if deleted else set(),
                }
            self._schedule_flush_locked()
        return True

    def _take_pending(self) -> List[Dict[str, Any]]:
        with self._batch_lock:
            entries = list(self._pending.values())
            self._pending.clear()
            self._batch_timer = None
        return entries

    def _flush_pending(self) -> bool:
        return self._submit_batch_async(self._take_pending())

    def _submit_batch_async(self, entries: List[Dict[str, Any]]) -> bool:
        if not entries:
            return True
        try:
            self._batch_executor.submit(self._submit_batch, entries)
        except RuntimeError:
            logger.warning("媒体库刷新执行器已关闭，无法提交新批次")
            return False
        return True

    def _refresh_service(
            self,
            name: str,
            service: Any,
            items: List[RefreshMediaItem],
            entries: List[Dict[str, Any]],
    ) -> bool:
        started_at = time.monotonic()
        logger.debug(f"开始刷新媒体库：{name} - {len(items)} 个媒体目录")
        if service.type == "emby":
            future = self._refresh_executor.submit(
                self._refresh_emby_entries, name, service, entries
            )
        else:
            future = self._refresh_executor.submit(
                service.instance.refresh_library_by_items, items
            )
        try:
            success = bool(future.result(timeout=self._REFRESH_TIMEOUT_SECONDS))
        except FutureTimeoutError:
            future.cancel()
            logger.error(
                f"媒体库刷新超时：{name} - 等待 "
                f"{self._REFRESH_TIMEOUT_SECONDS} 秒仍未返回，订阅任务不受影响"
            )
            return False
        except Exception as error:
            logger.error(
                f"媒体库刷新失败：{name} - {len(items)} 个媒体目录，"
                f"耗时 {time.monotonic() - started_at:.2f} 秒，原因：{error}"
            )
            return False

        elapsed = time.monotonic() - started_at
        if not success:
            logger.error(
                f"媒体库刷新失败：{name} - {len(items)} 个媒体目录，"
                f"耗时 {elapsed:.2f} 秒，媒体服务器未确认刷新请求"
            )
            return False
        logger.debug(
            f"媒体库刷新完成：{name} - {len(items)} 个媒体目录，"
            f"耗时 {elapsed:.2f} 秒"
        )
        return True

    def _submit_batch(self, entries: List[Dict[str, Any]]) -> bool:
        if not entries:
            return True
        services = MediaServerHelper().get_services(name_filters=self.mediaservers)
        if not services:
            logger.warning("未找到已选择的媒体服务器实例，无法提交入库通知")
            return False

        items = [entry["item"] for entry in entries]
        paths = list(
            dict.fromkeys(
                path
                for entry in entries
                for path in entry.get("paths", set())
            )
        )
        deleted_paths = {
            path
            for entry in entries
            for path in entry.get("deleted_paths", set())
        }
        mediainfo_paths = [path for path in paths if path not in deleted_paths]
        refreshed = 0
        submitted = 0
        started_at = time.monotonic()
        for name, service in services.items():
            if service.instance.is_inactive():
                logger.warning(f"媒体服务器未连接，跳过入库通知：{name}")
                continue
            if not hasattr(service.instance, "refresh_library_by_items"):
                logger.warning(f"媒体服务器不支持按项入库通知：{name}")
                continue
            submitted += 1
            if self._refresh_service(name, service, items, entries):
                refreshed += 1
                if self.emby_mediainfo_enabled and service.type == "emby":
                    for path in mediainfo_paths:
                        self._schedule_emby_mediainfo(name, path, attempt=1)
        if submitted:
            logger.info(
                f"媒体库刷新批次完成：成功 {refreshed}/{submitted}，"
                f"耗时 {time.monotonic() - started_at:.2f} 秒"
            )
        return refreshed > 0

    def close(self, flush: bool = True) -> None:
        """停止定时器；配置重载或插件停止时后台提交尚未发送的批次。"""
        with self._batch_lock:
            self._closed = True
            self._task_batch_depth = 0
            timer = self._batch_timer
            self._batch_timer = None
            if timer:
                timer.cancel()
            entries = list(self._pending.values()) if flush else []
            self._pending.clear()
            self._emby_refresh_recent.clear()
            mediainfo_timers = list(self._mediainfo_timers.values())
            self._mediainfo_timers.clear()
        for mediainfo_timer in mediainfo_timers:
            mediainfo_timer.cancel()
        if entries:
            try:
                self._batch_executor.submit(self._submit_final_batch, entries)
            except RuntimeError:
                self._refresh_executor.shutdown(wait=False, cancel_futures=True)
        else:
            self._refresh_executor.shutdown(wait=False, cancel_futures=True)
        self._batch_executor.shutdown(wait=False, cancel_futures=False)

    def _submit_final_batch(self, entries: List[Dict[str, Any]]) -> None:
        try:
            self._submit_batch(entries)
        finally:
            self._refresh_executor.shutdown(wait=False, cancel_futures=True)

    @classmethod
    def _emby_connection(cls, name: str):
        service = MediaServerHelper().get_service(name=name, type_filter="emby")
        if not service or service.instance.is_inactive():
            return None
        connection = cls._emby_refresh_connection(service)
        user_id = str(service.instance.get_user() or "").strip()
        if not connection or not user_id:
            return None
        host, api_key = connection
        return host, api_key, user_id

    @staticmethod
    def _emby_refresh_connection(service: Any):
        """读取 Emby 刷新所需连接信息，不依赖用户 ID。"""
        config = service.config.config or {}
        instance = service.instance
        host = str(
            config.get("host") or getattr(instance, "_host", "") or ""
        ).strip().rstrip("/")
        api_key = str(
            config.get("apikey") or getattr(instance, "_apikey", "") or ""
        ).strip()
        if not host or not api_key:
            return None
        if not host.startswith(("http://", "https://")):
            host = f"http://{host}"
        return host, api_key

    def _emby_item_id_by_path(
            self,
            host: str,
            api_key: str,
            folder: Path,
            cache: Dict[str, Optional[str]],
    ) -> Optional[str]:
        """从目标目录向上查找 Emby 中最近的已存在项目。"""
        candidates = [folder, *folder.parents]
        for candidate in candidates:
            path = self._normalize_path(candidate.as_posix())
            if path in {".", "/"}:
                break
            if path in cache:
                item_id = cache[path]
                if item_id:
                    return item_id
                continue
            item_id = None
            try:
                with RequestUtils(timeout=15).get_res(
                        url=f"{host}/emby/Items",
                        params={
                            "Path": path,
                            "Recursive": "true",
                            "Fields": "Path",
                            "IncludeItemTypes": (
                                    "Movie,Episode,Folder,Series,CollectionFolder"
                            ),
                            "api_key": api_key,
                        },
                ) as response:
                    if response and response.status_code == 200:
                        data = response.json() or {}
                        item_id = next(
                            (
                                str(item.get("Id"))
                                for item in data.get("Items", [])
                                if item.get("Id")
                                   and self._normalize_path(item.get("Path")) == path
                            ),
                            None,
                        )
            except Exception as error:
                logger.warning(
                    f"查询 Emby 刷新目录异常：{path}，原因：{error}"
                )
            cache[path] = item_id
            if item_id:
                return item_id
        return None

    @classmethod
    def _post_emby_request(
            cls,
            url: str,
            params: Dict[str, Any],
            operation: str,
            json_data: Optional[Dict[str, Any]] = None,
    ) -> bool:
        last_error = "无有效响应"
        for delay in cls._EMBY_REFRESH_RETRY_DELAYS:
            if delay:
                time.sleep(delay)
            try:
                response = RequestUtils(timeout=30).post_res(
                    url=url, params=params, json=json_data
                )
                if response is None:
                    continue
                with response:
                    status_code = getattr(response, "status_code", None)
                    if status_code in {200, 204}:
                        return True
                    last_error = f"HTTP {status_code}"
                    if status_code not in cls._EMBY_RETRY_STATUS_CODES:
                        break
            except Exception as error:
                last_error = str(error) or error.__class__.__name__
        logger.warning(f"{operation}失败：{last_error}")
        return False

    @classmethod
    def _post_emby_refresh(
            cls,
            host: str,
            api_key: str,
            item_id: str,
    ) -> bool:
        return cls._post_emby_request(
            url=f"{host}/emby/Items/{item_id}/Refresh",
            params={
                "Recursive": "true",
                "MetadataRefreshMode": "Default",
                "ImageRefreshMode": "Default",
                "ReplaceAllMetadata": "false",
                "ReplaceAllImages": "false",
                "api_key": api_key,
            },
            operation=f"提交 Emby 项目刷新 {item_id}",
        )

    @classmethod
    def _notify_emby_deleted_paths(
            cls,
            host: str,
            api_key: str,
            paths: List[str],
    ) -> bool:
        if not paths:
            return True
        return cls._post_emby_request(
            url=f"{host}/emby/Library/Media/Updated",
            params={"api_key": api_key},
            json_data={
                "Updates": [
                    {"Path": path, "UpdateType": "Deleted"}
                    for path in paths
                ]
            },
            operation=f"提交 Emby 删除通知 {len(paths)} 个路径",
        )

    def _refresh_emby_item(
            self,
            host: str,
            api_key: str,
            item_id: str,
            dedupe: bool,
    ) -> bool:
        key = f"{host}\0{item_id}"
        now = time.monotonic()
        if dedupe:
            with self._batch_lock:
                if now - self._emby_refresh_recent.get(key, 0) < (
                        self._EMBY_REFRESH_DEDUPE_SECONDS
                ):
                    logger.debug(f"Emby 项目刷新已去重：{item_id}")
                    return True
        success = self._post_emby_refresh(host, api_key, item_id)
        if success:
            with self._batch_lock:
                expire_before = now - self._EMBY_REFRESH_DEDUPE_SECONDS
                self._emby_refresh_recent = {
                    cache_key: refreshed_at
                    for cache_key, refreshed_at in self._emby_refresh_recent.items()
                    if refreshed_at >= expire_before
                }
                self._emby_refresh_recent[key] = now
        return success

    def _refresh_emby_entries(
            self,
            name: str,
            service: Any,
            entries: List[Dict[str, Any]],
    ) -> bool:
        """按最近父项目刷新全部目录，避开平台批量仅处理首项的问题。"""
        connection = self._emby_refresh_connection(service)
        if not connection:
            logger.warning(f"Emby 刷新配置无效：{name}")
            return False
        host, api_key = connection
        deleted_paths = list(dict.fromkeys(
            self._normalize_path(path.as_posix())
            for entry in entries
            for path in entry.get("deleted_paths", set())
        ))
        deleted_notified = self._notify_emby_deleted_paths(
            host, api_key, deleted_paths
        )
        cache: Dict[str, Optional[str]] = {}
        item_ids = []
        unresolved_count = 0
        for entry in entries:
            folder = Path(entry["folder"])
            item_id = self._emby_item_id_by_path(
                host, api_key, folder, cache
            )
            if not item_id:
                unresolved_count += 1
            elif item_id not in item_ids:
                item_ids.append(item_id)

        if not item_ids:
            logger.warning(
                f"Emby 未解析到可刷新的媒体项目，"
                f"跳过 {unresolved_count} 个目录且不执行全库刷新"
            )
            return False

        succeeded = sum(
            1
            for item_id in item_ids
            if self._refresh_emby_item(
                host, api_key, item_id, dedupe=not deleted_paths
            )
        )
        logger.info(
            f"Emby 刷新请求提交完成：目录 {len(entries)} 个，"
            f"目标项目 {len(item_ids)} 个，成功 {succeeded}/{len(item_ids)}，"
            f"未定位目录 {unresolved_count} 个"
        )
        return (
                deleted_notified
                and succeeded == len(item_ids)
                and not unresolved_count
        )

    def _schedule_emby_mediainfo(
            self, name: str, path: Path, attempt: int
    ) -> None:
        key = f"{name}\0{path.as_posix()}"

        def trigger() -> None:
            with self._batch_lock:
                self._mediainfo_timers.pop(key, None)
                if self._closed:
                    return
            self._trigger_emby_mediainfo(name, path, attempt)

        timer = Timer(
            10 if attempt == 1 else 15,
            trigger,
        )
        timer.daemon = True
        with self._batch_lock:
            if self._closed:
                return
            previous = self._mediainfo_timers.get(key)
            if previous:
                previous.cancel()
            elif len(self._mediainfo_timers) >= self._MEDIAINFO_TIMER_LIMIT:
                oldest_key = next(iter(self._mediainfo_timers))
                self._mediainfo_timers.pop(oldest_key).cancel()
                logger.warning(
                    "Emby 媒体信息提取等待队列已达上限，已丢弃最早任务"
                )
            self._mediainfo_timers[key] = timer
            timer.start()

    def _trigger_emby_mediainfo(
            self, name: str, path: Path, attempt: int
    ) -> None:
        connection = self._emby_connection(name)
        if not connection:
            logger.warning(f"Emby 媒体信息提取配置无效或服务未连接：{name}")
            return
        host, api_key, user_id = connection
        file_path = path.as_posix()
        try:
            with RequestUtils(timeout=15).get_res(
                    url=f"{host}/emby/Items",
                    params={
                        "Path": file_path,
                        "Recursive": "true",
                        "Fields": "Path",
                        "IncludeItemTypes": "Movie,Episode,Folder,Series",
                        "api_key": api_key,
                    },
            ) as response:
                items = response.json().get("Items", []) if response else []
            item_id = next(
                (
                    item.get("Id")
                    for item in items
                    if str(item.get("Path") or "").replace("\\", "/")
                       == file_path
                ),
                None,
            )
            if not item_id:
                if attempt < 4:
                    self._schedule_emby_mediainfo(name, path, attempt + 1)
                else:
                    logger.warning(
                        f"Emby 入库后仍未找到媒体项，跳过媒体信息提取：{name} - {file_path}"
                    )
                return
            with RequestUtils(timeout=30).post_res(
                    url=f"{host}/emby/Items/{item_id}/PlaybackInfo",
                    params={
                        "AutoOpenLiveStream": "true",
                        "IsPlayback": "true",
                        "api_key": api_key,
                        "UserId": user_id,
                    },
            ) as response:
                success = bool(response and response.status_code == 200)
            if success:
                logger.debug(f"Emby 媒体信息提取已触发：{name} - {file_path}")
            else:
                status_code = getattr(response, "status_code", None)
                logger.warning(
                    f"Emby 媒体信息提取失败：{name} - {file_path}，状态码：{status_code}"
                )
        except Exception as error:
            logger.warning(f"Emby 媒体信息提取异常：{name} - {file_path}，原因：{error}")
            if attempt < 4:
                self._schedule_emby_mediainfo(name, path, attempt + 1)
