"""光鸭离线下载能力。"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional
from urllib.parse import unquote

from app.log import logger

from ..common import safe_int
from ...utils.magnet import parse_magnet_metadata

_ED2K_RE = re.compile(
    r"ed2k://\|file\|([^|]+)\|(\d+)\|([0-9A-Fa-f]{32})\|/?", re.I
)


class GuangyaOfflineService:
    """封装光鸭离线接口、链接解析和任务提交。"""

    def __init__(self, client: Any, files: Any):
        self.client = client
        self._files = files

    def create_cloud_task(self, url: str, parent_id: str = "") -> Dict[str, Any]:
        return self.client.request(
            "POST",
            f"{self.client.API_BASE_URL}/nd.bizcloudcollection.s/v1/create_task",
            json_data={"url": url, "parentId": parent_id or ""},
        )

    def resolve_cloud_url(self, url: str) -> Dict[str, Any]:
        return self.client.request(
            "POST",
            f"{self.client.API_BASE_URL}/nd.bizcloudcollection.s/v1/resolve_res",
            json_data={"url": url},
        )

    def cloud_task_list(
            self,
            page: int = 0,
            page_size: int = 50,
            status: Optional[List[int]] = None,
    ) -> Dict[str, Any]:
        return self.client.request(
            "POST",
            f"{self.client.API_BASE_URL}/nd.bizcloudcollection.s/v1/list_task",
            json_data={
                "page": page,
                "pageSize": page_size,
                "status": status if status is not None else [0, 1, 3, 4],
            },
        )

    def get_task_status(self, task_id: str) -> Dict[str, Any]:
        return self.client.request(
            "POST",
            f"{self.client.API_BASE_URL}/nd.bizuserres.s/v1/get_task_status",
            json_data={"taskId": task_id},
        )

    @staticmethod
    def is_ed2k_url(url: str) -> bool:
        return isinstance(url, str) and url.lstrip().lower().startswith("ed2k://")

    @staticmethod
    def is_magnet_url(url: str) -> bool:
        return isinstance(url, str) and url.lstrip().lower().startswith("magnet:?")

    @classmethod
    def is_offline_url(cls, url: str) -> bool:
        return cls.is_ed2k_url(url) or cls.is_magnet_url(url)

    @staticmethod
    def parse_ed2k_link(url: str) -> Dict[str, Any]:
        normalized = str(url or "").replace("｜", "|").strip()
        match = _ED2K_RE.fullmatch(normalized)
        if not match:
            return {}
        return {
            "url": normalized,
            "name": unquote(match.group(1)),
            "size": safe_int(match.group(2)),
            "hash": match.group(3).upper(),
        }

    def parse_magnet_link(
            self, url: str, fetch_metadata: bool = False
    ) -> Dict[str, Any]:
        metadata = parse_magnet_metadata(
            url,
            fetch_info=fetch_metadata,
        )
        if not metadata:
            return {}
        return {
            "url": str(url).strip(),
            "name": metadata.get("display_name") or metadata["info_hash"],
            "size": safe_int(metadata.get("size")),
            "hash": metadata["info_hash"],
            "metadata": metadata,
        }

    def add_offline_download(self, url: str, save_path: str, **kwargs: Any) -> bool:
        if not self.is_offline_url(url):
            return False
        if not self.client.access_token:
            logger.error("添加光鸭离线下载失败：账号未登录")
            return False
        resolved = self.resolve_cloud_url(url)
        if not self.client.is_success(resolved):
            logger.error(
                "添加光鸭离线下载失败："
                f"{resolved.get('msg') or resolved.get('message') or resolved.get('error') or '链接解析失败'}"
            )
            return False
        lookup = self._files.resolve_directory(save_path, create=True)
        if not lookup.checked or lookup.directory_id is None:
            logger.error(f"添加光鸭离线下载失败：无法获取或创建目标目录 {save_path}")
            return False
        created = self.create_cloud_task(url, lookup.directory_id)
        if self.client.is_success(created):
            return True
        logger.error(
            "添加光鸭离线下载失败："
            f"{created.get('msg') or created.get('message') or created.get('error') or '任务创建失败'}"
        )
        return False
