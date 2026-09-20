"""PanSearch 对 MoviePilot 平台缓存工具的轻量封装。"""

import copy
import json
from contextlib import nullcontext
from hashlib import sha256
from typing import Any, Callable, Optional, Sequence

from app.core.cache import TTLCache


def _cache_identity(value: Any) -> str:
    """提取稳定身份，避免不同账号或客户端实例共享缓存区。"""
    if value in (None, ""):
        return ""
    if isinstance(value, (str, int, float, bool)):
        return str(value).strip()
    for name in (
            "cache_namespace", "user_id", "drive_id", "session_key",
            "token", "cookies", "cookie", "_email", "email", "username",
    ):
        try:
            identity = str(getattr(value, name, "") or "").strip()
        except Exception:
            continue
        if identity:
            return identity
    return f"instance:{id(value)}"


def normalize_platform_cache_key(value: Any) -> str:
    """将复合键编码为平台缓存后端兼容的稳定字符串。"""
    if isinstance(value, str):
        return value
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, default=str,
        separators=(",", ":"),
    )
    return sha256(payload.encode("utf-8")).hexdigest()


class _PlatformTTLCache(TTLCache):
    """确保所有键在进入 MoviePilot 后端前都是稳定字符串。"""

    @staticmethod
    def _key(value: Any) -> str:
        return normalize_platform_cache_key(value)

    def __getitem__(self, key: Any) -> Any:
        return super().__getitem__(self._key(key))

    def __setitem__(self, key: Any, value: Any) -> None:
        super().__setitem__(self._key(key), value)

    def __delitem__(self, key: Any) -> None:
        super().__delitem__(self._key(key))

    def __contains__(self, key: Any) -> bool:
        return super().__contains__(self._key(key))

    def get(self, key: Any, **kwargs: Any) -> Any:
        return super().get(self._key(key), **kwargs)

    def set(self, key: Any, value: Any, **kwargs: Any) -> None:
        super().set(self._key(key), value, **kwargs)

    def delete(self, key: Any, **kwargs: Any) -> None:
        super().delete(self._key(key), **kwargs)

    def exists(self, key: Any, **kwargs: Any) -> bool:
        return super().exists(self._key(key), **kwargs)

    def pop(self, key: Any, default: Any = None, **kwargs: Any) -> Any:
        try:
            return super().pop(self._key(key), default, **kwargs)
        except KeyError:
            return default

    def setdefault(self, key: Any, default: Any = None, **kwargs: Any) -> Any:
        return super().setdefault(self._key(key), default, **kwargs)

    def update(self, other: Any, **kwargs: Any) -> None:
        normalized = {
            self._key(key): value for key, value in dict(other or {}).items()
        }
        super().update(normalized, **kwargs)


def create_platform_ttl_cache(
        namespace: str,
        identity: Any = "",
        *,
        maxsize: int,
        ttl: int,
) -> TTLCache:
    """创建按业务命名、按客户端身份隔离的 MoviePilot TTL 缓存。"""
    identity_text = _cache_identity(identity)
    suffix = sha256(identity_text.encode("utf-8")).hexdigest()[:16]
    return _PlatformTTLCache(
        region=f"pansearch:{str(namespace).strip(':')}:{suffix}",
        maxsize=max(1, int(maxsize or 1)),
        ttl=max(1, int(ttl or 1)),
    )


def cached_resource_call(
        cache: TTLCache,
        key: Any,
        loader: Callable[[], Any],
        *,
        locks: Sequence[Any],
        access_lock: Optional[Any] = None,
        force_refresh: bool = False,
        on_hit: Optional[Callable[[Any], None]] = None,
        on_wait_hit: Optional[Callable[[Any], None]] = None,
) -> Any:
    """双重检查缓存并串行加载同一资源，失败结果不写入缓存。"""
    if not locks:
        raise ValueError("资源缓存至少需要一个分片锁")

    def read_cached() -> Any:
        if force_refresh:
            return None
        with access_lock if access_lock is not None else nullcontext():
            cached = cache.get(key)
        return copy.deepcopy(cached) if cached is not None else None

    cached = read_cached()
    if cached is not None:
        if on_hit:
            on_hit(cached)
        return cached

    normalized_key = normalize_platform_cache_key(key)
    cache_lock = locks[hash(normalized_key) % len(locks)]
    with cache_lock:
        cached = read_cached()
        if cached is not None:
            if on_wait_hit:
                on_wait_hit(cached)
            return cached

        value = loader()
        if value is not None:
            with access_lock if access_lock is not None else nullcontext():
                cache.set(key, copy.deepcopy(value))
        return value
