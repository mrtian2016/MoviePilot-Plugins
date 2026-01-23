"""
订阅处理模块
负责订阅状态检查、完成、站点更新等逻辑
"""
from typing import List, Optional, Callable, Dict, Any

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
            current_note = subscribe.note or []
            if mediainfo.type == MediaType.TV:
                new_note = list(set(current_note).union(set(success_episodes)))
            else:
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

                # 调用官方完成订阅接口
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

    # ===================== 新增：站点 sites 相关 =====================

    @staticmethod
    def _fetch_site_id_map(db) -> Dict[str, int]:
        """从 site 表读取 name->id 映射"""
        rows = db.execute(text("SELECT id, name FROM site")).fetchall()
        m: Dict[str, int] = {}
        for r in rows:
            try:
                sid = int(r[0])
                name = str(r[1])
                m[name] = sid
            except Exception:
                continue
        return m

    @staticmethod
    def _ensure_115_site_id(db) -> int:
        """
        确保存在“115网盘”站点 id
        - 先查 name='115网盘'
        - 找不到则按原作者方式插入 id=-1
        """
        row = db.execute(
            text("SELECT id FROM site WHERE name=:name LIMIT 1"),
            {"name": "115网盘"}
        ).fetchone()
        if row and row[0] is not None:
            return int(row[0])

        existing = Site.get(db, -1)
        if not existing:
            db.execute(
                text(
                    "INSERT INTO site (id, name, url, is_active, limit_interval, limit_count, limit_seconds, timeout) "
                    "VALUES (:id, :name, :url, :is_active, :limit_interval ,:limit_count, :limit_seconds, :timeout)"
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
            logger.info("已添加 115 网盘站点记录 (id=-1, name=115网盘)")
        return -1

    def _update_all_subscribe_sites(self, db, site_ids: List[int], action: str):
        """把所有订阅（除排除）sites 更新为 site_ids"""
        exclude_ids = set(self._exclude_subscribes or [])
        subscribes = SubscribeOper(db=db).list() or []

        updated = 0
        excluded = 0
        for s in subscribes:
            if s.id in exclude_ids:
                excluded += 1
                continue
            SubscribeOper(db=db).update(s.id, {"sites": site_ids})
            updated += 1

        logger.info(f"{action}：已更新 {updated} 个订阅 sites={site_ids}（跳过 {excluded} 个排除订阅）")

    def _try_update_global_subscribe_site_range(self, db, site_ids: List[int]):
        """
        尝试同步“全局订阅站点范围”（用于让 MP UI 的“设定->订阅->订阅站点”显示变化）
        不同版本 MP 的 key 可能不同，因此这里做多策略兼容：
        1) SystemConfigOper + SystemConfigKey 自动遍历
        2) 直接 SQL 尝试更新 systemconfig 表中可能的 key
        """
        # --- 方案1：SystemConfigOper + SystemConfigKey ---
        updated_keys = []
        try:
            from app.db.systemconfig_oper import SystemConfigOper
            from app.core.config import SystemConfigKey

            # 枚举所有包含 Subscribe + Site 的 key
            cand = []
            for n in dir(SystemConfigKey):
                if "Subscribe" in n and ("Site" in n or "Sites" in n):
                    try:
                        cand.append(getattr(SystemConfigKey, n))
                    except Exception:
                        pass

            # 去重
            uniq = []
            seen = set()
            for k in cand:
                if k in seen:
                    continue
                seen.add(k)
                uniq.append(k)

            for k in uniq:
                try:
                    ok = SystemConfigOper().set(k, site_ids)
                    if ok:
                        updated_keys.append(str(k))
                except Exception:
                    continue

            if updated_keys:
                logger.info(f"已同步全局订阅站点范围（SystemConfigKey）：{updated_keys} -> {site_ids}")
                return
        except Exception:
            pass

        # --- 方案2：SQL 直接更新 systemconfig ---
        # 仅做 best-effort，不保证所有版本都有此表/字段
        possible_keys = [
            "SubscribeSites",
            "SubscribeSite",
            "SubscribeSiteIds",
            "SubscribeSitesRange",
            "SubscribeSearchSites",
            "SubscribeSearchSiteIds",
        ]
        try:
            # 尝试看看 systemconfig 表是否存在
            rows = db.execute(text("SELECT key, value FROM systemconfig")).fetchmany(5)
            _ = rows  # 只为触发异常
        except Exception:
            return

        for k in possible_keys:
            try:
                db.execute(
                    text("UPDATE systemconfig SET value=:value WHERE key=:key"),
                    {"key": k, "value": site_ids}
                )
                db.commit()
                logger.info(f"已尝试同步全局订阅站点范围：systemconfig.{k}={site_ids}")
            except Exception:
                continue

    def set_sites_for_unblock(self, site_names: List[str]):
        """
        取消屏蔽时：按站点名称列表设置订阅 sites
        """
        with SessionFactory() as db:
            name_to_id = self._fetch_site_id_map(db)
            ids = []
            missing = []

            for n in site_names or []:
                if n in name_to_id:
                    ids.append(name_to_id[n])
                else:
                    missing.append(n)

            # 去重保序
            seen = set()
            site_ids = []
            for x in ids:
                if x not in seen:
                    seen.add(x)
                    site_ids.append(x)

            logger.info(f"取消屏蔽：站点名->id 映射：{ {n: name_to_id.get(n) for n in site_names} }")
            if missing:
                logger.warning(f"取消屏蔽：以下站点名在数据库 site 表中未找到，将被忽略：{missing}")

            self._update_all_subscribe_sites(db, site_ids, action=f"取消屏蔽：勾选订阅站点({site_names})")
            self._try_update_global_subscribe_site_range(db, site_ids)

    def set_sites_for_block_only_115(self):
        """
        恢复屏蔽时：只勾选 115网盘
        """
        with SessionFactory() as db:
            sid_115 = self._ensure_115_site_id(db)
            logger.info(f"恢复屏蔽：115网盘 site_id={sid_115}")
            self._update_all_subscribe_sites(db, [sid_115], action="恢复屏蔽：只勾选 115网盘")
            self._try_update_global_subscribe_site_range(db, [sid_115])

