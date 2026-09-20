"""Netflix Tudum Top10 TSV 客户端。"""
from ...utils.http_client import normalize_proxies, requests


class NetflixClient:
    BASE_URL = "https://www.netflix.com"
    MOST_POPULAR = "https://www.netflix.com/tudum/top10/data/most-popular.tsv"
    GLOBAL_WEEKLY = "https://www.netflix.com/tudum/top10/data/all-weeks-global.tsv"
    COUNTRIES_WEEKLY = "https://www.netflix.com/tudum/top10/data/all-weeks-countries.tsv"

    def __init__(self, base_url: str = BASE_URL) -> None:
        self.base_url = str(base_url or self.BASE_URL).strip().rstrip("/")

    def url(self, path: str) -> str:
        return f"{self.base_url}/{str(path or '').lstrip('/')}"

    @property
    def most_popular_url(self) -> str:
        return self.url("/tudum/top10/data/most-popular.tsv")

    @property
    def global_weekly_url(self) -> str:
        return self.url("/tudum/top10/data/all-weeks-global.tsv")

    @property
    def countries_weekly_url(self) -> str:
        return self.url("/tudum/top10/data/all-weeks-countries.tsv")

    def load_html(self, url: str, proxy=None) -> str:
        response = requests.get(
            url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36"
                )
            },
            timeout=120,
            proxies=normalize_proxies(proxy),
            impersonate="chrome",
        )
        try:
            response.raise_for_status()
            if not response.text:
                raise RuntimeError(f"Netflix 富元数据页面请求失败：{url}")
            return response.text
        finally:
            response.close()

    def load_tsv(self, url: str, proxy=None) -> list[dict]:
        response = requests.get(
            url,
            timeout=120,
            proxies=normalize_proxies(proxy),
            impersonate="chrome",
        )
        try:
            response.raise_for_status()
            content = response.content
        finally:
            response.close()
        if not content:
            raise RuntimeError(f"Netflix 榜单请求失败：{url}")
        lines = content.decode("utf-8", errors="replace").split("\n")
        if not lines:
            return []
        header = lines[0].strip("\r").split("\t")
        return [
            dict(zip(header, line.strip("\r").split("\t")))
            for line in lines[1:]
            if line.strip()
        ]
