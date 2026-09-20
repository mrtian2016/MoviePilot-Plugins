"""聚影搜索能力声明。"""

from typing import Any, Iterable, Mapping

from ...core.search import (
    SearchCapability,
    SearchPolicy,
    SearchProvider,
)


def create_juying_provider(
        service: Any,
        client: Any,
        resource_types: Iterable[str],
        cache_context: Mapping[str, Any],
) -> SearchProvider:
    return SearchProvider(
        key="juying",
        name="聚影",
        resource_types=frozenset(resource_types),
        services={
            SearchCapability.RESOURCE_SEARCH: service,
            SearchCapability.RESOURCE_RESOLVE: service,
            SearchCapability.ACCOUNT: client,
            SearchCapability.CHECKIN: client,
            SearchCapability.CACHE_MAINTENANCE: service,
        },
        policy=SearchPolicy(
            cacheable=False,
            cache_context=cache_context,
        ),
    )
