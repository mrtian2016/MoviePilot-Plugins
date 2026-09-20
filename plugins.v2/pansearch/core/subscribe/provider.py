"""自动订阅渠道契约。"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from threading import Event
from typing import Any, Iterator, Optional


def ranking_scan_limit(options: dict[str, Any]) -> int:
    """为榜单过滤预留后续候选，实际新增数量仍由 ``limit`` 控制。"""
    internal_limit = options.get("_candidate_scan_limit")
    if internal_limit is not None:
        try:
            return max(1, min(int(internal_limit), 500))
        except (TypeError, ValueError):
            pass
    try:
        limit = max(1, min(int(options.get("limit") or 20), 100))
    except (TypeError, ValueError):
        limit = 20
    return min(500, max(100, limit * 5))


@dataclass
class SubscribeContext:
    owner: Any = None
    event: Optional[Event] = None
    logger: Any = None
    config: dict[str, Any] | None = None
    # 由宿主后端统一构造的搜索代理 URL（含可选认证信息）。
    proxy: Any = None

    def stopped(self) -> bool:
        return bool(self.event and self.event.is_set())

    def proxy_for(self, enabled: Any) -> Any:
        """按渠道开关返回共享搜索代理；未启用时保持直连。"""
        if not bool(enabled):
            return None
        if not self.proxy:
            raise RuntimeError("已启用榜单代理，但搜索渠道代理地址未配置或无效")
        return self.proxy


class SubscribeProvider(ABC):
    provider_id = ""
    provider_name = ""

    @abstractmethod
    def spec(self) -> dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    def fetch(self, options: dict[str, Any], context: SubscribeContext) -> Iterator[Any]:
        raise NotImplementedError

    def has_listening(self, options: dict[str, Any]) -> bool:
        return True
