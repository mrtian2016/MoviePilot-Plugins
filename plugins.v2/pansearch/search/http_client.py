"""搜索请求共享门控与节流器。"""

import random
import re
import threading
import time
from collections import deque
from contextlib import contextmanager
from functools import partial
from typing import Any, Callable, Dict, Optional
from app.log import logger
from ..utils.http_client import (
    build_proxy_url,
    normalize_proxies,
    normalize_proxy_address,
    proxy_server,
    request_error_summary,
    requests,
    validate_proxy_address,
)

TRANSIENT_REQUEST_EXCEPTIONS = (
    requests.exceptions.Timeout,
    requests.exceptions.ConnectionError,
    requests.exceptions.SSLError,
)

def gated_request(
        gate: "RequestGate", requester: Callable, *args,
        retry_exceptions: tuple[type[BaseException], ...] = (),
        max_retries: int = 0,
        initial_delay: float = 0.5,
        backoff_factor: float = 2.0,
        on_retry: Optional[Callable[[BaseException, int], None]] = None,
        **kwargs,
):
    """通过共享门控执行请求，并对指定瞬态异常做指数退避。"""
    exceptions = retry_exceptions
    retries = max(0, min(int(max_retries or 0), 3))
    delay = max(0.1, min(float(initial_delay or 0.5), 10.0))
    factor = max(1.0, min(float(backoff_factor or 2.0), 4.0))
    for attempt in range(retries + 1):
        try:
            return gate.run(partial(requester, *args, **kwargs))
        except exceptions as error:
            if attempt >= retries:
                raise
            logger.debug(
                f"{gate.name} 请求异常：{request_error_summary(error)}，"
                f"{delay:.2f} 秒后第 {attempt + 1} 次重试"
            )
            if on_retry:
                on_retry(error, attempt + 1)
            time.sleep(delay)
            delay = min(delay * factor, 30.0)


def gated_idempotent_request(
        gate: "RequestGate", requester: Callable, method: str, *args,
        retry_delay: float = 0.75,
        retry_connection_errors: bool = True,
        on_retry: Optional[Callable[[BaseException, int], None]] = None,
        **kwargs,
):
    """幂等请求仅对连接类异常执行指数退避重试。"""
    normalized_method = str(method or "").strip().upper()
    should_retry = retry_connection_errors and normalized_method in {"GET", "HEAD"}
    return gated_request(
        gate, requester, normalized_method, *args,
        retry_exceptions=TRANSIENT_REQUEST_EXCEPTIONS if should_retry else (),
        max_retries=2 if should_retry else 0,
        initial_delay=max(0.1, float(retry_delay or 0.75)),
        backoff_factor=2.0,
        on_retry=on_retry,
        **kwargs,
    )


class RequestGateCancelled(RuntimeError):
    """请求在等待限速槽位时收到停止信号。"""


class RequestGateCooldown(RuntimeError):
    """调用方要求冷却期快速失败。"""

    def __init__(self, remaining: float, status: int = 0):
        super().__init__(f"请求渠道冷却中，剩余 {remaining:.1f} 秒")
        self.remaining = max(0.0, float(remaining or 0.0))
        self.status = int(status or 0)


class RequestGate:
    """串行协调登录、挑战和业务接口的请求间隔及风控冷却。"""

    # 所有搜索渠道的登录、列表、详情和解锁接口共用此最低间隔。
    _GLOBAL_MINIMUM_INTERVAL = 1.0
    _SHARED_LOCK = threading.RLock()
    _SHARED_GATES: Dict[tuple, "RequestGate"] = {}

    @classmethod
    def shared(cls, name: str, identity: Any, **kwargs) -> "RequestGate":
        """按渠道身份复用门控，避免客户端重建后瞬间打满接口。"""
        config = (
            round(float(kwargs.get("request_interval", 0.2) or 0.2), 3),
            round(float(kwargs.get("minimum_interval", 0.2) or 0.2), 3),
            int(kwargs.get("risk_cooldown_seconds", 60) or 60),
            int(kwargs.get("server_error_cooldown_seconds", 5) or 5),
            bool(kwargs.get("serial_requests", True)),
            int(kwargs.get("max_requests_per_window", 0) or 0),
            round(float(kwargs.get("request_window_seconds", 60.0) or 60.0), 3),
        )
        key = (str(name or "搜索接口"), str(identity or "default"), config)
        with cls._SHARED_LOCK:
            gate = cls._SHARED_GATES.get(key)
            if gate is None:
                gate = cls(name, **kwargs)
                cls._SHARED_GATES[key] = gate
                if len(cls._SHARED_GATES) > 256:
                    cls._SHARED_GATES.pop(next(iter(cls._SHARED_GATES)))
            return gate

    def __init__(
            self,
            name: str,
            request_interval: float,
            minimum_interval: float = 0.2,
            risk_cooldown_seconds: int = 60,
            server_error_cooldown_seconds: int = 5,
            challenge_detector: Optional[Callable] = None,
            serial_requests: bool = True,
            max_requests_per_window: int = 0,
            request_window_seconds: float = 60.0,
    ):
        self._name = str(name or "搜索接口")
        self._request_interval = max(
            self._GLOBAL_MINIMUM_INTERVAL,
            minimum_interval,
            min(float(request_interval or minimum_interval), 10.0),
        )
        self._risk_cooldown_seconds = max(1, int(risk_cooldown_seconds or 60))
        self._server_error_cooldown_seconds = max(
            1, int(server_error_cooldown_seconds or 5)
        )
        self._challenge_detector = challenge_detector
        self._serial_requests = bool(serial_requests)
        self._max_requests_per_window = max(0, int(max_requests_per_window or 0))
        self._request_window_seconds = max(1.0, float(request_window_seconds or 60.0))
        self._request_history = deque()
        self._last_request_at = 0.0
        self._cooldown_until = 0.0
        self._cooldown_status = 0
        self._lock = threading.RLock()
        self._sequence_local = threading.local()

    @property
    def request_interval(self) -> float:
        return self._request_interval

    @property
    def name(self) -> str:
        return self._name

    @property
    def cooldown_remaining(self) -> float:
        """返回当前冷却剩余秒数，供调用方决定等待或快速失败。"""
        with self._lock:
            return max(0.0, self._cooldown_until - time.monotonic())

    @property
    def cooldown_status(self) -> int:
        with self._lock:
            return self._cooldown_status if self._cooldown_until > time.monotonic() else 0

    def run(self, request: Callable):
        """按串行配置执行请求；非串行模式只锁定限速槽分配。"""
        queued_at = time.monotonic()
        if not self._serial_requests:
            self._reserve_request_slot()
            requested_at = time.monotonic()
            try:
                response = request()
                with self._lock:
                    self._apply_cooldown(response)
                return response
            finally:
                self._log_timing(queued_at, requested_at)
        with self._lock:
            self._wait_for_slot_locked(
                cancel_check=getattr(
                    self._sequence_local, "cancel_check", None
                ),
                fail_on_cooldown=bool(getattr(
                    self._sequence_local, "fail_on_cooldown", False
                )),
            )
            requested_at = time.monotonic()
            try:
                return self._run_request_locked(request)
            finally:
                self._log_timing(queued_at, requested_at)

    def _log_timing(self, queued_at: float, requested_at: float) -> None:
        """拆分门控排队与网络耗时，便于定位渠道慢请求。"""
        completed_at = time.monotonic()
        queue_seconds = max(0.0, requested_at - queued_at)
        request_seconds = max(0.0, completed_at - requested_at)
        if queue_seconds < 0.1 and request_seconds < 0.5:
            return
        logger.debug(
            f"{self._name} 请求阶段耗时：排队={queue_seconds:.2f}s，"
            f"网络={request_seconds:.2f}s"
        )

    def _run_request_locked(self, request: Callable):
        self._record_request_locked()
        try:
            response = request()
            self._apply_cooldown(response)
            return response
        finally:
            self._last_request_at = time.monotonic()

    @contextmanager
    def immediate_sequence(
            self,
            request_count: int = 1,
            cancel_check: Optional[Callable[[], bool]] = None,
            fail_on_cooldown: bool = False,
    ):
        """串行执行强关联请求，跳过链内普通间隔但保留窗口限流。"""
        with self._lock:
            previous_cancel = getattr(
                self._sequence_local, "cancel_check", None
            )
            previous_fail = bool(getattr(
                self._sequence_local, "fail_on_cooldown", False
            ))
            previous_skip = bool(getattr(
                self._sequence_local, "skip_interval", False
            ))
            if previous_skip:
                # 外层协议链已经独占门控；嵌套链只让真实请求逐次计数，
                # 避免登录刷新或验证码重试重复预留窗口容量。
                yield
                return
            self._wait_for_slot_locked(
                required_slots=request_count,
                cancel_check=cancel_check,
                fail_on_cooldown=fail_on_cooldown,
            )
            self._sequence_local.cancel_check = cancel_check
            self._sequence_local.fail_on_cooldown = fail_on_cooldown
            self._sequence_local.skip_interval = True
            try:
                yield
            finally:
                self._sequence_local.cancel_check = previous_cancel
                self._sequence_local.fail_on_cooldown = previous_fail
                self._sequence_local.skip_interval = previous_skip

    def _reserve_request_slot(self) -> None:
        with self._lock:
            self._wait_for_slot_locked()
            self._record_request_locked()
            self._last_request_at = time.monotonic()

    def _wait_for_slot_locked(
            self,
            required_slots: int = 1,
            cancel_check: Optional[Callable[[], bool]] = None,
            fail_on_cooldown: bool = False,
    ) -> None:
        now = time.monotonic()
        self._prune_request_history_locked(now)
        window_wait = 0.0
        required = max(1, int(required_slots or 1))
        if (
                self._max_requests_per_window
                and required > self._max_requests_per_window
        ):
            raise ValueError("连续请求数不能超过限流窗口容量")
        overflow = (
            len(self._request_history) + required - self._max_requests_per_window
            if self._max_requests_per_window else 0
        )
        if overflow > 0:
            expiry_index = min(overflow - 1, len(self._request_history) - 1)
            window_wait = max(
                self._request_window_seconds
                - (now - self._request_history[expiry_index]),
                0.0,
            )
        cooldown_wait = max(self._cooldown_until - now, 0.0)
        if fail_on_cooldown and cooldown_wait > 0:
            raise RequestGateCooldown(cooldown_wait, self._cooldown_status)
        interval_wait = 0.0
        if not getattr(self._sequence_local, "skip_interval", False):
            interval_wait = (
                    self._request_interval * random.uniform(1.0, 1.25)
                    - (now - self._last_request_at)
            )
        wait_seconds = max(cooldown_wait, window_wait, interval_wait, 0.0)
        deadline = time.monotonic() + wait_seconds
        while wait_seconds > 0:
            if cancel_check and cancel_check():
                raise RequestGateCancelled("请求等待已停止")
            time.sleep(min(wait_seconds, 0.25))
            wait_seconds = deadline - time.monotonic()

    def _prune_request_history_locked(self, now: float) -> None:
        while (
                self._request_history
                and now - self._request_history[0] >= self._request_window_seconds
        ):
            self._request_history.popleft()

    def _record_request_locked(self) -> None:
        if not self._max_requests_per_window:
            return
        now = time.monotonic()
        self._prune_request_history_locked(now)
        self._request_history.append(now)

    def activate_cooldown(
            self, seconds: int, status: int = 0, reason: str = "风险保护"
    ) -> None:
        """由协议层识别到软风控信号时主动开启共享冷却。"""
        normalized_seconds = max(1, min(int(seconds or 1), 10 * 60))
        with self._lock:
            cooldown_until = time.monotonic() + normalized_seconds
            if cooldown_until >= self._cooldown_until:
                self._cooldown_until = cooldown_until
                self._cooldown_status = int(status or 0)
        logger.warning(
            f"{self._name} 触发{reason}，冷却 {normalized_seconds} 秒"
        )

    def _apply_cooldown(self, response) -> None:
        status = int(getattr(response, "status_code", 0) or 0)
        if status not in {403, 429, 500, 502, 503, 504}:
            return
        if status == 403:
            # 没有挑战识别器时，403 只能表示接口拒绝，不能武断地当作风控。
            # 否则普通权限/会话错误会触发全渠道 60 秒冷却并掩盖真实原因。
            if not self._challenge_detector:
                return
            try:
                if not self._challenge_detector(response):
                    return
            except Exception:
                return
        retry_after = str(response.headers.get("retry-after") or "").strip()
        try:
            seconds = max(1, min(int(float(retry_after)), 10 * 60))
        except (TypeError, ValueError):
            seconds = 0
            if status == 429:
                try:
                    body = getattr(response, "text", "") or ""
                    if not body:
                        raw = getattr(response, "content", b"")
                        body = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
                    matched = re.search(
                        r"(?:冷却|重试|限制|retry)[^0-9]{0,24}(\d+)\s*(?:秒|s|seconds?)?",
                        body[:4096],
                        re.IGNORECASE,
                    )
                    seconds = int(matched.group(1)) if matched else 0
                except (TypeError, ValueError, UnicodeError):
                    seconds = 0
            if not seconds:
                seconds = (
                    self._server_error_cooldown_seconds
                    if status >= 500 else self._risk_cooldown_seconds
                )
        cooldown_until = time.monotonic() + seconds
        if cooldown_until >= self._cooldown_until:
            self._cooldown_until = cooldown_until
            self._cooldown_status = status
        logger.warning(
            f"{self._name} 触发 HTTP {status} 风控，冷却 {seconds} 秒"
        )


class AccountActionGate:
    """按渠道账号共享受保护操作的滚动窗口和执行锁。"""

    _STATE_LOCK = threading.RLock()
    _LOCKS: Dict[str, threading.RLock] = {}
    _HISTORIES: Dict[str, deque] = {}
    _GATES: Dict[tuple, "AccountActionGate"] = {}

    @classmethod
    def shared(
            cls,
            name: str,
            account_key: str,
            max_actions: int,
            maximum_actions: int,
            window_seconds: float = 60.0,
    ) -> "AccountActionGate":
        key = (
            str(account_key or "default"),
            max(1, min(int(max_actions or 1), int(maximum_actions or 1))),
            round(float(window_seconds or 60.0), 3),
        )
        with cls._STATE_LOCK:
            gate = cls._GATES.get(key)
            if gate is None:
                gate = cls(
                    name,
                    account_key,
                    max_actions=max_actions,
                    maximum_actions=maximum_actions,
                    window_seconds=window_seconds,
                )
                cls._GATES[key] = gate
                if len(cls._GATES) > 256:
                    cls._GATES.pop(next(iter(cls._GATES)))
            return gate

    def __init__(
            self,
            name: str,
            account_key: str,
            max_actions: int,
            maximum_actions: int,
            window_seconds: float = 60.0,
    ):
        self._name = str(name or "受保护操作")
        self._account_key = str(account_key or "default")
        self._max_actions = max(
            1, min(int(max_actions or 1), int(maximum_actions or 1))
        )
        self._window_seconds = max(1.0, float(window_seconds or 60.0))

    @property
    def max_actions(self) -> int:
        return self._max_actions

    def run(self, action: Callable):
        """串行等待可用操作槽；实际发起后无论结果均计入窗口。"""
        with self._action_lock():
            self._wait_for_slot()
            try:
                return action()
            finally:
                self._record_attempt()

    def _action_lock(self) -> threading.RLock:
        with self._STATE_LOCK:
            return self._LOCKS.setdefault(
                self._account_key, threading.RLock()
            )

    def _wait_for_slot(self) -> None:
        while True:
            with self._STATE_LOCK:
                history = self._HISTORIES.setdefault(
                    self._account_key, deque()
                )
                now = time.monotonic()
                self._prune(history, now)
                wait_seconds = 0.0
                if history:
                    interval = self._window_seconds / self._max_actions
                    wait_seconds = max(interval - (now - history[-1]), 0.0)
                if len(history) >= self._max_actions:
                    wait_seconds = max(
                        wait_seconds,
                        self._window_seconds - (now - history[0]),
                    )
            if wait_seconds <= 0:
                return
            logger.debug(f"{self._name}按风控节奏等待 {wait_seconds:.1f} 秒")
            time.sleep(wait_seconds)

    def _record_attempt(self) -> None:
        with self._STATE_LOCK:
            history = self._HISTORIES.setdefault(self._account_key, deque())
            now = time.monotonic()
            self._prune(history, now)
            history.append(now)

    def _prune(self, history: deque, now: float) -> None:
        while history and now - history[0] >= self._window_seconds:
            history.popleft()
