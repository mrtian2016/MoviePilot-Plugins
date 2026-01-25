"""
订阅处理模块
负责订阅状态检查、完成、站点更新等逻辑（v1.2.5）
"""
from typing import List, Callable, Dict, Any
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
        self._exclude_subscribes = exclude_subscribes or []
        self._notify = notify
        self._post_message = post_message_func

    # ------------------ 订阅完成逻辑（完整保留） ------------------

    def check_and_finish_subscribe(
        self,
        subscribe,
        mediainfo: MediaInfo,
        success_episodes: List[int]
    ):
        """
        检查订阅是否完成，如果完成则调用官方接口
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
                logger.info(f"更新订阅 {subscribe.name} note：{current_note} -> {new_note}")
            if new_lack != current_lack:
                update_data["lack_episode"] = new_lack
                logger.info(f"更新订阅 {subscribe.name} 缺失集数：{current_lack} -> {new_lack}")

            if update_data:
                SubscribeOper().update(subscribe.id, update_data)

            if new_lack == 0:
                logger.info(f"订阅 {subscribe.name} 已完成，准备移至历史记录")

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
                    logger.info(f"订阅 {subscribe.name} 已移至历史记录")
                    if self._notify and self._post_message:
                        season_text = f" 第{subscribe.season}季" if subscribe.type == MediaType.TV.value and subscribe.season else ""
                        self._post_message(
                            mtype=NotificationType.Plugin,
                            title="【115网盘订阅增强】订阅完成",
                            text=f"{subscribe.name}{season_text} 已完成，订阅已移至历史记录。"
                        )
                except Exception as e:
                    import traceback
                    logger.error(
                        f"完成订阅时出错 - 订阅ID:{subscribe.id} 名称:{subscribe.name} "
                        f"异常:{type(e).__name__}:{e}\n{traceback.format_exc()}"
                    )

        except Exception as e:
            import traceback
            logger.error(
                f"检查订阅完成状态出错 - 订阅ID:{getattr(subscribe, 'id', None)} 名称:{getattr(subscribe, 'name', None)} "
                f"异常:{type(e).__name__}:{e}\n{traceback.format_exc()}"
            )

    # ------------------ 站点写入增强 ------------------

    @staticmethod
    def _normalize_site_names(site_names: List[str]) -> List[str]:
        if not site_names:
            return []
        out, seen = [], set()
        for x in site_names:
            s = str(x).strip() if x is not None else ""
            if not s or s in seen:
                continue
            seen.add(s)
            out.append(s)
        return out

    @staticmethod
    def _get_site_ids_by_names(db, site_names: List[str]) -> Dict[str, int]:
        mapping: Dict[str, int] = {}
        for name in site_names:
            row = db.execute(text("SELECT id FROM site WHERE name=:name LIMIT 1"), {"name": name}).fetchone()
            if row and row[0] is not None:
                mapping[name] = int(row[0])
            else:
                logger.warning(f"未找到站点记录：name={name}（将跳过）")
        return mapping

    @staticmethod
    def _ensure_115_site_id(db) -> int:
        row = db.execute(text("SELECT id FROM site WHERE name=:name LIMIT 1"), {"name": "115网盘"}).fetchone()
        if row and row[0] is not None:
            return int(row[0])

        existing = Site.get(db, -1)
        if not existing:
            db.execute(
                text(
                    "INSERT INTO site (id, name, url, is_active, limit_interval, limit_count, limit_seconds, timeout) "
                    "VALUES (:id,:name,:url,:is_active,:limit_interval,:limit_count,:limit_seconds,:timeout)"
                ),
                {
                    "id": -1, "name": "115网盘", "url": "https://115.com", "is_active": True,
                    "limit_interval": 10000000, "limit_count": 1, "limit_seconds": 10000000, "timeout": 1
                }
            )
            db.commit()
            logger.info("已添加站点记录：115网盘(id=-1)")
        return -1

    @staticmethod
    def _guess_sites_storage_format_from_rows(rows: List[Any]) -> str:
        for v in rows:
            if isinstance(v, str):
                return "str"
            if isinstance(v, list):
                return "list"
        return "list"

    @staticmethod
    def _guess_sites_storage_format_for_subscribe(db, subscribe_id: int) -> str:
        row = db.execute(
            text("SELECT sites FROM subscribe WHERE id=:id LIMIT 1"),
            {"id": int(subscribe_id)}
        ).fetchone()
        if not row:
            return "list"
        v = row[0]
        if isinstance(v, str):
            return "str"
        if isinstance(v, list):
            return "list"
        return "list"

    def apply_subscribe_sites_by_site_names(self, site_names: List[str], action_desc: str = "") -> List[int]:
        action_desc = action_desc or f"设置订阅sites={site_names}"
        exclude_ids = set(self._exclude_subscribes or [])
        site_names_norm = self._normalize_site_names(site_names)

        if not site_names_norm:
            logger.warning(f"{action_desc}：站点列表为空，跳过")
            return []

        with SessionFactory() as db:
            mapping = self._get_site_ids_by_names(db, site_names_norm)
            site_ids = []
            for nm in site_names_norm:
                if nm in mapping:
                    site_ids.append(mapping[nm])

            seen = set()
            site_ids_uniq = []
            for x in site_ids:
                if x in seen:
                    continue
                seen.add(x)
                site_ids_uniq.append(x)

            logger.info(f"{action_desc}：站点映射 name->id = {mapping}")
            logger.info(f"{action_desc}：最终写入 sites = {site_ids_uniq}")

            if not site_ids_uniq:
                logger.warning(f"{action_desc}：未解析到有效站点ID，跳过写入（保持原状）")
                return []

            subscribes = SubscribeOper(db=db).list() or []
            sample_sites = []
            for s in subscribes[:5]:
                try:
                    sample_sites.append(getattr(s, "sites", None))
                except Exception:
                    pass
            storage = self._guess_sites_storage_format_from_rows(sample_sites)

            updated, excluded = 0, 0
            for s in subscribes:
                if s.id in exclude_ids:
                    excluded += 1
                    continue
                value = ",".join(str(x) for x in site_ids_uniq) if storage == "str" else site_ids_uniq
                SubscribeOper(db=db).update(s.id, {"sites": value})
                updated += 1

            logger.info(f"{action_desc}：已更新 {updated} 个订阅（跳过 {excluded} 个排除订阅）")
            return site_ids_uniq

    def set_unblocked_sites(self, unblocked_site_names: List[str]) -> List[int]:
        return self.apply_subscribe_sites_by_site_names(
            unblocked_site_names,
            action_desc="已恢复系统订阅：全量订阅站点同步"
        )

    def set_blocked_sites_only_115(self) -> List[int]:
        with SessionFactory() as db:
            site_id_115 = self._ensure_115_site_id(db)

            subscribes = SubscribeOper(db=db).list() or []
            sample_sites = []
            for s in subscribes[:5]:
                try:
                    sample_sites.append(getattr(s, "sites", None))
                except Exception:
                    pass
            storage = self._guess_sites_storage_format_from_rows(sample_sites)

            exclude_ids = set(self._exclude_subscribes or [])
            updated, excluded = 0, 0
            for s in subscribes:
                if s.id in exclude_ids:
                    excluded += 1
                    continue
                value = str(site_id_115) if storage == "str" else [site_id_115]
                SubscribeOper(db=db).update(s.id, {"sites": value})
                updated += 1

            logger.info(f"已屏蔽系统订阅：全量订阅仅115网盘（已更新 {updated} 个，跳过 {excluded} 个排除订阅）")
            return [site_id_115]

    # ------------------ 新增订阅站点写入（事件兜底用） ------------------

    def set_sites_for_subscribe_only_115(self, subscribe_id: int) -> List[int]:
        """
        新增订阅写入：仅115
        - v1.2.5：仅用于 SubscribeAdded（新订阅兜底）
        """
        with SessionFactory() as db:
            site_id_115 = self._ensure_115_site_id(db)
            storage = self._guess_sites_storage_format_for_subscribe(db, int(subscribe_id))
            value = str(site_id_115) if storage == "str" else [site_id_115]
            SubscribeOper(db=db).update(int(subscribe_id), {"sites": value})
            logger.info(f"已屏蔽系统订阅：检测到新增订阅，准备拉回仅115（subscribe_id={subscribe_id}）")
            return [site_id_115]

    def set_sites_for_subscribe_by_names(self, subscribe_id: int, site_names: List[str]) -> List[int]:
        """
        新增订阅写入：窗口站点
        - 用于“已恢复系统订阅”状态下，新订阅保持一致
        """
        site_names_norm = self._normalize_site_names(site_names)
        if not site_names_norm:
            logger.warning(f"已恢复系统订阅：新增订阅站点列表为空（subscribe_id={subscribe_id}），跳过")
            return []

        with SessionFactory() as db:
            mapping = self._get_site_ids_by_names(db, site_names_norm)
            site_ids = []
            for nm in site_names_norm:
                if nm in mapping:
                    site_ids.append(mapping[nm])

            seen = set()
            site_ids_uniq = []
            for x in site_ids:
                if x in seen:
                    continue
                seen.add(x)
                site_ids_uniq.append(x)

            if not site_ids_uniq:
                logger.warning(f"已恢复系统订阅：新增订阅未解析到站点ID（subscribe_id={subscribe_id}），跳过")
                return []

            storage = self._guess_sites_storage_format_for_subscribe(db, int(subscribe_id))
            value = ",".join(str(x) for x in site_ids_uniq) if storage == "str" else site_ids_uniq
            SubscribeOper(db=db).update(int(subscribe_id), {"sites": value})
            logger.info(f"已恢复系统订阅：新增订阅已同步窗口站点（subscribe_id={subscribe_id}）")
            return site_ids_uniq
