"""Mikan 季度新番客户端。"""
from typing import Optional
from urllib.parse import quote

from ...utils.http_client import normalize_proxies, requests


class MikanClient:
    BASE_URLS = ("https://mikanani.me", "https://mikanime.tv")
    USER_AGENT = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 Chrome/113 Safari/537.36 MikanProject/1.0.0"
    )

    def __init__(self, base_urls: tuple[str, ...] | list[str] | None = None) -> None:
        self.base_urls = tuple(
            str(value).strip().rstrip("/")
            for value in (base_urls or self.BASE_URLS)
            if str(value).strip()
        ) or self.BASE_URLS

    def get(self, path: str, proxy=None) -> Optional[tuple[str, str]]:
        last_error = None
        for base in self.base_urls:
            try:
                response = requests.get(
                    f"{base}{path}",
                    headers={"User-Agent": self.USER_AGENT},
                    timeout=30,
                    proxies=normalize_proxies(proxy),
                    impersonate="chrome",
                )
                try:
                    response.raise_for_status()
                    text = response.text
                finally:
                    response.close()
                if text:
                    return text, base
            except Exception as error:
                last_error = error
        if last_error:
            raise RuntimeError(f"Mikan 请求失败：{last_error}")
        return None

    def season(self, year: int, season: str, proxy=None) -> Optional[tuple[str, str]]:
        return self.get(
            f"/Home/BangumiCoverFlowByDayOfWeek?year={year}&seasonStr={quote(season)}",
            proxy,
        )

    def detail(self, mikan_id: str, proxy=None) -> Optional[tuple[str, str]]:
        return self.get(f"/Home/Bangumi/{mikan_id}", proxy)
