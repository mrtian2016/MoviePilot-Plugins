"""阿里云盘能力注册。"""

from dataclasses import dataclass

from .client import AliPanClient
from .files import AliPanFileService
from .share import AliPanShareService
from .upload import AliPanUploadService
from ...core.cloud import CloudDriveCapability, CloudDrivePolicy, CloudDriveProvider


@dataclass
class AliPanDrive:
    client: AliPanClient

    def close(self) -> None:
        self.client.close()


def create_alipan_provider(drive: AliPanDrive) -> CloudDriveProvider:
    files = AliPanFileService(drive.client)
    upload = AliPanUploadService(drive.client, files)
    share = AliPanShareService(drive.client, files)
    return CloudDriveProvider(
        key="alipan", name="阿里云盘", config_prefix="alipan",
        resource_types=frozenset({"alipan"}),
        services={
            CloudDriveCapability.AUTHENTICATION: drive.client,
            CloudDriveCapability.ACCOUNT: drive.client,
            CloudDriveCapability.SHARE_TRANSFER: share,
            CloudDriveCapability.DIRECTORY_READ: files,
            CloudDriveCapability.FILE_QUERY: files,
            CloudDriveCapability.FILE_MUTATION: files,
            CloudDriveCapability.BATCH_FILE_MUTATION: files,
            CloudDriveCapability.LOCAL_UPLOAD: upload,
            CloudDriveCapability.FILE_DOWNLOAD: files,
            CloudDriveCapability.QRCODE_AUTH: drive.client,
        },
        policy=CloudDrivePolicy(
            pagination_mode="cursor",
            max_page_size=100,
            supports_batch=True,
            max_batch_size=100,
            max_concurrency=2,
        ),
    )
