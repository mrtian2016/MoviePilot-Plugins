"""搜索渠道共享的资源类型定义。"""

from urllib.parse import urlparse

TYPE_ALIASES = {
    "115": "115",
    "115pan": "115",
    "123": "123",
    "123pan": "123",
    "quark": "quark",
    "aliyun": "alipan",
    "ali": "alipan",
    "alipan": "alipan",
    "189": "tianyi",
    "tianyi": "tianyi",
    "guangya": "guangya",
    "magnet": "magnet",
    "magnetlink": "magnet",
    "ed2k": "ed2k",
}
TYPE_HOSTS = {
    "115": {"115.com", "115cdn.com", "anxia.com"},
    "123": {
        "123pan.com", "123pan.cn", "123684.com", "123685.com",
        "123865.com", "123912.com", "123592.com",
    },
    "quark": {"quark.cn"},
    "alipan": {"alipan.com", "aliyundrive.com", "aliyundrive.net"},
    "tianyi": {"cloud.189.cn"},
    "guangya": {"guangyapan.com"},
}
TYPE_NAMES = {
    "115": "115网盘",
    "123": "123云盘",
    "quark": "夸克网盘",
    "alipan": "阿里云盘",
    "magnet": "磁力链接",
    "ed2k": "电驴链接",
    "tianyi": "天翼云盘",
    "guangya": "光鸭云盘",
}

SUPPORTED_CLOUD_TYPES = tuple(TYPE_HOSTS)
RESOURCE_TYPE_ORDER = (
    "115", "123", "quark", "guangya", "tianyi", "alipan",
    "ed2k", "magnet",
)
SUPPORTED_RESOURCE_TYPES = frozenset(RESOURCE_TYPE_ORDER)
RESOURCE_TYPE_PRIORITY = {
    resource_type: index
    for index, resource_type in enumerate(RESOURCE_TYPE_ORDER)
}
PANSOU_RESOURCE_TYPES = (
    "aliyun", "quark", "guangya", "tianyi",
    "115", "123", "magnet", "ed2k",
)
PREVIEW_PROVIDER_KEYS = {
    "115": "115",
    "123": "123",
    "quark": "quark",
    "guangya": "guangya",
    "tianyi": "tianyi",
    "alipan": "alipan",
}
PREVIEW_RESOURCE_TYPES = frozenset({*PREVIEW_PROVIDER_KEYS, "magnet"})

_TYPE_TEXT_MARKERS = {
    "115": ("115网盘", "115.com", "115cdn.com", "anxia.com"),
    "123": ("123云盘", "123网盘", "123pan"),
    "quark": ("夸克", "quark"),
    "guangya": ("光鸭", "guangya"),
    "tianyi": ("天翼", "cloud.189.cn"),
    "alipan": ("阿里云盘", "阿里网盘", "alipan", "aliyundrive"),
    "ed2k": ("ed2k://", "电驴"),
    "magnet": ("magnet:?", "磁力"),
}


def normalize_resource_type(value: str) -> str:
    normalized = str(value or "").strip().lower()
    return TYPE_ALIASES.get(normalized, normalized)


def resource_type_from_url(value: str) -> str:
    """根据标准链接 scheme 或域名识别内部资源类型。"""
    target = str(value or "").strip()
    lowered = target.casefold()
    if lowered.startswith("magnet:?"):
        return "magnet"
    if lowered.startswith("ed2k://"):
        return "ed2k"
    try:
        host = str(urlparse(target).hostname or "").casefold()
    except ValueError:
        return ""
    for resource_type, domains in TYPE_HOSTS.items():
        if any(host == domain or host.endswith(f".{domain}") for domain in domains):
            return resource_type
    return ""


def resource_type_from_text(value: str) -> str:
    """从外部类型值、链接或资源描述中识别内部资源类型。"""
    text = str(value or "").strip()
    normalized = normalize_resource_type(text)
    if normalized in SUPPORTED_RESOURCE_TYPES:
        return normalized
    from_url = resource_type_from_url(text)
    if from_url:
        return from_url
    lowered = text.casefold()
    for resource_type in RESOURCE_TYPE_ORDER:
        if any(marker in lowered for marker in _TYPE_TEXT_MARKERS[resource_type]):
            return resource_type
    return ""


def resource_type_name(value: str, fallback: str = "") -> str:
    normalized = normalize_resource_type(value)
    return TYPE_NAMES.get(normalized) or str(fallback or normalized).strip()
