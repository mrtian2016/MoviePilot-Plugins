"""猫眼专业版榜单客户端。"""
import random

from ...utils.http_client import normalize_proxies, requests


class MaoyanClient:
    BASE_URL = "https://piaofang.maoyan.com"
    USER_AGENTS = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/121 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/121 Safari/537.36",
    )

    def __init__(self, base_url: str = BASE_URL) -> None:
        self.base_url = str(base_url or self.BASE_URL).strip().rstrip("/")

    def get_json(self, path: str, proxy=None) -> dict:
        response = requests.get(
            f"{self.base_url}{path}",
            headers={"User-Agent": random.choice(self.USER_AGENTS)},
            timeout=60,
            proxies=normalize_proxies(proxy),
            impersonate="chrome",
        )
        try:
            response.raise_for_status()
            return response.json()
        finally:
            response.close()
