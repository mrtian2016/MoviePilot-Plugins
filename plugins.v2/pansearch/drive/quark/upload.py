"""夸克网盘本地文件上传能力。"""

from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from wsgiref.handlers import format_date_time
from xml.sax.saxutils import escape

import requests
from app.log import logger

from .files import QuarkFileService


@dataclass
class QuarkUploadService:
    """适配夸克上传预检、哈希秒传和 OSS 分片提交协议。"""

    client: Any
    files: QuarkFileService
    default_part_size: int = 10 * 1024 * 1024
    rapid_requires_local_file = True

    @staticmethod
    def _calculate_hashes(path: Path, file_sha1: str = "") -> tuple[str, str]:
        md5_digest = hashlib.md5()
        sha1_digest = hashlib.sha1() if not file_sha1 else None
        with path.open("rb") as file:
            while chunk := file.read(8 * 1024 * 1024):
                md5_digest.update(chunk)
                if sha1_digest:
                    sha1_digest.update(chunk)
        sha1_value = str(file_sha1 or "").strip().lower()
        return md5_digest.hexdigest(), sha1_value or sha1_digest.hexdigest()

    def try_rapid_upload(
            self, local_path: str, save_path: str, target_name: str,
            algorithm: str, checksum: str, size: int,
    ) -> bool:
        if algorithm != "md5":
            return False
        source = Path(local_path)
        lookup = self.files.resolve_directory(save_path, create=True)
        if not source.is_file() or not lookup.checked or lookup.directory_id is None:
            return False
        now_ms = int(time.time() * 1000)
        format_type = mimetypes.guess_type(target_name)[0] or ""
        pre_response = self.client.request(
            "POST", "file/upload/pre",
            json_data={
                "ccp_hash_update": True,
                "dir_name": "",
                "file_name": target_name,
                "format_type": format_type,
                "l_created_at": now_ms,
                "l_updated_at": now_ms,
                "pdir_fid": lookup.directory_id,
                "size": int(size),
            },
        )
        if not self.client.is_success(pre_response):
            raise RuntimeError(pre_response.get("message") or "上传预检失败")
        pre_data = self.client.data(pre_response)
        if not isinstance(pre_data, dict) or not pre_data:
            raise RuntimeError("上传预检未返回有效数据")
        if pre_data.get("finish"):
            return self._confirm_upload(save_path, target_name, source)
        _, sha1_value = self._calculate_hashes(source)
        hash_response = self.client.request(
            "POST", "file/update/hash",
            json_data={
                "md5": checksum.lower(),
                "sha1": sha1_value,
                "task_id": pre_data.get("task_id"),
            },
        )
        if not self.client.is_success(hash_response):
            raise RuntimeError(hash_response.get("message") or "上传哈希校验失败")
        hash_data = self.client.data(hash_response)
        if not (isinstance(hash_data, dict) and hash_data.get("finish")):
            return False
        return self._confirm_upload(save_path, target_name, source)

    def upload_file(
            self,
            local_path: str,
            save_path: str,
            target_name: str = "",
            file_sha1: str = "",
            progress_callback: Optional[Callable[[int, int], None]] = None,
    ) -> bool:
        source = Path(str(local_path or ""))
        if not source.is_file():
            logger.error(f"夸克本地上传文件不存在：{source}")
            return False
        lookup = self.files.resolve_directory(save_path, create=True)
        if not lookup.checked or lookup.directory_id is None:
            logger.error(f"夸克本地上传目录不可用：{save_path}")
            return False

        upload_name = str(target_name or source.name).strip()
        file_size = source.stat().st_size
        now_ms = int(time.time() * 1000)
        format_type = mimetypes.guess_type(upload_name)[0] or ""
        mime_type = format_type or "application/octet-stream"
        try:
            md5_value, sha1_value = self._calculate_hashes(source, file_sha1)
            pre_response = self.client.request(
                "POST",
                "file/upload/pre",
                json_data={
                    "ccp_hash_update": True,
                    "dir_name": "",
                    "file_name": upload_name,
                    "format_type": format_type,
                    "l_created_at": now_ms,
                    "l_updated_at": now_ms,
                    "pdir_fid": lookup.directory_id,
                    "size": file_size,
                },
            )
            if not self.client.is_success(pre_response):
                raise RuntimeError(pre_response.get("message") or "上传预检失败")
            pre_data = self.client.data(pre_response)
            if not isinstance(pre_data, dict) or not pre_data:
                raise RuntimeError("上传预检未返回有效数据")
            if pre_data.get("finish"):
                if progress_callback:
                    progress_callback(file_size, file_size)
                return self._confirm_upload(save_path, upload_name, source)

            hash_response = self.client.request(
                "POST",
                "file/update/hash",
                json_data={
                    "md5": md5_value,
                    "sha1": sha1_value,
                    "task_id": pre_data.get("task_id"),
                },
            )
            if not self.client.is_success(hash_response):
                raise RuntimeError(hash_response.get("message") or "上传哈希校验失败")
            hash_data = self.client.data(hash_response)
            if isinstance(hash_data, dict) and hash_data.get("finish"):
                if progress_callback:
                    progress_callback(file_size, file_size)
                return self._confirm_upload(save_path, upload_name, source)

            metadata = pre_response.get("metadata") or {}
            part_size = int(metadata.get("part_size") or self.default_part_size)
            etags = self._upload_parts(
                source, pre_data, max(1, part_size), mime_type, progress_callback, file_size
            )
            self._commit_upload(pre_data, etags)
            finish_response = self.client.request(
                "POST",
                "file/upload/finish",
                json_data={
                    "obj_key": pre_data.get("obj_key"),
                    "task_id": pre_data.get("task_id"),
                },
            )
            if not self.client.is_success(finish_response):
                raise RuntimeError(finish_response.get("message") or "上传完成回调失败")
            return self._confirm_upload(save_path, upload_name, source, retry=10)
        except Exception as error:
            logger.error(f"夸克本地文件上传失败：{source.name}，{error}")
            return False

    def _confirm_upload(
            self, save_path: str, upload_name: str, source: Path, retry: int = 5
    ) -> bool:
        for index in range(max(1, retry)):
            if self.files.find_file(save_path, upload_name):
                logger.info(f"夸克本地文件上传完成：{source.name} -> {save_path}/{upload_name}")
                return True
            if index < retry - 1:
                time.sleep(1)
        logger.error(f"夸克本地上传完成后未找到目标文件：{save_path}/{upload_name}")
        return False

    @staticmethod
    def _build_oss_url(pre_data: Dict[str, Any]) -> str:
        bucket = str(pre_data.get("bucket") or "")
        upload_url = str(pre_data.get("upload_url") or "")
        obj_key = str(pre_data.get("obj_key") or "")
        if not bucket or not upload_url or not obj_key:
            raise RuntimeError("OSS 上传地址参数不完整")
        suffix = upload_url.removeprefix("http://").removeprefix("https://")
        return f"https://{bucket}.{suffix}/{obj_key}"

    @staticmethod
    def _auth_meta(
            method: str,
            content_md5: str,
            content_type: str,
            date_value: str,
            extra_headers: Dict[str, str],
            bucket: str,
            obj_key: str,
            query: str,
    ) -> str:
        lines = [method, content_md5, content_type, date_value]
        lines.extend(f"{key}:{extra_headers[key]}" for key in sorted(extra_headers))
        lines.append(f"/{bucket}/{obj_key}?{query}")
        return "\n".join(lines)

    def _upload_auth(self, pre_data: Dict[str, Any], auth_meta: str) -> str:
        response = self.client.request(
            "POST",
            "file/upload/auth",
            json_data={
                "auth_info": pre_data.get("auth_info"),
                "auth_meta": auth_meta,
                "task_id": pre_data.get("task_id"),
            },
        )
        if not self.client.is_success(response):
            raise RuntimeError(response.get("message") or "获取上传鉴权失败")
        auth_key = str((self.client.data(response) or {}).get("auth_key") or "")
        if not auth_key:
            raise RuntimeError("获取上传鉴权失败")
        return auth_key

    def _upload_parts(
            self,
            source: Path,
            pre_data: Dict[str, Any],
            part_size: int,
            mime_type: str,
            progress_callback: Optional[Callable[[int, int], None]] = None,
            file_size: int = 0,
    ) -> List[str]:
        bucket = str(pre_data.get("bucket") or "")
        obj_key = str(pre_data.get("obj_key") or "")
        upload_id = str(pre_data.get("upload_id") or "")
        if not bucket or not obj_key or not upload_id:
            raise RuntimeError("上传预检返回的分片参数不完整")
        target_url = self._build_oss_url(pre_data)
        etags: List[str] = []
        with source.open("rb") as file:
            part_number = 1
            transferred = 0
            while chunk := file.read(part_size):
                date_value = format_date_time(time.time())
                signed_headers = {
                    "x-oss-date": date_value,
                    "x-oss-user-agent": "aliyun-sdk-js/1.0.0 Chrome on Windows",
                }
                query = f"partNumber={part_number}&uploadId={upload_id}"
                auth_key = self._upload_auth(
                    pre_data,
                    self._auth_meta(
                        "PUT", "", mime_type, date_value, signed_headers,
                        bucket, obj_key, query,
                    ),
                )
                response = self.client.rate_limiter.call(
                    requests.put,
                    target_url,
                    params={"partNumber": part_number, "uploadId": upload_id},
                    headers={
                        "Authorization": auth_key,
                        "Content-Type": mime_type,
                        "Referer": "https://pan.quark.cn/",
                        **signed_headers,
                    },
                    data=chunk,
                    timeout=max(getattr(self.client, "_timeout", 30), 120),
                )
                response.raise_for_status()
                etag = response.headers.get("ETag") or response.headers.get("Etag")
                if not etag:
                    raise RuntimeError(f"第 {part_number} 个分片未返回 ETag")
                etags.append(etag)
                transferred += len(chunk)
                if progress_callback:
                    progress_callback(transferred, file_size)
                part_number += 1
        if not etags:
            raise RuntimeError("上传分片结果为空")
        return etags

    def _commit_upload(self, pre_data: Dict[str, Any], etags: List[str]) -> None:
        bucket = str(pre_data.get("bucket") or "")
        obj_key = str(pre_data.get("obj_key") or "")
        upload_id = str(pre_data.get("upload_id") or "")
        callback = pre_data.get("callback") or {}
        body_lines = ["<?xml version=\"1.0\" encoding=\"UTF-8\"?>", "<CompleteMultipartUpload>"]
        for index, etag in enumerate(etags, start=1):
            body_lines.extend((
                "<Part>", f"<PartNumber>{index}</PartNumber>",
                f"<ETag>{escape(etag)}</ETag>", "</Part>",
            ))
        body_lines.append("</CompleteMultipartUpload>")
        body = "\n".join(body_lines).encode("utf-8")
        content_md5 = base64.b64encode(hashlib.md5(body).digest()).decode("utf-8")
        callback_value = base64.b64encode(
            json.dumps(callback, ensure_ascii=False).encode("utf-8")
        ).decode("utf-8")
        date_value = format_date_time(time.time())
        signed_headers = {
            "x-oss-callback": callback_value,
            "x-oss-date": date_value,
            "x-oss-user-agent": "aliyun-sdk-js/1.0.0 Chrome on Windows",
        }
        auth_key = self._upload_auth(
            pre_data,
            self._auth_meta(
                "POST", content_md5, "application/xml", date_value,
                signed_headers, bucket, obj_key, f"uploadId={upload_id}",
            ),
        )
        response = self.client.rate_limiter.call(
            requests.post,
            self._build_oss_url(pre_data),
            params={"uploadId": upload_id},
            headers={
                "Authorization": auth_key,
                "Content-MD5": content_md5,
                "Content-Type": "application/xml",
                "Referer": "https://pan.quark.cn/",
                **signed_headers,
            },
            data=body,
            timeout=max(getattr(self.client, "_timeout", 30), 120),
            max_retries=0,
        )
        response.raise_for_status()
