"""Dian115 搜索能力声明。"""

from .service import Dian115SearchService
from ...core.search import SearchCapability, SearchPolicy, SearchProvider


def create_dian115_provider(service: Dian115SearchService) -> SearchProvider:
    client = service.get_client()
    return SearchProvider(
        key="dian115",
        name="Dian115",
        resource_types=service.resource_types,
        services={
            SearchCapability.RESOURCE_SEARCH: service,
            SearchCapability.RESOURCE_UNLOCK: service,
            SearchCapability.ACCOUNT: client,
            SearchCapability.POINT_BUDGET: service.budget,
            SearchCapability.CACHE_MAINTENANCE: service,
            SearchCapability.LIFECYCLE: service,
        },
        policy=SearchPolicy(
            cacheable=True,
            cache_context=service.cache_context,
            max_concurrency=1,
        ),
    )
