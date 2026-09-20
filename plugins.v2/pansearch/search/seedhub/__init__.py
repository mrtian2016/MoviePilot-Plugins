"""SeedHub 网盘与 Magnet 搜索客户端。"""

from .client import SeedHubClient, SeedHubError
from .provider import create_seedhub_provider
from .service import SeedHubSearchService

__all__ = [
    "SeedHubClient",
    "SeedHubError",
    "SeedHubSearchService",
    "create_seedhub_provider",
]
