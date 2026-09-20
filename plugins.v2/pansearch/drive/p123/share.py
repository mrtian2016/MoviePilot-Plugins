"""123 分享访问与转存能力。"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, Mapping
from urllib.parse import parse_qs, urlsplit

from app.log import logger

from .client import P123_AVAILABLE, is_success
from .files import cloud_file
from ..common import iter_transfer_batches
from ...core.cloud import ShareLinkStatus

try:
    from p123client.tool import share_iterdir
except ImportError:
    share_iterdir = None


class P123ShareService:
    """使用 p123client 枚举分享树并将选中文件转存到账户。"""

    def __init__(self, client: Any, files: Any):
        self.client = client
        self._files = files
        self.page_size = files.page_size
        self._share_items: Dict[str, Dict[str, Dict[str, Any]]] = {}

    @staticmethod
    def extract_share_info(share_url: str) -> Dict[str, Any]:
        value = str(share_url or "").strip()
        if not value:
            return {}
        match = re.search(
            r"(?:https?://(?:[^/]*\.)?"
            r"(?:123pan\.(?:com|cn)|123684\.com|123685\.com|123865\.com|"
            r"123912\.com|123592\.com)/(?:s|123pan)/"
            r"|123://share/)([\w-]+)",
            value,
            re.I,
        )
        if not match:
            return {}
        password = ""
        try:
            query = parse_qs(urlsplit(value).query)
            password = str((query.get("pwd") or query.get("code") or [""])[0]).strip()
        except ValueError:
            pass
        if not password:
            code_match = re.search(
                r"(?:提取码|密码|pwd|code)\s*[：:=]?\s*([^\s&#]+)", value, re.I
            )
            password = code_match.group(1) if code_match else ""
        return {
            "share_code": match.group(1),
            "receive_code": password,
            "share_key": match.group(1),
            "share_pwd": password,
        }

    @staticmethod
    def _cache_key(info: Mapping[str, Any]) -> str:
        return f"{info.get('share_key', '')}|{info.get('share_pwd', '')}"

    def _iterate_share(self, info: Mapping[str, Any], max_depth: int = -1):
        if not P123_AVAILABLE or share_iterdir is None:
            raise RuntimeError("p123client 未安装")
        stack = [(0, 0)]
        while stack:
            parent_id, depth = stack.pop()
            items = self.client.rate_limiter.call(
                lambda: list(share_iterdir(
                    share_key=str(info.get("share_key") or ""),
                    share_pwd=str(info.get("share_pwd") or ""),
                    payload=parent_id,
                    max_depth=1,
                    keep_raw=True,
                ))
            )
            for item in items:
                yield item
                if item.get("is_dir") and (max_depth < 0 or depth + 1 < max_depth):
                    stack.append((int(item["id"]), depth + 1))

    def check_share_status(self, share_url: str) -> ShareLinkStatus:
        status = ShareLinkStatus()
        info = self.extract_share_info(share_url)
        if not info:
            status.error_message = "无效的 123 分享链接"
            return status
        try:
            first = next(self._iterate_share(info, max_depth=1), None)
            status.is_valid = True
            status.file_count = int((first or {}).get("total_siblings") or bool(first))
        except Exception as error:
            message = str(error)
            status.error_message = message or "123 分享不可用"
            lowered = message.lower()
            status.is_expired = "过期" in message or "expired" in lowered
            status.is_cancelled = "取消" in message or "cancel" in lowered
            status.is_deleted = "删除" in message or "不存在" in message
        return status

    def list_share_files(self, share_url: str, **kwargs: Any) -> list:
        info = self.extract_share_info(share_url)
        if not info:
            return []
        max_depth = int(kwargs.get("max_depth", -1) or -1)
        cache_key = self._cache_key(info)
        cached: Dict[str, Dict[str, Any]] = {}
        files = []
        try:
            for item in self._iterate_share(info, max_depth=max_depth):
                file_item = cloud_file(item)
                if not file_item or file_item.is_directory:
                    continue
                raw = item.get("raw")
                cached[file_item.id] = {
                    "item": dict(item),
                    "raw": dict(raw) if isinstance(raw, Mapping) else {},
                }
                files.append(dict(file_item))
            self._share_items[cache_key] = cached
            return files
        except Exception as error:
            logger.warning(f"读取123分享文件失败：{error}")
            return []

    def list_share_directory(
            self, share_url: str, parent_id: str = ""
    ) -> list:
        """列出分享中的当前目录，并向预览接口保留真实异常。"""
        info = self.extract_share_info(share_url)
        if not info:
            raise ValueError("无效的 123 分享链接")
        if not P123_AVAILABLE or share_iterdir is None:
            raise RuntimeError("p123client 未安装")
        directory_id = int(parent_id or 0)
        rows = self.client.rate_limiter.call(
            lambda: list(share_iterdir(
                share_key=str(info.get("share_key") or ""),
                share_pwd=str(info.get("share_pwd") or ""),
                payload=directory_id,
                max_depth=1,
                keep_raw=True,
            ))
        )
        result = []
        for raw in rows:
            item = cloud_file(raw)
            if item:
                result.append(dict(item))
        return result

    def _resolve_items(
            self, share_url: str, file_ids: Iterable[str]
    ) -> tuple[Dict[str, Any], Dict[str, Dict[str, Any]]]:
        info = self.extract_share_info(share_url)
        if not info:
            return {}, {}
        cache_key = self._cache_key(info)
        requested = {str(value) for value in file_ids}
        cached = self._share_items.get(cache_key, {})
        if not requested.issubset(cached):
            self.list_share_files(share_url)
            cached = self._share_items.get(cache_key, {})
        return info, {file_id: cached[file_id] for file_id in requested if file_id in cached}

    @staticmethod
    def _copy_payload(item: Mapping[str, Any]) -> Dict[str, Any]:
        normalized = item.get("item") or {}
        raw = item.get("raw") or {}
        return {
            "file_id": normalized.get("id") or raw.get("FileId"),
            "file_name": normalized.get("name") or raw.get("FileName"),
            "etag": normalized.get("md5") or raw.get("Etag") or "",
            "size": normalized.get("size") or raw.get("Size") or 0,
            "type": raw.get("Type", 0),
            "drive_id": raw.get("DriveId", 0),
            "s3_key_flag": normalized.get("s3keyflag") or raw.get("S3KeyFlag") or "",
        }

    def _copy(self, share_url: str, file_ids: list, save_path: str) -> bool:
        info, items = self._resolve_items(share_url, file_ids)
        if not info or len(items) != len({str(value) for value in file_ids}):
            return False
        lookup = self._files.resolve_directory(save_path, create=True)
        if not lookup.checked or lookup.directory_id is None:
            return False
        response = self.client.share_fs_copy(
            {
                "share_key": info["share_key"],
                "share_pwd": info["share_pwd"],
                "file_list": [self._copy_payload(items[str(file_id)]) for file_id in file_ids],
            },
            parent_id=int(lookup.directory_id),
        )
        return is_success(response)

    def transfer_share(self, share_url: str, save_path: str) -> bool:
        files = self.list_share_files(share_url)
        if not files:
            return False
        file_ids = [str(item["id"]) for item in files]
        succeeded, failed = self.transfer_files_batch(
            share_url, file_ids, save_path, batch_size=100
        )
        return len(succeeded) == len(file_ids) and not failed

    def transfer_file(
            self, share_url: str, file_id: str, save_path: str,
            target_name: str, **kwargs: Any,
    ) -> bool:
        return self._copy(share_url, [str(file_id)], save_path)

    def transfer_files_batch(
            self, share_url: str, file_ids: list, save_path: str, **kwargs: Any
    ) -> tuple:
        normalized = list(dict.fromkeys(str(value) for value in file_ids))
        succeeded = []
        failed = []
        for batch in iter_transfer_batches(
                normalized, kwargs.get("batch_size", 20),
                kwargs.get("batch_interval", 3), 100,
        ):
            (succeeded if self._copy(share_url, batch, save_path) else failed).extend(batch)
        return succeeded, failed
