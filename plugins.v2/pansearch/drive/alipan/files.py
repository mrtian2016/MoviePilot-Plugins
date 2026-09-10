"""阿里云盘目录、文件变更与下载能力。"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import PurePosixPath
from typing import Any, Dict, List

from app.core.cache import TTLCache

from ..common import (
    CloudDriveFileServiceBase,
    create_directory_cache,
    create_directory_path_cache,
    resolve_directory_path,
)
from ...core.cloud import CloudFile, DirectoryListing, DirectoryLookup
from ...core.transfer import HttpFileDownloadService


def cloud_file(item: dict | None) -> CloudFile | None:
    if not item or not item.get("file_id") or not item.get("name"):
        return None
    is_directory = item.get("type") == "folder"
    return CloudFile(
        id=str(item["file_id"]),
        name=str(item["name"]),
        is_directory=is_directory,
        size=0 if is_directory else int(item.get("size") or 0),
        sha1=str(item.get("content_hash") or "")
        if str(item.get("content_hash_name") or "").lower() == "sha1" else "",
        playback_values={
            "file_id": str(item["file_id"]),
            "drive_id": str(item.get("drive_id") or ""),
        } if not is_directory else {},
        native=item,
    )


@dataclass
class AliPanFileService(CloudDriveFileServiceBase):
    provider_name = "阿里云盘"

    client: Any
    _items: dict[str, dict] = field(default_factory=dict)
    _directory_cache: TTLCache = field(init=False, repr=False)

    def __post_init__(self):
        self._directory_cache = create_directory_cache("alipan", self.client)
        self._directory_path_cache = create_directory_path_cache(
            "alipan", self.client, "root"
        )

    def _remember(self, item: dict) -> CloudFile | None:
        value = cloud_file(item)
        if value:
            self._items[value.id] = item
        return value

    def _root(self) -> dict:
        self.client.ensure_session()
        return {
            "file_id": "root", "name": "/", "type": "folder",
            "drive_id": self.client.drive_id,
        }

    def native_item(self, file_id: str | None) -> dict | None:
        if str(file_id or "") == "root":
            return self._root()
        return self._items.get(str(file_id or ""))

    def resolve_directory(self, path: str, create: bool = False) -> DirectoryLookup:
        self._items["root"] = self._root()
        return resolve_directory_path(
            path,
            root_directory_id="root",
            path_cache=self._directory_path_cache,
            list_children=lambda directory_id: self.list_directory(directory_id).files,
            create_child=self._create_folder,
            create=create,
            provider_name="阿里云盘",
        )

    def _create_folder(self, name: str, parent_id: str) -> CloudFile | None:
        child = self.client.request("/adrive/v2/file/createWithFolders", {
            "drive_id": self.client.drive_id,
            "parent_file_id": parent_id,
            "name": name,
            "type": "folder",
            "check_name_mode": "refuse",
        })
        if not child:
            return None
        self._invalidate_directory_cache()
        return self._remember(child)

    def _list_native(self, directory_id: str) -> list[dict]:
        directory_id = str(directory_id or "root")
        cached = self._directory_cache.get(directory_id)
        if cached is not None:
            return [dict(item) for item in cached]
        marker = ""
        result = []
        while True:
            data = self.client.request("/adrive/v3/file/list", {
                "drive_id": self.client.drive_id,
                "parent_file_id": directory_id or "root",
                "fields": "*", "limit": 200, "marker": marker,
            })
            result.extend(data.get("items") or [])
            marker = str(data.get("next_marker") or "")
            if not marker:
                self._directory_cache.set(
                    directory_id, tuple(dict(item) for item in result)
                )
                return result

    def list_directory(self, directory_id: str) -> DirectoryListing:
        values = tuple(
            value for child in self._list_native(directory_id or "root")
            if (value := self._remember(child)) is not None
        )
        return DirectoryListing(True, values)

    def list_directories(self, path: str) -> list[dict[str, str]]:
        lookup = self.resolve_directory(path)
        if not lookup.directory_id:
            return []
        base = PurePosixPath("/" + str(path or "").strip("/"))
        return [
            {"id": item.id, "name": item.name, "path": str(base / item.name)}
            for item in self.list_directory(lookup.directory_id).files
            if item.is_directory
        ]

    def list_files_recursive(self, path: str, **kwargs) -> list[CloudFile]:
        lookup = self.resolve_directory(path)
        if not lookup.directory_id:
            return []
        result = []
        stack = [lookup.directory_id]
        while stack:
            for item in self.list_directory(stack.pop()).files:
                if item.is_directory:
                    stack.append(item.id)
                else:
                    result.append(item)
        return result

    def find_file(self, path: str, file_name: str, **kwargs) -> CloudFile | None:
        lookup = self.resolve_directory(path)
        if not lookup.directory_id:
            return None
        item = next(
            (value for value in self._list_native(lookup.directory_id)
             if value.get("name") == file_name), None,
        )
        return self._remember(item) if item else None

    find_file_strict = find_file
    get_cached_file = find_file

    def rename_file(self, path: str, item: CloudFile, target_name: str) -> bool:
        data = self.client.request("/v2/file/update", {
            "drive_id": self.client.drive_id, "file_id": item.id,
            "name": target_name, "check_name_mode": "refuse",
        })
        success = bool(data.get("file_id"))
        if success:
            self._invalidate_directory_cache()
            if item.is_directory:
                self._directory_path_cache.clear()
        return success

    def move_file(self, item: CloudFile, save_path: str, target_name: str) -> CloudFile | None:
        lookup = self.resolve_directory(save_path, create=True)
        if not lookup.directory_id:
            return None
        requests_payload = [{
            "body": {
                "drive_id": self.client.drive_id, "file_id": item.id,
                "to_parent_file_id": lookup.directory_id,
                "auto_rename": False,
            },
            "headers": {"Content-Type": "application/json"},
            "id": item.id, "method": "POST", "url": "/file/move",
        }]
        self.client.request("/v2/batch", {
            "requests": requests_payload, "resource": "file",
        })
        self._invalidate_directory_cache()
        if item.is_directory:
            self._directory_path_cache.clear()
        if target_name and target_name != item.name:
            self.rename_file(save_path, item, target_name)
        return self.find_file(save_path, target_name or item.name)

    def delete_file(self, file_id: str) -> bool:
        data = self.client.request("/v2/batch", {
            "requests": [{
                "body": {"drive_id": self.client.drive_id, "file_id": file_id},
                "headers": {"Content-Type": "application/json"},
                "id": file_id, "method": "POST", "url": "/recyclebin/trash",
            }],
            "resource": "file",
        })
        success = bool((data.get("responses") or [{}])[0].get("id"))
        if success:
            self._invalidate_directory_cache()
            self._directory_path_cache.clear()
        return success

    def _batch_request(self, requests: List[Dict[str, Any]]) -> set[str]:
        if not requests:
            return set()
        data = self.client.request("/v2/batch", {
            "requests": requests,
            "resource": "file",
        })
        succeeded = set()
        for response in data.get("responses") or []:
            request_id = str(response.get("id") or "")
            try:
                status = int(response.get("status") or 200)
            except (TypeError, ValueError):
                status = 0
            body = response.get("body")
            body_code = body.get("code") if isinstance(body, dict) else None
            if request_id and 200 <= status < 300 and body_code in (None, 0, "0"):
                succeeded.add(request_id)
        return succeeded

    def rename_files(self, path: str, items: dict) -> dict[str, CloudFile]:
        """使用阿里云盘 /v2/batch 批量更新文件名。"""
        entries = list(dict(items or {}).items())
        renamed = {}
        for offset in range(0, len(entries), 100):
            batch = entries[offset:offset + 100]
            succeeded = self._batch_request([
                {
                    "body": {
                        "drive_id": self.client.drive_id,
                        "file_id": value["item"].id,
                        "name": str(value["target_name"]),
                        "check_name_mode": "refuse",
                    },
                    "headers": {"Content-Type": "application/json"},
                    "id": str(key),
                    "method": "POST",
                    "url": "/file/update",
                }
                for key, value in batch
                if value.get("item") and value.get("target_name")
            ])
            for key, value in batch:
                if str(key) not in succeeded:
                    continue
                renamed[str(key)] = replace(
                    value["item"], name=str(value["target_name"])
                )
        if renamed:
            self._invalidate_directory_cache()
            if any(item.is_directory for item in renamed.values()):
                self._directory_path_cache.clear()
        return renamed

    def move_files(
            self, items: dict[str, CloudFile], save_path: str
    ) -> dict[str, CloudFile]:
        """使用阿里云盘 /v2/batch 批量移动文件。"""
        lookup = self.resolve_directory(save_path, create=True)
        if not lookup.checked or lookup.directory_id is None:
            return {}
        entries = list(dict(items or {}).items())
        moved = {}
        for offset in range(0, len(entries), 100):
            batch = entries[offset:offset + 100]
            succeeded = self._batch_request([
                {
                    "body": {
                        "drive_id": self.client.drive_id,
                        "file_id": item.id,
                        "to_parent_file_id": lookup.directory_id,
                        "auto_rename": False,
                    },
                    "headers": {"Content-Type": "application/json"},
                    "id": str(key),
                    "method": "POST",
                    "url": "/file/move",
                }
                for key, item in batch
            ])
            moved.update({
                str(key): item for key, item in batch if str(key) in succeeded
            })
        if moved:
            self._invalidate_directory_cache()
            if any(item.is_directory for item in moved.values()):
                self._directory_path_cache.clear()
        return moved

    def delete_files(self, file_ids: list[str]) -> set[str]:
        """使用阿里云盘 /v2/batch 批量移入回收站。"""
        file_ids = list(dict.fromkeys(
            str(value) for value in (file_ids or []) if str(value or "")
        ))
        deleted = set()
        for offset in range(0, len(file_ids), 100):
            batch = file_ids[offset:offset + 100]
            deleted.update(self._batch_request([
                {
                    "body": {
                        "drive_id": self.client.drive_id,
                        "file_id": file_id,
                    },
                    "headers": {"Content-Type": "application/json"},
                    "id": file_id,
                    "method": "POST",
                    "url": "/recyclebin/trash",
                }
                for file_id in batch
            ]))
        if deleted:
            self._invalidate_directory_cache()
            self._directory_path_cache.clear()
        return deleted

    def resolve_download_link(self, file_item: CloudFile) -> tuple[str, dict]:
        data = self.client.request("/v2/file/get_download_url", {
            "drive_id": self.client.drive_id,
            "file_id": file_item.id,
            "expire_sec": 14400,
        })
        url = str(data.get("url") or data.get("internal_url") or "")
        if not url:
            raise RuntimeError("阿里云盘未返回下载地址")
        headers = {"Referer": "https://www.aliyundrive.com/"}
        return url, headers

    def download_file(self, file_item: CloudFile, local_path: str,
                      progress_callback=None, stop_requested=None,
                      preserve_partial: bool = False,
                      download_threads: int = 5) -> str:
        url, headers = self.resolve_download_link(file_item)
        return HttpFileDownloadService(
            lambda _: (url, headers), concurrency=download_threads,
        ).download_file(
            file_item, local_path, progress_callback, stop_requested,
            preserve_partial=preserve_partial,
        )
