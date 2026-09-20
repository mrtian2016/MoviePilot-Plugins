"""配置保存与智能体配置 API。"""

import json
from typing import Any, Dict, Optional

from app.log import logger
from fastapi import Request

from .account import clear_account_cache
from .page import clear_ui_options_cache
from .. import OwnerDelegator
from ..config import DEFAULT_AUTO_SUBSCRIBE_USERNAME, UIConfig
from ..services.runtime import sync_lock
from ...utils.http_client import build_proxy_url, validate_proxy_address


class ConfigApi(OwnerDelegator):
    AGENT_CONFIG_FIELDS = frozenset(
        {"show_sidebar_nav", "agent_enabled", "notify", "search_cache_enabled", "search_cache_ttl_minutes",
         "search_concurrency", "subscription_concurrency", "pansou_result_limit", "hdhive_candidate_limit"})
    _AGENT_BOOL_FIELDS = frozenset({"show_sidebar_nav", "agent_enabled", "notify", "search_cache_enabled"})
    _AGENT_INT_RANGES = {"search_cache_ttl_minutes": (1, 1440), "search_concurrency": (1, 5),
                         "subscription_concurrency": (1, 5), "pansou_result_limit": (1, 100),
                         "hdhive_candidate_limit": (1, 20), "hdhive_unlocks_per_minute": (1, 3),
                         "dian115_unlocks_per_minute": (1, 10)}

    @staticmethod
    def _validate_search_proxy_config(payload: Dict[str, Any]) -> None:
        """校验并规范化搜索渠道专用代理配置。"""
        proxy = str(payload.get("search_proxy", "") or "").strip()
        username = str(payload.get("search_proxy_username", "") or "").strip()
        password = str(payload.get("search_proxy_password", "") or "")
        normalized = validate_proxy_address(proxy)
        build_proxy_url(normalized, username, password)
        payload["search_proxy"] = normalized
        payload["search_proxy_username"] = username
        payload["search_proxy_password"] = password

    def _validate_checkin_config(
            self,
            payload: Dict[str, Any],
    ) -> Optional[str]:
        providers = self.get_checkin_provider_specs()
        for provider in providers:
            mode_key = f"{provider['key']}_checkin_mode"
            mode = str(
                payload.get(mode_key, "normal") or "normal"
            ).strip().lower()
            if mode not in provider["modes"]:
                return f"{provider['name']} 签到模式无效"
            payload[mode_key] = mode
        try:
            lottery_count = int(
                payload.get("dian115_lottery_count", 1) or 1
            )
        except (TypeError, ValueError):
            return "Dian115 幸运转盘次数必须是整数"
        if not 1 <= lottery_count <= 20:
            return "Dian115 幸运转盘次数需在 1-20 次之间"
        payload["dian115_lottery_count"] = lottery_count
        payload["dian115_lottery_enabled"] = bool(
            payload.get("dian115_lottery_enabled", False)
        )
        cron = str(
            payload.get("checkin_cron", "0 8 * * *")
            or "0 8 * * *"
        ).strip()
        enabled = any(
            payload.get(f"{provider['key']}_checkin_enabled")
            for provider in providers
        )
        if enabled and not self._cron_is_valid(cron):
            return "签到服务 Cron 表达式无效"
        payload["checkin_cron"] = cron
        payload.pop("checkin_retry_period", None)
        payload["checkin_auto_retry"] = bool(
            payload.get("checkin_auto_retry", True)
        )
        try:
            retry_count = int(payload.get("checkin_retry_count", 2) or 2)
        except (TypeError, ValueError):
            return "自动重试次数必须是整数"
        if not 1 <= retry_count <= 10:
            return "自动重试次数需在 1-10 次之间"
        payload["checkin_retry_count"] = retry_count
        return None

    def _queue_pending_config(self, payload: Dict[str, Any]) -> None:
        """保存运行期间最后一次配置，等待同步任务结束后应用。"""
        with self._pending_config_lock:
            self._pending_config = dict(payload)

    def _apply_pending_config(self) -> bool:
        """应用运行期间暂存的最新配置；调用方必须持有全局同步锁。"""
        with self._pending_config_lock:
            payload = self._pending_config
            self._pending_config = None
        if not payload:
            return False

        try:
            self._apply_plugin_config(payload, reset_runtime=False)
            logger.info("待更新配置已自动应用")
            return True
        except Exception as error:
            with self._pending_config_lock:
                if self._pending_config is None:
                    self._pending_config = payload
            logger.error(f"应用待更新配置失败，将在下次同步前重试：{error}")
            return False

    async def api_vue_save_config(self, request: Request) -> dict:
        """在 Vue 配置页内保存配置，避免触发宿主关闭弹窗。"""
        try:
            payload = await request.json()
            if not isinstance(payload, dict):
                return {"success": False, "message": "配置数据格式错误"}
            self._validate_search_proxy_config(payload)
            auto_subscribe_error = self._validate_auto_subscribe_config(payload)
            if auto_subscribe_error:
                return {"success": False, "message": auto_subscribe_error}
            checkin_error = self._validate_checkin_config(payload)
            if checkin_error:
                return {"success": False, "message": checkin_error}
            self.update_config(payload)
            clear_ui_options_cache()
            clear_account_cache()
            if not sync_lock.acquire(blocking=False):
                self._queue_pending_config(payload)
                return {
                    "success": True,
                    "message": "配置已保存，将在当前订阅任务结束后自动生效",
                    "data": payload,
                }
            try:
                self._apply_plugin_config(payload, reset_runtime=False)
            finally:
                sync_lock.release()
            return {"success": True, "message": "配置已保存并生效", "data": payload}
        except Exception as error:
            logger.error(f"保存插件配置失败：{error}")
            return {"success": False, "message": str(error)}

    def _validate_auto_subscribe_config(
            self, payload: Dict[str, Any]
    ) -> Optional[str]:
        providers = ("douban", "maoyan", "netflix", "mikan", "tmdb", "bangumi", "anilist")
        existing_config = getattr(self, "_applied_config", None) or {}
        for provider_id in providers:
            enabled_key = f"auto_subscribe_{provider_id}_enabled"
            payload[enabled_key] = bool(
                payload.get(
                    enabled_key,
                    existing_config.get(enabled_key, False),
                )
            )
        cron = str(payload.get("auto_subscribe_cron") or "0 8 * * *").strip()
        if payload.get("auto_subscribe_enabled") and not self._cron_is_valid(cron):
            return "自动订阅 Cron 表达式无效"
        payload["auto_subscribe_cron"] = cron
        payload["auto_subscribe_onlyonce"] = bool(
            payload.get("auto_subscribe_onlyonce", False)
        )
        username = str(
            payload.get("auto_subscribe_username") or DEFAULT_AUTO_SUBSCRIBE_USERNAME
        ).strip()
        if len(username) > 64:
            return "自动订阅用户名不能超过 64 个字符"
        payload["auto_subscribe_username"] = username
        payload["auto_subscribe_notify"] = bool(payload.get("auto_subscribe_notify", False))
        payload["auto_subscribe_skip_season_zero"] = bool(
            payload.get("auto_subscribe_skip_season_zero", True)
        )
        proxy_enabled = False
        for provider_id in providers:
            proxy_key = f"auto_subscribe_{provider_id}_proxy"
            payload[proxy_key] = bool(payload.get(proxy_key, False))
            proxy_enabled = proxy_enabled or payload[proxy_key]
        proxy = str(payload.get("auto_subscribe_proxy", "") or "").strip()
        username = str(payload.get("auto_subscribe_proxy_username", "") or "").strip()
        password = str(payload.get("auto_subscribe_proxy_password", "") or "")
        if proxy or username or password:
            try:
                normalized = validate_proxy_address(proxy)
                build_proxy_url(normalized, username, password)
            except ValueError as error:
                return f"榜单代理配置无效：{error}"
            payload["auto_subscribe_proxy"] = normalized
        else:
            payload["auto_subscribe_proxy"] = ""
        payload["auto_subscribe_proxy_username"] = username
        payload["auto_subscribe_proxy_password"] = password
        if proxy_enabled and not payload["auto_subscribe_proxy"]:
            return "已启用榜单代理，请先填写榜单代理地址"
        provider_limits = {
            "douban": 30,
            "tmdb": 20,
            "bangumi": 50,
            "anilist": 30,
            "maoyan": 10,
            "netflix": 10,
            "mikan": 100,
        }
        for provider_id, default_limit in provider_limits.items():
            limit_key = f"auto_subscribe_{provider_id}_limit"
            raw_limit = payload.get(
                limit_key,
                existing_config.get(limit_key, default_limit),
            )
            if isinstance(raw_limit, bool):
                return f"{provider_id} 每榜条数必须是整数"
            try:
                limit = int(raw_limit)
            except (TypeError, ValueError):
                return f"{provider_id} 每榜条数必须是整数"
            if not 1 <= limit <= 100:
                return f"{provider_id} 每榜条数需在 1-100 之间"
            payload[limit_key] = limit
        UIConfig.normalize_auto_subscribe_dates(payload)
        service_urls = {
            "auto_subscribe_douban_rsshub_base": "https://rsshub.app",
            "auto_subscribe_maoyan_base_url": "https://piaofang.maoyan.com",
            "auto_subscribe_netflix_base_url": "https://www.netflix.com",
        }
        for key, default_value in service_urls.items():
            value = str(payload.get(key) or default_value).strip().rstrip("/")
            if not value.startswith(("http://", "https://")):
                return f"{key} 必须是 http(s) 服务地址"
            payload[key] = value
        rss_urls = payload.get("auto_subscribe_douban_rss_urls") or []
        if isinstance(rss_urls, str):
            rss_urls = rss_urls.splitlines()
        normalized_rss_urls = []
        for value in rss_urls:
            value = str(value or "").strip()
            if not value:
                continue
            if not value.startswith(("http://", "https://")):
                return "豆瓣自定义 RSS 地址必须以 http:// 或 https:// 开头"
            if value not in normalized_rss_urls:
                normalized_rss_urls.append(value)
        payload["auto_subscribe_douban_rss_urls"] = normalized_rss_urls
        mikan_urls = (
                payload.get("auto_subscribe_mikan_base_urls")
                or existing_config.get("auto_subscribe_mikan_base_urls")
                or ["https://mikanani.me", "https://mikanime.tv"]
        )
        if isinstance(mikan_urls, str):
            mikan_urls = [mikan_urls]
        normalized_mikan_urls = []
        for value in mikan_urls:
            value = str(value or "").strip().rstrip("/")
            if not value:
                continue
            if not value.startswith(("http://", "https://")):
                return "Mikan 服务地址必须以 http:// 或 https:// 开头"
            if value not in normalized_mikan_urls:
                normalized_mikan_urls.append(value)
        if not normalized_mikan_urls:
            return "Mikan 至少需要配置一个服务地址"
        payload["auto_subscribe_mikan_base_urls"] = normalized_mikan_urls
        for provider_id in ("douban", "maoyan"):
            media_type_key = f"auto_subscribe_{provider_id}_media_type"
            media_type = str(payload.get(media_type_key) or "all").strip().lower()
            if media_type not in {"all", "movie", "tv"}:
                return f"{provider_id} 媒体类型配置无效"
            payload[media_type_key] = media_type
        maoyan_map = payload.get("auto_subscribe_maoyan_web_platform_map", {})
        if not isinstance(maoyan_map, dict):
            return "猫眼平台与类型配置格式错误"
        country_selections = payload.get(
            "auto_subscribe_netflix_country_selections", {}
        )
        if isinstance(country_selections, str):
            try:
                parsed = json.loads(country_selections or "{}")
            except (TypeError, ValueError):
                return "Netflix 国家/地区榜配置必须是有效 JSON"
            if not isinstance(parsed, dict):
                return "Netflix 国家/地区榜配置必须是 JSON 对象"
            country_selections = parsed
        elif not isinstance(country_selections, dict):
            return "Netflix 国家/地区榜配置格式错误"
        payload["auto_subscribe_netflix_country_selections"] = country_selections
        if (
                "auto_subscribe_netflix_global_media_types" not in payload
                and "auto_subscribe_netflix_global_categories" in payload
        ):
            payload["auto_subscribe_netflix_global_media_types"] = payload.get(
                "auto_subscribe_netflix_global_categories"
            )
        try:
            max_workers = int(payload.get("auto_subscribe_netflix_max_workers", 4) or 4)
        except (TypeError, ValueError):
            return "Netflix 富模式并发数必须是整数"
        if not 1 <= max_workers <= 16:
            return "Netflix 富模式并发数需在 1-16 之间"
        payload["auto_subscribe_netflix_max_workers"] = max_workers
        payload["auto_subscribe_netflix_rich_metadata"] = bool(
            payload.get("auto_subscribe_netflix_rich_metadata", False)
        )
        payload["auto_subscribe_netflix_use_cache"] = bool(
            payload.get("auto_subscribe_netflix_use_cache", True)
        )
        return None

    def update_agent_config(self, updates: Dict[str, Any]) -> dict:
        """校验并应用智能体允许修改的非敏感配置。"""
        updates = dict(updates or {})
        if not updates:
            return {"success": False, "message": "没有提供需要修改的配置"}
        unknown = sorted(set(updates) - self.AGENT_CONFIG_FIELDS)
        if unknown:
            return {
                "success": False,
                "message": f"不允许智能体修改这些配置：{', '.join(unknown)}",
            }

        normalized: Dict[str, Any] = {}
        for key, value in updates.items():
            if key in self._AGENT_BOOL_FIELDS:
                if not isinstance(value, bool):
                    return {"success": False, "message": f"配置 {key} 必须是布尔值"}
                normalized[key] = value
                continue
            minimum, maximum = self._AGENT_INT_RANGES[key]
            if isinstance(value, bool) or not isinstance(value, int):
                return {"success": False, "message": f"配置 {key} 必须是整数"}
            if not minimum <= value <= maximum:
                return {
                    "success": False,
                    "message": f"配置 {key} 必须在 {minimum} 到 {maximum} 之间",
                }
            normalized[key] = value

        payload = dict(self._applied_config or UIConfig.get_default_config())
        changed = {
            key: value
            for key, value in normalized.items()
            if payload.get(key) != value
        }
        if not changed:
            return {"success": True, "message": "配置未变化", "data": {"changed": {}}}
        payload.update(changed)
        self.update_config(payload)
        if not sync_lock.acquire(blocking=False):
            self._queue_pending_config(payload)
            return {
                "success": True,
                "message": "配置已保存，将在当前订阅任务结束后自动生效",
                "data": {"changed": changed, "pending": True},
            }
        try:
            self._apply_plugin_config(payload, reset_runtime=False)
        finally:
            sync_lock.release()
        return {
            "success": True,
            "message": "配置已保存并生效",
            "data": {"changed": changed, "pending": False},
        }
