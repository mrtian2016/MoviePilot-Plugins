"""p123client 的 Token、扫码登录与调用计数封装。"""

from __future__ import annotations

from threading import RLock
from typing import Any, Dict, Optional

import requests
from app.log import logger

from ..common import DriveRateLimiter, format_size, safe_int

try:
    from p123client import P123Client, check_response

    P123_AVAILABLE = True
except ImportError:
    P123Client = None
    check_response = None
    P123_AVAILABLE = False
    logger.warning("p123client 未安装，123网盘功能不可用，请安装: pip install p123client")


def is_success(response: Any) -> bool:
    if not P123_AVAILABLE or not isinstance(response, dict):
        return False
    try:
        check_response(response)
        return True
    except Exception:
        return False


class P123ClientManager:
    """使用扫码取得的 Token 按需创建 123 客户端。"""

    _login_rate_limiter = DriveRateLimiter(min_interval=0.8)

    def __init__(
            self,
            token: str = "",
            timeout: int = 30,
    ):
        self.token = str(token or "").strip().removeprefix("Bearer ").strip()
        self.timeout = max(5, min(int(timeout or 30), 300))
        self._client: Optional[Any] = None
        self._lock = RLock()
        self.rate_limiter = DriveRateLimiter.shared(
            "p123", self.token, min_interval=0.5
        )

    def _create_client(self):
        return P123Client.init(
            token=self.token,
            timeout=self.timeout,
        )

    def _get_client(self):
        if not P123_AVAILABLE:
            raise RuntimeError("p123client 未安装")
        if not self.token:
            raise RuntimeError("请扫码登录 123 网盘")
        with self._lock:
            if self._client is None:
                self._client = self._create_client()
            return self._client

    def __getattr__(self, name: str):
        attr = getattr(self._get_client(), name)
        if not callable(attr):
            return attr

        def wrapped(*args, **kwargs):
            kwargs.setdefault("timeout", self.timeout)
            method = getattr(self._get_client(), name)

            def invoke():
                try:
                    return method(*args, **kwargs)
                except TypeError as error:
                    if "timeout" not in str(error) or "timeout" not in kwargs:
                        raise
                    kwargs.pop("timeout", None)
                    return method(*args, **kwargs)

            return self.rate_limiter.call(
                invoke,
                retry_exceptions=(requests.Timeout, requests.ConnectionError),
            )

        return wrapped

    def create_qrcode_login(self, client_type: str = "") -> Dict[str, Any]:
        """使用 p123client 创建 123 App 扫码登录会话。"""
        if not P123_AVAILABLE:
            raise RuntimeError("p123client 未安装")
        response = self._login_rate_limiter.call(
            P123Client.login_qrcode_generate,
            timeout=self.timeout,
            retry_exceptions=(requests.Timeout, requests.ConnectionError),
        )
        check_response(response)
        data = response.get("data") or {}
        uni_id = str(data.get("uniID") or "").strip()
        login_url = str(data.get("url") or "").strip()
        if not uni_id or not login_url:
            raise RuntimeError("123网盘未返回完整的二维码登录参数")
        separator = "&" if "?" in login_url else "?"
        return {
            "uni_id": uni_id,
            "qr_url": (
                f"{login_url}{separator}env=production&uniID={uni_id}"
                "&source=123pan&type=login"
            ),
            "expires_in": 300,
            "interval": 1,
        }

    def check_qrcode_login(self, **kwargs: Any) -> Dict[str, Any]:
        """使用 p123client 轮询 123 App 扫码状态。"""
        if not P123_AVAILABLE:
            raise RuntimeError("p123client 未安装")
        uni_id = str(kwargs.get("uni_id") or kwargs.get("uniID") or "").strip()
        if not uni_id:
            raise ValueError("缺少 123 网盘扫码会话参数")
        response = self._login_rate_limiter.call(
            P123Client.login_qrcode_result,
            uni_id,
            timeout=self.timeout,
            retry_exceptions=(requests.Timeout, requests.ConnectionError),
        )
        if not isinstance(response, dict):
            raise RuntimeError("123网盘扫码状态响应格式无效")
        data = response.get("data") or {}
        code = safe_int(response.get("code"))
        login_status = safe_int(data.get("loginStatus"))
        if code == 200:
            token = str(data.get("token") or "").strip()
            if not token:
                raise RuntimeError("请使用 123 云盘 App 扫码登录")
            return {
                "status": "success",
                "message": "登录成功",
                "token": token,
            }
        if code != 0:
            check_response(response)
        status_map = {
            0: {"status": "waiting", "message": "等待扫码"},
            1: {"status": "scanned", "message": "已扫码，等待确认"},
            2: {"status": "cancelled", "message": "已取消登录"},
            3: {"status": "waiting", "message": "正在登录"},
            4: {"status": "expired", "message": "二维码已失效"},
        }
        return status_map.get(
            login_status,
            {"status": "waiting", "message": "等待扫码"},
        )

    def check_login(self) -> bool:
        if not self.token:
            return False
        try:
            return is_success(self.user_info())
        except Exception:
            return False

    def get_account_info(self) -> Dict[str, Any]:
        if not self.token:
            return {"connected": False, "error": "请扫码登录 123 网盘"}
        try:
            response = self.user_info()
            if not is_success(response):
                return {
                    "connected": False,
                    "error": response.get("message") or "Token 已失效",
                }
            data = response.get("data") or {}
            total = safe_int(data.get("SpacePermanent"))
            used = safe_int(data.get("SpaceUsed"))
            return {
                "connected": True,
                "user": {
                    "name": str(
                        data.get("Nickname") or data.get("UserName") or "123用户"
                    ),
                    "avatar": str(data.get("HeadImage") or data.get("Avatar") or ""),
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
        except Exception as error:
            return {"connected": False, "error": str(error)}

    def close(self) -> None:
        with self._lock:
            client = self._client
            self._client = None
        close = getattr(client, "close", None) if client is not None else None
        if callable(close):
            close()
