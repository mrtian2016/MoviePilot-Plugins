"""阿里云盘上传能力，复用AliPan 的 Open API 实现。"""

from __future__ import annotations

import base64
import hashlib
import math
import time
from pathlib import Path


class AliPanUploadService:
    CHUNK_SIZE = 20 * 1024 * 1024

    def __init__(self, client, files):
        self.client = client
        self.files = files

    def upload_file(
            self, local_path: str, save_path: str, target_name: str = "",
            file_sha1: str = "", progress_callback=None,
    ) -> bool:
        source = Path(local_path)
        if not source.is_file() or source.stat().st_size <= 0:
            return False
        lookup = self.files.resolve_directory(save_path, create=True)
        directory = self.files.native_item(lookup.directory_id)
        if not lookup.checked or not directory:
            return False

        name = target_name or source.name
        total = source.stat().st_size
        sha1 = hashlib.sha1()
        with source.open("rb") as handle:
            while chunk := handle.read(self.CHUNK_SIZE):
                sha1.update(chunk)
        proof_offset = int(
            hashlib.md5(self.client.access_token.encode()).hexdigest()[:16], 16
        ) % total
        with source.open("rb") as handle:
            handle.seek(proof_offset)
            proof_code = base64.b64encode(
                handle.read(min(8, total - proof_offset))
            ).decode()
        part_count = math.ceil(total / self.CHUNK_SIZE)
        create_payload = {
            "drive_id": self.client.drive_id,
            "parent_file_id": str(directory.get("file_id") or "root"),
            "name": name,
            "type": "file",
            "check_name_mode": "auto_rename",
            "size": total,
            "content_hash_name": "sha1",
            "content_hash": sha1.hexdigest().upper(),
            "proof_code": proof_code,
            "proof_version": "v1",
            "part_info_list": [
                {"part_number": number}
                for number in range(1, part_count + 1)
            ],
        }
        created = self.client.request(
            "/adrive/v2/file/createWithFolders", create_payload
        )
        if created.get("rapid_upload"):
            if progress_callback:
                progress_callback(total, total)
            return True

        file_id = str(created.get("file_id") or "")
        upload_id = str(created.get("upload_id") or "")
        parts = list(created.get("part_info_list") or [])
        if not file_id or not upload_id or not parts:
            return False
        done = 0
        with source.open("rb") as handle:
            for part in parts:
                number = int(part.get("part_number") or 0)
                start = (number - 1) * self.CHUNK_SIZE
                size = min(self.CHUNK_SIZE, total - start)
                handle.seek(start)
                payload = handle.read(size)
                upload_url = str(part.get("upload_url") or "")
                for attempt in range(3):
                    if attempt:
                        refreshed = self.client.request("/v2/file/get_upload_url", {
                            "drive_id": self.client.drive_id,
                            "file_id": file_id,
                            "upload_id": upload_id,
                            "part_info_list": [{"part_number": number}],
                        }).get("part_info_list") or []
                        upload_url = str((refreshed[0] if refreshed else {}).get(
                            "upload_url"
                        ) or "")
                    response = self.client.rate_limiter.call(
                        self.client.session.put,
                        upload_url,
                        data=payload,
                        headers={"Content-Type": "application/octet-stream"},
                        timeout=self.client.timeout,
                    )
                    if response.status_code in (200, 409):
                        break
                    if attempt == 2:
                        response.raise_for_status()
                    time.sleep(2 ** attempt)
                done += len(payload)
                if progress_callback:
                    progress_callback(done, total)
        self.client.request("/v2/file/complete", {
            "drive_id": self.client.drive_id,
            "file_id": file_id,
            "upload_id": upload_id,
            "ignoreError": True,
        })
        return self.files.find_file(save_path, name) is not None
