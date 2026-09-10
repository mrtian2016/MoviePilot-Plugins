"""积分搜索渠道共享的任务与订阅预算账本。"""

import threading
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional


@dataclass(frozen=True)
class PointBudgetStatus:
    """单次积分请求对应的任务与订阅额度快照。"""

    requested: int
    task_spent: int
    subscribe_spent: int
    task_limit: int
    subscribe_limit: int

    @property
    def task_allowed(self) -> bool:
        return self.task_spent + self.requested <= self.task_limit

    @property
    def subscribe_allowed(self) -> bool:
        return self.subscribe_spent + self.requested <= self.subscribe_limit

    @property
    def allowed(self) -> bool:
        return self.task_allowed and self.subscribe_allowed


class PointBudgetLedger:
    """原子维护任务总预算、线程内订阅预算和持久化消费记录。"""

    def __init__(
            self,
            history_key: str,
            task_limit: int,
            subscribe_limit: int,
            unlocked_cache: Any = None,
    ):
        self.history_key = str(history_key or "")
        self.task_limit = max(0, int(task_limit or 0))
        self.subscribe_limit = max(0, int(subscribe_limit or 0))
        self.lock = threading.RLock()
        self._local = threading.local()
        self._task_spent = 0
        self._get_data: Optional[Callable[[str], Any]] = None
        self._save_data: Optional[Callable[[str, Any], None]] = None
        self._unlocked_cache = unlocked_cache

    @property
    def get_data_func(self) -> Optional[Callable[[str], Any]]:
        return self._get_data

    @property
    def save_data_func(self) -> Optional[Callable[[str, Any], None]]:
        return self._save_data

    @property
    def task_spent(self) -> int:
        return self._task_spent

    @property
    def subscribe_spent(self) -> int:
        return int(getattr(self._local, "spent", 0) or 0)

    @property
    def subscribe_key(self) -> str:
        return str(getattr(self._local, "key", "") or "")

    def configure_storage(
            self, get_data: Callable, save_data: Callable
    ) -> None:
        with self.lock:
            self._get_data = get_data
            self._save_data = save_data

    def _load_history(self) -> Dict[str, int]:
        if not self._get_data:
            return {}
        data = self._get_data(self.history_key) or {}
        return dict(data) if isinstance(data, dict) else {}

    def _save_history(self, history: Dict[str, int]) -> None:
        if self._save_data:
            self._save_data(self.history_key, history)

    @staticmethod
    def normalize_points(value: Any) -> Optional[int]:
        try:
            return max(0, int(value or 0))
        except (TypeError, ValueError):
            return None

    def reset_task(self) -> None:
        with self.lock:
            self._task_spent = 0
            self._local.key = ""
            self._local.spent = 0

    def reset_subscription(self, key: str = "") -> int:
        with self.lock:
            normalized_key = str(key or "")
            history = self._load_history() if normalized_key else {}
            spent = self.normalize_points(history.get(normalized_key)) or 0
            self._local.key = normalized_key
            self._local.spent = spent
            return spent

    def clear_subscription(self, key: str) -> bool:
        with self.lock:
            normalized_key = str(key or "")
            history = self._load_history()
            if normalized_key not in history:
                return False
            history.pop(normalized_key, None)
            self._save_history(history)
            return True

    def clear_history(self) -> int:
        """清空当前渠道的全部订阅消费记录并返回删除条数。"""
        with self.lock:
            history = self._load_history()
            count = len(history)
            if count:
                self._save_history({})
            self._task_spent = 0
            self._local.key = ""
            self._local.spent = 0
            if self._unlocked_cache is not None:
                self._unlocked_cache.clear()
            return count

    def has_budget(self, points: Any) -> bool:
        status = self.status(points)
        return bool(status and status.allowed)

    def cached_url(self, identity: Any) -> str:
        """返回积分资源已取得的链接，供所有积分渠道统一防重复解锁。"""
        if self._unlocked_cache is None:
            return ""
        return str(self._unlocked_cache.get(str(identity)) or "").strip()

    def cached_url_count(self) -> int:
        if self._unlocked_cache is None:
            return 0
        return len(list(self._unlocked_cache.items()))

    def discard_cached_url(self, identity: Any) -> None:
        """移除单个无效链接，避免格式错误的历史值反复命中。"""
        if self._unlocked_cache is not None:
            self._unlocked_cache.pop(str(identity), None)

    def clear_cached_urls(self) -> int:
        count = self.cached_url_count()
        if self._unlocked_cache is not None:
            self._unlocked_cache.clear()
        return count

    def status(self, points: Any) -> Optional[PointBudgetStatus]:
        """返回规范化后的额度快照；积分无效时返回 ``None``。"""
        normalized = self.normalize_points(points)
        if normalized is None:
            return None
        with self.lock:
            return PointBudgetStatus(
                requested=normalized,
                task_spent=self._task_spent,
                subscribe_spent=self.subscribe_spent,
                task_limit=self.task_limit,
                subscribe_limit=self.subscribe_limit,
            )

    def format_snapshot(self, points: Any = 0) -> str:
        """统一生成积分渠道 DEBUG 日志使用的预算快照。"""
        status = self.status(points)
        if status is None:
            return f"请求={points}（无效）"
        return (
            f"请求={status.requested}，"
            f"任务={status.task_spent}/{status.task_limit}，"
            f"订阅key={self.subscribe_key or '<none>'}，"
            f"订阅={status.subscribe_spent}/{status.subscribe_limit}，"
            f"允许={status.allowed}"
        )

    def record(self, points: Any) -> int:
        normalized = self.normalize_points(points)
        if not normalized:
            return 0
        with self.lock:
            self._task_spent += normalized
            subscribe_spent = self.subscribe_spent + normalized
            self._local.spent = subscribe_spent
            key = self.subscribe_key
            if key:
                history = self._load_history()
                history[key] = subscribe_spent
                self._save_history(history)
            return normalized

    def record_result(
            self, identity: Any, url: str, points: Any
    ) -> tuple[int, int, int]:
        """统一记录服务端实际扣分，并仅为有效链接建立幂等缓存。"""
        with self.lock:
            before_task = self._task_spent
            before_subscribe = self.subscribe_spent
            actual_points = self.record(points)
            normalized_url = str(url or "").strip()
            if normalized_url and self._unlocked_cache is not None:
                self._unlocked_cache.set(str(identity), normalized_url)
            return actual_points, before_task, before_subscribe

    def remaining(self) -> tuple[int, int]:
        with self.lock:
            return (
                max(0, self.task_limit - self._task_spent),
                max(0, self.subscribe_limit - self.subscribe_spent),
            )
