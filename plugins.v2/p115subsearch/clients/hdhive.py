"""
HDHive OpenAPI 客户端
基于官方 Python SDK 适配：应用 Secret (X-API-Key) + OAuth 用户 Access Token (Bearer) 双层认证
参考文档: https://hdhive.com/docs/open
"""
import json
import secrets
import time
import urllib.parse
from typing import Any, Callable, Dict, Optional

import requests

from app.log import logger


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

    DEFAULT_SCOPE = "query unlock"

    def __init__(
        self,
        app_secret: str,
        client_id: str = "",
        access_token: str = "",
        refresh_token: str = "",
        token_expires_at: float = 0,
        base_url: str = "https://hdhive.com",
        proxy: Any = None,
        timeout: int = 30,
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
        self.base_url = (base_url or "https://hdhive.com").rstrip("/")
        self.timeout = timeout
        self.on_token_update = on_token_update
        if isinstance(proxy, dict):
            self._proxies = proxy
        elif proxy:
            self._proxies = {"http": proxy, "https": proxy}
        else:
            self._proxies = None

    # ------------------ 状态 ------------------

    @property
    def is_ready(self) -> bool:
        """应用 Secret 和用户 Token 均已配置，可调用业务接口"""
        return bool(self.app_secret and self.access_token)

    # ------------------ OAuth ------------------

    def build_authorize_url(self, redirect_uri: str, scope: str = "", state: str = "") -> str:
        """生成用户授权页 URL"""
        params = {
            "client_id": self.client_id,
            "redirect_uri": redirect_uri,
            "scope": scope or self.DEFAULT_SCOPE,
            "state": state or secrets.token_hex(16),
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
        self._apply_token_set(data)
        logger.info("HDHive OpenAPI: 用户 Access Token 刷新成功")
        return data

    def _apply_token_set(self, data: Dict[str, Any]):
        """保存 Token 并触发持久化回调"""
        if not isinstance(data, dict):
            return
        self.access_token = str(data.get("access_token", "")).strip()
        self.refresh_token = str(data.get("refresh_token", "")).strip()
        expires_in = int(data.get("expires_in", 0) or 0)
        self.token_expires_at = time.time() + expires_in if expires_in else 0
        if self.on_token_update:
            try:
                self.on_token_update({
                    "access_token": self.access_token,
                    "refresh_token": self.refresh_token,
                    "token_expires_at": self.token_expires_at,
                })
            except Exception as e:
                logger.error(f"HDHive OpenAPI: Token 持久化回调失败: {e}")

    # ------------------ 业务接口 ------------------

    def ping(self) -> Dict[str, Any]:
        """验证应用 Secret（仅需 X-API-Key）"""
        return self._request("GET", "/api/open/ping", with_user_token=False)

    def get_me(self) -> Dict[str, Any]:
        """获取当前授权用户基础信息"""
        return self._request("GET", "/api/open/me")

    def query_resources(self, media_type: str, tmdb_id: Any) -> Dict[str, Any]:
        """
        根据 TMDB ID 查询资源列表
        :param media_type: movie 或 tv
        """
        path = "/api/open/resources/{}/{}".format(
            urllib.parse.quote(str(media_type), safe=""),
            urllib.parse.quote(str(tmdb_id), safe=""),
        )
        return self._request("GET", path)

    def unlock_resource(self, slug: str) -> Dict[str, Any]:
        """解锁单个资源并获取分享链接"""
        return self._request("POST", "/api/open/resources/unlock", body={"slug": slug})

    # ------------------ 请求 ------------------

    def _request_public(self, method: str, path: str, body: Optional[Dict] = None) -> Dict[str, Any]:
        """调用 OAuth 公共接口（仅应用 Secret，不带用户 Token），返回 data 部分"""
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
        resp = requests.request(
            method=method,
            url=url,
            headers=headers,
            json=body if body is not None else None,
            proxies=self._proxies,
            timeout=self.timeout,
        )
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
