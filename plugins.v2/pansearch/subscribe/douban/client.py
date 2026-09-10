"""豆瓣榜单 RSS 客户端。"""
from time import sleep
from urllib.parse import urlsplit, urlunsplit

from app.log import logger

from ...utils.http_client import normalize_proxies, requests


class DoubanClient:
    def __init__(self, timeout: int = 120) -> None:
        self.timeout = timeout

    def get(self, url: str, proxy=None) -> str:
        last_error = None
        for attempt in range(1, 4):
            response = None
            try:
                response = requests.get(
                    url,
                    timeout=self.timeout,
                    proxies=normalize_proxies(proxy),
                    impersonate="chrome",
                )
                if response.status_code in {403, 429} or response.status_code >= 500:
                    last_error = requests.exceptions.HTTPError(
                        f"HTTP Error {response.status_code}: {self._display_url(url)}"
                    )
                    logger.debug(
                        f"豆瓣 RSS 请求重试：url={self._display_url(url)}, status={response.status_code}, "
                        f"attempt={attempt}/3"
                    )
                    if attempt < 3:
                        sleep(0.4 * attempt)
                        continue
                    raise last_error
                if response.status_code >= 400:
                    raise RuntimeError(
                        f"HTTP Error {response.status_code}: {self._display_url(url)}"
                    )
                response.raise_for_status()
                if not response.text:
                    raise RuntimeError(f"豆瓣 RSS 请求无响应：{self._display_url(url)}")
                return response.text
            except Exception as error:
                last_error = error
                if attempt >= 3:
                    raise
            finally:
                if response is not None:
                    response.close()
        raise last_error or RuntimeError(f"豆瓣 RSS 请求失败：{self._display_url(url)}")

    @staticmethod
    def _display_url(url: str) -> str:
        """日志只显示地址结构，避免把自定义 RSS 查询参数写入日志。"""
        parsed = urlsplit(str(url or ""))
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
