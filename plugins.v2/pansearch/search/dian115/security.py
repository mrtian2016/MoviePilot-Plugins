"""Dian115 安全验证与前端路由协议的统一入口。"""

import base64
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Optional
from urllib.parse import urlsplit

from app.log import logger

_KEY_VERSION = 1
_KEY_MASK = (55, 161, 92, 233)


class Dian115Turnstile:
    """复用轻量浏览器，仅生成 Dian115 接口使用的一次性 Turnstile token。"""

    _HTML = """<!doctype html><html><head><meta charset=\"utf-8\">
    <script src=\"https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit\" defer></script>
    </head><body><div id=\"verification\" style=\"margin:80px\"></div></body></html>"""
    _START = """({siteKey, action}) => {
        const previous = window.dian115Verification;
        if (previous?.widget !== undefined) window.turnstile.remove(previous.widget);
        const state = {token: '', error: '', interactive: false};
        window.dian115Verification = state;
        state.widget = window.turnstile.render('#verification', {
            sitekey: siteKey, action, theme: 'light', language: 'zh-CN',
            appearance: 'interaction-only', execution: 'execute',
            'response-field': false,
            callback: token => { state.token = token; },
            'error-callback': code => { state.error = String(code || 'verification_failed'); },
            'expired-callback': () => { state.error = 'token_expired'; },
            'timeout-callback': () => { state.error = 'verification_timeout'; },
            'before-interactive-callback': () => { state.interactive = true; }
        });
        window.turnstile.execute(state.widget);
    }"""

    def __init__(self, base_url: str, proxy=None):
        self._base_url = base_url.rstrip("/")
        self._proxy = proxy
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="Dian115-Turnstile")
        self._context = None
        self._page = None

    def token(self, site_key: str, action: str) -> str:
        if action not in {"portal_login", "portal_unlock"} or not site_key:
            raise ValueError("Dian115 验证参数无效")
        return self._executor.submit(self._token, site_key, action).result(timeout=30)

    def _prepare_page(self) -> None:
        if self._page is not None and not self._page.is_closed():
            return
        self._close_browser()
        from app.core.config import settings
        from cloakbrowser import launch_context
        self._context = launch_context(
            headless=True, proxy=self._proxy,
            humanize=getattr(settings, "CLOAKBROWSER_HUMANIZE", True),
            human_preset="careful",
        )
        self._page = self._context.new_page()
        url = f"{self._base_url}/login"
        self._page.route(url, lambda route: route.fulfill(
            status=200, content_type="text/html", body=self._HTML
        ))
        self._page.goto(url, wait_until="domcontentloaded", timeout=30000)
        self._page.wait_for_function(
            "() => typeof window.turnstile?.render === 'function'", timeout=30000
        )

    def _token(self, site_key: str, action: str) -> str:
        started = time.monotonic()
        deadline = started + 60
        reused = self._page is not None and not self._page.is_closed()
        try:
            self._prepare_page()
            self._page.evaluate(self._START, {"siteKey": site_key, "action": action})
            clicked = False
            while time.monotonic() < deadline:
                state = self._page.evaluate("() => window.dian115Verification")
                if state.get("token"):
                    token = str(state["token"])
                    self._page.evaluate("""() => {
                        window.turnstile.remove(window.dian115Verification.widget);
                        window.dian115Verification = null;
                    }""")
                    logger.debug(
                        f"Dian115 Turnstile 就绪：action={action}，复用={reused}，"
                        f"耗时={time.monotonic() - started:.2f}s"
                    )
                    return token
                if state.get("error"):
                    raise RuntimeError(f"Cloudflare 验证失败：{state['error']}")
                if state.get("interactive") and not clicked:
                    for frame in self._page.frames:
                        if urlsplit(frame.url).hostname != "challenges.cloudflare.com":
                            continue
                        try:
                            element = frame.frame_element()
                            if not element.is_visible():
                                continue
                            box = element.bounding_box()
                        except Exception:
                            continue
                        if box and box["width"] >= 60 and box["height"] >= 30:
                            self._page.mouse.click(box["x"] + 30, box["y"] + box["height"] / 2)
                            clicked = True
                            break
                self._page.wait_for_timeout(200)
            raise TimeoutError("Cloudflare 验证超过 60 秒")
        except Exception:
            try:
                self._close_browser()
            except Exception as error:
                logger.debug(f"Dian115 关闭验证浏览器失败：{type(error).__name__}")
            raise

    def _close_browser(self) -> None:
        context, self._context = self._context, None
        self._page = None
        if context is not None:
            context.close()

    def close(self) -> None:
        try:
            self._executor.submit(self._close_browser).result()
        finally:
            self._executor.shutdown(wait=True, cancel_futures=True)


def encode_resource_key(
        source: str,
        media_type: str,
        resource_id: Any,
        season: Any = 0,
) -> str:
    """编码门户资源键。"""
    raw = (
        f"{_KEY_VERSION}|{str(source or '').strip().lower()}|"
        f"{str(media_type or '').strip().lower()}|{int(resource_id)}|"
        f"{int(season or 0)}"
    ).encode("utf-8")
    encoded = bytes(value ^ _KEY_MASK[index % len(_KEY_MASK)] for index, value in enumerate(raw))
    return base64.urlsafe_b64encode(encoded).decode("ascii").rstrip("=")


def resource_path(media_type: str, tmdb_id: Any, season: Any = 0) -> str:
    return f"/r/{encode_resource_key('tmdb', media_type, tmdb_id, season)}"


def share_path(share_id: Any) -> str:
    return f"/s/{encode_resource_key('share', 'other', share_id)}"


def turnstile_token(client: Any, action: str, allow_browser: bool = True) -> Optional[str]:
    """获取登录/解锁所需的 Turnstile token。"""
    client._check_cooldown()
    cached = client._turnstile_policy
    if not cached or cached[1] <= time.monotonic():
        policy = client._request_json(
            "GET", "/api/portal/auth/policy", "/login", require_login=False
        )
        client._turnstile_policy = (policy, time.monotonic() + 300)
    else:
        policy = cached[0]
    if policy.get("turnstile_enabled") is False:
        return None
    if policy.get("turnstile_enabled") is not True:
        raise client.error_type("Dian115 未返回 Cloudflare 验证策略", code="schema_changed")
    site_key = str(policy.get("turnstile_site_key") or "").strip()
    if not site_key:
        raise client.error_type("Dian115 未返回 Cloudflare site key", code="schema_changed")
    if not allow_browser:
        raise client.error_type(
            "Dian115 登录需要 Cloudflare 验证，请先刷新账户登录状态",
            code="browser_login_forbidden",
        )
    try:
        if client._turnstile is None:
            client._turnstile = Dian115Turnstile(client.base_url, client._browser_proxy())
        token = client._turnstile.token(site_key, action)
        if not token:
            raise RuntimeError("Cloudflare 未返回验证 token")
        return token
    except ImportError as error:
        raise client.error_type(
            "Dian115 Cloudflare 验证需要 CloakBrowser 浏览器环境",
            code="browser_unavailable",
        ) from error
    except TimeoutError as error:
        raise client.error_type("Dian115 Cloudflare 验证超时", code="turnstile_timeout") from error
    except RuntimeError as error:
        raise client.error_type(str(error), code="turnstile_failed") from error
    except Exception as error:
        raise client.error_type(
            f"Dian115 Cloudflare 验证失败：{type(error).__name__}", code="turnstile_failed"
        ) from error


__all__ = [
    "Dian115Turnstile",
    "encode_resource_key",
    "resource_path",
    "share_path",
    "turnstile_token",
]
