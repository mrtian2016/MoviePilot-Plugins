"""天翼目录读取与文件查询。"""

import json
import time
from pathlib import PurePosixPath

from app.core.cache import TTLCache

from ..common import (
    CloudDriveFileServiceBase,
    create_directory_cache,
    create_directory_path_cache,
    resolve_directory_path,
)
from ...core.cloud import CloudFile, DirectoryListing, DirectoryLookup
from ...core.transfer import HttpFileDownloadService


class TianyiFileService(CloudDriveFileServiceBase):
    provider_name = "天翼"

    def __init__(self, client):
        self.client = client
        self._items_by_id: dict[str, CloudFile] = {}
        self._directory_path_cache = create_directory_path_cache(
            "tianyi", client, "-11"
        )
        self._directory_cache: TTLCache = create_directory_cache(
            "tianyi", client
        )

    def _remember(self, item: CloudFile) -> CloudFile:
        if item.id:
            self._items_by_id[item.id] = item
        return item

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

    def resolve_download_link(self, file_item: CloudFile) -> tuple[str, dict]:
        data = self.client.request(
            "GET", "https://cloud.189.cn/api/portal/getFileInfo.action",
            params={"fileId": file_item.id},
        )
        url = str(data.get("downloadUrl") or data.get("fileDownloadUrl") or "")
        if url.startswith("//"):
            url = "https:" + url
        url = url.replace("http://", "https://", 1)
        headers = {
            "Cookie": self.client.session.headers.get("Cookie", ""),
            "Referer": "https://cloud.189.cn/",
        }
        return url, headers

    def resolve_directory(self, path: str, create: bool = False) -> DirectoryLookup:
        return resolve_directory_path(
            path,
            root_directory_id="-11",
            path_cache=self._directory_path_cache,
            list_children=lambda directory_id: self.list_directory(directory_id).files,
            create_child=self._create_folder,
            create=create,
            provider_name="天翼",
        )

    def _create_folder(self, name: str, parent_id: str) -> CloudFile | None:
        data = self.client.request(
            "POST", "https://cloud.189.cn/api/open/file/createFolder.action",
            data={"parentFolderId": parent_id, "folderName": name},
        )
        self._invalidate_directory_cache()
        result = data.get("data") if isinstance(data.get("data"), dict) else data
        folder_id = str(
            result.get("id") or result.get("folderId")
            or result.get("fileId") or ""
        )
        if not folder_id:
            return None
        return self._remember(CloudFile(folder_id, name, True, native=result))

    def list_directory(self, directory_id: str) -> DirectoryListing:
        directory_id = str(directory_id or "-11")
        cached = self._directory_cache.get(directory_id)
        if cached is not None:
            return cached
        result, page = [], 1
        while True:
            data = self.client.request("GET", "https://cloud.189.cn/api/open/file/listFiles.action",
                                       params={"pageSize": 60, "pageNum": page, "mediaType": 0,
                                               "folderId": directory_id, "iconOption": 5,
                                               "orderBy": "lastOpTime", "descending": "true"})
            info = data.get("fileListAO")
            if not isinstance(info, dict):
                raise RuntimeError("天翼目录响应缺少 fileListAO")
            for item in info.get("folderList") or []:
                result.append(self._remember(
                    CloudFile(str(item.get("id") or ""), str(item.get("name") or ""), True, native=item)))
            for item in info.get("fileList") or []:
                result.append(self._remember(CloudFile(str(item.get("id") or ""), str(item.get("name") or ""), False,
                                                       size=int(item.get("size") or 0), md5=str(item.get("md5") or ""),
                                                       native=item)))
            if len(result) >= int(info.get("count") or 0):
                break
            page += 1
        listing = DirectoryListing(True, tuple(result))
        self._directory_cache.set(directory_id, listing)
        return listing

    def list_directories(self, path: str):
        lookup = self.resolve_directory(path)
        if not lookup.directory_id:
            return []
        base = PurePosixPath("/" + str(path or "/").strip("/"))
        return [
            {"id": f.id, "name": f.name, "path": str(base / f.name)}
            for f in self.list_directory(lookup.directory_id).files
            if f.is_directory
        ]

    def find_file(self, path: str, file_name: str, **kwargs):
        lookup = self.resolve_directory(path)
        if not lookup.directory_id:
            return None
        return next((f for f in self.list_directory(lookup.directory_id).files if f.name == file_name), None)

    find_file_strict = find_file
    get_cached_file = find_file

    def _find_with_retry(self, path: str, file_name: str) -> CloudFile | None:
        for index in range(10):
            if item := self.find_file(path, file_name):
                return item
            if index < 9:
                time.sleep(0.5)
        return None

    def list_files_recursive(self, path: str, **kwargs):
        lookup = self.resolve_directory(path)
        if not lookup.directory_id:
            return []
        return self._list_files_recursive_by_id(lookup.directory_id)

    def _list_files_recursive_by_id(self, directory_id: str) -> list[CloudFile]:
        result = []
        for item in self.list_directory(directory_id).files:
            if item.is_directory:
                result.extend(self._list_files_recursive_by_id(item.id))
            else:
                result.append(item)
        return result

    def rename_file(self, path: str, item: CloudFile, target_name: str) -> bool:
        url = "https://cloud.189.cn/api/open/file/renameFolder.action" \
            if item.is_directory else "https://cloud.189.cn/api/open/file/renameFile.action"
        data = (
            {"folderId": item.id, "destFolderName": target_name}
            if item.is_directory
            else {"fileId": item.id, "destFileName": target_name}
        )
        self.client.request("POST", url, data=data)
        self._invalidate_directory_cache()
        success = self._find_with_retry(path, target_name) is not None
        if success and item.is_directory:
            self._directory_path_cache.clear()
        return success

    def _batch_task(
            self, task_type: str, items: list[CloudFile], target_id: str = ""
    ) -> None:
        items = [item for item in items if item]
        if not items:
            raise ValueError("天翼批量任务缺少文件")
        created = self.client.request(
            "POST", "https://cloud.189.cn/api/open/batch/createBatchTask.action",
            data={
                "type": task_type,
                "targetFolderId": target_id,
                "taskInfos": json.dumps([
                    {
                        "fileId": item.id,
                        "fileName": item.name,
                        "isFolder": 1 if item.is_directory else 0,
                    }
                    for item in items
                ], ensure_ascii=False),
            },
        )
        task_id = str(created.get("taskId") or created.get("task_id") or "")
        if not task_id:
            raise RuntimeError("天翼批量任务未返回任务 ID")
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            status = self.client.request(
                "POST", "https://cloud.189.cn/api/open/batch/checkBatchTask.action",
                data={"type": task_type, "taskId": task_id},
            )
            task_status = int(status.get("taskStatus") or 0)
            if task_status == 4:
                if int(status.get("failedCount") or 0) > 0:
                    raise RuntimeError("天翼批量任务存在失败文件")
                return
            if task_status == 2:
                raise RuntimeError("天翼批量任务存在同名冲突")
            time.sleep(0.4)
        raise TimeoutError("等待天翼批量任务完成超时")

    def move_file(
            self, item: CloudFile, save_path: str, target_name: str
    ) -> CloudFile | None:
        lookup = self.resolve_directory(save_path, create=True)
        if not lookup.directory_id:
            return None
        self._batch_task("MOVE", [item], lookup.directory_id)
        self._invalidate_directory_cache()
        if item.is_directory:
            self._directory_path_cache.clear()
        if target_name and target_name != item.name:
            moved = CloudFile(
                item.id, item.name, item.is_directory, item.size,
                item.sha1, item.md5, item.playback_values, item.native,
            )
            if not self.rename_file(save_path, moved, target_name):
                return None
        return self._find_with_retry(save_path, target_name or item.name)

    def delete_file(self, file_id: str) -> bool:
        item = self._items_by_id.get(str(file_id or ""))
        if not item:
            return False
        self._batch_task("DELETE", [item])
        self._invalidate_directory_cache()
        self._items_by_id.pop(item.id, None)
        if item.is_directory:
            self._directory_path_cache.clear()
        return True

    def move_files(
            self, items: dict[str, CloudFile], save_path: str
    ) -> dict[str, CloudFile]:
        """使用天翼批量任务一次移动最多 100 项。"""
        lookup = self.resolve_directory(save_path, create=True)
        if not lookup.checked or lookup.directory_id is None:
            return {}
        entries = list(dict(items or {}).items())
        moved = {}
        for offset in range(0, len(entries), 100):
            batch = entries[offset:offset + 100]
            try:
                self._batch_task(
                    "MOVE", [item for _, item in batch], lookup.directory_id
                )
            except (RuntimeError, TimeoutError, ValueError):
                continue
            moved.update({str(key): item for key, item in batch})
        if moved:
            self._invalidate_directory_cache()
            if any(item.is_directory for item in moved.values()):
                self._directory_path_cache.clear()
        return moved

    def delete_files(self, file_ids: list[str]) -> set[str]:
        """使用天翼批量任务一次回收最多 100 项。"""
        file_ids = list(dict.fromkeys(
            str(value) for value in (file_ids or []) if str(value or "")
        ))
        deleted = set()
        for offset in range(0, len(file_ids), 100):
            batch_ids = file_ids[offset:offset + 100]
            items = [
                self._items_by_id[file_id]
                for file_id in batch_ids
                if file_id in self._items_by_id
            ]
            if not items:
                continue
            try:
                self._batch_task("DELETE", items)
            except (RuntimeError, TimeoutError, ValueError):
                continue
            success_ids = {item.id for item in items}
            deleted.update(success_ids)
            for file_id in success_ids:
                self._items_by_id.pop(file_id, None)
        if deleted:
            self._invalidate_directory_cache()
            self._directory_path_cache.clear()
        return deleted
