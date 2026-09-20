"""
HDHive OpenAPI 客户端
基于官方 Python SDK 适配：应用 Secret (X-API-Key) + OAuth 用户 Access Token (Bearer) 双层认证
参考文档: https://re0.me/docs/open
"""
import json
import secrets
import threading
import time
import urllib.parse
from typing import Any, Callable, Dict, Optional

from app.log import logger

from ...http_client import RequestGate, gated_request, normalize_proxies, requests
from ....utils.cache import (
    cached_resource_call,
    create_platform_ttl_cache,
    normalize_platform_cache_key,
)


class HDHiveOpenAPIError(Exception):
    """HDHive OpenAPI 错误"""

    def __init__(self, code: str, message: str, description: str = "", status: int = 0):
        super().__init__(description or message or code)
        self.code = code
        self.message = message
        self.description = description
        self.status = status


class HDHiveOpenAPIClient:
    """
    HDHive OpenAPI 客户端

    认证模型:
    - 应用 Secret: 所有 /api/open/* 和 OAuth 接口都放在 X-API-Key 请求头
    - 用户 Access Token: 业务接口（资源查询/解锁等）附加 Authorization: Bearer
    - Access Token 过期时自动用 Refresh Token 刷新，并通过回调持久化新 Token
    """

    DEFAULT_SCOPE = "query unlock write"
    _RESOURCE_CACHE_TTL = 10 * 60
    _RESOURCE_CACHE_LIMIT = 256
    _DETAIL_CACHE_TTL = 10 * 60
    _DETAIL_CACHE_LIMIT = 512
    _RISK_COOLDOWN_SECONDS = 60
    _SERVER_ERROR_COOLDOWN_SECONDS = 5

    def __init__(
            self,
            app_secret: str,
            client_id: str = "",
            access_token: str = "",
            refresh_token: str = "",
            token_expires_at: float = 0,
            base_url: str = "https://re0.me",
            proxy: Any = None,
            timeout: int = 30,
            request_interval: float = 1.0,
            on_token_update: Optional[Callable[[Dict[str, Any]], None]] = None,
    ):
        """
        :param app_secret: OpenAPI 应用 Secret（X-API-Key）
        :param client_id: 应用公开 Client ID（用于生成授权链接）
        :param access_token: 用户 Access Token
        :param refresh_token: 用户 Refresh Token
        :param token_expires_at: Access Token 过期时间戳（秒），0 表示未知
        :param base_url: HDHive 站点地址
        :param proxy: 代理配置（字符串或 requests 格式字典）
        :param timeout: 请求超时秒数
        :param on_token_update: Token 刷新后的持久化回调，参数为
                                {"access_token", "refresh_token", "token_expires_at"}
        """
        self.app_secret = (app_secret or "").strip()
        self.client_id = (client_id or "").strip()
        self.access_token = (access_token or "").strip()
        self.refresh_token = (refresh_token or "").strip()
        self.token_expires_at = float(token_expires_at or 0)
        self.base_url = (base_url or "https://re0.me").rstrip("/")
        self.timeout = max(5, min(int(timeout or 30), 120))
        self.request_interval = max(
            0.2, min(float(request_interval or 1.0), 10.0)
        )
        self.on_token_update = on_token_update
        self._proxies = normalize_proxies(proxy)
        self._session = requests.Session(impersonate="chrome")
        cache_identity = f"{self.base_url}|{self.client_id}|{self.access_token}"
        self._resource_cache = create_platform_ttl_cache(
            "hdhive_open:resources", cache_identity,
            maxsize=self._RESOURCE_CACHE_LIMIT, ttl=self._RESOURCE_CACHE_TTL,
        )
        self._detail_cache = create_platform_ttl_cache(
            "hdhive_open:details", cache_identity,
            maxsize=self._DETAIL_CACHE_LIMIT, ttl=self._DETAIL_CACHE_TTL,
        )
        self._resource_locks = tuple(threading.Lock() for _ in range(16))
        self._detail_locks = tuple(threading.Lock() for _ in range(32))
        self._lock = threading.RLock()
        self._request_gate = RequestGate.shared(
            "HDHive OpenAPI",
            cache_identity,
            request_interval=self.request_interval,
            minimum_interval=0.2,
            risk_cooldown_seconds=self._RISK_COOLDOWN_SECONDS,
            server_error_cooldown_seconds=self._SERVER_ERROR_COOLDOWN_SECONDS,
        )

    @property
    def is_ready(self) -> bool:
        """应用 Secret 和用户 Token 均已配置，可调用业务接口"""
        return bool(self.app_secret and self.access_token)

    def close(self) -> None:
        with self._lock:
            self._session.close()
            self._resource_cache.clear()
            self._detail_cache.clear()

    def clear_cache(self) -> Dict[str, int]:
        """清空 OpenAPI 资源列表和分享详情缓存。"""
        with self._lock:
            counts = {
                "resources": len(list(self._resource_cache.items())),
                "details": len(list(self._detail_cache.items())),
            }
            self._resource_cache.clear()
            self._detail_cache.clear()
            return counts

    def build_authorize_url(
            self,
            redirect_uri: str,
            scope: str = "",
            state: str = "",
            response_mode: str = "redirect",
    ) -> str:
        """生成用户授权页 URL。state 必须由调用方保存并在回调时校验。"""
        if not self.client_id:
            raise HDHiveOpenAPIError("400", "缺少 OpenAPI Client ID")
        redirect_uri = str(redirect_uri or "").strip()
        parsed_redirect = urllib.parse.urlparse(redirect_uri)
        if (
                parsed_redirect.scheme not in {"http", "https"}
                or not parsed_redirect.netloc
                or parsed_redirect.fragment
        ):
            raise HDHiveOpenAPIError(
                "400", "OAuth Redirect URI 必须是无 fragment 的完整 HTTP/HTTPS 地址"
            )
        response_mode = str(response_mode or "redirect").strip().lower()
        if response_mode not in {"redirect", "postmessage"}:
            raise HDHiveOpenAPIError(
                "400", "当前插件仅支持 redirect 或 postmessage 授权回调"
            )
        state = str(state or "").strip() or secrets.token_urlsafe(32)
        params = {
            "client_id": self.client_id,
            "redirect_uri": redirect_uri,
            "scope": scope or self.DEFAULT_SCOPE,
            "state": state,
            "response_mode": response_mode,
        }
        return f"{self.base_url}/openapi/authorize?{urllib.parse.urlencode(params)}"

    def exchange_code(self, code: str, redirect_uri: str) -> Dict[str, Any]:
        """
        授权码换取用户 Token
        :param code: 一次性授权码
        :param redirect_uri: 必须与发起授权时的回调地址完全一致
        :return: Token 数据（access_token/refresh_token/expires_in 等）
        """
        data = self._request_public(
            "POST",
            "/api/public/openapi/oauth/token",
            {
                "grant_type": "authorization_code",
                "code": (code or "").strip(),
                "redirect_uri": (redirect_uri or "").strip(),
            },
        )
        if not str(data.get("access_token") or "").strip():
            raise HDHiveOpenAPIError(
                "INVALID_TOKEN_RESPONSE", "HDHive OAuth Token 响应缺少 Access Token"
            )
        self._apply_token_set(data)
        return data

    def refresh_access_token(self) -> Dict[str, Any]:
        """
        使用 Refresh Token 刷新用户 Token
        刷新失败返回 OPENAPI_REAUTH_REQUIRED 时需要重新发起授权
        """
        if not self.refresh_token:
            raise HDHiveOpenAPIError("OPENAPI_REAUTH_REQUIRED", "缺少 Refresh Token，请重新授权")
        data = self._request_public(
            "POST",
            "/api/public/openapi/oauth/refresh",
            {"refresh_token": self.refresh_token},
        )
        if not str(data.get("access_token") or "").strip():
            raise HDHiveOpenAPIError(
                "INVALID_TOKEN_RESPONSE", "HDHive OAuth Refresh 响应缺少 Access Token"
            )
        self._apply_token_set(data)
        logger.info("HDHive OpenAPI: 用户 Access Token 刷新成功")
        return data

    def _apply_token_set(self, data: Dict[str, Any]):
        """保存 Token 并触发持久化回调"""
        if not isinstance(data, dict):
            return
        self.access_token = str(data.get("access_token") or self.access_token).strip()
        self.refresh_token = str(data.get("refresh_token") or self.refresh_token).strip()
        expires_in = int(data.get("expires_in", 0) or 0)
        self.token_expires_at = time.time() + expires_in if expires_in else 0
        if self.on_token_update:
            try:
                token_data = dict(data)
                token_data.update({
                    "access_token": self.access_token,
                    "refresh_token": self.refresh_token,
                    "token_expires_at": self.token_expires_at,
                })
                self.on_token_update(token_data)
            except Exception as e:
                logger.error(f"HDHive OpenAPI: Token 持久化回调失败: {e}")

    def ping(self) -> Dict[str, Any]:
        """验证应用 Secret（仅需 X-API-Key）"""
        return self._request("GET", "/api/open/ping", with_user_token=False)

    def get_me(self) -> Dict[str, Any]:
        """获取当前授权用户基础信息"""
        return self._request("GET", "/api/open/me")

    def get_account_info(self) -> Dict[str, Any]:
        """读取当前授权用户的账户信息和积分余额。"""
        payload = self.get_me()
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict) or "points" not in data:
            raise HDHiveOpenAPIError(
                "OPENAPI_SCHEMA_CHANGED",
                "HDHive OpenAPI 账户接口缺少积分字段",
            )
        try:
            points = max(0, int(data.get("points") or 0))
        except (TypeError, ValueError) as error:
            raise HDHiveOpenAPIError(
                "OPENAPI_SCHEMA_CHANGED",
                "HDHive OpenAPI 账户积分格式异常",
            ) from error
        raw_signin_days = data.get("signin_days_total")
        try:
            signin_days = (
                max(0, int(raw_signin_days))
                if raw_signin_days is not None else None
            )
        except (TypeError, ValueError):
            signin_days = None
        return {
            "name": str(
                data.get("nickname") or data.get("username") or "HDHive 用户"
            ),
            "username": str(data.get("username") or ""),
            "email": str(data.get("email") or ""),
            "avatar": str(data.get("avatar_url") or data.get("avatar") or ""),
            "points": points,
            "signin_days": signin_days,
        }

    @staticmethod
    def _is_scope_error(error: HDHiveOpenAPIError) -> bool:
        text = " ".join(
            str(value or "")
            for value in (error.code, error.message, error.description)
        ).casefold()
        if error.status != 403:
            return False
        if any(marker in text for marker in ("blocked", "banned", "封禁", "停用")):
            return False
        return True

    def checkin(self, is_gambler: bool = False) -> Dict[str, Any]:
        """通过 HDHive OpenAPI 完成每日签到，并返回统一签到结果。"""
        before = self.get_account_info()
        try:
            payload = self._request(
                "POST",
                "/api/open/checkin",
                body={"is_gambler": bool(is_gambler)},
            )
        except HDHiveOpenAPIError as error:
            if self._is_scope_error(error):
                raise HDHiveOpenAPIError(
                    "OPENAPI_SCOPE_REQUIRED",
                    "HDHive OpenAPI 应用或 Token 缺少 write 权限，"
                    "请确认应用权限后重新授权",
                    error.description,
                    error.status,
                ) from error
            raise

        data = payload.get("data") if isinstance(payload, dict) else None
        if (
                not isinstance(data, dict)
                or "checked_in" not in data
                or "points" not in data
        ):
            raise HDHiveOpenAPIError(
                "OPENAPI_SCHEMA_CHANGED",
                "HDHive OpenAPI 签到接口返回格式异常",
            )
        status_code = payload.get("code") if isinstance(payload, dict) else 0
        try:
            status_code = int(status_code or 200)
        except (TypeError, ValueError):
            status_code = 200
        message = str(
            data.get("message")
            or payload.get("message")
            or "签到成功"
        )
        checked_in = bool(data.get("checked_in"))
        response_success = payload.get("success") is not False
        already_checked_in = bool(response_success and not checked_in)
        success = bool(status_code < 400 and response_success)
        after = (
            before
            if already_checked_in or not success
            else self.get_account_info()
        )
        points_before = int(before.get("points") or 0)
        points_after = int(after.get("points") or 0)
        signin_points = 0
        if checked_in:
            try:
                signin_points = int(data.get("points") or 0)
            except (TypeError, ValueError):
                signin_points = 0
        return {
            "success": success,
            "checked_in": checked_in and not already_checked_in,
            "already_checked_in": already_checked_in,
            "status": (
                "今日已签到"
                if already_checked_in
                else "签到成功" if success else "签到失败"
            ),
            "message": message,
            "is_gambler": bool(is_gambler),
            "signin_points": signin_points,
            "points_change": points_after - points_before,
            "points_before": points_before,
            "points_after": points_after,
            "signin_days": after.get("signin_days"),
            "status_code": status_code,
            "error_code": "" if success else str(payload.get("code") or ""),
            "captcha_verified": False,
            "raw": payload,
        }

    def query_resources(
            self, media_type: str, tmdb_id: Any, force_refresh: bool = False
    ) -> Dict[str, Any]:
        """
        根据 TMDB ID 查询资源列表
        :param media_type: movie 或 tv
        """
        media_type = str(media_type or "").strip().lower()
        if media_type not in ("movie", "tv"):
            raise HDHiveOpenAPIError("400", f"不支持的媒体类型: {media_type}")
        normalized_tmdb_id = str(tmdb_id).strip()
        cache_key = normalize_platform_cache_key(
            (media_type, normalized_tmdb_id)
        )
        path = "/api/open/resources/{}/{}".format(
            urllib.parse.quote(str(media_type), safe=""),
            urllib.parse.quote(normalized_tmdb_id, safe=""),
        )
        return cached_resource_call(
            self._resource_cache,
            cache_key,
            lambda: self._request("GET", path),
            locks=self._resource_locks,
            access_lock=self._lock,
            force_refresh=force_refresh,
        )

    def get_share_details(
            self, slug: str, force_refresh: bool = False
    ) -> Dict[str, Any]:
        """查询单个分享对当前用户的实际积分和解锁状态，不返回原始链接。"""
        slug = str(slug or "").strip()
        if not slug:
            raise HDHiveOpenAPIError("400", "资源 slug 不能为空")
        path = f"/api/open/shares/{urllib.parse.quote(slug, safe='')}"
        return cached_resource_call(
            self._detail_cache,
            slug,
            lambda: self._request("GET", path),
            locks=self._detail_locks,
            access_lock=self._lock,
            force_refresh=force_refresh,
        )

    def unlock_resource(
            self, slug: str, max_unlock_points: Optional[int] = None
    ) -> Dict[str, Any]:
        """解锁单个资源并获取分享链接"""
        slug = str(slug or "").strip()
        if not slug:
            raise HDHiveOpenAPIError("400", "资源 slug 不能为空")
        confirmed_points: Optional[int] = None
        if max_unlock_points is not None:
            detail_response = self.get_share_details(slug, force_refresh=True)
            detail = detail_response.get("data") or {}
            already_unlocked = bool(
                detail.get("is_unlocked") or detail.get("is_free_for_user")
            )
            try:
                current_points = max(0, int(
                    detail.get("actual_unlock_points")
                    if detail.get("actual_unlock_points") is not None
                    else detail.get("unlock_points") or 0
                ))
            except (TypeError, ValueError):
                current_points = 0
            confirmed_points = 0 if already_unlocked else current_points
            if not already_unlocked and current_points > int(max_unlock_points):
                raise HDHiveOpenAPIError(
                    "UNLOCK_BUDGET_EXCEEDED",
                    "HDHive 当前解锁价格超过预算",
                    f"需要 {current_points}，预算 {int(max_unlock_points)}",
                )
        data = self._request(
            "POST", "/api/open/resources/unlock", body={"slug": slug}
        )
        result = data.get("data") if isinstance(data, dict) else None
        result = result if isinstance(result, dict) else {}
        actual_points = 0
        point_sources = [result]
        if isinstance(result.get("unlock"), dict):
            point_sources.append(result["unlock"])
        point_sources.append(data)
        for source in point_sources:
            for key in (
                    "cost_points", "actual_unlock_points", "spent_points",
                    "points_cost", "actual_points",
            ):
                if source.get(key) is None:
                    continue
                try:
                    actual_points = max(0, int(source.get(key) or 0))
                except (TypeError, ValueError):
                    actual_points = 0
                break
            if actual_points > 0:
                break
        if actual_points <= 0 and confirmed_points is not None:
            actual_points = confirmed_points
        data["actual_points"] = actual_points
        with self._lock:
            self._detail_cache.delete(slug)
            self._resource_cache.clear()
        return data

    def _request_public(self, method: str, path: str, body: Optional[Dict] = None) -> Dict[str, Any]:
        """调用 OAuth 公共接口（仅应用 Secret，不带用户 Token），返回 data 部分"""
        if not self.app_secret:
            raise HDHiveOpenAPIError("MISSING_API_KEY", "未配置应用 Secret")
        headers = {
            "X-API-Key": self.app_secret,
            "Accept": "application/json",
        }
        data = self._do_request(method, path, headers, body)
        if isinstance(data, dict) and "data" in data:
            return data.get("data") or {}
        return data

    def _request(
            self,
            method: str,
            path: str,
            body: Optional[Dict] = None,
            with_user_token: bool = True,
            _retry: bool = True,
    ) -> Dict[str, Any]:
        """
        调用业务接口，返回完整响应 JSON（含 success/data/message）
        Access Token 过期时自动刷新并重试一次
        """
        if not self.app_secret:
            raise HDHiveOpenAPIError("MISSING_API_KEY", "未配置应用 Secret")

        if with_user_token:
            if not self.access_token:
                raise HDHiveOpenAPIError("OPENAPI_USER_REQUIRED", "未完成用户授权，缺少 Access Token")
            # 已知过期时间则提前刷新，避免无谓的 401 往返
            if self.refresh_token and self.token_expires_at and time.time() > self.token_expires_at - 60:
                try:
                    self.refresh_access_token()
                except HDHiveOpenAPIError as e:
                    logger.warning(f"HDHive OpenAPI: 预刷新 Token 失败（{e.code}），继续尝试当前 Token")

        headers = {
            "X-API-Key": self.app_secret,
            "Accept": "application/json",
        }
        if with_user_token:
            headers["Authorization"] = f"Bearer {self.access_token}"

        try:
            return self._do_request(method, path, headers, body)
        except HDHiveOpenAPIError as exc:
            if _retry and with_user_token and exc.code == "OPENAPI_REFRESH_REQUIRED" and self.refresh_token:
                self.refresh_access_token()
                return self._request(method, path, body, with_user_token, _retry=False)
            raise

    def _do_request(self, method: str, path: str, headers: Dict, body: Optional[Dict]) -> Dict[str, Any]:
        url = self.base_url + path
        try:
            with self._lock:
                resp = gated_request(
                    self._request_gate,
                    self._session.request,
                    method=method,
                    url=url,
                    headers=headers,
                    json=body if body is not None else None,
                    proxies=self._proxies,
                    timeout=self.timeout,
                )
        except requests.exceptions.RequestException as error:
            raise HDHiveOpenAPIError(
                "REQUEST_FAILED", "HDHive OpenAPI 请求失败", str(error)
            ) from error
        try:
            data = resp.json()
        except (json.JSONDecodeError, ValueError):
            raise HDHiveOpenAPIError(str(resp.status_code), f"响应解析失败 (HTTP {resp.status_code})",
                                     resp.text[:200], resp.status_code)
        if resp.status_code >= 400:
            raise HDHiveOpenAPIError(
                str(data.get("code", resp.status_code)),
                str(data.get("message", "")),
                str(data.get("description", "")),
                resp.status_code,
            )
        return data
