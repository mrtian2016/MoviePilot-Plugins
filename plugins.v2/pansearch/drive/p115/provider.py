"""115 网盘能力适配。"""

from dataclasses import dataclass
from typing import Any, Dict, FrozenSet

from .files import (
    P115BatchFileMutation,
    P115DirectoryReader,
    P115FileService,
    P115FileMutation,
    P115FileQuery,
)
from .offline import OfflineDownloadService
from .share import ShareService
from .upload import P115UploadService
from ...core.cloud import (
    CloudDriveCapability,
    CloudDrivePolicy,
    CloudDriveProvider,
    CloudFile,
)
from ...core.transfer import LocalRapidUploadAdapter


@dataclass(frozen=True)
class P115PlaybackReference:
    """将 115 文件的 pickcode 映射为通用 STRM 模板变量。"""

    template_variables: FrozenSet[str] = frozenset({"pickcode"})

    @staticmethod
    def reference_values(file_item: CloudFile) -> Dict[str, str]:
        return dict(file_item.playback_values)


@dataclass(frozen=True)
class P115CacheMaintenance:
    manager: Any

    def get_cache_stats(self) -> Dict[str, Any]:
        return self.manager.get_cache_stats()

    def clear_cache(self) -> Dict[str, int]:
        return self.manager.clear_cache()


def create_p115_provider(manager: Any) -> CloudDriveProvider:
    """把 P115ClientManager 暴露为其真实具备的分能力服务。"""
    files = manager._get_component(P115FileService)
    share = manager._get_component(ShareService)
    offline = manager._get_component(OfflineDownloadService)
    upload = manager._get_component(P115UploadService)
    directory_reader = P115DirectoryReader(files)
    file_query = P115FileQuery(files)
    file_mutation = P115FileMutation(files)
    batch_mutation = P115BatchFileMutation(files)
    cache_maintenance = P115CacheMaintenance(manager)

    return CloudDriveProvider(
        key="115",
        name="115网盘",
        config_prefix="p115",
        resource_types=frozenset({"115", "ed2k", "magnet"}),
        services={
            CloudDriveCapability.AUTHENTICATION: manager,
            CloudDriveCapability.ACCOUNT: manager,
            CloudDriveCapability.SHARE_TRANSFER: share,
            CloudDriveCapability.OFFLINE_DOWNLOAD: offline,
            CloudDriveCapability.DIRECTORY_READ: directory_reader,
            CloudDriveCapability.FILE_QUERY: file_query,
            CloudDriveCapability.FILE_MUTATION: file_mutation,
            CloudDriveCapability.CHECKSUM_RENAME: file_mutation,
            CloudDriveCapability.BATCH_FILE_MUTATION: batch_mutation,
            CloudDriveCapability.PLAYBACK_REFERENCE: P115PlaybackReference(),
            CloudDriveCapability.OFFLINE_TASKS: offline,
            CloudDriveCapability.LOCAL_UPLOAD: upload,
            CloudDriveCapability.RAPID_UPLOAD: LocalRapidUploadAdapter(upload, file_query, frozenset({"sha1"})),
            CloudDriveCapability.FILE_DOWNLOAD: file_query,
            CloudDriveCapability.QRCODE_AUTH: manager.__class__,
            CloudDriveCapability.CACHE_MAINTENANCE: cache_maintenance,
        },
        policy=CloudDrivePolicy(
            pagination_mode="offset",
            max_page_size=1000,
            supports_batch=True,
            max_batch_size=115,
            supports_cancel=True,
            max_concurrency=3,
            cache_ttl_seconds={
                "path": int(getattr(manager, "DEFAULT_PATH_CACHE_TTL", 3600)),
                "share_status": int(getattr(manager, "_share_cache_ttl", 1800)),
                "offline_tasks": int(getattr(manager, "OFFLINE_TASK_CACHE_TTL", 600)),
            },
        ),
    )
