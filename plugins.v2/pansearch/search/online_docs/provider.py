"""在线文档搜索能力声明。"""

from typing import Any, Iterable

from ...core.search import (
    SearchCapability,
    SearchProvider,
)
from .service import OnlineDocumentSearchService


def create_online_docs_provider(
        client: Any,
        resource_types: Iterable[str],
) -> SearchProvider:
    service = OnlineDocumentSearchService(client)
    return SearchProvider(
        key="online_docs",
        name="在线文档",
        resource_types=frozenset(resource_types),
        services={
            SearchCapability.RESOURCE_SEARCH: service,
            SearchCapability.CACHE_MAINTENANCE: service,
        },
    )
