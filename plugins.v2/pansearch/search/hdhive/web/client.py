"""HDHive WebAPI 登录、授权与受控请求客户端。"""

import base64
import contextlib
import hashlib
import json
import os
import re
import threading
import time
from typing import Any, Callable, Dict, Optional
from urllib.parse import urljoin, urlsplit

from app.core.config import settings
from app.log import logger

from .action import ServerActionProtocol, ServerActionResponse
from .captcha import HDHiveCaptchaError, HDHiveCaptchaSolver
from .parser import page_account_snapshot, response_body, response_text
from .security import (
    HDHiveSecurityProtocol,
    HDHiveSecuritySession,
    extract_bind_secret,
    is_persistent_cookie,
    is_risk_message,
    needs_action_proof,
    payload_error_message,
)
from ...http_client import (
    RequestGate,
    gated_idempotent_request,
    normalize_proxies,
    requests,
)
from ....utils.cache import create_platform_ttl_cache

# 多实例共享的会话文件锁，保证 Cookie 串行写入。
_SESSION_FILE_LOCK = threading.RLock()
class HDHiveWebError(RuntimeError):
    """HDHive WebAPI 请求、认证或协议错误。"""

    def __init__(self, message: str, code: str = "", status_code: int = 0):
        super().__init__(message)
        self.code = str(code or "")
        self.status_code = int(status_code or 0)


class _RiskCooldownState:
    """按账户共享的风控冷却记录：进程内存态 + 平台缓存持久化。"""

    _LOCK = threading.RLock()
    _BY_KEY: Dict[str, tuple] = {}

    def __init__(self, session_key: str, cache, cache_ttl: int):
        self._key = str(session_key or "")
        self._cache = cache
        self._cache_ttl = int(cache_ttl or 1)

    def remember(self, seconds: float, status: int) -> None:
        """记录一次风控冷却；仅当新的截止时间更晚时覆盖旧值。"""
        duration = max(0.0, float(seconds or 0.0))
        monotonic_until = time.monotonic() + duration
        with self._LOCK:
            current_until, _ = self._BY_KEY.get(self._key, (0.0, 0))
            if monotonic_until >= current_until:
                self._BY_KEY[self._key] = (monotonic_until, int(status or 0))
        if duration <= 0:
            return
        try:
            current = self._cache.get("state") or {}
            wall_until = time.time() + duration
            if wall_until < float(current.get("until") or 0):
                return
            self._cache.set(
                "state",
                {"until": wall_until, "status": int(status or 0)},
                ttl=max(1, min(int(duration + 0.999), self._cache_ttl)),
            )
        except Exception as error:
            logger.debug(f"HDHive 风控冷却持久化失败：{error}")

    def remaining(self) -> tuple:
        """返回（剩余秒数, 状态码），取内存态与持久化记录中的较晚者。"""
        wall_remaining = 0.0
        wall_status = 0
        try:
            persisted = self._cache.get("state") or {}
            wall_remaining = float(persisted.get("until") or 0) - time.time()
            wall_status = int(persisted.get("status") or 0)
        except Exception as error:
            logger.debug(f"HDHive 风控冷却读取失败：{error}")
        with self._LOCK:
            monotonic_until, status = self._BY_KEY.get(self._key, (0.0, 0))
            remaining = monotonic_until - time.monotonic()
            if remaining <= 0 and wall_remaining <= 0:
                self._BY_KEY.pop(self._key, None)
                return 0.0, 0
            if wall_remaining > remaining:
                return wall_remaining, wall_status
            return remaining, int(status or 0)


class HDHiveClient:
    """维护网页登录 Cookie、安全会话和统一请求限速。"""

    BASE_URL = "https://re0.me"
    _SESSION_FILE = (
            settings.PLUGIN_DATA_PATH
            / "PanSearch"
            / "hdhive-curl-session.json"
    )
    _RISK_COOLDOWN_SECONDS = 60
    _SOFT_RISK_COOLDOWN_SECONDS = 10 * 60
    _SERVER_ERROR_COOLDOWN_SECONDS = 5
    _MAX_REQUESTS_PER_MINUTE = 10
    _RISK_COOLDOWN_CACHE_TTL = 10 * 60
    _BIND_SECRETS: Dict[str, str] = {}
    def __init__(
            self,
            username: str,
            password: str,
            proxy: Any = None,
            request_interval: float = 5.0,
            timeout: int = 30,
            should_stop: Optional[Callable[[], bool]] = None,
    ):
        self._username = str(username or "").strip()
        self._password = str(password or "")
        self._proxies = normalize_proxies(proxy)
        self._timeout = max(5, min(int(timeout or 30), 120))
        self._should_stop = should_stop
        self._session_key = hashlib.sha256(
            f"{self.BASE_URL}\0{self._username}".encode("utf-8")
        ).hexdigest()
        self._risk_cooldowns = _RiskCooldownState(
            self._session_key,
            create_platform_ttl_cache(
                "hdhive:web:risk_cooldown",
                self._session_key,
                maxsize=1,
                ttl=self._RISK_COOLDOWN_CACHE_TTL,
            ),
            cache_ttl=self._RISK_COOLDOWN_CACHE_TTL,
        )
        self._session = requests.Session(impersonate="chrome")
        self._user_agent = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/136.0.0.0 Safari/537.36"
        )
        self._languages = "zh-CN,zh,en"
        self._session.headers.update({
            "user-agent": self._user_agent,
            "accept-language": "zh-CN,zh;q=0.9,en;q=0.8",
        })
        self._server_actions = ServerActionProtocol(
            self._session.cookies,
            error_factory=HDHiveWebError,
            warning=logger.warning,
        )
        self._lock = threading.RLock()
        self._authenticated = False
        self._security = HDHiveSecurityProtocol()
        self._security_session = HDHiveSecuritySession(
            self._security,
            self._raw_request,
            HDHiveWebError,
            lambda: (self._user_agent, self._languages, self._bind_secret),
            body_reader=response_body,
            base_url=self.BASE_URL,
            signed_transport=self._signed_request,
        )
        self._bind_secret = ""
        self._request_gate = RequestGate.shared(
            "HDHive WebAPI",
            self._session_key,
            request_interval=request_interval,
            minimum_interval=2.0,
            risk_cooldown_seconds=self._RISK_COOLDOWN_SECONDS,
            server_error_cooldown_seconds=self._SERVER_ERROR_COOLDOWN_SECONDS,
            challenge_detector=self._is_challenge_response,
            # 受保护页面、challenge 和业务接口必须按账号串行发出，
            # 避免多个客户端实例只预占槽位后仍重叠网络请求。
            serial_requests=True,
            max_requests_per_window=self._MAX_REQUESTS_PER_MINUTE,
            request_window_seconds=60.0,
        )
        self._captcha = HDHiveCaptchaSolver(
            self._raw_request,
            server_actions=self._server_actions,
        )
        self._load_cookies()

    @property
    def is_configured(self) -> bool:
        return bool(self._username and self._password)

    @property
    def cache_namespace(self) -> str:
        return self._session_key[:12]

    @property
    def cooldown_remaining(self) -> float:
        """返回账户风险冷却与请求门控冷却中的较长剩余时间。"""
        shared_remaining, _ = self._risk_cooldowns.remaining()
        return max(shared_remaining, self._request_gate.cooldown_remaining)

    def stop_requested(self) -> bool:
        try:
            return bool(self._should_stop and self._should_stop())
        except Exception as error:
            logger.warning(f"读取 HDHive 停止状态失败：{error}")
            return False

    def matches_config(
            self, username: str, password: str, proxy: Any,
            request_interval: float,
    ) -> bool:
        """判断现有认证会话能否复用于当前配置。"""
        return (
                self._username == str(username or "").strip()
                and self._password == str(password or "")
                and self._proxies == normalize_proxies(proxy)
                and self._request_gate.request_interval
                == max(2.0, min(float(request_interval or 5.0), 10.0))
        )

    def close(self) -> None:
        with self._lock:
            self._save_cookies()
            self._session.close()

    def activate_risk_cooldown(
            self, reason: str, seconds: Optional[int] = None
    ) -> None:
        """将协议层识别到的异常页面纳入所有 Web 请求的共享冷却。"""
        cooldown_seconds = max(
            1,
            min(
                int(seconds or self._SOFT_RISK_COOLDOWN_SECONDS),
                10 * 60,
            ),
        )
        self._request_gate.activate_cooldown(
            cooldown_seconds,
            reason=reason,
        )
        self._risk_cooldowns.remember(cooldown_seconds, status=0)

    @classmethod
    def _body_cooldown_seconds(cls, response) -> int:
        """从 429 文本中提取站点给出的中文冷却秒数。"""
        if int(getattr(response, "status_code", 0) or 0) != 429:
            return 0
        text = response_text(response)[:4096]
        match = re.search(r"(?:冷却|重试|限制)[^0-9]{0,24}(\d+)\s*秒", text)
        if not match:
            return 0
        try:
            return max(1, min(int(match.group(1)), 10 * 60))
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _is_challenge_response(response) -> bool:
        content_type = str(response.headers.get("content-type") or "").lower()
        return (
                "text/html" in content_type
                or str(response.headers.get("cf-mitigated") or "").lower()
                == "challenge"
        )

    def _raw_request(self, method: str, path: str, **kwargs):
        shared_remaining, shared_status = self._risk_cooldowns.remaining()
        cooldown_remaining = max(
            shared_remaining,
            self._request_gate.cooldown_remaining,
        )
        if cooldown_remaining > 0:
            status = shared_status or self._request_gate.cooldown_status
            status_label = f"HTTP {status}" if status else "风险保护"
            raise HDHiveWebError(
                f"HDHive WebAPI 处于{status_label}冷却期，"
                f"跳过请求（剩余 {int(cooldown_remaining + 0.999)} 秒）",
                code=(
                    "rate_limited" if status in {0, 403, 429}
                    else "server_cooldown"
                ),
                status_code=status,
            )
        request_headers = dict(kwargs.pop("headers", {}) or {})
        try:
            csrf_token = str(
                self._session.cookies.get_dict().get("csrf_access_token") or ""
            ).strip()
        except Exception:
            csrf_token = ""
        if csrf_token and "x-csrf-token" not in {
            str(key).lower() for key in request_headers
        }:
            request_headers["x-csrf-token"] = csrf_token
        try:
            response = gated_idempotent_request(
                self._request_gate,
                self._session.request,
                method,
                urljoin(f"{self.BASE_URL}/", str(path or "").lstrip("/")),
                retry_connection_errors=False,
                proxies=self._proxies,
                timeout=self._timeout,
                headers=request_headers,
                **kwargs,
            )
            body_cooldown = self._body_cooldown_seconds(response)
            if body_cooldown > self._request_gate.cooldown_remaining:
                self._request_gate.activate_cooldown(
                    body_cooldown,
                    status=429,
                    reason="HTTP 429 风控",
                )
            cooldown_status = self._request_gate.cooldown_status
            if cooldown_status in {403, 429}:
                self._risk_cooldowns.remember(
                    self._request_gate.cooldown_remaining,
                    status=cooldown_status,
                )
            return response
        except requests.exceptions.RequestException as error:
            raise HDHiveWebError(
                f"HDHive WebAPI 请求失败：{error}", code="request_failed"
            ) from error

    def request(self, method: str, path: str, **kwargs):
        """执行已登录请求；认证失效时自动重新登录一次。"""
        with self._lock:
            return self._authenticated_request(method, path, **kwargs)

    def _authenticated_request(
            self,
            method: str,
            path: str,
            retry_login: bool = True,
            retry_captcha: bool = True,
            **kwargs,
    ):
        """已登录请求；验证码与登录失效按有界循环各重试一次。"""
        self._ensure_authenticated()
        response = self._raw_request(method, path, **kwargs)
        while True:
            if self._captcha.is_challenge_response(response):
                logger.debug(
                    f"HDHive 请求命中验证码挑战："
                    f"{method} {str(path).split('?', 1)[0]}"
                )
                if not retry_captcha:
                    raise HDHiveWebError(
                        "HDHive 验证通过后仍返回安全验证页",
                        code="captcha_retry_failed",
                    )
                self._solve_captcha(response, path)
                retry_captcha = False
            elif retry_login and response.status_code == 401:
                self._authenticated = False
                if not self._refresh_auth_tokens():
                    self._session.cookies.clear()
                    self._login_with_sequence()
                retry_login = False
                # 与原递归实现一致：重新登录后恢复验证码重试机会。
                retry_captcha = True
            elif retry_login and (
                    response.status_code == 403
                    or "/login" in str(getattr(response, "url", ""))
            ):
                self._authenticated = False
                self._session.cookies.clear()
                self._login_with_sequence()
                retry_login = False
                retry_captcha = True
            else:
                break
            response = self._raw_request(method, path, **kwargs)
        if response.status_code >= 400:
            raise HDHiveWebError(
                f"HDHive 网页请求失败（HTTP {response.status_code}）",
                code=(
                    "rate_limited"
                    if response.status_code == 429
                    else "request_failed"
                ),
                status_code=response.status_code,
            )
        return response

    def _refresh_auth_tokens(self) -> bool:
        """自动刷新token避免重复登录。"""
        if not self._has_login_cookie():
            return False
        try:
            response = self._raw_request(
                "POST",
                "/api/public/auth/refresh",
                headers={
                    "content-type": "application/json",
                    "x-skip-auth-refresh": "true",
                },
                data=b"{}",
            )
            payload = self._json_payload(
                response, "HDHive Token 刷新接口返回格式异常"
            )
            if (
                    int(response.status_code or 0) < 400
                    and payload.get("success") is True
                    and self._has_login_cookie()
            ):
                self._adopt_login_session(response, payload)
                return True
        except HDHiveWebError as error:
            logger.debug(f"HDHive WebAPI 刷新登录 Token 失败：{error}")
        return False

    def _solve_captcha(
            self,
            response,
            path: str,
            status_code: int = 0,
    ) -> None:
        """在协议链内完成一次动态验证码挑战并保存 Cookie。"""
        with self.related_requests(4):
            try:
                clearance_seconds = self._captcha.solve(response, path)
            except HDHiveCaptchaError as error:
                logger.debug(
                    f"HDHive 验证码处理失败：code={error.code}，原因={error}"
                )
                raise HDHiveWebError(
                    str(error),
                    code=error.code,
                    status_code=status_code,
                ) from error
            self._save_cookies()
            logger.info(
                "HDHive 动态验证码验证通过"
                + (
                    f"，有效期 {clearance_seconds} 秒"
                    if clearance_seconds > 0 else ""
                )
            )

    def _has_login_cookie(self) -> bool:
        try:
            cookies = self._session.cookies.get_dict()
        except Exception:
            return False
        return bool(cookies.get("token") and cookies.get("refresh_token"))

    @contextlib.contextmanager
    def related_requests(self, request_count: int):
        """连续执行协议链，并固定客户端锁先于门控锁以避免反向等待。"""
        with self._lock:
            with self._request_gate.immediate_sequence(
                    request_count=request_count,
                    cancel_check=self.stop_requested,
            ):
                yield

    def _login(self) -> None:
        """通过 WASM 安全线保护的登录接口完成账号登录"""

        if not self.is_configured:
            raise HDHiveWebError("HDHive 未配置用户名或密码", code="not_configured")
        self._raw_request("GET", "/login")
        body = json.dumps({
            "username": self._username,
            "password": self._password,
        }, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        response = self._signed_request(
            "POST",
            "/api/public/auth/login",
            body=body,
            headers={"content-type": "application/json"},
            canonical_path="/api/public/auth/login",
            require_login=False,
        )
        payload = self._json_payload(response, "HDHive 登录接口返回格式异常")
        login_data = payload.get("data") if isinstance(payload, dict) else None
        logger.debug(
            "HDHive 登录响应："
            f"success={bool(payload.get('success'))}，"
            f"user_id={login_data.get('id') if isinstance(login_data, dict) else 0}"
        )
        if response.status_code != 200 or not self._has_login_cookie():
            message = payload_error_message(payload) or "登录失败"
            raise HDHiveWebError(
                f"HDHive {message}（HTTP {response.status_code}）",
                code="login_failed",
                status_code=response.status_code,
            )
        self._adopt_login_session(response, payload)

    def _adopt_login_session(self, response, payload: Dict[str, Any]) -> None:
        """记录登录成功状态并采纳新的安全绑定密钥。"""
        self._authenticated = True
        bind_secret = self._login_bind_secret(response, payload)
        if bind_secret:
            self._bind_secret = bind_secret
            session_key = getattr(self, "_session_key", "")
            if session_key:
                self._BIND_SECRETS[session_key] = bind_secret
            self._security.invalidate()
        self._save_cookies()

    def _login_bind_secret(self, response, payload: Any) -> str:
        """提取登录响应携带的安全绑定密钥。"""
        if isinstance(payload, dict):
            # Web 登录响应实际将 bind_secret 放在 meta；兼容页面/旧接口的
            # data.bindSecret 与 data.bind_secret 结构。
            for container_key in ("data", "meta"):
                container = payload.get(container_key)
                if not isinstance(container, dict):
                    continue
                for key in ("bindSecret", "bind_secret"):
                    value = str(container.get(key) or "").strip()
                    if value:
                        return value
        return extract_bind_secret(response_text(response))

    @staticmethod
    def _json_payload(response, error_message: str) -> Dict[str, Any]:
        try:
            payload = response.json()
        except ValueError as error:
            raise HDHiveWebError(
                error_message, code="schema_changed",
                status_code=response.status_code,
            ) from error
        return payload if isinstance(payload, dict) else {}

    def _login_with_sequence(self) -> None:
        """连续完成登录页访问与签名登录请求。"""
        with self.related_requests(3):
            self._login()

    def _ensure_authenticated(self) -> None:
        if self._authenticated and self._has_login_cookie():
            return
        if self._has_login_cookie():
            self._authenticated = True
            return
        self._login_with_sequence()

    def _load_cookies(self) -> None:
        with _SESSION_FILE_LOCK:
            try:
                payload = json.loads(
                    self._SESSION_FILE.read_text(encoding="utf-8")
                )
                account = (
                        payload.get("accounts", {}).get(self._session_key) or {}
                )
                if isinstance(account, list):
                    cookies = account
                else:
                    cookies = account.get("cookies") or []
                    self._bind_secret = str(
                        account.get("bind_secret")
                        or self._BIND_SECRETS.get(self._session_key)
                        or ""
                    )
            except (
                    FileNotFoundError, json.JSONDecodeError,
                    OSError, AttributeError,
            ):
                return
        now = time.time()
        for cookie in cookies:
            if not isinstance(cookie, dict):
                continue
            if not is_persistent_cookie(
                    str(cookie.get("name") or "")
            ):
                continue
            expires = float(cookie.get("expires") or 0)
            if expires > 0 and expires <= now:
                continue
            try:
                self._session.cookies.set(
                    str(cookie.get("name") or ""),
                    str(cookie.get("value") or ""),
                    domain=str(cookie.get("domain") or "re0.me"),
                    path=str(cookie.get("path") or "/"),
                    secure=bool(cookie.get("secure", True)),
                )
            except Exception:
                continue
        self._authenticated = self._has_login_cookie()
        if self._bind_secret:
            self._BIND_SECRETS[self._session_key] = self._bind_secret

    def _save_cookies(self) -> None:
        cookies = []
        try:
            for cookie in self._session.cookies.jar:
                if not is_persistent_cookie(cookie.name):
                    continue
                cookies.append({
                    "name": cookie.name,
                    "value": cookie.value,
                    "domain": cookie.domain,
                    "path": cookie.path,
                    "secure": bool(cookie.secure),
                    "expires": int(cookie.expires or 0),
                })
        except Exception:
            return
        with _SESSION_FILE_LOCK:
            payload: Dict[str, Any] = {"version": 1, "accounts": {}}
            try:
                current = json.loads(
                    self._SESSION_FILE.read_text(encoding="utf-8")
                )
                if isinstance(current, dict) and isinstance(
                        current.get("accounts"), dict
                ):
                    payload = current
            except (FileNotFoundError, json.JSONDecodeError, OSError):
                pass
            payload["version"] = 1
            payload.setdefault("accounts", {})[self._session_key] = {
                "cookies": cookies,
                "bind_secret": self._bind_secret,
            }
            try:
                session_file = self._SESSION_FILE
                session_file.parent.mkdir(parents=True, exist_ok=True)
                temp_file = session_file.with_suffix(".tmp")
                temp_file.write_text(
                    json.dumps(
                        payload, ensure_ascii=False, separators=(",", ":")
                    ),
                    encoding="utf-8",
                )
                os.chmod(temp_file, 0o600)
                os.replace(temp_file, session_file)
            except OSError as error:
                logger.debug(f"保存 HDHive WebAPI Cookie 失败：{error}")

    def _user_id(self) -> str:
        """取得签名用户 ID。"""
        try:
            cookie_user_id = str(self._session.cookies.get_dict().get("hdh_uid") or "")
            if re.fullmatch(r"[1-9]\d*", cookie_user_id):
                return cookie_user_id
        except Exception:
            pass
        return self._jwt_user_id() or "0"

    def _jwt_user_id(self) -> str:
        """读取 user_id（仅用于签名字段）。"""
        try:
            token = str(self._session.cookies.get_dict().get("token") or "")
            parts = token.split(".")
            if len(parts) != 3:
                return ""
            padding = "=" * (-len(parts[1]) % 4)
            payload = json.loads(base64.urlsafe_b64decode(parts[1] + padding))
            for key in ("user_id", "sub"):
                value = payload.get(key)
                if isinstance(value, int) and value > 0:
                    return str(value)
                if isinstance(value, str) and re.fullmatch(r"[1-9]\d*", value):
                    return value
            return ""
        except (ValueError, TypeError, KeyError, json.JSONDecodeError,
                UnicodeDecodeError):
            return ""

    def signed_request(
            self,
            method: str,
            path: str,
            body: bytes = b"",
            headers: Optional[Dict[str, str]] = None,
            canonical_path: str = "",
    ):
        """执行带 HDHive 安全会话签名的授权请求。"""
        with self._lock:
            return self._signed_request(
                method,
                path,
                body=body,
                headers=headers,
                canonical_path=canonical_path,
            )

    def _signed_request(
            self,
            method: str,
            path: str,
            body: bytes = b"",
            headers: Optional[Dict[str, str]] = None,
            retry: bool = True,
            retry_captcha: bool = True,
            canonical_path: str = "",
            require_login: bool = True,
    ):
        """签名请求主循环：验证码与安全重试各有界一次。"""
        signed_path = str(canonical_path or urlsplit(path).path or "/")
        captcha_solved = False
        while True:
            response = self._send_signed(
                method, path, signed_path, body, headers,
                require_login=require_login,
            )
            if self._captcha.is_challenge_response(response):
                if not retry_captcha:
                    raise HDHiveWebError(
                        "HDHive 验证通过后仍要求安全验证",
                        code="captcha_retry_failed",
                        status_code=response.status_code,
                    )
                logger.debug(
                    f"HDHive 签名请求命中验证码挑战：{method} {signed_path}"
                )
                self._solve_captcha(
                    response, path, status_code=response.status_code
                )
                retry_captcha = False
                captcha_solved = True
                continue
            try:
                self._security_session.verify_response(response, signed_path)
            except HDHiveWebError as error:
                # 风控类失败由客户端统一纳入共享冷却，协议层只负责识别。
                if error.code == "rate_limited" and is_risk_message(str(error)):
                    self.activate_risk_cooldown("受保护接口要求人机验证")
                raise
            if retry and response.status_code == 401:
                error_code = self._security.response_error_code(response)
                if error_code == "session_user_mismatch":
                    self._authenticated = False
                    self._session.cookies.clear()
                    self._bind_secret = ""
                    self._security.invalidate()
                    self._login_with_sequence()
                    retry = False
                    continue
                if self._security_session.prepare_retry(error_code):
                    retry = False
                    continue
            if captcha_solved:
                setattr(response, "hdhive_captcha_verified", True)
            return response

    def _send_signed(
            self,
            method: str,
            path: str,
            signed_path: str,
            body: bytes,
            headers: Optional[Dict[str, str]],
            require_login: bool = True,
    ):
        """单次发送：附加安全签名头、发送并按 X-HDH-Enc 解密。"""
        if require_login:
            self._ensure_authenticated()
        self._security_session.ensure()
        request_headers = dict(headers or {})
        request_headers.update(self._security.request_headers(
            method, signed_path, body, self._user_id()
        ))
        response = self._raw_request(
            method, path, headers=request_headers, data=body or None
        )
        return self._security_session.decode_response(response)

    def signed_json_request(
            self,
            method: str,
            path: str,
            body: Optional[Dict[str, Any]],
            *,
            action: str = "",
            path_template: str = "",
            resource_slug: str = "",
            require_login: bool = True,
            referer: str = "",
    ):
        """签名 JSON 请求；动作接口在首次业务请求前取得 Action Proof。"""
        with self._lock:
            body_bytes = (
                json.dumps(body, ensure_ascii=False, separators=(",", ":"))
                .encode("utf-8") if body is not None else b""
            )
            base_headers = {
                "content-type": "application/json",
                "origin": self.BASE_URL,
            }
            if referer:
                base_headers["referer"] = str(referer)
            if action:
                proof_headers = self._security_session.action_proof_headers(
                    action,
                    method,
                    path_template or path,
                    resource_slug,
                    body_bytes,
                )
                base_headers.update(proof_headers)
            response = self._signed_request(
                method,
                path,
                body=body_bytes,
                headers=dict(base_headers),
                canonical_path=path,
                require_login=require_login,
            )
            if not action or not needs_action_proof(response):
                return response
            # proof 过期时重新生成一次挑战证明；仍受普通门控约束。
            proof_headers = self._security_session.action_proof_headers(
                action, method, path_template or path, resource_slug, body_bytes,
            )
            base_headers.update(proof_headers)
            return self._signed_request(
                method,
                path,
                body=body_bytes,
                headers=base_headers,
                canonical_path=path,
                require_login=require_login,
            )

    def web_unlock_request(
            self,
            resource_page_path: str,
            slug: str,
            page_headers: Optional[Dict[str, str]] = None,
            on_submit: Optional[Callable[[], None]] = None,
    ) -> ServerActionResponse:
        """按资源详情页的 Server Action 流程提交解锁。"""
        with self._lock:
            started = time.monotonic()
            (
                page_response,
                response,
                honeypot_token,
                chunk,
            ) = self._server_actions.unlock(
                self._authenticated_request,
                resource_page_path,
                slug,
                page_headers=page_headers,
                base_url=self.BASE_URL,
                on_submit=on_submit,
            )
            logger.debug(
                "HDHive 资源页 Action 上下文就绪："
                f"honeypot长度={len(honeypot_token)}，"
                f"chunk={chunk.rsplit('/', 1)[-1]}"
            )
            logger.debug(
                "HDHive 网页解锁序列完成：详情页 HTTP "
                f"{getattr(page_response, 'status_code', 0)}，"
                f"Action HTTP {response.status_code}，"
                f"耗时 {(time.monotonic() - started):.2f}s"
            )
            return response

    def get_account_info(self) -> Dict[str, Any]:
        """通过已签名的用户接口读取 HDHive 可用积分。"""
        response = self.signed_request("GET", "/api/customer/user/current")
        try:
            payload = response.json()
        except ValueError as error:
            raise HDHiveWebError(
                "HDHive 账户接口返回格式异常", code="schema_changed"
            ) from error
        data = payload.get("data") if isinstance(payload, dict) else None
        if response.status_code != 200 or not isinstance(data, dict):
            raise HDHiveWebError(
                "HDHive 账户接口返回异常",
                code="schema_changed",
                status_code=response.status_code,
            )
        user_meta = data.get("user_meta")
        merged = {**data, **(user_meta if isinstance(user_meta, dict) else {})}
        if "points" not in merged:
            raise HDHiveWebError(
                "HDHive 账户接口缺少积分字段"
                f"（返回字段：{','.join(sorted(map(str, data.keys())))}）",
                code="schema_changed",
                status_code=response.status_code,
            )
        try:
            points = int(merged.get("points") or 0)
        except (TypeError, ValueError) as error:
            raise HDHiveWebError(
                "HDHive 账户积分格式异常", code="schema_changed"
            ) from error
        level = str(merged.get("level") or "").strip().lower()
        return {
            "name": str(
                merged.get("nickname") or merged.get("username") or "HDHive 用户"
            ),
            "email": str(merged.get("email") or ""),
            "username": str(merged.get("username") or ""),
            "avatar": str(
                merged.get("avatar_url") or merged.get("gravatar_url") or ""
            ),
            "points": max(0, points),
            "is_vip": bool(
                merged.get("is_active_vip")
                or merged.get("is_vip")
                or level in {"vip", "forever_vip"}
            ),
            "share_count": max(0, int(merged.get("share_num") or 0)),
            "signin_days": max(
                0, int(
                    merged.get("signin_days_total")
                    or merged.get("signin_days") or 0
                )
            ),
            "created_at": str(merged.get("created_at") or ""),
            "last_login_at": str(
                merged.get("last_web_login_at")
                or merged.get("last_login_at") or ""
            ),
            "status": str(
                merged.get("lifecycle_status")
                or ("suspended" if merged.get("is_blocked") else "") or ""
            ),
            "captcha_verified": bool(
                getattr(response, "hdhive_captcha_verified", False)
            ),
        }

    def checkin(self, is_gambler: bool = False) -> Dict[str, Any]:
        """抓取首页后，通过网页 Server Action 完成一次签到。"""
        with self.related_requests(5):
            page_snapshot: Dict[str, Any] = {}
            response = None
            try:
                response = self._server_actions.checkin(
                    self._authenticated_request,
                    bool(is_gambler),
                    base_url=self.BASE_URL,
                    on_page=lambda page: page_snapshot.update(
                        page_account_snapshot(response_text(page))
                    ),
                )
            except HDHiveWebError:
                raise
            status_code = response.status_code
            message = response.message
            error_code = response.code
            payload = response.payload or {}
            data = response.data
            response_snapshot = page_account_snapshot(response.text)
            before = dict(page_snapshot)
            after = dict(response_snapshot or before)
            for source in (data, payload):
                if not isinstance(source, dict):
                    continue
                if "points" in source and "points" not in after:
                    after["points"] = source["points"]
                if "signin_days_total" in source and "signin_days" not in after:
                    after["signin_days"] = source["signin_days_total"]
                if "signin_days" in source and "signin_days" not in after:
                    after["signin_days"] = source["signin_days"]
            already_checked_in = bool(
                data.get("already_checked_in")
                or data.get("checked_in") is False
                or any(marker in message for marker in (
                    "已经签到", "今日已签到", "签到过", "明天再来",
                ))
                or error_code in {
                    "ALREADY_CHECKED_IN", "CHECKIN_ALREADY_COMPLETED",
                }
            )
            checked_in_value = data.get("checked_in")
            success = bool(
                already_checked_in
                or (
                        status_code < 400
                        and payload.get("success") is not False
                )
            )
        points_before = int(before.get("points") or 0)
        points_after = int(after.get("points") or 0)
        points_change = points_after - points_before
        return {
            "success": success,
            "checked_in": bool(checked_in_value) and not already_checked_in,
            "already_checked_in": already_checked_in,
            "status": (
                "今日已签到" if already_checked_in
                else "签到成功" if success else "签到失败"
            ),
            "message": message or (
                "今日已签到" if already_checked_in
                else "签到成功" if success
                else f"签到失败（HTTP {status_code}）"
            ),
            "is_gambler": bool(is_gambler),
            "signin_points": 0 if already_checked_in else points_change,
            "points_change": points_change,
            "points_before": points_before,
            "points_after": points_after,
            "signin_days": int(after.get("signin_days") or 0),
            "status_code": status_code,
            "error_code": error_code,
            "captcha_verified": bool(
                before.get("captcha_verified")
                or after.get("captcha_verified")
                or getattr(response, "hdhive_captcha_verified", False)
            ),
            "raw": payload,
        }
