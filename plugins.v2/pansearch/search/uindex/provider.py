"""UIndex 搜索能力声明。"""

from typing import Any, Mapping

from ...core.search import (
    SearchCapability,
    SearchPolicy,
    SearchProvider,
)


def create_uindex_provider(
        service: Any,
        cache_context: Mapping[str, Any],
) -> SearchProvider:
    return SearchProvider(
        key="uindex",
        name="UIndex",
        resource_types=frozenset({"magnet"}),
        services={
            SearchCapability.RESOURCE_SEARCH: service,
            SearchCapability.CACHE_MAINTENANCE: service,
        },
        policy=SearchPolicy(cache_context=cache_context),
    )
