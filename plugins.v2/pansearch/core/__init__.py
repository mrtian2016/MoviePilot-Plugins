"""插件核心基础能力。"""

from .cloud import (
    CloudDriveCapability,
    CloudDriveCapabilityError,
    CloudDrivePolicy,
    CloudFile,
    CloudDriveProvider,
    CloudDriveRegistry,
    DirectoryListing,
    DirectoryLookup,
)
from .delegation import OwnerDelegator, get_component, resolve_component
from .scraper import MediaScraper
from .search import (
    SearchCandidate,
    SearchCapability,
    SearchCapabilityError,
    SearchPolicy,
    SearchProvider,
    SearchQuery,
    SearchRegistry,
    format_search_label,
    format_search_log_prefix,
    normalize_search_candidate,
)
from .transfer import CrossDriveTransfer, CrossTransferTaskManager, LocalRapidUploadAdapter

__all__ = [
    "OwnerDelegator",
    "CloudDriveCapability",
    "CloudDriveCapabilityError",
    "CloudDrivePolicy",
    "CloudFile",
    "CloudDriveProvider",
    "CloudDriveRegistry",
    "DirectoryListing",
    "DirectoryLookup",
    "MediaScraper",
    "SearchCandidate",
    "SearchCapability",
    "SearchCapabilityError",
    "SearchPolicy",
    "SearchProvider",
    "SearchQuery",
    "SearchRegistry",
    "format_search_label",
    "format_search_log_prefix",
    "normalize_search_candidate",
    "get_component",
    "resolve_component",
    "CrossDriveTransfer",
    "LocalRapidUploadAdapter",
    "CrossTransferTaskManager",
]
