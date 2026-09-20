"""115 目录、文件查询和文件变更能力。"""

import hashlib
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Iterable, List, Mapping, Optional, Set, Tuple

from app.core.cache import TTLCache
from app.log import logger

from ..common import (
    create_directory_cache,
    error_http_status,
    normalize_path,
    retry_transient_call,
    safe_int,
)
from ...core import OwnerDelegator
from ...core.cloud import CloudFile, DirectoryListing, DirectoryLookup, native_dict
from ...core.transfer import HttpFileDownloadService

try:
    from p115client.tool.edit import batch_delete, batch_move, makedir, update_name
    from p115client.tool.iterdir import iterdir
except ImportError:
    batch_delete = batch_move = iterdir = makedir = update_name = None


def cloud_file(item: Any) -> CloudFile | None:
    if not isinstance(item, Mapping):
        return None
    raw = dict(item)
    fid = raw.get("fid")
    is_directory = bool(raw.get("is_dir") or str(fid) == "0")
    file_id = raw.get("file_id") or raw.get("id") or (raw.get("cid") if is_directory else fid)
    name = str(raw.get("name") or raw.get("n") or raw.get("file_name") or "").strip()
    if file_id in (None, "") or not name:
        return None
    pickcode = raw.get("pick_code") or raw.get("pickcode") or raw.get("pc")
    return CloudFile(
        id=str(file_id),
        name=name,
        is_directory=is_directory,
        size=safe_int(raw.get("size") or raw.get("s")),
        sha1=str(raw.get("sha1") or raw.get("sha") or ""),
        md5=str(raw.get("md5") or ""),
        playback_values={"pickcode": str(pickcode)} if pickcode else {},
        native=raw,
    )


def cloud_files(items: Iterable[Any]) -> list[CloudFile]:
    return [file for item in (items or []) if (file := cloud_file(item)) is not None]


@dataclass(frozen=True)
class P115DirectoryReader:
    manager: Any
    _directory_cache: TTLCache = field(init=False, repr=False, compare=False)

    def __post_init__(self):
        object.__setattr__(
            self,
            "_directory_cache",
            create_directory_cache("115", self.manager),
        )

    def resolve_directory(self, path: str, create: bool = False) -> DirectoryLookup:
        if create:
            directory_id = self.manager.get_pid_by_path(path, mkdir=True)
            return DirectoryLookup(
                checked=directory_id != -1,
                directory_id=str(directory_id) if directory_id != -1 else None,
            )
        checked, directory_id = self.manager.get_pid_by_path_checked(path)
        return DirectoryLookup(
            checked=bool(checked),
            directory_id=str(directory_id) if checked and directory_id != -1 else None,
        )

    def list_directory(self, directory_id: str) -> DirectoryListing:
        directory_id = str(directory_id or "0")
        cached = self._directory_cache.get(directory_id)
        if cached is not None:
            return cached
        checked, files = self.manager.list_files_by_cid_checked(directory_id)
        listing = DirectoryListing(bool(checked), tuple(cloud_files(files)))
        if listing.checked:
            self._directory_cache.set(directory_id, listing)
        return listing

    def list_directories(self, path: str) -> list[Dict[str, str]]:
        lookup = self.resolve_directory(path)
        if not lookup.checked or lookup.directory_id is None:
            raise RuntimeError(f"无法读取115目录：{path}")
        listing = self.list_directory(lookup.directory_id)
        if not listing.checked:
            raise RuntimeError(f"列出115目录失败：{path}")
        base = PurePosixPath(normalize_path(path))
        return [
            {
                "id": item.id,
                "name": item.name,
                "path": str(base / item.name),
            }
            for item in listing.files
            if item.is_directory
        ]

    def refresh_directories(self) -> None:
        self._directory_cache.clear()
        self.manager.clear_path_cache()


@dataclass(frozen=True)
class P115FileQuery:
    manager: Any

    def list_files_recursive(self, path: str, **kwargs: Any) -> list[CloudFile]:
        return cloud_files(self.manager.list_files_recursive(path, **kwargs))

    def find_file(self, path: str, file_name: str, **kwargs: Any) -> CloudFile | None:
        return cloud_file(self.manager.find_file_by_exact_name(path, file_name, **kwargs))

    def find_file_strict(self, path: str, file_name: str) -> CloudFile | None:
        return cloud_file(self.manager.find_file_for_delete(path, file_name))

    def get_cached_file(self, path: str, file_name: str) -> CloudFile | None:
        return cloud_file(self.manager.get_cached_target_file(path, file_name))

    def download_file(self, file_item: CloudFile, local_path: str,
                      progress_callback=None, stop_requested=None,
                      preserve_partial: bool = False,
                      download_threads: int = 5) -> str:
        url, headers = self.resolve_download_link(file_item)
        service = HttpFileDownloadService(
            lambda _: (url, headers), concurrency=download_threads,
        )
        return service.download_file(
            file_item, local_path, progress_callback, stop_requested,
            preserve_partial=preserve_partial,
        )

    def resolve_download_link(self, file_item: CloudFile) -> tuple[str, dict]:
        pickcode = str(file_item.playback_values.get("pickcode") or "")
        client = getattr(self.manager, "client", None)
        if not client or not pickcode:
            raise RuntimeError("115 文件缺少 pickcode 或客户端未登录")
        value = self.manager.rate_limiter.call(client.download_url, pickcode)
        return str(value or ""), dict(getattr(value, "headers", None) or {})


@dataclass(frozen=True)
class P115FileMutation:
    manager: Any

    def rename_file_by_checksum(
            self, path: str, checksum: str, target_name: str,
            algorithm: str = "sha1", **kwargs: Any,
    ) -> bool:
        if str(algorithm).lower() != "sha1":
            raise ValueError("115网盘仅支持按 SHA1 定位文件")
        return self.manager.rename_file_by_sha1(path, checksum, target_name, **kwargs)

    def rename_file(self, path: str, item: CloudFile, target_name: str) -> bool:
        # item 可能是 CloudFile，也可能是提供方原始 dict（整包 magnet 终态
        # 链路会把 dict 形态的条目透传到驱动层）。统一归一化后再取 .name，
        # 否则直接读属性会抛 AttributeError: 'dict' object has no attribute 'name'。
        normalized = item if isinstance(item, CloudFile) else cloud_file(item)
        if normalized is None:
            return False
        return self.manager.rename_file_item(
            path, native_dict(normalized), normalized.name, target_name
        )

    def move_file(
            self, item: CloudFile, save_path: str, target_name: str
    ) -> CloudFile | None:
        return cloud_file(
            self.manager.move_and_rename_file(native_dict(item), save_path, target_name)
        )

    def delete_file(self, file_id: str) -> bool:
        return self.manager.delete_file(file_id)


@dataclass(frozen=True)
class P115BatchFileMutation:
    manager: Any

    def rename_files(self, path: str, items: dict) -> dict[str, CloudFile]:
        native_items = {
            str(key): {**dict(value), "item": native_dict(value["item"])}
            for key, value in items.items()
        }
        renamed = self.manager.rename_file_items_batch(path, native_items)
        return {
            str(key): file for key, item in (renamed or {}).items()
            if (file := cloud_file(item)) is not None
        }

    def move_files(
            self, items: dict[str, CloudFile], save_path: str
    ) -> dict[str, CloudFile]:
        moved = self.manager.move_file_items_batch(
            {str(key): native_dict(item) for key, item in items.items()}, save_path
        )
        return {
            str(key): file for key, item in (moved or {}).items()
            if (file := cloud_file(item)) is not None
        }

    def delete_files(self, file_ids: list[str]) -> set[str]:
        return self.manager.delete_files_batch(file_ids)


class P115FileService(OwnerDelegator):
    DIRECTORY_PAGE_SIZE = 1000
    MUTATION_BATCH_SIZE = 1000
    # web 渠道偶发 405，切换 ios 渠道前先做有界退避重试。
    _ITER_RETRY_ATTEMPTS = 3
    _ITER_RETRY_DELAYS = (0.5, 1.0)

    def _iter_directory(self, cid: Any, ensure_file: Optional[bool] = None):
        """按页读取一个115目录，并让工具层处理字段标准化和响应校验。

        瞬态异常（网络抖动/5xx/429）指数退避重试；持续 405 时切换
        ios 渠道重放同一接口，返回行结构保持一致。
        """
        def _fetch(**channel):
            return self.rate_limiter.call(
                iterdir,
                self.client,
                cid=cid,
                page_size=self.DIRECTORY_PAGE_SIZE,
                show_dir=1,
                fc_mix=0,
                ensure_file=ensure_file,
                cooldown=self.rate_limiter.min_interval,
                max_workers=0,
                max_retries=0,
                **channel,
                **self._ios_request_kwargs(app=False),
            )

        try:
            return retry_transient_call(
                lambda: _fetch(app="web"),
                attempts=self._ITER_RETRY_ATTEMPTS,
                delays=self._ITER_RETRY_DELAYS,
            )
        except Exception as error:
            if error_http_status(error) != 405:
                raise
            logger.warning(
                f"115 目录列举持续 405，切换 ios 渠道重试：cid={cid}，{error}"
            )
            return retry_transient_call(
                lambda: _fetch(app="ios"),
                attempts=self._ITER_RETRY_ATTEMPTS,
                delays=self._ITER_RETRY_DELAYS,
            )

    def _list_child_directories(self, cid: Any) -> List[dict]:
        """只读取目录项；115 目录与文件可能混合排序，不能依赖“目录置顶”提前停止。"""
        return list(self._iter_directory(cid, ensure_file=False))

    def _rename_items(self, pairs: List[Tuple[Any, str]]) -> Set[str]:
        if not pairs:
            return set()
        renamed = self._rate_limited_call(
            update_name,
            self.client,
            pairs,
            batch_size=self.MUTATION_BATCH_SIZE,
            app="web",
            **self._ios_request_kwargs(app=False),
        )
        return {str(file_id) for file_id in renamed}

    def _move_items(self, file_ids: Iterable[Any], target_pid: Any) -> None:
        normalized = [str(file_id) for file_id in file_ids if file_id not in (None, "")]
        if not normalized:
            return
        self._rate_limited_call(
            batch_move,
            self.client,
            normalized,
            pid=target_pid,
            batch_size=self.MUTATION_BATCH_SIZE,
            app="web",
            **self._ios_request_kwargs(app=False),
        )

    def _delete_items(self, file_ids: Iterable[Any]) -> Set[str]:
        normalized = list(dict.fromkeys(
            str(file_id) for file_id in file_ids if file_id not in (None, "")
        ))
        if not normalized:
            return set()
        self._rate_limited_call(
            batch_delete,
            self.client,
            normalized,
            batch_size=self.MUTATION_BATCH_SIZE,
            app="web",
            **self._ios_request_kwargs(app=False),
        )
        return set(normalized)

    def _resolve_pid_by_path(
            self, path: str, create: bool
    ) -> Tuple[bool, int]:
        """按路径解析目录 CID；优先服务端一次解析，失败回退逐级定位。

        返回 (检查是否成功, 目录CID)；CID 为 -1 表示确认目录不存在。
        """
        if not self.client:
            return False, -1
        raw_path = str(path or "").replace("\\", "/")
        if "://" in raw_path:
            logger.warning(f"115目录路径不能是URL，已跳过目录解析：{raw_path}")
            return False, -1
        normalized = normalize_path(raw_path)
        if normalized == "/":
            return True, 0

        cached_cid = self.path_cache.get(normalized)
        if cached_cid is not None:
            return True, int(cached_cid)

        directory_id = self._resolve_directory_id_api(normalized, create)
        if directory_id is None:
            logger.debug(f"115 路径一次解析不可用，回退逐级定位：{normalized}")
            return self._resolve_pid_by_path_walk(normalized, create)
        if directory_id == -1:
            if create:
                logger.debug(f"115 路径一次解析未创建目录，回退逐级创建：{normalized}")
                return self._resolve_pid_by_path_walk(normalized, create)
            return True, -1
        self.path_cache.set(normalized, directory_id)
        return True, directory_id

    def _resolve_directory_id_api(
            self, normalized: str, create: bool
    ) -> Optional[int]:
        """调用 115 服务端 files/getid 一次解析目录；不可用时返回 None。

        返回目录 CID；-1 表示目录不存在，None 表示接口不可用或解析失败。
        """
        try:
            if create:
                response = self._rate_limited_call(
                    self.client.fs_dir_getid2,
                    {"path": normalized, "is_create": 1},
                )
                raw_id = (response.get("data") or {}).get("file_id")
            else:
                response = self._rate_limited_call(
                    self.client.fs_dir_getid,
                    {"path": normalized},
                )
                raw_id = response.get("id")
        except Exception as error:
            logger.warning(f"115 路径一次解析异常 {normalized}: {error}")
            return None

        if not response or not response.get("state"):
            logger.warning(
                f"115 路径一次解析失败 {normalized}: "
                f"{(response or {}).get('error') or (response or {}).get('message') or ''}"
            )
            return None

        if raw_id in (None, "", 0, "0"):
            return -1
        try:
            return int(raw_id)
        except (TypeError, ValueError):
            logger.warning(f"115 目录ID解析失败：{normalized} -> {raw_id!r}")
            return None

    def _resolve_pid_by_path_walk(
            self, normalized: str, create: bool
    ) -> Tuple[bool, int]:
        """逐级列举目录定位 CID，作为服务端一次解析的兜底。"""
        parent_id = 0
        current_path = ""
        for part in (part for part in normalized.split("/") if part):
            current_path = f"{current_path}/{part}"
            cached = self.path_cache.get(current_path)
            if cached is not None:
                parent_id = int(cached)
                continue

            try:
                directories = self._list_child_directories(parent_id)
            except Exception as error:
                logger.warning(f"检查115目录异常 {current_path}: {error}")
                return False, -1

            matches = [
                item for item in directories
                if str(item.get("name") or "") == part and item.get("id")
            ]
            if matches:
                if len(matches) > 1:
                    logger.debug(
                        f"检测到 {len(matches)} 个同名目录 {current_path}，"
                        f"复用列表首项，禁止继续创建重复目录"
                    )
                parent_id = int(matches[0]["id"])
                self.path_cache.set(current_path, parent_id)
                continue

            if not create:
                return True, -1

            try:
                parent_id = self._rate_limited_call(
                    makedir,
                    self.client,
                    part,
                    pid=parent_id,
                    contain_dir=True,
                    app="ios",
                    **self._ios_request_kwargs(app=False),
                )
                self.path_cache.set(current_path, parent_id)
                logger.debug(f"创建目录成功: {current_path} -> {parent_id}")
            except Exception as error:
                logger.error(f"创建目录异常 {current_path}: {error}")
                return False, -1

        return True, parent_id

    def get_pid_by_path(self, path: str, mkdir: bool = True) -> int:
        """通过路径获取目录 CID；需要时逐级创建目录。"""
        checked, directory_id = self._resolve_pid_by_path(path, create=mkdir)
        return directory_id if checked else -1

    def get_pid_by_path_checked(self, path: str) -> Tuple[bool, int]:
        """只读解析目录，区分“确认不存在”和“115接口检查失败”。"""
        return self._resolve_pid_by_path(path, create=False)

    def rename_file_by_sha1(
            self,
            save_path: str,
            source_sha1: str,
            target_name: str,
            attempts: int = 1,
            interval: float = 1.5,
    ) -> bool:
        """按网盘 SHA1 定位转存文件并应用平台文件名。"""
        if not target_name:
            return True
        source_hash = self._normalize_hash(source_sha1)
        total_attempts = max(1, int(attempts))
        for attempt in range(total_attempts):
            files = self.list_files(save_path) or []
            for item in files:
                current_name = str(
                    item.get("name") or item.get("n")
                    or item.get("file_name") or ""
                ).strip()
                if current_name == target_name:
                    self._cache_target_file(save_path, target_name, item)
                    return True

            if len(source_hash) != 40:
                logger.info(f"目标文件尚未就绪，暂无法按平台规则确认：{target_name}")
                return False

            for item in files:
                item_hash = self._normalize_hash(item.get("sha1") or item.get("sha"))
                if item_hash != source_hash:
                    continue
                file_id = item.get("fid") or item.get("id")
                current_name = str(
                    item.get("name") or item.get("n")
                    or item.get("file_name") or ""
                )
                try:
                    if str(file_id) in self._rename_items([(file_id, target_name)]):
                        renamed_item = dict(item)
                        renamed_item.update({"name": target_name, "n": target_name})
                        self._cache_target_file(save_path, target_name, renamed_item)
                        logger.info(f"115 文件按平台规则重命名成功：{current_name} -> {target_name}")
                        return True
                    logger.error(f"115 文件重命名失败：{current_name} -> {target_name}")
                    return False
                except Exception as error:
                    logger.error(f"115 文件重命名异常：{error}")
                    return False
            if attempt + 1 < total_attempts:
                time.sleep(max(0, float(interval)))
        logger.info(
            f"115文件仍在系统处理，暂未按SHA1定位：{target_name}，稍后重试"
        )
        return False

    def rename_files_by_sha1_batch(
            self,
            save_path: str,
            rename_items: Dict[str, Dict[str, str]],
            success_ids: List[str],
            log_unresolved: bool = True,
    ) -> Tuple[Dict[str, dict], List[str]]:
        """使用一次目录快照和一次批量请求重命名转存文件。"""
        if not rename_items or not success_ids:
            return {}, []

        files = self.list_files(save_path) or []
        files_by_name: Dict[str, dict] = {}
        files_by_sha1: Dict[str, dict] = {}
        for item in files:
            current_name = str(
                item.get("name") or item.get("n")
                or item.get("file_name") or ""
            ).strip()
            if current_name:
                files_by_name[current_name] = item
            item_hash = self._normalize_hash(item.get("sha1") or item.get("sha"))
            if len(item_hash) == 40:
                files_by_sha1[item_hash] = item

        ready: Dict[str, dict] = {}
        unresolved: List[str] = []
        rename_pairs = []
        pending_items: Dict[str, Tuple[dict, str, str]] = {}
        for file_id in success_ids:
            key = str(file_id)
            rename_item = rename_items.get(key) or rename_items.get(file_id) or {}
            target_name = str(rename_item.get("target_name") or "").strip()
            if not target_name:
                continue

            item = files_by_name.get(target_name)
            if item:
                self._cache_target_file(save_path, target_name, item)
                ready[key] = dict(item)
                continue

            source_hash = self._normalize_hash(rename_item.get("sha1"))
            item = files_by_sha1.get(source_hash) if len(source_hash) == 40 else None
            if not item:
                unresolved.append(key)
                continue

            current_name = str(
                item.get("name") or item.get("n")
                or item.get("file_name") or ""
            ).strip()
            if (
                    current_name
                    and Path(current_name).suffix.lower() != Path(target_name).suffix.lower()
            ):
                logger.warning(
                    f"拒绝重命名扩展名不一致的转存文件：{current_name} -> {target_name}"
                )
                unresolved.append(key)
                continue
            target_file_id = item.get("fid") or item.get("id")
            if not target_file_id:
                unresolved.append(key)
                continue
            rename_pairs.append((target_file_id, target_name))
            pending_items[key] = (item, target_name, str(target_file_id))

        if rename_pairs:
            try:
                renamed_ids = self._rename_items(rename_pairs)
                for key, (item, target_name, target_file_id) in pending_items.items():
                    if target_file_id not in renamed_ids:
                        unresolved.append(key)
                        continue
                    renamed_item = dict(item)
                    renamed_item.update({"name": target_name, "n": target_name})
                    pickcode = (
                            renamed_item.get("pick_code")
                            or renamed_item.get("pickcode")
                            or renamed_item.get("pc")
                    )
                    if pickcode:
                        renamed_item["pickcode"] = str(pickcode)
                    self._cache_target_file(save_path, target_name, renamed_item)
                    ready[key] = renamed_item
                logger.debug(
                    f"115 批量重命名完成：成功 {len(ready)} 个，"
                    f"待后处理 {len(unresolved)} 个"
                )
            except Exception as error:
                logger.warning(f"115 批量重命名异常：{error}")
                unresolved.extend(pending_items)

        unresolved = list(dict.fromkeys(unresolved))
        if unresolved and log_unresolved:
            logger.debug(f"批量转存中有 {len(unresolved)} 个文件尚未就绪，进入后处理队列")
        return ready, unresolved

    def rename_file_by_exact_name(
            self, save_path: str, current_name: str, target_name: str
    ) -> bool:
        """在确定的目标目录内按离线任务真实文件名执行平台重命名。"""
        current_name = str(current_name or "").strip()
        target_name = str(target_name or "").strip()
        if not current_name or not target_name:
            return False
        if Path(current_name).suffix.lower() != Path(target_name).suffix.lower():
            logger.warning(
                f"拒绝重命名扩展名不一致的文件：{current_name} -> {target_name}"
            )
            return False
        if current_name == target_name:
            return bool(
                self.find_file_by_exact_name(save_path, target_name, attempts=1)
            )

        for item in self.list_files(save_path):
            item_name = str(
                item.get("name")
                or item.get("n")
                or item.get("file_name")
                or ""
            ).strip()
            if item_name != current_name:
                continue
            return self.rename_file_item(save_path, item, current_name, target_name)
        return False

    def rename_file_item(
            self,
            save_path: str,
            item: Dict[str, Any],
            current_name: str,
            target_name: str,
    ) -> bool:
        """重命名已从目录快照定位的文件，避免再次读取同一115目录。"""
        file_id = item.get("fid") or item.get("id")
        if not file_id:
            return False
        try:
            if str(file_id) not in self._rename_items([(file_id, target_name)]):
                logger.warning(
                    f"115 文件重命名失败：{current_name} -> {target_name}"
                )
                return False
            renamed_item = dict(item)
            renamed_item.update({"name": target_name, "n": target_name})
            pickcode = (
                    renamed_item.get("pick_code")
                    or renamed_item.get("pickcode")
                    or renamed_item.get("pc")
            )
            if pickcode:
                renamed_item["pickcode"] = str(pickcode)
            self._cache_target_file(save_path, target_name, renamed_item)
            if item.get("is_dir"):
                self.path_cache.clear()
            logger.info(
                f"115 文件重命名成功："
                f"{current_name} -> {target_name}"
            )
            return True
        except Exception as error:
            logger.warning(
                f"115 文件重命名异常：{current_name} -> {target_name}，{error}"
            )
            return False

    def rename_file_items_batch(
            self,
            save_path: str,
            rename_items: Dict[str, Dict[str, Any]],
    ) -> Dict[str, dict]:
        """下载完成后批量改为平台文件名，并复核115目录中的真实结果。"""
        if not self.client or not rename_items:
            return {}
        rename_pairs = []
        pending_items: Dict[str, Tuple[dict, str, str]] = {}
        ready: Dict[str, dict] = {}
        for key, rename_item in rename_items.items():
            item = dict(rename_item.get("item") or {})
            target_name = str(rename_item.get("target_name") or "").strip()
            current_name = str(
                item.get("name") or item.get("n")
                or item.get("file_name") or ""
            ).strip()
            file_id = item.get("fid") or item.get("id")
            if not file_id or not current_name or not target_name:
                continue
            if Path(current_name).suffix.lower() != Path(target_name).suffix.lower():
                logger.warning(
                    f"拒绝批量重命名扩展名不一致的文件："
                    f"{current_name} -> {target_name}"
                )
                continue
            if current_name == target_name:
                self._cache_target_file(save_path, target_name, item)
                ready[str(key)] = item
                continue
            rename_pairs.append((file_id, target_name))
            pending_items[str(key)] = (item, target_name, str(file_id))

        if not rename_pairs:
            return ready
        try:
            renamed_ids = self._rename_items(rename_pairs)
            verified_ids = set()
            expected_ids = {
                file_id for _, _, file_id in pending_items.values()
                if file_id in renamed_ids
            }
            for attempt in range(3):
                actual_files = self.list_files(save_path)
                actual_by_id = {
                    str(item.get("fid") or item.get("id")): item
                    for item in actual_files
                    if item.get("fid") or item.get("id")
                }
                for key, (_, target_name, file_id) in pending_items.items():
                    if file_id not in expected_ids:
                        continue
                    actual_item = actual_by_id.get(file_id)
                    actual_name = str(
                        (actual_item or {}).get("name")
                        or (actual_item or {}).get("n")
                        or (actual_item or {}).get("file_name")
                        or ""
                    ).strip()
                    if actual_item and actual_name == target_name:
                        normalized_item = dict(actual_item)
                        pickcode = (
                                normalized_item.get("pick_code")
                                or normalized_item.get("pickcode")
                                or normalized_item.get("pc")
                        )
                        if pickcode:
                            normalized_item["pickcode"] = str(pickcode)
                        self._cache_target_file(
                            save_path, target_name, normalized_item
                        )
                        ready[key] = normalized_item
                        verified_ids.add(file_id)
                if verified_ids == expected_ids or attempt == 2:
                    break
                time.sleep(1)
            unresolved_count = len(pending_items) - len(verified_ids)
            message = (
                f"115 后处理文件重命名：提交 {len(rename_pairs)} 个，"
                f"接口成功 {len(renamed_ids)} 个，目录确认 {len(verified_ids)} 个"
            )
            if unresolved_count:
                logger.warning(f"{message}，待重试 {unresolved_count} 个")
            else:
                logger.debug(message)
            return ready
        except Exception as error:
            logger.warning(f"115 后处理文件批量重命名异常：{error}")
            return {}

    def list_files(self, path: str) -> List[dict]:
        """
        列出指定路径下的文件

        :param path: 目录路径
        :return: 文件列表
        """
        if not self.client:
            return []

        cid = self.get_pid_by_path(path, mkdir=False)
        if cid == -1:
            return []

        return self.list_files_by_cid(cid)

    def list_files_by_cid(self, cid: Any) -> List[dict]:
        """使用已解析的目录 CID 列出文件，避免重复进行路径定位。"""
        _, items = self.list_files_by_cid_checked(cid)
        return items

    def list_files_by_cid_checked(self, cid: Any) -> Tuple[bool, List[dict]]:
        """按 CID 列目录，并区分空目录和115接口失败。"""
        if not self.client or cid in (None, "", -1):
            return False, []
        try:
            return True, list(self._iter_directory(cid))
        except Exception as e:
            logger.error(f"列出文件失败: {e}")
            return False, []

    def list_files_recursive(self, path: str, max_depth: int = 5) -> List[dict]:
        """下载完成后按目录批次读取真实文件树，并保留源父目录。"""
        root_cid = self.get_pid_by_path(path, mkdir=False)
        if root_cid == -1:
            return []
        result = []
        queue = deque([(root_cid, str(path).rstrip("/"), 0)])
        while queue:
            parent_cid, parent_path, depth = queue.popleft()
            checked, items = self.list_files_by_cid_checked(parent_cid)
            if not checked:
                return []
            for raw_item in items:
                item = dict(raw_item)
                name = str(item.get("name") or item.get("n") or "").strip()
                is_dir = bool(
                    item.get("is_dir")
                    or (str(item.get("fid") or "0") == "0" and item.get("cid"))
                )
                item["is_dir"] = is_dir
                item["_parent_cid"] = str(parent_cid)
                item["_cloud_dir"] = parent_path
                if is_dir:
                    child_cid = item.get("cid") or item.get("file_id") or item.get("id")
                    if child_cid and depth < max(0, int(max_depth)):
                        queue.append((
                            child_cid,
                            f"{parent_path}/{name}" if parent_path else f"/{name}",
                            depth + 1,
                        ))
                else:
                    result.append(item)
        return result

    def move_and_rename_file(
            self, item: Dict[str, Any], save_path: str, target_name: str
    ) -> Optional[dict]:
        """把离线包内已匹配文件移动到媒体目录并应用平台文件名。"""
        file_id = item.get("fid") or item.get("file_id") or item.get("id")
        if not file_id:
            return None
        target_pid = self.get_pid_by_path(save_path, mkdir=True)
        if target_pid == -1:
            return None
        source_name = str(item.get("name") or item.get("n") or "").strip()
        try:
            if str(item.get("_parent_cid") or "") != str(target_pid):
                self._move_items([file_id], target_pid)
            moved = dict(item)
            moved["fid"] = file_id
            if source_name != target_name:
                if not self.rename_file_item(save_path, moved, source_name, target_name):
                    return None
            moved.update({"name": target_name, "n": target_name})
            self._cache_target_file(save_path, target_name, moved)
            if item.get("is_dir"):
                self.path_cache.clear()
            return moved
        except Exception as error:
            logger.error(f"移动离线文件失败：{source_name} -> {save_path}/{target_name}，{error}")
            return None

    def move_file_items_batch(
            self, items: Dict[str, Dict[str, Any]], save_path: str
    ) -> Dict[str, dict]:
        """将一组已完成平台重命名的文件批量移动到同一目录。"""
        if not self.client or not items:
            return {}
        target_pid = self.get_pid_by_path(save_path, mkdir=True)
        if target_pid == -1:
            return {}
        file_ids = {
            str(key): str(
                item.get("fid") or item.get("file_id") or item.get("id") or ""
            )
            for key, item in items.items()
        }
        file_ids = {key: file_id for key, file_id in file_ids.items() if file_id}
        if not file_ids:
            return {}
        try:
            self._move_items(file_ids.values(), target_pid)
            moved = {}
            for key, file_id in file_ids.items():
                item = dict(items[key])
                target_name = str(item.get("name") or item.get("n") or "").strip()
                if not target_name:
                    continue
                item.update({"fid": file_id, "_parent_cid": str(target_pid)})
                self._cache_target_file(save_path, target_name, item)
                moved[key] = item
            logger.debug(f"115 文件批量移动完成：{len(moved)} 个，目录 {save_path}")
            return moved
        except Exception as error:
            logger.warning(f"115 文件批量移动失败：{save_path}，{error}")
            return {}

    def delete_file(self, file_id: Any) -> bool:
        """将指定115文件移入回收站。"""
        if not self.client or file_id in (None, ""):
            return False
        try:
            self._delete_items([file_id])
            self._target_file_cache.clear()
            self.path_cache.clear()
            logger.debug(f"115文件已移入回收站：file_id={file_id}")
            return True
        except Exception as error:
            logger.error(f"删除115文件失败：file_id={file_id}，错误：{error}")
            return False

    def delete_files_batch(self, file_ids: List[Any]) -> Set[str]:
        """按115接口批次将多个文件或目录移入回收站。"""
        if not self.client or not file_ids:
            return set()
        try:
            deleted = self._delete_items(file_ids)
            self._target_file_cache.clear()
            self.path_cache.clear()
            logger.debug(f"115文件批量移入回收站：{len(deleted)} 个")
            return deleted
        except Exception as error:
            logger.error(f"批量删除115文件失败：数量={len(file_ids)}，错误：{error}")
            return set()

    def find_file_for_delete(self, dir_path: str, file_name: str) -> Optional[dict]:
        """严格查询待删除文件，避免把115接口异常误判为文件不存在。"""
        if not self.client:
            raise RuntimeError("115客户端未初始化")
        cid = self.get_pid_by_path(dir_path, mkdir=False)
        if cid == -1:
            raise RuntimeError(f"无法确认115目录是否存在：{dir_path}")
        checked, files = self.list_files_by_cid_checked(cid)
        if not checked:
            raise RuntimeError(f"无法读取115目录：{dir_path}")
        target_name = str(file_name or "").strip()
        return next(
            (
                dict(item)
                for item in files
                if str(
                item.get("name") or item.get("n")
                or item.get("file_name") or ""
            ).strip() == target_name
            ),
            None,
        )

    def find_file_by_exact_name(
            self,
            dir_path: str,
            file_name: str,
            attempts: int = 3,
            interval: float = 1.0,
    ) -> Optional[dict]:
        """按最终文件名定位115目标文件，供 STRM 获取真实 pickcode。"""
        target_name = str(file_name or "").strip()
        if not target_name:
            return None
        cache_key = self._target_file_cache_key(dir_path, target_name)
        cached = self._target_file_cache.get(cache_key)
        if isinstance(cached, dict):
            return dict(cached)
        for attempt in range(max(1, int(attempts))):
            for item in self.list_files(dir_path):
                current_name = str(
                    item.get("name") or item.get("n")
                    or item.get("file_name") or ""
                ).strip()
                if current_name != target_name:
                    continue
                pickcode = item.get("pick_code") or item.get("pickcode") or item.get("pc")
                if pickcode:
                    result = dict(item)
                    result["pickcode"] = str(pickcode)
                    self._cache_target_file(dir_path, target_name, result)
                    return result
            if attempt + 1 < max(1, int(attempts)):
                time.sleep(max(0, float(interval)))
        return None

    def get_cached_target_file(self, dir_path: str, file_name: str) -> Optional[dict]:
        """读取批量重命名写入的目标文件缓存，不访问115接口。"""
        cache_key = self._target_file_cache_key(dir_path, file_name)
        cached = self._target_file_cache.get(cache_key)
        if not isinstance(cached, dict):
            return None
        return dict(cached)

    @staticmethod
    def _target_file_cache_key(dir_path: str, file_name: str) -> str:
        normalized_path = "/" + str(dir_path or "").replace("\\", "/").strip("/")
        identity = "\0".join((
            normalized_path.rstrip("/") or "/",
            str(file_name or "").strip(),
        ))
        return hashlib.sha1(identity.encode("utf-8")).hexdigest()

    def _cache_target_file(self, dir_path: str, file_name: str, item: dict) -> None:
        pickcode = item.get("pick_code") or item.get("pickcode") or item.get("pc")
        if not pickcode:
            return
        cached = dict(item)
        cached["pickcode"] = str(pickcode)
        cache_key = self._target_file_cache_key(dir_path, file_name)
        self._target_file_cache[cache_key] = cached

    def list_directories(self, path: str) -> List[dict]:
        """
        列出指定路径下的所有目录（不包含文件）

        :param path: 目录路径
        :return: 目录列表，每个目录包含 name 和 path 字段
        """
        checked, cid = self.get_pid_by_path_checked(path)
        if not checked or cid == -1:
            raise RuntimeError(f"无法读取115目录：{path}")
        listed, files = self.list_files_by_cid_checked(cid)
        if not listed:
            raise RuntimeError(f"列出115目录失败：{path}")

        directories = []
        for item in files:
            if not item.get("is_dir"):
                continue
            dir_name = str(
                item.get("name") or item.get("n") or item.get("file_name") or ""
            ).strip()
            dir_id = item.get("id") or item.get("file_id") or item.get("cid")
            if not dir_name or dir_id in (None, ""):
                continue
            dir_path = (
                f"{path.rstrip('/')}/{dir_name}"
                if path != "/"
                else f"/{dir_name}"
            )
            directories.append({
                "name": dir_name,
                "path": dir_path,
                "cid": dir_id,
            })

        return directories

    def clear_path_cache(self):
        """清空路径缓存"""
        self.path_cache.clear()

    def find_file_in_dir(self, dir_path: str, filename: str) -> Optional[dict]:
        """在指定115目录下查找文件名匹配的文件

        :param dir_path: 115目录路径
        :param filename: 要查找的文件名（不含ext）
        :return: 匹配的文件信息dict，未找到返回None
        """
        files = self.list_files(dir_path)
        MEDIA_EXTS = {'.mkv', '.mp4', '.ts', '.avi', '.mov', '.wmv', '.flv', '.webm', '.iso', '.m2ts'}
        for f in files:
            fname = f.get("name", "")
            name_no_ext = fname.rsplit('.', 1)[0] if '.' in fname else fname
            ext = f".{fname.rsplit('.', 1)[-1].lower()}" if '.' in fname else ""
            if name_no_ext == filename and ext in MEDIA_EXTS:
                return f
        return None
