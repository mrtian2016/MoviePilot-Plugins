"""光鸭目录与文件操作能力。"""

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from app.core.cache import TTLCache

from ..common import (
    CloudDriveFileServiceBase,
    create_directory_cache,
    extract_list,
    safe_int,
)
from ...core.cloud import CloudFile
from ...core.transfer import HttpFileDownloadService


def list_data(client: Any, response: Any) -> list:
    return extract_list(
        client.data(response),
        ("list", "files", "items", "records", "fileList", "infoList"),
    )


def cloud_file(item: Any) -> Optional[CloudFile]:
    if not isinstance(item, dict):
        return None
    file_id = item.get("fileId") or item.get("id") or item.get("fid") or item.get("resId")
    name = str(item.get("fileName") or item.get("name") or item.get("filename") or "").strip()
    if file_id in (None, "") or not name:
        return None
    raw_type = item.get("type", item.get("resType", item.get("fileType", item.get("dirType"))))
    is_directory = bool(
        item.get("isDir")
        or item.get("is_dir")
        or item.get("dir")
        or raw_type in (2, "2", "dir", "folder")
    )
    if raw_type in (0, 1, "0", "1", "file"):
        is_directory = False
    return CloudFile(
        id=str(file_id),
        name=name,
        is_directory=is_directory,
        size=0 if is_directory else safe_int(item.get("fileSize") or item.get("size")),
        sha1=str(item.get("sha1") or ""),
        md5=str(item.get("md5") or item.get("gcid") or item.get("gcId") or ""),
        playback_values=(
            {
                "file_id": str(file_id),
                "gcid": str(item.get("gcid") or item.get("gcId") or ""),
            }
            if not is_directory else {}
        ),
        native=item,
    )


@dataclass
class GuangyaFileService(CloudDriveFileServiceBase):
    client: Any
    page_size: int = 100
    root_directory_id = ""
    provider_name = "光鸭"
    provider_key = "guangya"
    _directory_cache: TTLCache = field(init=False, repr=False)

    def __post_init__(self):
        self._directory_cache = create_directory_cache("guangya", self.client)

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
        access_token = str(
            file_item.playback_values.get("share_access_token") or ""
        ).strip()
        if access_token:
            response = self.client.request(
                "POST",
                f"{self.client.API_BASE_URL}/nd.bizuserres.s/v1/get_share_download_url",
                json_data={"fileId": file_item.id, "accessToken": access_token},
                authenticated=False,
            )
        else:
            response = self.client.request(
                "POST",
                f"{self.client.API_BASE_URL}/nd.bizuserres.s/v1/get_res_download_url",
                json_data={"fileId": file_item.id},
            )
        data = self.client.data(response) or {}
        if not isinstance(data, dict):
            data = {}
        url = str(
            data.get("signedUrl") or data.get("downloadUrl")
            or data.get("url") or data.get("fileDownloadUrl") or ""
        ).strip()
        if not url:
            message = str(response.get("msg") or response.get("message") or "")
            raise RuntimeError(message or "光鸭未返回有效下载地址")
        return url, {}

    def _get_file_list(
            self, parent_id: str = "", page: int = 0, page_size: int = 100
    ) -> Dict[str, Any]:
        return self.client.request(
            "POST",
            f"{self.client.API_BASE_URL}/userres/v1/file/get_file_list",
            json_data={
                "parentId": parent_id or "",
                "page": page,
                "pageSize": page_size,
                "orderBy": 0,
                "sortType": 0,
                "fileTypes": [],
            },
        )

    def _create_folder_request(self, name: str, parent_id: str = "") -> Dict[str, Any]:
        return self.client.request(
            "POST",
            f"{self.client.API_BASE_URL}/nd.bizuserres.s/v1/file/create_dir",
            json_data={"dirName": name, "parentId": parent_id or "", "failIfNameExist": True},
        )

    def _list(self, directory_id: str) -> List[CloudFile]:
        directory_id = str(directory_id or "")
        cached = self._directory_cache.get(directory_id)
        if cached is not None:
            return list(cached)
        files: List[CloudFile] = []
        page = 0
        while True:
            response = self._get_file_list(directory_id, page, self.page_size)
            if not self.client.is_success(response):
                raise RuntimeError(response.get("msg") or response.get("error") or "读取光鸭目录失败")
            raw_items = list_data(self.client, response)
            files.extend(item for raw in raw_items if (item := cloud_file(raw)))
            data = self.client.data(response)
            total = safe_int(data.get("total") if isinstance(data, dict) else 0)
            if len(raw_items) < self.page_size or (total and len(files) >= total):
                self._directory_cache.set(directory_id, tuple(files))
                return files
            page += 1

    def _create_folder(self, name: str, parent_id: str) -> Optional[CloudFile]:
        response = self._create_folder_request(name, parent_id)
        if not self._is_success(response):
            raise RuntimeError(response.get("msg") or response.get("error") or "创建光鸭目录失败")
        self._invalidate_directory_cache()
        return cloud_file(self.client.data(response))

    def _is_success(self, response: Any) -> bool:
        return self.client.is_success(response)

    def rename_file(self, path: str, item: CloudFile, target_name: str) -> bool:
        response = self.client.request(
            "POST",
            f"{self.client.API_BASE_URL}/nd.bizuserres.s/v1/file/rename",
            json_data={"fileId": item.id, "newName": target_name},
        )
        success = self._is_success(response)
        if success:
            self._invalidate_directory_cache()
            if item.is_directory:
                self._invalidate_path_cache()
        return success

    def move_file(
            self, item: CloudFile, save_path: str, target_name: str
    ) -> Optional[CloudFile]:
        lookup = self.resolve_directory(save_path, create=True)
        if not lookup.checked or lookup.directory_id is None:
            return None
        moved = self.client.request(
            "POST",
            f"{self.client.API_BASE_URL}/nd.bizuserres.s/v1/file/move_file",
            json_data={"fileIds": [item.id], "parentId": lookup.directory_id or ""},
        )
        if not self._is_success(moved):
            return None
        self._invalidate_directory_cache()
        if item.is_directory:
            self._invalidate_path_cache()
        if target_name and target_name != item.name:
            if not self.rename_file(save_path, item, target_name):
                return None
        return self.find_file(save_path, target_name or item.name)

    def delete_file(self, file_id: str) -> bool:
        response = self.client.request(
            "POST",
            f"{self.client.API_BASE_URL}/nd.bizuserres.s/v1/file/delete_file",
            json_data={"fileIds": [file_id]},
        )
        success = self._is_success(response)
        if success:
            self._invalidate_directory_cache()
            self._invalidate_path_cache()
        return success

    def _batch_action_completed(self, response: Dict[str, Any]) -> bool:
        if not self._is_success(response):
            return False
        data = self.client.data(response)
        task_id = str(
            data.get("taskId") or data.get("task_id") or ""
        ) if isinstance(data, dict) else ""
        if not task_id:
            return True
        for retry_index in range(120):
            status_response = self.client.request(
                "POST",
                f"{self.client.API_BASE_URL}/nd.bizuserres.s/v1/get_task_status",
                json_data={"taskId": task_id},
            )
            status_data = self.client.data(status_response)
            status = (
                status_data.get("status", status_data.get("taskStatus"))
                if isinstance(status_data, dict) else None
            )
            if status in (2, "2", "success", "done", "finished"):
                return True
            if status in (3, "3", "failed", "error") or status_response.get(
                    "code"
            ) in (145, "145"):
                return False
            if retry_index < 119:
                time.sleep(0.5)
        return False

    def move_files(
            self, items: dict[str, CloudFile], save_path: str
    ) -> dict[str, CloudFile]:
        """使用光鸭 move_file 原生批量接口。"""
        lookup = self.resolve_directory(save_path, create=True)
        if not lookup.checked or lookup.directory_id is None:
            return {}
        entries = list(dict(items or {}).items())
        moved = {}
        for offset in range(0, len(entries), 50):
            batch = entries[offset:offset + 50]
            response = self.client.request(
                "POST",
                f"{self.client.API_BASE_URL}/nd.bizuserres.s/v1/file/move_file",
                json_data={
                    "fileIds": [item.id for _, item in batch],
                    "parentId": lookup.directory_id or "",
                },
            )
            if self._batch_action_completed(response):
                moved.update({str(key): item for key, item in batch})
        if moved:
            self._invalidate_directory_cache()
            if any(item.is_directory for item in moved.values()):
                self._invalidate_path_cache()
        return moved

    def delete_files(self, file_ids: list[str]) -> set[str]:
        """使用光鸭 delete_file 原生批量接口。"""
        file_ids = list(dict.fromkeys(
            str(value) for value in (file_ids or []) if str(value or "")
        ))
        deleted = set()
        for offset in range(0, len(file_ids), 50):
            batch = file_ids[offset:offset + 50]
            response = self.client.request(
                "POST",
                f"{self.client.API_BASE_URL}/nd.bizuserres.s/v1/file/delete_file",
                json_data={"fileIds": batch},
            )
            if self._batch_action_completed(response):
                deleted.update(batch)
        if deleted:
            self._invalidate_directory_cache()
            self._invalidate_path_cache()
        return deleted
