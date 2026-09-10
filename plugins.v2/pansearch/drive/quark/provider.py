"""夸克网盘能力适配。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, FrozenSet

from .client import QuarkClient
from .files import QuarkFileService
from .share import QuarkShareService
from .upload import QuarkUploadService
from ...core.cloud import (
    CloudDriveCapability,
    CloudDrivePolicy,
    CloudDriveProvider,
    CloudFile,
)
from ...core.transfer import LocalRapidUploadAdapter


@dataclass
class QuarkDrive:
    client: QuarkClient
    page_size: int = 100

    def close(self) -> None:
        """关闭底层 HTTP 会话。"""
        self.client.close()


@dataclass(frozen=True)
class QuarkPlaybackReference:
    template_variables: FrozenSet[str] = frozenset({"file_id"})

    @staticmethod
    def reference_values(file_item: CloudFile) -> Dict[str, str]:
        return dict(file_item.playback_values)


def create_quark_provider(drive: QuarkDrive) -> CloudDriveProvider:
    files = QuarkFileService(drive.client, drive.page_size)
    share = QuarkShareService(drive.client, files)
    upload = QuarkUploadService(drive.client, files)
    playback_reference = QuarkPlaybackReference()

    return CloudDriveProvider(
        key="quark",
        name="夸克网盘",
        config_prefix="quark",
        resource_types=frozenset({"quark"}),
        services={
            CloudDriveCapability.AUTHENTICATION: drive.client,
            CloudDriveCapability.ACCOUNT: drive.client,
            CloudDriveCapability.SHARE_TRANSFER: share,
            CloudDriveCapability.DIRECTORY_READ: files,
            CloudDriveCapability.FILE_QUERY: files,
            CloudDriveCapability.FILE_MUTATION: files,
            CloudDriveCapability.BATCH_FILE_MUTATION: files,
            CloudDriveCapability.PLAYBACK_REFERENCE: playback_reference,
            CloudDriveCapability.LOCAL_UPLOAD: upload,
            CloudDriveCapability.RAPID_UPLOAD: LocalRapidUploadAdapter(upload, files, frozenset({"md5"})),
            CloudDriveCapability.FILE_DOWNLOAD: files,
            CloudDriveCapability.QRCODE_AUTH: QuarkClient,
        },
        policy=CloudDrivePolicy(
            pagination_mode="offset",
            max_page_size=100,
            supports_batch=True,
            max_batch_size=50,
            max_concurrency=2,
        ),
    )
