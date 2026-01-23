"""
订阅处理模块
负责订阅状态检查、完成、站点更新等逻辑
"""
from typing import List, Callable, Optional, Dict, Any

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

    # 用于备份恢复 sites 的插件数据 key
    _BACKUP_KEY = "p115_block_sites_backup_v1"

    def __init__(
        self,
        exclude_subscribes: List[int] = None,
        notify: bool = False,
        post_message_func: Callable = None,
        get_data_func: Callable = None,
        save_data_func: Callable = None,
    ):
        """
        初始化订阅处理器

        :param exclude_subscribes: 排除的订阅ID列表
        :param notify: 是否发送通知
        :param post_message_func: 发送消息的函数
        :param get_data_func: 读取插件数据的方法（PluginBase.get_data）
        :param save_data_func: 保存插件数据的方法（PluginBase.save_data）
        """
        self._exclude_subscribes = exclude_subscribes or []
        self._notify = notify
        self._post_message = post_message_func
        self._get_data = get_data_func
        self._save_data = save_data_func

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
            current_lack = subscribe.lack_episode or 0
            total_episode = subscribe.total_episode or 0
            start_episode = subscribe.start_episode or 1

            if mediainfo.type == MediaType.TV and total_episode > 0:
                expected_episodes = set(range(start_episode, total_episode + 1))
                downloaded_episodes = set(new_note)
                remaining_episodes = expected_episodes - downloaded_episodes
                new_lack = len(remaining_episodes)
            else:
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

                meta = MetaInfo(subscribe.name)
                meta.year = subscribe.year
                meta.begin_season = subscribe.season or None
                try:
                    meta.type = MediaType(subscribe.type)
                except ValueError:
                    logger.error(f'订阅 {subscribe.name} 类型错误：{subscribe.type}')
                    return

                try:
                    SubscribeChain().finish_subscribe_or_not(
                        subscribe=subscribe,
                        meta=meta,
                        mediainfo=mediainfo,
                        downloads=None,
                        lefts={},
                        force=True
                    )
                    logger.info(f"订阅 {subscribe.name} 已完成并移至历史记录")
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
        屏蔽/恢复系统订阅（遵循原作者思路：sites=[-1] + site(id=-1)）

        ✅增强点：
        - 屏蔽时备份每个订阅原 sites
        - 恢复时按备份还原原 sites，避免把用户配置洗成 []
        """
        try:
            sites_value = [-1] if block else []
            action = "屏蔽" if block else "恢复"
            exclude_ids = set(self._exclude_subscribes or [])

            with SessionFactory() as db:
                subscribes = SubscribeOper(db=db).list()

                # 1) 更新所有订阅的 sites 字段
                updated_count = 0
                excluded_count = 0

                if subscribes:
                    if block:
                        # === 屏蔽：备份原 sites ===
                        if not self._save_data:
                            raise RuntimeError("缺少 save_data_func，无法备份 sites（请在插件里注入 get_data/save_data）")

                        backup: Dict[str, Any] = {}
                        for subscribe in subscribes:
                            if subscribe.id in exclude_ids:
                                excluded_count += 1
                                continue
                            backup[str(subscribe.id)] = subscribe.sites or []

                        self._save_data(self._BACKUP_KEY, backup)
                        logger.info(f"已备份 {len(backup)} 个订阅的原 sites 到插件数据：{self._BACKUP_KEY}")

                        # === 屏蔽：强制 sites=[-1] ===
                        for subscribe in subscribes:
                            if subscribe.id in exclude_ids:
                                continue
                            SubscribeOper(db=db).update(subscribe.id, {"sites": sites_value})
                            updated_count += 1

                    else:
                        # === 恢复：读取备份并还原 ===
                        backup = self._get_data(self._BACKUP_KEY) if self._get_data else None
                        backup = backup or {}

                        for subscribe in subscribes:
                            if subscribe.id in exclude_ids:
                                excluded_count += 1
                                continue

                            old_sites = backup.get(str(subscribe.id))
                            if old_sites is None:
                                # 没备份到的订阅：保守恢复为空
                                old_sites = []
                            SubscribeOper(db=db).update(subscribe.id, {"sites": old_sites})
                            updated_count += 1

                        # 清理备份
                        if self._save_data:
                            self._save_data(self._BACKUP_KEY, None)

                    if excluded_count:
                        logger.info(f"跳过 {excluded_count} 个排除订阅")
                    logger.info(f"系统订阅{action}完成，已更新 {updated_count} 个订阅的 sites 字段")

                # 2) 添加或删除 id=-1 的站点记录（保持原作者逻辑）
                if block:
                    existing = Site.get(db, -1)
                    if not existing:
                        db.execute(
                            text(
                                "INSERT INTO site (id, name, url, is_active, limit_interval, limit_count, limit_seconds, timeout) "
                                "VALUES (:id, :name, :url, :is_active, :limit_interval, :limit_count, :limit_seconds, :timeout)"
                            ),
                            {
                                "id": -1,
                                "name": "115网盘",
                                "url": "https://115.com",
                                "is_active": True,
                                "limit_interval": 10000000,
                                "limit_count": 1,
                                "limit_seconds": 10000000,
                                "timeout": 1
                            }
                        )
                        db.commit()
                        logger.info("已添加屏蔽站点记录 (id=-1, name=115网盘, is_active=True)")
                    else:
                        logger.info("屏蔽站点记录已存在，跳过添加")
                else:
                    existing = Site.get(db, -1)
                    if existing:
                        Site.delete(db, -1)
                        logger.info("已删除屏蔽站点记录 (id=-1)")
                    else:
                        logger.info("屏蔽站点记录不存在，跳过删除")

        except Exception as e:
            logger.error(f"更新屏蔽站点记录失败: {e}")
