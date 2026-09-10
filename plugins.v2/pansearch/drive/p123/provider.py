"""123 网盘能力适配。"""

from dataclasses import dataclass
from typing import Dict, FrozenSet

from .client import P123ClientManager
from .files import P123FileService
from .offline import P123OfflineService
from .share import P123ShareService
from .upload import P123UploadService
from ...core.cloud import (
    CloudDriveCapability,
    CloudDrivePolicy,
    CloudDriveProvider,
    CloudFile,
)
from ...core.transfer import LocalRapidUploadAdapter


@dataclass
class P123Drive:
    client: P123ClientManager
    page_size: int = 100

    def close(self) -> None:
        self.client.close()


@dataclass(frozen=True)
class P123PlaybackReference:
    template_variables: FrozenSet[str] = frozenset({
        "file_id", "md5", "size", "s3_key_flag"
    })

    @staticmethod
    def reference_values(file_item: CloudFile) -> Dict[str, str]:
        return dict(file_item.playback_values)


def create_p123_provider(drive: P123Drive) -> CloudDriveProvider:
    files = P123FileService(drive.client, drive.page_size)
    share = P123ShareService(drive.client, files)
    offline = P123OfflineService(drive.client, files)
    upload = P123UploadService(drive.client, files)
    playback_reference = P123PlaybackReference()
    return CloudDriveProvider(
        key="123",
        name="123网盘",
        config_prefix="p123",
        resource_types=frozenset({"123", "ed2k", "magnet"}),
        services={
            CloudDriveCapability.AUTHENTICATION: drive.client,
            CloudDriveCapability.ACCOUNT: drive.client,
            CloudDriveCapability.SHARE_TRANSFER: share,
            CloudDriveCapability.OFFLINE_DOWNLOAD: offline,
            CloudDriveCapability.DIRECTORY_READ: files,
            CloudDriveCapability.FILE_QUERY: files,
            CloudDriveCapability.FILE_MUTATION: files,
            CloudDriveCapability.BATCH_FILE_MUTATION: files,
            CloudDriveCapability.PLAYBACK_REFERENCE: playback_reference,
            CloudDriveCapability.OFFLINE_TASKS: offline,
            CloudDriveCapability.LOCAL_UPLOAD: upload,
            CloudDriveCapability.RAPID_UPLOAD: LocalRapidUploadAdapter(upload, files, frozenset({"md5"})),
            CloudDriveCapability.FILE_DOWNLOAD: files,
            CloudDriveCapability.QRCODE_AUTH: drive.client,
        },
        policy=CloudDrivePolicy(
            pagination_mode="offset",
            max_page_size=100,
            supports_batch=True,
            max_batch_size=100,
            max_concurrency=2,
        ),
    )
