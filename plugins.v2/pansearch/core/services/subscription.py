"""订阅搜索接管时段与洗版开关管理。"""

import datetime

import pytz
from app.core.config import settings

from ...core import OwnerDelegator


class SubscriptionControlService(OwnerDelegator):
    """管理订阅搜索接管时段与洗版状态。"""

    def _is_subscribe_excluded(self, subscribe_id: int) -> bool:
        """
        按订阅过滤模式判断订阅是否不归本插件处理

        - exclude 排除模式：勾选的订阅被排除，其余全部处理
        - include 指定模式：仅处理勾选的订阅，其余全部排除
        """
        if self._subscribe_filter_mode == "include":
            return subscribe_id not in set(self._include_subscribes or [])
        return subscribe_id in set(self._exclude_subscribes or [])

    def _is_time_in_block(self, time_str: str = None) -> bool:
        """
        判断指定时间（或当前时间）是否在屏蔽时间段内。
        仅在 block_system_subscribe=OFF 时生效。
        支持跨天时段（如 22:00 ~ 06:00）。
        """
        if self._block_system_subscribe:
            return False
        if not self._block_start_time or not self._block_end_time:
            return False

        tz = pytz.timezone(settings.TZ)
        now = datetime.datetime.now(tz=tz).strftime("%H:%M")
        check = time_str or now

        b_start = self._block_start_time.strip()
        b_end = self._block_end_time.strip()

        if b_start < b_end:
            return b_start <= check <= b_end
        else:
            return check >= b_start or check <= b_end

    def _is_takeover_active(self) -> bool:
        if not self._enabled:
            return False
        if self._block_system_subscribe:
            return True
        return self._is_time_in_block()

    def _is_cloud_upgrade_subscribe(self, subscribe) -> bool:
        """判断订阅是否属于插件网盘洗版范围。best_version 是必要条件。"""
        if (
                not self._enable_cloud_upgrade
                or not subscribe
                or not bool(getattr(subscribe, "best_version", False))
        ):
            return False
        selected_ids = {str(value) for value in (self._upgrade_subscribe_ids or [])}
        return not selected_ids or str(getattr(subscribe, "id", "")) in selected_ids
