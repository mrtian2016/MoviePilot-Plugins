"""光鸭网盘本地文件上传能力。"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Optional
from urllib.parse import urlparse

from app.log import logger

from .files import GuangyaFileService

try:
    import oss2

    OSS2_AVAILABLE = True
except ImportError:
    oss2 = None
    OSS2_AVAILABLE = False


@dataclass
class GuangyaUploadService:
    """适配光鸭 MD5 秒传、临时 OSS 凭证和上传任务确认协议。"""

    client: Any
    files: GuangyaFileService

    @staticmethod
    def _file_md5(path: Path) -> str:
        digest = hashlib.md5()
        with path.open("rb") as file:
            while chunk := file.read(8 * 1024 * 1024):
                digest.update(chunk)
        return digest.hexdigest().upper()

    def try_rapid_upload(
            self, local_path: str, save_path: str, target_name: str,
            algorithm: str, checksum: str, size: int,
    ) -> bool:
        if algorithm != "md5":
            return False
        lookup = self.files.resolve_directory(save_path, create=True)
        if not lookup.checked or lookup.directory_id is None:
            raise RuntimeError(f"光鸭本地上传目录不可用：{save_path}")
        response = self._request(
            "/nd.bizuserres.s/v1/check_can_flash_upload",
            {
                "taskId": "",
                "gcid": checksum.upper(),
                "fileSize": int(size),
                "name": target_name,
                "parentId": lookup.directory_id or "",
            },
        )
        if not (self.client.is_success(response) and response.get("data")):
            return False
        return self._confirm_upload(
            save_path, target_name, Path(local_path), retry=10
        )

    def upload_file(
            self,
            local_path: str,
            save_path: str,
            target_name: str = "",
            file_sha1: str = "",
            progress_callback: Optional[Callable[[int, int], None]] = None,
    ) -> bool:
        if not OSS2_AVAILABLE:
            logger.error("光鸭本地上传不可用：oss2 未安装")
            return False
        source = Path(str(local_path or ""))
        if not source.is_file():
            logger.error(f"光鸭本地上传文件不存在：{source}")
            return False
        lookup = self.files.resolve_directory(save_path, create=True)
        if not lookup.checked or lookup.directory_id is None:
            logger.error(f"光鸭本地上传目录不可用：{save_path}")
            return False

        upload_name = str(target_name or source.name).strip()
        file_size = source.stat().st_size
        file_md5 = self._file_md5(source)
        try:
            flash_response = self._request(
                "/nd.bizuserres.s/v1/check_can_flash_upload",
                {
                    "taskId": "",
                    "gcid": file_md5,
                    "fileSize": file_size,
                    "name": upload_name,
                    "parentId": lookup.directory_id or "",
                },
            )
            if self.client.is_success(flash_response) and flash_response.get("data"):
                if progress_callback:
                    progress_callback(file_size, file_size)
                if self._confirm_upload(save_path, upload_name, source, retry=10):
                    return True

            token_response = self._request(
                "/nd.bizuserres.s/v1/get_res_center_token",
                {
                    "capacity": 2,
                    "name": upload_name,
                    "res": {"fileSize": file_size, "md5": file_md5},
                    "parentId": lookup.directory_id or "",
                },
            )
            if token_response.get("code") in (156, "156"):
                return self._confirm_upload(save_path, upload_name, source, retry=20)
            if not self.client.is_success(token_response):
                raise RuntimeError(
                    token_response.get("msg") or token_response.get("error")
                    or "获取上传凭证失败"
                )
            data = self.client.data(token_response)
            if not isinstance(data, dict):
                raise RuntimeError("上传凭证响应格式无效")
            self._upload_to_oss(source, data, progress_callback, file_size)
            task_id = str(data.get("taskId") or data.get("task_id") or "")
            if task_id:
                self._wait_task(task_id)
            return self._confirm_upload(save_path, upload_name, source, retry=20)
        except Exception as error:
            logger.error(f"光鸭本地文件上传失败：{source.name}，{error}")
            return False

    def _request(self, endpoint: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self.client.request(
            "POST", f"{self.client.API_BASE_URL}{endpoint}", json_data=payload
        )

    @staticmethod
    def _upload_to_oss(
            source: Path,
            data: Dict[str, Any],
            progress_callback: Optional[Callable[[int, int], None]] = None,
            file_size: int = 0,
    ) -> None:
        object_path = str(data.get("objectPath") or "")
        bucket_name = str(data.get("bucketName") or "")
        endpoint = str(data.get("endPoint") or data.get("fullEndPoint") or "")
        credentials = data.get("creds") or {}
        access_key = str(credentials.get("accessKeyID") or "")
        secret_key = str(credentials.get("secretAccessKey") or "")
        security_token = str(credentials.get("sessionToken") or "")
        if not all((object_path, bucket_name, endpoint, access_key, secret_key, security_token)):
            raise RuntimeError("上传凭证缺少 OSS 参数")
        parsed = urlparse(endpoint if endpoint.startswith("http") else f"https://{endpoint}")
        host = parsed.netloc or parsed.path
        if host.startswith(f"{bucket_name}."):
            host = host[len(bucket_name) + 1:]
        auth = oss2.StsAuth(access_key, secret_key, security_token)
        bucket = oss2.Bucket(auth, f"https://{host}", bucket_name)
        result = oss2.resumable_upload(
            bucket,
            object_path,
            str(source),
            part_size=5 * 1024 * 1024,
            progress_callback=(
                (lambda consumed, total: progress_callback(consumed, total))
                if progress_callback else None
            ),
        )
        if result is None:
            raise RuntimeError("OSS 分片上传未返回结果")

    def _wait_task(self, task_id: str, retry: int = 120) -> None:
        for index in range(max(1, retry)):
            status_response = self._request(
                "/nd.bizuserres.s/v1/get_task_status", {"taskId": task_id}
            )
            data = self.client.data(status_response)
            status = data.get("status", data.get("taskStatus")) if isinstance(data, dict) else None
            if status in (2, "2", "success", "done", "finished"):
                return
            if status_response.get("code") in (145, "145"):
                raise RuntimeError(status_response.get("msg") or "上传任务失败")
            info_response = self._request(
                "/nd.bizuserres.s/v1/file/get_info_by_task_id", {"taskId": task_id}
            )
            info_data = self.client.data(info_response)
            if isinstance(info_data, dict):
                raw = next(
                    (
                        info_data.get(key)
                        for key in ("fileInfo", "info", "Info", "detail")
                        if isinstance(info_data.get(key), dict)
                    ),
                    info_data,
                )
                if any(raw.get(key) for key in ("fileId", "id", "fid", "resId", "FileId")):
                    return
            info_code = info_response.get("code") if isinstance(info_response, dict) else None
            info_message = str(
                info_response.get("msg") or info_response.get("message") or ""
            ) if isinstance(info_response, dict) else ""
            if info_code not in (147, "147") and "上传中" not in info_message:
                if not self.client.is_success(info_response):
                    raise RuntimeError(info_message or "查询上传任务失败")
            if index < retry - 1:
                time.sleep(1)
        raise TimeoutError("等待光鸭上传任务完成超时")

    def _confirm_upload(
            self, save_path: str, upload_name: str, source: Path, retry: int
    ) -> bool:
        for index in range(max(1, retry)):
            if self.files.find_file(save_path, upload_name):
                logger.info(f"光鸭本地文件上传完成：{source.name} -> {save_path}/{upload_name}")
                return True
            if index < retry - 1:
                time.sleep(0.5)
        logger.error(f"光鸭本地上传完成后未找到目标文件：{save_path}/{upload_name}")
        return False
