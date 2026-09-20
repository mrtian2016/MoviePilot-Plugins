# -*- coding: utf-8 -*-
"""
签到处理模块（v1.8.0）

负责 115 网盘每日签到与癫影（Dian115）签到 / 转盘抽奖。

设计约束（与 P115SubSearch 既有风格一致）：
    * 不使用 SQLite，历史记录走插件的 get_data / save_data；
    * 不引入新的第三方依赖，癫影复用 clients/dian115.py；
    * 单一 provider 失败不影响其他 provider；
    * 通知统一走 notify_func，由主插件决定是否发送。
"""
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from app.log import logger


class CheckinHandler:
    """签到处理器"""

    # 签到历史持久化键
    HISTORY_KEY = "checkin_history"
    # 历史保留条数上限
    HISTORY_LIMIT = 100

    PROVIDER_NAMES = {
        "p115": "115网盘",
        "dian115": "癫影",
    }

    def __init__(
        self,
        p115_manager=None,
        dian115_client=None,
        p115_checkin_enabled: bool = True,
        dian115_checkin_enabled: bool = False,
        dian115_checkin_mode: str = "normal",
        dian115_lottery_enabled: bool = False,
        dian115_lottery_count: int = 0,
        notify_func: Optional[Callable[[str], None]] = None,
        get_data_func: Optional[Callable[[str], Any]] = None,
        save_data_func: Optional[Callable[[str, Any], None]] = None
    ):
        """
        初始化签到处理器

        :param p115_manager: P115ClientManager 实例
        :param dian115_client: Dian115Client 实例
        :param p115_checkin_enabled: 是否启用 115 签到
        :param dian115_checkin_enabled: 是否启用癫影签到
        :param dian115_checkin_mode: 癫影签到模式（normal / lucky）
        :param dian115_lottery_enabled: 是否启用癫影转盘抽奖
        :param dian115_lottery_count: 转盘目标次数（客户端内部硬上限 20）
        :param notify_func: 通知回调
        :param get_data_func: 插件数据读取函数
        :param save_data_func: 插件数据写入函数
        """
        self._p115_manager = p115_manager
        self._dian115_client = dian115_client
        self._p115_checkin_enabled = p115_checkin_enabled
        self._dian115_checkin_enabled = dian115_checkin_enabled
        self._dian115_checkin_mode = dian115_checkin_mode or "normal"
        self._dian115_lottery_enabled = dian115_lottery_enabled
        self._dian115_lottery_count = max(0, int(dian115_lottery_count or 0))
        self._notify_func = notify_func
        self._get_data_func = get_data_func
        self._save_data_func = save_data_func

    # ------------------ 对外能力 ------------------

    def enabled_providers(self) -> List[str]:
        """返回当前已启用且凭据可用的签到源。"""
        providers: List[str] = []

        if self._p115_checkin_enabled:
            if self._p115_manager and getattr(self._p115_manager, "client", None):
                providers.append("p115")
            else:
                logger.warning("115 签到已启用但未配置 115 Cookie，跳过")

        if self._dian115_checkin_enabled:
            if self._dian115_client:
                providers.append("dian115")
            else:
                logger.warning("癫影签到已启用但客户端未就绪（缺少账号或未通过依赖检查），跳过")

        return providers

    def run_checkin(self, providers: Optional[List[str]] = None) -> Dict[str, Any]:
        """
        执行签到

        :param providers: 指定要签到的源；为空时使用 enabled_providers()
        :return: {"success": bool, "total": int, "success_count": int,
                  "fail_count": int, "records": [...]}
        """
        targets = providers or self.enabled_providers()
        if not targets:
            logger.info("没有可用的签到源，跳过本次签到")
            return {"success": True, "total": 0, "success_count": 0, "fail_count": 0, "records": []}

        records: List[Dict[str, Any]] = []
        for provider in targets:
            provider = str(provider or "").strip().lower()
            if provider == "p115":
                record = self._run_p115()
            elif provider == "dian115":
                record = self._run_dian115()
            else:
                logger.warning(f"未知的签到源: {provider}")
                continue

            records.append(record)
            self._append_history(record)
            if self._notify_func:
                try:
                    self._notify_func(
                        f"[{record.get('provider_name')}] {record.get('status')}：{record.get('message')}"
                    )
                except Exception as e:
                    logger.warning(f"签到通知发送失败: {e}")

        success_count = len([r for r in records if r.get("success")])
        fail_count = len(records) - success_count
        return {
            "success": fail_count == 0,
            "total": len(records),
            "success_count": success_count,
            "fail_count": fail_count,
            "records": records
        }

    def get_history(self, limit: int = 50) -> List[Dict[str, Any]]:
        """读取签到历史（最近的在前）。"""
        raw = self._load_history()
        if not raw:
            return []
        ordered = sorted(raw, key=lambda x: str(x.get("time") or ""), reverse=True)
        return ordered[:max(1, int(limit or 50))]

    def last_record(self, provider: str) -> Optional[Dict[str, Any]]:
        """取指定源最近一条签到记录。"""
        provider = str(provider or "").strip().lower()
        for record in self.get_history(limit=self.HISTORY_LIMIT):
            if str(record.get("provider") or "").lower() == provider:
                return record
        return None

    # ------------------ 各源实现 ------------------

    def _run_p115(self) -> Dict[str, Any]:
        """执行 115 网盘签到。"""
        record = self._new_record("p115")
        if not self._p115_manager:
            record.update({"status": "签到失败", "message": "115 客户端未初始化"})
            return record

        try:
            result = self._p115_manager.points_sign()
        except Exception as e:
            logger.error(f"115 签到异常: {e}")
            record.update({"status": "签到失败", "message": f"签到异常：{e}"})
            return record

        if not result.get("success"):
            record.update({
                "status": "签到失败",
                "message": str(result.get("message") or "接口返回失败")
            })
            return record

        record.update({
            "success": True,
            "status": "今日已签到" if result.get("already") else "签到成功",
            "message": str(result.get("message") or "签到完成"),
            "points": int(result.get("points") or 0),
            "days": int(result.get("days") or 0)
        })
        return record

    def _run_dian115(self) -> Dict[str, Any]:
        """执行癫影签到 + 转盘抽奖，合并为一条业务结果。"""
        record = self._new_record("dian115")
        client = self._dian115_client
        if not client:
            record.update({"status": "签到失败", "message": "癫影客户端未初始化"})
            return record

        lottery_count = self._dian115_lottery_count if self._dian115_lottery_enabled else 0

        try:
            before = client.get_account_info()
            signin = client.signin(mode=self._dian115_checkin_mode)
            lottery = (
                client.run_lottery(lottery_count)
                if lottery_count else {
                    "success": True,
                    "target_count": 0,
                    "executed": 0,
                    "cost_points": 0,
                    "award_points": 0,
                    "vip_days": 0,
                    "points_change": 0,
                    "used_after": 0
                }
            )
            try:
                after = client.get_account_info()
            except Exception:
                after = dict(before)
                fallback = lottery.get("new_balance")
                if fallback is None:
                    fallback = signin.get("new_balance")
                if fallback is not None:
                    after["points"] = fallback
        except Exception as e:
            logger.error(f"癫影签到异常: {e}")
            record.update({"status": "签到失败", "message": f"签到异常：{e}"})
            return record

        points_before = int(before.get("points") or 0)
        points_after = int(after.get("points") or 0)
        signin_points = signin.get("award_points")
        if signin_points is None:
            signin_points = points_after - points_before - int(lottery.get("points_change") or 0)

        # 转盘次数口径（v1.8.3 修正，对齐网盘搜索助手）：
        #   used_after  = 当日累计已用次数（含本次）
        #   executed    = 本次执行次数（增量）
        #   target_count= 配置的当日总目标
        # 历史版本误把 executed 当作进度显示，导致「当天已抽过 3 次、本次再抽 2 次」
        # 时显示成 2/5，看起来像少抽了。
        used_after = int(lottery.get("used_after") or 0)
        lottery_target = int(lottery.get("target_count") or lottery_count or 0)
        lottery_max_plays = int(lottery.get("max_plays") or 0)
        lottery_executed = int(lottery.get("executed") or 0)

        parts = [
            "今日已签到" if signin.get("already_checked_in") else f"签到 {signin_points} 积分"
        ]
        if lottery_count:
            parts.append(f"转盘 当日 {used_after}/{lottery_target} 次")
        if not lottery.get("success"):
            parts.append(f"转盘未完成：{lottery.get('message') or '接口返回失败'}")

        success = bool(signin.get("success") and lottery.get("success"))
        record.update({
            "success": success,
            "status": (
                "今日已签到" if signin.get("already_checked_in") and not lottery_count
                else "签到完成" if success else "签到未完成"
            ),
            "message": "；".join(parts),
            "points": int(points_after),
            "points_change": points_after - points_before,
            "days": int(after.get("consecutive_signin") or signin.get("signin_days") or 0),
            # 扁平化转盘字段，与网盘搜索助手 _build_record 保持一致
            "lottery_target_count": lottery_target,
            "lottery_executed": lottery_executed,
            "lottery_used_after": used_after,
            "lottery_max_plays": lottery_max_plays,
            "lottery_cost_points": int(lottery.get("cost_points") or 0),
            "lottery_award_points": int(lottery.get("award_points") or 0),
            "lottery_vip_days": int(lottery.get("vip_days") or 0),
        })
        return record

    # ------------------ 内部工具 ------------------

    def _new_record(self, provider: str) -> Dict[str, Any]:
        """新建一条签到记录骨架（默认失败态，成功时由调用方覆盖）。"""
        return {
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "provider": provider,
            "provider_name": self.PROVIDER_NAMES.get(provider, provider),
            "success": False,
            "status": "未完成",
            "message": "",
            "points": 0,
            "points_change": 0,
            "days": 0
        }

    def _load_history(self) -> List[Dict[str, Any]]:
        if not self._get_data_func:
            return []
        try:
            raw = self._get_data_func(self.HISTORY_KEY)
        except Exception as e:
            logger.warning(f"读取签到历史失败: {e}")
            return []
        if isinstance(raw, list):
            return [x for x in raw if isinstance(x, dict)]
        return []

    def _append_history(self, record: Dict[str, Any]) -> None:
        """追加一条签到历史并截断到上限。"""
        if not self._save_data_func:
            return
        history = self._load_history()
        history.append(record)
        history = sorted(history, key=lambda x: str(x.get("time") or ""))[-self.HISTORY_LIMIT:]
        try:
            self._save_data_func(self.HISTORY_KEY, history)
        except Exception as e:
            logger.warning(f"保存签到历史失败: {e}")
