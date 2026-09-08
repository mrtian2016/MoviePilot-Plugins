"""聚影网页资源搜索客户端。"""

from .client import JuyingClient, JuyingError
from .provider import create_juying_provider
from .resource import JuyingResourceService
from .service import JuyingSearchService

__all__ = [
    "JuyingClient",
    "JuyingError",
    "JuyingResourceService",
    "JuyingSearchService",
    "create_juying_provider",
]
