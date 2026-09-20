"""阿里云盘 HTTP 客户端，使用 refresh_token 管理完整会话。"""

from __future__ import annotations

import base64
import json
from threading import RLock
from typing import Any, Callable, Dict, Optional

import requests
from app.utils.string import StringUtils

from ..common import DriveRateLimiter


class AliPanClient:
    API = "https://api.aliyundrive.com"
    AUTH_API = "https://auth.aliyundrive.com/v2/account/token"
    PASSPORT_QR_API = "https://passport.aliyundrive.com/newlogin/qrcode"

    def __init__(
            self, access_token: str = "", refresh_token: str = "",
            timeout: int = 60,
            on_token_refresh: Optional[Callable[[str, str], None]] = None,
    ):
        self.access_token = str(access_token or "").strip()
        self.refresh_token = str(refresh_token or "").strip()
        self.timeout = max(10, min(int(timeout or 60), 300))
        self.on_token_refresh = on_token_refresh
        self.drive_id = ""
        self.user_id = ""
        self._lock = RLock()
        self.rate_limiter = DriveRateLimiter.shared(
            "alipan", self.refresh_token or self.access_token, min_interval=0.5
        )
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36"
            ),
            "Referer": "https://www.aliyundrive.com/",
            "Content-Type": "application/json",
        })

    def close(self) -> None:
        self.session.close()

    @staticmethod
    def _passport_data(response: requests.Response) -> Dict[str, Any]:
        response.raise_for_status()
        payload = response.json()
        content = payload.get("content") or {}
        data = content.get("data") or {}
        if not isinstance(data, dict):
            raise RuntimeError("阿里云盘扫码响应格式无效")
        return data

    def create_qrcode_login(self, client_type: str = "") -> Dict[str, Any]:
        response = self.raw_request(
            "GET",
            f"{self.PASSPORT_QR_API}/generate.do",
            params={
                "appName": "aliyun_drive",
                "fromSite": "52",
                "appEntrance": "web",
                "_csrf_token": "",
                "umidToken": "",
                "isMobile": "false",
                "lang": "zh_CN",
                "returnUrl": "",
                "hsiz": "",
                "bizParams": "",
                "_bx-v": "2.0.31",
            },
        )
        data = self._passport_data(response)
        qr_url = str(data.get("codeContent") or "").strip()
        qrcode_time = str(data.get("t") or "").strip()
        ck = str(data.get("ck") or "").strip()
        if not qr_url or not qrcode_time or not ck:
            raise RuntimeError("阿里云盘未返回完整的二维码登录参数")
        return {
            "qr_url": qr_url,
            "t": qrcode_time,
            "ck": ck,
            "expires_in": 300,
            "interval": 2,
        }

    @staticmethod
    def _decode_login_result(value: str) -> Dict[str, Any]:
        encoded = str(value or "").strip()
        if not encoded:
            raise RuntimeError("阿里云盘扫码成功但未返回登录凭证")
        raw = base64.b64decode(encoded + "=" * (-len(encoded) % 4))
        for encoding in ("utf-8", "gbk"):
            try:
                result = json.loads(raw.decode(encoding))
                if isinstance(result, dict):
                    return result
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
        raise RuntimeError("阿里云盘登录凭证解析失败")

    def check_qrcode_login(self, **kwargs: Any) -> Dict[str, Any]:
        qrcode_time = str(kwargs.get("t") or "").strip()
        ck = str(kwargs.get("ck") or "").strip()
        if not qrcode_time or not ck:
            raise ValueError("缺少阿里云盘扫码会话参数")
        response = self.raw_request(
            "POST",
            f"{self.PASSPORT_QR_API}/query.do",
            params={
                "appName": "aliyun_drive",
                "fromSite": "52",
                "_bx-v": "2.0.31",
            },
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": "https://passport.aliyundrive.com",
                "Referer": (
                    "https://passport.aliyundrive.com/mini_login.htm?"
                    "&appName=aliyun_drive"
                ),
            },
            data={
                "t": qrcode_time,
                "ck": ck,
                "appName": "aliyun_drive",
                "appEntrance": "web",
                "isMobile": "false",
                "lang": "zh_CN",
                "fromSite": "52",
                "navlanguage": "zh-CN",
                "navUserAgent": self.session.headers.get("User-Agent", ""),
                "navPlatform": "Win32",
            },
        )
        data = self._passport_data(response)
        status = str(data.get("qrCodeStatus") or "NEW").upper()
        if status == "CONFIRMED":
            login = self._decode_login_result(data.get("bizExt") or {}).get(
                "pds_login_result"
            ) or {}
            refresh_token = str(login.get("refreshToken") or "").strip()
            access_token = str(login.get("accessToken") or "").strip()
            if not refresh_token and not access_token:
                raise RuntimeError("阿里云盘扫码成功但未获得 Token")
            return {
                "status": "success",
                "message": "登录成功",
                "access_token": access_token,
                "refresh_token": refresh_token,
            }
        if status in {"SCANED", "SCANNED"}:
            return {"status": "scanned", "message": "已扫码，请在手机上确认"}
        if status == "EXPIRED":
            return {"status": "expired", "message": "二维码已失效"}
        if status in {"CANCELED", "CANCELLED"}:
            return {"status": "cancelled", "message": "已取消登录"}
        return {"status": "waiting", "message": "等待扫码"}

    def raw_request(
            self, method: str, url: str, *, authenticated: bool = False,
            retry: bool = True, raise_for_status: bool = True, **kwargs,
    ) -> requests.Response:
        kwargs.setdefault("timeout", self.timeout)
        headers = dict(kwargs.pop("headers", {}) or {})
        if authenticated:
            self.ensure_session()
            headers["Authorization"] = (
                self.access_token
                if self.access_token.lower().startswith("bearer ")
                else f"Bearer {self.access_token}"
            )
        response = self.rate_limiter.call(
            self.session.request,
            method,
            url,
            headers=headers,
            retry_exceptions=(requests.Timeout, requests.ConnectionError),
            **kwargs,
        )
        if authenticated and retry and response.status_code in (401, 403):
            self.refresh()
            return self.raw_request(
                method, url, authenticated=True, retry=False,
                headers=headers, **kwargs,
            )
        if raise_for_status:
            response.raise_for_status()
        return response

    def request(
            self, path: str, payload: Optional[Dict[str, Any]] = None,
            *, headers: Optional[Dict[str, str]] = None,
            authenticated: bool = True,
            retry: bool = True,
    ) -> Dict[str, Any]:
        response = self.raw_request(
            "POST", self.API + path, authenticated=authenticated,
            headers=headers, json=payload or {}, raise_for_status=False,
        )
        data = response.json()
        if (
                authenticated and retry
                and data.get("code") in {"AccessTokenInvalid", "InvalidParameter.RefreshToken"}
        ):
            self.refresh()
            return self.request(
                path, payload, headers=headers, authenticated=True, retry=False
            )
        if data.get("code"):
            raise RuntimeError(
                f"阿里云盘 API 请求失败：{data.get('code')} - "
                f"{data.get('message') or ''}"
            )
        response.raise_for_status()
        return data

    def refresh(self) -> None:
        if not self.refresh_token:
            raise RuntimeError("未配置阿里云盘 Refresh Token")
        with self._lock:
            response = self.raw_request(
                "POST", self.AUTH_API,
                json={
                    "refresh_token": self.refresh_token,
                    "grant_type": "refresh_token",
                },
            )
            data = response.json()
            access_token = str(data.get("access_token") or "")
            if not access_token:
                raise RuntimeError(data.get("message") or "阿里云盘 Token 刷新失败")
            self.access_token = access_token
            new_refresh_token = str(data.get("refresh_token") or self.refresh_token)
            self.drive_id = str(
                data.get("default_drive_id") or data.get("drive_id") or self.drive_id
            )
            self.user_id = str(data.get("user_id") or self.user_id)
            self.refresh_token = new_refresh_token
            if self.on_token_refresh:
                self.on_token_refresh(self.access_token, self.refresh_token)

    def ensure_session(self) -> None:
        if not self.access_token:
            self.refresh()
        if not self.drive_id:
            headers = {
                "Authorization": (
                    self.access_token
                    if self.access_token.lower().startswith("bearer ")
                    else f"Bearer {self.access_token}"
                )
            }
            response = self.rate_limiter.call(
                self.session.post,
                f"{self.API}/v2/user/get",
                headers=headers,
                json={},
                timeout=self.timeout,
                retry_exceptions=(requests.Timeout, requests.ConnectionError),
            )
            response.raise_for_status()
            data = response.json()
            if data.get("code"):
                raise RuntimeError(
                    f"阿里云盘用户信息读取失败：{data.get('code')} - "
                    f"{data.get('message') or ''}"
                )
            self.user_id = str(data.get("user_id") or self.user_id)
            self.drive_id = str(
                data.get("default_drive_id") or data.get("drive_id")
                or self.drive_id
            )

    def check_login(self) -> bool:
        try:
            data = self.request("/v2/user/get")
            self.user_id = str(data.get("user_id") or self.user_id)
            self.drive_id = str(data.get("default_drive_id") or self.drive_id)
            return bool(self.user_id and self.drive_id)
        except Exception:
            return False

    def get_account_info(self) -> Dict[str, Any]:
        try:
            if not self.check_login():
                return {
                    "connected": False,
                    "error": "请扫码登录阿里云盘",
                }
            data = self.request("/v2/user/get")
            capacity = self.request("/adrive/v1/user/driveCapacityDetails")
            total = int(capacity.get("drive_total_size") or 0)
            used = int(capacity.get("drive_used_size") or 0)
            return {
                "connected": True,
                "user": {
                    "name": str(data.get("nick_name") or data.get("user_name") or "阿里云盘用户"),
                    "avatar": str(data.get("avatar") or ""),
                    "membership_supported": False,
                    "is_vip": False,
                    "is_forever_vip": False,
                    "vip_expire_date": "",
                },
                "storage": {
                    "total": StringUtils.str_filesize(total),
                    "used": StringUtils.str_filesize(used),
                    "remaining": StringUtils.str_filesize(max(0, total - used)),
                },
            }
        except Exception as error:
            return {"connected": False, "error": str(error)}
