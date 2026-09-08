"""网盘与搜索源账户 API。"""

import asyncio
import copy
import inspect
import secrets
import time
from threading import RLock
from typing import Any, Dict, Tuple
from urllib.parse import parse_qs, urlparse

from app.log import logger
from fastapi import Request

from .page import clear_ui_options_cache
from .. import CloudDriveCapability, OwnerDelegator
from ...utils.cache import create_platform_ttl_cache

_ACCOUNT_INFO_CACHE = create_platform_ttl_cache(
    "account:info", maxsize=16, ttl=5 * 60
)
_ACCOUNT_REFRESH_GUARD = create_platform_ttl_cache(
    "account:refresh_guard", maxsize=16, ttl=30
)
_ACCOUNT_INFO_LOCK = RLock()
_HDHIVE_OAUTH_PENDING = create_platform_ttl_cache(
    "hdhive:oauth_pending", maxsize=16, ttl=10 * 60
)
_HDHIVE_OAUTH_LOCK = RLock()


class AccountApi(OwnerDelegator):
    @staticmethod
    def _search_account_card(
            source: str, info: Dict[str, Any]
    ) -> Dict[str, Any]:
        """将搜索渠道账户数据转换为通用信息卡片。"""
        badge = str(info.get("level") or info.get("role") or "").strip()
        if badge.lower() == "vip":
            badge = "VIP"
        if source == "hdhive" and info.get("is_vip"):
            badge = "VIP"

        details = []

        def add_detail(label: str, value: Any) -> None:
            text = str(value or "").strip()
            if text:
                details.append({"label": label, "value": text})

        name = str(info.get("name") or "").strip()
        email = str(info.get("email") or "").strip().lower()
        if not name or "@" in name or (email and name.lower() == email):
            name = {
                "hdhive": "HDHive 用户",
                "dian115": "Dian115 用户",
                "juying": "聚影用户",
                "pinglian": "盘链用户",
            }.get(source, "渠道用户")
        if source == "hdhive":
            add_detail("会员状态", "VIP" if info.get("is_vip") else "普通用户")
            add_detail("累计签到", f"{int(info.get('signin_days') or 0)} 天")
            add_detail("分享数量", f"{int(info.get('share_count') or 0)} 个")
            status = {
                "active": "正常",
                "inactive": "未激活",
                "suspended": "已停用",
            }.get(str(info.get("status") or "").lower())
            add_detail("账户状态", status)
        elif source == "dian115":
            add_detail("会员状态", "VIP" if info.get("is_vip") else "普通用户")
            add_detail(
                "连续签到", f"{int(info.get('consecutive_signin') or 0)} 天"
            )
            add_detail("已解锁", f"{int(info.get('unlock_count') or 0)} 次")
        elif source == "pinglian":
            add_detail("会员到期", info.get("expires_at"))
            add_detail("注册日期", info.get("registered_at"))
            add_detail("邀请用户", info.get("invite_count"))
        else:
            add_detail("累计签到", f"{int(info.get('checkin_days') or 0)} 天")
            add_detail("上传资源", f"{int(info.get('upload_count') or 0)} 个")
            add_detail("收藏资源", f"{int(info.get('favorite_count') or 0)} 个")
        return {
            "connected": True,
            "user": {
                "name": name,
                "avatar": str(info.get("avatar") or ""),
                "membership_supported": False,
                "badge": badge,
            },
            "points": {
                "label": "金币余额" if source == "pinglian" else "可用积分",
                "available": max(0, int(info.get("points") or 0)),
            },
            "details": details,
        }

    def _load_search_account(self, source: str) -> Dict[str, Any]:
        """读取单个搜索渠道的账户信息。"""
        from ...search.dian115 import Dian115Client
        from ...search.hdhive import HDHiveClient
        from ...search.juying import JuyingClient
        source = str(source or "").strip().lower()
        client = None
        close_client = False
        try:
            if source == "hdhive":
                if self._hdhive_query_mode == "api":
                    if not self._hdhive_client or not self._hdhive_client.is_ready:
                        return {
                            "connected": False,
                            "error": "请先完成 HDHive OpenAPI 用户授权并保存配置",
                        }
                    data = self._hdhive_client.get_me().get("data") or {}
                    level = str(data.get("level") or "").strip().lower()
                    return self._search_account_card(source, {
                        "name": data.get("nickname") or data.get("username"),
                        "avatar": data.get("avatar_url"),
                        "points": data.get("points"),
                        "level": level,
                        "is_vip": level in {"vip", "forever_vip"},
                        "signin_days": data.get("signin_days_total"),
                        "share_count": data.get("share_num"),
                        "status": "suspended" if data.get("is_blocked") else "active",
                    })
                if not self._hdhive_username or not self._hdhive_password:
                    return {
                        "connected": False,
                        "error": "请填写 HDHive 用户名和密码并保存配置",
                    }
                client = HDHiveClient(
                    username=self._hdhive_username,
                    password=self._hdhive_password,
                    proxy=self._search_proxy,
                    request_interval=self._hdhive_request_interval,
                    timeout=10,
                )
                close_client = True
            elif source == "dian115":
                if not self._dian115_email or not self._dian115_password:
                    return {
                        "connected": False,
                        "error": "请填写 Dian115 邮箱和密码并保存配置",
                    }
                client = Dian115Client(
                    email=self._dian115_email,
                    password=self._dian115_password,
                    base_url=self._dian115_base_url,
                    proxy=self._search_proxy,
                    request_interval=self._dian115_request_interval,
                    unlocks_per_minute=self._dian115_unlocks_per_minute,
                    timeout=10,
                    get_data_func=self.get_data,
                    save_data_func=self.save_data,
                )
                close_client = True
            elif source == "juying":
                if not self._juying_username or not self._juying_password:
                    return {
                        "connected": False,
                        "error": "请填写聚影账号和密码并保存配置",
                    }
                client = JuyingClient(
                    username=self._juying_username,
                    password=self._juying_password,
                    proxy=self._search_proxy,
                    request_timeout=10,
                    request_interval=self._juying_request_interval,
                    get_data_func=self.get_data,
                    save_data_func=self.save_data,
                )
                close_client = True
            elif source == "pinglian":
                if not self._pinglian_username or not self._pinglian_password:
                    return {
                        "connected": False,
                        "error": "请填写盘链账号和密码并保存配置",
                    }
                client = PinglianClient(
                    username=self._pinglian_username,
                    password=self._pinglian_password,
                    proxy=self._search_proxy,
                    request_timeout=min(self._pinglian_timeout, 30),
                    request_interval=self._pinglian_request_interval,
                    get_data_func=self.get_data,
                    save_data_func=self.save_data,
                )
                close_client = True
            else:
                raise ValueError("不支持的搜索账户")
            return self._search_account_card(source, client.get_account_info())
        except Exception as error:
            logger.debug(f"读取{source}搜索账户信息失败：{error}")
            return {
                "connected": False,
                "error": "账户信息读取失败，请检查登录凭据或稍后重试",
            }
        finally:
            if client and close_client:
                client.close()

    def _load_drive_account(
            self, provider_key: str, force: bool = False
    ) -> Dict[str, Any]:
        """读取单个网盘账户；手动刷新时绕过支持的内部缓存。"""
        if not self._cloud_drive_registry:
            return {"connected": False, "error": "网盘服务尚未初始化"}
        try:
            provider = self._cloud_drive_registry.get(provider_key)
            if not provider.supports(CloudDriveCapability.ACCOUNT):
                return {"connected": False, "error": "当前网盘不支持账户信息"}
            service = provider.require(CloudDriveCapability.ACCOUNT)
            getter = service.get_account_info
            parameters = inspect.signature(getter).parameters
            if force and "cache_ttl" in parameters:
                return getter(cache_ttl=0)
            return getter()
        except Exception as error:
            logger.debug(f"读取{provider_key}网盘账户信息失败：{error}")
            return {
                "connected": False,
                "error": "网盘账户信息读取失败，请检查凭据或稍后重试",
            }

    def _account_info(
            self, account_key: str, refresh: bool = False
    ) -> Tuple[Dict[str, Any], bool]:
        """读取单卡片信息，使用独立数据库快照并实施刷新冷却。"""
        normalized_key = str(account_key or "").strip().lower()
        if ":" not in normalized_key:
            raise ValueError("账户卡片标识无效")
        category, source = normalized_key.split(":", 1)
        if category not in {"drive", "search"} or not source:
            raise ValueError("账户卡片标识无效")

        with _ACCOUNT_INFO_LOCK:
            stored_account = self._get_data_store().load_account(normalized_key)
            cached_account = (
                    _ACCOUNT_INFO_CACHE.get(normalized_key) or stored_account
            )
            if cached_account:
                _ACCOUNT_INFO_CACHE.set(normalized_key, cached_account)
            if refresh and _ACCOUNT_REFRESH_GUARD.get(normalized_key):
                return cached_account or {
                    "connected": False,
                    "error": "账户信息正在冷却，请稍后再刷新",
                }, True
            if not refresh and cached_account:
                return cached_account, False
            _ACCOUNT_REFRESH_GUARD.set(normalized_key, True)

        account = (
            self._load_drive_account(source, force=refresh)
            if category == "drive"
            else self._load_search_account(source)
        )
        account["refreshed_at"] = int(time.time())
        with _ACCOUNT_INFO_LOCK:
            _ACCOUNT_INFO_CACHE.set(normalized_key, account)
            self._get_data_store().save_account(normalized_key, account)
        return account, False

    def _cached_account_info(
            self, account_key: str, fallback: Dict[str, Any]
    ) -> Dict[str, Any]:
        """配置页只读取内存或独立数据库快照，不访问第三方接口。"""
        with _ACCOUNT_INFO_LOCK:
            cached = _ACCOUNT_INFO_CACHE.get(account_key)
            if cached:
                return cached
            account = self._get_data_store().load_account(account_key)
            if account:
                _ACCOUNT_INFO_CACHE.set(account_key, account)
                return account
            return fallback

    def update_search_account_points(
            self,
            source: str,
            points: Any,
            signin_days: Any = None,
    ) -> bool:
        """用签到结果更新搜索渠道账户快照，避免重复请求第三方接口。"""
        account_key = f"search:{str(source or '').strip().lower()}"
        try:
            normalized_points = max(0, int(points))
        except (TypeError, ValueError):
            return False
        try:
            normalized_days = (
                max(0, int(signin_days))
                if signin_days is not None else None
            )
        except (TypeError, ValueError):
            normalized_days = None

        with _ACCOUNT_INFO_LOCK:
            account = (
                    _ACCOUNT_INFO_CACHE.get(account_key)
                    or self._get_data_store().load_account(account_key)
            )
            if not isinstance(account, dict) or not account.get("connected"):
                return False
            account = copy.deepcopy(account)
            point_info = dict(account.get("points") or {})
            point_info["available"] = normalized_points
            account["points"] = point_info
            if normalized_days is not None:
                details = list(account.get("details") or [])
                for item in details:
                    if (
                            isinstance(item, dict)
                            and item.get("label") in {"累计签到", "连续签到"}
                    ):
                        item["value"] = f"{normalized_days} 天"
                        break
                account["details"] = details
            account["refreshed_at"] = int(time.time())
            _ACCOUNT_INFO_CACHE.set(account_key, account)
            _ACCOUNT_REFRESH_GUARD.set(account_key, True)
            self._get_data_store().save_account(account_key, account)
        return True

    async def api_vue_refresh_account(self, request: Request) -> dict:
        """手动刷新单个账户信息卡片，不联动其他卡片或 Tab。"""
        try:
            payload = await request.json()
            account_key = str(
                payload.get("key") if isinstance(payload, dict) else ""
            ).strip()
            account, limited = await asyncio.to_thread(
                self._account_info, account_key, True
            )
            category, source = account_key.lower().split(":", 1)
            with _ACCOUNT_INFO_LOCK:
                clear_ui_options_cache()
            return {
                "success": True,
                "message": (
                    "刷新过于频繁，已显示最近一次账户信息"
                    if limited else "账户信息已刷新"
                ),
                "data": {
                    "key": f"{category}:{source}",
                    "account": account,
                    "limited": limited,
                },
            }
        except Exception as error:
            logger.debug(f"手动刷新账户信息失败：{error}")
            return {"success": False, "message": f"刷新账户信息失败：{error}"}

    @staticmethod
    def _hdhive_oauth_values(payload: Dict[str, Any]) -> Tuple[str, str, str, str]:
        """校验授权发起和换 Token 共用的 OpenAPI 参数。"""
        client_id = str(payload.get("client_id") or "").strip()
        redirect_uri = str(payload.get("redirect_uri") or "").strip()
        response_mode = str(payload.get("response_mode") or "redirect").strip().lower()
        requested_scopes = [
            value for value in str(
                payload.get("scope") or "query unlock write"
            ).split()
            if value in {"query", "unlock", "write"}
        ]
        scopes = list(dict.fromkeys(requested_scopes))
        if "query" not in scopes:
            scopes.insert(0, "query")
        if "write" not in scopes:
            scopes.append("write")
        if not client_id:
            raise ValueError("请填写 HDHive OpenAPI Client ID")
        parsed = urlparse(redirect_uri)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.fragment:
            raise ValueError("OAuth Redirect URI 必须是无 fragment 的完整 HTTP/HTTPS 地址")
        if response_mode not in {"redirect", "postmessage"}:
            raise ValueError("当前插件仅支持 redirect 或 postmessage 授权回调")
        return client_id, redirect_uri, " ".join(scopes), response_mode

    def api_vue_hdhive_oauth_start(self, payload: Dict[str, Any]) -> dict:
        """生成带服务端 state 记录的 HDHive OpenAPI 授权链接。"""
        from ...search.hdhive import HDHiveOpenAPIClient

        try:
            payload = dict(payload or {})
            client_id, redirect_uri, scope, response_mode = self._hdhive_oauth_values(payload)
            state = secrets.token_urlsafe(32)
            client = HDHiveOpenAPIClient(
                app_secret="",
                client_id=client_id,
                proxy=self._search_proxy,
                request_interval=self._hdhive_request_interval,
            )
            try:
                authorize_url = client.build_authorize_url(
                    redirect_uri=redirect_uri,
                    scope=scope,
                    state=state,
                    response_mode=response_mode,
                )
            finally:
                client.close()
            with _HDHIVE_OAUTH_LOCK:
                _HDHIVE_OAUTH_PENDING.set(state, {
                    "client_id": client_id,
                    "redirect_uri": redirect_uri,
                    "scope": scope,
                    "response_mode": response_mode,
                })
            return {
                "success": True,
                "message": "HDHive 授权页已准备，请在 10 分钟内完成授权",
                "data": {
                    "authorize_url": authorize_url,
                    "state": state,
                    "response_mode": response_mode,
                    "expires_in": 600,
                },
            }
        except Exception as error:
            return {"success": False, "message": str(error)}

    def api_vue_hdhive_oauth_exchange(self, payload: Dict[str, Any]) -> dict:
        """校验 OAuth state，使用授权码换 Token 并返回当前用户摘要。"""
        from ...search.hdhive import HDHiveOpenAPIClient, HDHiveOpenAPIError

        client = None
        try:
            payload = dict(payload or {})
            client_id, redirect_uri, _scope, response_mode = self._hdhive_oauth_values(payload)
            app_secret = str(payload.get("app_secret") or "").strip()
            if not app_secret:
                raise ValueError("请填写 HDHive OpenAPI 应用 Secret")

            code = str(payload.get("code") or "").strip()
            state = str(payload.get("state") or "").strip()
            callback = str(payload.get("callback") or "").strip()
            if callback:
                parsed_callback = urlparse(callback)
                query = parsed_callback.query if parsed_callback.scheme else callback.lstrip("?")
                values = parse_qs(query, keep_blank_values=True)
                code = str((values.get("code") or [code])[0] or "").strip()
                state = str((values.get("state") or [state])[0] or "").strip()
            if not code or not state:
                raise ValueError("请粘贴包含 code 和 state 的完整回调 URL")

            with _HDHIVE_OAUTH_LOCK:
                pending = _HDHIVE_OAUTH_PENDING.get(state)
            if not pending:
                raise ValueError("授权 state 已失效，请重新打开授权页")
            if (
                    pending.get("client_id") != client_id
                    or pending.get("redirect_uri") != redirect_uri
                    or pending.get("response_mode") != response_mode
            ):
                raise ValueError("授权回调与发起授权时的应用配置不一致")

            client = HDHiveOpenAPIClient(
                app_secret=app_secret,
                client_id=client_id,
                proxy=self._search_proxy,
                request_interval=self._hdhive_request_interval,
            )
            token_data = client.exchange_code(code, redirect_uri)
            warning = ""
            user = {}
            try:
                user = client.get_me().get("data") or {}
            except HDHiveOpenAPIError as error:
                warning = f"Token 已获取，但读取授权用户失败：[{error.code}] {error.message}"
            with _HDHIVE_OAUTH_LOCK:
                _HDHIVE_OAUTH_PENDING.delete(state)
            return {
                "success": True,
                "message": "HDHive OpenAPI 用户授权成功",
                "data": {
                    "access_token": client.access_token,
                    "refresh_token": client.refresh_token,
                    "token_expires_at": client.token_expires_at,
                    "expires_in": token_data.get("expires_in") or 0,
                    "refresh_expires_in": token_data.get("refresh_expires_in") or 0,
                    "scope": token_data.get("scope") or pending.get("scope") or "",
                    "scopes": token_data.get("scopes") or [],
                    "user": user,
                    "warning": warning,
                },
            }
        except HDHiveOpenAPIError as error:
            return {
                "success": False,
                "message": f"[{error.code}] {error.message} {error.description}".strip(),
            }
        except Exception as error:
            return {"success": False, "message": str(error)}
        finally:
            if client:
                client.close()


def clear_account_cache(account_key: str = "") -> None:
    normalized_key = str(account_key or "").strip().lower()
    with _ACCOUNT_INFO_LOCK:
        if normalized_key:
            _ACCOUNT_INFO_CACHE.delete(normalized_key)
            _ACCOUNT_REFRESH_GUARD.delete(normalized_key)
        else:
            _ACCOUNT_INFO_CACHE.clear()
            _ACCOUNT_REFRESH_GUARD.clear()
