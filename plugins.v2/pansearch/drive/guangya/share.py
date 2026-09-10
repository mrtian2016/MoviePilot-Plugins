"""光鸭分享访问与转存能力。"""

from __future__ import annotations

import re
from threading import RLock
from typing import Any, Dict, Iterable
from urllib.parse import parse_qs, urlsplit

from app.log import logger

from .files import cloud_file, list_data
from ..common import iter_transfer_batches, safe_int
from ...core.cloud import ShareLinkStatus
from ...utils.cache import create_platform_ttl_cache


class GuangyaShareService:
    """封装光鸭分享接口、文件遍历和转存。"""

    def __init__(self, client: Any, files: Any, offline_service: Any):
        self._files = files
        self._offline = offline_service
        self.client = client
        self.page_size = files.page_size
        self._share_token_cache = create_platform_ttl_cache(
            "guangya:share_tokens",
            client,
            maxsize=256,
            ttl=10 * 60,
        )
        self._share_token_lock = RLock()

    def _share_summary(self, share_id: str) -> Dict[str, Any]:
        return self.client.request(
            "POST",
            f"{self.client.API_BASE_URL}/nd.bizuserres.s/v1/get_share_summary",
            json_data={"shareId": share_id},
            authenticated=False,
        )

    def _share_access_token(self, share_id: str, code: str) -> Dict[str, Any]:
        return self.client.request(
            "POST",
            f"{self.client.API_BASE_URL}/nd.bizuserres.s/v1/get_share_access_token",
            json_data={"shareId": share_id, "code": code},
            authenticated=False,
        )

    def _share_files(
            self, access_token: str, parent_id: str = "", page: int = 1,
            page_size: int = 100,
    ) -> Dict[str, Any]:
        return self.client.request(
            "POST",
            f"{self.client.API_BASE_URL}/nd.bizuserres.s/v1/get_share_page_files_list",
            json_data={
                "accessToken": access_token,
                "parentId": parent_id,
                "page": page,
                "pageSize": page_size,
                "orderBy": 0,
                "sortType": 0,
            },
            authenticated=False,
        )

    def _restore(self, access_token: str, file_ids: list, parent_id: str = "") -> Dict[str, Any]:
        return self.client.request(
            "POST",
            f"{self.client.API_BASE_URL}/nd.bizuserres.s/v1/restore_share",
            json_data={
                "accessToken": access_token,
                "fileIds": file_ids,
                "parentId": parent_id or "",
            },
        )

    @staticmethod
    def extract_share_info(share_url: str) -> Dict[str, Any]:
        raw = str(share_url or "").strip()
        try:
            parsed = urlsplit(raw)
        except ValueError:
            return {}
        if not parsed.hostname or not parsed.hostname.lower().endswith("guangyapan.com"):
            return {}
        combined_path = "/".join(
            value.strip("/") for value in (parsed.path, parsed.fragment) if value
        )
        match = re.search(r"(?:^|/)(?:s|share)/([A-Za-z0-9_-]+)", combined_path, re.I)
        query = parse_qs(parsed.query)
        share_id = (query.get("shareId") or query.get("share_id") or query.get("id") or [""])[0]
        if not share_id and match:
            share_id = match.group(1)
        code = (query.get("code") or query.get("pwd") or [""])[0]
        if not code:
            code_match = re.search(r"(?:提取码|密码|code)\s*[：:]?\s*([A-Za-z0-9]+)", raw, re.I)
            code = code_match.group(1) if code_match else ""
        if not share_id:
            return {}
        return {
            "share_code": share_id,
            "receive_code": code,
            "share_id": share_id,
            "password": code,
        }

    def _share_access(self, share_url: str) -> tuple[Dict[str, Any], str]:
        info = self.extract_share_info(share_url)
        if not info:
            raise ValueError("无效的光鸭分享链接")
        cache_key = f"{info['share_id']}|{info['password']}"
        with self._share_token_lock:
            cached = self._share_token_cache.get(cache_key)
        if cached:
            return info, str(cached)
        response = self._share_access_token(info["share_id"], info["password"])
        data = self.client.data(response)
        token = str(
            data.get("accessToken") or data.get("access_token") or data.get("token") or ""
        ) if isinstance(data, dict) else ""
        if not self.client.is_success(response) or not token:
            raise RuntimeError(response.get("msg") or response.get("error") or "获取光鸭分享令牌失败")
        with self._share_token_lock:
            self._share_token_cache.set(cache_key, token)
        return info, token

    def check_share_status(self, share_url: str) -> ShareLinkStatus:
        status = ShareLinkStatus()
        if self._offline.is_offline_url(share_url):
            info = (
                self._offline.parse_ed2k_link(share_url)
                if self._offline.is_ed2k_url(share_url)
                else self._offline.parse_magnet_link(share_url)
            )
            status.is_valid = bool(info and self.client.access_token)
            status.file_count = 1 if self._offline.is_ed2k_url(share_url) and info else 0
            status.error_message = "" if status.is_valid else "无效的离线链接或光鸭账号未登录"
            return status
        try:
            info = self.extract_share_info(share_url)
            response = self._share_summary(info.get("share_id") or "")
            if not info or not self.client.is_success(response):
                status.error_message = response.get("msg") or response.get("error") or "分享不可用"
                return status
            status.is_valid = True
            data = self.client.data(response)
            status.file_count = safe_int(data.get("fileCount") or data.get("total")) if isinstance(data, dict) else 0
            status.share_info = data if isinstance(data, dict) else {}
        except Exception as error:
            status.error_message = str(error)
        return status

    def list_share_files(self, share_url: str, **kwargs: Any) -> list:
        if self._offline.is_ed2k_url(share_url):
            info = self._offline.parse_ed2k_link(share_url)
            return [{
                "id": info["hash"],
                "url": info["url"],
                "name": info["name"],
                "size": info["size"],
                "is_dir": False,
                "sha1": "",
                "resource_type": "ed2k",
            }] if info else []
        if self._offline.is_magnet_url(share_url):
            return []
        try:
            info, token = self._share_access(share_url)
            result = []
            stack = [""]
            while stack:
                parent_id = stack.pop()
                page = 1
                while True:
                    response = self._share_files(token, parent_id, page, self.page_size)
                    if not self.client.is_success(response):
                        return result
                    items = list_data(self.client, response)
                    for raw in items:
                        item = cloud_file(raw)
                        if not item:
                            continue
                        if item.is_directory:
                            stack.append(item.id)
                        else:
                            item.playback_values["share_access_token"] = token
                            result.append(dict(item))
                    if len(items) < self.page_size:
                        break
                    page += 1
            return result
        except Exception as error:
            logger.warning(f"读取光鸭分享文件失败：{error}")
            return []

    def list_share_directory(
            self, share_url: str, parent_id: str = ""
    ) -> list:
        """列出分享中的当前目录，并向预览接口保留真实异常。"""
        if self._offline.is_offline_url(share_url):
            return self.list_share_files(share_url)
        _, token = self._share_access(share_url)
        result = []
        page = 1
        while True:
            response = self._share_files(
                token, str(parent_id or ""), page, self.page_size
            )
            if not self.client.is_success(response):
                raise RuntimeError(
                    response.get("msg") or response.get("error") or "读取光鸭分享目录失败"
                )
            items = list_data(self.client, response)
            result.extend(dict(item) for raw in items if (item := cloud_file(raw)))
            if len(items) < self.page_size:
                return result
            page += 1

    def _restore_share(
            self, share_url: str, file_ids: Iterable[str], save_path: str
    ) -> bool:
        info, token = self._share_access(share_url)
        lookup = self._files.resolve_directory(save_path, create=True)
        if not lookup.checked or lookup.directory_id is None:
            return False
        normalized = [str(value) for value in file_ids]
        if not normalized:
            root = self._share_files(token, page=1, page_size=self.page_size)
            normalized = [
                item.id for raw in list_data(self.client, root)
                if (item := cloud_file(raw))
            ]
        if not normalized:
            return False
        response = self._restore(token, normalized, lookup.directory_id)
        success = self.client.is_success(response)
        if not success:
            with self._share_token_lock:
                self._share_token_cache.delete(
                    f"{info['share_id']}|{info['password']}"
                )
        return success

    def transfer_share(self, share_url: str, save_path: str) -> bool:
        return self._restore_share(share_url, [], save_path)

    def transfer_file(
            self, share_url: str, file_id: str, save_path: str,
            target_name: str, **kwargs: Any,
    ) -> bool:
        if self._offline.is_offline_url(share_url):
            return self._offline.add_offline_download(
                share_url, save_path, target_name=target_name
            )
        return self._restore_share(share_url, [file_id], save_path)

    def transfer_files_batch(
            self, share_url: str, file_ids: list, save_path: str, **kwargs: Any
    ) -> tuple:
        normalized = [str(value) for value in file_ids]
        if self._offline.is_offline_url(share_url):
            succeeded = (
                normalized
                if self._offline.add_offline_download(share_url, save_path)
                else []
            )
            return succeeded, [value for value in normalized if value not in succeeded]
        succeeded, failed = [], []
        for batch in iter_transfer_batches(
                normalized, kwargs.get("batch_size", 20),
                kwargs.get("batch_interval", 3), 50,
        ):
            (succeeded if self._restore_share(share_url, batch, save_path) else failed).extend(batch)
        return succeeded, failed
