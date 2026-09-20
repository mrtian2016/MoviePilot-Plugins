"""页面与API适配。"""

from .account import AccountApi
from .checkin import CheckinApi
from .config import ConfigApi
from .history import HistoryApi
from .media_library import MediaLibraryApi
from .page import PageApi
from .qrcode import QRCodeService
from .registration import MoviePilotRegistration
from .runtime import RuntimeApi
from .search import SearchApi
from .sync import SyncApi

__all__ = [
    "AccountApi",
    "CheckinApi",
    "ConfigApi",
    "HistoryApi",
    "MediaLibraryApi",
    "MoviePilotRegistration",
    "PageApi",
    "QRCodeService",
    "RuntimeApi",
    "SearchApi",
    "SyncApi",
]
