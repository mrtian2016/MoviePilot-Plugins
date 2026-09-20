# -*- coding: utf-8 -*-
"""
Dian115（癫影）资源查询 / 签到 / 转盘客户端。

精简自 MoviePilot-PanSearch 插件的 ``search/dian115`` 实现，保留站点的
**必备握手协议**，去掉与多源搜索框架绑定的部分（Points 预算、分级限速器）。

必须保留的握手（服务端强制校验，缺一不可）：
    1. GET  /api/portal/auth/browser-challenge  -> { proof, ttl }
    2. POST /api/portal/auth/browser-session    -> Set-Cookie __Host-portal_browser
    3. 每个业务请求带四个头：
       x-portal-browser-proof / -ts / -nonce / -sig
       其中 sig 是对
           "portal-browser-request/v1\\n{METHOD}\\n{path}\\n{ts}\\n{nonce}"
       的 ES256 签名（因此 cryptography 是硬依赖）
    4. POST /api/portal/auth/login { email, password }
       -> Set-Cookie __Host-portal_token

实测结论（2026-09-13，MoviePilot v2.15.6 容器内，真实账号）：
    * browser-challenge 这一步用普通 ``requests`` 能拿到 proof（HTTP 200）；
    * 但 **登录接口会被 Cloudflare 人机验证（Turnstile）拦截**，
      实测报错 ``turnstile_required``。站点按 TLS/JA3 指纹分流，
      普通 requests 的指纹会被判为机器人。
    * 容器内已能正常工作的两个实现（PanSearch 的 search/dian115、
      三方插件 Dian115Sign）**都使用 curl_cffi 的 chrome124 指纹**。

因此本客户端采用**软依赖自适应**：
    1. 优先 ``curl_cffi.requests.Session(impersonate="chrome124")``；
    2. 环境没有 curl_cffi 时回退普通 ``requests``，届时登录大概率被
       人机验证拦截，日志会给出明确的依赖安装提示，而不是静默失败。

Cloudflare Turnstile（v1.8.1 起）
--------------------------------
癫影对登录与解锁接口都强制 Turnstile。手工复制 ``__Host-portal_token``
的方案只能撑 24 小时，维护成本过高。v1.8.1 起接入
:mod:`clients.dian115_turnstile` 自动解算（cloakbrowser 反检测浏览器，
纯本地，无需付费 solver）：

* ``auto_login=True``（默认）且环境具备 cloakbrowser 时，账号密码即可
  全自动登录，**无需再手工粘贴 Token**；
* 环境缺失 cloakbrowser 时抛 ``code="browser_unavailable"``，上层可
  回退到手工 Token 路径，**不破坏兼容**；
* 手工粘贴的 ``__Host-portal_token`` 仍然有效，且优先级高于账号密码
  （省下一次登录请求）。

未保留的部分：
    * 积分解锁预算：P115SubSearch 只做「已解锁 / 免费」的 115 分享链接，
      不消耗积分解锁。
"""

import base64
import json
import os
import re
import threading
import time
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urljoin, urlsplit

import requests
from app.log import logger

from .dian115_turnstile import Dian115Turnstile, Dian115TurnstileUnavailable

# 软依赖：TLS 指纹伪装。缺失时回退普通 requests（见模块说明）。
try:
    from curl_cffi import requests as curl_requests

    _CURL_ERROR: Optional[str] = None
except ImportError as _curl_exc:  # pragma: no cover - 取决于运行环境
    curl_requests = None  # type: ignore[assignment]
    _CURL_ERROR = str(_curl_exc)

# ES256 签名是站点强制的浏览器证明，缺依赖必须在初始化时给出可操作提示。
try:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

    _CRYPTOGRAPHY_ERROR: Optional[str] = None
except ImportError as _exc:  # pragma: no cover - 取决于运行环境
    hashes = ec = decode_dss_signature = None  # type: ignore[assignment]
    _CRYPTOGRAPHY_ERROR = str(_exc)

# 门户资源键编码（与站点前端一致）
_KEY_VERSION = 1
_KEY_MASK = (55, 161, 92, 233)

# 会话持久化键
SESSION_DATA_KEY = "dian115_auth_session"


class Dian115Error(RuntimeError):
    """Dian115 接口错误。"""

    def __init__(self, message: str, code: str = "", status_code: int = 0):
        super().__init__(message)
        self.code = str(code or "")
        self.status_code = int(status_code or 0)


def encode_resource_key(source: str, media_type: str, resource_id: Any, season: Any = 0) -> str:
    """编码门户资源键（source|media_type|id|season 异或后 base64url）。"""
    raw = (
        f"{_KEY_VERSION}|{str(source or '').strip().lower()}|"
        f"{str(media_type or '').strip().lower()}|{int(resource_id)}|"
        f"{int(season or 0)}"
    ).encode("utf-8")
    encoded = bytes(value ^ _KEY_MASK[index % len(_KEY_MASK)]
                    for index, value in enumerate(raw))
    return base64.urlsafe_b64encode(encoded).decode("ascii").rstrip("=")


def resource_path(media_type: str, tmdb_id: Any, season: Any = 0) -> str:
    """资源详情页路径。"""
    return f"/r/{encode_resource_key('tmdb', media_type, tmdb_id, season)}"


def share_path(share_id: Any) -> str:
    """分享详情页路径。"""
    return f"/s/{encode_resource_key('share', 'other', share_id)}"


def share_url_from_codes(share_code: str, receive_code: str) -> str:
    """按分享码/提取码拼出 115 分享链接。"""
    code = str(share_code or "").strip()
    password = str(receive_code or "").strip()
    if not code:
        return ""
    if password:
        return f"https://115.com/s/{code}?password={password}"
    return f"https://115.com/s/{code}"


def is_115_share_url(url: str) -> bool:
    """判断是否为可直接转存的 115 分享链接。"""
    value = str(url or "").strip()
    if not value:
        return False
    return bool(re.match(r"^https?://(?:www\.)?(?:115\.com|115cdn\.com|anxia\.com)/s/", value))


def _jwt_expires_at(token: str) -> float:
    """从 ``__Host-portal_token``（JWT）里解出 ``exp``。

    站点签发的该 Cookie 是标准 JWT（HS256），payload 含 ``exp``，
    ``Max-Age=86400``（24 小时）。本地解出过期时间即可在 UI 上提示
    剩余有效期，无需额外请求。

    :return: Unix 时间戳；解析失败返回 0.0（调用方据此判断为「未知」）
    """
    value = str(token or "").strip()
    if value.count(".") != 2:
        return 0.0
    try:
        payload_segment = value.split(".")[1]
        padding = "=" * (-len(payload_segment) % 4)
        payload = json.loads(
            base64.urlsafe_b64decode(payload_segment + padding).decode("utf-8")
        )
        return float(payload.get("exp") or 0)
    except Exception:
        return 0.0


class Dian115Client:
    """Dian115 门户客户端：登录 + 资源查询 + 签到 + 转盘。

    只保留单插件需要的路径，请求串行化（单锁）以保证浏览器证明与登录态一致。
    """

    BASE_URL = "https://m.dian115.com"
    # curl_cffi 的浏览器指纹；站点按 TLS/JA3 分流，chrome124 实测可过 WAF
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
    _PORTAL_COOKIES = ("__Host-portal_token", "__Host-portal_browser")
    _PROOF_MARGIN_SECONDS = 15
    _RISK_COOLDOWN_SECONDS = 60
    _SERVER_ERROR_COOLDOWN_SECONDS = 5
    _PROOF_RETRY_CODES = ("browser_proof_required", "browser_proof_invalid")
    _AUTH_RETRY_CODES = (
        "unauthorized", "auth_required", "invalid_token", "token_revoked", "no_token",
    )
    _TURNSTILE_CODES = {"turnstile_required", "turnstile_failed"}

    def __init__(
            self,
            email: str = "",
            password: str = "",
            base_url: str = "",
            proxy: Any = None,
            request_interval: float = 1.0,
            timeout: int = 30,
            get_data_func: Optional[Callable] = None,
            save_data_func: Optional[Callable] = None,
            token: str = "",
            auto_login: bool = True,
            browser_proxy: Any = None,
    ):
        self._email = str(email or "").strip()
        self._password = str(password or "").strip()
        self._explicit_token = str(token or "").strip()
        self._auto_login = bool(auto_login)
        self.base_url = str(base_url or self.BASE_URL).rstrip("/")
        self._proxies = self._normalize_proxies(proxy)
        # 浏览器专用代理（默认复用 HTTP 代理）；与门户请求代理解耦，
        # 便于用户单独指定海外出口给 Cloudflare 验证使用
        self._browser_proxy_setting = browser_proxy
        self._timeout = max(5, min(int(timeout or 30), 120))
        self._request_interval = max(0.2, min(float(request_interval or 1.0), 10.0))
        self._get_data_func = get_data_func
        self._save_data_func = save_data_func

        self._session = self._new_session()
        self._session.headers.update({
            "user-agent": self._USER_AGENT,
            "sec-ch-ua": self._SEC_CH_UA,
            "sec-ch-ua-full-version": self._SEC_CH_UA_FULL_VERSION,
            "sec-ch-ua-full-version-list": self._SEC_CH_UA_FULL_VERSION_LIST,
        })
        self._private_key = None
        if _CRYPTOGRAPHY_ERROR is None:
            self._private_key = ec.generate_private_key(ec.SECP256R1())

        self._lock = threading.RLock()
        self._proof: Optional[tuple] = None
        self._browser_session_expires_at = 0.0
        self._server_time_offset_ms = 0
        self._authenticated = False
        self._saved_token = ""
        self._last_request_at = 0.0
        self._cooldown_until = 0.0
        self._cooldown_status = 0
        self._turnstile_policy: Optional[tuple] = None
        # Turnstile 解算器（懒创建；环境缺失 cloakbrowser 时保持 None）
        self._turnstile: Optional[Dian115Turnstile] = None
        # 环境不可用标记：一旦确认缺失就不再重复尝试导入，直接走手工 Token 回退
        self._turnstile_unavailable: str = ""
        self._restore_auth_cookie()

    # ------------------------------------------------------------------
    # 基本属性与配置比对
    # ------------------------------------------------------------------
    @property
    def is_configured(self) -> bool:
        return bool(self._email and self._password)

    @property
    def error_type(self):
        """缺失的硬依赖名；依赖齐备时返回 None。

        调用方据此判断是否可用：``if client.error_type: 不可用``。
        注意：不要返回异常类本身，否则真值恒为 True 会把可用实例误判为不可用。
        """
        if _CRYPTOGRAPHY_ERROR:
            return "cryptography（ES256 签名必需）"
        return None

    @property
    def transport(self) -> str:
        """当前使用的传输层：curl_cffi（带指纹）或 requests（无指纹）。"""
        return "curl_cffi" if curl_requests is not None else "requests"

    def _new_session(self):
        """创建会话：优先带 Chrome TLS 指纹的 curl_cffi，否则回退 requests。"""
        if curl_requests is not None:
            return curl_requests.Session(impersonate=self._IMPERSONATE)
        if _CURL_ERROR:
            logger.warning(
                "未安装 curl_cffi，癫影登录可能被 Cloudflare 人机验证拦截：%s" % _CURL_ERROR
            )
        return requests.Session()

    def matches_config(
            self,
            email: str,
            password: str,
            proxy: Any = None,
            request_interval: float = 1.0,
            auto_login: bool = True,
    ) -> bool:
        """判断现有实例是否仍匹配最新配置（用于复用长连接）。"""
        return (
                self._email == str(email or "").strip()
                and self._password == str(password or "").strip()
                and self._auto_login == bool(auto_login)
                and self._proxies == self._normalize_proxies(proxy)
                and abs(self._request_interval
                        - max(0.2, min(float(request_interval or 1.0), 10.0))) < 1e-6
        )

    def close(self) -> None:
        with self._lock:
            self._session.close()
            self._session = self._new_session()
            self._session.headers.update({
                "user-agent": self._USER_AGENT,
                "sec-ch-ua": self._SEC_CH_UA,
                "sec-ch-ua-full-version": self._SEC_CH_UA_FULL_VERSION,
                "sec-ch-ua-full-version-list": self._SEC_CH_UA_FULL_VERSION_LIST,
            })
            self._proof = None
            self._browser_session_expires_at = 0.0
            self._authenticated = False
            # 释放 Turnstile 浏览器（子线程内关闭，避免阻塞调用方）
            self._close_turnstile()

    @staticmethod
    def _normalize_proxies(proxy: Any) -> Dict[str, str]:
        if not proxy:
            return {}
        if isinstance(proxy, dict):
            return {str(k): str(v) for k, v in proxy.items() if v}
        return {"http": str(proxy), "https": str(proxy)}

    @staticmethod
    def _base64url(value: bytes) -> str:
        return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")

    # ------------------------------------------------------------------
    # Cookie 持久化（跨重启复用登录态，避免频繁登录触发风控）
    # ------------------------------------------------------------------
    def _cookie(self, name: str) -> str:
        return str(self._session.cookies.get_dict().get(name) or "")

    def _drop_cookie(self, name: str) -> None:
        """删除会话 Cookie（兼容 requests / curl_cffi 两套 Cookie 容器）。

        两个坑都踩过：
          * ``requests`` 的 ``RequestsCookieJar`` **没有 ``delete()``**
            （``http.cookiejar.CookieJar`` 也没有），历史实现误用 ``delete()``
            导致 ``_apply_token`` 每次在写入前抛 AttributeError 被吞掉，
            表现为「Token 填了却始终未登录」；
          * ``curl_cffi`` 的 ``Cookies.clear()`` **不接受 ``name`` 关键字**
            （只有 domain/path），照搬 requests 的签名同样抛 TypeError。

        因此这里改成「只依赖两套实现都稳定的 `set(name, "", expire=0)`
        过期法」，任何一步失败都不影响后续写入。
        """
        try:
            # 最稳妥：把该 Cookie 置空并立即过期——requests 的 jar 会把它
            # 标记为已过期（max-age=0 → 源站不再看到该值）
            old = self._session.cookies.get(name)
            if old is not None:
                self._session.cookies.set(name, "", expire=0)
        except Exception:
            pass
        try:
            # requests 分支：标准 cookiejar 的 clear(domain, path, name)
            self._session.cookies.clear(domain="m.dian115.com", path="/", name=name)
        except Exception:
            pass
        try:
            # curl_cffi 分支：Cookies 支持按键删除
            if name in self._session.cookies.get_dict():
                del self._session.cookies[name]
        except Exception:
            pass

    def _restore_auth_cookie(self) -> None:
        """装载认证 Cookie。

        优先级：
            1. 用户在插件配置里手工粘贴的浏览器 Token（``token`` 参数）——
               手工 Token 一定是最新鲜的，先用它可省下一次登录请求；
               其 JWT ``exp`` 已过期时直接忽略，转走自动登录；
            2. 上次成功登录（含自动登录）后持久化到插件数据里的 token。
        """
        token = self._explicit_token
        expires_at = _jwt_expires_at(token)
        if token and expires_at and expires_at <= time.time():
            logger.info("癫影手工 Token 已过期（JWT exp 到期），转为自动登录")
            token = ""
        if not token and self._get_data_func:
            try:
                data = self._get_data_func(SESSION_DATA_KEY) or {}
                if (
                        isinstance(data, dict)
                        and str(data.get("email") or "").casefold() == self._email.casefold()
                        and data.get("base_url", self.BASE_URL) == self.base_url
                ):
                    saved = str(data.get("token") or "")
                    saved_expires_at = _jwt_expires_at(saved)
                    if saved and (not saved_expires_at or saved_expires_at > time.time()):
                        token = saved
            except Exception as e:
                logger.debug(f"读取 Dian115 会话失败: {e}")
        if token:
            self._apply_token(token)

    def _apply_token(self, token: str) -> None:
        """把 token 写入会话 Cookie 并标记为已认证。"""
        try:
            self._drop_cookie("__Host-portal_token")
            # 同时给 domain 与 path：__Host- 前缀 Cookie 的规范约束是
            # Secure + Path=/ + 无 Domain；显式给 domain 让两套实现都能命中
            self._session.cookies.set(
                "__Host-portal_token", token, domain="m.dian115.com",
                path="/", secure=True,
            )
        except Exception as e:
            logger.warning(f"写入 Dian115 Token 失败: {e}")
            return
        if not self._cookie("__Host-portal_token"):
            logger.warning("写入 Dian115 Token 后回读为空，认证态不可用")
            return
        self._saved_token = token
        self._authenticated = True

    def _save_auth_cookie(self, token: str = "") -> None:
        value = str(token or "")
        if not self._save_data_func or value == self._saved_token:
            return
        try:
            self._save_data_func(
                SESSION_DATA_KEY,
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

    def _clear_portal_cookies(self) -> None:
        for name in self._PORTAL_COOKIES:
            self._drop_cookie(name)
        self._proof = None
        self._browser_session_expires_at = 0.0
        self._server_time_offset_ms = 0
        self._authenticated = False
        self._save_auth_cookie("")

    # ------------------------------------------------------------------
    # 请求层：节流 / 冷却 / 载荷校验
    # ------------------------------------------------------------------
    def _check_cooldown(self) -> None:
        remaining = self._cooldown_until - time.monotonic()
        if remaining > 0:
            raise Dian115Error(
                f"Dian115 处于冷却期，跳过请求（剩余 {int(remaining + 0.999)} 秒）",
                code="rate_limited" if self._cooldown_status in {0, 403, 429}
                else "server_cooldown",
                status_code=self._cooldown_status,
            )

    def _throttle(self) -> None:
        """简易串行节流：替代原实现的分级限速器。"""
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < self._request_interval:
            time.sleep(self._request_interval - elapsed)

    def _mark_cooldown(self, status_code: int) -> None:
        seconds = (self._RISK_COOLDOWN_SECONDS if status_code in {403, 429}
                   else self._SERVER_ERROR_COOLDOWN_SECONDS)
        self._cooldown_until = time.monotonic() + seconds
        self._cooldown_status = int(status_code or 0)

    @staticmethod
    def _is_challenge_response(response) -> bool:
        content_type = str(response.headers.get("content-type") or "").lower()
        cf_mitigated = str(response.headers.get("cf-mitigated") or "").strip().lower()
        return cf_mitigated == "challenge" or "text/html" in content_type

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
        cookies = [
            f"{name}={self._cookie(name)}"
            for name in self._PORTAL_COOKIES if self._cookie(name)
        ]
        if cookies:
            headers["Cookie"] = "; ".join(cookies)
        return headers

    def _raw_request(self, method: str, path: str, **kwargs):
        self._check_cooldown()
        self._throttle()
        try:
            response = self._session.request(
                method,
                urljoin(f"{self.base_url}/", path.lstrip("/")),
                proxies=self._proxies or None,
                timeout=self._timeout,
                **kwargs,
            )
        except requests.exceptions.RequestException as error:
            raise Dian115Error(f"Dian115 请求失败：{error}") from error
        finally:
            self._last_request_at = time.monotonic()
        if response.status_code in {403, 429} or response.status_code >= 500:
            self._mark_cooldown(response.status_code)
        self._save_auth_cookie(self._cookie("__Host-portal_token"))
        return response

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

    @staticmethod
    def _raise_response_error(response, payload: Dict[str, Any]) -> None:
        code = str(payload.get("code") or "")
        message = str(
            payload.get("msg") or payload.get("message")
            or f"HTTP {response.status_code}"
        )
        raise Dian115Error(message, code=code, status_code=response.status_code)

    # ------------------------------------------------------------------
    # 浏览器证明（ES256）
    # ------------------------------------------------------------------
    def _require_cryptography(self) -> None:
        if self._private_key is None:
            raise Dian115Error(
                "Dian115 浏览器证明需要 cryptography，请先安装依赖后重启插件"
                f"（{_CRYPTOGRAPHY_ERROR}）",
                code="browser_unavailable",
            )

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
        self._require_cryptography()
        numbers = self._private_key.public_key().public_numbers()
        return {
            "kty": "EC",
            "crv": "P-256",
            "x": self._base64url(numbers.x.to_bytes(32, "big")),
            "y": self._base64url(numbers.y.to_bytes(32, "big")),
        }

    def _ensure_browser_session(self, current_path: str, proof: str) -> None:
        if self._browser_session_expires_at > time.time() + self._PROOF_MARGIN_SECONDS:
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
        if payload.get("enabled") is False:
            # 站点关闭了浏览器证明，后续请求不再要求签名头。
            self._browser_session_expires_at = time.time() + 3600
            return
        if not self._cookie("__Host-portal_browser"):
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
        self._require_cryptography()
        timestamp = str(round(time.time() * 1000 + self._server_time_offset_ms))
        nonce = self._base64url(os.urandom(24))
        path = urlsplit(str(api_path or "/")).path or "/"
        canonical = (
            "portal-browser-request/v1\n"
            f"{str(method or 'GET').strip().upper()}\n"
            f"{path}\n{timestamp}\n{nonce}"
        ).encode("utf-8")
        der_signature = self._private_key.sign(canonical, ec.ECDSA(hashes.SHA256()))
        r_value, s_value = decode_dss_signature(der_signature)
        signature = self._base64url(
            r_value.to_bytes(32, "big") + s_value.to_bytes(32, "big")
        )
        return {
            "x-portal-browser-ts": timestamp,
            "x-portal-browser-nonce": nonce,
            "x-portal-browser-sig": signature,
        }

    def _authorized_headers(self, method: str, api_path: str, current_path: str) -> Dict[str, str]:
        proof = self._browser_proof(current_path)
        self._ensure_browser_session(current_path, proof)
        headers = self._headers(current_path)
        headers["x-portal-browser-proof"] = proof
        headers.update(self._browser_signature(method, api_path))
        return headers

    # ------------------------------------------------------------------
    # 登录
    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    # Cloudflare Turnstile 自动解算（v1.8.1）
    # ------------------------------------------------------------------
    def _browser_proxy(self) -> Any:
        """浏览器使用的代理。

        优先使用独立配置的 ``browser_proxy``；未配置时复用门户请求代理
        （puppeteer/playwright 只接受单个代理字符串，不接受 requests 的
        ``{"http": ..., "https": ...}`` 字典）。
        """
        value = self._browser_proxy_setting
        if value:
            if isinstance(value, dict):
                return value.get("https") or value.get("http") or None
            return str(value)
        if self._proxies:
            return self._proxies.get("https") or self._proxies.get("http") or None
        return None

    def _close_turnstile(self) -> None:
        """释放 Turnstile 解算器（幂等）。"""
        solver, self._turnstile = self._turnstile, None
        if solver is None:
            return
        try:
            solver.close()
        except Exception as error:
            logger.debug(f"Dian115 释放验证浏览器失败：{type(error).__name__}")

    def _turnstile_browser_ready(self) -> bool:
        """探测浏览器环境是否可用（结果缓存，避免重复 import）。"""
        if self._turnstile_unavailable:
            return False
        try:
            from cloakbrowser import launch_context  # noqa: F401
        except ImportError as error:
            self._turnstile_unavailable = str(error) or "cloakbrowser 未安装"
            return False
        return True

    @property
    def auto_login_available(self) -> bool:
        """自动登录是否可用（供 UI / 日志 / 状态诊断使用）。"""
        return bool(self._email and self._password
                    and self._auto_login
                    and self._turnstile_browser_ready())

    def _turnstile_token(self, action: str, allow_browser: bool) -> Optional[str]:
        """获取登录/解锁所需的 Turnstile token。

        :param action: ``portal_login`` 或 ``portal_unlock``
        :param allow_browser: 是否允许驱动浏览器解算。签到等后台链路传
            ``False``，避免在任务线程里冷启动浏览器。
        :raises Dian115Error: 策略异常 / 浏览器不可用 / 解算失败
        """
        cached = self._turnstile_policy
        now = time.monotonic()
        if cached and cached[1] > now:
            policy = cached[0]
        else:
            policy = self._request_json(
                "GET", "/api/portal/auth/policy", "/login", require_login=False
            )
            self._turnstile_policy = (policy, now + 300)
        if policy.get("turnstile_enabled") is False:
            # 站点关掉了人机验证，直接走纯净登录
            return None
        if policy.get("turnstile_enabled") is not True:
            raise Dian115Error(
                "Dian115 未返回 Cloudflare 验证策略", code="schema_changed"
            )
        site_key = str(policy.get("turnstile_site_key") or "").strip()
        if not site_key:
            raise Dian115Error(
                "Dian115 未返回 Cloudflare site key", code="schema_changed"
            )
        if not allow_browser:
            raise Dian115Error(
                "该接口需要 Cloudflare 人机验证，但当前链路不允许驱动浏览器；"
                "请先手动刷新账户登录状态",
                code="browser_login_forbidden",
            )
        if not self._auto_login:
            raise Dian115Error(
                "癫影已开启 Cloudflare 人机验证，且插件「自动登录」开关为关闭状态。"
                "请在「癫影」页签打开「自动登录（Cloudflare 验证）」，"
                "或手工粘贴浏览器 Cookie 中的 __Host-portal_token",
                code="turnstile_required",
            )
        if not self._turnstile_browser_ready():
            raise Dian115Error(
                "癫影需要 Cloudflare 人机验证，但当前环境缺少 cloakbrowser 浏览器"
                f"（{self._turnstile_unavailable or '未知原因'}）。"
                "请在 MoviePilot 内运行，或手工粘贴 __Host-portal_token",
                code="browser_unavailable",
            )
        try:
            if self._turnstile is None:
                self._turnstile = Dian115Turnstile(
                    self.base_url, self._browser_proxy()
                )
            solver = self._turnstile
            token = solver.token(site_key, action)
            if not token:
                raise RuntimeError("Cloudflare 未返回验证 token")
            return token
        except Dian115TurnstileUnavailable as error:
            raise Dian115Error(str(error), code="browser_unavailable") from error
        except TimeoutError as error:
            raise Dian115Error("Dian115 Cloudflare 验证超时", code="turnstile_timeout") from error
        except RuntimeError as error:
            raise Dian115Error(str(error), code="turnstile_failed") from error
        except Exception as error:
            raise Dian115Error(
                f"Dian115 Cloudflare 验证失败：{type(error).__name__}",
                code="turnstile_failed",
            ) from error

    def _login(self, allow_browser_login: bool = True) -> None:
        if self._authenticated:
            return
        if not self.is_configured:
            raise Dian115Error("Dian115 未配置邮箱或密码", code="not_configured")
        self._restore_auth_cookie()
        if self._authenticated:
            return
        body = {"email": self._email, "password": self._password}
        token = self._turnstile_token("portal_login", allow_browser_login)
        if token:
            body["turnstile_token"] = token
        payload = self._request_json(
            "POST", "/api/portal/auth/login", "/login",
            require_login=False, allow_browser_login=allow_browser_login,
            json=body,
        )
        if not payload.get("user") or not self._cookie("__Host-portal_token"):
            raise Dian115Error(
                "Dian115 登录响应缺少用户信息或认证 Cookie", code="login_failed"
            )
        self._authenticated = True
        logger.info("Dian115 登录成功")

    def login_status(self) -> Dict[str, Any]:
        """供 UI / 诊断使用的登录态摘要（**不发起任何网络请求**）。"""
        token = self._explicit_token or self._saved_token
        expires_at = _jwt_expires_at(token)
        return {
            "email": self._email,
            "has_password": bool(self._password),
            "auto_login": self._auto_login,
            "browser_available": self._turnstile_browser_ready(),
            "browser_error": self._turnstile_unavailable,
            "has_token": bool(token),
            "token_expires_at": expires_at,
            "token_remaining_hours": (
                round(max(0.0, expires_at - time.time()) / 3600, 1) if expires_at else None
            ),
        }

    # ------------------------------------------------------------------
    # 统一请求入口
    # ------------------------------------------------------------------
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
            needs_turnstile = (
                    method.upper() == "POST"
                    and api_path in {"/api/portal/auth/login", "/api/portal/unlock"}
            )
            while True:
                self._check_cooldown()
                if require_login:
                    self._login(allow_browser_login=allow_browser_login)
                request_kwargs = dict(kwargs)
                if needs_turnstile:
                    body = dict(kwargs.get("json") or {})
                    token = self._turnstile_token(
                        "portal_login" if api_path.endswith("/login") else "portal_unlock",
                        allow_browser_login,
                    )
                    if token:
                        body["turnstile_token"] = token
                    request_kwargs["json"] = body
                headers = self._authorized_headers(method, api_path, current_path)
                headers.update(supplied_headers)
                response = self._raw_request(
                    method, api_path, headers=headers, **request_kwargs
                )
                payload = self._payload(response)
                if response.status_code == 200 and payload.get("code") in {"ok", 0, "0", None}:
                    return payload
                code = str(payload.get("code") or "")
                if code in self._TURNSTILE_CODES:
                    self._turnstile_policy = None
                if retry_proof and code in self._PROOF_RETRY_CODES:
                    retry_proof = False
                    self._proof = None
                    self._browser_session_expires_at = 0.0
                    self._drop_cookie("__Host-portal_browser")
                    logger.debug(f"Dian115 浏览器证明失效，重新握手：{api_path}")
                    continue
                if (
                        require_login and retry_login
                        and code not in self._PROOF_RETRY_CODES
                        and (response.status_code == 401 or code in self._AUTH_RETRY_CODES)
                ):
                    retry_login = False
                    self._clear_portal_cookies()
                    logger.debug(f"Dian115 登录状态失效，重新登录：{api_path}")
                    continue
                self._raise_response_error(response, payload)

    def request_json(self, method: str, api_path: str, current_path: str, **kwargs) -> Dict[str, Any]:
        """执行带登录态与浏览器证明的门户 JSON 请求。"""
        return self._request_json(method, api_path, current_path, **kwargs)

    # ------------------------------------------------------------------
    # 账户 / 签到 / 转盘
    # ------------------------------------------------------------------
    def get_account_info(self, allow_browser_login: bool = True) -> Dict[str, Any]:
        """读取账户与积分。"""
        payload = self.request_json(
            "GET", "/api/portal/me", "/me",
            allow_browser_login=allow_browser_login,
        )
        user = payload.get("user") if isinstance(payload, dict) else None
        if not isinstance(user, dict) or "points" not in user:
            raise Dian115Error("Dian115 账户接口缺少积分字段", code="schema_changed")
        try:
            points = int(user.get("points") or 0)
        except (TypeError, ValueError) as error:
            raise Dian115Error("Dian115 账户积分格式异常", code="schema_changed") from error
        return {
            "name": str(
                user.get("nickname") or user.get("username")
                or user.get("email") or "Dian115 用户"
            ),
            "email": str(user.get("email") or ""),
            "username": str(user.get("username") or ""),
            "avatar": str(user.get("avatar") or ""),
            "points": max(0, points),
            "role": str(user.get("role") or ""),
            "is_vip": bool(user.get("is_vip")),
            "vip_until": str(user.get("vip_until") or ""),
            "unlock_count": int(user.get("unlock_count") or 0),
            "consecutive_signin": int(
                user.get("consecutive_signin") or user.get("signin_streak") or 0
            ),
            "created_at": str(user.get("created_at") or ""),
            "last_login_at": str(user.get("last_login_at") or ""),
        }

    @staticmethod
    def _game_item(payload: Dict[str, Any], key: str) -> Dict[str, Any]:
        """取出指定娱乐项。

        站点实测返回 ``{"code": "ok", "items": {"daily_wheel": {...}}}``，
        历史上也曾是 ``games`` 或直接挂在根上；三种容器都要兼容，
        否则会误报「缺少 daily_wheel 字段」导致签到/抽奖整条链路失败。
        """
        if not isinstance(payload, dict):
            raise Dian115Error(f"Dian115 娱乐状态缺少 {key} 字段", code="schema_changed")
        for container in (
                payload.get("items"),
                payload.get("games"),
                payload.get("data"),
                payload,
        ):
            if isinstance(container, dict) and isinstance(container.get(key), dict):
                return container[key]
        raise Dian115Error(f"Dian115 娱乐状态缺少 {key} 字段", code="schema_changed")

    def get_game_status(self) -> Dict[str, Any]:
        """读取每日转盘次数；签到链路禁止触发浏览器登录。"""
        return self.request_json(
            "GET", "/api/portal/games/status", "/me/lottery",
            allow_browser_login=False,
        )

    def signin(self, mode: str = "normal") -> Dict[str, Any]:
        """普通或运气签到。"""
        normalized_mode = str(mode or "normal").strip().lower()
        if normalized_mode not in {"normal", "lucky"}:
            raise Dian115Error("Dian115 签到模式无效", code="invalid_mode")
        try:
            payload = self.request_json(
                "POST", "/api/portal/signin", "/me/signin",
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
        """把幸运转盘补齐到当天目标次数（硬上限 20）。"""
        target_plays = max(0, min(int(target_count or 0), 20))
        wheel_results: List[Dict[str, Any]] = []
        wheel_error: Optional[Dian115Error] = None
        used_before = 0
        max_plays = 20
        play_count = 0
        if target_plays:
            wheel = self._game_item(self.get_game_status(), "daily_wheel")
            try:
                used_before = max(0, int(wheel.get("used_today") or 0))
                max_plays = max(0, min(int(wheel.get("max_plays") or 0), 20))
            except (TypeError, ValueError) as error:
                raise Dian115Error(
                    "Dian115 转盘次数格式异常", code="schema_changed"
                ) from error
            play_count = max(0, min(target_plays, max_plays) - used_before)
            for _ in range(play_count):
                try:
                    wheel_results.append(self.request_json(
                        "POST", "/api/portal/lottery/wheel", "/me/lottery",
                        allow_browser_login=False,
                    ))
                except Dian115Error as error:
                    wheel_error = error
                    break
        wheel_cost = wheel_award = wheel_vip_days = 0
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
            item.get("new_balance") for item in wheel_results
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

    # ------------------------------------------------------------------
    # 资源查询（只产出可直接转存的 115 分享链接）
    # ------------------------------------------------------------------
    def resource_detail(
            self, tmdb_id: int, media_type: str, season: int = 0
    ) -> Dict[str, Any]:
        """按 TMDB 媒体标识读取资源与分享列表。"""
        normalized_id = int(tmdb_id or 0)
        normalized_type = str(media_type or "").strip().lower()
        if normalized_type not in {"movie", "tv"}:
            raise Dian115Error("Dian115 资源类型无效", code="invalid_media_type")
        if normalized_id <= 0:
            raise Dian115Error("Dian115 缺少 TMDB ID", code="missing_tmdb_id")
        path = resource_path(normalized_type, normalized_id, int(season or 0))
        key = path.rsplit("/", 1)[-1]
        payload = self.request_json(
            "GET", "/api/portal/resource-detail", path, params={"key": key}
        )
        payload["resource_key"] = key
        payload["resource_path"] = path
        return payload

    def search_resources(
            self, tmdb_id: int, media_type: str, season: int = 0, limit: int = 20
    ) -> List[Dict[str, Any]]:
        """查询资源并转换为 P115SubSearch 统一格式（仅 115 分享链接）。

        统一格式与 PanSou / Nullbr 一致：
            {"url": "...", "title": "...", "update_time": ""}
        """
        detail = self.resource_detail(tmdb_id, media_type, season)
        resource = detail.get("resource") or {}
        shares = detail.get("shares") or []
        results: List[Dict[str, Any]] = []
        for share in shares:
            converted = self._normalize_share(share, resource)
            if converted:
                results.append(converted)
            if len(results) >= max(1, int(limit or 20)):
                break
        return results

    def unlock_share(self, share_id: int, resource_id: int = 0) -> Dict[str, Any]:
        """积分解锁单个分享，返回含真实链接与实扣积分的响应。

        Dian115 的解锁接口为 ``POST /api/portal/unlock``，
        请求体 ``{"share_id": N}``（可选 ``resource_id``）。

        :return: ``{"url": <115 链接>, "actual_points": <服务端实扣积分>,
                   "already": <是否为历史已解锁>, "raw": <原始响应>}``
        """
        normalized_share_id = int(share_id or 0)
        if normalized_share_id <= 0:
            raise Dian115Error("Dian115 分享 ID 无效", code="invalid_share_id")
        body: Dict[str, Any] = {"share_id": normalized_share_id}
        if int(resource_id or 0):
            body["resource_id"] = int(resource_id)
        logger.debug(f"Dian115 申请积分解锁：share_id={normalized_share_id}")
        payload = self._request_json(
            "POST",
            "/api/portal/unlock",
            share_path(normalized_share_id),
            json=body,
        )
        unlock = payload.get("unlock") or {}
        if not isinstance(unlock, dict):
            unlock = {}
        raw_data = unlock.get("payload") or payload.get("payload") or {}
        if not isinstance(raw_data, dict):
            raw_data = {}
        already = bool(
            payload.get("already")
            or payload.get("owner")
            or unlock.get("already")
            or unlock.get("is_unlocked")
        )
        url = self._share_url(raw_data) or self._share_url(unlock) or self._share_url(payload)
        try:
            actual_points = 0 if already else max(0, int(unlock.get("cost_points") or 0))
        except (TypeError, ValueError):
            actual_points = 0
        logger.debug(
            f"Dian115 解锁完成：share_id={normalized_share_id}，"
            f"actual_points={actual_points}，already={already}，有链接={bool(url)}"
        )
        return {
            "url": url,
            "actual_points": actual_points,
            "already": already,
            "raw": payload,
        }

    @staticmethod
    def _share_url(payload: Dict[str, Any]) -> str:
        """从任意层级的响应片中提取 115 分享链接（兼容多种字段命名）。"""
        if not isinstance(payload, dict):
            return ""
        keys = ("url", "share_url", "full_url", "link", "share_link")
        # 先找真正的 115 分享链接；找不到再回退离线链接（magnet/ed2k），
        # 由上层按链接类型决定转存方式（云下载）
        for key in keys:
            value = str(payload.get(key) or "").strip()
            if value and is_115_share_url(value):
                return value
        for key in keys:
            value = str(payload.get(key) or "").strip()
            if value:
                return value
        share_code = str(payload.get("share_code") or "").strip()
        if share_code:
            url = share_url_from_codes(share_code, payload.get("receive_code"))
            return url if is_115_share_url(url) else ""
        return ""

    @staticmethod
    def _normalize_share(
            share: Dict[str, Any], resource: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """把单个分享条目转为统一格式。

        有直接链接（免费/已解锁）→ 返回可转存的条目；
        需要积分解锁 → 返回 ``need_unlock=True`` 的占位条目，
        由上层按预算决定是否真正调用 :meth:`unlock_share` 扣分。

        只保留 115 分享链接（ed2k / magnet 一律丢弃）。
        """
        if not isinstance(share, dict):
            return None
        if str(share.get("status") or "active").strip().lower() != "active":
            return None
        share_id = 0
        try:
            share_id = int(share.get("id") or 0)
        except (TypeError, ValueError):
            share_id = 0
        unlock_cost = 0
        try:
            unlock_cost = max(0, int(share.get("unlock_cost") or 0))
        except (TypeError, ValueError):
            unlock_cost = 0
        is_unlocked = bool(share.get("is_unlocked"))
        url = share_url_from_codes(
            share.get("share_code"), share.get("receive_code")
        )
        if url and not is_115_share_url(url):
            url = ""
        title = str(
            share.get("offline_title")
            or share.get("title_override")
            or share.get("file_name")
            or share.get("resource_title")
            or resource.get("title")
            or ""
        ).strip()
        update_time = str(
            share.get("update_time")
            or share.get("created_at")
            or ""
        )
        if url and (unlock_cost <= 0 or is_unlocked):
            return {
                "url": url,
                "title": title,
                "update_time": update_time,
            }
        # 无直接链接：仅当存在有效分享 ID 且需要积分解锁时才交给上层决策
        if share_id > 0 and unlock_cost > 0 and not is_unlocked:
            return {
                "url": "",
                "title": title,
                "update_time": update_time,
                "share_id": share_id,
                "resource_ref": str(share_id),
                "need_unlock": True,
                "unlock_points": unlock_cost,
            }
        return None
