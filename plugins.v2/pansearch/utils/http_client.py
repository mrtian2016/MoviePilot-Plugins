"""PanSearch 通用 HTTP 与代理工具。"""

from typing import Any, Dict, Optional
from urllib.parse import quote, unquote, urlsplit, urlunsplit

from curl_cffi import requests

_PROXY_SCHEMES = frozenset({"http", "https", "socks4", "socks5", "socks5h"})


def request_error_summary(error: BaseException) -> str:
    """返回不包含请求地址和凭据的网络异常摘要。"""
    name = type(error).__name__
    code = getattr(error, "code", 0)
    try:
        code_value = int(code or 0)
    except (TypeError, ValueError):
        code_value = 0
    return f"{name} (curl {code_value})" if code_value else name


def normalize_proxy_address(proxy: Any) -> str:
    value = str(proxy or "").strip()
    if not value:
        return ""
    if value.startswith("//"):
        value = f"http:{value}"
    elif "://" not in value:
        value = f"http://{value}"
    try:
        parsed = urlsplit(value)
        if parsed.scheme.lower() not in _PROXY_SCHEMES or not parsed.hostname:
            return ""
        host = parsed.hostname
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        userinfo = ""
        if parsed.username is not None:
            userinfo = quote(unquote(parsed.username), safe="")
            if parsed.password is not None:
                userinfo += f":{quote(unquote(parsed.password), safe='')}"
            userinfo += "@"
        netloc = f"{userinfo}{host}"
        if parsed.port is not None:
            netloc += f":{parsed.port}"
        return urlunsplit((parsed.scheme.lower(), netloc, parsed.path, parsed.query, parsed.fragment))
    except (TypeError, ValueError):
        return ""


def validate_proxy_address(proxy: Any) -> str:
    raw_value = str(proxy or "").strip()
    if not raw_value:
        return ""
    value = normalize_proxy_address(raw_value)
    if not value:
        raise ValueError("代理地址无效，仅支持 http、https、socks4、socks5 或 socks5h")
    parsed = urlsplit(value)
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ValueError("代理地址不能包含路径、查询参数或 fragment")
    return urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))


def proxy_server(proxy: Any) -> str:
    value = normalize_proxy_address(proxy)
    if not value:
        return ""
    parsed = urlsplit(value)
    host = parsed.hostname or ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    server = f"{parsed.scheme}://{host}"
    if parsed.port is not None:
        server += f":{parsed.port}"
    return server


def build_proxy_url(proxy: Any, username: Any = "", password: Any = "") -> str:
    value = validate_proxy_address(proxy)
    normalized_username = str(username or "").strip()
    normalized_password = str(password or "")
    if not value:
        if normalized_username or normalized_password:
            raise ValueError("填写代理鉴权信息前必须先填写代理地址")
        return ""
    if normalized_password and not normalized_username:
        raise ValueError("填写代理密码时必须同时填写代理用户名")
    if not normalized_username:
        return value
    parsed = urlsplit(value)
    userinfo = quote(normalized_username, safe="")
    if normalized_password:
        userinfo += f":{quote(normalized_password, safe='')}"
    return urlunsplit((parsed.scheme, f"{userinfo}@{urlsplit(proxy_server(value)).netloc}", "", "", ""))


def normalize_proxies(proxy: Any) -> Optional[Dict[str, str]]:
    if not proxy:
        return None
    if isinstance(proxy, str):
        value = normalize_proxy_address(proxy)
        return {"http": value, "https": value} if value else None
    if isinstance(proxy, dict):
        if proxy.get("server"):
            value = build_proxy_url(proxy.get("server"), proxy.get("username"), proxy.get("password"))
            return {"http": value, "https": value} if value else None
        normalized = {str(key): normalize_proxy_address(value) for key, value in proxy.items() if
                      normalize_proxy_address(value)}
        return normalized or None
    return None
