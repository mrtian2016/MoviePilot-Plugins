"""
订阅处理模块
负责订阅状态检查、完成、站点更新等逻辑
"""
from typing import List, Optional, Callable

from sqlalchemy import text

from app.core.metainfo import MetaInfo
from app.chain.subscribe import SubscribeChain
from app.db import SessionFactory
from app.db.subscribe_oper import SubscribeOper
from app.db.models.site import Site
from app.log import logger
from app.schemas import MediaInfo
from app.schemas.types import MediaType, NotificationType


class SubscribeHandler:
    """订阅处理器"""

    def __init__(
        self,
        exclude_subscribes: List[int] = None,
        notify: bool = False,
        post_message_func: Callable = None
    ):
        """
        初始化订阅处理器

        :param exclude_subscribes: 排除的订阅ID列表
        :param notify: 是否发送通知
        :param post_message_func: 发送消息的函数
        """
        self._exclude_subscribes = exclude_subscribes or []
        self._notify = notify
        self._post_message = post_message_func

    def check_and_finish_subscribe(
        self,
        subscribe,
        mediainfo: MediaInfo,
        success_episodes: List[int]
    ):
        """
        检查订阅是否完成，如果完成则调用官方接口

        :param subscribe: 订阅对象
        :param mediainfo: 媒体信息
        :param success_episodes: 本次成功转存的集数列表（电影为[1]）
        """
        try:
            # 1. 更新 note 字段（记录已下载集数，与系统订阅检查兼容）
            # 系统会读取 note 字段来判断哪些集已下载，避免 lack_episode 被重置
            current_note = subscribe.note or []
            if mediainfo.type == MediaType.TV:
                new_note = list(set(current_note).union(set(success_episodes)))
            else:
                # 电影用 [1] 表示已下载
                new_note = list(set(current_note).union({1}))

            # 2. 更新缺失集数
            # 根据已下载集数和总集数计算新的缺失集数
            current_lack = subscribe.lack_episode or 0
            total_episode = subscribe.total_episode or 0
            start_episode = subscribe.start_episode or 1
            
            if mediainfo.type == MediaType.TV and total_episode > 0:
                # 计算实际缺失：总集数 - 开始集数之前的集数 - 已下载集数（note中的）
                expected_episodes = set(range(start_episode, total_episode + 1))
                downloaded_episodes = set(new_note)
                remaining_episodes = expected_episodes - downloaded_episodes
                new_lack = len(remaining_episodes)
            else:
                # 电影或无法计算时，使用简单减法
                new_lack = max(0, current_lack - len(success_episodes))

            # 3. 一次性更新 note 和 lack_episode
            update_data = {}
            if new_note != current_note:
                update_data["note"] = new_note
                logger.info(f"更新订阅 {subscribe.name} note 字段：{current_note} -> {new_note}")
            if new_lack != current_lack:
                update_data["lack_episode"] = new_lack
                logger.info(f"更新订阅 {subscribe.name} 缺失集数：{current_lack} -> {new_lack}")

            if update_data:
                SubscribeOper().update(subscribe.id, update_data)

            # 检查是否完成
            if new_lack == 0:
                logger.info(f"订阅 {subscribe.name} 所有内容已转存完成，准备完成订阅")

                # 生成元数据
                meta = MetaInfo(subscribe.name)
                meta.year = subscribe.year
                meta.begin_season = subscribe.season or None
                try:
                    meta.type = MediaType(subscribe.type)
                except ValueError:
                    logger.error(f'订阅 {subscribe.name} 类型错误：{subscribe.type}')
                    return

                # 调用官方完成订阅接口
                try:
                    SubscribeChain().finish_subscribe_or_not(
                        subscribe=subscribe,
                        meta=meta,
                        mediainfo=mediainfo,
                        downloads=None,  # 我们已经更新了 lack_episode
                        lefts={},        # 没有剩余集数
                        force=True       # 强制完成
                    )
                    logger.info(f"订阅 {subscribe.name} 已完成并移至历史记录")
                    # 发送订阅完成通知
                    if self._notify and self._post_message:
                        season_text = f" 第{subscribe.season}季" if subscribe.type == MediaType.TV.value and subscribe.season else ""
                        self._post_message(
                            mtype=NotificationType.Plugin,
                            title="【115网盘订阅追更】订阅完成",
                            text=f"{subscribe.name}{season_text} 所有内容已转存完成，订阅已移至历史记录。"
                        )
                except Exception as e:
                    import traceback
                    logger.error(
                        f"检查订阅完成状态时出错 - "
                        f"订阅ID: {subscribe.id}, 订阅名: {subscribe.name}, "
                        f"异常类型: {type(e).__name__}, 异常消息: {e}\n"
                        f"堆栈跟踪:\n{traceback.format_exc()}"
                    )

        except Exception as e:
            import traceback
            logger.error(
                f"检查订阅完成状态时出错 - "
                f"订阅ID: {subscribe.id}, 订阅名: {subscribe.name}, "
                f"异常类型: {type(e).__name__}, 异常消息: {e}\n"
                f"堆栈跟踪:\n{traceback.format_exc()}"
            )

    def update_subscribe_sites(self, block: bool):
        """
        屏蔽/恢复系统订阅

        :param block: True 表示屏蔽（添加id=-1的115网盘站点，并更新订阅sites为[-1]），
                      False 表示恢复（删除该记录，并恢复订阅sites为[]）
        """
        try:
            sites_value = [-1] if block else []
            action = "屏蔽" if block else "恢复"

            with SessionFactory() as db:
                # 1. 更新所有订阅的sites字段
                subscribes = SubscribeOper(db=db).list()
                updated_count = 0
                excluded_count = 0
                if subscribes:
                    for subscribe in subscribes:
                        if subscribe.id in self._exclude_subscribes:
                            excluded_count += 1
                            continue
                        SubscribeOper(db=db).update(subscribe.id, {"sites": sites_value})
                        updated_count += 1
                    if excluded_count:
                        logger.info(f"跳过 {excluded_count} 个排除订阅")
                    logger.info(f"系统订阅{action}完成，已更新 {updated_count} 个订阅的sites字段为 {sites_value}")

                # 2. 添加或删除id=-1的站点记录
                if block:
                    # 开启屏蔽：添加id=-1的站点记录
                    existing = Site.get(db, -1)
                    if not existing:
                        # 使用原生SQL插入，因为需要指定id为-1
                        db.execute(
                            text(
                                "INSERT INTO site (id, name, url, is_active, limit_interval, limit_count, limit_seconds, timeout) VALUES (:id, :name, :url, :is_active, :limit_interval ,:limit_count, :limit_seconds, :timeout)"
                            ),
                            {"id": -1, "name": "115网盘", "url": "https://115.com", "is_active": True, "limit_interval": 10000000, "limit_count": 1, "limit_seconds": 10000000, "timeout": 1}
                        )
                        db.commit()
                        logger.info("已添加屏蔽站点记录 (id=-1, name=115网盘, is_active=True)")
                    else:
                        logger.info("屏蔽站点记录已存在，跳过添加")
                else:
                    # 关闭屏蔽：删除id=-1的站点记录
                    existing = Site.get(db, -1)
                    if existing:
                        Site.delete(db, -1)
                        logger.info("已删除屏蔽站点记录 (id=-1)")
                    else:
                        logger.info("屏蔽站点记录不存在，跳过删除")

        except Exception as e:
            logger.error(f"更新屏蔽站点记录失败: {e}")
