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

    # ------------------ 订阅完成逻辑（原样保留） ------------------

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
            current_note = subscribe.note or []
            if mediainfo.type == MediaType.TV:
                new_note = list(set(current_note).union(set(success_episodes)))
            else:
                new_note = list(set(current_note).union({1}))

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

            update_data = {}
            if new_note != current_note:
                update_data["note"] = new_note
                logger.info(f"更新订阅 {subscribe.name} note 字段：{current_note} -> {new_note}")
            if new_lack != current_lack:
                update_data["lack_episode"] = new_lack
                logger.info(f"更新订阅 {subscribe.name} 缺失集数：{current_lack} -> {new_lack}")

            if update_data:
                SubscribeOper().update(subscribe.id, update_data)

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

    # ------------------ 站点/订阅sites更新：增强版 ------------------

    @staticmethod
    def _normalize_site_names(site_names: List[str]) -> List[str]:
        """清洗站点名：去空格、去重、过滤空值，保持原顺序"""
        if not site_names:
            return []
        out = []
        seen = set()
        for x in site_names:
            if x is None:
                continue
            s = str(x).strip()
            if not s:
                continue
            if s in seen:
                continue
            seen.add(s)
            out.append(s)
        return out

    @staticmethod
    def _get_site_ids_by_names(db, site_names: List[str]) -> Dict[str, int]:
        """
        根据站点名称列表查询 site.id
        返回 dict: {name: id}
        - 找不到的名称会跳过，并记录 warning
        """
        mapping: Dict[str, int] = {}
        if not site_names:
            return mapping

        for name in site_names:
            row = db.execute(
                text("SELECT id FROM site WHERE name = :name LIMIT 1"),
                {"name": name}
            ).fetchone()
            if row and row[0] is not None:
                mapping[name] = int(row[0])
            else:
                logger.warning(f"未找到站点记录：name={name}（将跳过）")
        return mapping

    @staticmethod
    def _ensure_115_site_id(db) -> int:
        """
        确保存在“115网盘”站点可用于订阅 sites。
        优先使用数据库里已有 name='115网盘' 的站点；
        若不存在，则按原作者方式插入 id=-1 的 115站点，并返回 -1。
        """
        row = db.execute(
            text("SELECT id FROM site WHERE name = :name LIMIT 1"),
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
            logger.info("已添加屏蔽站点记录 (id=-1, name=115网盘, is_active=True)")
        return -1

    @staticmethod
    def _guess_sites_storage_format(subscribes: List[Any]) -> str:
        """
        粗略判断 sites 字段存储格式：
        - list/json: 直接写 list
        - str: 写逗号字符串
        """
        for s in subscribes:
            try:
                v = getattr(s, "sites", None)
                if isinstance(v, str):
                    return "str"
                if isinstance(v, list):
                    return "list"
            except Exception:
                pass
        return "list"

    def apply_subscribe_sites_by_site_names(self, site_names: List[str], action_desc: str = ""):
        """
        把所有订阅（除排除订阅）sites 设置为给定站点名对应的 site.id 列表
        - ✅增强：解析不到任何站点时不写空 sites（避免系统订阅退化只跑单站）
        - ✅增强：打印 name->id 映射与最终 sites 列表
        """
        action_desc = action_desc or f"设置订阅sites={site_names}"
        exclude_ids = set(self._exclude_subscribes or [])

        site_names_norm = self._normalize_site_names(site_names)
        if not site_names_norm:
            logger.warning(f"{action_desc}：站点列表为空，跳过更新")
            return

        with SessionFactory() as db:
            mapping = self._get_site_ids_by_names(db, site_names_norm)
            # 按输入顺序生成 ids
            site_ids: List[int] = []
            for nm in site_names_norm:
                if nm in mapping:
                    site_ids.append(mapping[nm])

            # 去重保序
            seen = set()
            site_ids_uniq = []
            for x in site_ids:
                if x in seen:
                    continue
                seen.add(x)
                site_ids_uniq.append(x)

            logger.info(f"{action_desc}：站点映射 name->id = {mapping}")
            logger.info(f"{action_desc}：最终写入的 sites = {site_ids_uniq}")

            if not site_ids_uniq:
                # ✅关键兜底：不要写空
                logger.warning(f"{action_desc}：未解析到任何有效站点ID，跳过写入（避免订阅退化）")
                return

            subscribes = SubscribeOper(db=db).list() or []
            storage = self._guess_sites_storage_format(subscribes)

            updated = 0
            excluded = 0
            for s in subscribes:
                if s.id in exclude_ids:
                    excluded += 1
                    continue

                # 兼容不同格式
                if storage == "str":
                    value = ",".join(str(x) for x in site_ids_uniq)
                else:
                    value = site_ids_uniq

                SubscribeOper(db=db).update(s.id, {"sites": value})
                updated += 1

            if excluded:
                logger.info(f"{action_desc}：跳过 {excluded} 个排除订阅")
            logger.info(f"{action_desc}：已更新 {updated} 个订阅 sites={site_ids_uniq}")

    def set_unblocked_sites(self, unblocked_site_names: List[str]):
        """取消屏蔽时：按 UI/默认配置勾选订阅站点"""
        self.apply_subscribe_sites_by_site_names(unblocked_site_names, action_desc="取消屏蔽：勾选订阅站点")

    def set_blocked_sites_only_115(self):
        """恢复屏蔽时：只勾选 115网盘（若无则插入 id=-1）"""
        with SessionFactory() as db:
            site_id_115 = self._ensure_115_site_id(db)

            subscribes = SubscribeOper(db=db).list() or []
            storage = self._guess_sites_storage_format(subscribes)
            exclude_ids = set(self._exclude_subscribes or [])

            updated = 0
            excluded = 0
            for s in subscribes:
                if s.id in exclude_ids:
                    excluded += 1
                    continue

                if storage == "str":
                    value = str(site_id_115)
                else:
                    value = [site_id_115]

                SubscribeOper(db=db).update(s.id, {"sites": value})
                updated += 1

            if excluded:
                logger.info(f"恢复屏蔽：只勾选115网盘：跳过 {excluded} 个排除订阅")
            logger.info(f"恢复屏蔽：只勾选115网盘：已更新 {updated} 个订阅 sites={[site_id_115]}")
