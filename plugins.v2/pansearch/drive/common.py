"""网盘适配器共享的无状态转换和目录操作。"""

from __future__ import annotations

import time
from contextlib import nullcontext
from dataclasses import replace
from hashlib import sha256
from pathlib import PurePosixPath
from random import uniform
from threading import Lock, RLock
from typing import Any, Callable, ClassVar, Dict, Iterator, List, Mapping, Optional, Sequence

try:
    import requests
except ImportError:  # pragma: no cover - requests is a platform dependency
    requests = None

from app.core.cache import TTLCache
from app.log import logger
from app.utils.string import StringUtils

from ..core.cloud import CloudFile, DirectoryListing, DirectoryLookup
from ..utils.cache import create_platform_ttl_cache

if requests is not None:
    DRIVE_RETRY_EXCEPTIONS = (
        TimeoutError,
        ConnectionError,
        requests.exceptions.Timeout,
        requests.exceptions.ConnectionError,
    )
else:
    DRIVE_RETRY_EXCEPTIONS = (TimeoutError, ConnectionError)


class DriveRateLimiter:
    """网盘客户端共享的串行请求门控，避免同一账号突发请求触发风控。"""

    _shared: dict[tuple[str, str, float, float], "DriveRateLimiter"] = {}
    _shared_lock = RLock()

    def __init__(self, min_interval: float = 0.5, jitter_ratio: float = 0.2):
        self.min_interval = max(0.0, min(float(min_interval or 0.5), 60.0))
        self.jitter_ratio = max(0.0, min(float(jitter_ratio or 0.0), 0.5))
        self._last_request = 0.0
        self._lock = Lock()

    @classmethod
    def shared(
            cls, provider: str, identity: Any, *, min_interval: float = 0.5,
            jitter_ratio: float = 0.2,
    ) -> "DriveRateLimiter":
        identity_key = sha256(
            str(identity or "").encode("utf-8")
        ).hexdigest()[:16]
        key = (
            str(provider or "drive").strip().lower(),
            identity_key,
            round(float(min_interval or 0.5), 3),
            round(float(jitter_ratio or 0.0), 3),
        )
        with cls._shared_lock:
            limiter = cls._shared.get(key)
            if limiter is None:
                limiter = cls(
                    min_interval=min_interval,
                    jitter_ratio=jitter_ratio,
                )
                cls._shared[key] = limiter
            return limiter

    def _before_request(self) -> None:
        with self._lock:
            elapsed = time.monotonic() - self._last_request
            jitter = self.min_interval * self.jitter_ratio
            interval = self.min_interval + uniform(-jitter, jitter)
            if elapsed < interval:
                time.sleep(interval - elapsed)
            self._last_request = time.monotonic()

    def call(
            self,
            func: Callable,
            *args,
            retry_exceptions: tuple[type[BaseException], ...] = None,
            max_retries: int = 2,
            initial_delay: float = 0.5,
            backoff_factor: float = 2.0,
            **kwargs,
    ):
        """限流执行请求，并仅对调用方指定的瞬态异常指数退避重试。"""
        if retry_exceptions is None:
            retry_exceptions = DRIVE_RETRY_EXCEPTIONS
        retries = max(0, min(int(max_retries or 0), 5))
        delay = max(0.1, min(float(initial_delay or 0.5), 30.0))
        factor = max(1.0, min(float(backoff_factor or 2.0), 4.0))
        for attempt in range(retries + 1):
            self._before_request()
            try:
                return func(*args, **kwargs)
            except retry_exceptions as error:
                if attempt >= retries:
                    raise
                name = getattr(func, "__name__", type(func).__name__)
                logger.debug(
                    f"网盘请求 {name} 失败：{error}，"
                    f"{delay:.2f} 秒后第 {attempt + 1} 次重试"
                )
                time.sleep(delay)
                delay = min(delay * factor, 60.0)


def safe_int(value: Any) -> int:
    """将外部接口值转换为整数，空值和非法值按 0 处理。"""
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def format_size(value: Any) -> str:
    """通过 MoviePilot 平台工具格式化外部字节值。"""
    return StringUtils.format_size(max(0, safe_int(value)))


def extract_list(data: Any, keys: Sequence[str]) -> list:
    """从网盘响应数据中提取列表，兼容各提供方的容器字段。"""
    if isinstance(data, list):
        return data
    if isinstance(data, Mapping):
        for key in keys:
            value = data.get(key)
            if isinstance(value, list):
                return value
    return []


def normalize_path(path: str) -> str:
    value = str(path or "/").replace("\\", "/")
    return str(PurePosixPath("/" + value.lstrip("/")))


def create_directory_cache(provider: str, client: Any) -> TTLCache:
    """创建按账号隔离的统一平台目录缓存。"""
    return create_platform_ttl_cache(
        f"{provider}:directories",
        client,
        maxsize=256,
        ttl=5,
    )


class DirectoryPathCache:
    """统一维护网盘路径到目录 ID 的短期映射。"""

    def __init__(
            self,
            provider: str,
            identity: Any,
            root_directory_id: Any,
            *,
            ttl: int = 3600,
            maxsize: int = 2000,
    ):
        self.provider = str(provider or "drive")
        self.root_directory_id = (
            "" if root_directory_id is None else str(root_directory_id)
        )
        self.ttl = max(1, int(ttl or 3600))
        self.maxsize = max(16, int(maxsize or 2000))
        self._cache = create_platform_ttl_cache(
            f"{self.provider}:paths",
            identity,
            maxsize=self.maxsize,
            ttl=self.ttl,
        )
        self.set("/", self.root_directory_id)

    def get(self, path: str) -> Optional[str]:
        value = self._cache.get(normalize_path(path))
        return None if value is None else str(value)

    def set(self, path: str, directory_id: Any) -> None:
        value = "" if directory_id is None else str(directory_id)
        self._cache.set(normalize_path(path), value)

    def delete(self, path: str) -> None:
        self._cache.delete(normalize_path(path))

    def clear(self) -> None:
        self._cache.clear()
        self.set("/", self.root_directory_id)

    def stats(self) -> dict[str, int]:
        return {
            "entries": len(list(self._cache.items())),
            "limit": self.maxsize,
            "ttl_seconds": self.ttl,
        }


def create_directory_path_cache(
        provider: str,
        client: Any,
        root_directory_id: Any,
        *,
        ttl: int = 3600,
        maxsize: int = 2000,
) -> DirectoryPathCache:
    return DirectoryPathCache(
        provider,
        client,
        root_directory_id,
        ttl=ttl,
        maxsize=maxsize,
    )


def resolve_directory_path(
        path: str,
        *,
        root_directory_id: Any,
        path_cache: DirectoryPathCache,
        list_children: Callable[[str], Sequence[CloudFile]],
        create_child: Optional[Callable[[str, str], Optional[CloudFile]]] = None,
        create: bool = False,
        provider_name: str = "网盘",
        lock: Any = None,
) -> DirectoryLookup:
    """按统一规则逐级解析路径，平台只需提供目录读取和创建回调。"""
    guard = lock if lock is not None else nullcontext()
    try:
        with guard:
            current_id = "" if root_directory_id is None else str(root_directory_id)
            normalized = normalize_path(path)
            cached_directory_id = path_cache.get(normalized)
            if cached_directory_id is not None:
                return DirectoryLookup(True, cached_directory_id)
            current_path = ""
            for part in (value for value in normalized.split("/") if value):
                current_path = f"{current_path}/{part}"
                cached_id = path_cache.get(current_path)
                if cached_id is not None:
                    current_id = cached_id
                    continue
                children = list_children(current_id)
                match = next(
                    (item for item in children
                     if item.is_directory and item.name == part),
                    None,
                )
                if not match and create and create_child is not None:
                    match = create_child(part, current_id)
                    if not match:
                        match = next(
                            (item for item in list_children(current_id)
                             if item.is_directory and item.name == part),
                            None,
                        )
                if not match:
                    return DirectoryLookup(True, None)
                current_id = str(match.id)
                path_cache.set(current_path, current_id)
            return DirectoryLookup(True, current_id)
    except Exception as error:
        logger.warning(f"解析{provider_name}目录失败：{path} - {error}")
        return DirectoryLookup(False)


def iter_transfer_batches(
        values: Sequence[str], batch_size: int, batch_interval: float,
        provider_limit: int,
) -> Iterator[List[str]]:
    """按公共风控节奏切分转存请求，并遵守网盘自身单批上限。"""
    size = max(1, min(int(batch_size or 1), int(provider_limit or 1)))
    interval = max(0.0, min(float(batch_interval or 0), 60.0))
    normalized = list(dict.fromkeys(str(value) for value in values))
    for offset in range(0, len(normalized), size):
        if offset and interval:
            time.sleep(interval)
        yield normalized[offset:offset + size]


class CloudDriveFileServiceBase:
    """复用目录解析、遍历和基础文件操作，具体 API 调用由文件服务提供。"""

    root_directory_id: ClassVar[str]
    provider_name: ClassVar[str]
    client: Any

    def _get_directory_path_cache(self) -> DirectoryPathCache:
        cache = getattr(self, "_directory_path_cache", None)
        if cache is None:
            cache = create_directory_path_cache(
                getattr(self, "provider_key", self.provider_name),
                self.client,
                self.root_directory_id,
            )
            self._directory_path_cache = cache
        return cache

    def _invalidate_path_cache(self) -> None:
        self._get_directory_path_cache().clear()

    def _invalidate_directory_cache(self) -> None:
        cache = getattr(self, "_directory_cache", None)
        if cache is not None:
            cache.clear()

    def refresh_directories(self) -> None:
        """清空目录相关短缓存，使目录选择器重新读取网盘。"""
        self._invalidate_directory_cache()
        self._invalidate_path_cache()

    def _list(self, directory_id: str) -> list[CloudFile]:
        raise NotImplementedError

    def _create_folder(self, name: str, parent_id: str) -> Optional[CloudFile]:
        raise NotImplementedError

    def _is_success(self, response: Any) -> bool:
        raise NotImplementedError

    def close(self) -> None:
        self.client.close()

    def resolve_directory(self, path: str, create: bool = False) -> DirectoryLookup:
        return resolve_directory_path(
            path,
            root_directory_id=self.root_directory_id,
            path_cache=self._get_directory_path_cache(),
            list_children=self._list,
            create_child=self._create_folder,
            create=create,
            provider_name=self.provider_name,
            lock=getattr(self, "_directory_lock", None),
        )

    def list_directory(self, directory_id: str) -> DirectoryListing:
        try:
            return DirectoryListing(
                True,
                tuple(self._list(directory_id or self.root_directory_id)),
            )
        except Exception as error:
            logger.warning(f"读取{self.provider_name}目录失败：{error}")
            return DirectoryListing(False)

    def list_directories(self, path: str) -> list[Dict[str, str]]:
        lookup = self.resolve_directory(path)
        if not lookup.checked or lookup.directory_id is None:
            return []
        base = PurePosixPath(normalize_path(path))
        return [
            {
                "id": item.id,
                "name": item.name,
                "path": str(base / item.name),
            }
            for item in self._list(lookup.directory_id)
            if item.is_directory
        ]

    def list_files_recursive(self, path: str, **kwargs: Any) -> list[CloudFile]:
        lookup = self.resolve_directory(path)
        if not lookup.checked or lookup.directory_id is None:
            return []
        result: list[CloudFile] = []
        stack = [lookup.directory_id]
        while stack:
            for item in self._list(stack.pop()):
                if item.is_directory:
                    stack.append(item.id)
                else:
                    result.append(item)
        return result

    def find_file(
            self, path: str, file_name: str, **kwargs: Any
    ) -> Optional[CloudFile]:
        lookup = self.resolve_directory(path)
        if not lookup.checked or lookup.directory_id is None:
            return None
        return next(
            (
                item
                for item in self._list(lookup.directory_id)
                if item.name == file_name
            ),
            None,
        )

    def find_file_strict(self, path: str, file_name: str) -> Optional[CloudFile]:
        return self.find_file(path, file_name)

    def get_cached_file(self, path: str, file_name: str) -> Optional[CloudFile]:
        return self.find_file(path, file_name)

    def rename_file(self, path: str, item: CloudFile, target_name: str) -> bool:
        success = self._is_success(self.client.rename_file(item.id, target_name))
        if success and item.is_directory:
            self._invalidate_path_cache()
        return success

    def move_file(
            self, item: CloudFile, save_path: str, target_name: str
    ) -> Optional[CloudFile]:
        lookup = self.resolve_directory(save_path, create=True)
        if not lookup.checked or lookup.directory_id is None:
            return None
        if not self._is_success(self.client.move_files([item.id], lookup.directory_id)):
            return None
        if item.is_directory:
            self._invalidate_path_cache()
        if target_name and target_name != item.name:
            if not self._is_success(self.client.rename_file(item.id, target_name)):
                return None
        return self.find_file(save_path, target_name or item.name)

    def delete_file(self, file_id: str) -> bool:
        return self._is_success(self.client.delete_files([file_id]))

    def _log_batch_failures(self, action: str, failures: list[str]) -> None:
        if not failures:
            return
        logger.warning(
            f"{self.provider_name}批量{action}异常 {len(failures)} 项，"
            f"首项：{failures[0]}"
        )

    def rename_files(self, path: str, items: dict) -> dict[str, CloudFile]:
        """顺序执行批量重命名，原生支持批量的平台可覆盖此方法。"""
        renamed = {}
        failures = []
        for key, value in dict(items or {}).items():
            item = value.get("item") if isinstance(value, dict) else None
            target_name = str(value.get("target_name") or "") if isinstance(
                value, dict
            ) else ""
            if not item or not target_name:
                continue
            try:
                if self.rename_file(path, item, target_name):
                    renamed[str(key)] = replace(item, name=target_name)
            except Exception as error:
                failures.append(
                    f"{getattr(item, 'name', item)} -> {target_name}，{error}"
                )
        self._log_batch_failures("重命名", failures)
        return renamed

    def move_files(
            self, items: dict[str, CloudFile], save_path: str
    ) -> dict[str, CloudFile]:
        """顺序执行批量移动，并返回成功项供上层统一提交。"""
        moved = {}
        failures = []
        for key, item in dict(items or {}).items():
            if not item:
                continue
            try:
                target = self.move_file(item, save_path, item.name)
                if target:
                    moved[str(key)] = target
            except Exception as error:
                failures.append(
                    f"{getattr(item, 'name', item)} -> {save_path}，{error}"
                )
        self._log_batch_failures("移动", failures)
        return moved

    def delete_files(self, file_ids: list[str]) -> set[str]:
        """顺序执行批量删除，失败项不计入结果并由上层重试。"""
        deleted = set()
        failures = []
        for file_id in dict.fromkeys(
                str(value) for value in (file_ids or []) if str(value or "")
        ):
            if not file_id:
                continue
            try:
                if self.delete_file(file_id):
                    deleted.add(file_id)
            except Exception as error:
                failures.append(f"{file_id}，{error}")
        self._log_batch_failures("删除", failures)
        return deleted
