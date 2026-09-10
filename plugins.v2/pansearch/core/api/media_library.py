"""媒体库 Webhook、播放与内容查询 API。"""

from threading import RLock, Timer
from typing import Any, Dict, Optional

from app.chain.mediaserver import MediaServerChain
from app.db import SessionFactory
from app.db.models.mediaserver import MediaServerItem
from app.helper.mediaserver import MediaServerHelper
from app.log import logger
from sqlalchemy import func

from .. import OwnerDelegator
from ..media import media_server_tmdb_filters, tmdb_id_of


class MediaLibraryApi(OwnerDelegator):
    _SYNC_DEBOUNCE_SECONDS = 2
    _PLATFORM_SYNC_EVENTS = {"library.new", "library.deleted"}

    def __init__(self, owner):
        super().__init__(owner)
        object.__setattr__(self, "_sync_timer_lock", RLock())
        object.__setattr__(self, "_sync_timers", {})

    def handle_platform_media_webhook(self, event_info: Any) -> bool:
        """消费已鉴权并标准化的媒体服务器 Webhook 事件。"""
        if not event_info:
            return False
        event_name = str(getattr(event_info, "event", "") or "").strip().lower()
        channel = str(getattr(event_info, "channel", "") or "").strip().lower()
        if event_name == "deep.delete":
            return self._handle_platform_deep_delete(event_info)
        if not self._platform_media_sync_enabled:
            return False
        server_name = str(
            getattr(event_info, "server_name", "") or ""
        ).strip()
        if channel != "emby" or event_name not in self._PLATFORM_SYNC_EVENTS:
            return False
        if not server_name:
            logger.debug("平台 Emby Webhook 缺少媒体服务器来源，跳过数据同步")
            return False
        service = MediaServerHelper().get_service(
            name=server_name,
            type_filter="emby",
        )
        if not service:
            logger.debug(
                f"平台 Emby Webhook 来源未匹配已配置服务：{server_name}"
            )
            return False
        scheduled = self._schedule_platform_media_sync(server_name)
        if scheduled:
            logger.debug(
                f"已接收平台 Emby Webhook：{server_name} - {event_name}"
            )
        return scheduled

    def _handle_platform_deep_delete(self, event_info: Any) -> bool:
        """按神医通知中的媒体服务器路径精确删除关联内容。"""
        if not self._platform_deep_delete_enabled:
            logger.info("收到神医深度删除事件，联动删除未启用，已跳过")
            return False
        if not self._sync_handler:
            logger.warning("神医深度删除联动失败：同步处理器未初始化")
            return False
        paths = self._deep_delete_paths(event_info)
        if not paths:
            logger.warning("神医深度删除通知缺少 Item Path，已跳过")
            return False
        logger.info(
            f"收到神医深度删除事件：路径={len(paths)} 个，开始匹配插件历史"
        )
        result = self._sync_handler.delete_by_media_server_paths(paths)
        if not result["matched"]:
            logger.warning(
                f"神医深度删除未匹配插件历史：{', '.join(paths)}"
            )
            return False
        message = (
            f"路径 {len(paths)} 个，匹配 {result['matched']} 条，"
            f"删除历史 {result['deleted']} 条、网盘文件 "
            f"{result.get('cloud_files_deleted', 0)} 个、STRM "
            f"{result.get('strm_deleted', 0)} 个"
        )
        if result["skipped"]:
            message += f"，跳过 {result['skipped']} 条"
        logger.info(f"神医深度删除联动完成：{message}")
        if self._notify:
            try:
                self.post_message(
                    mtype=self._notification_type,
                    title="【网盘订阅】神医联动删除完成",
                    text=message,
                )
            except Exception as error:
                logger.warning(f"神医深度删除结果通知发送失败：{error}")
        return result["deleted"] > 0

    @classmethod
    def _deep_delete_paths(cls, event_info: Any) -> list[str]:
        """合并标准路径与神医 Description 中的多版本路径。"""
        paths = [str(getattr(event_info, "item_path", "") or "").strip()]
        payload = getattr(event_info, "json_object", None) or {}
        description = str(payload.get("Description") or "")
        in_path_section = False
        for raw_line in description.splitlines():
            line = raw_line.strip()
            if "Item Path:" in line:
                in_path_section = True
                _, _, inline_path = line.partition(":")
                paths.append(inline_path.strip())
                continue
            if not in_path_section:
                continue
            if any(marker in line for marker in (
                    "Mount Paths:", "Item Name:", "Description:", "Other Info:"
            )):
                in_path_section = False
                continue
            if line and not line.startswith(("http://", "https://")):
                paths.append(line)
        normalized = []
        seen = set()
        for path in paths:
            value = cls._normalize_media_path(path)
            if value and value not in seen:
                seen.add(value)
                normalized.append(value)
        return normalized

    @staticmethod
    def _normalize_media_path(path: str) -> str:
        """统一神医与媒体服务器通知中的路径分隔符。"""
        value = str(path or "").strip().replace("\\", "/")
        while "//" in value:
            value = value.replace("//", "/")
        return value.rstrip("/")

    def _schedule_platform_media_sync(self, server_name: str) -> bool:
        """仅由有效 Emby Webhook 触发，并按媒体服务器合并短时重复事件。"""
        name = str(server_name or "").strip()
        if not name or not self._platform_media_sync_enabled:
            return False

        def trigger() -> None:
            with self._sync_timer_lock:
                self._sync_timers.pop(name, None)
            if not self._platform_media_sync_enabled:
                return
            try:
                MediaServerChain().sync(server=name)
                logger.info(f"Emby Webhook 触发媒体库数据同步完成：{name}")
            except Exception as error:
                logger.warning(
                    f"Emby Webhook 触发媒体库数据同步失败："
                    f"{name}，原因：{error}"
                )

        timer = Timer(self._SYNC_DEBOUNCE_SECONDS, trigger)
        timer.daemon = True
        with self._sync_timer_lock:
            previous = self._sync_timers.get(name)
            if previous:
                previous.cancel()
            self._sync_timers[name] = timer
            timer.start()
        logger.debug(f"Emby Webhook 已安排媒体库数据同步：{name}")
        return True

    def close(self) -> None:
        with self._sync_timer_lock:
            timers = list(self._sync_timers.values())
            self._sync_timers.clear()
        for timer in timers:
            timer.cancel()

    @staticmethod
    def _history_group_key(media_type: str, tmdb_id: Any) -> str:
        return f"tmdb:{media_type or '未知类型'}:{tmdb_id}"

    def _history_emby_play_items(self, history: list) -> Dict[str, str]:
        """一次数据库查询返回可在已启用 Emby 中播放的历史汇总项。"""
        services = MediaServerHelper().get_services(type_filter="emby")
        active_services = {
            name: service
            for name, service in services.items()
            if not service.instance.is_inactive()
        }
        if not active_services:
            return {}
        eligible = {
            (int(item.get("tmdb_id")), str(item.get("type") or ""))
            for item in history
            if item.get("tmdb_id")
               and str(item.get("status") or "") == "成功"
               and not item.get("finalize_key")
        }
        if not eligible:
            return {}
        tmdb_ids = {tmdb_id for tmdb_id, _ in eligible}
        with SessionFactory() as db:
            rows = db.query(MediaServerItem).filter(
                MediaServerItem.server == "emby",
                *media_server_tmdb_filters(MediaServerItem, tmdb_ids),
            ).all()
        result = {}
        for row in rows:
            identity = (tmdb_id_of(row) or 0, str(row.item_type or ""))
            if identity not in eligible or not row.item_id:
                continue
            result[self._history_group_key(identity[1], identity[0])] = str(
                row.item_id
            )
        return result

    def api_vue_emby_play(self, item_id: str) -> dict:
        """仅从已启用 Emby 实例解析播放链接。"""
        item_id = str(item_id or "").strip()
        if not item_id:
            return {"success": False, "message": "缺少 Emby 媒体条目ID"}
        services = MediaServerHelper().get_services(type_filter="emby")
        chain = MediaServerChain()
        for name, service in services.items():
            if service.instance.is_inactive():
                continue
            item = chain.iteminfo(server=name, item_id=item_id)
            if not item:
                continue
            play_url = chain.get_play_url(server=name, item_id=item_id)
            if play_url:
                return {"success": True, "data": {"url": play_url}}
        return {"success": False, "message": "未在已启用的 Emby 中找到播放地址"}

    @staticmethod
    def _media_type(value: Any) -> str:
        text = str(getattr(value, "value", value) or "").strip().casefold()
        if text in {"movie", "film", "电影"}:
            return "movie"
        if text in {"tv", "series", "show", "television", "电视剧", "剧集"}:
            return "tv"
        return ""

    @staticmethod
    def _storage_server_name(name: str, service: Any) -> str:
        return str(getattr(service, "type", "") or name or "").strip().casefold()

    def lookup_media_library(self, media_items) -> Dict[str, list]:
        """按 TMDB ID 和媒体类型汇总媒体库条目，避免电影/剧集同 ID 误判。"""
        requested = {}
        for item in media_items or []:
            if isinstance(item, dict):
                value = item.get("tmdb_id")
                media_type = self._media_type(item.get("media_type"))
            else:
                value = item
                media_type = ""
            try:
                value = int(value)
            except (TypeError, ValueError):
                continue
            if value > 0:
                requested.setdefault(value, set()).add(media_type)
        normalized_ids = set(requested)
        if not normalized_ids:
            return {}

        services = MediaServerHelper().get_services() or {}
        storage_servers = set()
        server_names = {}
        for name, service in services.items():
            if not service or getattr(service, "instance", None) is None:
                continue
            if service.instance.is_inactive():
                continue
            storage_name = self._storage_server_name(name, service)
            aliases = {str(name).casefold(), storage_name}
            storage_servers.update(aliases)
            for alias in aliases:
                server_names[alias] = str(name)
        if not storage_servers:
            return {}

        result = {str(tmdb_id): [] for tmdb_id in normalized_ids}
        with SessionFactory() as db:
            rows = db.query(MediaServerItem).filter(
                func.lower(MediaServerItem.server).in_(storage_servers),
                *media_server_tmdb_filters(MediaServerItem, normalized_ids),
            ).all()
        for row in rows:
            tmdb_id = tmdb_id_of(row)
            if not tmdb_id:
                continue
            row_type = self._media_type(getattr(row, "item_type", ""))
            requested_types = requested.get(tmdb_id, set())
            if requested_types and "" not in requested_types and row_type not in requested_types:
                continue
            storage_server = str(row.server or "").casefold()
            result.setdefault(str(tmdb_id), []).append({
                "server": server_names.get(storage_server, str(row.server or "媒体服务器")),
                "library": str(row.library or "").strip(),
                "item_id": str(row.item_id or "").strip(),
                "title": str(row.title or "").strip(),
                "year": row.year,
                "item_type": str(row.item_type or "").strip(),
                "media_type": row_type,
                "path": str(row.path or "").strip(),
                "seasoninfo": row.seasoninfo or {},
            })
        return {key: value for key, value in result.items() if value}

    def lookup_media_library_by_douban(self, douban_ids) -> Dict[str, list]:
        """按豆瓣 ID 查询媒体库，用于未补全 TMDB ID 的豆瓣榜单卡片。"""
        normalized = {
            str(value).strip() for value in (douban_ids or [])
            if str(value or "").strip()
        }
        if not normalized:
            return {}
        if not hasattr(MediaServerItem, "media_source") or not hasattr(MediaServerItem, "media_id"):
            return {}
        services = MediaServerHelper().get_services() or {}
        storage_servers = set()
        server_names = {}
        for name, service in services.items():
            if not service or getattr(service, "instance", None) is None:
                continue
            if service.instance.is_inactive():
                continue
            storage_name = self._storage_server_name(name, service)
            aliases = {str(name).casefold(), storage_name}
            storage_servers.update(aliases)
            for alias in aliases:
                server_names[alias] = str(name)
        if not storage_servers:
            return {}
        result = {value: [] for value in normalized}
        with SessionFactory() as db:
            rows = db.query(MediaServerItem).filter(
                func.lower(MediaServerItem.server).in_(storage_servers),
                func.lower(MediaServerItem.media_source) == "douban",
                MediaServerItem.media_id.in_(normalized),
            ).all()
        for row in rows:
            key = str(row.media_id or "").strip()
            if key not in result:
                continue
            result[key].append({
                "server": server_names.get(str(row.server or "").casefold(), str(row.server or "媒体服务器")),
                "library": str(row.library or "").strip(),
                "item_id": str(row.item_id or "").strip(),
                "title": str(row.title or "").strip(),
                "year": row.year,
                "item_type": str(row.item_type or "").strip(),
                "media_type": self._media_type(getattr(row, "item_type", "")),
                "path": str(row.path or "").strip(),
                "seasoninfo": row.seasoninfo or {},
            })
        return {key: value for key, value in result.items() if value}

    def lookup_media_library_by_bangumi(self, bangumi_ids) -> Dict[str, list]:
        """按 Bangumi ID 查询媒体库入库状态。"""
        normalized = {
            str(value).strip() for value in (bangumi_ids or [])
            if str(value or "").strip()
        }
        if not normalized:
            return {}
        if not hasattr(MediaServerItem, "media_source") or not hasattr(MediaServerItem, "media_id"):
            return {}
        services = MediaServerHelper().get_services() or {}
        storage_servers = set()
        server_names = {}
        for name, service in services.items():
            if not service or getattr(service, "instance", None) is None:
                continue
            if service.instance.is_inactive():
                continue
            storage_name = self._storage_server_name(name, service)
            aliases = {str(name).casefold(), storage_name}
            storage_servers.update(aliases)
            for alias in aliases:
                server_names[alias] = str(name)
        if not storage_servers:
            return {}
        result = {value: [] for value in normalized}
        with SessionFactory() as db:
            rows = db.query(MediaServerItem).filter(
                func.lower(MediaServerItem.server).in_(storage_servers),
                func.lower(MediaServerItem.media_source).in_(["bangumi", "bgm"]),
                MediaServerItem.media_id.in_(normalized),
            ).all()
        for row in rows:
            key = str(row.media_id or "").strip()
            if key not in result:
                continue
            result[key].append({
                "server": server_names.get(str(row.server or "").casefold(), str(row.server or "媒体服务器")),
                "library": str(row.library or "").strip(),
                "item_id": str(row.item_id or "").strip(),
                "title": str(row.title or "").strip(),
                "year": row.year,
                "item_type": str(row.item_type or "").strip(),
                "media_type": self._media_type(getattr(row, "item_type", "")),
                "path": str(row.path or "").strip(),
                "seasoninfo": row.seasoninfo or {},
            })
        return {key: value for key, value in result.items() if value}

    def api_vue_media_server_content(
            self,
            server: str = "",
            keyword: str = "",
            tmdb_id: Optional[int] = None,
            media_type: str = "",
    ) -> dict:
        if not self._sync_handler:
            return {"success": False, "message": "同步处理器未初始化"}
        try:
            data = self._sync_handler.list_media_server_content(
                server=server,
                keyword=keyword,
                tmdb_id=tmdb_id,
                media_type=media_type,
            )
            return {
                "success": True,
                "message": f"已读取 {len(data.get('items') or [])} 个媒体库内容",
                "data": data,
            }
        except Exception as error:
            logger.warning(f"读取媒体服务器内容失败：{error}")
            return {"success": False, "message": str(error)}

    def api_vue_media_servers(self) -> dict:
        """返回可用于洗版的媒体服务器选项，不触发内容查询。"""
        if not self._sync_handler:
            return {"success": False, "message": "同步处理器未初始化"}
        try:
            data = self._sync_handler.list_media_server_content()
            return {
                "success": True,
                "data": {"servers": data.get("servers") or []},
            }
        except Exception as error:
            logger.warning(f"读取媒体服务器选项失败：{error}")
            return {"success": False, "message": str(error)}
