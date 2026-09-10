"""搜索渠道注册表组装。"""

from typing import Any

from .dian115 import create_dian115_provider
from .hdhive import create_hdhive_provider
from .juying import JuyingSearchService, create_juying_provider
from .online_docs import create_online_docs_provider
from .pansou import create_pansou_provider
from .pinglian import PinglianSearchService, create_pinglian_provider
from .piratebay import PirateBaySearchService, create_piratebay_provider
from .seedhub import SeedHubSearchService, create_seedhub_provider
from .uindex import UIndexSearchService, create_uindex_provider
from ..core.search import SearchRegistry


def create_search_registry(
        owner: Any,
        pansou_service: Any,
        hdhive_service: Any,
        dian115_service: Any,
) -> SearchRegistry:
    """根据当前配置组装可用渠道，调用端无需理解来源差异。"""
    registry = SearchRegistry()
    resource_types = tuple(owner._resource_type_order_config)

    if hdhive_service and getattr(hdhive_service, "available", False):
        registry.register(create_hdhive_provider(hdhive_service))
    if dian115_service and getattr(dian115_service, "available", False):
        registry.register(create_dian115_provider(dian115_service))

    if owner._pansou_client:
        pansou_service = pansou_service or PanSouSearchService(
            owner._pansou_client,
            owner._pansou_resource_types,
            owner._pansou_refresh,
            owner._pansou_timeout,
        )
        registry.register(create_pansou_provider(
            pansou_service,
            resource_types,
            {
                "channels": list(owner._pansou_channels),
                "plugins": list(owner._pansou_plugins),
                "cloud_types": list(owner._pansou_cloud_types),
                "filter": dict(owner._pansou_filter),
                "concurrency": owner._pansou_concurrency,
                "result_limit": owner._pansou_result_limit,
                "refresh": owner._pansou_refresh,
                "timeout": owner._pansou_timeout,
            },
        ))
    if (
            owner._juying_resources
            and owner._juying_client
            and getattr(owner._juying_client, "is_configured", False)
            and owner._juying_resource_types
    ):
        registry.register(create_juying_provider(
            JuyingSearchService(
                owner._juying_client,
                owner._juying_resources,
                owner._juying_resource_types,
                owner._juying_result_limit,
            ),
            owner._juying_client,
            owner._juying_resource_types,
            {
                "result_limit": owner._juying_result_limit,
                "resource_types": list(owner._juying_resource_types),
            },
        ))
    if owner._seedhub_client:
        registry.register(create_seedhub_provider(
            SeedHubSearchService(
                owner._seedhub_client, owner._seedhub_result_limit
            ),
            {"result_limit": owner._seedhub_result_limit},
        ))
    if getattr(owner, "_piratebay_client", None):
        registry.register(create_piratebay_provider(
            PirateBaySearchService(
                owner._piratebay_client,
                getattr(owner, "_piratebay_result_limit", 20),
            ),
            {"result_limit": getattr(owner, "_piratebay_result_limit", 20)},
        ))
    if getattr(owner, "_uindex_client", None):
        registry.register(create_uindex_provider(
            UIndexSearchService(
                owner._uindex_client,
                getattr(owner, "_uindex_result_limit", 20),
            ),
            {"result_limit": getattr(owner, "_uindex_result_limit", 20)},
        ))
    if (
            owner._pinglian_client
            and getattr(owner._pinglian_client, "is_configured", False)
            and resource_types
    ):
        registry.register(create_pinglian_provider(
            PinglianSearchService(
                owner._pinglian_client,
                resource_types,
                owner._pinglian_result_limit,
            ),
            owner._pinglian_client,
            resource_types,
            {"result_limit": owner._pinglian_result_limit},
        ))
    if getattr(owner, "_online_docs_client", None):
        registry.register(create_online_docs_provider(
            owner._online_docs_client,
            resource_types,
        ))
    return registry
