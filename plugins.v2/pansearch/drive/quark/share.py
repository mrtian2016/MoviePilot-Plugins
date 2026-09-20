"""夸克分享访问与转存能力。"""

from __future__ import annotations

import re
import time
from threading import RLock
from typing import Any, Dict, Iterable

from app.log import logger

from .files import cloud_file, list_data
from ..common import safe_int
from ...core.cloud import ShareLinkStatus
from ...utils.cache import create_platform_ttl_cache


class QuarkShareService:
    """封装夸克分享接口、文件遍历和转存。"""

    def __init__(self, client: Any, files: Any):
        self._files = files
        self.client = client
        self.page_size = files.page_size
        self._share_items: Dict[str, Dict[str, Dict[str, str]]] = {}
        self._share_tokens = create_platform_ttl_cache(
            "quark:share_tokens",
            client,
            maxsize=256,
            ttl=10 * 60,
        )
        self._share_items_lock = RLock()
        self._transfer_blocked_until = 0.0
        self._transfer_block_reason = ""

    @property
    def transfer_risk_blocked(self) -> bool:
        """返回转存接口是否因明确的账号风控信号处于熔断期。"""
        return time.monotonic() < self._transfer_blocked_until

    def _get_share_token(self, share_id: str, password: str = "") -> Dict[str, Any]:
        return self.client.request(
            "POST",
            "share/sharepage/token",
            json_data={
                "pwd_id": share_id,
                "passcode": password,
                "support_visit_limit_private_share": True,
            },
            base_url=self.client.SHARE_PAGE_BASE_URL,
            request_timeout=(2, self.client.request_timeout),
        )

    def _get_share_files(
            self, share_id: str, token: str, parent_id: str = "0",
            page: int = 1, size: int = 100,
    ) -> Dict[str, Any]:
        return self.client.request(
            "GET",
            "share/sharepage/detail",
            params={
                "pwd_id": share_id,
                "stoken": token,
                "pdir_fid": parent_id,
                "force": "0",
                "_page": page,
                "_size": size,
                "_fetch_banner": "1",
                "_fetch_share": "1",
                "_fetch_total": "1",
                "_sort": "file_type:asc,file_name:asc",
            },
            base_url=self.client.SHARE_PAGE_BASE_URL,
            request_timeout=(2, self.client.request_timeout),
        )

    def _save_shared_files(
            self, share_id: str, token: str, file_ids: list, target_id: str,
            file_tokens: list,
    ) -> Dict[str, Any]:
        payload = {
            "fid_list": file_ids,
            "fid_token_list": file_tokens,
            "to_pdir_fid": target_id,
            "pwd_id": share_id,
            "stoken": token,
            "pdir_fid": "0",
            "scene": "link",
        }
        if not file_ids:
            payload.update({"pdir_save_all": True, "exclude_fids": []})
        result = self.client.request(
            "POST",
            "share/sharepage/save",
            json_data=payload,
            base_url=self.client.SHARE_BASE_URL,
        )
        task_id = (self.client.data(result) or {}).get("task_id")
        if self.client.is_success(result) and task_id:
            result["task_success"] = self.client.wait_for_task(str(task_id))
        return result

    @staticmethod
    def extract_share_info(share_url: str) -> Dict[str, Any]:
        match = re.search(
            r"(?:https?://pan\.quark\.cn/s/|quark://share/)([A-Za-z0-9]+)",
            share_url or "",
            re.I,
        )
        if not match:
            return {}
        code_match = re.search(
            r"(?:提取码|密码|code)\s*[：:]?\s*([A-Za-z0-9]+)",
            share_url,
            re.I,
        )
        receive_code = code_match.group(1) if code_match else ""
        return {
            "share_code": match.group(1),
            "receive_code": receive_code,
            "share_id": match.group(1),
            "password": receive_code,
        }

    def _share_access(self, share_url: str) -> tuple[Dict[str, Any], str]:
        info = self.extract_share_info(share_url)
        if not info:
            raise ValueError("无效的夸克分享链接")
        cache_key = f"{info['share_id']}|{info['password']}"
        with self._share_items_lock:
            cached = self._share_tokens.get(cache_key)
        if cached:
            return info, str(cached)
        response = self._get_share_token(info["share_id"], info["password"])
        token = str((self.client.data(response) or {}).get("stoken") or "")
        if not self.client.is_success(response) or not token:
            raise RuntimeError(response.get("message") or "获取夸克分享令牌失败")
        with self._share_items_lock:
            self._share_tokens.set(cache_key, token)
        return info, token

    def check_share_status(self, share_url: str) -> ShareLinkStatus:
        status = ShareLinkStatus()
        try:
            info, token = self._share_access(share_url)
            response = self._get_share_files(info["share_id"], token, size=1)
            if not self.client.is_success(response):
                status.error_message = response.get("message") or "分享不可用"
                return status
            status.is_valid = True
            data = self.client.data(response)
            status.file_count = safe_int(data.get("total") if isinstance(data, dict) else 0)
            status.share_info = {
                "share_title": str(data.get("title") or "")
                if isinstance(data, dict) else ""
            }
        except Exception as error:
            status.error_message = str(error)
        return status

    def list_share_files(self, share_url: str, **kwargs: Any) -> list:
        try:
            info, token = self._share_access(share_url)
            result = []
            share_items: Dict[str, Dict[str, str]] = {}
            stack = ["0"]
            while stack:
                parent_id = stack.pop()
                page = 1
                while True:
                    response = self._get_share_files(
                        info["share_id"], token, parent_id, page, self.page_size
                    )
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
                            share_items[item.id] = {
                                "token": str(raw.get("share_fid_token") or ""),
                                "parent_id": str(raw.get("pdir_fid") or parent_id),
                            }
                            result.append(dict(item))
                    if len(items) < self.page_size:
                        break
                    page += 1
            with self._share_items_lock:
                self._share_items[info["share_id"]] = share_items
                self._share_tokens.set(
                    f"{info['share_id']}|{info['password']}", token
                )
            return result
        except Exception as error:
            logger.warning(f"读取夸克分享文件失败：{error}")
            return []

    def list_share_directory(
            self, share_url: str, parent_id: str = ""
    ) -> list:
        """列出分享中的当前目录，预览调用方需要保留目录节点和错误。"""
        info, token = self._share_access(share_url)
        result = []
        page = 1
        directory_id = str(parent_id or "0")
        while True:
            response = self._get_share_files(
                info["share_id"], token, directory_id, page, self.page_size
            )
            if not self.client.is_success(response):
                raise RuntimeError(
                    response.get("message") or response.get("msg") or "读取夸克分享目录失败"
                )
            items = list_data(self.client, response)
            result.extend(dict(item) for raw in items if (item := cloud_file(raw)))
            if len(items) < self.page_size:
                return result
            page += 1

    def _save_share(
            self, share_url: str, file_ids: Iterable[str], save_path: str
    ) -> bool:
        if time.monotonic() < self._transfer_blocked_until:
            logger.warning(
                f"夸克转存已熔断，暂不重试：{self._transfer_block_reason}"
            )
            return False
        info = self.extract_share_info(share_url)
        if not info:
            raise ValueError("无效的夸克分享链接")
        normalized = list(dict.fromkeys(str(value) for value in file_ids))
        file_tokens = []
        if normalized:
            with self._share_items_lock:
                cached = dict(self._share_items.get(info["share_id"], {}))
                token = str(self._share_tokens.get(
                    f"{info['share_id']}|{info['receive_code']}"
                ) or "")
            if not token or not all(file_id in cached for file_id in normalized):
                self.list_share_files(share_url)
                with self._share_items_lock:
                    cached = dict(self._share_items.get(info["share_id"], {}))
                    token = str(self._share_tokens.get(
                        f"{info['share_id']}|{info['receive_code']}"
                    ) or "")
            file_tokens = [
                str((cached.get(file_id) or {}).get("token") or "")
                for file_id in normalized
            ]
            if not token or not all(file_tokens):
                logger.error(
                    f"夸克分享文件缺少 stoken 或 share_fid_token，无法转存："
                    f"{len([value for value in file_tokens if value])}/{len(normalized)}"
                )
                return False
        else:
            _, token = self._share_access(share_url)
        lookup = self._files.resolve_directory(save_path, create=True)
        if not lookup.checked or lookup.directory_id is None:
            logger.error(f"夸克转存目录不可用：{save_path}")
            return False
        result = self._save_shared_files(
            info["share_id"], token, normalized, lookup.directory_id, file_tokens,
        )
        success = (
                self.client.is_success(result)
                and result.get("task_success", True) is not False
        )
        if not success:
            message = str(result.get("message") or result.get("msg") or "")
            if any(marker in message for marker in ("封禁", "风控", "限制", "频繁")):
                self._transfer_block_reason = message or "账号转存受限"
                self._transfer_blocked_until = time.monotonic() + max(
                    300, int(getattr(self.client, "risk_cooldown", 1800))
                )
            logger.error(
                f"夸克分享转存失败：{message or '异步任务未完成'}，"
                f"文件数={len(normalized) or '全部'}，目录={save_path}"
            )
            with self._share_items_lock:
                self._share_tokens.delete(
                    f"{info['share_id']}|{info['receive_code']}"
                )
        return success

    def transfer_share(self, share_url: str, save_path: str) -> bool:
        return self._save_share(share_url, [], save_path)

    def transfer_file(
            self, share_url: str, file_id: str, save_path: str,
            target_name: str, **kwargs: Any,
    ) -> bool:
        return self._save_share(share_url, [file_id], save_path)

    def transfer_files_batch(
            self, share_url: str, file_ids: list, save_path: str, **kwargs: Any
    ) -> tuple:
        normalized = [str(value) for value in file_ids]
        batch_size = max(1, min(int(kwargs.get("batch_size", 5) or 5), 20))
        interval = max(0.0, min(float(kwargs.get("batch_interval", 3) or 0), 60.0))
        self.client.risk_cooldown = max(
            60, min(int(kwargs.get("risk_cooldown", 1800) or 1800), 86400)
        )
        succeeded = []
        failed = []
        for offset in range(0, len(normalized), batch_size):
            batch = normalized[offset:offset + batch_size]
            if self._save_share(share_url, batch, save_path):
                succeeded.extend(batch)
            else:
                failed.extend(batch)
                if time.monotonic() < self._transfer_blocked_until:
                    failed.extend(normalized[offset + len(batch):])
                    break
            if offset + len(batch) < len(normalized):
                time.sleep(interval)
        return succeeded, failed
