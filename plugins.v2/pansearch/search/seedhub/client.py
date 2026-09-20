"""SeedHub 网盘与 Magnet 搜索客户端。"""

import base64
import html
import re
import threading
import time
from html.parser import HTMLParser
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, quote, unquote, urljoin, urlparse

from ..http_client import (
    RequestGate, gated_request, normalize_proxies, normalize_proxy_address,
    proxy_server, requests,
)
from ..matching import extract_year
from ..types import resource_type_from_url
from ...utils.cache import create_platform_ttl_cache


class SeedHubError(RuntimeError):
    """SeedHub 请求或页面解析失败。"""


class _SeedHubLinkParser(HTMLParser):
    """提取详情页网盘中转项与中转页真实直链。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.pan_entries: List[Dict[str, str]] = []
        self.direct_links: List[str] = []
        self._pan_list_depth = 0
        self._current: Optional[Dict[str, str]] = None
        self._current_text: List[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        attributes = dict(attrs)
        classes = set(str(attributes.get("class") or "").split())
        if tag == "ul":
            if self._pan_list_depth:
                self._pan_list_depth += 1
            elif "pan-links" in classes:
                self._pan_list_depth = 1
        if tag != "a":
            return
        href = html.unescape(str(attributes.get("href") or "")).strip()
        if "direct-pan" in classes and href:
            self.direct_links.append(href)
        if self._pan_list_depth and "redirect_to=pan_id_" in href:
            self._current = {
                "href": href,
                "title": html.unescape(str(attributes.get("title") or "")).strip(),
                "host": str(attributes.get("data-link") or "").strip().lower(),
            }
            self._current_text = []

    def handle_data(self, data: str) -> None:
        if self._current is not None:
            self._current_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._current is not None:
            item = dict(self._current)
            item["title"] = item["title"] or " ".join(self._current_text).strip()
            self.pan_entries.append(item)
            self._current = None
            self._current_text = []
        if tag == "ul" and self._pan_list_depth:
            self._pan_list_depth -= 1


class SeedHubClient:
    """搜索作品页并解析其中的网盘或 Magnet 链接。"""

    _HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/123.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }
    _SEARCH_PATTERN = re.compile(
        r'<div class="cover">.*?title="(?P<anchor_title>[^"]*)"[^>]*'
        r'href="/movies/(?P<movie_id>\d+)/".*?'
        r'<li><h2><a[^>]+href="/movies/\d+/"[^>]*>.*?</a>\s*'
        r'(?P<title>.*?)</h2></li>\s*<li>(?P<meta>.*?)</li>'
        r'(?P<extra>.*?)</ul>\s*</div>',
        re.IGNORECASE | re.DOTALL,
    )
    _DOUBAN_PATTERN = re.compile(
        r'(?:movie\.)?douban\.com/subject/(?P<douban_id>\d+)',
        re.IGNORECASE,
    )
    _ENTRY_PATTERN = re.compile(
        r'<li>\s*(?P<a><a[^>]+href="/link_start/\?seed_id=(?P<seed>\d+)'
        r'[^"]*"[^>]*>.*?</a>)\s*/\s*'
        r'<code class="size">(?P<size>[^<]*)</code>.*?'
        r'<span class="create-time"[^>]*>(?P<updated>[^<]*)</span>',
        re.IGNORECASE | re.DOTALL,
    )

    def __init__(
            self,
            base_url: str = "https://www.seedhub.cc",
            proxy: Any = None,
            request_timeout: int = 20,
            request_interval: float = 0.3,
    ):
        self.base_url = str(base_url or "https://www.seedhub.cc").rstrip("/")
        self._proxies = normalize_proxies(proxy)
        self._browser_proxy = self._normalize_browser_proxy(proxy)
        self._request_timeout = max(5, min(int(request_timeout or 20), 60))
        self._magnet_cache = create_platform_ttl_cache(
            "seedhub:magnets", self.base_url, maxsize=1024, ttl=60 * 60
        )
        self._cache_lock = threading.RLock()
        self._browser_lock = threading.RLock()
        self._browser_state_version = 0
        self._browser_cookie_header = ""
        self._browser_user_agent = ""
        self._request_gate = RequestGate.shared(
            "SeedHub",
            f"{self.base_url}|{self._proxies}",
            request_interval=request_interval,
            minimum_interval=0.2,
            challenge_detector=self._is_challenge_response,
            serial_requests=False,
        )

    @staticmethod
    def _normalize_browser_proxy(proxy: Any) -> Optional[Dict[str, str]]:
        if isinstance(proxy, dict):
            proxy = (
                    proxy.get("https") or proxy.get("http") or proxy.get("server")
            )
        value = normalize_proxy_address(proxy)
        if not value:
            return None
        parsed = urlparse(value)
        if not parsed.scheme or not parsed.hostname:
            return None
        result = {"server": proxy_server(value)}
        if parsed.username:
            result["username"] = unquote(parsed.username)
        if parsed.password:
            result["password"] = unquote(parsed.password)
        return result

    @staticmethod
    def _clean_text(value: object) -> str:
        return html.unescape(re.sub(r"<[^>]+>", "", str(value or ""))).strip()

    @staticmethod
    def _is_cloudflare_challenge(
            text: str, status_code: int = 200, server: str = ""
    ) -> bool:
        lowered = str(text or "").lower()
        markers = (
            "cf-chl-",
            "cdn-cgi/challenge-platform",
            "enable javascript and cookies",
            "<title>just a moment",
        )
        return any(marker in lowered for marker in markers) or (
            status_code in {403, 503} and str(server or "").lower() == "cloudflare"
        )

    def _request_headers(self) -> Dict[str, str]:
        with self._browser_lock:
            headers = dict(self._HEADERS)
            if self._browser_user_agent:
                headers["User-Agent"] = self._browser_user_agent
            if self._browser_cookie_header:
                headers["Cookie"] = self._browser_cookie_header
            return headers

    @classmethod
    def _is_challenge_response(cls, response) -> bool:
        return cls._is_cloudflare_challenge(
            response.text or "",
            response.status_code,
            response.headers.get("Server", ""),
        )

    def _request_once(self, url: str):
        return gated_request(
            self._request_gate,
            requests.get,
            url,
            impersonate="chrome",
            headers=self._request_headers(),
            proxies=self._proxies,
            timeout=(8, self._request_timeout),
            allow_redirects=True,
        )

    def _get_browser_text(self, url: str, observed_version: int) -> str:
        with self._browser_lock:
            if self._browser_state_version != observed_version:
                try:
                    response = self._request_once(url)
                    text = response.text or ""
                    if (
                            response.ok
                            and not self._is_cloudflare_challenge(
                                text,
                                response.status_code,
                                response.headers.get("Server", ""),
                            )
                    ):
                        return text
                except requests.exceptions.RequestException:
                    pass

            from app.helper.browser import PlaywrightHelper

            def snapshot(page) -> Dict[str, Any]:
                return {
                    "text": page.content() or "",
                    "cookies": page.context.cookies(),
                    "user_agent": page.evaluate("navigator.userAgent") or "",
                }

            result = self._request_gate.run(lambda: PlaywrightHelper().action(
                url=url,
                callback=snapshot,
                proxies=self._browser_proxy,
                headless=True,
                timeout=max(30, self._request_timeout),
            ))
            if not isinstance(result, dict):
                return ""
            text = str(result.get("text") or "")
            if not text or self._is_cloudflare_challenge(text):
                self._request_gate.activate_cooldown(
                    60, reason="SeedHub 浏览器验证"
                )
                return ""
            seedhub_host = str(urlparse(self.base_url).hostname or "").lower()
            cookies = [
                f"{cookie.get('name')}={cookie.get('value')}"
                for cookie in (result.get("cookies") or [])
                if cookie.get("name") and cookie.get("value") is not None
                if (
                    not cookie.get("domain")
                    or seedhub_host == str(cookie.get("domain")).lstrip(".").lower()
                    or seedhub_host.endswith(
                        f".{str(cookie.get('domain')).lstrip('.').lower()}"
                    )
                )
            ]
            self._browser_cookie_header = "; ".join(cookies)
            self._browser_user_agent = str(result.get("user_agent") or "")
            self._browser_state_version += 1
            return text

    def _get_text(self, url: str) -> str:
        last_error = ""
        for attempt in range(2):
            try:
                with self._browser_lock:
                    browser_state_version = self._browser_state_version
                response = self._request_once(url)
                text = response.text or ""
                if self._is_cloudflare_challenge(
                        text,
                        response.status_code,
                        response.headers.get("Server", ""),
                ):
                    browser_text = self._get_browser_text(
                        url, browser_state_version
                    )
                    if browser_text:
                        return browser_text
                    raise SeedHubError("SeedHub 浏览器仿真未通过 Cloudflare 验证")
                if response.status_code == 429 or response.status_code >= 500:
                    last_error = f"HTTP {response.status_code}"
                    if attempt == 0:
                        time.sleep(0.3)
                        continue
                response.raise_for_status()
                return text
            except SeedHubError:
                raise
            except requests.exceptions.RequestException as error:
                last_error = type(error).__name__
                if attempt == 0:
                    time.sleep(0.3)
                    continue
        raise SeedHubError(f"SeedHub 请求失败：{last_error or '未知错误'}")

    def _parse_search_candidates(self, text: str) -> List[Dict[str, str]]:
        candidates = []
        seen = set()
        for matched in self._SEARCH_PATTERN.finditer(text):
            movie_id = str(matched.group("movie_id") or "").strip()
            if not movie_id or movie_id in seen:
                continue
            seen.add(movie_id)
            meta = self._clean_text(matched.group("meta"))
            meta_parts = [part.strip() for part in meta.split("/") if part.strip()]
            douban_match = self._DOUBAN_PATTERN.search(
                html.unescape(matched.group("extra") or "")
            )
            candidates.append({
                "movie_id": movie_id,
                "title": self._clean_text(matched.group("title")),
                "anchor_title": self._clean_text(matched.group("anchor_title")),
                "year": extract_year(meta),
                "media_type": meta_parts[1] if len(meta_parts) >= 2 else "",
                "douban_id": (
                    douban_match.group("douban_id") if douban_match else ""
                ),
            })
        return candidates

    def search_candidates(self, keyword: str) -> List[Dict[str, str]]:
        """请求搜索页并返回协议层作品条目。"""
        text = self._get_text(f"{self.base_url}/s/{quote(keyword)}/")
        return self._parse_search_candidates(text)

    def _parse_entries(self, text: str) -> List[Dict[str, str]]:
        entries = []
        seen = set()
        for matched in self._ENTRY_PATTERN.finditer(text):
            seed_id = str(matched.group("seed") or "").strip()
            if not seed_id or seed_id in seen:
                continue
            seen.add(seed_id)
            anchor = str(matched.group("a") or "")
            title_match = re.search(r'title="([^"]*)"', anchor, re.IGNORECASE)
            entries.append({
                "kind": "magnet",
                "seed_id": seed_id,
                "title": self._clean_text(title_match.group(1) if title_match else ""),
                "size": self._clean_text(matched.group("size")),
                "updated_at": self._clean_text(matched.group("updated")),
            })
        return entries

    @staticmethod
    def _parse_pan_entries(text: str) -> List[Dict[str, str]]:
        parser = _SeedHubLinkParser()
        parser.feed(text or "")
        parser.close()
        return parser.pan_entries

    def detail_entries(self, movie_id: str) -> List[Dict[str, str]]:
        """请求作品页并返回尚未解析链接的协议层资源条目。"""
        text = self._get_text(f"{self.base_url}/movies/{movie_id}/")
        entries = self._parse_entries(text)
        entries.extend(
            {"kind": "pan", **item} for item in self._parse_pan_entries(text)
        )
        return entries

    def _resolve_magnet(self, seed_id: str) -> str:
        with self._cache_lock:
            cached = self._magnet_cache.get(seed_id)
        if cached:
            return str(cached)
        text = self._get_text(
            f"{self.base_url}/link_start/?seed_id={quote(seed_id)}&movie_title=seedhub"
        )
        matched = re.search(r'const\s+data\s*=\s*"([A-Za-z0-9+/=]+)"', text)
        if not matched:
            return ""
        try:
            magnet = base64.b64decode(matched.group(1)).decode(
                "utf-8", errors="ignore"
            ).strip()
        except (ValueError, TypeError):
            return ""
        if not magnet.lower().startswith("magnet:?"):
            return ""
        with self._cache_lock:
            self._magnet_cache.set(seed_id, magnet)
        return magnet

    def _resolve_pan_link(self, entry: Dict[str, str]) -> tuple[str, str]:
        href = str(entry.get("href") or "").strip()
        if not href:
            return "", ""
        host_hint = str(entry.get("host") or "").strip()
        if host_hint and not resource_type_from_url(f"https://{host_hint}"):
            return "", ""
        cache_key = f"pan:{href}"
        with self._cache_lock:
            cached = self._magnet_cache.get(cache_key)
        if cached:
            url = str(cached)
            return url, resource_type_from_url(url)
        parser = _SeedHubLinkParser()
        parser.feed(self._get_text(urljoin(f"{self.base_url}/", href)))
        parser.close()
        url = next((str(value).strip() for value in parser.direct_links if value), "")
        resource_type = resource_type_from_url(url)
        if not resource_type:
            return "", ""
        with self._cache_lock:
            self._magnet_cache.set(cache_key, url)
        return url, resource_type

    def resolve_entry(self, item: Dict[str, str]) -> tuple[str, str]:
        if item.get("kind") == "pan":
            return self._resolve_pan_link(item)
        magnet = self._resolve_magnet(str(item.get("seed_id") or ""))
        return magnet, "magnet" if magnet else ""

    def resolve_resource(
            self,
            kind: str,
            resource_type: str,
            seed_id: str = "",
            path: str = "",
            host: str = "",
    ) -> Dict[str, str]:
        """解析测试列表中用户选中的单条 SeedHub 资源。"""
        kind = str(kind or "").strip().lower()
        expected_type = str(resource_type or "").strip().lower()
        if kind == "magnet":
            if expected_type != "magnet":
                raise SeedHubError("SeedHub Magnet 资源类型无效")
            seed_id = str(seed_id or "").strip()
            if not re.fullmatch(r"\d{1,32}", seed_id):
                raise SeedHubError("SeedHub 资源标识无效")
            item = {"kind": "magnet", "seed_id": seed_id}
        elif kind == "pan":
            path = str(path or "").strip()
            parsed = urlparse(path)
            redirect_to = parse_qs(parsed.query).get("redirect_to") or []
            if (
                    parsed.scheme or parsed.netloc
                    or parsed.path.rstrip("/") != "/link_start"
                    or len(redirect_to) != 1
                    or not re.fullmatch(r"pan_id_[A-Za-z0-9_-]{1,128}", redirect_to[0])
            ):
                raise SeedHubError("SeedHub 网盘资源标识无效")
            host = str(host or "").strip().lower()
            hinted_type = resource_type_from_url(f"https://{host}") if host else ""
            if not hinted_type or hinted_type != expected_type:
                raise SeedHubError("SeedHub 网盘资源类型无效")
            item = {"kind": "pan", "href": path, "host": host}
        else:
            raise SeedHubError("SeedHub 资源类型无效")

        url, actual_type = self.resolve_entry(item)
        if not url or actual_type != expected_type:
            raise SeedHubError("SeedHub 资源链接解析失败")
        return {"url": url, "resource_type": actual_type}

    def clear_cache(self) -> Dict[str, int]:
        with self._cache_lock:
            counts = {
                "links": len(list(self._magnet_cache.items())),
            }
            self._magnet_cache.clear()
        return counts
