"""Mikan 自动订阅能力组装。"""
from datetime import datetime
from time import sleep
from typing import Iterator

from .client import MikanClient
from .service import MikanService
from ...core.subscribe import MediaCandidate, SubscribeContext, SubscribeProvider
from ...core.subscribe.provider import ranking_scan_limit
from ...core.subscribe.registry import register


@register
class MikanSubscribeProvider(SubscribeProvider):
    provider_id = "mikan"
    provider_name = "Mikan 新番"

    def __init__(self, client: MikanClient | None = None, service: MikanService | None = None):
        self.client = client or MikanClient()
        self.service = service or MikanService()

    def spec(self) -> dict:
        return {"id": self.provider_id, "name": self.provider_name, "default_cron": "0 10 * * 1"}

    @staticmethod
    def _season(value: str) -> str:
        value = str(value or "当前")
        if value in {"春", "夏", "秋", "冬"}:
            return value
        month = datetime.now().month
        return "冬" if month <= 3 else "春" if month <= 6 else "夏" if month <= 9 else "秋"

    def fetch(self, options: dict, context: SubscribeContext) -> Iterator[MediaCandidate]:
        year = int(options.get("year") or datetime.now().year)
        season = self._season(options.get("season"))
        limit = int(options.get("limit") or 100)
        scan_limit = ranking_scan_limit(options)
        proxy = context.proxy_for(options.get("proxy"))
        base_urls = options.get("base_urls")
        client = self.client
        if isinstance(base_urls, (list, tuple)) and base_urls:
            client = MikanClient(base_urls=base_urls)
        result = client.season(year, season, proxy)
        if not result:
            return
        html, base = result
        entries = self.service.season_entries(html, base)[:scan_limit]
        resolve_id = bool(options.get("resolve_bangumi_id", True))

        def load_detail(entry: dict) -> dict:
            if context.stopped() or not resolve_id:
                return {}
            response = client.detail(entry["mikan_id"], proxy)
            detail = self.service.detail(response[0]) if response else {}
            sleep(0.5)
            return detail

        yield from self.service.candidates(entries, str(year), load_detail)


def create_mikan_provider() -> MikanSubscribeProvider:
    return MikanSubscribeProvider()
