"""网盘提供方能力规范与运行时注册表。"""

from dataclasses import dataclass, field
from enum import Enum
from functools import wraps
from threading import BoundedSemaphore, local
from typing import Any, Callable, ClassVar, Dict, FrozenSet, Iterable, Iterator, Mapping, Optional, Protocol, \
    runtime_checkable


class CloudDriveCapability(str, Enum):
    """订阅流程可按需使用的独立网盘能力。"""

    AUTHENTICATION = "authentication"
    ACCOUNT = "account"
    SHARE_TRANSFER = "share_transfer"
    OFFLINE_DOWNLOAD = "offline_download"
    DIRECTORY_READ = "directory_read"
    FILE_QUERY = "file_query"
    FILE_MUTATION = "file_mutation"
    CHECKSUM_RENAME = "checksum_rename"
    BATCH_FILE_MUTATION = "batch_file_mutation"
    PLAYBACK_REFERENCE = "playback_reference"
    OFFLINE_TASKS = "offline_tasks"
    LOCAL_UPLOAD = "local_upload"
    RAPID_UPLOAD = "rapid_upload"
    FILE_DOWNLOAD = "file_download"
    QRCODE_AUTH = "qrcode_auth"
    CACHE_MAINTENANCE = "cache_maintenance"


class CloudDriveCapabilityError(RuntimeError):
    """当前网盘不支持调用方要求的能力。"""


@dataclass(frozen=True)
class CloudDrivePolicy:
    """提供方级执行边界，调用层据此决定并发、分页和批量方式。"""

    pagination_mode: str = "none"
    max_page_size: int = 0
    supports_batch: bool = False
    max_batch_size: int = 1
    supports_cancel: bool = False
    max_concurrency: int = 1
    cache_ttl_seconds: Mapping[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        mode = str(self.pagination_mode or "none").strip().lower()
        if mode not in {"none", "offset", "cursor"}:
            raise ValueError(f"不支持的网盘分页模式：{mode}")
        object.__setattr__(self, "pagination_mode", mode)
        object.__setattr__(self, "max_page_size", max(0, int(self.max_page_size or 0)))
        object.__setattr__(self, "max_batch_size", max(1, int(self.max_batch_size or 1)))
        object.__setattr__(self, "max_concurrency", max(1, int(self.max_concurrency or 1)))
        object.__setattr__(
            self,
            "cache_ttl_seconds",
            {
                str(key): max(0, int(value or 0))
                for key, value in dict(self.cache_ttl_seconds or {}).items()
            },
        )


@dataclass(frozen=True)
class CloudFile(Mapping[str, Any]):
    """网盘工作流使用的统一文件模型，提供方私有响应仅保留在适配器内部。"""

    id: str
    name: str
    is_directory: bool
    size: int = 0
    sha1: str = ""
    md5: str = ""
    playback_values: Mapping[str, str] = field(default_factory=dict)
    native: Any = field(default=None, repr=False, compare=False)
    _MAPPING_KEYS: ClassVar[tuple[str, ...]] = (
        "id", "name", "is_dir", "size", "sha1", "md5",
    )

    def __getitem__(self, key: str) -> Any:
        if key == "id":
            return self.id
        if key == "name":
            return self.name
        if key == "is_dir":
            return self.is_directory
        if key == "size":
            return self.size
        if key == "sha1":
            return self.sha1
        if key == "md5":
            return self.md5
        raise KeyError(key)

    def __iter__(self) -> Iterator[str]:
        return iter(self._MAPPING_KEYS)

    def __len__(self) -> int:
        return 6

    def get(self, key: str, default: Any = None) -> Any:
        try:
            return self[key]
        except KeyError:
            return default

    def as_native_dict(self) -> Dict[str, Any]:
        """Expand into a mutable dict: standard fields plus native fields."""
        merged: Dict[str, Any] = {}
        if isinstance(self.native, Mapping):
            merged.update(dict(self.native))
        merged.update({
            "id": self.id,
            "name": self.name,
            "is_dir": self.is_directory,
            "size": self.size,
            "sha1": self.sha1,
            "md5": self.md5,
        })
        for key, value in self.playback_values.items():
            merged.setdefault(str(key), value)
        return merged


def native_dict(item: Any) -> dict:
    """Expand a CloudFile (or Mapping) into a plain mutable dict.

    CloudFile is a frozen Mapping whose native payload may be None; passing
    it straight into mutation helpers crashes on dict(None). Always go
    through this helper when handing items to manager-level mutators.
    """
    if isinstance(item, CloudFile):
        return item.as_native_dict()
    if isinstance(item, Mapping):
        return dict(item)
    return dict(item or {})


@dataclass(frozen=True)
class DirectoryLookup:
    """目录解析结果；checked=False 表示提供方未能完成确认。"""

    checked: bool
    directory_id: str | None = None


@dataclass(frozen=True)
class DirectoryListing:
    """目录读取结果；空目录与读取失败由 checked 明确区分。"""

    checked: bool
    files: tuple[CloudFile, ...] = ()


@dataclass(frozen=True)
class RapidUploadResult:
    """秒传探测结果；reused=False 表示可安全回退普通上传。"""

    reused: bool
    file: CloudFile | None = None
    message: str = ""


@dataclass
class ShareLinkStatus:
    """网盘分享链接的统一可用状态。"""

    is_valid: bool = False
    is_expired: bool = False
    is_cancelled: bool = False
    is_deleted: bool = False
    error_code: int = 0
    error_message: str = ""
    file_count: int = 0
    share_info: Dict[str, Any] = field(default_factory=dict)

    @property
    def status_text(self) -> str:
        if self.is_valid:
            return "有效"
        if self.is_expired:
            return "已过期"
        if self.is_cancelled:
            return "已取消"
        if self.is_deleted:
            return "文件已删除"
        return self.error_message or "未知状态"


@runtime_checkable
class AuthenticationOperations(Protocol):
    def check_login(self) -> bool: ...


@runtime_checkable
class AccountOperations(Protocol):
    def get_account_info(self) -> Dict[str, Any]: ...


@runtime_checkable
class ShareTransferOperations(Protocol):
    def extract_share_info(self, share_url: str) -> Dict[str, Any]: ...

    def check_share_status(self, share_url: str) -> ShareLinkStatus: ...

    def list_share_files(self, share_url: str, **kwargs: Any) -> list: ...

    def list_share_directory(
            self, share_url: str, parent_id: str = ""
    ) -> list: ...

    def transfer_share(self, share_url: str, save_path: str) -> bool: ...

    def transfer_file(
            self, share_url: str, file_id: str, save_path: str, target_name: str
    ) -> bool: ...

    def transfer_files_batch(
            self, share_url: str, file_ids: list, save_path: str, **kwargs: Any
    ) -> tuple: ...


@runtime_checkable
class OfflineDownloadOperations(Protocol):
    def is_offline_url(self, url: str) -> bool: ...

    def is_ed2k_url(self, url: str) -> bool: ...

    def is_magnet_url(self, url: str) -> bool: ...

    def parse_ed2k_link(self, url: str) -> Dict[str, Any]: ...

    def parse_magnet_link(self, url: str, **kwargs: Any) -> Dict[str, Any]: ...

    # p115 实现返回任务 info_hash 字符串（失败为空串，真值语义等同成功布尔）。
    def add_offline_download(self, url: str, save_path: str, **kwargs: Any) -> bool: ...

    def get_offline_tasks(self, force: bool = False) -> list[Dict[str, Any]]: ...


@runtime_checkable
class DirectoryReadOperations(Protocol):
    def refresh_directories(self) -> None: ...

    def resolve_directory(
            self, path: str, create: bool = False
    ) -> DirectoryLookup: ...

    def list_directory(self, directory_id: str) -> DirectoryListing: ...

    def list_directories(self, path: str) -> list[Dict[str, str]]: ...


@runtime_checkable
class FileQueryOperations(Protocol):
    def list_files_recursive(self, path: str, **kwargs: Any) -> list[CloudFile]: ...

    def find_file(
            self, path: str, file_name: str, **kwargs: Any
    ) -> CloudFile | None: ...

    def find_file_strict(self, path: str, file_name: str) -> CloudFile | None: ...

    def get_cached_file(self, path: str, file_name: str) -> CloudFile | None: ...


@runtime_checkable
class FileMutationOperations(Protocol):
    def rename_file(
            self, path: str, item: CloudFile, target_name: str
    ) -> bool: ...

    def move_file(
            self, item: CloudFile, save_path: str, target_name: str
    ) -> CloudFile | None: ...

    def delete_file(self, file_id: str) -> bool: ...


@runtime_checkable
class ChecksumRenameOperations(Protocol):
    def rename_file_by_checksum(
            self,
            path: str,
            checksum: str,
            target_name: str,
            algorithm: str = "sha1",
            **kwargs: Any,
    ) -> bool: ...


@runtime_checkable
class BatchFileMutationOperations(Protocol):
    def rename_files(self, path: str, items: dict) -> dict[str, CloudFile]: ...

    def move_files(
            self, items: dict[str, CloudFile], save_path: str
    ) -> dict[str, CloudFile]: ...

    def delete_files(self, file_ids: list[str]) -> set[str]: ...


@runtime_checkable
class PlaybackReferenceOperations(Protocol):
    @property
    def template_variables(self) -> FrozenSet[str]: ...

    def reference_values(self, file_item: CloudFile) -> Dict[str, str]: ...


@runtime_checkable
class OfflineTaskOperations(Protocol):
    def get_offline_task_list_snapshot(
            self, force: bool = False, task_ids: Optional[set[str]] = None
    ) -> Dict[str, Any]: ...

    def get_offline_tasks_snapshot(self, force: bool = False) -> Dict[str, Any]: ...

    def restart_offline_task(self, task_id: str) -> bool: ...

    def delete_offline_task(self, task_id: str, **kwargs: Any) -> bool: ...

    def delete_offline_tasks(self, task_ids: list, **kwargs: Any) -> int: ...


@runtime_checkable
class LocalUploadOperations(Protocol):
    def upload_file(
            self,
            local_path: str,
            save_path: str,
            target_name: str = "",
            file_sha1: str = "",
            progress_callback: Optional[Callable[[int, int], None]] = None,
    ) -> bool: ...


@runtime_checkable
class RapidUploadOperations(Protocol):
    """目标网盘按校验和尝试秒传，失败时由编排器决定是否回退。"""

    @property
    def algorithms(self) -> FrozenSet[str]: ...

    def upload_by_hash(
            self, local_path: str, save_path: str, target_name: str,
            algorithm: str, checksum: str, size: int = 0,
            progress_callback: Optional[Callable[[int, int], None]] = None,
    ) -> RapidUploadResult: ...


@runtime_checkable
class FileDownloadOperations(Protocol):
    def resolve_download_link(
            self, file_item: CloudFile,
    ) -> tuple[str, Mapping[str, str]]: ...

    def download_file(
            self, file_item: CloudFile, local_path: str,
            progress_callback: Optional[Callable[[int, int], None]] = None,
            stop_requested: Optional[Callable[[], bool]] = None,
            preserve_partial: bool = False,
            download_threads: int = 5,
    ) -> str: ...

@runtime_checkable
class QrCodeAuthOperations(Protocol):
    def create_qrcode_login(self, client_type: str) -> Dict[str, Any]: ...

    def check_qrcode_login(self, **kwargs: Any) -> Dict[str, Any]: ...


@runtime_checkable
class CacheMaintenanceOperations(Protocol):
    def get_cache_stats(self) -> Dict[str, Any]: ...

    def clear_cache(self) -> Dict[str, int]: ...


CAPABILITY_CONTRACTS = {
    CloudDriveCapability.AUTHENTICATION: AuthenticationOperations,
    CloudDriveCapability.ACCOUNT: AccountOperations,
    CloudDriveCapability.SHARE_TRANSFER: ShareTransferOperations,
    CloudDriveCapability.OFFLINE_DOWNLOAD: OfflineDownloadOperations,
    CloudDriveCapability.DIRECTORY_READ: DirectoryReadOperations,
    CloudDriveCapability.FILE_QUERY: FileQueryOperations,
    CloudDriveCapability.FILE_MUTATION: FileMutationOperations,
    CloudDriveCapability.CHECKSUM_RENAME: ChecksumRenameOperations,
    CloudDriveCapability.BATCH_FILE_MUTATION: BatchFileMutationOperations,
    CloudDriveCapability.PLAYBACK_REFERENCE: PlaybackReferenceOperations,
    CloudDriveCapability.OFFLINE_TASKS: OfflineTaskOperations,
    CloudDriveCapability.LOCAL_UPLOAD: LocalUploadOperations,
    CloudDriveCapability.RAPID_UPLOAD: RapidUploadOperations,
    CloudDriveCapability.FILE_DOWNLOAD: FileDownloadOperations,
    CloudDriveCapability.QRCODE_AUTH: QrCodeAuthOperations,
    CloudDriveCapability.CACHE_MAINTENANCE: CacheMaintenanceOperations,
}


def _implements_contract(service: Any, contract: type) -> bool:
    """验证能力对象的公开成员"""

    for name, declaration in contract.__dict__.items():
        if name.startswith("_"):
            continue
        try:
            value = getattr(service, name)
        except AttributeError:
            return False
        if callable(declaration) and not callable(value):
            return False
    return True


class _ProviderIoGate:
    """限制同一 provider 的顶层网盘 IO，并允许同线程嵌套调用。"""

    def __init__(self, max_concurrency: int):
        self._semaphore = BoundedSemaphore(max(1, int(max_concurrency or 1)))
        self._local = local()

    def call(self, func: Callable, *args: Any, **kwargs: Any) -> Any:
        depth = int(getattr(self._local, "depth", 0) or 0)
        if depth:
            return func(*args, **kwargs)
        with self._semaphore:
            self._local.depth = depth + 1
            try:
                return func(*args, **kwargs)
            finally:
                self._local.depth = depth


class _ProviderServiceProxy:
    """为能力对象的公开方法统一注入 provider IO 并发门。"""

    def __init__(self, service: Any, gate: _ProviderIoGate):
        self._service = service
        self._gate = gate
        self._method_cache: Dict[str, Callable[..., Any]] = {}

    def __getattr__(self, name: str) -> Any:
        cached = self._method_cache.get(name)
        if cached is not None:
            return cached
        value = getattr(self._service, name)
        if name.startswith("_") or not callable(value):
            return value

        @wraps(value)
        def guarded(*args: Any, **kwargs: Any) -> Any:
            return self._gate.call(value, *args, **kwargs)

        self._method_cache[name] = guarded
        return guarded


@dataclass(frozen=True)
class CloudDriveProvider:
    """一个网盘及其实际具备的能力集合。

    同一个服务对象可以实现多个能力；未注册的能力不会出现在提供方上，
    调用方必须通过 :meth:`require` 获取后再使用。
    """

    key: str
    name: str
    services: Mapping[CloudDriveCapability, Any]
    resource_types: FrozenSet[str] = frozenset()
    config_prefix: str = ""
    policy: CloudDrivePolicy = field(default_factory=CloudDrivePolicy)
    _io_gate: _ProviderIoGate = field(init=False, repr=False, compare=False)
    _service_proxies: Mapping[CloudDriveCapability, Any] = field(
        init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        key = str(self.key or "").strip().lower()
        if not key:
            raise ValueError("网盘提供方 key 不能为空")
        object.__setattr__(self, "key", key)
        object.__setattr__(
            self,
            "resource_types",
            frozenset(
                str(value or "").strip().lower()
                for value in self.resource_types
                if str(value or "").strip()
            ),
        )
        services = dict(self.services)
        mutations = services.get(CloudDriveCapability.FILE_MUTATION)
        if (
                mutations
                and CloudDriveCapability.BATCH_FILE_MUTATION not in services
                and _implements_contract(mutations, BatchFileMutationOperations)
        ):
            services[CloudDriveCapability.BATCH_FILE_MUTATION] = mutations
        object.__setattr__(self, "services", services)
        io_gate = _ProviderIoGate(self.policy.max_concurrency)
        io_capabilities = {
            CloudDriveCapability.SHARE_TRANSFER,
            CloudDriveCapability.OFFLINE_DOWNLOAD,
            CloudDriveCapability.DIRECTORY_READ,
            CloudDriveCapability.FILE_QUERY,
            CloudDriveCapability.FILE_MUTATION,
            CloudDriveCapability.CHECKSUM_RENAME,
            CloudDriveCapability.BATCH_FILE_MUTATION,
            CloudDriveCapability.OFFLINE_TASKS,
            CloudDriveCapability.LOCAL_UPLOAD,
            CloudDriveCapability.RAPID_UPLOAD,
            CloudDriveCapability.FILE_DOWNLOAD,
        }
        object.__setattr__(self, "_io_gate", io_gate)
        object.__setattr__(
            self,
            "_service_proxies",
            {
                capability: _ProviderServiceProxy(service, io_gate)
                for capability, service in self.services.items()
                if capability in io_capabilities
            },
        )
        for capability, service in self.services.items():
            contract = CAPABILITY_CONTRACTS.get(capability)
            if contract is None:
                raise ValueError(f"未定义的网盘能力：{capability}")
            if not _implements_contract(service, contract):
                raise TypeError(
                    f"{self.name}的 {capability.value} 服务不符合接口 {contract.__name__}"
                )

    @property
    def capabilities(self) -> FrozenSet[CloudDriveCapability]:
        return frozenset(self.services)

    def supports(self, capability: CloudDriveCapability) -> bool:
        return capability in self.services

    def supports_resource_type(self, resource_type: str) -> bool:
        return str(resource_type or "").strip().lower() in self.resource_types

    def require(self, capability: CloudDriveCapability) -> Any:
        service = self.services.get(capability)
        if service is None:
            raise CloudDriveCapabilityError(
                f"{self.name}不支持能力：{capability.value}"
            )
        return self._service_proxies.get(capability, service)


class CloudDriveRegistry:
    """按稳定 key 管理网盘提供方，不隐式选择第一个实现。"""

    def __init__(self, providers: Iterable[CloudDriveProvider] = ()):
        self._providers: Dict[str, CloudDriveProvider] = {}
        for provider in providers:
            self.register(provider)

    def register(self, provider: CloudDriveProvider, replace: bool = False) -> None:
        key = str(provider.key or "").strip().lower()
        if not key:
            raise ValueError("网盘提供方 key 不能为空")
        if key in self._providers and not replace:
            raise ValueError(f"网盘提供方重复注册：{key}")
        self._providers[key] = provider

    def get(self, key: str) -> CloudDriveProvider:
        normalized = str(key or "").strip().lower()
        provider = self._providers.get(normalized)
        if provider is None:
            raise KeyError(f"网盘提供方未注册：{normalized or '<empty>'}")
        return provider

    def available(self) -> list:
        return list(self._providers.values())
