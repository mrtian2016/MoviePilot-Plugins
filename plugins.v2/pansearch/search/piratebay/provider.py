"""海盗湾搜索能力声明。"""

from typing import Any, Mapping

from ...core.search import (
    SearchCapability,
    SearchPolicy,
    SearchProvider,
)


def create_piratebay_provider(
        service: Any,
        cache_context: Mapping[str, Any],
) -> SearchProvider:
    return SearchProvider(
        key="piratebay",
        name="海盗湾",
        resource_types=frozenset({"magnet"}),
        services={
            SearchCapability.RESOURCE_SEARCH: service,
            SearchCapability.CACHE_MAINTENANCE: service,
        },
        policy=SearchPolicy(cache_context=cache_context),
    )
