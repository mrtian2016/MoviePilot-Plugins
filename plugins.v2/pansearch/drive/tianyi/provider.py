"""天翼云盘能力适配。"""

from dataclasses import dataclass

from .client import TianyiClient
from .files import TianyiFileService
from .share import TianyiShareService
from .upload import TianyiUploadService
from ...core.cloud import CloudDriveCapability, CloudDrivePolicy, CloudDriveProvider
from ...core.transfer import LocalRapidUploadAdapter


@dataclass
class TianyiDrive:
    client: TianyiClient

    def close(self):
        self.client.close()


def create_tianyi_provider(drive: TianyiDrive) -> CloudDriveProvider:
    files = TianyiFileService(drive.client)
    upload = TianyiUploadService(drive.client, files)
    share = TianyiShareService(drive.client, files)
    return CloudDriveProvider(
        key="tianyi",
        name="天翼云盘",
        config_prefix="tianyi",
        resource_types=frozenset({"tianyi", "189", "ed2k", "magnet"}),
        services={
            CloudDriveCapability.AUTHENTICATION: drive.client,
            CloudDriveCapability.ACCOUNT: drive.client,
            CloudDriveCapability.QRCODE_AUTH: drive.client,
            CloudDriveCapability.DIRECTORY_READ: files,
            CloudDriveCapability.FILE_QUERY: files,
            CloudDriveCapability.FILE_MUTATION: files,
            CloudDriveCapability.BATCH_FILE_MUTATION: files,
            CloudDriveCapability.SHARE_TRANSFER: share,
            CloudDriveCapability.LOCAL_UPLOAD: upload,
            CloudDriveCapability.RAPID_UPLOAD: LocalRapidUploadAdapter(upload, files, frozenset({"md5"})),
            CloudDriveCapability.FILE_DOWNLOAD: files,
        },
        policy=CloudDrivePolicy(
            pagination_mode="offset",
            max_page_size=100,
            supports_batch=True,
            max_batch_size=100,
            max_concurrency=2,
        ),
    )
