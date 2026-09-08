"""阿里云盘公开分享读取与单文件转存。"""

from __future__ import annotations

import json
import re
from threading import RLock
from urllib.parse import parse_qs, urlsplit

from app.log import logger

from ..common import iter_transfer_batches
from ...core.cloud import ShareLinkStatus
from ...utils.cache import create_platform_ttl_cache


class AliPanShareService:
    API = "https://api.aliyundrive.com"

    def __init__(self, client, files):
        self.client = client
        self.files = files
        self._shares = {}
        self._share_listing_cache = create_platform_ttl_cache(
            "alipan:share_listings",
            client,
            maxsize=1024,
            ttl=10 * 60,
        )
        self._share_cache_lock = RLock()

    @staticmethod
    def extract_share_info(share_url: str) -> dict[str, str]:
        value = str(share_url or "").strip()
        match = re.search(r"(?:alipan\.com|aliyundrive\.com)/s/([\w-]+)", value, re.I)
        if not match:
            return {}
        query = parse_qs(urlsplit(value).query)
        password = str((query.get("pwd") or query.get("code") or [""])[0])
        return {"share_id": match.group(1), "share_pwd": password}

    def _request(
            self, path: str, *, headers=None, payload=None,
            authenticated: bool = False,
    ) -> dict:
        response = self.client.raw_request(
            "POST", self.API + path,
            authenticated=authenticated,
            headers=headers,
            json=payload or {},
        )
        data = response.json()
        if data.get("code"):
            raise RuntimeError(data.get("message") or data.get("code"))
        return data

    def _prepare(self, share_url: str) -> dict:
        parsed = self.extract_share_info(share_url)
        if not parsed:
            raise ValueError("无效的阿里云盘分享链接")
        cached = self._shares.get(parsed["share_id"])
        if cached and cached.get("share_pwd") == parsed["share_pwd"]:
            return cached
        token = self._request(
            "/v2/share_link/get_share_token", payload=parsed
        ).get("share_token")
        if not token:
            raise RuntimeError("阿里云盘未返回分享 Token")
        info = {**parsed, "share_token": str(token), "items": {}}
        self._shares[parsed["share_id"]] = info
        return info

    def _list(self, info: dict, parent_id: str = "root") -> list[dict]:
        cache_key = f"{info['share_id']}|{parent_id}"
        with self._share_cache_lock:
            cached = self._share_listing_cache.get(cache_key)
        if isinstance(cached, list):
            return [dict(item) for item in cached]
        marker = ""
        result = []
        while True:
            data = self._request(
                "/adrive/v3/file/list",
                headers={"x-share-token": info["share_token"]},
                payload={
                    "share_id": info["share_id"],
                    "parent_file_id": parent_id,
                    "limit": 200,
                    "marker": marker,
                    "order_by": "name",
                    "order_direction": "ASC",
                },
            )
            result.extend(data.get("items") or [])
            marker = str(data.get("next_marker") or "")
            if not marker:
                with self._share_cache_lock:
                    self._share_listing_cache.set(
                        cache_key, [dict(item) for item in result]
                    )
                return result

    def check_share_status(self, share_url: str) -> ShareLinkStatus:
        status = ShareLinkStatus()
        try:
            info = self._prepare(share_url)
            items = self._list(info)
            status.is_valid = True
            status.file_count = len(items)
            status.share_info = {"share_id": info["share_id"]}
        except Exception as error:
            status.error_message = str(error) or "阿里云盘分享不可用"
        return status

    def list_share_files(self, share_url: str, **kwargs) -> list:
        try:
            info = self._prepare(share_url)
            result = []
            stack = ["root"]
            while stack:
                for item in self._list(info, stack.pop()):
                    if item.get("type") == "folder":
                        stack.append(str(item.get("file_id") or ""))
                        continue
                    file_id = str(item.get("file_id") or "")
                    name = str(item.get("name") or "")
                    if not file_id or not name:
                        continue
                    info["items"][file_id] = item
                    result.append({
                        "id": file_id, "name": name, "is_dir": False,
                        "size": int(item.get("size") or 0),
                        "sha1": str(item.get("content_hash") or ""),
                    })
            return result
        except Exception as error:
            logger.warning(f"读取阿里云盘分享文件失败：{error}")
            return []

    def list_share_directory(
            self, share_url: str, parent_id: str = ""
    ) -> list:
        """列出分享中的当前目录，并向预览接口保留真实异常。"""
        info = self._prepare(share_url)
        result = []
        for raw in self._list(info, str(parent_id or "root")):
            file_id = str(raw.get("file_id") or "")
            name = str(raw.get("name") or "")
            if not file_id or not name:
                continue
            is_dir = raw.get("type") == "folder"
            result.append({
                "id": file_id,
                "name": name,
                "is_dir": is_dir,
                "size": 0 if is_dir else int(raw.get("size") or 0),
                "sha1": str(raw.get("content_hash") or ""),
            })
        return result

    def transfer_file(
            self, share_url: str, file_id: str, save_path: str,
            target_name: str, **kwargs,
    ) -> bool:
        info = self._prepare(share_url)
        if str(file_id) not in info["items"]:
            self.list_share_files(share_url)
        item = info["items"].get(str(file_id)) or {}
        name = target_name or str(item.get("name") or file_id)
        lookup = self.files.resolve_directory(save_path, create=True)
        if not lookup.directory_id:
            return False
        data = self._request(
            "/adrive/v2/batch",
            authenticated=True,
            headers={"x-share-token": info["share_token"]},
            payload={
                "requests": [{
                    "body": {
                        "file_id": str(file_id),
                        "share_id": info["share_id"],
                        "to_drive_id": self.client.drive_id,
                        "to_parent_file_id": lookup.directory_id,
                        "auto_rename": False,
                    },
                    "headers": {"Content-Type": "application/json"},
                    "id": str(file_id), "method": "POST", "url": "/file/copy",
                }],
                "resource": "file",
            },
        )
        response = (data.get("responses") or [{}])[0]
        body = response.get("body") or {}
        if isinstance(body, str):
            try:
                body = json.loads(body)
            except json.JSONDecodeError:
                body = {}
        status = int(response.get("status") or response.get("status_code") or 0)
        if body.get("code") or (status and status not in (200, 201)):
            return False
        if name != str(item.get("name") or ""):
            copied = self.files.find_file(save_path, str(item.get("name") or ""))
            if not copied or not self.files.rename_file(save_path, copied, name):
                return False
        return True

    def transfer_share(self, share_url: str, save_path: str) -> bool:
        files = self.list_share_files(share_url)
        return bool(files) and all(
            self.transfer_file(share_url, item["id"], save_path, item["name"])
            for item in files
        )

    def transfer_files_batch(
            self, share_url: str, file_ids: list, save_path: str, **kwargs,
    ) -> tuple:
        succeeded, failed = [], []
        for batch in iter_transfer_batches(
                file_ids, kwargs.get("batch_size", 20),
                kwargs.get("batch_interval", 3), 20,
        ):
            for file_id in batch:
                if self.transfer_file(share_url, file_id, save_path, ""):
                    succeeded.append(file_id)
                else:
                    failed.append(file_id)
        return succeeded, failed
