# -*- coding: utf-8 -*-
"""
Dian115（癫影）Cloudflare Turnstile 自动解算。

背景
----
癫影站点对「邮箱 + 密码」登录与「积分解锁」两个接口都开启了 Cloudflare
Turnstile 人机验证（``GET /api/portal/auth/policy`` 返回
``{"turnstile_enabled": true, "turnstile_site_key": "0x4AAA..."}``）。
手工方案是让用户在浏览器登录后复制 ``__Host-portal_token``，但该 Cookie
``Max-Age=86400``（24 小时），过期即失效，维护成本极高。

实测结论（2026-09-13，MoviePilot v2.15.6 容器内，真实账号端到端验证通过）
-----------------------------------------------------------------------
MoviePilot 内置的 ``cloakbrowser``（反检测浏览器，基于 Playwright + 指纹
补丁）能够稳定地自动通过 Turnstile，**无需任何付费 solver 服务**：

* 不需要加载真实的 ``/login`` 页面 —— 用 ``page.route()`` 直接 fulfill
  一个仅含 Turnstile script 的最小 HTML，既快又躲开了站点自身的
  反调试脚本；
* 用 ``appearance: 'interaction-only'`` + ``execution: 'execute'`` 渲染
  隐藏式 widget，绝大多数情况无需点击即自动出 token；
* 当 Cloudflare 判定需要交互时（``before-interactive-callback`` 触发），
  遍历 frames 找到 ``challenges.cloudflare.com`` 的 iframe 并模拟
  真人鼠标点击即可；
* 实测首次解算耗时约 11 秒，同一浏览器上下文复用时 < 2 秒。

依赖策略
--------
``cloakbrowser`` 是**可选依赖**：本模块在 import 期不做任何硬依赖检查，
真正调用 ``token()`` 时才 ``import``，缺失时抛 ``Dian115TurnstileUnavailable``，
由上层（``Dian115Client``）捕获后**回退到手工 Token 路径**，保证在
非 MoviePilot 环境下插件依然能正常加载与运行。
"""

import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Optional
from urllib.parse import urlsplit

from app.log import logger

# 解算整体超时（秒）；实测首次约 11s，交互点击场景约 15~20s
_TOKEN_TIMEOUT = 30
# 单次解算的内部等待上限（秒）
_SOLVE_DEADLINE = 60


class Dian115TurnstileUnavailable(RuntimeError):
    """浏览器环境不可用（未安装 cloakbrowser / 内核缺失）。"""


class Dian115Turnstile:
    """复用轻量浏览器，仅生成 Dian115 接口使用的一次性 Turnstile token。

    浏览器上下文在多次解算之间**复用**（站点风控对同一指纹更友好，
    且省去冷启动开销）。一旦解算抛错即销毁上下文，下次重新冷启动，
    避免残留在异常状态。
    """

    _HTML = """<!doctype html><html><head><meta charset="utf-8">
    <script src="https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit" defer></script>
    </head><body><div id="verification" style="margin:80px"></div></body></html>"""

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

    _CLEAR = """() => {
        window.turnstile.remove(window.dian115Verification.widget);
        window.dian115Verification = null;
    }"""

    def __init__(self, base_url: str, proxy: Any = None):
        self._base_url = str(base_url or "").rstrip("/")
        self._proxy = proxy or None
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="Dian115-Turnstile"
        )
        self._context = None
        self._page = None

    # ------------------------------------------------------------------
    # 对外接口
    # ------------------------------------------------------------------
    def token(self, site_key: str, action: str) -> str:
        """解算一次 Turnstile，返回一次性 token。

        :raises Dian115TurnstileUnavailable: 浏览器环境缺失
        :raises TimeoutError: 解算超时
        :raises RuntimeError: Cloudflare 返回失败码
        """
        if action not in {"portal_login", "portal_unlock"} or not site_key:
            raise ValueError("Dian115 验证参数无效")
        return self._executor.submit(
            self._token, site_key, action
        ).result(timeout=_TOKEN_TIMEOUT)

    def close(self) -> None:
        """释放浏览器与线程池；可重复调用。"""
        try:
            self._executor.submit(self._close_browser).result()
        except Exception as error:  # pragma: no cover - 关闭路径不应抛出
            logger.debug(f"Dian115 关闭验证浏览器失败：{type(error).__name__}")
        finally:
            self._executor.shutdown(wait=True, cancel_futures=True)

    # ------------------------------------------------------------------
    # 内部实现
    # ------------------------------------------------------------------
    @staticmethod
    def _load_launch_context():
        """延迟导入 cloakbrowser；缺失时抛 Dian115TurnstileUnavailable。"""
        try:
            from cloakbrowser import launch_context
        except ImportError as error:
            raise Dian115TurnstileUnavailable(
                "未安装 cloakbrowser，无法自动完成 Cloudflare 验证"
            ) from error
        return launch_context

    def _prepare_page(self) -> None:
        if self._page is not None and not self._page.is_closed():
            return
        self._close_browser()
        launch_context = self._load_launch_context()
        try:
            from app.core.config import settings
            humanize = getattr(settings, "CLOAKBROWSER_HUMANIZE", True)
        except Exception:
            humanize = True
        try:
            self._context = launch_context(
                headless=True,
                proxy=self._proxy,
                humanize=humanize,
                human_preset="careful",
            )
        except Exception as error:
            # 内核缺失 / 启动失败：统一归为「环境不可用」，让上层回退手工 Token
            raise Dian115TurnstileUnavailable(
                f"浏览器启动失败：{type(error).__name__}: {error}"
            ) from error
        self._page = self._context.new_page()
        url = f"{self._base_url}/login"
        # 拦截真实页面：只喂一个最小 Turnstile 宿主页，绕开站点反调试与
        # 无关资源加载，同时把解算耗时压到最低。
        self._page.route(
            url,
            lambda route: route.fulfill(
                status=200, content_type="text/html", body=self._HTML
            ),
        )
        self._page.goto(url, wait_until="domcontentloaded", timeout=30000)
        self._page.wait_for_function(
            "() => typeof window.turnstile?.render === 'function'", timeout=30000
        )

    def _click_challenge(self) -> bool:
        """遍历 frames 找到 Turnstile 交互框并模拟点击。

        :return: 本次是否成功点击
        """
        for frame in self._page.frames:
            try:
                if urlsplit(frame.url).hostname != "challenges.cloudflare.com":
                    continue
                element = frame.frame_element()
                if not element.is_visible():
                    continue
                box = element.bounding_box()
            except Exception:
                continue
            if box and box["width"] >= 60 and box["height"] >= 30:
                # 点击框内偏左位置（避开右侧 Cloudflare 徽标区域）
                self._page.mouse.click(
                    box["x"] + 30, box["y"] + box["height"] / 2
                )
                return True
        return False

    def _token(self, site_key: str, action: str) -> str:
        started = time.monotonic()
        deadline = started + _SOLVE_DEADLINE
        reused = self._page is not None and not self._page.is_closed()
        try:
            self._prepare_page()
            self._page.evaluate(
                self._START, {"siteKey": site_key, "action": action}
            )
            clicked = False
            while time.monotonic() < deadline:
                state = self._page.evaluate("() => window.dian115Verification")
                if not isinstance(state, dict):
                    state = {}
                if state.get("token"):
                    token = str(state["token"])
                    try:
                        self._page.evaluate(self._CLEAR)
                    except Exception as error:
                        logger.debug(
                            f"Dian115 清理 Turnstile widget 失败：{type(error).__name__}"
                        )
                    logger.debug(
                        f"Dian115 Turnstile 就绪：action={action}，复用={reused}，"
                        f"耗时={time.monotonic() - started:.2f}s"
                    )
                    return token
                if state.get("error"):
                    raise RuntimeError(f"Cloudflare 验证失败：{state['error']}")
                if state.get("interactive") and not clicked:
                    clicked = self._click_challenge()
                self._page.wait_for_timeout(200)
            raise TimeoutError("Cloudflare 验证超过 60 秒")
        except Exception:
            # 任何异常都销毁上下文：避免带着可能被 CF 标记的指纹继续复用
            try:
                self._close_browser()
            except Exception as error:
                logger.debug(f"Dian115 关闭验证浏览器失败：{type(error).__name__}")
            raise

    def _close_browser(self) -> None:
        context, self._context = self._context, None
        self._page = None
        if context is not None:
            try:
                context.close()
            except Exception as error:  # pragma: no cover
                logger.debug(f"Dian115 关闭浏览器上下文失败：{type(error).__name__}")


__all__ = ["Dian115Turnstile", "Dian115TurnstileUnavailable"]
