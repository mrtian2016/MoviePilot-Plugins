"""HDHive Next.js Server Action 发现、提交与响应协议。"""

import base64
import json
import re
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Optional, Pattern, Tuple
from urllib.parse import urljoin, urlsplit

from .parser import decode_embedded_text, response_body, response_text

SCRIPT_SRC_RE = re.compile(
    r'<script\b[^>]*\bsrc=["\']([^"\']+)["\']', re.I
)
FLIGHT_LINE_RE = re.compile(r"^[0-9a-f]+:(.*)$", re.I)
SERVER_ACTION_TOKEN_COOKIE = "hdh_sa_token"
RESOURCE_PAGE_CHUNK_RE = re.compile(
    r"static/chunks/app/\(no-layout\)/resource/[^\"']+/page-[A-Za-z0-9]+\.js"
)
HONEYPOT_TOKEN_RE = re.compile(
    r'"honeypotToken"\s*:\s*("(?:\\.|[^"\\])*")'
)
LOGIN_CHUNK_RE = re.compile(
    r"static/chunks/app/\(auth\)/login/page-[^\\\"']+\.js"
)
LOGIN_ACTION_RE = re.compile(
    r"createServerReference\)\(\"([0-9a-f]{40,64})\".{0,200}?\"login\"",
    re.S,
)
CHECKIN_CHUNK_RE = re.compile(
    r"static/chunks/app/\(app\)/layout-[^\\\"']+\.js"
)
CHECKIN_ACTION_RE = re.compile(
    r"createServerReference\)\(\"([0-9a-f]{40,64})\".{0,300}?\"checkIn\"",
    re.S,
)
UNLOCK_ACTION_RE = re.compile(
    r"createServerReference\)\(\"([0-9a-f]{40,64})\""
    r".{0,240}?\"unlockResource\"",
    re.S,
)
SERVER_ACTION_REDIRECT_RE = re.compile(
    r"NEXT_REDIRECT;(?:replace|push);([^;\r\n]+);(?:\d+);",
    re.I,
)
BIND_SECRET_RE = re.compile(
    r'[\\"]bindSecret[\\"]\s*:\s*[\\"]([^\\"]+)', re.I
)
LOGIN_ACTION_FALLBACK = "602b98dd108c13ebbb69c9e649267ed988f9c3ce8e"
CHECKIN_ACTION_FALLBACK = "4068b21f57fce3dc23dca0ca104769e9e42c011d22"
ACTION_CACHE_TTL = 60 * 60
LOGIN_ROUTER_STATE_TREE = (
    "%5B%22%22%2C%7B%22children%22%3A%5B%22(auth)%22%2C%7B%22children%22%3A"
    "%5B%22login%22%2C%7B%22children%22%3A%5B%22__PAGE__%22%2C%7B%7D%2Cnull"
    "%2Cnull%5D%7D%2Cnull%2Cnull%5D%7D%2Cnull%2Cnull%5D%7D%2Cnull%2Cnull%2C"
    "true%5D"
)
CHECKIN_ROUTER_STATE_TREE = (
    "%5B%22%22%2C%7B%22children%22%3A%5B%22(app)%22%2C%7B%22children%22%3A"
    "%5B%22__PAGE__%22%2C%7B%7D%2Cnull%2Cnull%5D%7D%2Cnull%2Cnull%5D%7D%2C"
    "null%2Cnull%2Ctrue%5D"
)


class ServerActionTokenError(RuntimeError):
    """目标页面未签发 Server Action 短期令牌。"""

    def __init__(self, message: str, status_code: int = 0):
        super().__init__(message)
        self.code = "action_token_required"
        self.status_code = int(status_code or 0)


@dataclass(frozen=True)
class ServerActionResponse:
    """统一封装 Server Action 的 HTTP 响应和业务负载。"""

    status_code: int
    body: bytes
    text: str
    payload: Optional[Dict[str, Any]]

    @property
    def data(self) -> Dict[str, Any]:
        value = self.payload.get("data") if isinstance(self.payload, dict) else None
        return value if isinstance(value, dict) else {}

    @property
    def success(self) -> bool:
        return bool(self.payload and self.payload.get("success"))

    @property
    def code(self) -> str:
        return server_action_code(self.payload)

    @property
    def message(self) -> str:
        return server_action_message(self.payload)

    @property
    def redirect_url(self) -> str:
        """读取 Next.js Action 成功时通过 RSC 返回的跳转地址。"""
        return server_action_redirect_url(self.text)


def server_action_headers(
        action_id: str,
        *,
        referer: str,
        router_state: str = "",
        next_url: str = "",
        no_cache: bool = True,
) -> Dict[str, str]:
    """构造登录、签到、解锁和验证码共用的 Action 请求头。"""
    headers = {
        "accept": "text/x-component",
        "content-type": "text/plain;charset=UTF-8",
        "next-action": str(action_id or ""),
        "origin": "https://re0.me",
        "referer": str(referer or "https://re0.me/"),
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
    }
    if router_state:
        headers["next-router-state-tree"] = router_state
    if next_url:
        headers["next-url"] = next_url
    if no_cache:
        headers.update({"cache-control": "no-cache", "pragma": "no-cache"})
    return headers


def server_action_body(arguments: list) -> str:
    """按站点前端格式序列化 Action 参数。"""
    return json.dumps(arguments, ensure_ascii=False, separators=(",", ":"))


def clear_server_action_token(cookies: Any) -> None:
    """删除所有 domain/path 下的短期 Action Cookie，避免发送同名旧值。"""
    try:
        cookies.delete(SERVER_ACTION_TOKEN_COOKIE)
    except (AttributeError, KeyError):
        return


def server_action_token(cookies: Any) -> str:
    """读取页面预检产生的短期 Action Cookie。"""
    try:
        return str(
            cookies.get_dict().get(SERVER_ACTION_TOKEN_COOKIE) or ""
        ).strip()
    except (AttributeError, KeyError, ValueError):
        return ""


class ServerActionProtocol:
    """统一管理 HDHive Server Action 的 Cookie、预检、提交和返回。"""

    def __init__(
            self,
            cookies: Any,
            error_factory: Optional[Callable[..., Exception]] = None,
            warning: Optional[Callable[[str], None]] = None,
    ):
        self._cookies = cookies
        self._error_factory = error_factory
        self._warning = warning
        self._action_cache: Dict[str, Tuple[str, float]] = {}

    def _error(self, message: str, code: str, status_code: int = 0) -> Exception:
        if self._error_factory:
            return self._error_factory(
                message,
                code=code,
                status_code=status_code,
            )
        if code == "action_token_required":
            return ServerActionTokenError(message, status_code=status_code)
        return RuntimeError(message)

    def _raise(self, message: str, code: str, status_code: int = 0) -> None:
        raise self._error(message, code=code, status_code=status_code)

    @staticmethod
    def is_persistent_cookie(name: str) -> bool:
        return str(name or "") != SERVER_ACTION_TOKEN_COOKIE

    @staticmethod
    def bind_secret(response: ServerActionResponse) -> str:
        match = BIND_SECRET_RE.search(response.text)
        return decode_embedded_text(match.group(1)) if match else ""

    def preflight(
            self,
            request: Callable[..., Any],
            path: str,
            require_token: bool = True,
            **kwargs,
    ) -> Any:
        """清除旧 Action Cookie，并通过目标页面 GET 获取新值。

        普通首页签到由当前 Next.js 页面 Action 直接承载，不依赖资源解锁
        使用的短期令牌；登录、验证码和资源解锁仍保持令牌校验。
        """
        clear_server_action_token(self._cookies)
        response = request("GET", path, **kwargs)
        if not require_token or server_action_token(self._cookies):
            return response
        status_code = int(getattr(response, "status_code", 0) or 0)
        self._raise(
            "HDHive 页面预检未设置 hdh_sa_token",
            code="action_token_required",
            status_code=status_code,
        )

    def action_id_from_scripts(
            self,
            request: Callable[..., Any],
            script_paths: Iterable[str],
            *,
            action_name: str,
            action_pattern: Pattern[str],
            fallback: str = "",
    ) -> str:
        """从当前页面发布的前端模块发现 Server Action ID。"""
        errors = []
        for path in script_paths:
            try:
                response = request("GET", path)
            except RuntimeError as error:
                errors.append(str(error))
                continue
            match = action_pattern.search(response_text(response))
            if match:
                return match.group(1)
        if fallback:
            if self._warning:
                self._warning(
                    f"HDHive 未能动态发现 {action_name} Server Action，使用兜底 ID"
                )
            return fallback
        detail = errors[-1] if errors else "页面未返回匹配的前端模块"
        self._raise(
            f"HDHive {action_name} Server Action 未找到：{detail}",
            code="schema_changed",
        )

    def discover(
            self,
            request: Callable[..., Any],
            page_response: Any,
            *,
            action_name: str,
            preferred_path: str,
            action_pattern: Pattern[str],
            fallback: str = "",
            chunk_patterns: Iterable[Pattern[str]] = (),
    ) -> str:
        page_url = str(
            getattr(page_response, "url", "") or "https://re0.me/"
        )
        return self.action_id_from_scripts(
            request,
            server_action_script_paths(
                response_text(page_response),
                page_url,
                preferred_path=preferred_path,
                chunk_patterns=chunk_patterns,
            ),
            action_name=action_name,
            action_pattern=action_pattern,
            fallback=fallback,
        )

    def _cached_action(
            self,
            cache_key: str,
            discover: Callable[[], str],
            *,
            force: bool = False,
    ) -> str:
        now = time.monotonic()
        cached = self._action_cache.get(cache_key)
        if not force and cached and cached[1] > now:
            return cached[0]
        action_id = discover()
        self._action_cache[cache_key] = (
            action_id, now + ACTION_CACHE_TTL
        )
        return action_id

    def invalidate(self, cache_key: str) -> None:
        self._action_cache.pop(str(cache_key or ""), None)

    def login(
            self,
            request: Callable[..., Any],
            username: str,
            password: str,
            *,
            base_url: str = "https://re0.me",
            refresh_action: bool = False,
    ) -> ServerActionResponse:
        """预检登录页、发现并提交登录 Action。"""
        page_response = self.preflight(request, "/login")
        status_code = int(getattr(page_response, "status_code", 0) or 0)
        if status_code != 200:
            self._raise(
                f"HDHive 登录页请求失败（HTTP {status_code}）",
                code="login_page_failed",
                status_code=status_code,
            )
        action_id = self._cached_action(
            "login",
            lambda: self.discover(
                request,
                page_response,
                action_name="登录",
                preferred_path="/app/(auth)/login/page-",
                action_pattern=LOGIN_ACTION_RE,
                fallback=LOGIN_ACTION_FALLBACK,
                chunk_patterns=(LOGIN_CHUNK_RE,),
            ),
            force=refresh_action,
        )
        encoded_password = base64.b64encode(
            str(password or "").encode("utf-8")
        ).decode("ascii")
        response = self.post(
            request,
            "/login",
            action_id,
            [{
                "username": str(username or "").strip(),
                "password": encoded_password,
                "password_transport": "base64",
            }, "/"],
            referer=f"{base_url.rstrip('/')}/login",
            router_state=LOGIN_ROUTER_STATE_TREE,
        )
        if response.status_code == 404 and not refresh_action:
            self.invalidate("login")
            return self.login(
                request,
                username,
                password,
                base_url=base_url,
                refresh_action=True,
            )
        return response

    def checkin(
            self,
            request: Callable[..., Any],
            is_gambler: bool,
            *,
            base_url: str = "https://re0.me",
            refresh_action: bool = False,
            retry_token: bool = True,
            on_page: Optional[Callable[[Any], None]] = None,
    ) -> ServerActionResponse:
        """预检首页、发现并提交签到 Action。"""
        page_response = self.preflight(request, "/", require_token=False)
        if on_page:
            on_page(page_response)
        action_id = self._cached_action(
            "checkin",
            lambda: self.discover(
                request,
                page_response,
                action_name="签到",
                preferred_path="/app/(app)/layout-",
                action_pattern=CHECKIN_ACTION_RE,
                fallback=CHECKIN_ACTION_FALLBACK,
                chunk_patterns=(CHECKIN_CHUNK_RE,),
            ),
            force=refresh_action,
        )
        try:
            response = self.post(
                request,
                "/",
                action_id,
                [bool(is_gambler)],
                referer=f"{base_url.rstrip('/')}/",
                router_state=CHECKIN_ROUTER_STATE_TREE,
            )
        except Exception as error:
            if (
                    int(getattr(error, "status_code", 0) or 0) == 404
                    and not refresh_action
            ):
                self.invalidate("checkin")
                return self.checkin(
                    request,
                    is_gambler,
                    base_url=base_url,
                    refresh_action=True,
                    retry_token=retry_token,
                    on_page=on_page,
                )
            raise
        if response.status_code == 404 and not refresh_action:
            self.invalidate("checkin")
            return self.checkin(
                request,
                is_gambler,
                base_url=base_url,
                refresh_action=True,
                retry_token=retry_token,
                on_page=on_page,
            )
        if response.payload is None:
            self._raise(
                "HDHive 签到 Server Action 响应格式异常",
                code="schema_changed",
                status_code=response.status_code,
            )
        if response.code == "action_token_required" and retry_token:
            return self.checkin(
                request,
                is_gambler,
                base_url=base_url,
                retry_token=False,
                on_page=on_page,
            )
        return response

    def unlock(
            self,
            request: Callable[..., Any],
            resource_page_path: str,
            slug: str,
            *,
            page_headers: Optional[Dict[str, str]] = None,
            base_url: str = "https://re0.me",
            on_submit: Optional[Callable[[], None]] = None,
    ) -> Tuple[Any, ServerActionResponse, str, str]:
        """预检详情页、读取蜜罐证明并提交解锁 Action。"""
        page_response = self.preflight(
            request, resource_page_path, headers=page_headers or {}
        )
        honeypot_token, chunk = self.resource_context(page_response)
        page_url = str(
            getattr(page_response, "url", "") or f"{base_url.rstrip('/')}/"
        )
        action_id = self._cached_action(
            f"unlock:{chunk}",
            lambda: self.action_id_from_scripts(
                request,
                server_action_script_paths(
                    response_text(page_response),
                    page_url,
                    preferred_path=chunk,
                    prefer_related=True,
                ),
                action_name="解锁",
                action_pattern=UNLOCK_ACTION_RE,
            ),
        )
        if on_submit:
            on_submit()
        response = self.post(
            request,
            resource_page_path,
            action_id,
            [str(slug or "").strip(), honeypot_token],
            referer=f"{base_url.rstrip('/')}{resource_page_path}",
            next_url=resource_page_path,
        )
        return page_response, response, honeypot_token, chunk

    def resource_context(self, page_response: Any) -> tuple[str, str]:
        """从资源详情页读取解锁 Action 所需的蜜罐字段和客户端模块。"""
        text = decode_embedded_text(response_text(page_response))
        token_match = HONEYPOT_TOKEN_RE.search(text)
        chunk_match = RESOURCE_PAGE_CHUNK_RE.search(text)
        if not token_match:
            self._raise(
                "HDHive 资源页未返回 honeypotToken 字段",
                code="action_proof_missing",
            )
        if not chunk_match:
            self._raise(
                "HDHive 资源页未返回解锁客户端模块",
                code="schema_changed",
            )
        try:
            token = str(json.loads(token_match.group(1)) or "")
        except (TypeError, ValueError) as error:
            raise self._error(
                "HDHive honeypotToken 字段格式异常",
                code="action_proof_invalid",
            ) from error
        return token, chunk_match.group(0)

    @staticmethod
    def post(
            request: Callable[..., Any],
            path: str,
            action_id: str,
            arguments: list,
            *,
            referer: str,
            router_state: str = "",
            next_url: str = "",
            no_cache: bool = True,
    ) -> "ServerActionResponse":
        return post_server_action(
            request,
            path,
            action_id,
            arguments,
            referer=referer,
            router_state=router_state,
            next_url=next_url,
            no_cache=no_cache,
        )


def server_action_script_paths(
        page_text: str,
        page_url: str,
        *,
        preferred_path: str = "",
        chunk_patterns: Iterable[Pattern[str]] = (),
        prefer_related: bool = False,
) -> List[str]:
    """从脚本标签和 RSC 数据中提取、去重并排序同源 JS 路径。"""
    origin = urlsplit(page_url).netloc.lower()
    paths = []
    for raw_url in SCRIPT_SRC_RE.findall(page_text or ""):
        parsed = urlsplit(urljoin(page_url, decode_embedded_text(raw_url)))
        if parsed.netloc.lower() == origin and parsed.path.endswith(".js"):
            paths.append(parsed.path + (
                f"?{parsed.query}" if parsed.query else ""
            ))
    for pattern in chunk_patterns:
        for chunk in pattern.findall(page_text or ""):
            paths.append(f"/_next/{str(chunk).lstrip('/')}")
    result = list(dict.fromkeys(paths))
    if prefer_related and preferred_path:
        preferred = f"/_next/{str(preferred_path).lstrip('/')}"
        try:
            preferred_index = result.index(preferred)
        except ValueError:
            pass
        else:
            # Next.js places route dependencies immediately before the route
            # page chunk. Try those first so action discovery remains bounded.
            result = [
                preferred,
                *reversed(result[:preferred_index]),
                *result[preferred_index + 1:],
            ]
            return result
    result.sort(key=lambda value: (
        bool(preferred_path and preferred_path not in value), value
    ))
    return result


def _json_values(text: str) -> Iterable[Any]:
    stripped = str(text or "").strip()
    if stripped:
        try:
            yield json.loads(stripped)
        except json.JSONDecodeError:
            pass
    for line in stripped.splitlines():
        match = FLIGHT_LINE_RE.match(line.strip())
        if not match:
            continue
        try:
            yield json.loads(match.group(1))
        except json.JSONDecodeError:
            continue


def _json_objects(values: Iterable[Any]) -> List[Dict[str, Any]]:
    objects = []
    pending = list(values)
    while pending:
        current = pending.pop()
        if isinstance(current, dict):
            objects.append(current)
            pending.extend(current.values())
        elif isinstance(current, list):
            pending.extend(current)
    return objects


def server_action_payload(text: str) -> Optional[Dict[str, Any]]:
    """提取 Action 业务响应；错误包装优先于内部 success 字段。"""
    objects = _json_objects(_json_values(text))
    for item in objects:
        error = item.get("error")
        if isinstance(error, dict):
            return {
                "success": False,
                "message": str(
                    error.get("message")
                    or error.get("description")
                    or "操作失败"
                ),
                "code": str(error.get("code") or ""),
                "error": error,
                "data": (
                    error.get("data")
                    if isinstance(error.get("data"), dict)
                    else {}
                ),
            }
    for item in objects:
        response = item.get("response")
        if isinstance(response, dict) and "success" in response:
            return response
    return next(
        (item for item in objects if "success" in item),
        None,
    )


def server_action_code(payload: Optional[Dict[str, Any]]) -> str:
    """读取站点不同 Action 包装中的统一错误码。"""
    if not isinstance(payload, dict):
        return ""
    error = payload.get("error")
    return str(
        payload.get("code")
        or payload.get("error_code")
        or (error.get("code") if isinstance(error, dict) else "")
        or ""
    ).strip()


def server_action_message(payload: Optional[Dict[str, Any]]) -> str:
    """读取 data、顶层或 error 中的首个业务提示。"""
    if not isinstance(payload, dict):
        return ""
    data = payload.get("data")
    error = payload.get("error")
    data_messages = (
        data.get("message") if isinstance(data, dict) else "",
        data.get("description") if isinstance(data, dict) else "",
        data.get("detail") if isinstance(data, dict) else "",
    )
    payload_messages = (
        payload.get("message"),
        payload.get("description"),
        payload.get("detail"),
    )
    error_messages = (
        error.get("message") if isinstance(error, dict) else "",
        error.get("description") if isinstance(error, dict) else "",
        error.get("detail") if isinstance(error, dict) else "",
        error if isinstance(error, str) else "",
    )
    candidates = (
        payload_messages + error_messages + data_messages
        if error
        else data_messages + payload_messages
    )
    return next((str(value).strip() for value in candidates if value), "")


def server_action_redirect_url(text: str, base_url: str = "https://re0.me") -> str:
    """解析 Action RSC 中的绝对或相对跳转地址。"""
    normalized = decode_embedded_text(text)
    match = SERVER_ACTION_REDIRECT_RE.search(normalized)
    if not match:
        return ""
    raw_url = str(match.group(1) or "").strip()
    parsed = urlsplit(raw_url)
    if parsed.scheme.lower() in {"http", "https", "ed2k", "magnet"}:
        return raw_url
    resolved = urlsplit(urljoin(f"{str(base_url).rstrip('/')}/", raw_url.lstrip("/")))
    if resolved.scheme.lower() not in {"http", "https"} or not resolved.netloc:
        return ""
    return resolved.geturl()


def server_action_response(response: Any) -> ServerActionResponse:
    """读取 HTTP 正文并解析普通 JSON 或 Flight Action 响应。"""
    body = response_body(response)
    text = response_text(response)
    return ServerActionResponse(
        status_code=int(getattr(response, "status_code", 0) or 0),
        body=body,
        text=text,
        payload=server_action_payload(text),
    )


def post_server_action(
        request: Callable[..., Any],
        path: str,
        action_id: str,
        arguments: list,
        *,
        referer: str,
        router_state: str = "",
        next_url: str = "",
        no_cache: bool = True,
) -> ServerActionResponse:
    """使用统一请求格式提交 Server Action 并解析返回。"""
    response = request(
        "POST",
        path,
        headers=server_action_headers(
            action_id,
            referer=referer,
            router_state=router_state,
            next_url=next_url,
            no_cache=no_cache,
        ),
        data=server_action_body(arguments),
    )
    return server_action_response(response)
