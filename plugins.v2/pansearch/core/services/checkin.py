"""通用每日签到执行、通知与历史持久化。"""

import copy
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, Optional, Tuple, Type

import pytz
from app.core.config import settings
from app.log import logger
from p115client import check_response

from .. import OwnerDelegator
from ..delegation import get_component
from ...drive.quark import QuarkClient
from ...search.dian115 import Dian115Error, Dian115SearchService
from ...search.hdhive import (
    HDHiveOpenAPIError,
    HDHiveSearchService,
    HDHiveWebError,
)
from ...search.juying import JuyingError


class P115CheckinError(RuntimeError):
    """115 签到接口错误。"""


class QuarkCheckinError(RuntimeError):
    """夸克签到接口错误。"""


@dataclass(frozen=True)
class CheckinProvider:
    """签到提供方与插件配置、客户端之间的最小适配契约。"""

    key: str
    name: str
    credential_attrs: Tuple[str, ...]
    error_types: Tuple[Type[Exception], ...]
    modes: Tuple[str, ...]

    @property
    def history_key(self) -> str:
        return f"{self.key}_checkin_history"


class CheckinService(OwnerDelegator):
    """统一编排各提供方的签到、通知和历史。"""

    _PROVIDERS = {
        "hdhive": CheckinProvider(
            key="hdhive",
            name="HDHive",
            credential_attrs=("_hdhive_username", "_hdhive_password"),
            error_types=(HDHiveWebError, HDHiveOpenAPIError),
            modes=("normal", "gambler"),
        ),
        "dian115": CheckinProvider(
            key="dian115",
            name="Dian115",
            credential_attrs=("_dian115_email", "_dian115_password"),
            error_types=(Dian115Error,),
            modes=("normal", "lucky"),
        ),
        "juying": CheckinProvider(
            key="juying",
            name="聚影",
            credential_attrs=("_juying_username", "_juying_password"),
            error_types=(JuyingError,),
            modes=("normal",),
        ),
        "p115": CheckinProvider(
            key="p115",
            name="115 网盘",
            credential_attrs=("_p115_cookies",),
            error_types=(P115CheckinError,),
            modes=("normal",),
        ),
        "quark": CheckinProvider(
            key="quark",
            name="夸克网盘",
            credential_attrs=("_quark_checkin_url",),
            error_types=(QuarkCheckinError,),
            modes=("normal",),
        ),
    }
    _HISTORY_LIMIT = 60
    _RETRY_START_HOUR = 9
    _RETRY_END_HOUR = 23
    _DEFAULT_RETRY_COUNT = 2
    _MAX_RETRY_COUNT = 10
    _SCHEDULE_STATE_KEY = "checkin_schedule_state"

    def __init__(self, owner):
        super().__init__(owner)
        object.__setattr__(self, "_run_lock", threading.Lock())
        object.__setattr__(self, "_history_lock", threading.RLock())
        object.__setattr__(self, "_schedule_lock", threading.Lock())

    @staticmethod
    def _now_text() -> str:
        return datetime.now(
            pytz.timezone(settings.TZ)
        ).isoformat(timespec="seconds")

    @staticmethod
    def _now() -> datetime:
        return datetime.now(pytz.timezone(settings.TZ))

    @classmethod
    def _resolve_provider(cls, provider: str) -> Optional[CheckinProvider]:
        return cls._PROVIDERS.get(str(provider or "").strip().lower())

    def _checkin_credentials_ready(self, provider: CheckinProvider) -> bool:
        if (
                provider.key == "hdhive"
                and str(getattr(self, "_hdhive_query_mode", "web")) == "api"
        ):
            client = getattr(self, "_hdhive_client", None)
            return bool(client and client.is_ready)
        return all(
            bool(getattr(self, attr, None))
            for attr in provider.credential_attrs
        )

    def _checkin_configuration_message(self, provider: CheckinProvider) -> str:
        if provider.key == "quark":
            return "请先配置并保存夸克签到 URL"
        if provider.key == "p115":
            return "请先配置并保存 115 Cookie"
        if (
                provider.key == "hdhive"
                and str(getattr(self, "_hdhive_query_mode", "web")) == "api"
        ):
            return "请先配置并保存 HDHive OpenAPI 应用 Secret 和用户授权"
        return f"请先配置并保存 {provider.name} 账号和密码"

    def _get_checkin_client(self, provider: CheckinProvider) -> Any:
        """直接从渠道服务获取签到客户端，不依赖搜索渠道是否启用。"""
        if provider.key == "hdhive":
            if str(getattr(self, "_hdhive_query_mode", "web")) == "api":
                client = getattr(self, "_hdhive_client", None)
                if not client or not client.is_ready:
                    raise HDHiveOpenAPIError(
                        "OPENAPI_USER_REQUIRED",
                        "HDHive OpenAPI 应用配置或用户授权不完整",
                    )
                return client
            return self._search_component(
                HDHiveSearchService
            ).get_client()
        if provider.key == "dian115":
            return self._search_component(
                Dian115SearchService
            ).get_client()
        if provider.key == "p115":
            manager = getattr(self, "_p115_manager", None)
            client = getattr(manager, "client", None) if manager else None
            if client is not None:
                return client
            raise P115CheckinError("115 客户端未初始化")
        if provider.key == "quark":
            drive = getattr(self, "_quark_drive", None)
            client = getattr(drive, "client", None) if drive else None
            if isinstance(client, QuarkClient):
                return client
            raise QuarkCheckinError("夸克客户端未初始化")
        client = getattr(self, "_juying_client", None)
        if client and client.is_configured:
            return client
        raise JuyingError("聚影账号未配置，请先保存账号和密码")

    def _search_component(self, component_type):
        """复用搜索处理器创建并缓存的渠道服务组件。"""
        return get_component(
            self._search_handler, component_type, "_search_components"
        )

    def _refresh_checkin_account(
            self,
            provider: CheckinProvider,
            record: Dict[str, Any],
    ) -> None:
        if provider.key in {"p115", "quark"}:
            return
        from ..api.account import clear_account_cache
        from ..api.page import clear_ui_options_cache

        account_key = f"search:{provider.key}"
        try:
            updated = self.update_search_account_points(
                provider.key,
                record.get("points_after"),
                record.get("signin_days"),
            )
            if not updated:
                clear_account_cache(account_key)
                self._account_info(account_key, refresh=True)
            clear_ui_options_cache()
        except Exception as error:
            logger.debug(f"刷新 {provider.name} 搜索账户积分失败：{error}")

    def get_checkin_provider_specs(self) -> list[Dict[str, Any]]:
        """向配置校验与调度注册暴露稳定、无客户端对象的提供方信息。"""
        return [
            {
                "key": item.key,
                "name": item.name,
                "credential_attrs": item.credential_attrs,
                "modes": item.modes,
            }
            for item in self._PROVIDERS.values()
        ]

    def _load_history(self, provider: CheckinProvider) -> list[Dict[str, Any]]:
        stored = self.get_data(provider.history_key) or []
        if not isinstance(stored, list):
            return []
        return [
            copy.deepcopy(item)
            for item in stored[-self._HISTORY_LIMIT:]
            if isinstance(item, dict)
        ]

    def _save_history(
            self,
            provider: CheckinProvider,
            record: Dict[str, Any],
    ) -> None:
        with self._history_lock:
            history = self._load_history(provider)
            history.append(copy.deepcopy(record))
            self.save_data(
                provider.history_key,
                history[-self._HISTORY_LIMIT:],
            )

    def get_checkin_history(
            self,
            provider: str,
            limit: int = 20,
    ) -> Optional[Dict[str, Any]]:
        adapter = self._resolve_provider(provider)
        if adapter is None:
            return None
        with self._history_lock:
            history = self._load_history(adapter)
        normalized_limit = max(1, min(int(limit or 20), self._HISTORY_LIMIT))
        return {
            "total": len(history),
            "limit": normalized_limit,
            "items": list(reversed(history))[:normalized_limit],
        }

    @staticmethod
    def _public_record(record: Dict[str, Any]) -> Dict[str, Any]:
        """移除验证码、HTTP 与错误码等仅供内部诊断的字段。"""
        return {
            key: copy.deepcopy(record.get(key))
            for key in (
                "id", "provider", "provider_name", "executed_at", "trigger",
                "mode", "success", "status", "points_change",
                "points_before", "points_after", "signin_days",
                "signin_points", "message",
                "lottery_target_count", "lottery_executed",
                "lottery_cost_points", "lottery_award_points",
                "lottery_vip_days",
            )
        }

    def list_checkin_details(
            self,
            provider: str = "",
            limit: int = 10,
    ) -> Dict[str, Any]:
        """按渠道集中返回供智能体与远程命令展示的签到详情。"""
        provider_key = str(provider or "").strip().lower()
        if provider_key in {"all", "全部"}:
            provider_key = ""
        if provider_key:
            adapter = self._resolve_provider(provider_key)
            if adapter is None:
                return {"success": False, "message": "不支持的签到提供方"}
            providers = [adapter] if bool(
                getattr(self, f"_{adapter.key}_checkin_enabled", False)
            ) else []
        else:
            providers = [
                item for item in self._PROVIDERS.values()
                if bool(getattr(self, f"_{item.key}_checkin_enabled", False))
            ]
        normalized_limit = max(1, min(int(limit or 10), self._HISTORY_LIMIT))
        channels = []
        with self._history_lock:
            for item in providers:
                history = self._load_history(item)
                records = [
                    self._public_record(record)
                    for record in reversed(history)
                ][:normalized_limit]
                for record in records:
                    if record.get("trigger") not in {"scheduled", "retry"}:
                        record["trigger"] = "manual"
                channels.append({
                    "provider": item.key,
                    "provider_name": item.name,
                    "total": len(history),
                    "items": records,
                })
        total = sum(item["total"] for item in channels)
        return {
            "success": True,
            "message": f"共查询到 {total} 条签到记录",
            "data": {"channels": channels, "total": total},
        }

    @staticmethod
    def _record_date_key(record: Dict[str, Any]) -> str:
        value = str(record.get("executed_at") or "").strip()
        if not value:
            return ""
        try:
            executed_at = datetime.fromisoformat(value)
            timezone = pytz.timezone(settings.TZ)
            if executed_at.tzinfo is None:
                executed_at = timezone.localize(executed_at)
            return executed_at.astimezone(timezone).strftime("%Y-%m-%d")
        except ValueError:
            return value[:10]

    @staticmethod
    def _number(value: Any) -> int:
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    def _overview_status(
            self,
            record: Optional[Dict[str, Any]],
            *,
            enabled: bool,
            configured: bool,
            retry_pending: bool,
    ) -> Dict[str, str]:
        if not enabled:
            return {"key": "disabled", "label": "未启用", "tone": "disabled"}
        if not configured:
            return {"key": "unconfigured", "label": "未配置", "tone": "warning"}
        if not record:
            return {"key": "pending", "label": "待签到", "tone": "pending"}
        if record.get("success"):
            already = any(
                "已签到" in str(record.get(key) or "")
                for key in ("status", "message")
            )
            return {
                "key": "already" if already else "success",
                "label": "已签到" if already else "签到成功",
                "tone": "success",
            }
        if retry_pending and self._checkin_auto_retry:
            return {"key": "retry", "label": "等待重试", "tone": "warning"}
        return {"key": "failed", "label": "签到失败", "tone": "error"}

    def get_checkin_overview(self, days: int = 7) -> Dict[str, Any]:
        """聚合多渠道签到状态，供平台仪表盘一次读取。"""
        normalized_days = max(3, min(int(days or 7), 14))
        snapshot = self._get_data_store().load_checkin_snapshot()
        histories = snapshot.get("histories") or {}
        schedule = snapshot.get("schedule") or {}
        now = self._now()
        date_keys = [
            (now - timedelta(days=index)).strftime("%Y-%m-%d")
            for index in range(normalized_days)
        ]
        today = date_keys[0]
        retry_providers = {
            str(value or "").strip().lower()
            for value in (schedule.get("pending_providers") or [])
            if str(value or "").strip()
        }
        channels = []
        today_points = 0
        lottery_net_points = 0
        lottery_executed = 0

        for provider in self._PROVIDERS.values():
            records = [
                copy.deepcopy(item)
                for item in (histories.get(provider.key) or [])
                if isinstance(item, dict)
            ]
            records_by_date = {}
            for record in records:
                date_key = self._record_date_key(record)
                if date_key:
                    records_by_date[date_key] = record
                if date_key == today:
                    today_points += self._number(record.get("points_change"))
                    lottery_executed += self._number(
                        record.get("lottery_executed")
                    )
                    lottery_net_points += (
                            self._number(record.get("lottery_award_points"))
                            - self._number(record.get("lottery_cost_points"))
                    )

            enabled = bool(getattr(
                self, f"_{provider.key}_checkin_enabled", False
            ))
            configured = self._checkin_credentials_ready(provider)
            today_record = records_by_date.get(today)
            status = self._overview_status(
                today_record,
                enabled=enabled,
                configured=configured,
                retry_pending=provider.key in retry_providers,
            )
            timeline = []
            for date_key in date_keys:
                record = records_by_date.get(date_key)
                day_status = self._overview_status(
                    record,
                    enabled=enabled if record is None else True,
                    configured=configured if record is None else True,
                    retry_pending=(
                            date_key == today
                            and provider.key in retry_providers
                    ),
                )
                timeline.append({
                    "date": date_key,
                    "status": day_status["key"],
                    "label": day_status["label"],
                    "success": bool(record and record.get("success")),
                })
            latest = self._public_record(records[-1]) if records else None
            channels.append({
                "provider": provider.key,
                "provider_name": provider.name,
                "enabled": enabled,
                "configured": configured,
                "mode": str(getattr(
                    self, f"_{provider.key}_checkin_mode", "normal"
                ) or "normal"),
                "status": status,
                "today": self._public_record(today_record)
                if today_record else None,
                "latest": latest,
                "timeline": timeline,
                "total": len(records),
            })

        ready_channels = [
            item for item in channels
            if item["enabled"] and item["configured"]
        ]
        return {
            "generated_at": now.isoformat(timespec="seconds"),
            "running": self._run_lock.locked(),
            "days": date_keys,
            "summary": {
                "enabled": sum(item["enabled"] for item in channels),
                "ready": len(ready_channels),
                "today_success": sum(
                    item["status"]["key"] in {"success", "already"}
                    for item in ready_channels
                ),
                "today_failed": sum(
                    item["status"]["key"] in {"failed", "retry"}
                    for item in ready_channels
                ),
                "today_pending": sum(
                    item["status"]["key"] == "pending"
                    for item in ready_channels
                ),
                "today_points": today_points,
                "lottery_executed": lottery_executed,
                "lottery_net_points": lottery_net_points,
            },
            "schedule": {
                "cron": str(self._checkin_cron or ""),
                "auto_retry": bool(self._checkin_auto_retry),
                "retry_count": self._configured_retry_count(),
                "state": copy.deepcopy(schedule),
            },
            "channels": channels,
        }

    def _notify_checkin(
            self,
            provider: CheckinProvider,
            record: Dict[str, Any],
    ) -> None:
        if not self._notify:
            return
        delta = record.get("points_change")
        delta_text = self._signed_points(delta)
        points_label = "枫叶" if provider.key == "p115" else "积分"
        balance = record.get("points_after")
        signin_days = record.get("signin_days")
        mode = {
            "gambler": "赌狗签到",
            "lucky": "运气签到",
        }.get(record.get("mode"), "普通签到")
        lines = [
            f"模式：{mode}",
            f"状态：{record.get('status') or '未知'}",
            f"{points_label}：{delta_text}，余额 {balance if balance is not None else '未知'}",
            f"累计：{signin_days if signin_days is not None else '未知'} 天",
        ]
        if record.get("lottery_target_count"):
            lines.append(
                f"转盘：{record.get('lottery_executed') or 0}/"
                f"{record.get('lottery_target_count')} 次，净积分 "
                f"{self._signed_points((record.get('lottery_award_points') or 0) - (record.get('lottery_cost_points') or 0))}"
            )
        if not record.get("success") and record.get("message"):
            lines.append(f"原因：{record.get('message')}")
        self.post_message(
            mtype=self._notification_type,
            title=(
                f"【网盘订阅助手】{provider.name} 签到完成"
                if record.get("success")
                else f"【网盘订阅助手】{provider.name} 签到失败"
            ),
            text="\n".join(lines),
        )

    def _notify_checkin_summary(
            self,
            results: list[Dict[str, Any]],
            title: str,
    ) -> None:
        """批量签到完成后发送一条短汇总，避免每个渠道各发一条。"""
        if not self._notify or not results:
            return
        lines = []
        for item in results:
            record = item.get("data") if isinstance(item, dict) else None
            record = record if isinstance(record, dict) else {}
            provider_name = str(
                item.get("provider_name") or record.get("provider_name")
                or item.get("provider") or record.get("provider") or "未知渠道"
            )
            points_label = "枫叶" if (
                                             item.get("provider") or record.get("provider")
                                     ) == "p115" else "积分"
            success = bool(item.get("success") or record.get("success"))
            status = str(
                record.get("status") or item.get("message") or
                ("签到成功" if success else "签到失败")
            )
            details = ["成功" if success else "失败", status]
            if record.get("points_change") is not None:
                details.append(
                    f"{points_label} {self._signed_points(record.get('points_change'))}"
                )
            if not success and record.get("message"):
                message = str(record.get("message"))
                if message != status:
                    details.append(message[:36])
            lines.append(f"{provider_name}：{'，'.join(details)}")
        self.post_message(
            mtype=self._notification_type,
            title=f"【网盘订阅】{title}",
            text="\n".join(lines),
        )

    @staticmethod
    def _signed_points(value: Any) -> str:
        try:
            normalized = int(value)
        except (TypeError, ValueError):
            return "未知"
        return f"{normalized:+d}"

    def _build_record(
            self,
            provider: CheckinProvider,
            trigger: str,
            mode: str,
            result: Optional[Dict[str, Any]] = None,
            error: Optional[Exception] = None,
    ) -> Dict[str, Any]:
        data = result or {}
        lottery = data.get("lottery") if isinstance(data, dict) else None
        lottery = lottery if isinstance(lottery, dict) else {}
        success = bool(data.get("success")) if result is not None else False
        default_message = "" if result is not None else str(error or "签到失败")
        return {
            "id": f"{provider.key}-{uuid.uuid4().hex}",
            "provider": provider.key,
            "provider_name": provider.name,
            "executed_at": self._now_text(),
            "trigger": str(trigger or "manual"),
            "mode": mode,
            "success": success,
            "status": str(data.get("status") or (
                "签到成功" if success else "签到失败"
            )),
            "message": str(data.get("message") or default_message),
            "points_change": data.get("points_change"),
            "points_before": data.get("points_before"),
            "points_after": data.get("points_after"),
            "signin_days": data.get("signin_days"),
            "signin_points": data.get("signin_points"),
            "lottery_target_count": lottery.get("target_count"),
            "lottery_executed": lottery.get(
                "used_after", lottery.get("executed")
            ),
            "lottery_cost_points": lottery.get("cost_points"),
            "lottery_award_points": lottery.get("award_points"),
            "lottery_vip_days": lottery.get("vip_days"),
            "http_status": int(
                data.get("status_code")
                or getattr(error, "status_code", 0)
                or getattr(error, "status", 0)
                or 0
            ),
            "error_code": str(
                data.get("error_code")
                or getattr(error, "code", "")
                or ("unexpected_error" if error is not None else "")
            ),
            "captcha_verified": bool(data.get("captcha_verified")),
        }

    def _run_dian115_actions(
            self, client: Any, mode: str
    ) -> Dict[str, Any]:
        """分别执行 Dian115 签到和转盘，再合并为单条业务结果。"""
        before = client.get_account_info()
        signin = client.signin(mode=mode)
        lottery_count = (
            getattr(self, "_dian115_lottery_count", 0)
            if getattr(self, "_dian115_lottery_enabled", False)
            else 0
        )
        lottery = (
            client.run_lottery(lottery_count)
            if lottery_count else {
                "success": True,
                "target_count": 0,
                "executed": 0,
                "cost_points": 0,
                "award_points": 0,
                "vip_days": 0,
            }
        )
        try:
            after = client.get_account_info()
        except Dian115Error:
            after = dict(before)
            fallback_balance = (
                lottery.get("new_balance")
                if lottery.get("new_balance") is not None
                else signin.get("new_balance")
            )
            if fallback_balance is not None:
                after["points"] = fallback_balance
        points_before = int(before.get("points") or 0)
        points_after = int(after.get("points") or 0)
        signin_points = signin.get("award_points")
        if signin_points is None:
            signin_points = (
                    points_after - points_before
                    - int(lottery.get("points_change") or 0)
            )
        signin_label = (
            "今日已签到"
            if signin.get("already_checked_in")
            else f"签到 {self._signed_points(signin_points)}"
        )
        parts = [signin_label]
        if lottery_count:
            parts.append(
                f"转盘 {lottery.get('used_after') or 0}/"
                f"{lottery.get('target_count') or lottery_count}"
            )
        if not lottery.get("success"):
            parts.append(
                f"转盘未完成：{lottery.get('message') or '接口返回失败'}"
            )
        success = bool(signin.get("success") and lottery.get("success"))
        return {
            "success": success,
            "status": (
                "今日已签到" if signin.get("already_checked_in") and not lottery_count
                else "签到完成" if success else "签到未完成"
            ),
            "message": "；".join(parts),
            "mode": mode,
            "signin_points": signin_points,
            "points_change": points_after - points_before,
            "points_before": points_before,
            "points_after": points_after,
            "signin_days": int(
                after.get("consecutive_signin")
                or signin.get("signin_days")
                or 0
            ),
            "status_code": int(
                lottery.get("status_code") or signin.get("status_code") or 0
            ),
            "error_code": str(
                lottery.get("error_code") or signin.get("error_code") or ""
            ),
            "lottery": lottery,
        }

    def _execute_provider_checkin(
            self, adapter: CheckinProvider, client: Any, mode: str
    ) -> Dict[str, Any]:
        if adapter.key == "dian115":
            return self._run_dian115_actions(client, mode)
        if adapter.key == "hdhive":
            return client.checkin(is_gambler=mode == "gambler")
        if adapter.key == "p115":
            return self._run_p115_checkin(client)
        if adapter.key == "quark":
            try:
                return client.checkin(getattr(self, "_quark_checkin_url", ""))
            except Exception as error:
                raise QuarkCheckinError(str(error)) from error
        return client.checkin()

    @staticmethod
    def _run_p115_checkin(client: Any) -> Dict[str, Any]:
        """执行 115 每日签到并领取枫叶。"""
        try:
            status = check_response(client.user_points_sign())
            data = status.get("data") or {}
            if int(data.get("is_sign_today") or 0) == 1:
                return {
                    "success": True,
                    "already_checked_in": True,
                    "status": "今日已签到",
                    "message": "今日已签到，无需重复签到",
                    "signin_points": 0,
                    "points_change": 0,
                    "signin_days": data.get("continuous_day"),
                    "status_code": 200,
                }
            result = check_response(client.user_points_sign_post())
            result_data = result.get("data") or {}
            points = int(result_data.get("points_num") or 0)
            days = result_data.get("continuous_day")
            return {
                "success": True,
                "status": "签到成功",
                "message": f"签到成功，连续签到 {days or 0} 天，获得 {points} 枫叶",
                "signin_points": points,
                "points_change": points,
                "points_after": result_data.get("points") or result_data.get("balance"),
                "signin_days": days,
                "status_code": 200,
            }
        except Exception as error:
            raise P115CheckinError(str(error)) from error

    def _prepare_checkin(
            self, provider: str, mode: str
    ) -> Tuple[
        Optional[CheckinProvider], str, Optional[Dict[str, Any]]
    ]:
        adapter = self._resolve_provider(provider)
        if adapter is None:
            return None, "", {
                "success": False,
                "message": "不支持的签到提供方",
            }
        if not bool(getattr(self, f"_{adapter.key}_checkin_enabled", False)):
            return adapter, "", {
                "success": False,
                "message": f"{adapter.name} 每日签到未启用",
            }
        if not self._checkin_credentials_ready(adapter):
            return adapter, "", {
                "success": False,
                "message": self._checkin_configuration_message(adapter),
            }
        default_mode = getattr(
            self, f"_{adapter.key}_checkin_mode", "normal"
        )
        normalized_mode = str(mode or default_mode).strip().lower()
        if normalized_mode not in adapter.modes:
            return adapter, normalized_mode, {
                "success": False,
                "message": f"{adapter.name} 签到模式无效",
            }
        return adapter, normalized_mode, None

    def start_manual_checkin(
            self, provider: str, mode: str = ""
    ) -> Dict[str, Any]:
        """预占签到锁并后台执行，避免长耗时渠道阻塞 HTTP 请求。"""
        adapter, normalized_mode, error = self._prepare_checkin(provider, mode)
        if error:
            return error
        if not self._run_lock.acquire(blocking=False):
            return {
                "success": False,
                "message": f"{adapter.name} 签到正在执行，请稍后重试",
            }
        try:
            threading.Thread(
                target=self.run_checkin,
                kwargs={
                    "provider": adapter.key,
                    "trigger": "manual",
                    "mode": normalized_mode,
                    "lock_acquired": True,
                },
                daemon=True,
                name=f"pansearch-checkin-{adapter.key}",
            ).start()
        except Exception:
            self._run_lock.release()
            raise
        return {
            "success": True,
            "message": f"{adapter.name} 签到任务已提交",
            "data": {
                "provider": adapter.key,
                "mode": normalized_mode,
                "running": True,
            },
        }

    def run_checkin(
            self,
            provider: str,
            trigger: str = "manual",
            mode: str = "",
            lock_acquired: bool = False,
            notify: bool = True,
    ) -> Dict[str, Any]:
        """执行一次提供方签到；同一插件实例不允许签到并发。"""
        adapter, normalized_mode, error = self._prepare_checkin(provider, mode)
        if error:
            if lock_acquired:
                self._run_lock.release()
            return error
        if not lock_acquired and not self._run_lock.acquire(blocking=False):
            return {
                "success": False,
                "message": f"{adapter.name} 签到正在执行，请稍后重试",
            }

        try:
            try:
                client = self._get_checkin_client(adapter)
                result = self._execute_provider_checkin(
                    adapter, client, normalized_mode
                )
                record = self._build_record(
                    adapter, trigger, normalized_mode, result=result
                )
            except Exception as error:
                if not isinstance(error, adapter.error_types):
                    logger.error(
                        f"{adapter.name} 签到异常："
                        f"{type(error).__name__}: {error}"
                    )
                record = self._build_record(
                    adapter, trigger, normalized_mode, error=error
                )
            if record["success"]:
                self._refresh_checkin_account(adapter, record)
            self._save_history(adapter, record)
            if notify:
                self._notify_checkin(adapter, record)
            log_func = logger.info if record["success"] else logger.warning
            log_func(
                f"{adapter.name} 签到结果："
                f"模式={normalized_mode}，状态={record['status']}，"
                f"积分变化={record['points_change']}，消息={record['message']}"
            )
            return {
                "success": bool(record["success"]),
                "message": record["message"],
                "data": copy.deepcopy(record),
            }
        finally:
            self._run_lock.release()

    def run_quick_checkin(
            self,
            provider: str = "",
            mode: str = "",
    ) -> Dict[str, Any]:
        """供智能体和远程命令复用的签到入口。"""
        provider_key = str(provider or "").strip().lower()
        if provider_key in {"all", "全部"}:
            provider_key = ""
        if provider_key:
            adapter = self._resolve_provider(provider_key)
            if adapter is None:
                return {"success": False, "message": "不支持的签到提供方"}
            providers = [adapter]
        else:
            providers = self._ready_providers()
        if not providers:
            return {"success": False, "message": "没有已启用且配置完整的签到渠道"}

        requested_mode = str(mode or "").strip().lower()
        if requested_mode and not all(
                requested_mode in item.modes for item in providers
        ):
            supported = sorted({mode for item in providers for mode in item.modes})
            return {
                "success": False,
                "message": f"所选渠道签到模式仅支持 {', '.join(supported)}",
            }
        items = []
        aggregate = not provider_key
        for item in providers:
            result = self.run_checkin(
                provider=item.key,
                trigger="manual",
                mode=requested_mode,
                notify=not aggregate,
            )
            public_result = dict(result)
            if isinstance(result.get("data"), dict):
                public_result["data"] = self._public_record(result["data"])
            items.append({
                "provider": item.key,
                "provider_name": item.name,
                **public_result,
            })
        if aggregate:
            self._notify_checkin_summary(items, "签到汇总")
        success = bool(items) and all(item.get("success") for item in items)
        return {
            "success": success,
            "message": (
                f"已完成 {len(items)} 个渠道签到"
                if success else f"已执行 {len(items)} 个渠道，存在签到失败"
            ),
            "data": {"items": items},
        }

    def _ready_providers(self) -> list[CheckinProvider]:
        return [
            provider
            for provider in self._PROVIDERS.values()
            if bool(getattr(
                self, f"_{provider.key}_checkin_enabled", False
            ))
               and self._checkin_credentials_ready(provider)
        ]

    def _today_records(
            self,
            provider: CheckinProvider,
            today: str,
    ) -> list[Dict[str, Any]]:
        records = []
        for record in reversed(self._load_history(provider)):
            try:
                executed_at = datetime.fromisoformat(
                    str(record.get("executed_at") or "")
                )
            except ValueError:
                continue
            if executed_at.tzinfo is None:
                timezone = pytz.timezone(settings.TZ)
                executed_at = timezone.localize(executed_at)
            if executed_at.astimezone(
                    pytz.timezone(settings.TZ)
            ).date().isoformat() == today:
                records.append(record)
        return records

    def _configured_retry_count(self) -> int:
        if not bool(getattr(self, "_checkin_auto_retry", True)):
            return 0
        try:
            value = int(getattr(
                self, "_checkin_retry_count", self._DEFAULT_RETRY_COUNT
            ))
        except (TypeError, ValueError):
            value = self._DEFAULT_RETRY_COUNT
        return max(1, min(value, self._MAX_RETRY_COUNT))

    def _load_schedule_state(
            self,
            today: str,
            providers: list[CheckinProvider],
    ) -> Dict[str, Any]:
        provider_keys = sorted(provider.key for provider in providers)
        stored = self.get_data(self._SCHEDULE_STATE_KEY)
        if (
                isinstance(stored, dict)
                and stored.get("date") == today
                and stored.get("full_completed") is True
                and stored.get("retry_count") == self._configured_retry_count()
                and stored.get("providers") == provider_keys
        ):
            return copy.deepcopy(stored)

        records_by_provider = {
            provider.key: self._today_records(provider, today)
            for provider in providers
        }
        full_completed = any(
            any(record.get("trigger") == "scheduled" for record in records)
            for records in records_by_provider.values()
        )
        retry_count = self._configured_retry_count()
        pending = []
        if full_completed and retry_count:
            for provider in providers:
                records = records_by_provider[provider.key]
                if not any(record.get("success") for record in records):
                    pending.append(provider.key)
        return {
            "date": today,
            "providers": provider_keys,
            "full_completed": full_completed,
            "retry_count": retry_count,
            "pending_providers": pending,
            "completed_retry_count": 0,
        }

    def _execute_scheduled_providers(
            self,
            providers: list[CheckinProvider],
            trigger: str,
    ) -> list[Dict[str, Any]]:
        results = []
        for provider in providers:
            result = self.run_checkin(
                provider=provider.key,
                trigger=trigger,
                notify=False,
            )
            results.append({
                "provider": provider.key,
                "provider_name": provider.name,
                **result,
            })
        return results

    @staticmethod
    def _scheduled_result(
            results: list[Dict[str, Any]],
            trigger: str,
    ) -> Dict[str, Any]:
        success = bool(results) and all(
            item.get("success") for item in results
        )
        label = "首次签到" if trigger == "scheduled" else "异常重试"
        return {
            "success": success,
            "message": (
                f"{label}已执行 {len(results)} 个渠道"
                if success
                else f"{label}存在失败渠道"
                if results
                else "没有需要执行的签到渠道"
            ),
            "data": {"trigger": trigger, "items": results},
        }

    def run_scheduled_checkins(self) -> Dict[str, Any]:
        """单任务入口：每天首次全量签到，随后仅重试失败渠道。"""
        if not self._schedule_lock.acquire(blocking=False):
            return {
                "success": False,
                "message": "签到调度正在执行",
                "data": {"skipped": True, "items": []},
            }
        try:
            now = self._now()
            today = now.date().isoformat()
            providers = self._ready_providers()
            if not providers:
                return self._scheduled_result([], "scheduled")

            state = self._load_schedule_state(
                today=today,
                providers=providers,
            )
            if not state["full_completed"]:
                results = self._execute_scheduled_providers(
                    providers, trigger="scheduled"
                )
                retry_count = self._configured_retry_count()
                state.update({
                    "full_completed": True,
                    "retry_count": retry_count,
                    "pending_providers": [
                        item["provider"]
                        for item in results
                        if retry_count and not item.get("success")
                    ],
                    "completed_retry_count": 0,
                })
                self.save_data(self._SCHEDULE_STATE_KEY, state)
                self._notify_checkin_summary(results, "签到汇总")
                return self._scheduled_result(results, "scheduled")

            retry_count = self._configured_retry_count()
            completed_retry_count = int(
                state.get("completed_retry_count", 0) or 0
            )
            if not retry_count or completed_retry_count >= retry_count:
                return {
                    "success": True,
                    "message": "签到异常重试已关闭或已完成",
                    "data": {"skipped": True, "items": []},
                }

            provider_by_key = {provider.key: provider for provider in providers}
            pending = []
            for key in state.get("pending_providers", []):
                provider = provider_by_key.get(str(key))
                if provider is None:
                    continue
                if any(
                        record.get("success")
                        for record in self._today_records(provider, today)
                ):
                    continue
                pending.append(provider)

            if not pending:
                state["pending_providers"] = []
                state["completed_retry_count"] = retry_count
                self.save_data(self._SCHEDULE_STATE_KEY, state)
                return {
                    "success": True,
                    "message": "没有需要重试的签到渠道",
                    "data": {"skipped": True, "items": []},
                }

            results = self._execute_scheduled_providers(
                pending, trigger="retry"
            )
            state["pending_providers"] = [
                item["provider"]
                for item in results
                if not item.get("success")
            ]
            state["completed_retry_count"] = completed_retry_count + 1
            self.save_data(self._SCHEDULE_STATE_KEY, state)
            self._notify_checkin_summary(results, "签到重试汇总")
            return self._scheduled_result(results, "retry")
        finally:
            self._schedule_lock.release()
