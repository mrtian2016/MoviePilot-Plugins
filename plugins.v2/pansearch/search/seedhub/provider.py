"""SeedHub 搜索能力声明。"""

from typing import Any, Mapping

from ...core.search import (
    SearchCapability,
    SearchPolicy,
    SearchProvider,
)


def create_seedhub_provider(
        service: Any,
        cache_context: Mapping[str, Any],
) -> SearchProvider:
    return SearchProvider(
        key="seedhub",
        name="SeedHub",
        resource_types=frozenset({"magnet"}),
        services={
            SearchCapability.RESOURCE_SEARCH: service,
            SearchCapability.RESOURCE_RESOLVE: service,
            SearchCapability.CACHE_MAINTENANCE: service,
        },
        policy=SearchPolicy(cache_context=cache_context),
    )
