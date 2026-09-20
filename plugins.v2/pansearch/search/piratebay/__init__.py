"""海盗湾 (The Pirate Bay) 搜索渠道。"""

from .client import PirateBayClient, PirateBayError
from .provider import create_piratebay_provider
from .service import PirateBaySearchService

__all__ = [
    "PirateBayClient",
    "PirateBayError",
    "PirateBaySearchService",
    "create_piratebay_provider",
]
