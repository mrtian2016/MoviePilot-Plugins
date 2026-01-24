"""
订阅处理模块
负责订阅状态检查、完成、站点更新等逻辑
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

    # -------- 订阅完成逻辑（略，保持原样即可） --------
    def check_and_finish_subscribe(self, subscribe, mediainfo: MediaInfo, success_episodes: List[int]):
        # 你原实现保持不动（此处省略）
        return

    # -------- 站点写入增强 --------

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
                {"id": -1, "name": "115网盘", "url": "https://115.com", "is_active": True,
                 "limit_interval": 10000000, "limit_count": 1, "limit_seconds": 10000000, "timeout": 1}
            )
            db.commit()
            logger.info("已添加站点记录：115网盘(id=-1)")
        return -1

    @staticmethod
    def _guess_sites_storage_format(subscribes: List[Any]) -> str:
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

            # 去重保序
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
            storage = self._guess_sites_storage_format(subscribes)

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
        return self.apply_subscribe_sites_by_site_names(unblocked_site_names, action_desc="已恢复系统订阅：勾选订阅站点")

    def set_blocked_sites_only_115(self) -> List[int]:
        with SessionFactory() as db:
            site_id_115 = self._ensure_115_site_id(db)

            subscribes = SubscribeOper(db=db).list() or []
            storage = self._guess_sites_storage_format(subscribes)
            exclude_ids = set(self._exclude_subscribes or [])

            updated, excluded = 0, 0
            for s in subscribes:
                if s.id in exclude_ids:
                    excluded += 1
                    continue
                value = str(site_id_115) if storage == "str" else [site_id_115]
                SubscribeOper(db=db).update(s.id, {"sites": value})
                updated += 1

            logger.info(f"已屏蔽系统订阅：仅115网盘：已更新 {updated} 个订阅（跳过 {excluded} 个排除订阅）")
            return [site_id_115]
