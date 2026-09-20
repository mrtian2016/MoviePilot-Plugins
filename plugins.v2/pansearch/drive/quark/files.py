"""夸克目录与文件操作能力。"""

import time
from dataclasses import dataclass, field
from threading import RLock
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
    return extract_list(client.data(response), ("list", "files", "items", "records"))


def cloud_file(item: Any) -> Optional[CloudFile]:
    if not isinstance(item, dict):
        return None
    file_id = item.get("fid") or item.get("file_id") or item.get("id")
    name = str(item.get("file_name") or item.get("name") or item.get("filename") or "").strip()
    if file_id in (None, "") or not name:
        return None
    file_type = item.get("file_type")
    is_directory = bool(item.get("dir") or item.get("is_dir") or file_type == 0)
    if file_type not in (None, 0, "0"):
        is_directory = False
    return CloudFile(
        id=str(file_id),
        name=name,
        is_directory=is_directory,
        size=0 if is_directory else safe_int(item.get("size") or item.get("file_size")),
        sha1=str(item.get("sha1") or ""),
        md5=str(item.get("md5") or ""),
        playback_values={"file_id": str(file_id)} if not is_directory else {},
        native=item,
    )


@dataclass
class QuarkFileService(CloudDriveFileServiceBase):
    DOWNLOAD_PARAMS = {
        "sys": "win32", "ve": "2.5.20", "ut": "", "guid": "",
    }
    DOWNLOAD_WEB_USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36 Edg/143.0.0.0"
    )
    DOWNLOAD_DESKTOP_USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) quark-cloud-drive/2.5.20 Chrome/100.0.4896.160 "
        "Electron/18.3.5.4-b478491100 Safari/537.36 Channel/pckk_other_ch"
    )

    client: Any
    page_size: int = 100
    root_directory_id = "0"
    provider_name = "夸克"
    provider_key = "quark"
    _directory_lock: RLock = field(default_factory=RLock, repr=False)
    _directory_cache: TTLCache = field(init=False, repr=False)

    def __post_init__(self):
        self._directory_cache = create_directory_cache("quark", self.client)

    def download_file(self, file_item: CloudFile, local_path: str,
                      progress_callback=None, stop_requested=None,
                      preserve_partial: bool = False,
                      download_threads: int = 5) -> str:
        url, headers = self.resolve_download_link(file_item)
        service = HttpFileDownloadService(
            lambda _: (url, headers),
            concurrency=download_threads,
            part_size=10 * 1024 * 1024,
        )
        return service.download_file(
            file_item, local_path, progress_callback, stop_requested,
            preserve_partial=preserve_partial,
        )

    def resolve_download_link(self, file_item: CloudFile) -> tuple[str, dict]:
        entry = {}
        response = {}
        download_user_agent = self.DOWNLOAD_WEB_USER_AGENT
        for user_agent in (
                self.DOWNLOAD_WEB_USER_AGENT,
                self.DOWNLOAD_DESKTOP_USER_AGENT,
        ):
            response = self.client.request(
                "POST", "file/download",
                params=self.DOWNLOAD_PARAMS,
                json_data={"fids": [file_item.id]},
                base_url=self.client.SHARE_BASE_URL,
                request_headers={"user-agent": user_agent},
            )
            data = self.client.data(response) or []
            entry = data[0] if isinstance(data, list) and data else {}
            download_user_agent = user_agent
            if entry.get("download_url"):
                break
            if int(response.get("code") or 0) != 23018:
                break
        if not entry.get("download_url"):
            raise RuntimeError(
                response.get("message") or "夸克未返回文件下载地址"
            )
        return (
            str(entry["download_url"]),
            self.client.download_headers({"user-agent": download_user_agent}),
        )

    def _get_file_list(
            self, parent_id: str = "0", page: int = 1, size: int = 100
    ) -> Dict[str, Any]:
        return self.client.request(
            "GET",
            "file/sort",
            params={
                "pdir_fid": parent_id,
                "_page": page,
                "_size": size,
                "_sort": "file_name:asc",
            },
        )

    def _create_folder_request(
            self, name: str, parent_id: str = "0"
    ) -> Dict[str, Any]:
        return self.client.request(
            "POST",
            "file",
            json_data={
                "pdir_fid": parent_id,
                "file_name": name,
                "dir_init_lock": False,
                "dir_path": "",
            },
        )

    def _list(self, directory_id: str) -> List[CloudFile]:
        directory_id = str(directory_id or self.root_directory_id)
        cached = self._directory_cache.get(directory_id)
        if cached is not None:
            return list(cached)
        files: List[CloudFile] = []
        page = 1
        while True:
            response = self._get_file_list(directory_id, page, self.page_size)
            if not self.client.is_success(response):
                raise RuntimeError(response.get("message") or "读取夸克目录失败")
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
            message = str(response.get("message") or "创建夸克目录失败")
            if "doloading" not in message.lower() and "同名冲突" not in message:
                raise RuntimeError(message)
        else:
            created = cloud_file(self.client.data(response))
            if created:
                self._invalidate_directory_cache()
                return created

        self._invalidate_directory_cache()
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            time.sleep(0.5)
            try:
                existing = next(
                    (
                        item for item in self._list(parent_id)
                        if item.is_directory and item.name == name
                    ),
                    None,
                )
            except RuntimeError:
                continue
            if existing:
                return existing
        raise RuntimeError(f"夸克目录创建后长时间不可见：{name}")

    def _is_success(self, response: Any) -> bool:
        return self.client.is_success(response)

    def rename_file(self, path: str, item: CloudFile, target_name: str) -> bool:
        response = self.client.request(
            "POST", "file/rename",
            json_data={"fid": item.id, "file_name": target_name},
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
            "POST", "file/move",
            json_data={
                "action_type": 1,
                "to_pdir_fid": lookup.directory_id,
                "filelist": [item.id],
                "exclude_fids": [],
            },
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
            "POST", "file/delete",
            json_data={"action_type": 2, "filelist": [file_id], "exclude_fids": []},
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
        if not isinstance(data, dict):
            return True
        task_id = str(data.get("task_id") or "")
        if task_id and data.get("finish") is not True:
            return self.client.wait_for_task(task_id)
        return data.get("finish") is not False

    def move_files(
            self, items: dict[str, CloudFile], save_path: str
    ) -> dict[str, CloudFile]:
        """使用夸克 file/move 原生批量接口并等待异步任务完成。"""
        lookup = self.resolve_directory(save_path, create=True)
        if not lookup.checked or lookup.directory_id is None:
            return {}
        entries = list(dict(items or {}).items())
        moved = {}
        for offset in range(0, len(entries), 50):
            batch = entries[offset:offset + 50]
            response = self.client.request(
                "POST", "file/move",
                json_data={
                    "action_type": 1,
                    "to_pdir_fid": lookup.directory_id,
                    "filelist": [item.id for _, item in batch],
                    "exclude_fids": [],
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
        """使用夸克 file/delete 原生批量接口并等待异步任务完成。"""
        file_ids = list(dict.fromkeys(
            str(value) for value in (file_ids or []) if str(value or "")
        ))
        deleted = set()
        for offset in range(0, len(file_ids), 50):
            batch = file_ids[offset:offset + 50]
            response = self.client.request(
                "POST", "file/delete",
                json_data={
                    "action_type": 2,
                    "filelist": batch,
                    "exclude_fids": [],
                },
            )
            if self._batch_action_completed(response):
                deleted.update(batch)
        if deleted:
            self._invalidate_directory_cache()
            self._invalidate_path_cache()
        return deleted
