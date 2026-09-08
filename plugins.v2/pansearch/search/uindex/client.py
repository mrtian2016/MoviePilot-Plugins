"""UIndex 磁力搜索客户端。"""

import html
import re
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


class UIndexError(RuntimeError):
    """UIndex 请求或解析失败。"""


_SIZE_REGEX = re.compile(r"(\d+(?:\.\d+)?)\s*(TB|GB|MB|KB|B)", re.IGNORECASE)
_MAGNET_REGEX = re.compile(r"magnet:\?xt=urn:btih:[a-zA-Z0-9]{32,40}[^\s\"'<>]*", re.IGNORECASE)
_HASH_REGEX = re.compile(r"urn:btih:([a-zA-Z0-9]{32,40})", re.IGNORECASE)


def parse_size_str(size_text: str) -> int:
    """解析字符串大小为字节数。"""
    match = _SIZE_REGEX.search(size_text or "")
    if not match:
        return 0
    val, unit = float(match.group(1)), match.group(2).upper()
    units = {
        "B": 1,
        "KB": 1024,
        "MB": 1024 ** 2,
        "GB": 1024 ** 3,
        "TB": 1024 ** 4,
    }
    return int(val * units.get(unit, 1))


class UIndexClient:
    """通过 UIndex 检索磁力资源。"""

    DEFAULT_BASE_URL = "https://uindex.org"
    _HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
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
            "UIndex",
            f"{self.base_url}|{self._proxy}",
            request_interval=self.request_interval,
            minimum_interval=0.2,
            serial_requests=False,
        )
        self._cache = create_platform_ttl_cache("uindex_search", ttl=1800, maxsize=500)
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
            "UIndex",
            f"{self.base_url}|{self._proxy}",
            request_interval=self.request_interval,
            minimum_interval=0.2,
            serial_requests=False,
        )

    def search(self, query: str) -> List[Dict[str, Any]]:
        keyword = str(query or "").strip()
        if not keyword:
            return []
        cache_key = keyword.casefold()
        with self._cache_lock:
            cached = self._cache.get(cache_key)
            if cached is not None:
                return [dict(item) for item in cached]

        url = f"{self.base_url}/search.php?search={quote(keyword)}"
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
                raise UIndexError(f"HTTP {response.status_code}")
            page_html = response.text
        except Exception as error:
            raise UIndexError(f"UIndex 请求失败: {error}") from error

        results = self._parse_search_page(page_html)
        with self._cache_lock:
            self._cache[cache_key] = results
        return [dict(item) for item in results]

    def _parse_search_page(self, page_html: str) -> List[Dict[str, Any]]:
        """解析搜索列表 HTML。"""
        results = []
        if not page_html:
            return results

        # 匹配每一行 <tr>...</tr>
        row_regex = re.compile(r"<tr[^>]*>(.*?)</tr>", re.IGNORECASE | re.DOTALL)
        for row_match in row_regex.finditer(page_html):
            row_content = row_match.group(1)
            # 必须包含磁力链接或带有 btih 的内容
            mag_match = _MAGNET_REGEX.search(row_content)
            if not mag_match:
                continue

            magnet_url = html.unescape(mag_match.group(0))
            hash_match = _HASH_REGEX.search(magnet_url)
            if not hash_match:
                continue
            info_hash = hash_match.group(1).upper()

            # 提取标题
            # 优先从详情链接文本或 title 属性提取
            title = ""
            title_match = re.search(r"<a[^>]+href=[\"'][^\"']*details\.php[^\"']*[\"'][^>]*>(.*?)</a>", row_content,
                                    re.IGNORECASE | re.DOTALL)
            if title_match:
                title = re.sub(r"<[^>]+>", "", title_match.group(1)).strip()
            if not title:
                # 尝试从 magnet 的 dn 参数提取
                dn_match = re.search(r"dn=([^&]+)", magnet_url)
                if dn_match:
                    from urllib.parse import unquote
                    title = unquote(dn_match.group(1))

            if not title:
                title = f"UIndex 资源 {info_hash[:8]}"

            # 提取文件大小
            size_bytes = parse_size_str(row_content)

            # 提取做种数
            seeders = 0
            seed_match = re.search(r"class=[\"'][^\"']*seeders?[^\"']*[\"'][^>]*>(\d+)</td>", row_content,
                                   re.IGNORECASE)
            if seed_match:
                seeders = int(seed_match.group(1))

            results.append({
                "url": magnet_url,
                "title": html.unescape(title).strip(),
                "size": size_bytes,
                "seeders": seeders,
                "info_hash": info_hash,
                "resource_type": "magnet",
                "source": "uindex",
                "source_url": f"{self.base_url}/search.php",
            })

        return results

    def clear_cache(self) -> int:
        with self._cache_lock:
            count = len(self._cache)
            self._cache.clear()
            return count
