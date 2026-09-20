"""HDHive 官方开放 API 与 WebAPI 客户端。"""

from .open import (
    HDHiveOpenAPIClient,
    HDHiveOpenAPIError,
)
from .provider import create_hdhive_provider
from .service import HDHiveSearchService
from .web import (
    HDHIVE_DETAIL_RESOURCE_TYPES,
    HDHIVE_RESOURCE_TYPES,
    HDHiveClient,
    HDHiveResourceService,
    HDHiveWebError,
    valid_share_url,
)

__all__ = [
    "HDHiveClient",
    "HDHiveResourceService",
    "HDHiveWebError",
    "HDHiveOpenAPIClient",
    "HDHiveOpenAPIError",
    "HDHiveSearchService",
    "create_hdhive_provider",
    "HDHIVE_DETAIL_RESOURCE_TYPES",
    "HDHIVE_RESOURCE_TYPES",
    "valid_share_url",
]
