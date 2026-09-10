"""天翼公开分享读取与单文件转存。"""

from __future__ import annotations

import json
import re
import time
from threading import RLock
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from app.log import logger

from ..common import iter_transfer_batches
from ...core.cloud import ShareLinkStatus
from ...utils.cache import create_platform_ttl_cache


class TianyiShareService:
    def __init__(self, client, files):
        self.client = client
        self.files = files
        self._share_items: dict[str, dict[str, dict[str, Any]]] = {}
        self._share_info_cache = create_platform_ttl_cache(
            "tianyi:share_info",
            client,
            maxsize=256,
            ttl=10 * 60,
        )
        self._share_cache_lock = RLock()

    @staticmethod
    def extract_share_info(share_url: str) -> dict[str, str]:
        value = str(share_url or "").strip()
        decoded = unquote(value)
        match = re.search(
            r"cloud\.189\.cn/(?:t/|web/share\?code=)([A-Za-z0-9]+)",
            decoded,
            re.I,
        )
        if not match:
            return {}
        query = parse_qs(urlsplit(decoded).query)
        access_code = str((query.get("pwd") or query.get("accessCode") or [""])[0]).strip()
        if not access_code:
            code_match = re.search(
                r"(?:访问码|提取码|密码|pwd)\s*[：:=]?\s*([A-Za-z0-9]{4,8})",
                decoded,
                re.I,
            )
            access_code = code_match.group(1) if code_match else ""
        return {"share_code": match.group(1), "access_code": access_code}

    def _share_info(self, share_url: str) -> dict[str, Any]:
        parsed = self.extract_share_info(share_url)
        if not parsed:
            raise ValueError("无效的天翼分享链接")
        cache_key = f"{parsed['share_code']}|{parsed['access_code']}"
        with self._share_cache_lock:
            cached = self._share_info_cache.get(cache_key)
        if isinstance(cached, dict):
            return dict(cached)
        result = self.client.public_request(
            "GET", "https://cloud.189.cn/api/open/share/getShareInfoByCodeV2.action",
            params={"shareCode": parsed["share_code"]},
        )
        data = result.get("data") if isinstance(result.get("data"), dict) else result
        info = {**parsed, **data}
        requires_code = str(info.get("needAccessCode") or "").lower() in {
            "1", "true", "yes"
        }
        if requires_code and not parsed["access_code"]:
            raise ValueError("天翼分享需要访问码")
        if not info.get("shareId"):
            access = self.client.public_request(
                "GET", "https://cloud.189.cn/api/open/share/checkAccessCode.action",
                params={
                    "shareCode": parsed["share_code"],
                    "accessCode": parsed["access_code"],
                },
            )
            if access.get("success") is False:
                raise ValueError("天翼分享访问码错误")
            info.update(access)
        if not info.get("shareId"):
            raise ValueError("天翼分享未返回分享 ID")
        with self._share_cache_lock:
            self._share_info_cache.set(cache_key, info)
        return dict(info)

    def check_share_status(self, share_url: str) -> ShareLinkStatus:
        status = ShareLinkStatus()
        try:
            info = self._share_info(share_url)
            status.is_valid = bool(info.get("shareId"))
            status.file_count = int(info.get("fileCount") or 0)
            status.share_info = info
            if not status.is_valid:
                status.error_message = "天翼分享未返回分享 ID"
        except Exception as error:
            message = str(error)
            status.error_message = message or "天翼分享不可用"
            status.is_expired = "过期" in message or "expired" in message.lower()
            status.is_cancelled = "取消" in message
            status.is_deleted = "删除" in message or "不存在" in message
        return status

    def _list_directory(
            self, info: dict[str, Any], folder_id: str = "-11"
    ) -> tuple[list[dict], list[dict]]:
        result = self.client.public_request(
            "GET", "https://cloud.189.cn/api/open/share/listShareDir.action",
            params={
                "shareId": info["shareId"],
                "fileId": folder_id or info.get("fileId") or "-11",
                "isFolder": "true",
                "orderBy": "lastOpTime",
                "descending": "true",
                "shareMode": info.get("shareMode") or "1",
                "pageNum": 1,
                "pageSize": 1000,
                "accessCode": info.get("access_code") or "",
            },
        )
        listing = result.get("fileListAO") or {}
        return list(listing.get("fileList") or []), list(listing.get("folderList") or [])

    def list_share_files(self, share_url: str, **kwargs) -> list:
        try:
            info = self._share_info(share_url)
            share_id = str(info.get("shareId") or "")
            cached: dict[str, dict[str, Any]] = {}
            files = []
            stack = [str(info.get("fileId") or "-11")]
            while stack:
                file_list, folder_list = self._list_directory(info, stack.pop())
                stack.extend(str(item.get("id") or item.get("fileId") or "") for item in folder_list)
                for item in file_list:
                    file_id = str(item.get("id") or item.get("fileId") or "")
                    name = str(item.get("name") or item.get("fileName") or "")
                    if not file_id or not name:
                        continue
                    normalized = {
                        "id": file_id,
                        "name": name,
                        "is_dir": False,
                        "size": int(item.get("size") or item.get("fileSize") or 0),
                        "md5": str(item.get("md5") or ""),
                    }
                    cached[file_id] = item
                    files.append(normalized)
            self._share_items[share_id] = cached
            return files
        except Exception as error:
            logger.warning(f"读取天翼分享文件失败：{error}")
            return []

    def list_share_directory(
            self, share_url: str, parent_id: str = ""
    ) -> list:
        """列出分享中的当前目录，并向预览接口保留真实异常。"""
        info = self._share_info(share_url)
        file_list, folder_list = self._list_directory(
            info, str(parent_id or info.get("fileId") or "-11")
        )
        result = []
        for raw, is_dir in (
                *((item, True) for item in folder_list),
                *((item, False) for item in file_list),
        ):
            file_id = str(raw.get("id") or raw.get("fileId") or "")
            name = str(raw.get("name") or raw.get("fileName") or "")
            if not file_id or not name:
                continue
            result.append({
                "id": file_id,
                "name": name,
                "is_dir": is_dir,
                "size": 0 if is_dir else int(raw.get("size") or raw.get("fileSize") or 0),
                "md5": str(raw.get("md5") or ""),
            })
        return result

    def _save(self, share_url: str, file_ids: list[str], save_path: str) -> bool:
        info = self._share_info(share_url)
        share_id = str(info.get("shareId") or "")
        cached = self._share_items.get(share_id, {})
        if not set(file_ids).issubset(cached):
            self.list_share_files(share_url)
            cached = self._share_items.get(share_id, {})
        if not set(file_ids).issubset(cached):
            return False
        lookup = self.files.resolve_directory(save_path, create=True)
        if not lookup.directory_id:
            return False
        task_infos = [{
            "fileId": file_id,
            "fileName": cached[file_id].get("name") or cached[file_id].get("fileName") or "",
            "isFolder": 0,
        } for file_id in file_ids]
        created = self.client.request(
            "POST", "https://cloud.189.cn/api/open/batch/createBatchTask.action",
            data={
                "type": "SHARE_SAVE",
                "taskInfos": json.dumps(task_infos, ensure_ascii=False),
                "targetFolderId": lookup.directory_id,
                "shareId": share_id,
            },
        )
        task_id = str(created.get("taskId") or "")
        if not task_id:
            return False
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            result = self.client.request(
                "POST", "https://cloud.189.cn/api/open/batch/checkBatchTask.action",
                data={"taskId": task_id, "type": "SHARE_SAVE"},
            )
            task_status = int(result.get("taskStatus") or 0)
            if task_status == 4:
                return int(result.get("failedCount") or 0) == 0
            if task_status == 2:
                return False
            time.sleep(0.5)
        raise TimeoutError("等待天翼分享转存完成超时")

    def transfer_share(self, share_url: str, save_path: str) -> bool:
        ids = [str(item["id"]) for item in self.list_share_files(share_url)]
        return bool(ids) and self._save(share_url, ids, save_path)

    def transfer_file(
            self, share_url: str, file_id: str, save_path: str,
            target_name: str, **kwargs,
    ) -> bool:
        return self._save(share_url, [str(file_id)], save_path)

    def transfer_files_batch(
            self, share_url: str, file_ids: list, save_path: str, **kwargs,
    ) -> tuple:
        succeeded, failed = [], []
        for batch in iter_transfer_batches(
                file_ids, kwargs.get("batch_size", 20),
                kwargs.get("batch_interval", 3), 100,
        ):
            (succeeded if self._save(share_url, batch, save_path) else failed).extend(batch)
        return succeeded, failed
