"""订阅、入库与资源下载事件响应。"""

import io
import re
from threading import Thread
from typing import Any, Dict, Optional, Tuple

from app.core.event import Event
from app.db.subscribe_oper import SubscribeOper
from app.log import logger
from app.schemas import Notification
from app.schemas.event import ResourceDownloadEventData
from app.schemas.types import MediaType, NotificationType
from torf import Torrent, TorfError

from ..media import list_subscribes_by_tmdb_id
from ...core import OwnerDelegator
from ...search.types import resource_type_from_url, resource_type_name


class PluginEventHandler(OwnerDelegator):
    """处理事件总线回调。"""

    _ACCESS_CODE_PATTERN = re.compile(
        r"(?:提取码|访问码|密码|口令|pwd|password)\s*[:：=]?\s*[A-Za-z0-9]{1,16}",
        re.IGNORECASE,
    )

    @staticmethod
    def _torrent_payload_to_magnet(payload: bytes) -> Tuple[str, Dict[str, Any]]:
        if not payload or len(payload) > 10 * 1024 * 1024:
            return "", {}
        try:
            torrent = Torrent.read_stream(io.BytesIO(payload), validate=True)
            magnet_url = str(torrent.magnet())
        except (OSError, TorfError, TypeError, ValueError):
            return "", {}
        files = [str(item) for item in torrent.files]
        if not files and torrent.name:
            files = [str(torrent.name)]
        return magnet_url, {
            "info_hash": str(torrent.infohash or "").upper(),
            "display_name": str(torrent.name or "").strip(),
            "size": int(torrent.size or 0),
            "torrent_files": files,
            "metadata_available": bool(torrent.name or files),
            "metadata_source": "moviepilot",
        }

    def _takeover_platform_download(
            self,
            event_data: ResourceDownloadEventData,
            subscribe: Any,
            season: int,
            episodes: list,
    ) -> bool:
        context = event_data.context
        torrent_info = context.torrent_info
        if "magnet" not in (self._resource_type_order or []):
            logger.debug("接管平台资源下载失败：资源类型优先级未启用 Magnet")
            return False

        from app.chain.download import DownloadChain

        payload, _, _ = DownloadChain().download_torrent(
            torrent=torrent_info,
            channel=event_data.channel,
            source=event_data.origin,
        )
        magnet_url = ""
        metadata: Dict[str, Any] = {}
        if isinstance(payload, str) and payload.lower().startswith("magnet:?"):
            magnet_url = payload
            parsed = self._sync_handler._offline_download.parse_magnet_link(
                magnet_url, fetch_metadata=True
            )
            metadata = dict((parsed or {}).get("metadata") or {})
        elif isinstance(payload, bytes):
            magnet_url, metadata = self._torrent_payload_to_magnet(payload)
        if not magnet_url:
            logger.warning(f"接管平台资源下载失败：无法读取种子元数据，{torrent_info.title}")
            return False

        resource = {
            "resource_type": "magnet",
            "pan_type": "magnet",
            "source": "moviepilot",
            "title": str(torrent_info.title or metadata.get("display_name") or "PT资源"),
            "url": magnet_url,
            "share_url": magnet_url,
            "magnet_metadata": metadata,
        }
        pending_key = self._sync_handler._queue_magnet_package(
            resource=resource,
            share_url=magnet_url,
            subscribe=subscribe,
            mediainfo=context.media_info,
            season=season,
            target_episodes=episodes,
            sub_key=f"pt:{getattr(subscribe, 'id', '')}",
        )
        if not pending_key:
            logger.error(f"接管平台资源下载失败：离线任务未创建，{torrent_info.title}")
            return False
        logger.debug(f"已接管平台资源下载：{torrent_info.title}")
        return True

    def _has_pending_cloud_target(
            self,
            subscribes: list,
            seasons: list,
            episodes: list,
    ) -> bool:
        """判断平台资源是否与正在处理的网盘订阅目标重叠。"""
        subscribe_ids = {
            int(getattr(subscribe, "id", 0) or 0) for subscribe in subscribes
        }
        subscribe_ids.discard(0)
        if not subscribe_ids or not self._sync_handler:
            return False
        season_set = {int(value) for value in (seasons or []) if int(value) > 0}
        episode_set = {int(value) for value in (episodes or []) if int(value) > 0}
        for item in self._sync_handler.get_pending_finalize_tasks():
            if int(item.get("subscribe_id") or 0) not in subscribe_ids:
                continue
            pending_season = int(item.get("season") or 0)
            if season_set and pending_season and pending_season not in season_set:
                continue
            pending_episodes = {
                int(value)
                for value in (
                        item.get("target_episodes")
                        or item.get("success_episodes")
                        or item.get("notification_episodes")
                        or ([item.get("episode")] if item.get("episode") else [])
                )
                if int(value) > 0
            }
            if not episode_set or not pending_episodes or episode_set & pending_episodes:
                return True
        return False

    def _get_subscribe_id_from_event(self, event: Event) -> Optional[int]:
        if not event or not event.event_data:
            return None
        data = event.event_data or {}
        subscribe_id = data.get("subscribe_id") or data.get("id")
        if not subscribe_id and isinstance(data.get("subscribe"), dict):
            subscribe_id = data["subscribe"].get("id")
        try:
            return int(subscribe_id) if subscribe_id is not None else None
        except Exception:
            return None

    def on_subscribe_added(self, event: Event):
        """新增订阅由搜索调度钩子自动分流。"""
        sid = self._get_subscribe_id_from_event(event)
        if not sid:
            return
        if self._is_subscribe_excluded(sid):
            logger.debug(f"新增订阅不在插件处理范围：subscribe_id={sid}")
            return
        logger.debug(f"新增订阅等待搜索调度：subscribe_id={sid}")

    def on_subscribe_modified(self, event: Event):
        """ 用户手动修改订阅站点时，不自动覆盖用户操作 """
        sid = self._get_subscribe_id_from_event(event)
        if not sid:
            return
        logger.debug(f"订阅配置已修改，不改写站点：subscribe_id={sid}")
        return

    def on_transfer_complete(self, event: Event):
        """PT 整理完成后异步进入网盘洗版上传。"""
        if (
                not event
                or not self._enabled
                or not self._enable_pt_upgrade
                or not self._sync_handler
        ):
            return
        event_data = event.event_data or {}
        if not event_data.get("downloader") or not event_data.get("download_hash"):
            return
        Thread(
            target=self._sync_handler.process_pt_upgrade,
            args=(dict(event_data),),
            daemon=True,
            name="pansearch-pt-upgrade",
        ).start()

    @staticmethod
    def _event_userid(event_data: dict):
        return event_data.get("userid") or event_data.get("user")

    def _post_command_message(
            self,
            event_data: dict,
            title: str,
            text: str,
            buttons: Optional[list] = None,
    ) -> None:
        message_args = {
            "mtype": NotificationType.Plugin,
            "channel": event_data.get("channel"),
            "source": event_data.get("source"),
            "title": title,
            "text": text,
            "userid": self._event_userid(event_data),
            "username": event_data.get("username"),
            "buttons": buttons,
            "disable_web_page_preview": True,
        }
        try:
            response = self.chain.send_direct_message(Notification(
                **message_args,
                save_history=False,
            ))
            if response and getattr(response, "success", False):
                return
            logger.warning(f"Telegram 命令回复直发失败，回退普通通知：{title}")
        except Exception as error:
            logger.warning(f"Telegram 命令回复直发异常，回退普通通知：{error}")
        try:
            self.post_message(**message_args)
        except Exception as error:
            logger.error(f"Telegram 命令回复发送失败：{error}", exc_info=True)

    @staticmethod
    def _progress_message_data(response) -> Optional[dict]:
        if not response or not getattr(response, "success", False):
            return None
        message_id = getattr(response, "message_id", None)
        chat_id = getattr(response, "chat_id", None)
        if message_id is None or chat_id is None:
            return None
        return {
            "message_id": message_id,
            "chat_id": chat_id,
            "channel": getattr(response, "channel", None),
            "source": getattr(response, "source", None),
            "metadata": getattr(response, "metadata", None),
        }

    def _start_progress_message(
            self,
            event_data: dict,
            title: str,
            text: str,
    ) -> Optional[dict]:
        """直接发送可编辑的进度消息，失败时退回普通插件通知。"""
        try:
            response = self.chain.send_direct_message(Notification(
                mtype=NotificationType.Plugin,
                channel=event_data.get("channel"),
                source=event_data.get("source"),
                title=title,
                text=text,
                userid=self._event_userid(event_data),
                username=event_data.get("username"),
                disable_web_page_preview=True,
                save_history=False,
            ))
            progress_message = self._progress_message_data(response)
            if progress_message:
                return progress_message
        except Exception as error:
            logger.warning(f"Telegram 进度消息发送失败，将使用普通通知：{error}")
        self._post_command_message(event_data, title, text)
        return None

    def _edit_progress_message(
            self,
            event_data: dict,
            title: str,
            text: str,
            buttons: Optional[list] = None,
    ) -> bool:
        progress_message = event_data.get("progress_message") or {}
        if not progress_message:
            return False
        try:
            edited = self.chain.edit_message(
                channel=progress_message.get("channel") or event_data.get("channel"),
                source=progress_message.get("source") or event_data.get("source"),
                message_id=progress_message.get("message_id"),
                chat_id=progress_message.get("chat_id"),
                title=title,
                text=text,
                buttons=buttons if buttons is not None else [],
                metadata=progress_message.get("metadata"),
            )
            if edited:
                return True
            logger.warning("Telegram 进度消息更新失败，将发送普通通知")
        except Exception as error:
            logger.warning(f"Telegram 进度消息更新异常，将发送普通通知：{error}")
        self._post_command_message(
            event_data,
            title,
            text,
            buttons=buttons or None,
        )
        return False

    def _submit_remote_links(
            self,
            event_data: dict,
            subscribe_id: int = 0,
            raw_links: str = "",
            title: str = "",
            media_type: str = "",
            selection_id: str = "",
            tmdb_id: Optional[int] = None,
    ) -> dict:
        resource_types = list(dict.fromkeys(
            resource_type_name(resource_type_from_url(link))
            for link in self.extract_resource_links(raw_links)
            if resource_type_from_url(link)
        ))
        logger.info(
            f"Telegram 资源提交开始：类型={','.join(resource_types) or '未知'}，"
            f"标题={title or '自动识别'}，候选选择={'是' if selection_id else '否'}"
        )
        try:
            result = self.submit_platform_links(
                subscribe_id=subscribe_id,
                resource_links=raw_links,
                title=title,
                media_type=media_type,
                selection_id=selection_id,
                tmdb_id=tmdb_id,
                selection_scope=self._command_selection_scope(event_data),
            )
        except Exception as error:
            logger.error(f"Telegram 资源提交异常：{error}", exc_info=True)
            result = {
                "success": False,
                "message": "资源处理异常，请查看插件日志后重试",
            }
        label = str(event_data.get("link_label") or "").strip()
        result_title = f"【网盘订阅】{label + ' ' if label else ''}资源提交结果"
        result_text = self._format_remote_link_result(result)
        result_buttons = self._remote_link_result_buttons(result)
        if not self._edit_progress_message(
                event_data,
                result_title,
                result_text,
                buttons=result_buttons,
        ):
            if not event_data.get("progress_message"):
                self._post_command_message(
                    event_data,
                    result_title,
                    result_text,
                    buttons=result_buttons,
                )
        logger.info(
            f"Telegram 资源提交完成：成功={bool(result.get('success'))}，"
            f"需要选择={bool((result.get('data') or {}).get('selection_required'))}，"
            f"结果={result.get('message') or '无'}"
        )
        return result

    @staticmethod
    def _command_selection_scope(event_data: dict) -> str:
        channel = getattr(event_data.get("channel"), "value", event_data.get("channel"))
        userid = event_data.get("userid") or event_data.get("user")
        source = str(event_data.get("source") or "")
        return f"telegram:{channel}:{source}:{userid}"

    @staticmethod
    def _format_remote_link_result(result: dict) -> str:
        data = result.get("data") or {}
        if not data.get("selection_required"):
            lines = [str(result.get("message") or "资源提交失败")]
            media = data.get("media") or {}
            if media:
                media_type = {
                    "movie": "电影",
                    "tv": "电视剧",
                }.get(media.get("media_type"), "媒体")
                year = f" ({media.get('year')})" if media.get("year") else ""
                seasons = [
                    int(value) for value in media.get("seasons") or []
                    if str(value).isdigit() and int(value) > 0
                ]
                if not seasons and media.get("season"):
                    seasons = [int(media.get("season"))]
                season = (
                    " · " + "/".join(f"S{value:02d}" for value in sorted(set(seasons)))
                    if seasons else ""
                )
                lines.append(
                    f"已识别：{media.get('title') or '未知媒体'}{year} · "
                    f"{media_type}{season}"
                )
            return "\n".join(lines)
        lines = [str(result.get("message") or "请选择 TMDB 媒体")]
        for item in list(data.get("candidates") or [])[:10]:
            year = f" ({item.get('year')})" if item.get("year") else ""
            media_type = item.get("media_type_name") or item.get("media_type") or ""
            lines.append(
                f"{item.get('title') or '未知媒体'}{year} · {media_type}"
            )
        return "\n".join(lines)

    def _remote_link_result_buttons(self, result: dict) -> Optional[list]:
        data = result.get("data") or {}
        selection_id = str(data.get("selection_id") or "")
        if not data.get("selection_required") or not selection_id:
            return None
        plugin_id = self._owner.__class__.__name__
        buttons = []
        for item in list(data.get("candidates") or [])[:10]:
            media_type = str(item.get("media_type") or "").lower()
            media_code = (
                "m" if media_type == "movie"
                else "t" if media_type == "tv"
                else ""
            )
            try:
                tmdb_id = int(item.get("tmdb_id") or 0)
            except (TypeError, ValueError):
                tmdb_id = 0
            if not media_code or tmdb_id <= 0:
                continue
            type_name = "电影" if media_code == "m" else "电视剧"
            year = f" ({item.get('year')})" if item.get("year") else ""
            label = f"{type_name} · {item.get('title') or '未知媒体'}{year}"
            buttons.append([{
                "text": label,
                "callback_data": (
                    f"[PLUGIN]{plugin_id}|cl|{selection_id}|{media_code}|{tmdb_id}"
                ),
            }])
        return buttons or None

    def handle_telegram_links(self, event_data: dict) -> None:
        """处理 Telegram 普通消息中的资源链接。"""
        links = list(event_data.get("links") or [])
        raw_text = str(event_data.get("text") or "").strip()
        if not links:
            return
        items = self._telegram_link_items(raw_text, links)
        for index, item in enumerate(items, start=1):
            item_event = dict(event_data)
            if len(items) > 1:
                item_event["link_label"] = f"第 {index}/{len(items)} 条"
            resource_type = resource_type_from_url(item["link"])
            label = str(item_event.get("link_label") or "").strip()
            item_event["progress_message"] = self._start_progress_message(
                item_event,
                f"【网盘订阅】{label + ' ' if label else ''}正在识别",
                f"正在识别{resource_type_name(resource_type, '网盘')}分享内容并校验转存流程。",
            )
            logger.info(
                f"Telegram 分享链接识别：进度={index}/{len(items)}，"
                f"类型={resource_type_name(resource_type, '未知')}，"
                f"标题={item['title'] or '从分享内容识别'}"
            )
            self._submit_remote_links(
                event_data=item_event,
                raw_links=item["link"],
                title=item["title"],
            )

    @staticmethod
    def _normalize_telegram_title(value: str) -> str:
        """移除分享访问码等非媒体文本，避免误送 TMDB 查询。"""
        cleaned = PluginEventHandler._ACCESS_CODE_PATTERN.sub(" ", str(value or ""))
        return " ".join(cleaned.split()).strip(" -—|｜,，;；")

    @staticmethod
    def _telegram_link_items(raw_text: str, links: list) -> list:
        """按消息行拆分链接，并为每条链接提取就近的可选媒体名称。"""
        if len(links) == 1:
            return [{
                "link": links[0],
                "title": PluginEventHandler._normalize_telegram_title(
                    raw_text.replace(links[0], " ")
                ),
            }]
        lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
        items = []
        for link in links:
            title = ""
            line_index = next(
                (index for index, line in enumerate(lines) if link in line),
                -1,
            )
            if line_index >= 0:
                line = lines[line_index]
                links_in_line = [value for value in links if value in line]
                if len(links_in_line) == 1:
                    title = PluginEventHandler._normalize_telegram_title(
                        line.replace(link, " ")
                    )
                if (
                        not title
                        and line_index > 0
                        and not any(value in lines[line_index - 1] for value in links)
                ):
                    title = PluginEventHandler._normalize_telegram_title(
                        lines[line_index - 1]
                    )
            items.append({"link": link, "title": title})
        return items

    def on_message_action(self, event: Event) -> None:
        """处理 Telegram TMDB 候选按钮回调。"""
        if (
                not event
                or not self._enabled
                or not self._direct_transfer_enabled
        ):
            return
        event_data = event.event_data or {}
        plugin_id = str(event_data.get("plugin_id") or "").strip().lower()
        own_plugin_id = self._owner.__class__.__name__.lower()
        if plugin_id and plugin_id != own_plugin_id:
            return
        parts = str(event_data.get("text") or "").strip().split("|")
        if len(parts) != 4 or parts[0] != "cl":
            return
        media_type = {"m": "movie", "t": "tv"}.get(parts[2])
        if not media_type or not parts[3].isdigit():
            return
        progress_message = {
            "message_id": event_data.get("original_message_id"),
            "chat_id": event_data.get("original_chat_id") or event_data.get("chat_id"),
            "channel": event_data.get("channel"),
            "source": event_data.get("source"),
            "metadata": event_data.get("metadata"),
        }
        submit_event = dict(event_data)
        if (
                progress_message["message_id"] is not None
                and progress_message["chat_id"] is not None
        ):
            submit_event["progress_message"] = progress_message
        self._edit_progress_message(
            submit_event,
            "【网盘订阅】正在提交资源",
            "已选择媒体，正在继续完整转存流程。",
            buttons=[],
        )
        Thread(
            target=self._submit_remote_links,
            kwargs={
                "event_data": submit_event,
                "selection_id": parts[1],
                "media_type": media_type,
                "tmdb_id": int(parts[3]),
            },
            daemon=True,
            name="pansearch-telegram-link-select",
        ).start()

    def _run_remote_checkin(
            self,
            event_data: dict,
            provider: str,
            mode: str,
    ) -> None:
        result = self.run_quick_checkin(
            provider=provider,
            mode=mode,
        )
        data = result.get("data") or {}
        lines = [str(result.get("message") or "签到失败")]
        for item in data.get("items") or []:
            record = item.get("data") or {}
            details = [
                str(record.get("status") or item.get("message") or "签到失败"),
            ]
            if record.get("points_change") is not None:
                points_label = "枫叶" if item.get("provider") == "p115" else "积分"
                details.append(f"{points_label} {int(record.get('points_change') or 0):+d}")
            if record.get("points_after") is not None:
                details.append(f"当前 {record.get('points_after')}")
            if record.get("lottery_target_count"):
                details.append(
                    f"转盘 {record.get('lottery_executed') or 0}/"
                    f"{record.get('lottery_target_count')} 次"
                )
            lines.append(f"{item.get('provider_name') or item.get('provider')}：{' · '.join(details)}")
        self._post_command_message(
            event_data,
            "【网盘订阅】签到结果",
            "\n".join(lines),
        )

    def _run_remote_auto_subscribe(self, event_data: dict) -> None:
        try:
            result = self.run_auto_subscribe(notify=False)
        except Exception as error:
            self._post_command_message(
                event_data,
                "【网盘订阅】榜单订阅失败",
                str(error),
            )
            return
        self._post_command_message(
            event_data,
            "【网盘订阅】榜单订阅结果",
            str(result.get("message") or "榜单自动订阅执行完成"),
        )

    @staticmethod
    def _format_checkin_history(result: dict) -> str:
        if not result.get("success"):
            return str(result.get("message") or "签到详情查询失败")
        channels = (result.get("data") or {}).get("channels") or []
        lines = []
        for channel in channels:
            provider = channel.get("provider") or ""
            name = channel.get("provider_name") or provider or "未知渠道"
            records = channel.get("items") or []
            if not records:
                lines.append(f"【{name}】{channel.get('total') or 0} 条，暂无记录")
                continue
            record = records[0]
            executed_at = str(record.get("executed_at") or "")
            executed_at = executed_at.split("T", 1)[-1][:5]
            details = ["成功" if record.get("success") else "失败"]
            status = str(record.get("status") or "")
            if status and status not in {
                "签到成功", "签到完成", "今日已签到", "签到失败", "签到未完成"
            }:
                details.append(status[:12])
            if record.get("points_change") is not None:
                points_label = "枫叶" if provider == "p115" else "积分"
                details.append(f"{points_label} {int(record.get('points_change') or 0):+d}")
            if not record.get("success") and record.get("message"):
                message = str(record.get("message"))
                if message != status:
                    details.append(message[:32])
            lines.append(
                f"【{name}】{channel.get('total') or 0} 条，最近 {executed_at} "
                f"{' '.join(details)}"
            )
        if not lines:
            return "暂无签到记录"
        text = "\n".join(lines)
        max_length = 3500
        if len(text) > max_length:
            text = text[:max_length - 16].rstrip() + "\n…（内容已截断）"
        return text

    def on_plugin_action(self, event: Event):
        """处理通用远程命令，耗时校验交给后台线程。"""
        if not event or not self._enabled:
            return
        event_data = event.event_data or {}
        action = str(event_data.get("action") or "")
        if not action.startswith("pansearch_"):
            return

        if action == "pansearch_status":
            overview = self.get_platform_overview(0)
            stats = {item["title"]: item["value"] for item in overview["stats"]}
            runtime = overview["runtime"]
            self._post_command_message(
                event_data,
                "【网盘订阅】运行状态",
                (
                    f"状态：{runtime.get('task') or runtime.get('status')}\n"
                    f"任务：{len(runtime.get('tasks') or [])} 个\n"
                    f"转存：总计 {stats.get('总转存', 0)}，今日 {stats.get('今日转存', 0)}，"
                    f"成功 {stats.get('成功', 0)}，失败 {stats.get('失败', 0)}"
                ),
            )
            return

        if action == "pansearch_sync":
            result = self.start_platform_sync()
            self._post_command_message(
                event_data,
                "【网盘订阅】任务提交",
                str(result.get("message") or "任务启动失败"),
            )
            return

        if action == "pansearch_auto_subscribe":
            Thread(
                target=self._run_remote_auto_subscribe,
                args=(dict(event_data),),
                daemon=True,
                name="pansearch-command-auto-subscribe",
            ).start()
            return

        if action == "pansearch_links":
            raw = str(event_data.get("arg_str") or "").strip()
            links = self.extract_resource_links(raw)
            if not links:
                self._post_command_message(
                    event_data,
                    "【网盘订阅】参数错误",
                    "格式：/cloud_link [订阅ID或媒体名称] 115分享、ED2K或Magnet链接",
                )
                return
            target_text = raw
            for link in links:
                target_text = target_text.replace(link, " ")
            target_parts = target_text.split()
            subscribe_id = int(target_parts.pop(0)) if target_parts and target_parts[0].isdigit() else 0
            title = " ".join(target_parts).strip()
            Thread(
                target=self._submit_remote_links,
                kwargs={
                    "event_data": dict(event_data),
                    "subscribe_id": subscribe_id,
                    "raw_links": "\n".join(links),
                    "title": title,
                },
                daemon=True,
                name="pansearch-command-links",
            ).start()
            self._post_command_message(
                event_data,
                "【网盘订阅】正在校验资源",
                "链接已接收，正在校验并提交。",
            )
            return

        if action == "pansearch_link_select":
            args = str(event_data.get("arg_str") or "").strip().split()
            selection_type, separator, tmdb_text = (
                args[1].lower().partition(":") if len(args) == 2 else ("", "", "")
            )
            if (
                    len(args) != 2
                    or not separator
                    or selection_type not in {"movie", "tv"}
                    or not tmdb_text.isdigit()
            ):
                self._post_command_message(
                    event_data,
                    "【网盘订阅】参数错误",
                    "格式：/cloud_link_select 选择ID movie:TMDB_ID",
                )
                return
            Thread(
                target=self._submit_remote_links,
                kwargs={
                    "event_data": dict(event_data),
                    "selection_id": args[0],
                    "media_type": selection_type,
                    "tmdb_id": int(tmdb_text),
                },
                daemon=True,
                name="pansearch-command-link-select",
            ).start()
            self._post_command_message(
                event_data,
                "【网盘订阅】正在提交资源",
                "已收到 TMDB 选择，正在继续完整转存流程。",
            )
            return

        if action == "pansearch_checkin":
            args = str(event_data.get("arg_str") or "").strip().lower().split()
            mode_aliases = {
                "normal": "normal", "普通": "normal",
                "gambler": "gambler", "赌狗": "gambler",
                "lucky": "lucky", "运气": "lucky",
            }
            mode = ""
            provider = ""
            for value in args:
                if value in mode_aliases and not mode:
                    mode = mode_aliases[value]
                elif not provider:
                    provider = value
                else:
                    self._post_command_message(
                        event_data,
                        "【网盘订阅】参数错误",
                        "格式：/cloud_checkin [渠道] [normal|gambler|lucky]",
                    )
                    return
            Thread(
                target=self._run_remote_checkin,
                args=(dict(event_data), provider, mode),
                daemon=True,
                name="pansearch-command-checkin",
            ).start()
            self._post_command_message(
                event_data,
                "【网盘订阅】正在签到",
                "签到请求已接收，完成后将发送结果。",
            )
            return

        if action == "pansearch_checkin_history":
            args = str(event_data.get("arg_str") or "").strip().lower().split()
            if len(args) > 2:
                self._post_command_message(
                    event_data,
                    "【网盘订阅】参数错误",
                    "格式：/cloud_checkin_history [渠道] [数量]",
                )
                return
            provider = ""
            limit = 10
            for value in args:
                if value.isdigit():
                    limit = max(1, min(int(value), 60))
                elif not provider:
                    provider = value
                else:
                    self._post_command_message(
                        event_data,
                        "【网盘订阅】参数错误",
                        "格式：/cloud_checkin_history [渠道] [数量]",
                    )
                    return
            result = self.list_checkin_details(provider=provider, limit=limit)
            self._post_command_message(
                event_data,
                "【网盘订阅】签到详情",
                self._format_checkin_history(result),
            )
            return

        if action == "pansearch_cache_clear":
            result = self.api_vue_clear_cache()
            self._post_command_message(
                event_data,
                "【网盘订阅】缓存清理",
                str(result.get("message") or "缓存清理失败"),
            )

    def on_resource_download(self, event: Event):
        """接管或拦截即将创建的平台资源下载。"""
        if not event or not self._enabled:
            return
        if not self._sync_handler:
            return

        event_data: ResourceDownloadEventData = event.event_data
        if not event_data:
            return

        # 处理平台资源下载事件（PT、RSS、刷流等）。
        context = event_data.context
        if not context:
            return

        torrent = context.torrent_info
        media = context.media_info
        meta = context.meta_info
        if not torrent or not media or not meta:
            return

        tmdbid = media.tmdb_id
        if not tmdbid:
            return

        # 查找匹配的订阅；电影没有 season 字段，必须按 TMDB 全量查询。
        season_list = meta.season_list or [1]
        if media_type := getattr(getattr(media, "type", None), "value", getattr(media, "type", None)):
            if media_type == MediaType.MOVIE.value:
                all_subs = list_subscribes_by_tmdb_id(
                    SubscribeOper(), tmdbid, None
                )
            else:
                all_subs = []
                for season in season_list:
                    all_subs.extend(list_subscribes_by_tmdb_id(
                        SubscribeOper(), tmdbid, season
                    ))
        else:
            all_subs = []
            for season in season_list:
                all_subs.extend(list_subscribes_by_tmdb_id(
                    SubscribeOper(), tmdbid, season
                ))

        if not all_subs:
            return

        # 接管时段内，按平台下载策略处理本插件负责的订阅。
        managed_subscribes = [
            subscribe for subscribe in all_subs
            if not self._is_subscribe_excluded(subscribe.id)
        ]
        all_plugin_managed = (
            bool(managed_subscribes)
            and self._is_takeover_active()
            and len(managed_subscribes) == len(all_subs)
        )
        episode_list = event_data.episodes or meta.episode_list or []
        policy = self._platform_download_policy

        if all_plugin_managed and policy == "allow":
            if self._has_pending_cloud_target(
                    managed_subscribes, season_list, episode_list
            ):
                event_data.cancel = True
                event_data.source = "PanSearch-重复资源拦截"
                event_data.reason = "同一订阅季集已有网盘任务正在处理，已阻止重复下载"
                logger.debug(f"已阻止平台重复下载：{torrent.title}")
            return

        is_tv = media_type == MediaType.TV.value
        can_match = bool(episode_list) if is_tv else True
        if all_plugin_managed and policy == "cloud":
            subscribe = managed_subscribes[0]
            takeover_success = False
            if can_match:
                try:
                    takeover_success = self._takeover_platform_download(
                        event_data=event_data,
                        subscribe=subscribe,
                        season=int(season_list[0] or 1),
                        episodes=sorted({int(value) for value in episode_list}),
                    )
                except Exception as error:
                    logger.error(f"接管平台资源下载异常：{error}")
            event_data.cancel = True
            event_data.source = "PanSearch-平台资源下载接管"
            event_data.reason = (
                "平台资源已提交插件离线下载"
                if takeover_success else "平台资源下载接管失败，已阻止平台下载"
            )
            return

        if all_plugin_managed and policy == "block":
            sub_name = all_subs[0].name if all_subs else "未知"
            event_data.cancel = True
            event_data.source = "PanSearch-平台资源下载拦截"
            event_data.reason = (
                f"订阅{sub_name}已由网盘搜索助手接管，"
                f"已拦截平台资源下载：{torrent.title}"
            )
            logger.debug(
                f"订阅接管已拦截平台资源下载：{sub_name}，{torrent.title}"
            )
            return
