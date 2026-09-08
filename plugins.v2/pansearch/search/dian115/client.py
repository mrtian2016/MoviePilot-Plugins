"""Dian115 门户登录、浏览器会话与受控请求客户端。"""

import base64
import os
import threading
import time
from typing import Any, Callable, Dict, Optional
from urllib.parse import unquote, urljoin, urlparse, urlsplit

from app.log import logger
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

from ..http_client import (
    AccountActionGate,
    RequestGate,
    gated_idempotent_request,
    normalize_proxies,
    requests,
)


class Dian115Error(RuntimeError):
    """Dian115 请求或协议错误。"""

    def __init__(self, message: str, code: str = "", status_code: int = 0):
        super().__init__(message)
        self.code = str(code or "")
        self.status_code = int(status_code or 0)


class Dian115Client:
    """维护登录 Cookie、浏览器证明和全接口统一限速。"""

    BASE_URL = "https://m.dian115.com"
    _IMPERSONATE = "chrome124"
    _USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
    _SEC_CH_UA = '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"'
    _SEC_CH_UA_FULL_VERSION = '"124.0.6367.207"'
    _SEC_CH_UA_FULL_VERSION_LIST = (
        '"Chromium";v="124.0.6367.207", '
        '"Google Chrome";v="124.0.6367.207", '
        '"Not-A.Brand";v="99.0.0.0"'
    )
    _PROOF_MARGIN_SECONDS = 15
    _RISK_COOLDOWN_SECONDS = 60
    _SERVER_ERROR_COOLDOWN_SECONDS = 5
    _PORTAL_COOKIES = ("__Host-portal_token", "__Host-portal_browser")
    _SESSION_DATA_KEY = "dian115_auth_session"
    _PROOF_RETRY_CODES = ("browser_proof_required", "browser_proof_invalid")
    _AUTH_RETRY_CODES = (
        "unauthorized", "auth_required", "invalid_token", "token_revoked", "no_token",
    )
    _LOGIN_LOCK = threading.RLock()

    @staticmethod
    def _normalize_request_interval(value: float) -> float:
        """将配置值归一化为与 RequestGate 初始化完全一致的范围。"""
        return max(0.2, min(float(value or 1.0), 10.0))

    @staticmethod
    def _normalize_unlocks_per_minute(value: int) -> int:
        """将解锁频率归一化为与 AccountActionGate 初始化一致的范围。"""
        return max(1, min(int(value or 6), 10))

    def __init__(
            self,
            email: str,
            password: str,
            base_url: str = BASE_URL,
            proxy: Any = None,
            request_interval: float = 1.0,
            unlocks_per_minute: int = 6,
            timeout: int = 30,
            get_data_func: Optional[Callable] = None,
            save_data_func: Optional[Callable] = None,
    ):
        self._email = str(email or "").strip()
        self.base_url = str(base_url or self.BASE_URL).rstrip("/")
        self._password = str(password or "").strip()
        self._proxies = normalize_proxies(proxy)
        self._timeout = max(5, min(int(timeout or 30), 120))
        self._session = requests.Session(impersonate=self._IMPERSONATE)
        self._session.headers.update({
            "user-agent": self._USER_AGENT,
            "sec-ch-ua": self._SEC_CH_UA,
            "sec-ch-ua-full-version": self._SEC_CH_UA_FULL_VERSION,
            "sec-ch-ua-full-version-list": self._SEC_CH_UA_FULL_VERSION_LIST,
        })
        self._proof: Optional[tuple[str, float]] = None
        self._browser_private_key = ec.generate_private_key(ec.SECP256R1())
        self._browser_session_expires_at = 0.0
        self._server_time_offset_ms = 0
        self._authenticated = False
        self._saved_token = ""
        self._turnstile = None
        self._turnstile_policy = None
        self._get_data_func = get_data_func
        self._save_data_func = save_data_func
        self._lock = threading.RLock()
        self._request_gate = RequestGate.shared(
            "Dian115",
            f"{self.base_url}|{self._email.casefold()}|{self._proxies}",
            request_interval=self._normalize_request_interval(request_interval),
            minimum_interval=0.2,
            risk_cooldown_seconds=self._RISK_COOLDOWN_SECONDS,
            server_error_cooldown_seconds=self._SERVER_ERROR_COOLDOWN_SECONDS,
            challenge_detector=self._is_challenge_response,
        )
        self._unlock_gate = AccountActionGate.shared(
            "Dian115 解锁接口",
            f"dian115:{self._email.casefold()}",
            max_actions=self._normalize_unlocks_per_minute(unlocks_per_minute),
            maximum_actions=10,
        )
        self._restore_auth_cookie()

    @property
    def is_configured(self) -> bool:
        return bool(self._email and self._password)

    @property
    def error_type(self):
        return Dian115Error

    def matches_config(
            self, email: str, password: str, proxy: Any,
            request_interval: float, unlocks_per_minute: int,
    ) -> bool:
        return (
                self._email == str(email or "").strip()
                and self._password == str(password or "").strip()
                and self._proxies == normalize_proxies(proxy)
                and self._request_gate.request_interval
                == self._normalize_request_interval(request_interval)
                and self._unlock_gate.max_actions
                == self._normalize_unlocks_per_minute(unlocks_per_minute)
        )

    def close(self) -> None:
        with self._lock:
            try:
                if self._turnstile is not None:
                    self._turnstile.close()
            finally:
                self._turnstile = None
                self._turnstile_policy = None
                self._session.close()
                self._proof = None
                self._browser_session_expires_at = 0.0
                self._authenticated = False

    def _clear_portal_cookies(self) -> None:
        for name in self._PORTAL_COOKIES:
            self._session.cookies.delete(name)
        self._proof = None
        self._browser_session_expires_at = 0.0
        self._server_time_offset_ms = 0
        self._authenticated = False
        self._save_auth_cookie("")

    def _cookie(self, name: str) -> str:
        return str(self._session.cookies.get_dict().get(name) or "")

    def _restore_auth_cookie(self) -> None:
        if not self._get_data_func:
            return
        try:
            data = self._get_data_func(self._SESSION_DATA_KEY) or {}
            if (
                    not isinstance(data, dict)
                    or str(data.get("email") or "").casefold() != self._email.casefold()
                    or data.get("base_url", self.BASE_URL) != self.base_url
            ):
                return
            token = str(data.get("token") or "")
            if token:
                self._session.cookies.delete("__Host-portal_token")
                self._session.cookies.set("__Host-portal_token", token, secure=True)
                self._saved_token = token
                self._authenticated = True
        except Exception as error:
            logger.debug(f"Dian115 恢复登录状态失败：{error}")

    def _save_auth_cookie(self, token: str = "") -> None:
        value = str(token or "")
        if not self._save_data_func or value == self._saved_token:
            return
        try:
            self._save_data_func(
                self._SESSION_DATA_KEY,
                {
                    "email": self._email,
                    "base_url": self.base_url,
                    "token": value,
                    "updated_at": int(time.time()),
                } if value else {},
            )
            self._saved_token = value
        except Exception as error:
            logger.debug(f"Dian115 持久化登录状态失败：{error}")

    def _headers(self, current_path: str) -> Dict[str, str]:
        path = current_path if str(current_path).startswith("/") else "/"
        headers = {
            "accept": "application/json, text/plain, */*",
            "accept-language": "zh-CN,zh;q=0.9,en;q=0.8",
            "sec-ch-ua-arch": '"x86"',
            "sec-ch-ua-bitness": '"64"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-model": '""',
            "sec-ch-ua-platform": '"Windows"',
            "sec-ch-ua-platform-version": '"19.0.0"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "x-requested-with": "XMLHttpRequest",
            "referer": urljoin(f"{self.base_url}/", path.lstrip("/")),
        }
        # curl_cffi 恢复的 __Host- Cookie 没有域名，显式携带这两个站点凭证。
        cookies = [
            f"{name}={self._cookie(name)}"
            for name in self._PORTAL_COOKIES if self._cookie(name)
        ]
        if cookies:
            headers["Cookie"] = "; ".join(cookies)
        return headers

    @staticmethod
    def _is_challenge_response(response) -> bool:
        content_type = str(response.headers.get("content-type") or "").lower()
        cf_mitigated = str(
            response.headers.get("cf-mitigated") or ""
        ).strip().lower()
        return cf_mitigated == "challenge" or "text/html" in content_type

    def _check_cooldown(self) -> None:
        remaining = self._request_gate.cooldown_remaining
        if remaining > 0:
            status = self._request_gate.cooldown_status
            raise Dian115Error(
                f"Dian115 处于风控冷却期，跳过请求（剩余 {int(remaining + 0.999)} 秒）",
                code="rate_limited" if status in {0, 403, 429} else "server_cooldown",
                status_code=status,
            )

    def _raw_request(self, method: str, path: str, **kwargs):
        self._check_cooldown()
        try:
            response = gated_idempotent_request(
                self._request_gate,
                self._session.request,
                method,
                urljoin(f"{self.base_url}/", path.lstrip("/")),
                proxies=self._proxies,
                timeout=self._timeout,
                **kwargs,
            )
            self._save_auth_cookie(self._cookie("__Host-portal_token"))
            return response
        except requests.exceptions.RequestException as error:
            raise Dian115Error(f"Dian115 请求失败：{error}") from error

    @classmethod
    def _payload(cls, response) -> Dict[str, Any]:
        try:
            payload = response.json()
        except ValueError as error:
            code = "invalid_response"
            if response.status_code == 429:
                code = "rate_limited"
            elif cls._is_challenge_response(response):
                code = "cloudflare_challenge"
            raise Dian115Error(
                f"Dian115 返回非 JSON 响应，HTTP {response.status_code}",
                code=code,
                status_code=response.status_code,
            ) from error
        if not isinstance(payload, dict):
            raise Dian115Error("Dian115 返回结构异常", code="schema_changed")
        return payload

    @classmethod
    def _raise_response_error(cls, response, payload: Dict[str, Any]) -> None:
        code = str(payload.get("code") or "")
        message = str(
            payload.get("msg") or payload.get("message")
            or f"HTTP {response.status_code}"
        )
        raise Dian115Error(message, code=code, status_code=response.status_code)

    @staticmethod
    def _base64url(value: bytes) -> str:
        return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")

    def _browser_proof(self, current_path: str) -> str:
        cached = self._proof
        if cached and cached[1] > time.time() + self._PROOF_MARGIN_SECONDS:
            return cached[0]
        response = self._raw_request(
            "GET", "/api/portal/auth/browser-challenge",
            headers=self._headers(current_path),
        )
        payload = self._payload(response)
        proof = str(payload.get("proof") or "")
        if response.status_code != 200 or payload.get("code") != "ok" or not proof:
            self._raise_response_error(response, payload)
        self._proof = (proof, time.time() + max(30, int(payload.get("ttl") or 600)))
        return proof

    def _public_jwk(self) -> Dict[str, str]:
        numbers = self._browser_private_key.public_key().public_numbers()
        return {
            "kty": "EC",
            "crv": "P-256",
            "x": self._base64url(numbers.x.to_bytes(32, "big")),
            "y": self._base64url(numbers.y.to_bytes(32, "big")),
        }

    def _ensure_browser_session(
            self, current_path: str, proof: str
    ) -> None:
        if (
                self._browser_session_expires_at
                > time.time() + self._PROOF_MARGIN_SECONDS
        ):
            return
        headers = self._headers(current_path)
        headers.update({
            "content-type": "application/json",
            "x-portal-browser-proof": proof,
        })
        response = self._raw_request(
            "POST", "/api/portal/auth/browser-session",
            headers=headers, json={"public_jwk": self._public_jwk()},
        )
        payload = self._payload(response)
        if response.status_code != 200 or payload.get("code") not in {"ok", None}:
            self._raise_response_error(response, payload)
        if payload.get("enabled") is not False and not self._cookie("__Host-portal_browser"):
            raise Dian115Error(
                "Dian115 浏览器会话未返回 Cookie", code="browser_session_missing"
            )
        now = time.time()
        try:
            self._server_time_offset_ms = int(payload["server_time_ms"]) - round(now * 1000)
        except (KeyError, TypeError, ValueError):
            self._server_time_offset_ms = 0
        self._browser_session_expires_at = now + max(60, int(payload.get("ttl") or 1800))

    def _browser_signature(self, method: str, api_path: str) -> Dict[str, str]:
        timestamp = str(round(time.time() * 1000 + self._server_time_offset_ms))
        nonce = self._base64url(os.urandom(24))
        path = urlsplit(str(api_path or "/")).path or "/"
        canonical = (
            "portal-browser-request/v1\n"
            f"{str(method or 'GET').strip().upper()}\n"
            f"{path}\n{timestamp}\n{nonce}"
        ).encode("utf-8")
        der_signature = self._browser_private_key.sign(
            canonical, ec.ECDSA(hashes.SHA256())
        )
        r_value, s_value = decode_dss_signature(der_signature)
        signature = self._base64url(
            r_value.to_bytes(32, "big") + s_value.to_bytes(32, "big")
        )
        return {
            "x-portal-browser-ts": timestamp,
            "x-portal-browser-nonce": nonce,
            "x-portal-browser-sig": signature,
        }

    def _authorized_headers(
            self,
            method: str,
            api_path: str,
            current_path: str,
    ) -> Dict[str, str]:
        proof = self._browser_proof(current_path)
        self._ensure_browser_session(current_path, proof)
        headers = self._headers(current_path)
        headers["x-portal-browser-proof"] = proof
        headers.update(self._browser_signature(method, api_path))
        return headers

    def _browser_proxy(self) -> Optional[Dict[str, str]]:
        proxies = self._proxies or {}
        proxy = proxies.get("https") or proxies.get("http")
        if not proxy:
            return None
        parsed = urlparse(str(proxy))
        if not parsed.scheme or not parsed.hostname:
            return None
        host = parsed.hostname
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        server = f"{parsed.scheme}://{host}"
        if parsed.port:
            server += f":{parsed.port}"
        result = {"server": server}
        if parsed.username:
            result["username"] = unquote(parsed.username)
        if parsed.password:
            result["password"] = unquote(parsed.password)
        return result

    def _login(self, allow_browser_login: bool = True) -> None:
        if self._authenticated:
            return
        if not self.is_configured:
            raise Dian115Error("Dian115 未配置邮箱或密码")
        with self._LOGIN_LOCK:
            self._restore_auth_cookie()
            if self._authenticated:
                return
            payload = self._request_json(
                "POST", "/api/portal/auth/login", "/login",
                require_login=False, allow_browser_login=allow_browser_login,
                json={"email": self._email, "password": self._password},
            )
            if not payload.get("user") or not self._cookie("__Host-portal_token"):
                raise Dian115Error(
                    "Dian115 登录响应缺少用户信息或认证 Cookie",
                    code="login_failed",
                )
            self._authenticated = True
            logger.debug("Dian115 接口登录成功，已保存登录状态")

    def _request_json(
            self,
            method: str,
            api_path: str,
            current_path: str,
            allow_browser_login: bool = True,
            require_login: bool = True,
            **kwargs,
    ) -> Dict[str, Any]:
        with self._lock:
            supplied_headers = dict(kwargs.pop("headers", {}) or {})
            retry_login = retry_proof = True
            action = {
                "/api/portal/auth/login": "portal_login",
                "/api/portal/unlock": "portal_unlock",
            }.get(api_path) if method.upper() == "POST" else None
            while True:
                self._check_cooldown()
                if require_login:
                    self._login(allow_browser_login=allow_browser_login)
                request_kwargs = dict(kwargs)
                if action:
                    from .security import turnstile_token
                    token = turnstile_token(self, action, allow_browser_login)
                    body = dict(kwargs.get("json") or {})
                    if token:
                        body["turnstile_token"] = token
                    else:
                        body.pop("turnstile_token", None)
                    request_kwargs["json"] = body
                # token 生成可能等待人机验证，签名时间戳必须在它之后生成。
                headers = self._authorized_headers(method, api_path, current_path)
                headers.update(supplied_headers)
                response = self._raw_request(
                    method, api_path, headers=headers, **request_kwargs
                )
                payload = self._payload(response)
                if response.status_code == 200 and payload.get("code") in {"ok", 0, "0", None}:
                    return payload
                code = str(payload.get("code") or "")
                if code in {"turnstile_failed", "turnstile_required"}:
                    self._turnstile_policy = None
                if retry_proof and code in self._PROOF_RETRY_CODES:
                    retry_proof = False
                    self._proof = None
                    self._browser_session_expires_at = 0.0
                    self._session.cookies.delete("__Host-portal_browser")
                    logger.debug(f"Dian115 浏览器证明失效，重新握手：{api_path}")
                    continue
                if require_login and retry_login and code not in self._PROOF_RETRY_CODES and (
                        response.status_code == 401 or code in self._AUTH_RETRY_CODES
                ):
                    retry_login = False
                    self._clear_portal_cookies()
                    logger.debug(f"Dian115 登录状态失效，重新登录：{api_path}")
                    continue
                self._raise_response_error(response, payload)

    def request_json(
            self,
            method: str,
            api_path: str,
            current_path: str,
            allow_browser_login: bool = True,
            **kwargs,
    ) -> Dict[str, Any]:
        """执行带登录态和浏览器证明的门户 JSON 请求。"""
        return self._request_json(
            method,
            api_path,
            current_path,
            allow_browser_login=allow_browser_login,
            **kwargs,
        )

    def get_account_info(
            self, allow_browser_login: bool = True
    ) -> Dict[str, Any]:
        """读取当前 Dian115 账户及可用积分。"""
        payload = self.request_json(
            "GET",
            "/api/portal/me",
            "/me",
            allow_browser_login=allow_browser_login,
        )
        user = payload.get("user") if isinstance(payload, dict) else None
        if not isinstance(user, dict) or "points" not in user:
            raise Dian115Error(
                "Dian115 账户接口缺少积分字段", code="schema_changed"
            )
        try:
            points = int(user.get("points") or 0)
        except (TypeError, ValueError) as error:
            raise Dian115Error(
                "Dian115 账户积分格式异常", code="schema_changed"
            ) from error
        return {
            "name": str(
                user.get("nickname") or user.get("username")
                or user.get("email") or "Dian115 用户"
            ),
            "email": str(user.get("email") or ""),
            "username": str(user.get("username") or ""),
            "avatar": str(user.get("avatar_url") or ""),
            "points": max(0, points),
            "role": str(user.get("role") or ""),
            "is_vip": bool(user.get("vip")),
            "vip_until": str(user.get("vip_until") or ""),
            "unlock_count": max(0, int(payload.get("unlock_count") or 0)),
            "consecutive_signin": max(
                0, int(user.get("consecutive_signin") or 0)
            ),
            "created_at": str(user.get("created_at") or ""),
            "last_login_at": str(user.get("last_login_at") or ""),
        }

    @staticmethod
    def _game_item(payload: Dict[str, Any], key: str) -> Dict[str, Any]:
        items = payload.get("items") if isinstance(payload, dict) else None
        item = items.get(key) if isinstance(items, dict) else None
        if not isinstance(item, dict):
            raise Dian115Error(
                f"Dian115 娱乐状态缺少 {key} 字段", code="schema_changed"
            )
        return item

    def get_game_status(self) -> Dict[str, Any]:
        """读取每日转盘次数；签到链路禁止触发浏览器登录。"""
        return self.request_json(
            "GET",
            "/api/portal/games/status",
            "/me/lottery",
            allow_browser_login=False,
        )

    def signin(self, mode: str = "normal") -> Dict[str, Any]:
        """通过门户签到接口执行普通或运气签到。"""
        normalized_mode = str(mode or "normal").strip().lower()
        if normalized_mode not in {"normal", "lucky"}:
            raise Dian115Error("Dian115 签到模式无效", code="invalid_mode")
        try:
            payload = self.request_json(
                "POST",
                "/api/portal/signin",
                "/me/signin",
                allow_browser_login=False,
                json={"mode": normalized_mode},
            )
        except Dian115Error as error:
            if error.code != "already_signed":
                raise
            return {
                "success": True,
                "already_checked_in": True,
                "status": "今日已签到",
                "message": "今日已签到",
                "mode": normalized_mode,
                "award_points": 0,
                "status_code": error.status_code,
                "error_code": error.code,
            }
        return {
            "success": True,
            "already_checked_in": False,
            "status": "签到成功",
            "message": str(payload.get("message") or "签到成功"),
            "mode": normalized_mode,
            "award_points": payload.get("award"),
            "new_balance": payload.get("new_balance"),
            "signin_days": payload.get("streak_after"),
            "lucky_tier": payload.get("lucky_tier"),
            "multiplier": payload.get("multiplier"),
            "status_code": 200,
            "error_code": "",
        }

    def run_lottery(self, target_count: int) -> Dict[str, Any]:
        """将幸运转盘补齐到当天目标次数，目标值硬限制为 20。"""
        target_plays = max(0, min(int(target_count or 0), 20))
        wheel_results = []
        wheel_error: Optional[Dian115Error] = None
        used_before = 0
        max_plays = 20
        play_count = 0
        if target_plays:
            wheel = self._game_item(self.get_game_status(), "daily_wheel")
            try:
                used_before = max(0, int(wheel.get("used_today") or 0))
                max_plays = max(
                    0, min(int(wheel.get("max_plays") or 0), 20)
                )
            except (TypeError, ValueError) as error:
                raise Dian115Error(
                    "Dian115 转盘次数格式异常", code="schema_changed"
                ) from error
            play_count = max(
                0, min(target_plays, max_plays) - used_before
            )
            for _ in range(play_count):
                try:
                    wheel_results.append(self.request_json(
                        "POST",
                        "/api/portal/lottery/wheel",
                        "/me/lottery",
                        allow_browser_login=False,
                    ))
                except Dian115Error as error:
                    wheel_error = error
                    break
        wheel_cost = 0
        wheel_award = 0
        wheel_vip_days = 0
        for item in wheel_results:
            prize = item.get("prize") if isinstance(item, dict) else None
            prize = prize if isinstance(prize, dict) else {}
            try:
                wheel_cost += max(0, int(item.get("cost") or 0))
                wheel_award += int(prize.get("points") or 0)
                wheel_vip_days += max(0, int(prize.get("vip_days") or 0))
            except (TypeError, ValueError):
                continue
        executed = len(wheel_results)
        success = wheel_error is None
        message = f"转盘 {executed}/{target_plays} 次"
        if target_plays and executed == 0 and used_before >= target_plays:
            message = f"今日转盘已完成 {used_before} 次"
        elif wheel_error:
            message = f"{message}，中断：{wheel_error}"
        balances = [
            item.get("new_balance")
            for item in wheel_results
            if isinstance(item, dict) and item.get("new_balance") is not None
        ]
        return {
            "success": success,
            "status": "转盘完成" if success else "转盘未完成",
            "message": message,
            "new_balance": balances[-1] if balances else None,
            "points_change": wheel_award - wheel_cost,
            "status_code": int(getattr(wheel_error, "status_code", 0) or 200),
            "error_code": str(getattr(wheel_error, "code", "") or ""),
            "target_count": target_plays,
            "max_plays": max_plays,
            "used_before": used_before,
            "planned": play_count,
            "executed": executed,
            "used_after": used_before + executed,
            "cost_points": wheel_cost,
            "award_points": wheel_award,
            "vip_days": wheel_vip_days,
        }
