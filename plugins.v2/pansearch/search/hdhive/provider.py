"""HDHive 搜索能力声明。"""

from .service import HDHiveSearchService
from ...core.search import SearchCapability, SearchPolicy, SearchProvider


def create_hdhive_provider(service: HDHiveSearchService) -> SearchProvider:
    client = service.get_client()
    return SearchProvider(
        key="hdhive",
        name="HDHive",
        resource_types=service.resource_types,
        services={
            SearchCapability.RESOURCE_SEARCH: service,
            SearchCapability.RESOURCE_PREVIEW: service,
            SearchCapability.RESOURCE_UNLOCK: service,
            SearchCapability.ACCOUNT: client,
            SearchCapability.CHECKIN: client,
            SearchCapability.POINT_BUDGET: service.budget,
            SearchCapability.CACHE_MAINTENANCE: service,
            SearchCapability.LIFECYCLE: service,
        },
        policy=SearchPolicy(
            cacheable=True,
            cache_empty_results=False,
            cache_context=service.cache_context,
            max_concurrency=1,
        ),
    )
