"""
订阅处理模块
负责订阅状态检查、完成、站点更新等逻辑
"""
import ast
from threading import RLock
from typing import Callable, List

from app.chain.subscribe import SubscribeChain
from app.core.metainfo import MetaInfo
from app.db import SessionFactory
from app.db.subscribe_oper import SubscribeOper
from app.db.systemconfig_oper import SystemConfigOper
from app.helper.message import TemplateHelper
from app.log import logger
from app.schemas import MediaInfo
from app.schemas.types import MediaType, SystemConfigKey


class SubscribeCompletionChain(SubscribeChain):
    """逐字段渲染完成通知，避免媒体文本中的引号破坏模板 JSON。"""

    def post_message(
            self,
            message=None,
            meta=None,
            mediainfo=None,
            torrentinfo=None,
            transferinfo=None,
            **kwargs
    ):
        if message and not (message.title or message.text) and message.ctype:
            try:
                templates = SystemConfigOper().get(SystemConfigKey.NotificationTemplates) or {}
                template = templates.get(message.ctype.value)
                fields = ast.literal_eval(template) if isinstance(template, str) else template
                if not isinstance(fields, dict):
                    raise ValueError("通知模板不是字典")
                context = TemplateHelper().builder.build(
                    meta=meta,
                    mediainfo=mediainfo,
                    torrentinfo=torrentinfo,
                    transferinfo=transferinfo,
                    **kwargs
                )
                for key, value in fields.items():
                    if hasattr(message, key):
                        setattr(message, key, TemplateHelper.render_with_context(str(value), context))
                if not message.title and not message.text:
                    raise ValueError("通知标题和内容同时为空")
            except Exception as err:
                logger.warning(f"订阅完成通知模板逐字段渲染失败，将使用简化标题：{err}")
                title_year = getattr(mediainfo, "title_year", None) or getattr(mediainfo, "title", "")
                message.title = f"{title_year} 已完成{kwargs.get('msgstr', '订阅')}"

        return super().post_message(
            message=message,
            meta=meta,
            mediainfo=mediainfo,
            torrentinfo=torrentinfo,
            transferinfo=transferinfo,
            **kwargs
        )


class SubscribeHandler:
    """订阅处理器"""

    def __init__(
            self,
            exclude_subscribes: List[int] = None,
            is_excluded_func: Callable[[int], bool] = None
    ):
        """
        :param exclude_subscribes: 排除的订阅ID列表（is_excluded_func 未提供时使用）
        :param is_excluded_func: 订阅过滤判断函数，支持排除/指定两种模式
        """
        self._exclude_subscribes = exclude_subscribes or []
        self._is_excluded_func = is_excluded_func
        self._progress_lock = RLock()

    def _is_excluded(self, subscribe_id: int) -> bool:
        """判断订阅是否不归本插件处理"""
        if self._is_excluded_func:
            return bool(self._is_excluded_func(subscribe_id))
        return subscribe_id in set(self._exclude_subscribes or [])

    def _update_progress_locked(
            self,
            subscribe,
            mediainfo: MediaInfo,
            success_episodes: List[int],
    ):
        """在持锁状态下合并进度，返回最新订阅和剩余缺集数。"""
        subscribe_id = int(getattr(subscribe, "id", 0) or 0)
        if subscribe_id > 0:
            with SessionFactory() as db:
                latest_subscribe = SubscribeOper(db=db).get(subscribe_id)
            if not latest_subscribe:
                logger.info(f"更新订阅进度时订阅已不存在：{subscribe_id}")
                return None
            subscribe = latest_subscribe

        current_note = {
            int(episode) for episode in (subscribe.note or [])
            if str(episode).isdigit()
        }
        succeeded = {
            int(episode) for episode in (success_episodes or [])
            if str(episode).isdigit()
        }
        progress_episodes = (
            succeeded
            if mediainfo.type == MediaType.TV
            else ({1} if succeeded else set())
        )
        new_note = sorted(current_note | progress_episodes)
        current_lack = int(subscribe.lack_episode or 0)
        total_episode = int(subscribe.total_episode or 0)
        start_episode = int(subscribe.start_episode or 1)

        if mediainfo.type == MediaType.TV and total_episode > 0:
            expected_episodes = set(range(start_episode, total_episode + 1))
            new_lack = len(expected_episodes - set(new_note))
        elif mediainfo.type == MediaType.MOVIE and succeeded:
            new_lack = 0
        else:
            new_lack = current_lack

        update_data = {}
        if set(new_note) != current_note:
            update_data["note"] = new_note
            logger.info(
                f"更新订阅 {subscribe.name} note："
                f"{sorted(current_note)} -> {new_note}"
            )
        if new_lack != current_lack:
            update_data["lack_episode"] = new_lack
            logger.debug(
                f"更新订阅 {subscribe.name} 缺失集数："
                f"{current_lack} -> {new_lack}"
            )
        if update_data:
            if subscribe_id > 0:
                SubscribeOper().update(subscribe.id, update_data)
            for key, value in update_data.items():
                setattr(subscribe, key, value)
        return subscribe, new_lack

    def update_subscribe_progress(
            self,
            subscribe,
            mediainfo: MediaInfo,
            success_episodes: List[int],
    ):
        """只更新订阅进度，不触发完成迁移，供洗版流程复用。"""
        with self._progress_lock:
            try:
                result = self._update_progress_locked(
                    subscribe, mediainfo, success_episodes
                )
                return result[1] if result else None
            except Exception as error:
                logger.error(
                    f"更新订阅进度失败 - 订阅ID:{getattr(subscribe, 'id', None)} "
                    f"名称:{getattr(subscribe, 'name', None)}：{error}"
                )
                return None

    # 平台订阅完成处理

    def check_and_finish_subscribe(
            self,
            subscribe,
            mediainfo: MediaInfo,
            success_episodes: List[int]
    ):
        """
        合并订阅进度；完成时调用官方订阅链迁移。
        """
        with self._progress_lock:
            try:
                progress = self._update_progress_locked(
                    subscribe, mediainfo, success_episodes
                )
                if not progress:
                    return None
                subscribe, new_lack = progress
                if new_lack != 0:
                    return new_lack
                if bool(getattr(subscribe, "best_version", False)):
                    logger.info(
                        f"订阅 {subscribe.name} 当前版本已完成，"
                        "保留订阅继续洗版"
                    )
                    return 0
                logger.debug(f"订阅 {subscribe.name} 已完成，准备移至历史记录")

                meta = MetaInfo(subscribe.name)
                meta.year = subscribe.year
                meta.begin_season = subscribe.season or None
                try:
                    meta.type = MediaType(subscribe.type)
                except ValueError:
                    logger.error(f'订阅 {subscribe.name} 类型错误：{subscribe.type}')
                    return None

                try:
                    SubscribeCompletionChain().finish_subscribe_or_not(
                        subscribe=subscribe,
                        meta=meta,
                        mediainfo=mediainfo,
                        downloads=None,
                        lefts={},
                        force=True
                    )
                    logger.info(f"订阅 {subscribe.name} 已移至历史记录")
                    return 0
                except Exception as e:
                    import traceback
                    logger.error(
                        f"完成订阅时出错 - 订阅ID:{subscribe.id} 名称:{subscribe.name} "
                        f"异常:{type(e).__name__}:{e}\n{traceback.format_exc()}"
                    )
                    return None
            except Exception as e:
                import traceback
                logger.error(
                    f"检查订阅完成状态出错 - 订阅ID:{getattr(subscribe, 'id', None)} "
                    f"名称:{getattr(subscribe, 'name', None)} "
                    f"异常:{type(e).__name__}:{e}\n{traceback.format_exc()}"
                )
                return None
