"""PanSearch 插件业务数据与可恢复运行状态的 ORM 入口。"""

from __future__ import annotations

from threading import RLock
from typing import Any, Dict

from app.log import logger

from .database import PanSearchDatabaseManager, PanSearchRepositories
class PanSearchDataStore:
    """业务数据和可恢复运行状态按职责写入独立数据库。"""

    HISTORY_KEY = "history"
    OFFLINE_PENDING_KEY = "pending_offline_strm"
    SCHEDULE_KEY = "checkin_schedule_state"
    RUNTIME_KEYS = {
        "account_info_cache",
        "dian115_auth_session",
        "juying_auth_session",
        "pansou_auth_session",
        "pinglian_auth_session",
    }

    def __init__(self, owner):
        self._lock = RLock()
        self._initialized = False
        self.manager = PanSearchDatabaseManager(
            owner.get_data_path() / "pansearch.db"
        )
        self.repositories = PanSearchRepositories(self.manager)

    @staticmethod
    def _checkin_provider(key: str) -> str:
        return key[:-len("_checkin_history")]

    @staticmethod
    def _budget_provider(key: str) -> str:
        return key[:-len("_sub_points_history")]

    @staticmethod
    def _auth_provider(key: str) -> str:
        return key[:-len("_auth_session")]

    @classmethod
    def is_persistent_key(cls, key: str) -> bool:
        normalized = str(key or "")
        return bool(
            normalized in {
                cls.HISTORY_KEY,
                cls.OFFLINE_PENDING_KEY,
                cls.SCHEDULE_KEY,
            }
            or normalized.endswith("_checkin_history")
            or normalized.endswith("_sub_points_history")
        )

    @classmethod
    def is_runtime_key(cls, key: str) -> bool:
        normalized = str(key or "")
        return normalized in cls.RUNTIME_KEYS or normalized.endswith(
            "_auth_session"
        )

    @classmethod
    def handles(cls, key: str) -> bool:
        return cls.is_persistent_key(key) or cls.is_runtime_key(key)

    def initialize(self) -> None:
        with self._lock:
            if self._initialized:
                return
            self.manager.open()
            self.manager.init_db()
            self.manager.update_db()
            repaired = self.repositories.history.repair_group_keys()
            if repaired:
                logger.info(f"PanSearch 历史媒体分组已修复：{repaired} 条")
            self._initialized = True

    def _load_database(self, key: str) -> Any:
        if key == self.HISTORY_KEY:
            return self.repositories.history.list_all()
        if key == self.OFFLINE_PENDING_KEY:
            return self.repositories.offline.load_all()
        if key == self.SCHEDULE_KEY:
            return self.repositories.schedule.load()
        if key.endswith("_checkin_history"):
            return self.repositories.checkin.list_provider(
                self._checkin_provider(key)
            )
        if key.endswith("_sub_points_history"):
            return self.repositories.budget.load_provider(
                self._budget_provider(key)
            )
        if key == "account_info_cache":
            return self.repositories.account.load_all()
        if key.endswith("_auth_session"):
            return self.repositories.auth.load_provider(
                self._auth_provider(key)
            )
        return None

    def _save_database(self, key: str, value: Any) -> None:
        if key == self.HISTORY_KEY:
            self.repositories.history.replace_all(value or [])
        elif key == self.OFFLINE_PENDING_KEY:
            self.repositories.offline.replace_all(value or {})
        elif key == self.SCHEDULE_KEY:
            self.repositories.schedule.save(value or {})
        elif key.endswith("_checkin_history"):
            self.repositories.checkin.replace_provider(
                self._checkin_provider(key), value or []
            )
        elif key.endswith("_sub_points_history"):
            self.repositories.budget.replace_provider(
                self._budget_provider(key), value or {}
            )
        elif key == "account_info_cache":
            self.repositories.account.replace_all(value or {})
        elif key.endswith("_auth_session"):
            self.repositories.auth.save_provider(
                self._auth_provider(key), value or {}
            )

    def load(self, key: str) -> Any:
        normalized = str(key or "")
        with self._lock:
            if not self.handles(normalized):
                return None
            self.initialize()
            return self._load_database(normalized)

    def save(self, key: str, value: Any) -> None:
        normalized = str(key or "")
        with self._lock:
            if not self.handles(normalized):
                raise ValueError(f"未声明的数据键不能持久化：{normalized}")
            self.initialize()
            self._save_database(normalized, value)

    def query_history_page(self, **kwargs) -> Dict[str, Any]:
        self.initialize()
        return self.repositories.history.query_group_page(**kwargs)

    def update_history_status_by_finalize_keys(
            self, finalize_keys, status: str, reason: str = ""
    ) -> list:
        """按 finalize_key 定向更新历史状态（v1.5.14）。

        历史实现走 ``load("history")`` + ``save("history", ...)``，一次状态
        变更要全量读并整体重写全部历史行；该方法被后处理监控在
        ``_offline_pending_lock`` 锁内调用，历史一多就把监控器与前端 API
        全部堵死。这里直连仓储的定向 UPDATE，复杂度降到 O(命中行数)。
        """
        self.initialize()
        return self.repositories.history.update_status_by_finalize_keys(
            finalize_keys, status, reason
        )

    def history_overview(
            self, today: str, recent_limit: int = 20
    ) -> Dict[str, Any]:
        self.initialize()
        return self.repositories.history.overview(today, recent_limit)

    def history_summary(self, today: str) -> Dict[str, int]:
        self.initialize()
        return self.repositories.history.summary(today)

    def load_checkin_snapshot(self) -> Dict[str, Any]:
        """在同一只读会话中加载全部签到历史和调度状态。"""
        self.initialize()
        with self.manager.session() as db:
            return {
                "histories": self.repositories.checkin.load_all(db=db),
                "schedule": self.repositories.schedule.load(db=db),
            }

    def load_account(self, account_key: str) -> Dict[str, Any]:
        self.initialize()
        return self.repositories.account.load_account(account_key)

    def save_account(self, account_key: str, value: Dict[str, Any]) -> None:
        self.initialize()
        self.repositories.account.save_account(account_key, value)

    def close(self) -> None:
        self.manager.close()
