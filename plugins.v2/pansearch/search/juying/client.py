"""聚影普通账号网页资源搜索客户端。"""

import threading
import time
from typing import Any, Callable, Dict, Optional

from app.log import logger

from ..http_client import (
    RequestGate,
    gated_idempotent_request,
    gated_request,
    normalize_proxies,
    request_error_summary,
    requests,
)


class JuyingError(RuntimeError):
    """聚影登录、搜索或资源解析失败。"""

    def __init__(self, message: str, code: str = "juying_error"):
        super().__init__(message)
        self.code = code


class JuyingClient:
    """维护聚影 CSRF、登录令牌和受控 JSON 请求。"""

    BASE_URL = "https://www.jying.top"
    _SESSION_DATA_KEY = "juying_auth_session"
    _LOGIN_LOCK = threading.RLock()

    _HEADERS = {
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "X-Requested-With": "XMLHttpRequest",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    }

    def __init__(
            self,
            username: str,
            password: str,
            base_url: str = BASE_URL,
            proxy: Any = None,
            request_timeout: int = 30,
            request_interval: float = 1.0,
            get_data_func: Optional[Callable] = None,
            save_data_func: Optional[Callable] = None,
            cache_namespace: str = "",
    ):
        self.base_url = str(base_url or self.BASE_URL).rstrip("/")
        self.username = str(username or "").strip()
        self.password = str(password or "")
        self._proxies = normalize_proxies(proxy)
        self._request_timeout = max(5, min(int(request_timeout or 30), 60))
        self._session = self._create_session()
        self._token = ""
        self._get_data_func = get_data_func
        self._save_data_func = save_data_func
        self.cache_namespace = str(cache_namespace or "").strip()
        self._lock = threading.RLock()
        self._circuit_open_until = 0.0
        self._request_gate = RequestGate.shared(
            "聚影",
            f"{self.base_url}|{self.username.casefold()}|{self._proxies}",
            request_interval=request_interval,
            minimum_interval=1,
        )
        self._restore_token()

    @property
    def _timeout(self) -> tuple[int, int]:
        return min(15, self._request_timeout), self._request_timeout

    @classmethod
    def _create_session(cls):
        session = requests.Session(impersonate="chrome")
        session.headers.update(cls._HEADERS)
        return session

    def _session_request(self, *args, **kwargs):
        return self._session.request(*args, **kwargs)

    def _reset_transport(self, error: BaseException, attempt: int) -> None:
        cookies = self._session.cookies.get_dict()
        try:
            self._session.close()
        except Exception:
            pass
        self._session = self._create_session()
        for name, value in cookies.items():
            self._session.cookies.set(name, value)
        logger.debug(
            f"聚影连接异常后重建 HTTP 会话："
            f"{type(error).__name__}，重试={attempt}"
        )

    def _restore_token(self) -> None:
        if not self._get_data_func:
            return
        try:
            data = self._get_data_func(self._SESSION_DATA_KEY) or {}
            if (
                    not isinstance(data, dict)
                    or str(data.get("username") or "").strip() != self.username
            ):
                return
            token = str(data.get("token") or "").strip()
            if token:
                self._token = token
                logger.debug("聚影已恢复持久化登录状态")
        except Exception as error:
            logger.debug(f"聚影恢复持久化登录状态失败：{error}")

    def _save_token(self) -> None:
        if not self._save_data_func:
            return
        try:
            self._save_data_func(
                self._SESSION_DATA_KEY,
                {
                    "username": self.username,
                    "token": self._token,
                    "updated_at": int(time.time()),
                } if self._token else {},
            )
        except Exception as error:
            logger.debug(f"聚影持久化登录状态失败：{error}")

    def _set_token(self, token: str = "") -> None:
        self._token = str(token or "").strip()
        self._save_token()

    def _csrf_headers(self) -> Dict[str, str]:
        headers = {
            "Origin": self.base_url,
            "Referer": f"{self.base_url}/",
        }
        csrf_token = str(self._session.cookies.get("csrftoken") or "")
        if csrf_token:
            headers["X-CSRFToken"] = csrf_token
        return headers

    @staticmethod
    def _json_response(response: requests.Response) -> bool:
        return "application/json" in str(
            response.headers.get("content-type") or ""
        ).casefold()

    def _ensure_available(self) -> None:
        if not self.username or not self.password:
            raise JuyingError("聚影账号或密码未配置", "juying_not_configured")
        if self._circuit_open_until > time.monotonic():
            raise JuyingError("聚影请求暂时受限，请稍后重试", "juying_rate_limited")

    @property
    def is_configured(self) -> bool:
        return bool(self.username and self.password)

    def _login(self, force: bool = False) -> None:
        self._ensure_available()
        if self._token and not force:
            return
        with self._LOGIN_LOCK:
            if not force:
                self._restore_token()
                if self._token:
                    return
            try:
                csrf_response = gated_idempotent_request(
                    self._request_gate,
                    self._session_request,
                    "GET",
                    f"{self.base_url}/api/csrf/",
                    on_retry=self._reset_transport,
                    proxies=self._proxies,
                    timeout=self._timeout,
                )
            except requests.exceptions.RequestException as error:
                raise JuyingError(
                    f"聚影登录初始化失败：{request_error_summary(error)}",
                    "juying_login_failed",
                ) from error
            if csrf_response.status_code != 200:
                raise JuyingError(
                    f"聚影 CSRF 初始化失败（HTTP {csrf_response.status_code}）",
                    "juying_login_failed",
                )
            try:
                response = gated_request(
                    self._request_gate,
                    self._session_request,
                    "POST",
                    f"{self.base_url}/api/app/login/",
                    json={"username": self.username, "password": self.password},
                    headers=self._csrf_headers(),
                    proxies=self._proxies,
                    timeout=self._timeout,
                )
            except requests.exceptions.RequestException as error:
                raise JuyingError(
                    f"聚影登录失败：{request_error_summary(error)}",
                    "juying_login_failed",
                ) from error
            if response.status_code != 200 or not self._json_response(response):
                raise JuyingError(
                    f"聚影登录失败（HTTP {response.status_code}）",
                    "juying_login_failed",
                )
            payload = response.json()
            token = (
                str(payload.get("token") or "").strip()
                if isinstance(payload, dict) else ""
            )
            if not token:
                message = payload.get("message") if isinstance(payload, dict) else ""
                raise JuyingError(
                    str(message or "聚影登录未返回会话令牌"),
                    "juying_login_failed",
                )
            self._set_token(token)

    def _request(
            self,
            method: str,
            path: str,
            retry_auth: bool = True,
            protected_access: bool = False,
            **kwargs: Any,
    ) -> Dict[str, Any]:
        self._login()
        headers = dict(kwargs.pop("headers", {}) or {})
        headers.update(self._csrf_headers())
        headers["X-App-User-Token"] = self._token
        try:
            def request():
                return gated_idempotent_request(
                    self._request_gate,
                    self._session_request,
                    method,
                    f"{self.base_url}{path}",
                    on_retry=self._reset_transport,
                    headers=headers,
                    proxies=self._proxies,
                    timeout=self._timeout,
                    **kwargs,
                )

            response = request()
        except requests.exceptions.RequestException as error:
            raise JuyingError(
                f"聚影请求失败：{request_error_summary(error)}",
                "juying_request_failed",
            ) from error

        refreshed = str(response.headers.get("x-refreshed-token") or "").strip()
        if refreshed:
            self._set_token(refreshed)
        if response.status_code == 401 and retry_auth:
            self._set_token()
            self._login(force=True)
            return self._request(
                method,
                path,
                retry_auth=False,
                protected_access=protected_access,
                **kwargs,
            )
        if response.status_code == 429:
            retry_after = response.headers.get("retry-after") or ""
            try:
                seconds = max(60, min(600, int(float(retry_after))))
            except (TypeError, ValueError):
                seconds = 300
            self._request_gate.activate_cooldown(
                seconds, status=429, reason="聚影 HTTP 429"
            )
            self._circuit_open_until = time.monotonic() + seconds
            raise JuyingError("聚影请求过于频繁，已临时暂停该渠道", "juying_rate_limited")
        if response.status_code >= 400:
            message = ""
            if self._json_response(response):
                try:
                    body = response.json()
                    message = str(body.get("message") or body.get("detail") or "")
                except ValueError:
                    pass
            if response.status_code == 403 and protected_access:
                logger.warning(
                    "聚影资源 access 返回 HTTP 403："
                    f"path={path}，token={'present' if self._token else 'missing'}，"
                    f"csrf={'present' if self._session.cookies.get('csrftoken') else 'missing'}，"
                    f"message={message or '<empty>'}"
                )
                raise JuyingError(
                    message or "聚影资源访问票据已失效",
                    "juying_access_forbidden",
                )
            raise JuyingError(
                message or f"聚影请求失败（HTTP {response.status_code}）",
                "juying_request_failed",
            )
        if not self._json_response(response):
            raise JuyingError(
                "聚影返回了非 JSON 页面，接口响应协议异常或已改版",
                "juying_schema_changed",
            )
        try:
            payload = response.json()
        except ValueError as error:
            raise JuyingError("聚影返回数据格式异常", "juying_schema_changed") from error
        if not isinstance(payload, dict):
            raise JuyingError("聚影返回数据格式异常", "juying_schema_changed")
        return payload

    def request_json(
            self,
            method: str,
            path: str,
            protected_access: bool = False,
            **kwargs,
    ) -> Dict[str, Any]:
        """执行带登录态的聚影 JSON 请求。"""
        with self._lock:
            return self._request(
                method, path, protected_access=protected_access, **kwargs
            )

    def get_account_info(self) -> Dict[str, Any]:
        """读取当前聚影账户及可用积分。"""
        payload = self.request_json("GET", "/api/app/profile/")
        user = payload.get("user") if isinstance(payload, dict) else None
        if (
                payload.get("status") != "success"
                or not isinstance(user, dict)
                or ("points" not in user and "reward_points" not in user)
        ):
            raise JuyingError(
                "聚影账户接口缺少积分字段", "juying_schema_changed"
            )
        raw_points = (
            user.get("points")
            if "points" in user else user.get("reward_points")
        )
        try:
            points = int(raw_points or 0)
        except (TypeError, ValueError) as error:
            raise JuyingError(
                "聚影账户积分格式异常", "juying_schema_changed"
            ) from error
        return {
            "name": str(user.get("username") or user.get("email") or "聚影用户"),
            "email": str(user.get("email") or ""),
            "username": str(user.get("username") or ""),
            "avatar": str(user.get("avatar") or ""),
            "points": max(0, points),
            "level": str(user.get("level_name") or ""),
            "upload_count": max(0, int(user.get("upload_count") or 0)),
            "favorite_count": max(0, int(user.get("favorite_count") or 0)),
            "checkin_days": max(0, int(user.get("checkin_days") or 0)),
            "registered_days": max(0, int(payload.get("registered_days") or 0)),
            "created_at": str(user.get("date_joined") or ""),
        }

    def get_checkin_stats(self) -> Dict[str, Any]:
        """读取聚影当日签到状态与奖励。"""
        payload = self.request_json("GET", "/api/app/checkin/stats/")
        if payload.get("status") != "success":
            raise JuyingError(
                "聚影签到状态接口返回异常", "juying_schema_changed"
            )
        return payload

    def checkin(self) -> Dict[str, Any]:
        """通过聚影 WebAPI 完成每日签到。"""
        before = self.get_account_info()
        stats_before = self.get_checkin_stats()
        already_checked_in = bool(stats_before.get("checked_today"))
        payload: Dict[str, Any] = {}
        stats_after = stats_before
        if not already_checked_in:
            try:
                payload = self.request_json(
                    "POST", "/api/app/checkin/do/"
                )
            except JuyingError:
                stats_after = self.get_checkin_stats()
                if not stats_after.get("checked_today"):
                    raise
            else:
                stats_after = self.get_checkin_stats()
        after = before if already_checked_in else self.get_account_info()
        success = bool(
            already_checked_in
            or payload.get("status") == "success"
            or stats_after.get("checked_today")
        )
        points_before = int(before.get("points") or 0)
        points_after = int(after.get("points") or 0)
        reward_points = stats_before.get("reward_points")
        try:
            reward_points = int(reward_points or 0)
        except (TypeError, ValueError):
            reward_points = points_after - points_before
        status = (
            "今日已签到"
            if already_checked_in
            else "签到成功" if success else "签到失败"
        )
        return {
            "success": success,
            "already_checked_in": already_checked_in,
            "status": status,
            "message": str(
                payload.get("message")
                or status
            ),
            "mode": "normal",
            "signin_points": 0 if already_checked_in else reward_points,
            "points_change": points_after - points_before,
            "points_before": points_before,
            "points_after": points_after,
            "signin_days": int(
                stats_after.get("my_total_days")
                or after.get("checkin_days")
                or 0
            ),
            "status_code": 200,
            "error_code": "",
        }

    def close(self) -> None:
        with self._lock:
            self._session.close()
