"""工具模块。"""
from .cache import (
    cached_resource_call,
    create_platform_ttl_cache,
    normalize_platform_cache_key,
)
from .file_matcher import FileMatcher
from .file_parser import MediaFileParser
from .magnet import (
    DEFAULT_METADATA_URL_TEMPLATE,
    clear_magnet_metadata_cache,
    configure_magnet_metadata_url,
    parse_magnet_metadata,
)
from .strm import StrmGenerator, StrmTemplateError

__all__ = [
    "FileMatcher",
    "MediaFileParser",
    "StrmGenerator",
    "StrmTemplateError",
    "parse_magnet_metadata",
    "DEFAULT_METADATA_URL_TEMPLATE",
    "configure_magnet_metadata_url",
    "clear_magnet_metadata_cache",
    "cached_resource_call",
    "create_platform_ttl_cache",
    "normalize_platform_cache_key",
]
