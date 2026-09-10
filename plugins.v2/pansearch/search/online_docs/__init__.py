"""在线文档搜索渠道。"""

from .client import OnlineDocumentClient, is_online_document_url, parse_online_document
from .provider import create_online_docs_provider
from .service import OnlineDocumentSearchService

__all__ = [
    "OnlineDocumentClient",
    "OnlineDocumentSearchService",
    "create_online_docs_provider",
    "is_online_document_url",
    "parse_online_document",
]
