"""跨网盘传输编排：优先目标端秒传，失败后回退本地上传。"""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from threading import Event, Lock, Semaphore, Thread
from typing import Callable, Optional

import requests

from .cloud import (
    CloudDriveCapability,
    CloudDriveProvider,
    CloudFile,
    RapidUploadResult,
)


class _RangeDownloadUnsupported(RuntimeError):
    pass


class _SpeedSampler:
    """按固定时间窗口计算平滑速度，避免并发分块回调产生瞬时尖峰。"""

    def __init__(self, window_seconds: float = 1.0):
        self._window_seconds = max(0.5, float(window_seconds or 1.0))
        self._sample_bytes = 0
        self._sample_time = time.monotonic()
        self._speed = 0.0

    def update(self, done: int) -> float:
        current = max(0, int(done or 0))
        now = time.monotonic()
        if current < self._sample_bytes:
            self._sample_bytes = current
            self._sample_time = now
            self._speed = 0.0
            return self._speed
        elapsed = now - self._sample_time
        if elapsed < self._window_seconds:
            return self._speed
        instant = max(0.0, (current - self._sample_bytes) / elapsed)
        self._speed = (
            instant if self._speed <= 0
            else self._speed * 0.65 + instant * 0.35
        )
        self._sample_bytes = current
        self._sample_time = now
        return self._speed


def file_checksum(path: str, algorithm: str) -> str:
    digest = hashlib.new(algorithm.lower())
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class HttpFileDownloadService:
    """把 Provider 的临时下载地址解析器适配为流式文件下载能力。"""

    def __init__(
            self,
            resolver: Callable[[CloudFile], tuple[str, dict]],
            timeout: int = 300,
            concurrency: int = 5,
            part_size: int = 10 * 1024 * 1024,
    ):
        self._resolver = resolver
        self._timeout = timeout
        self._concurrency = max(1, min(int(concurrency or 5), 10))
        self._part_size = max(1024 * 1024, int(part_size or 0))

    @property
    def _request_timeout(self) -> tuple[int, int]:
        # CDN 大文件分段可能长时间无数据，不能把调用方配置的 300 秒硬截断为 30 秒。
        return 15, min(max(30, self._timeout), 120)

    @staticmethod
    def _parts_path(target: Path) -> Path:
        return target.with_name(f"{target.name}.parts.json")

    @staticmethod
    def _write_parts(path: Path, payload: dict) -> None:
        temporary = path.with_name(f"{path.name}.tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        temporary.replace(path)

    def _load_completed_ranges(
            self, target: Path, total: int, ranges: list[tuple[int, int]],
    ) -> set[int]:
        parts_path = self._parts_path(target)
        if not target.is_file() or not parts_path.is_file():
            return set()
        try:
            payload = json.loads(parts_path.read_text(encoding="utf-8"))
            if (
                    int(payload.get("version") or 0) != 1
                    or int(payload.get("total") or 0) != total
                    or int(payload.get("part_size") or 0) != self._part_size
                    or target.stat().st_size != total
            ):
                return set()
            raw_completed = payload.get("completed") or []
            if not isinstance(raw_completed, list):
                return set()
            valid_starts = {start for start, _ in ranges}
            completed = {int(value) for value in raw_completed}
            if (
                    len(completed) != len(raw_completed)
                    or not completed.issubset(valid_starts)
            ):
                return set()
            return completed
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return set()

    def _download_serial(
            self, url: str, headers: dict, file_item: CloudFile, target: Path,
            progress_callback=None, stop_requested=None,
    ) -> str:
        done = 0
        with requests.get(
                url,
                headers=headers,
                stream=True,
                timeout=self._request_timeout,
        ) as response:
            response.raise_for_status()
            total = int(
                response.headers.get("Content-Length") or file_item.size or 0
            )
            with target.open("wb") as handle:
                for chunk in response.iter_content(256 * 1024):
                    if stop_requested and stop_requested():
                        raise InterruptedError
                    if not chunk:
                        continue
                    handle.write(chunk)
                    done += len(chunk)
                    if progress_callback:
                        progress_callback(done, total)
        if done <= 0:
            raise RuntimeError("源网盘下载为空文件")
        return str(target)

    def _download_parallel(
            self, url: str, headers: dict, total: int, target: Path,
            progress_callback=None, stop_requested=None,
    ) -> str:
        ranges = [
            (start, min(total - 1, start + self._part_size - 1))
            for start in range(0, total, self._part_size)
        ]
        worker_count = min(self._concurrency, len(ranges))
        if worker_count <= 1:
            raise _RangeDownloadUnsupported

        progress_lock = Lock()
        abort_event = Event()
        parts_path = self._parts_path(target)
        completed = self._load_completed_ranges(target, total, ranges)
        if not completed:
            with target.open("wb") as handle:
                handle.truncate(total)
            self._write_parts(parts_path, {
                "version": 1,
                "total": total,
                "part_size": self._part_size,
                "completed": [],
            })
        downloaded = sum(
            end - start + 1 for start, end in ranges if start in completed
        )
        pending_ranges = [value for value in ranges if value[0] not in completed]
        if progress_callback and downloaded:
            progress_callback(downloaded, total)
        if not pending_ranges:
            parts_path.unlink(missing_ok=True)
            return str(target)

        def download_part(byte_range: tuple[int, int]) -> None:
            nonlocal downloaded
            start, end = byte_range
            request_headers = dict(headers)
            request_headers["Range"] = f"bytes={start}-{end}"
            for attempt in range(3):
                received = 0
                try:
                    with requests.get(
                            url,
                            headers=request_headers,
                            stream=True,
                            timeout=self._request_timeout,
                    ) as response:
                        if response.status_code != 206:
                            raise _RangeDownloadUnsupported(
                                f"HTTP {response.status_code}"
                            )
                        content_range = str(response.headers.get("Content-Range") or "")
                        if not content_range.lower().startswith(
                                f"bytes {start}-{end}/".lower()
                        ):
                            raise _RangeDownloadUnsupported(
                                f"无效 Content-Range: {content_range or '空'}"
                            )
                        with target.open("r+b") as handle:
                            handle.seek(start)
                            for chunk in response.iter_content(256 * 1024):
                                if (
                                        abort_event.is_set()
                                        or (stop_requested and stop_requested())
                                ):
                                    raise InterruptedError
                                if not chunk:
                                    continue
                                handle.write(chunk)
                                received += len(chunk)
                                with progress_lock:
                                    downloaded += len(chunk)
                                    if progress_callback:
                                        progress_callback(downloaded, total)
                        if received != end - start + 1:
                            raise IOError(
                                f"分段下载不完整：{start}-{end}，实际 {received} 字节"
                            )
                    break
                except (_RangeDownloadUnsupported, InterruptedError):
                    raise
                except (requests.RequestException, OSError):
                    with progress_lock:
                        downloaded = max(0, downloaded - received)
                        if progress_callback:
                            progress_callback(downloaded, total)
                    if attempt >= 2:
                        raise
                    time.sleep(attempt + 1)
            with progress_lock:
                completed.add(start)
                self._write_parts(parts_path, {
                    "version": 1,
                    "total": total,
                    "part_size": self._part_size,
                    "completed": sorted(completed),
                })

        executor = ThreadPoolExecutor(
            max_workers=worker_count,
            thread_name_prefix="pansearch-range-download",
        )
        futures = [executor.submit(download_part, value) for value in pending_ranges]
        try:
            for future in as_completed(futures):
                future.result()
        except Exception:
            abort_event.set()
            for future in futures:
                future.cancel()
            raise
        finally:
            executor.shutdown(wait=True, cancel_futures=True)
        parts_path.unlink(missing_ok=True)
        return str(target)

    def download_file(self, file_item: CloudFile, local_path: str,
                      progress_callback=None, stop_requested=None,
                      preserve_partial: bool = False) -> str:
        url, headers = self._resolver(file_item)
        if not url:
            raise RuntimeError("源网盘未返回下载地址")
        target = Path(local_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        request_headers = dict(headers or {})
        request_headers.setdefault("Accept-Encoding", "identity")
        total = int(file_item.size or 0)
        reported = 0

        def report(done: int, current_total: int) -> None:
            nonlocal reported
            reported = max(reported, int(done or 0))
            if progress_callback:
                progress_callback(reported, current_total)

        try:
            if self._concurrency > 1 and total > self._part_size:
                try:
                    return self._download_parallel(
                        url,
                        request_headers,
                        total,
                        target,
                        report,
                        stop_requested,
                    )
                except _RangeDownloadUnsupported:
                    target.unlink(missing_ok=True)
                    self._parts_path(target).unlink(missing_ok=True)
            return self._download_serial(
                url,
                request_headers,
                file_item,
                target,
                report,
                stop_requested,
            )
        except Exception:
            if not preserve_partial or not self._parts_path(target).is_file():
                target.unlink(missing_ok=True)
                self._parts_path(target).unlink(missing_ok=True)
            raise


class LocalRapidUploadAdapter:
    """把现有 Provider 的上传服务暴露为统一能力。

    Provider 必须单独实现 try_rapid_upload；秒传未命中后由编排器调用
    upload_file，避免通过进度回调猜测是否发生了普通上传。
    """

    def __init__(self, upload: object, files: object, algorithms: frozenset[str]):
        self._upload = upload
        self._files = files
        self._algorithms = algorithms

    @property
    def algorithms(self) -> frozenset[str]:
        return self._algorithms

    @property
    def requires_local_file(self) -> bool:
        return bool(getattr(self._upload, "rapid_requires_local_file", False))

    def upload_by_hash(
            self, local_path: str, save_path: str, target_name: str,
            algorithm: str, checksum: str, size: int = 0,
            progress_callback: Optional[Callable[[int, int], None]] = None,
    ) -> RapidUploadResult:
        algorithm = str(algorithm or "").lower()
        if algorithm not in self._algorithms:
            return RapidUploadResult(False, message="目标网盘不支持该秒传算法")
        actual = str(checksum or "").lower()
        if local_path:
            calculated = file_checksum(local_path, algorithm)
            if actual and calculated.lower() != actual:
                raise ValueError(f"{algorithm} 校验和不匹配")
            actual = calculated.lower()
        elif self.requires_local_file:
            return RapidUploadResult(False, message="秒传探测需要本地文件")
        if not actual:
            return RapidUploadResult(False, message="缺少秒传校验和")
        rapid_upload = getattr(self._upload, "try_rapid_upload", None)
        if not callable(rapid_upload):
            return RapidUploadResult(False, message="目标网盘未实现独立秒传探测")
        reused = bool(rapid_upload(
            local_path, save_path, target_name, algorithm, actual, size
        ))
        if not reused:
            return RapidUploadResult(False, message="未命中秒传")
        item = self._files.find_file(save_path, target_name)
        if not item:
            raise RuntimeError("秒传成功后未找到目标文件")
        return RapidUploadResult(True, file=item, message="秒传命中")


@dataclass(frozen=True)
class CrossDriveTransferResult:
    file: CloudFile
    method: str


class CrossDriveTransfer:
    """执行单文件传输；源文件需先由调用层下载到本地临时路径。"""

    @staticmethod
    def select_algorithm(source: CloudFile, target: CloudDriveProvider) -> tuple[str, str]:
        """按目标能力选择源文件已有校验和，MD5 与 SHA1 绝不互换。"""
        if not target.supports(CloudDriveCapability.RAPID_UPLOAD):
            return "", ""
        algorithms = target.require(CloudDriveCapability.RAPID_UPLOAD).algorithms
        if "md5" in algorithms and source.md5:
            return "md5", source.md5
        if "sha1" in algorithms and source.sha1:
            return "sha1", source.sha1
        return "", ""

    def transfer(
            self, source_path: str, target: CloudDriveProvider, save_path: str,
            target_name: str = "", algorithm: str = "", checksum: str = "",
            fallback: bool = True,
            progress_callback: Optional[Callable[[int, int], None]] = None,
    ) -> CrossDriveTransferResult | None:
        target_name = target_name or Path(source_path).name
        if target.supports(CloudDriveCapability.RAPID_UPLOAD) and algorithm:
            rapid = target.require(CloudDriveCapability.RAPID_UPLOAD)
            if algorithm.lower() in rapid.algorithms:
                rapid_result = rapid.upload_by_hash(
                    source_path, save_path, target_name, algorithm, checksum,
                    Path(source_path).stat().st_size, progress_callback,
                )
                if rapid_result.reused and rapid_result.file:
                    return CrossDriveTransferResult(rapid_result.file, "rapid")
        if not fallback:
            return None
        upload = target.require(CloudDriveCapability.LOCAL_UPLOAD)
        if not upload.upload_file(source_path, save_path, target_name, progress_callback=progress_callback):
            return None
        item = target.require(CloudDriveCapability.FILE_QUERY).find_file(
            save_path, target_name
        )
        return CrossDriveTransferResult(item, "fallback") if item else None


class CrossTransferTaskManager:
    """本地中继任务管理器。"""

    ACTIVE = frozenset({"pending", "running", "stopping"})
    _MIN_FREE_BYTES = 512 * 1024 * 1024

    def __init__(self, provider_resolver: Callable[[str], CloudDriveProvider],
                 download_path: str = "", download_threads: int = 5,
                 max_concurrent: int = 2,
                 on_change: Optional[Callable[[], None]] = None):
        self._provider_resolver = provider_resolver
        self._download_path = str(download_path or "").strip()
        self._download_threads = max(1, min(int(download_threads or 5), 10))
        self._transfer_slots = Semaphore(max(1, min(int(max_concurrent or 2), 10)))
        self._tasks: dict[str, dict] = {}
        self._lock = Lock()
        self._on_change = on_change

    def _notify_change(self) -> None:
        if self._on_change:
            self._on_change()

    def _cache_root(self) -> Path:
        root = Path(self._download_path) if self._download_path else Path(tempfile.gettempdir())
        root.mkdir(parents=True, exist_ok=True)
        return root

    @staticmethod
    def _cache_key(source_key: str, source: CloudFile) -> str:
        fingerprint = "|".join((
            str(source_key or ""),
            str(source.sha1 or "").lower(),
            str(source.md5 or "").lower(),
            str(source.size or 0),
            str(source.name or ""),
        ))
        return hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()[:32]

    def _cache_path(self, cache_key: str) -> Path:
        return self._cache_root() / f"cloud-transfer-{cache_key}.bin"

    @staticmethod
    def _cache_meta_path(path: Path) -> Path:
        return path.with_name(f"{path.name}.meta.json")

    @staticmethod
    def _cache_parts_path(path: Path) -> Path:
        return path.with_name(f"{path.name}.parts.json")

    def _delete_cache_path(self, path: Path) -> int:
        deleted = 0
        for candidate in (
                path,
                self._cache_meta_path(path),
                self._cache_meta_path(path).with_name(
                    f"{self._cache_meta_path(path).name}.tmp"
                ),
                self._cache_parts_path(path),
                self._cache_parts_path(path).with_name(
                    f"{self._cache_parts_path(path).name}.tmp"
                ),
        ):
            if candidate.is_file():
                candidate.unlink(missing_ok=True)
                deleted += 1
        return deleted

    def delete_cache(self, cache_key: str) -> int:
        normalized = str(cache_key or "").strip().lower()
        if len(normalized) != 32 or any(value not in "0123456789abcdef" for value in normalized):
            return 0
        return self._delete_cache_path(self._cache_path(normalized))

    def _validate_complete_cache(
            self, path: Path, cache_key: str, source: CloudFile,
            verify_checksum: bool = True,
    ) -> bool:
        meta_path = self._cache_meta_path(path)
        if not path.is_file() or not meta_path.is_file():
            return False
        try:
            metadata = json.loads(meta_path.read_text(encoding="utf-8"))
            expected_size = int(source.size or 0)
            if (
                    metadata.get("cache_key") != cache_key
                    or not metadata.get("complete")
                    or int(metadata.get("size") or 0) != expected_size
                    or path.stat().st_size != expected_size
            ):
                return False
            expected_sha1 = str(source.sha1 or "").lower()
            expected_md5 = str(source.md5 or "").lower()
            if (
                    str(metadata.get("sha1") or "").lower() != expected_sha1
                    or str(metadata.get("md5") or "").lower() != expected_md5
            ):
                return False
            if not verify_checksum:
                return expected_size > 0
            if expected_sha1:
                return file_checksum(str(path), "sha1").lower() == expected_sha1
            if expected_md5:
                return file_checksum(str(path), "md5").lower() == expected_md5
            return expected_size > 0
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return False

    def _partial_cache_size(self, path: Path, source: CloudFile) -> int:
        parts_path = self._cache_parts_path(path)
        expected_size = int(source.size or 0)
        if expected_size <= 0 or not path.is_file() or not parts_path.is_file():
            return 0
        try:
            payload = json.loads(parts_path.read_text(encoding="utf-8"))
            part_size = int(payload.get("part_size") or 0)
            if (
                    int(payload.get("version") or 0) != 1
                    or int(payload.get("total") or 0) != expected_size
                    or part_size <= 0
                    or path.stat().st_size != expected_size
            ):
                return 0
            valid_starts = set(range(0, expected_size, part_size))
            completed = {
                int(value) for value in (payload.get("completed") or [])
                if int(value) in valid_starts
            }
            if len(completed) != len(payload.get("completed") or []):
                return 0
            return sum(
                min(part_size, expected_size - start) for start in completed
            )
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return 0

    def _mark_complete_cache(
            self, path: Path, cache_key: str, source: CloudFile,
    ) -> None:
        expected_size = int(source.size or 0)
        if expected_size <= 0 or path.stat().st_size != expected_size:
            raise IOError(
                f"本地缓存文件大小不完整：{path.stat().st_size}/{expected_size}"
            )
        sha1 = str(source.sha1 or "").lower()
        md5 = str(source.md5 or "").lower()
        if sha1 and file_checksum(str(path), "sha1").lower() != sha1:
            raise IOError("本地缓存 SHA1 校验失败")
        if md5 and file_checksum(str(path), "md5").lower() != md5:
            raise IOError("本地缓存 MD5 校验失败")
        meta_path = self._cache_meta_path(path)
        temporary = meta_path.with_name(f"{meta_path.name}.tmp")
        temporary.write_text(json.dumps({
            "version": 1,
            "cache_key": cache_key,
            "complete": True,
            "size": expected_size,
            "sha1": sha1,
            "md5": md5,
        }, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        temporary.replace(meta_path)

    def cache_info(
            self, source_key: str, source: CloudFile,
            verify_checksum: bool = True,
    ) -> dict:
        cache_key = self._cache_key(source_key, source)
        path = self._cache_path(cache_key)
        complete = self._validate_complete_cache(
            path, cache_key, source, verify_checksum=verify_checksum
        )
        partial_size = 0 if complete else self._partial_cache_size(path, source)
        return {
            "cache_key": cache_key,
            "cache_status": (
                "complete" if complete else "partial" if partial_size else "missing"
            ),
            "cache_size": int(source.size or 0) if complete else partial_size,
        }

    def _temporary_path(self, task: dict) -> str:
        source = task.get("source_file")
        cache_key = self._cache_key(task.get("source_provider"), source)
        return str(self._cache_path(cache_key))

    def create(self, source_path: str = "", target_key: str = "", save_path: str = "/",
               target_name: str = "", algorithm: str = "", checksum: str = "",
               fallback: bool = True, source_key: str = "", source_file: CloudFile | None = None,
               parent_task_id: str = "") -> dict:
        source = Path(source_path) if source_path else None
        if source is not None and not source.is_file():
            raise ValueError("源文件不存在或不是文件")
        if source is None and (not source_key or not source_file):
            raise ValueError("缺少源文件路径或源网盘文件")
        task_id = uuid.uuid4().hex
        now = time.time()
        total = int(source.stat().st_size) if source else int(source_file.size or 0)
        cache_root = self._cache_root()
        required = max(self._MIN_FREE_BYTES, int(total * 1.10))
        try:
            free_bytes = int(shutil.disk_usage(cache_root).free)
        except OSError as error:
            raise RuntimeError(f"无法检查中继磁盘空间：{error}") from error
        if free_bytes < required:
            raise RuntimeError(
                f"中继磁盘可用空间不足：剩余 {free_bytes} 字节，"
                f"至少需要 {required} 字节"
            )
        task = {
            "id": f"cross:{task_id}", "task_kind": "cross_transfer",
            "parent_task_id": str(parent_task_id or ""),
            "title": target_name or (source.name if source else source_file.name),
            "source_path": str(source) if source else "",
            "source_provider": source_key,
            "source_file": source_file,
            "target_provider": target_key, "target_path": save_path,
            "status": "pending", "phase": "pending", "progress": 0,
            "transferred": 0, "total": total,
            "stage_transferred": 0, "stage_total": 0,
            "downloaded_bytes": 0, "uploaded_bytes": 0,
            "download_speed_bytes_per_second": 0.0,
            "upload_speed_bytes_per_second": 0.0,
            "speed_bytes_per_second": 0.0, "message": "等待跨盘传输",
            "error": "", "method": "rapid" if algorithm else "fallback",
            "algorithm": algorithm.lower(), "fallback": bool(fallback),
            "queued_at": now, "started_at": None, "finished_at": None,
            "updated_at": now, "stop_event": Event(),
        }
        with self._lock:
            terminal = [key for key, value in self._tasks.items()
                        if value["status"] not in self.ACTIVE]
            for stale_id in terminal[:-49]:
                self._tasks.pop(stale_id, None)
            self._tasks[task["id"]] = task
        self._notify_change()
        Thread(target=self._run, args=(task["id"], checksum), daemon=True).start()
        return self._public(task)

    def create_from_cloud_file(self, source_key: str, source_file: CloudFile,
                               target_key: str, save_path: str, target_name: str = "",
                               algorithm: str = "", fallback: bool = True,
                               parent_task_id: str = "") -> dict:
        selected = algorithm or ""
        if not selected:
            try:
                selected, _ = CrossDriveTransfer.select_algorithm(
                    source_file, self._provider_resolver(target_key)
                )
            except Exception:
                selected = ""
        checksum = source_file.md5 if selected == "md5" else source_file.sha1
        return self.create(
            target_key=target_key, save_path=save_path, target_name=target_name,
            algorithm=selected, checksum=checksum, fallback=fallback,
            source_key=source_key, source_file=source_file,
            parent_task_id=parent_task_id,
        )

    def list(self, active_only: bool = False) -> list[dict]:
        with self._lock:
            tasks = [self._public(task) for task in self._tasks.values()
                     if not active_only or task["status"] in self.ACTIVE]
        return sorted(tasks, key=lambda item: item["queued_at"], reverse=True)

    def wait(self, task_id: str, poll_seconds: float = 0.2,
             cancel_check: Optional[Callable[[], bool]] = None) -> bool:
        while True:
            with self._lock:
                task = self._tasks.get(task_id)
                if not task:
                    return False
                status = task["status"]
            if status not in self.ACTIVE:
                return status == "success"
            if cancel_check and cancel_check():
                self.cancel(task_id)
            time.sleep(max(0.05, poll_seconds))

    def cancel(self, task_id: str) -> bool:
        with self._lock:
            task = self._tasks.get(task_id)
            if not task or task["status"] not in self.ACTIVE:
                return False
            task["status"], task["phase"] = "stopping", "stopping"
            task["message"] = "等待当前网络操作安全停止"
            task["stop_event"].set()
        self._notify_change()
        return True

    def cancel_parent(self, parent_task_id: str) -> int:
        """取消属于同一订阅任务的全部跨盘子任务。"""
        parent_task_id = str(parent_task_id or "")
        if not parent_task_id:
            return 0
        canceled = 0
        with self._lock:
            for task in self._tasks.values():
                if (
                        task.get("parent_task_id") == parent_task_id
                        and task.get("status") in self.ACTIVE
                ):
                    task["status"], task["phase"] = "stopping", "stopping"
                    task["message"] = "等待当前网络操作安全停止"
                    task["stop_event"].set()
                    canceled += 1
        if canceled:
            self._notify_change()
        return canceled

    def _update(self, task_id: str, **values) -> None:
        changed = False
        with self._lock:
            task = self._tasks.get(task_id)
            if task and any(task.get(key) != value for key, value in values.items()):
                task.update(values)
                task["updated_at"] = time.time()
                changed = True
        if changed:
            self._notify_change()

    @staticmethod
    def _public(task: dict) -> dict:
        return {key: value for key, value in task.items()
                if key not in {"stop_event", "source_path", "source_file"}}

    @staticmethod
    def _ranges_cover(
            completed: list[int], part_size: int, total: int,
            start: int, end: int,
    ) -> bool:
        cursor = start
        for part_start in sorted(completed):
            part_end = min(total, part_start + part_size) - 1
            if part_end < cursor:
                continue
            if part_start > cursor:
                return False
            cursor = part_end + 1
            if cursor > end:
                return True
        return cursor > end

    def _progressive_transfer(
            self, task_id: str, task: dict, downloader: object,
            target: CloudDriveProvider, temporary_path: str,
            cache_key: str, target_name_text: str,
            algorithm: str, checksum: str,
    ) -> CrossDriveTransferResult | None:
        """下载 Range 落盘后立即交给支持该能力的目标盘上传。"""
        stop_event = task["stop_event"]
        source_file = task["source_file"]
        expected_size = int(source_file.size or task["total"] or 0)
        cache_path = Path(temporary_path)
        parts_path = self._cache_parts_path(cache_path)
        download_done = Event()
        progress_changed = Event()
        download_error: list[BaseException] = []
        download_speed = _SpeedSampler()
        upload_speed = _SpeedSampler()

        def update_progress(**values) -> None:
            with self._lock:
                current = self._tasks.get(task_id) or {}
                downloaded = int(values.get(
                    "downloaded_bytes", current.get("downloaded_bytes") or 0
                ))
                uploaded = int(values.get(
                    "uploaded_bytes", current.get("uploaded_bytes") or 0
                ))
                download_rate = float(values.get(
                    "download_speed_bytes_per_second",
                    current.get("download_speed_bytes_per_second") or 0,
                ))
                upload_rate = float(values.get(
                    "upload_speed_bytes_per_second",
                    current.get("upload_speed_bytes_per_second") or 0,
                ))
            progress = (
                min(99, int((downloaded + uploaded) * 50 / expected_size))
                if expected_size > 0 else 0
            )
            values.update(
                phase="streaming", progress=progress,
                transferred=int(expected_size * progress / 100),
                stage_transferred=uploaded, stage_total=expected_size,
                speed_bytes_per_second=(
                    upload_rate if uploaded > 0 else download_rate
                ),
                message=f"正在边下载边上传到{target_name_text}",
            )
            self._update(task_id, **values)

        def download_progress(done: int, total: int) -> None:
            if stop_event.is_set():
                raise InterruptedError
            speed = download_speed.update(done)
            update_progress(
                downloaded_bytes=done,
                download_speed_bytes_per_second=speed,
            )
            progress_changed.set()

        def run_download() -> None:
            try:
                path = downloader.download_file(
                    source_file, temporary_path, download_progress,
                    stop_event.is_set, preserve_partial=True,
                    download_threads=self._download_threads,
                )
                self._mark_complete_cache(Path(path), cache_key, source_file)
            except BaseException as error:
                download_error.append(error)
            finally:
                download_done.set()
                progress_changed.set()

        def wait_for_range(start: int, end: int) -> None:
            while True:
                if stop_event.is_set():
                    raise InterruptedError
                if download_error:
                    raise download_error[0]
                if parts_path.is_file():
                    try:
                        payload = json.loads(parts_path.read_text(encoding="utf-8"))
                        if self._ranges_cover(
                                [int(value) for value in payload.get("completed") or []],
                                int(payload.get("part_size") or 0),
                                int(payload.get("total") or expected_size),
                                start, end,
                        ):
                            return
                    except (OSError, ValueError, TypeError, json.JSONDecodeError):
                        pass
                if download_done.is_set():
                    if download_error:
                        raise download_error[0]
                    if cache_path.is_file() and cache_path.stat().st_size == expected_size:
                        return
                    raise IOError("本地缓存下载完成但文件大小不完整")
                progress_changed.wait(0.1)
                progress_changed.clear()

        def upload_progress(done: int, total: int) -> None:
            if stop_event.is_set():
                raise InterruptedError
            speed = upload_speed.update(done)
            update_progress(
                uploaded_bytes=done,
                upload_speed_bytes_per_second=speed,
            )

        self._update(
            task_id, phase="streaming",
            message=f"准备边下载边上传到{target_name_text}",
        )
        download_thread = Thread(
            target=run_download,
            name="pansearch-progressive-download",
            daemon=True,
        )
        download_thread.start()
        uploaded = False
        upload_error: Exception | None = None
        try:
            uploaded = bool(target.require(
                CloudDriveCapability.LOCAL_UPLOAD
            ).upload_progressive(
                temporary_path, task["target_path"], task["title"],
                expected_size, algorithm, checksum, wait_for_range,
                upload_progress, stop_event.is_set,
            ))
        except InterruptedError:
            raise
        except Exception as error:
            upload_error = error
        finally:
            download_thread.join()
        if stop_event.is_set():
            raise InterruptedError
        if download_error:
            raise download_error[0]
        if upload_error:
            # 下载缓存已完整保留，后续由普通本地上传继续，不丢失本轮成果。
            self._update(task_id, message="流水线上传未完成，正在使用完整缓存重试")
            return None
        if not uploaded:
            return None
        item = target.require(CloudDriveCapability.FILE_QUERY).find_file(
            task["target_path"], task["title"]
        )
        return CrossDriveTransferResult(item, "progressive") if item else None

    def _run(self, task_id: str, checksum: str) -> None:
        self._transfer_slots.acquire()
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                self._transfer_slots.release()
                return
            source_path = task["source_path"]
            stop_event = task["stop_event"]
            target_key, target_path = task["target_provider"], task["target_path"]
            target_name, algorithm = task["title"], task["algorithm"]
            fallback = task["fallback"]
        started = time.time()
        self._update(
            task_id, status="running",
            phase="hashing" if source_path else "downloading",
            started_at=started,
            message="正在校验源文件" if source_path else "准备下载源文件",
            progress=2 if source_path else 0,
        )
        try:
            if stop_event.is_set():
                raise InterruptedError
            target = self._provider_resolver(target_key)
            target_name_text = str(
                getattr(target, "name", "") or target_key or "目标网盘"
            )
            remote_rapid_attempted = False
            if (
                    not source_path and algorithm and checksum
                    and target.supports(CloudDriveCapability.RAPID_UPLOAD)
            ):
                rapid_service = target.require(CloudDriveCapability.RAPID_UPLOAD)
                if not bool(getattr(rapid_service, "requires_local_file", True)):
                    remote_rapid_attempted = True
                    self._update(
                        task_id, phase="rapid_upload",
                        message=f"正在向{target_name_text}尝试秒传",
                        progress=5,
                    )
                    rapid_result = rapid_service.upload_by_hash(
                        "", target_path, target_name, algorithm, checksum,
                        int(task["total"] or 0),
                    )
                    if rapid_result.reused and rapid_result.file:
                        result_file = rapid_result.file
                        self._update(
                            task_id, status="success", phase="done", progress=100,
                            transferred=task["total"], speed_bytes_per_second=0.0,
                            stage_transferred=0, stage_total=0,
                            message=f"已秒传到{target_name_text}", method="rapid",
                            result_file_id=str(result_file.id or ""),
                            result_file_name=str(result_file.name or target_name),
                            result_file_size=int(result_file.size or task["total"] or 0),
                            result_sha1=str(result_file.sha1 or ""),
                            result_md5=str(result_file.md5 or ""),
                            finished_at=time.time(),
                        )
                        return
            source_provider = None
            source_name = ""
            if not source_path:
                source_provider = self._provider_resolver(task["source_provider"])
                source_name = str(
                    getattr(source_provider, "name", "")
                    or task["source_provider"] or "源网盘"
                )
            if (
                    not source_path
                    and fallback
                    and algorithm == "sha1"
                    and bool(checksum)
                    and int(task["total"] or 0) > 0
                    and source_provider.supports(CloudDriveCapability.FILE_DOWNLOAD)
                    and target.supports(CloudDriveCapability.LOCAL_UPLOAD)
                    and callable(getattr(
                target.require(CloudDriveCapability.LOCAL_UPLOAD),
                "upload_from_link", None,
            ))
            ):
                try:
                    download_url, download_headers = source_provider.require(
                        CloudDriveCapability.FILE_DOWNLOAD
                    ).resolve_download_link(task["source_file"])
                    remote_speed = _SpeedSampler()

                    def remote_progress(done: int, total: int) -> None:
                        if stop_event.is_set():
                            raise InterruptedError
                        percent = min(99, int(done * 100 / total)) if total else 0
                        speed = remote_speed.update(done)
                        self._update(
                            task_id, phase="streaming", progress=percent,
                            transferred=done, total=total,
                            downloaded_bytes=done, uploaded_bytes=done,
                            download_speed_bytes_per_second=speed,
                            upload_speed_bytes_per_second=speed,
                            speed_bytes_per_second=speed,
                            stage_transferred=done, stage_total=total,
                            message=f"正在从{source_name} Range 直传到{target_name_text}",
                        )

                    self._update(
                        task_id, phase="rapid_upload", progress=1,
                        message=(
                            f"正在从{source_name}向{target_name_text}尝试 SHA1 秒传"
                        ),
                    )
                    remote_uploaded = target.require(
                        CloudDriveCapability.LOCAL_UPLOAD
                    ).upload_from_link(
                        download_url, download_headers, target_path,
                        target_name, int(task["total"]), algorithm, checksum,
                        remote_progress, stop_event.is_set,
                    )
                    if remote_uploaded:
                        remote_rapid = remote_uploaded == "rapid"
                        result_file = target.require(
                            CloudDriveCapability.FILE_QUERY
                        ).find_file(target_path, target_name)
                        if not result_file:
                            raise RuntimeError("跨盘传输完成后未找到目标文件")
                        self._update(
                            task_id, status="success", phase="done", progress=100,
                            transferred=task["total"],
                            downloaded_bytes=task["total"],
                            uploaded_bytes=task["total"],
                            download_speed_bytes_per_second=0.0,
                            upload_speed_bytes_per_second=0.0,
                            speed_bytes_per_second=0.0,
                            stage_transferred=0, stage_total=0,
                            message=(
                                f"已从{source_name} SHA1 秒传到{target_name_text}"
                                if remote_rapid else
                                f"已从{source_name} Range 直传到{target_name_text}"
                            ),
                            method="rapid" if remote_rapid else "remote",
                            result_file_id=str(result_file.id or ""),
                            result_file_name=str(result_file.name or target_name),
                            result_file_size=int(
                                result_file.size or task["total"] or 0
                            ),
                            result_sha1=str(result_file.sha1 or ""),
                            result_md5=str(result_file.md5 or ""),
                            finished_at=time.time(),
                        )
                        return
                except InterruptedError:
                    raise
                except Exception:
                    self._update(
                        task_id, phase="downloading", progress=0,
                        message="Range 直传不可用，正在回退本地缓存",
                    )
            temporary_path = ""
            cache_complete = False
            cache_reused = False
            progressive_result = None
            if not source_path:
                temporary_path = self._temporary_path(task)
                cache_path = Path(temporary_path)
                source_file = task["source_file"]
                cache_key = self._cache_key(task["source_provider"], source_file)
                expected_size = int(source_file.size or task["total"] or 0)
                cache_complete = self._validate_complete_cache(
                    cache_path, cache_key, source_file
                )
                if (
                        cache_path.exists() and not cache_complete
                        and not self._cache_parts_path(cache_path).is_file()
                ):
                    self._delete_cache_path(cache_path)
                if cache_complete:
                    cache_reused = True
                    source_path = temporary_path
                    self._update(task_id, phase="cache_ready", progress=50,
                                 transferred=int(expected_size * 0.5),
                                 stage_transferred=0, stage_total=0,
                                 speed_bytes_per_second=0.0,
                                 message="已找到完整本地缓存，继续目标盘上传")
                else:
                    download_message = f"正在从{source_name}下载到本地缓存"
                    downloader = source_provider.require(CloudDriveCapability.FILE_DOWNLOAD)
                    can_stream = (
                            fallback
                            and algorithm == "md5"
                            and bool(checksum)
                            and expected_size > 0
                            and target.supports(CloudDriveCapability.LOCAL_UPLOAD)
                            and callable(getattr(
                        target.require(CloudDriveCapability.LOCAL_UPLOAD),
                        "upload_progressive", None,
                    ))
                    )
                    if can_stream:
                        progressive_result = self._progressive_transfer(
                            task_id, task, downloader, target, temporary_path,
                            cache_key, target_name_text,
                            algorithm, checksum,
                        )
                        source_path = temporary_path
                    else:
                        self._update(
                            task_id, phase="downloading",
                            message=download_message, progress=0,
                        )
                        download_speed = _SpeedSampler()

                        def download_progress(done: int, total: int) -> None:
                            if stop_event.is_set():
                                raise InterruptedError
                            percent = min(49, int(done * 50 / total)) if total else 0
                            display_total = int(task["total"] or total or 0)
                            speed = download_speed.update(done)
                            self._update(
                                task_id, progress=percent,
                                transferred=int(display_total * percent / 100),
                                stage_transferred=done, stage_total=total,
                                downloaded_bytes=done,
                                download_speed_bytes_per_second=speed,
                                speed_bytes_per_second=speed,
                                message=download_message,
                            )

                        source_path = downloader.download_file(
                            task["source_file"], temporary_path,
                            download_progress, stop_event.is_set,
                            preserve_partial=True,
                            download_threads=self._download_threads,
                        )
                        self._mark_complete_cache(
                            Path(source_path), cache_key, source_file
                        )
                    cache_complete = True
            if progressive_result:
                result_file = progressive_result.file
                self._update(
                    task_id, status="success", phase="done", progress=100,
                    transferred=task["total"],
                    downloaded_bytes=task["total"], uploaded_bytes=task["total"],
                    download_speed_bytes_per_second=0.0,
                    upload_speed_bytes_per_second=0.0,
                    speed_bytes_per_second=0.0,
                    stage_transferred=0, stage_total=0,
                    message=f"已边下载边上传到{target_name_text}",
                    method="progressive",
                    result_file_id=str(result_file.id or ""),
                    result_file_name=str(result_file.name or target_name),
                    result_file_size=int(result_file.size or task["total"] or 0),
                    result_sha1=str(result_file.sha1 or ""),
                    result_md5=str(result_file.md5 or ""),
                    finished_at=time.time(),
                )
                self._delete_cache_path(Path(temporary_path))
                return
            had_download = bool(temporary_path)
            hash_message = (
                "正在校验本地缓存"
                if cache_reused else (
                    f"正在校验从{source_name}下载的文件"
                    if had_download and source_name else "正在校验源文件"
                )
            )
            self._update(task_id, phase="hashing", message=hash_message,
                         progress=50 if had_download else 2,
                         stage_transferred=0, stage_total=0,
                         speed_bytes_per_second=0.0)
            upload_speed = _SpeedSampler()

            def progress(done: int, total: int) -> None:
                if stop_event.is_set():
                    raise InterruptedError
                raw_percent = min(99, int(done * 100 / total)) if total else 0
                percent = 50 + min(49, raw_percent // 2) if had_download else raw_percent
                display_total = int(task["total"] or total or 0)
                display_done = (
                    int(display_total * percent / 100) if had_download else done
                )
                speed = upload_speed.update(done)
                self._update(task_id, phase="uploading",
                             message=f"正在上传到{target_name_text}",
                             progress=percent, transferred=display_done,
                             total=display_total,
                             uploaded_bytes=done,
                             upload_speed_bytes_per_second=speed,
                             stage_transferred=done, stage_total=total,
                             speed_bytes_per_second=speed)

            self._update(task_id, phase="rapid_upload",
                         message=f"正在向{target_name_text}尝试秒传",
                         progress=52 if had_download else 5,
                         stage_transferred=0, stage_total=0)
            transfer_algorithm = "" if remote_rapid_attempted else algorithm
            transfer_checksum = "" if remote_rapid_attempted else checksum
            if (
                    not remote_rapid_attempted and not transfer_algorithm
                    and target.supports(CloudDriveCapability.RAPID_UPLOAD)
            ):
                supported = target.require(
                    CloudDriveCapability.RAPID_UPLOAD
                ).algorithms
                transfer_algorithm = (
                    "md5" if "md5" in supported
                    else "sha1" if "sha1" in supported else ""
                )
                if transfer_algorithm:
                    transfer_checksum = file_checksum(
                        source_path, transfer_algorithm
                    )
            result = CrossDriveTransfer().transfer(
                source_path, target, target_path, target_name,
                transfer_algorithm, transfer_checksum,
                fallback=fallback, progress_callback=progress,
            )
            if not result:
                raise RuntimeError("秒传未命中且普通上传失败" if fallback else "秒传未命中")
            result_file = result.file
            rapid = result.method == "rapid"
            self._update(task_id, status="success", phase="done", progress=100,
                         transferred=task["total"], speed_bytes_per_second=0.0,
                         stage_transferred=0, stage_total=0,
                         message=(f"已秒传到{target_name_text}"
                                  if rapid else f"已上传到{target_name_text}"),
                         method="rapid" if rapid else "fallback",
                         result_file_id=str(result_file.id or ""),
                         result_file_name=str(result_file.name or target_name),
                         result_file_size=int(result_file.size or task["total"] or 0),
                         result_sha1=str(result_file.sha1 or ""),
                         result_md5=str(result_file.md5 or ""),
                         finished_at=time.time())
            if temporary_path:
                self._delete_cache_path(Path(temporary_path))
        except InterruptedError:
            if (
                    'temporary_path' in locals() and temporary_path
                    and not locals().get("cache_complete", False)
                    and not self._cache_parts_path(Path(temporary_path)).is_file()
            ):
                self._delete_cache_path(Path(temporary_path))
            self._update(task_id, status="canceled", phase="canceled", message="任务已取消",
                         speed_bytes_per_second=0.0, finished_at=time.time())
        except Exception as error:
            if (
                    'temporary_path' in locals() and temporary_path
                    and not locals().get("cache_complete", False)
                    and not self._cache_parts_path(Path(temporary_path)).is_file()
            ):
                self._delete_cache_path(Path(temporary_path))
            self._update(task_id, status="failed", phase="failed", message="跨盘传输失败",
                         error=str(error), speed_bytes_per_second=0.0, finished_at=time.time())
        finally:
            self._transfer_slots.release()
