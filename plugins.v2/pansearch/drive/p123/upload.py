"""123 网盘本地文件上传能力。"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from app.log import logger

from .client import P123_AVAILABLE, check_response
from .files import P123FileService


@dataclass
class P123UploadService:
    """复用 p123client 的秒传、预签名分片和完成接口。"""

    client: Any
    files: P123FileService

    @staticmethod
    def _file_md5(path: Path) -> str:
        digest = hashlib.md5()
        with path.open("rb") as file:
            while chunk := file.read(8 * 1024 * 1024):
                digest.update(chunk)
        return digest.hexdigest()

    def try_rapid_upload(
            self, local_path: str, save_path: str, target_name: str,
            algorithm: str, checksum: str, size: int,
    ) -> bool:
        if algorithm != "md5" or not P123_AVAILABLE:
            return False
        lookup = self.files.resolve_directory(save_path, create=True)
        if not lookup.checked or lookup.directory_id is None:
            raise RuntimeError(f"123 本地上传目录不可用：{save_path}")
        response = self.client.upload_request({
            "etag": checksum.lower(),
            "fileName": target_name,
            "size": int(size),
            "parentFileId": int(lookup.directory_id or 0),
            "type": 0,
            "duplicate": 2,
        })
        check_response(response)
        if not (response.get("data") or {}).get("Reuse"):
            return False
        return self._confirm_upload(save_path, target_name, Path(local_path))

    def upload_file(
            self,
            local_path: str,
            save_path: str,
            target_name: str = "",
            file_sha1: str = "",
            progress_callback: Optional[Callable[[int, int], None]] = None,
    ) -> bool:
        if not P123_AVAILABLE:
            logger.error("123 本地上传不可用：p123client 未安装")
            return False
        source = Path(str(local_path or ""))
        if not source.is_file():
            logger.error(f"123 本地上传文件不存在：{source}")
            return False
        lookup = self.files.resolve_directory(save_path, create=True)
        if not lookup.checked or lookup.directory_id is None:
            logger.error(f"123 本地上传目录不可用：{save_path}")
            return False

        upload_name = str(target_name or source.name).strip()
        file_size = source.stat().st_size
        try:
            upload_data = self._initialize_upload(
                lookup.directory_id, upload_name, file_size,
                self._file_md5(source),
            )
            if upload_data.get("Reuse"):
                if progress_callback:
                    progress_callback(file_size, file_size)
                return self._confirm_upload(save_path, upload_name, source)

            slice_size = int(upload_data.get("SliceSize") or 0)
            if slice_size <= 0:
                raise RuntimeError("上传初始化未返回有效分片大小")
            request_kwargs = {
                "method": "PUT",
                "headers": {"authorization": ""},
                "parse": ...,
            }
            if file_size > slice_size:
                with source.open("rb") as file:
                    part_number = 1
                    transferred = 0
                    while chunk := file.read(slice_size):
                        upload_data["partNumberStart"] = part_number
                        upload_data["partNumberEnd"] = part_number + 1
                        prepared = self.client.upload_prepare(upload_data)
                        check_response(prepared)
                        upload_url = str(
                            (prepared.get("data") or {}).get("presignedUrls", {}).get(
                                str(part_number)
                            )
                            or ""
                        )
                        if not upload_url:
                            raise RuntimeError(f"第 {part_number} 个分片未返回上传地址")
                        self.client.request(upload_url, data=chunk, **request_kwargs)
                        transferred += len(chunk)
                        if progress_callback:
                            progress_callback(transferred, file_size)
                        part_number += 1
            else:
                authorized = self.client.upload_auth(upload_data)
                check_response(authorized)
                upload_url = str(
                    (authorized.get("data") or {}).get("presignedUrls", {}).get("1")
                    or ""
                )
                if not upload_url:
                    raise RuntimeError("上传授权未返回上传地址")
                self.client.request(upload_url, data=source.read_bytes(), **request_kwargs)
                if progress_callback:
                    progress_callback(file_size, file_size)

            upload_data["isMultipart"] = file_size > slice_size
            completed = self.client.upload_complete(upload_data)
            check_response(completed)
            return self._confirm_upload(save_path, upload_name, source)
        except Exception as error:
            logger.error(f"123 本地文件上传失败：{source.name}，{error}")
            return False

    def upload_progressive(
            self,
            local_path: str,
            save_path: str,
            target_name: str,
            file_size: int,
            algorithm: str,
            checksum: str,
            wait_for_range: Callable[[int, int], None],
            progress_callback: Optional[Callable[[int, int], None]] = None,
            stop_requested: Optional[Callable[[], bool]] = None,
    ) -> bool:
        """源文件已有可信 MD5 时，消费下载线程刚落盘的连续分片。"""
        if (
                not P123_AVAILABLE
                or algorithm.lower() != "md5"
                or not checksum
                or int(file_size or 0) <= 0
        ):
            return False
        source = Path(str(local_path or ""))
        lookup = self.files.resolve_directory(save_path, create=True)
        if not lookup.checked or lookup.directory_id is None:
            raise RuntimeError(f"123 流水线上传目录不可用：{save_path}")
        upload_name = str(target_name or source.name).strip()
        try:
            upload_data = self._initialize_upload(
                lookup.directory_id, upload_name, file_size, checksum.lower()
            )
            if upload_data.get("Reuse"):
                if progress_callback:
                    progress_callback(file_size, file_size)
                return self._confirm_upload(save_path, upload_name, source)

            slice_size = int(upload_data.get("SliceSize") or 0)
            if slice_size <= 0:
                raise RuntimeError("上传初始化未返回有效分片大小")
            request_kwargs = {
                "method": "PUT",
                "headers": {"authorization": ""},
                "parse": ...,
            }
            part_number = 1
            transferred = 0
            while transferred < file_size:
                if stop_requested and stop_requested():
                    raise InterruptedError
                end = min(file_size, transferred + slice_size) - 1
                wait_for_range(transferred, end)
                with source.open("rb") as file:
                    file.seek(transferred)
                    chunk = file.read(end - transferred + 1)
                if len(chunk) != end - transferred + 1:
                    raise IOError(
                        f"流水线上传分片尚未完整落盘：{transferred}-{end}"
                    )
                if file_size > slice_size:
                    upload_data["partNumberStart"] = part_number
                    upload_data["partNumberEnd"] = part_number + 1
                    prepared = self.client.upload_prepare(upload_data)
                    check_response(prepared)
                    upload_url = str(
                        (prepared.get("data") or {}).get("presignedUrls", {}).get(
                            str(part_number)
                        )
                        or ""
                    )
                else:
                    authorized = self.client.upload_auth(upload_data)
                    check_response(authorized)
                    upload_url = str(
                        (authorized.get("data") or {}).get(
                            "presignedUrls", {}
                        ).get("1") or ""
                    )
                if not upload_url:
                    raise RuntimeError(f"第 {part_number} 个分片未返回上传地址")
                self.client.request(upload_url, data=chunk, **request_kwargs)
                transferred += len(chunk)
                if progress_callback:
                    progress_callback(transferred, file_size)
                part_number += 1

            upload_data["isMultipart"] = file_size > slice_size
            completed = self.client.upload_complete(upload_data)
            check_response(completed)
            return self._confirm_upload(save_path, upload_name, source)
        except InterruptedError:
            raise
        except Exception as error:
            logger.error(f"123 流水线上传失败：{upload_name}，{error}")
            return False

    def _initialize_upload(
            self, directory_id: str, upload_name: str,
            file_size: int, file_md5: str,
    ) -> dict:
        response = self.client.upload_request({
            "etag": file_md5,
            "fileName": upload_name,
            "size": int(file_size),
            "parentFileId": int(directory_id or 0),
            "type": 0,
            "duplicate": 2,
        })
        check_response(response)
        return response.get("data") or {}

    def _confirm_upload(self, save_path: str, upload_name: str, source: Path) -> bool:
        for index in range(10):
            if self.files.find_file(save_path, upload_name):
                logger.info(f"123 本地文件上传完成：{source.name} -> {save_path}/{upload_name}")
                return True
            if index < 9:
                time.sleep(0.5)
        logger.error(f"123 本地上传完成后未找到目标文件：{save_path}/{upload_name}")
        return False
