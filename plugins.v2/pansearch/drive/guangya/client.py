"""光鸭网盘 HTTP 客户端，仅包含 PanSearch 使用的接口。"""

from __future__ import annotations

import secrets
import uuid
from typing import Any, Callable, Dict, Iterable, Optional

import requests
from app.log import logger

from ..common import DriveRateLimiter, format_size, safe_int


def _find_number(data: Any, keys: Iterable[str]) -> int:
    key_set = set(keys)
    if isinstance(data, dict):
        for key, value in data.items():
            if key in key_set and value not in (None, ""):
                return safe_int(value)
        for value in data.values():
            found = _find_number(value, key_set)
            if found:
                return found
    elif isinstance(data, list):
        for value in data:
            found = _find_number(value, key_set)
            if found:
                return found
    return 0


class GuangyaClient:
    ACCOUNT_BASE_URL = "https://account.guangyapan.com"
    API_BASE_URL = "https://api.guangyapan.com"
    DEFAULT_CLIENT_ID = "aMe-8VSlkrbQXpUR"

    def __init__(
            self,
            access_token: str = "",
            refresh_token: str = "",
            client_id: str = "",
            device_id: str = "",
            on_token_refresh: Optional[Callable[[str, str], None]] = None,
            timeout: int = 30,
    ):
        self.access_token = str(access_token or "").strip()
        self.refresh_token_value = str(refresh_token or "").strip()
        self.client_id = str(client_id or self.DEFAULT_CLIENT_ID).strip() or self.DEFAULT_CLIENT_ID
        self.device_id = str(device_id or uuid.uuid4().hex).replace("-", "").strip()
        self._on_token_refresh = on_token_refresh
        self._timeout = max(5, int(timeout or 30))
        self.rate_limiter = DriveRateLimiter.shared(
            "guangya", self.access_token or self.device_id, min_interval=0.5
        )
        self._session = requests.Session()

    def close(self) -> None:
        self._session.close()

    @staticmethod
    def is_success(response: Any) -> bool:
        if not isinstance(response, dict):
            return False
        code = response.get("code")
        message = str(response.get("msg") or response.get("message") or "").lower()
        return code in (0, 200, "0", "200", None) and message not in ("error", "fail", "failed") and not response.get(
            "error")

    @staticmethod
    def data(response: Any) -> Any:
        if not isinstance(response, dict):
            return {}
        return response.get("data") or response.get("result") or response

    def _common_headers(self) -> Dict[str, str]:
        return {
            "accept": "application/json, text/plain, */*",
            "content-type": "application/json",
            "origin": "https://www.guangyapan.com",
            "referer": "https://www.guangyapan.com/",
            "user-agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
            ),
            "did": self.device_id,
            "dt": "4",
            "traceparent": f"00-{secrets.token_hex(16)}-{secrets.token_hex(8)}-01",
        }

    def _account_headers(self) -> Dict[str, str]:
        headers = self._common_headers()
        headers.update({
            "accept": "*/*",
            "x-client-id": self.client_id,
            "x-client-version": "0.0.1",
            "x-device-id": self.device_id,
            "x-device-model": "chrome%2F147.0.0.0",
            "x-device-name": "PC-Chrome",
            "x-device-sign": f"wdi10.{self.device_id}{secrets.token_hex(16)}",
            "x-net-work-type": "NONE",
            "x-os-version": "Win32",
            "x-platform-version": "1",
            "x-protocol-version": "301",
            "x-provider-name": "NONE",
            "x-sdk-version": "9.0.2",
        })
        return headers

    def request(
            self,
            method: str,
            url: str,
            *,
            json_data: Optional[Dict[str, Any]] = None,
            authenticated: bool = True,
            account: bool = False,
            retry_auth: bool = True,
            accept_error: bool = False,
    ) -> Dict[str, Any]:
        headers = self._account_headers() if account else self._common_headers()
        if authenticated and self.access_token:
            headers["authorization"] = f"Bearer {self.access_token}"
            headers["accessToken"] = self.access_token
        try:
            response = self.rate_limiter.call(
                self._session.request,
                method.upper(), url,
                headers=headers, json=json_data, timeout=self._timeout,
                retry_exceptions=(requests.Timeout, requests.ConnectionError),
            )
            if response.status_code == 401 and authenticated and retry_auth and self.refresh_token_value:
                if self.refresh_access_token():
                    return self.request(
                        method,
                        url,
                        json_data=json_data,
                        authenticated=authenticated,
                        account=account,
                        retry_auth=False,
                        accept_error=accept_error,
                    )
            if response.status_code >= 400:
                try:
                    payload = response.json()
                except ValueError:
                    payload = {"error": response.text[:300]}
                if accept_error:
                    return payload
                return {
                    "code": response.status_code,
                    "msg": payload.get("message") or payload.get("error") or "请求失败",
                    "error": payload.get("error") or response.reason,
                }
            return response.json() if response.text else {"code": 0, "msg": "success"}
        except (requests.RequestException, ValueError) as error:
            logger.error(f"光鸭网盘请求失败：{url} - {error}")
            return {"code": -1, "msg": "error", "error": str(error)}

    def create_device_code(self) -> Dict[str, Any]:
        return self.request(
            "POST",
            f"{self.ACCOUNT_BASE_URL}/v1/auth/device/code",
            json_data={"scope": "user", "client_id": self.client_id},
            authenticated=False,
            account=True,
        )

    def poll_device_code(self, device_code: str) -> Dict[str, Any]:
        result = self.request(
            "POST",
            f"{self.ACCOUNT_BASE_URL}/v1/auth/token",
            json_data={
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "device_code": device_code,
                "client_id": self.client_id,
            },
            authenticated=False,
            account=True,
            accept_error=True,
        )
        if result.get("access_token"):
            self.access_token = str(result.get("access_token") or "")
            self.refresh_token_value = str(result.get("refresh_token") or "")
        return result

    def create_qrcode_login(self, client_type: str = "") -> Dict[str, Any]:
        """创建光鸭设备码登录会话。"""
        device_id = uuid.uuid4().hex
        temporary = type(self)(
            client_id=self.client_id,
            device_id=device_id,
            timeout=self._timeout,
        )
        try:
            result = temporary.create_device_code()
        finally:
            temporary.close()
        if not result or result.get("error") or not result.get("device_code"):
            raise RuntimeError(
                result.get("error_description")
                or result.get("error")
                or "光鸭设备码获取失败"
            )
        return {
            "device_code": str(result.get("device_code") or ""),
            "device_id": device_id,
            "client_id": self.client_id,
            "user_code": str(result.get("user_code") or ""),
            "verification_uri": str(result.get("verification_uri") or ""),
            "verification_uri_complete": str(
                result.get("verification_uri_complete") or ""
            ),
            "expires_in": safe_int(result.get("expires_in")) or 300,
            "interval": safe_int(result.get("interval")) or 5,
        }

    def check_qrcode_login(self, **kwargs: Any) -> Dict[str, Any]:
        """轮询光鸭设备码登录状态并返回登录凭证。"""
        device_code = str(kwargs.get("device_code") or "").strip()
        device_id = str(kwargs.get("device_id") or "").strip()
        client_id = str(kwargs.get("client_id") or self.client_id).strip()
        if not device_code or not device_id:
            raise ValueError("缺少光鸭扫码会话参数")
        temporary = type(self)(
            client_id=client_id,
            device_id=device_id,
            timeout=self._timeout,
        )
        try:
            result = temporary.poll_device_code(device_code)
        finally:
            temporary.close()
        if result.get("error") == "authorization_pending":
            return {"status": "waiting", "message": "等待扫码"}
        if result.get("error") in {
            "expired_token",
            "access_denied",
            "invalid_grant",
        }:
            return {
                "status": "expired",
                "message": result.get("error_description") or "二维码已失效",
            }
        if not result.get("access_token"):
            return {"status": "waiting", "message": "等待扫码"}
        return {
            "status": "success",
            "message": "登录成功",
            "access_token": str(result.get("access_token") or ""),
            "refresh_token": str(result.get("refresh_token") or ""),
            "client_id": client_id,
            "device_id": device_id,
        }

    def refresh_access_token(self) -> bool:
        if not self.refresh_token_value:
            return False
        result = self.request(
            "POST",
            f"{self.ACCOUNT_BASE_URL}/v1/auth/token",
            json_data={
                "grant_type": "refresh_token",
                "refresh_token": self.refresh_token_value,
                "client_id": self.client_id,
            },
            authenticated=False,
            account=True,
            retry_auth=False,
        )
        token = str(result.get("access_token") or "")
        if not token:
            return False
        self.access_token = token
        self.refresh_token_value = str(result.get("refresh_token") or self.refresh_token_value)
        if self._on_token_refresh:
            self._on_token_refresh(self.access_token, self.refresh_token_value)
        return True

    def get_user_info(self) -> Dict[str, Any]:
        if not self.access_token:
            return {"code": 401, "msg": "未配置 Access Token", "error": "missing_token"}
        return self.request(
            "GET", f"{self.ACCOUNT_BASE_URL}/v1/user/me", account=True
        )

    def get_assets(self) -> Dict[str, Any]:
        return self.request(
            "POST", f"{self.API_BASE_URL}/nd.bizassets.s/v1/get_assets", json_data={}
        )

    def check_login(self) -> bool:
        return self.is_success(self.get_user_info())

    def get_account_info(self) -> Dict[str, Any]:
        if not self.access_token:
            return {"connected": False, "error": "请填写 Token 或扫码登录"}
        user_response = self.get_user_info()
        if not self.is_success(user_response):
            return {
                "connected": False,
                "error": user_response.get("msg") or "Token 已失效",
            }
        user = self.data(user_response)
        assets = self.data(self.get_assets())
        total = _find_number(assets, (
            "totalSpaceSize", "totalSpace", "total", "totalSize",
            "capacity", "quotaSize",
        ))
        used = _find_number(assets, (
            "usedSpaceSize", "usedSpace", "used", "usedSize",
            "spaceUsed", "useSize",
        ))
        return {
            "connected": True,
            "user": {
                "name": str(
                    user.get("nickname") or user.get("nickName")
                    or user.get("name") or user.get("phone") or "光鸭用户"
                ),
                "avatar": str(
                    user.get("picture") or user.get("avatar")
                    or user.get("avatarUrl") or ""
                ),
                "membership_supported": False,
                "is_vip": False,
                "is_forever_vip": False,
                "vip_expire_date": "",
            },
            "storage": {
                "total": format_size(total),
                "used": format_size(used),
                "remaining": format_size(max(0, total - used)),
            },
        }
