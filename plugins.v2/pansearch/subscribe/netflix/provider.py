"""Netflix 自动订阅能力组装。"""
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from threading import RLock
from typing import Iterator

from .client import NetflixClient
from .service import NetflixService
from ...core.subscribe import MediaCandidate, SubscribeContext, SubscribeProvider
from ...core.subscribe.provider import ranking_scan_limit
from ...core.subscribe.registry import register

GLOBAL_CATEGORIES = (
    "Films (English)", "Films (Non-English)", "TV (English)", "TV (Non-English)"
)
COUNTRY_CATEGORIES = ("Films", "TV")


@register
class NetflixSubscribeProvider(SubscribeProvider):
    provider_id = "netflix"
    provider_name = "Netflix 榜单"
    _cache_lock = RLock()
    _week_cache: dict[str, tuple[str, object]] = {}

    def __init__(self, client: NetflixClient | None = None, service: NetflixService | None = None):
        self.client = client or NetflixClient()
        self.service = service or NetflixService()

    def spec(self) -> dict:
        return {"id": self.provider_id, "name": self.provider_name, "default_cron": "0 11 * * 3"}

    def has_listening(self, options: dict) -> bool:
        global_types = options.get("global_media_types")
        if global_types is None:
            global_types = options.get("global_categories")
        return bool(
            (options.get("global", True) and global_types)
            or options.get("country_selections")
        )

    def fetch(self, options: dict, context: SubscribeContext) -> Iterator[MediaCandidate]:
        proxy = context.proxy_for(options.get("proxy"))
        limit = max(1, min(int(options.get("limit") or 10), 100))
        scan_limit = ranking_scan_limit(options)
        base_url = str(options.get("base_url") or NetflixClient.BASE_URL).strip()
        client = self.client if base_url.rstrip("/") == self.client.base_url else NetflixClient(base_url)
        seen: set[str] = set()
        if options.get("rich_metadata"):
            yield from self._fetch_rich(options, context, scan_limit, proxy, seen, client)
            return
        if options.get("global", True):
            dataset = str(options.get("global_dataset") or "weekly")
            url = client.most_popular_url if dataset == "popular" else client.global_weekly_url
            rows = self._load(options, context, f"tsv:{url}", lambda: client.load_tsv(url, proxy))
            if dataset != "popular":
                rows = self.service.latest_week(rows)
            categories = options.get("global_media_types")
            if categories is None:
                categories = options.get("global_categories")
            for category in categories or GLOBAL_CATEGORIES:
                if category not in GLOBAL_CATEGORIES:
                    continue
                for item in self.service.candidates(rows, category, "global", scan_limit):
                    if item.unique_seed not in seen:
                        seen.add(item.unique_seed)
                        yield item

    def _fetch_rich(
            self, options: dict, context: SubscribeContext, limit: int, proxy: bool,
            seen: set[str], client: NetflixClient,
    ) -> Iterator[MediaCandidate]:
        tasks = []
        categories = options.get("global_media_types") or options.get("global_categories") or GLOBAL_CATEGORIES
        categories = list(categories) if isinstance(categories, (list, tuple, set)) else [str(categories)]
        fallback_categories = {
            category for category in categories if category in GLOBAL_CATEGORIES
                                                   and ("Non-English" in category or category.startswith(
                "Films") or category.startswith("TV"))
        }
        if options.get("global", True):
            if "Films (English)" in categories:
                tasks.append(("films", "global", client.url("/tudum/top10/films")))
            if "TV (English)" in categories:
                tasks.append(("tv", "global", client.url("/tudum/top10/tv")))
        selections = options.get("country_selections") or {}
        if isinstance(selections, str):
            try:
                selections = json.loads(selections or "{}")
            except (TypeError, ValueError):
                selections = {}
        countries = {
            "JP": "japan", "KR": "south-korea", "US": "united-states", "GB": "united-kingdom",
            "CA": "canada", "FR": "france", "DE": "germany", "AU": "australia", "HK": "hong-kong",
            "TW": "taiwan", "IN": "india", "ES": "spain",
        }
        for country, types in selections.items():
            slug = countries.get(str(country).upper())
            if not slug:
                continue
            for media_type in types or []:
                if media_type == "Films":
                    tasks.append(("films", str(country).upper(), client.url(f"/tudum/top10/{slug}/films")))
                elif media_type == "TV":
                    tasks.append(("tv", str(country).upper(), client.url(f"/tudum/top10/{slug}/tv")))
        workers = max(1, min(int(options.get("max_workers") or 4), 16))
        results = {}
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(
                    self._load,
                    options,
                    context,
                    f"html:{url}",
                    lambda url=url: client.load_html(url, proxy),
                ): (kind, scope)
                for kind, scope, url in tasks
            }
            for future in as_completed(futures):
                if context.stopped():
                    return
                kind, scope = futures[future]
                try:
                    results[(kind, scope)] = self.service.rich_entries(future.result())
                except Exception as error:
                    if context.logger:
                        context.logger.warning(f"Netflix 富元数据页面跳过：{error}")
        for kind, scope, _ in tasks:
            for item in self.service.rich_candidates(results.get((kind, scope), []), kind, scope, limit):
                if item.unique_seed not in seen:
                    seen.add(item.unique_seed)
                    yield item

        # Tudum 全球页不稳定时，全球 TSV 是官方回退；非英语榜单始终走 TSV
        # 以保证完整性，英文榜单仅在富页没有结果时回退。
        if fallback_categories and not context.stopped():
            dataset = str(options.get("global_dataset") or "weekly")
            url = client.most_popular_url if dataset == "popular" else client.global_weekly_url
            try:
                rows = self._load(
                    options,
                    context,
                    f"tsv:{url}",
                    lambda: client.load_tsv(url, proxy),
                )
                if dataset != "popular":
                    rows = self.service.latest_week(rows)
                for category in fallback_categories:
                    for item in self.service.candidates(rows, category, "global", limit):
                        if item.unique_seed not in seen:
                            seen.add(item.unique_seed)
                            yield item
            except Exception as error:
                if context.logger:
                    context.logger.warning(f"Netflix 全球 TSV 回退跳过：{error}")
        selections = options.get("country_selections") or {}
        if isinstance(selections, str):
            try:
                selections = json.loads(selections or "{}")
            except (TypeError, ValueError):
                selections = {}
        if selections and not context.stopped():
            rows = self.service.latest_week(
                self._load(
                    options,
                    context,
                    f"tsv:{client.countries_weekly_url}",
                    lambda: client.load_tsv(client.countries_weekly_url, proxy),
                )
            )
            for country, categories in selections.items():
                if isinstance(categories, dict):
                    categories = categories.get("cats", []) if categories.get("on", True) else []
                country_rows = [row for row in rows if row.get("country_iso2") == country]
                for category in categories or []:
                    if category not in COUNTRY_CATEGORIES:
                        continue
                    for item in self.service.candidates(country_rows, category, country, limit):
                        if item.unique_seed not in seen:
                            seen.add(item.unique_seed)
                            yield item

    @classmethod
    def _week_token(cls) -> str:
        return datetime.now(timezone.utc).strftime("%G-W%V")

    @classmethod
    def _load(cls, options: dict, context: SubscribeContext, key: str, loader):
        if not bool(options.get("use_cache", True)):
            return loader()
        token = cls._week_token()
        with cls._cache_lock:
            cached = cls._week_cache.get(key)
            if cached and cached[0] == token:
                return cached[1]
        value = loader()
        with cls._cache_lock:
            cls._week_cache[key] = (token, value)
            if len(cls._week_cache) > 64:
                cls._week_cache = {
                    cache_key: entry
                    for cache_key, entry in cls._week_cache.items()
                    if entry[0] == token
                }
        return value


def create_netflix_provider() -> NetflixSubscribeProvider:
    return NetflixSubscribeProvider()
