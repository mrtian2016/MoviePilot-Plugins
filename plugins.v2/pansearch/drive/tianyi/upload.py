"""天翼 MD5 秒传与 10MiB 分片上传。"""

from __future__ import annotations

import base64
import hashlib
import time
from pathlib import Path
from urllib.parse import unquote


class TianyiUploadService:
    SLICE_SIZE = 10 * 1024 * 1024

    def __init__(self, client, files):
        self.client = client
        self.files = files

    def try_rapid_upload(
            self, local_path: str, save_path: str, target_name: str,
            algorithm: str, checksum: str, size: int,
    ) -> bool:
        if algorithm != "md5" or int(size or 0) <= 0:
            return False
        lookup = self.files.resolve_directory(save_path, create=True)
        if not lookup.directory_id:
            raise RuntimeError(f"天翼本地上传目录不可用：{save_path}")
        created = self.client.request(
            "POST", "https://cloud.189.cn/createUploadFile.action",
            data={
                "parentFolderId": lookup.directory_id,
                "fileName": target_name,
                "size": str(size),
                "md5": checksum.lower(),
                "opertype": "3",
                "flag": "1",
                "resumePolicy": "1",
                "isLog": "0",
            },
        )
        data = created.get("data") if isinstance(created.get("data"), dict) else created
        if int(data.get("fileDataExists") or 0) != 1:
            return False
        upload_id = str(data.get("uploadFileId") or "")
        commit_url = str(data.get("fileCommitUrl") or "")
        if not upload_id or not commit_url:
            raise RuntimeError("天翼秒传响应缺少提交信息")
        if commit_url.startswith("/"):
            commit_url = "https://cloud.189.cn" + commit_url
        self.client.request("POST", commit_url, data={
            "opertype": "3",
            "resumePolicy": "1",
            "uploadFileId": upload_id,
            "isLog": "0",
        })
        for index in range(10):
            if self.files.find_file(save_path, target_name):
                return True
            if index < 9:
                time.sleep(0.5)
        raise RuntimeError("天翼秒传完成后未找到目标文件")

    def upload_file(self, local_path: str, save_path: str, target_name: str = "",
                    file_sha1: str = "", progress_callback=None) -> bool:
        source = Path(local_path)
        if not source.is_file():
            return False
        lookup = self.files.resolve_directory(save_path, create=True)
        if not lookup.directory_id:
            return False
        name = target_name or source.name
        chunks, full = [], hashlib.md5()
        with source.open("rb") as handle:
            while chunk := handle.read(self.SLICE_SIZE):
                full.update(chunk)
                chunks.append(hashlib.md5(chunk).hexdigest().upper())
        file_md5 = full.hexdigest()
        slice_md5 = file_md5 if len(chunks) <= 1 else hashlib.md5("\n".join(chunks).encode()).hexdigest()
        initialized = self.client.upload_request("/person/initMultiUpload", {
            "parentFolderId": lookup.directory_id, "fileName": name,
            "fileSize": str(source.stat().st_size), "sliceSize": str(self.SLICE_SIZE),
            "fileMd5": file_md5, "sliceMd5": slice_md5,
        })
        data = initialized.get("data") or {}
        upload_id = str(data.get("uploadFileId") or "")
        if not upload_id:
            return False
        if int(data.get("fileDataExists") or 0) != 1:
            with source.open("rb") as handle:
                for index, chunk_md5 in enumerate(chunks, 1):
                    chunk = handle.read(self.SLICE_SIZE)
                    part_md5 = base64.b64encode(bytes.fromhex(chunk_md5)).decode()
                    urls = self.client.upload_request("/person/getMultiUploadUrls", {
                        "partInfo": f"{index}-{part_md5}", "uploadFileId": upload_id,
                    })
                    part = (urls.get("uploadUrls") or {}).get(f"partNumber_{index}") or {}
                    headers = {}
                    for value in unquote(str(part.get("requestHeader") or "")).split("&"):
                        if "=" in value:
                            key, header_value = value.split("=", 1)
                            headers[key] = header_value
                    response = self.client.rate_limiter.call(
                        self.client.session.put,
                        str(part.get("requestURL") or ""),
                        data=chunk,
                        headers=headers,
                        timeout=self.client.timeout,
                    )
                    response.raise_for_status()
                    if progress_callback:
                        uploaded = min(source.stat().st_size, index * self.SLICE_SIZE)
                        progress_callback(uploaded, source.stat().st_size)
        self.client.upload_request("/person/commitMultiUploadFile", {
            "uploadFileId": upload_id, "fileMd5": file_md5, "sliceMd5": slice_md5,
            "lazyCheck": "1", "opertype": "3",
        })
        return self.files.find_file(save_path, name) is not None
