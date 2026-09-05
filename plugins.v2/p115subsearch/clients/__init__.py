"""
客户端模块
包含115网盘、PanSou、Nullbr等客户端
"""
from .p115 import P115ClientManager
from .pansou import PanSouClient
from .nullbr import NullbrClient
from .hdhive import HDHiveOpenAPIClient, HDHiveOpenAPIError
from .kdocs import KDocsClient, KDocsError

__all__ = [
    "P115ClientManager",
    "PanSouClient",
    "NullbrClient",
    "HDHiveOpenAPIClient",
    "HDHiveOpenAPIError",
    "KDocsClient",
    "KDocsError"
]
