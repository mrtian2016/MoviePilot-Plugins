"""海盗湾 (The Pirate Bay / apibay) 搜索客户端。"""

import html
import threading
from typing import Any, Dict, List, Optional
from urllib.parse import quote

from ..http_client import (
    RequestGate,
    gated_request,
    normalize_proxies,
    normalize_proxy_address,
    requests,
)
from ...utils.cache import create_platform_ttl_cache


class PirateBayError(RuntimeError):
    """海盗湾请求或解析失败。"""


class PirateBayClient:
    """通过 apibay.org API 搜索海盗湾磁力资源。"""

    DEFAULT_BASE_URL = "https://apibay.org"
    _HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
    }

    def __init__(
            self,
            base_url: str = DEFAULT_BASE_URL,
            proxy: Optional[str] = None,
            timeout: int = 20,
            request_interval: float = 1.0,
    ) -> None:
        self._raw_base_url = str(base_url or self.DEFAULT_BASE_URL).strip()
        self.base_url = self._raw_base_url.rstrip("/") or self.DEFAULT_BASE_URL
        self._proxy = normalize_proxy_address(proxy)
        self.timeout = max(5, int(timeout or 20))
        self.request_interval = max(0.2, float(request_interval or 1.0))
        self._gate = RequestGate.shared(
            "PirateBay",
            f"{self.base_url}|{self._proxy}",
            request_interval=self.request_interval,
            minimum_interval=0.2,
            serial_requests=False,
        )
        self._cache = create_platform_ttl_cache("piratebay_search", ttl=1800, maxsize=500)
        self._cache_lock = threading.Lock()

    @property
    def proxy(self) -> str:
        return self._proxy

    def update_config(
            self,
            base_url: Optional[str] = None,
            proxy: Optional[str] = None,
            timeout: Optional[int] = None,
            request_interval: Optional[float] = None,
    ) -> None:
        if base_url is not None:
            raw = str(base_url).strip()
            self._raw_base_url = raw or self.DEFAULT_BASE_URL
            self.base_url = self._raw_base_url.rstrip("/") or self.DEFAULT_BASE_URL
        if proxy is not None:
            self._proxy = normalize_proxy_address(proxy)
        if timeout is not None:
            self.timeout = max(5, int(timeout or 20))
        if request_interval is not None:
            self.request_interval = max(0.2, float(request_interval or 1.0))
        self._gate = RequestGate.shared(
            "PirateBay",
            f"{self.base_url}|{self._proxy}",
            request_interval=self.request_interval,
            minimum_interval=0.2,
            serial_requests=False,
        )

    def search(self, query: str, cat: int = 0) -> List[Dict[str, Any]]:
        keyword = str(query or "").strip()
        if not keyword:
            return []
        cache_key = f"{keyword.casefold()}:{cat}"
        with self._cache_lock:
            cached = self._cache.get(cache_key)
            if cached is not None:
                return [dict(item) for item in cached]

        url = f"{self.base_url}/q.php?q={quote(keyword)}&cat={cat}"
        proxies = normalize_proxies(self._proxy)

        def _requester() -> Any:
            return requests.get(
                url,
                headers=self._HEADERS,
                proxies=proxies,
                timeout=self.timeout,
            )

        try:
            response = gated_request(self._gate, _requester, max_retries=2, initial_delay=1.0)
            if response.status_code != 200:
                raise PirateBayError(f"HTTP {response.status_code}")
            items = response.json()
        except Exception as error:
            raise PirateBayError(f"海盗湾请求失败: {error}") from error

        results = []
        if isinstance(items, list):
            for entry in items:
                if not isinstance(entry, dict):
                    continue
                item_id = str(entry.get("id") or "").strip()
                name = html.unescape(str(entry.get("name") or "").strip())
                info_hash = str(entry.get("info_hash") or "").strip().lower()
                if item_id == "0" or not info_hash or info_hash == "0000000000000000000000000000000000000000":
                    continue
                try:
                    size = int(entry.get("size") or 0)
                except (ValueError, TypeError):
                    size = 0
                try:
                    seeders = int(entry.get("seeders") or 0)
                except (ValueError, TypeError):
                    seeders = 0
                try:
                    leechers = int(entry.get("leechers") or 0)
                except (ValueError, TypeError):
                    leechers = 0

                magnet_url = f"magnet:?xt=urn:btih:{info_hash}&dn={quote(name)}"
                results.append({
                    "url": magnet_url,
                    "title": name,
                    "size": size,
                    "seeders": seeders,
                    "leechers": leechers,
                    "info_hash": info_hash.upper(),
                    "resource_type": "magnet",
                    "source": "piratebay",
                    "source_url": f"https://thepiratebay.org/description.php?id={item_id}" if item_id else "",
                    "added": str(entry.get("added") or ""),
                })

        with self._cache_lock:
            self._cache[cache_key] = results
        return [dict(item) for item in results]

    def clear_cache(self) -> int:
        with self._cache_lock:
            count = len(self._cache)
            self._cache.clear()
            return count
