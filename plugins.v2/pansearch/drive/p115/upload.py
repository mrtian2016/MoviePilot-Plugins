"""115 本地文件上传能力。"""

import hashlib
import io
from pathlib import Path
from typing import Callable, Mapping, Optional

import requests
from app.log import logger

from .files import P115DirectoryReader, P115FileService
from ...core import OwnerDelegator

try:
    from p115client import check_response

    P115_AVAILABLE = True
except ImportError:
    P115_AVAILABLE = False


class P115UploadService(OwnerDelegator):
    """使用 p115client 原生上传接口将本地文件写入 115。"""

    rapid_requires_local_file = True

    @staticmethod
    def _file_sha1(path: Path) -> str:
        digest = hashlib.sha1()
        with path.open("rb") as file:
            while chunk := file.read(8 * 1024 * 1024):
                digest.update(chunk)
        return digest.hexdigest().upper()

    def try_rapid_upload(
            self, local_path: str, save_path: str, target_name: str,
            algorithm: str, checksum: str, size: int,
    ) -> bool:
        if algorithm != "sha1" or not P115_AVAILABLE or not self.client:
            return False
        source = Path(local_path)
        files = self._owner._get_component(P115FileService)
        lookup = P115DirectoryReader(files).resolve_directory(save_path, create=True)
        if not source.is_file() or not lookup.checked or lookup.directory_id is None:
            return False

        def read_range(value: str):
            start, end = (int(part) for part in value.split("-", 1))
            with source.open("rb") as handle:
                handle.seek(start)
                return handle.read(end - start + 1)

        response = self._rate_limited_call(
            self.client.upload_file_init,
            target_name,
            checksum.upper(),
            int(size),
            read_range_bytes_or_hash=read_range,
            pid=int(lookup.directory_id or 0),
        )
        check_response(response)
        if not response.get("reuse"):
            return False
        self._target_file_cache.clear()
        return True

    def upload_file(
            self,
            local_path: str,
            save_path: str,
            target_name: str = "",
            file_sha1: str = "",
            progress_callback: Optional[Callable[[int, int], None]] = None,
    ) -> bool:
        if not P115_AVAILABLE or not self.client:
            logger.error("115 本地上传不可用：客户端未初始化")
            return False
        source = Path(str(local_path or ""))
        if not source.is_file():
            logger.error(f"115 本地上传文件不存在：{source}")
            return False
        files = self._owner._get_component(P115FileService)
        lookup = P115DirectoryReader(files).resolve_directory(
            save_path, create=True
        )
        if not lookup.checked or lookup.directory_id is None:
            logger.error(f"115 本地上传目录不可用：{save_path}")
            return False
        upload_name = str(target_name or source.name).strip()
        checksum = str(file_sha1 or "").strip().upper() or self._file_sha1(source)
        try:
            file_size = source.stat().st_size
            if progress_callback:
                with source.open("rb") as file:
                    response = self._rate_limited_call(
                        self.client.upload_file,
                        _ProgressReader(file, file_size, progress_callback),
                        pid=int(lookup.directory_id or 0),
                        filename=upload_name,
                        filesha1=checksum,
                        filesize=file_size,
                        partsize=-1,
                        max_retries=0,
                    )
            else:
                response = self._rate_limited_call(
                    self.client.upload_file,
                    source,
                    pid=int(lookup.directory_id or 0),
                    filename=upload_name,
                    filesha1=checksum,
                    filesize=file_size,
                    partsize=-1,
                    max_retries=0,
                )
            check_response(response)
            self._target_file_cache.clear()
            if progress_callback:
                progress_callback(file_size, file_size)
            logger.info(f"115 本地文件上传完成：{source.name} -> {save_path}/{upload_name}")
            return True
        except Exception as error:
            logger.error(f"115 本地文件上传失败：{source.name}，{error}")
            return False

    def upload_from_link(
            self,
            download_url: str,
            download_headers: Mapping[str, str],
            save_path: str,
            target_name: str,
            file_size: int,
            algorithm: str,
            checksum: str,
            progress_callback: Optional[Callable[[int, int], None]] = None,
            stop_requested: Optional[Callable[[], bool]] = None,
    ) -> bool | str:
        """优先用远程 Range 完成 SHA1 秒传，未命中时继续流式上传。"""
        if (
                not P115_AVAILABLE
                or not self.client
                or algorithm.lower() != "sha1"
                or not checksum
                or int(file_size or 0) <= 0
                or not download_url
        ):
            return False
        files = self._owner._get_component(P115FileService)
        lookup = P115DirectoryReader(files).resolve_directory(
            save_path, create=True
        )
        if not lookup.checked or lookup.directory_id is None:
            raise RuntimeError(f"115 远程上传目录不可用：{save_path}")
        reader = _HttpRangeReader(
            download_url, download_headers, file_size, stop_requested
        )
        uploaded = 0

        def report(size: int) -> None:
            nonlocal uploaded
            if stop_requested and stop_requested():
                raise InterruptedError
            uploaded = min(file_size, uploaded + max(0, int(size or 0)))
            if progress_callback:
                progress_callback(uploaded, file_size)

        try:
            init_response = self._rate_limited_call(
                self.client.upload_file_init,
                filename=target_name,
                filesize=file_size,
                filesha1=checksum.upper(),
                pid=int(lookup.directory_id or 0),
                read_range_bytes_or_hash=reader.read_range_sha1,
            )
            check_response(init_response)
            if init_response.get("reuse"):
                self._target_file_cache.clear()
                logger.info(
                    f"115 SHA1 秒传完成：{target_name} -> "
                    f"{save_path}/{target_name}"
                )
                return "rapid"

            reader.seek(0)
            response = self._rate_limited_call(
                self.client.upload_file,
                reader,
                pid=int(lookup.directory_id or 0),
                filename=target_name,
                filesha1=checksum.upper(),
                filesize=file_size,
                partsize=-1,
                reporthook=report,
                max_retries=0,
            )
            check_response(response)
            self._target_file_cache.clear()
            if progress_callback:
                progress_callback(file_size, file_size)
            logger.info(
                f"115 Range 直传完成：{target_name} -> {save_path}/{target_name}"
            )
            return "remote"
        except InterruptedError:
            raise
        except Exception as error:
            logger.warning(f"115 Range 直传失败，将回退本地缓存：{error}")
            return False
        finally:
            reader.close()


class _ProgressReader:
    """为 p115client 的文件读取过程补充字节进度回调。"""

    def __init__(self, file, total: int, callback: Callable[[int, int], None]):
        self._file = file
        self._total = total
        self._callback = callback

    def read(self, size: int = -1):
        chunk = self._file.read(size)
        self._callback(self._file.tell(), self._total)
        return chunk

    def __getattr__(self, name):
        return getattr(self._file, name)


class _HttpRangeReader(io.RawIOBase):
    """把远程下载地址适配为 p115oss 所需的 read/seek 文件对象。"""

    def __init__(
            self, url: str, headers: Mapping[str, str], size: int,
            stop_requested: Optional[Callable[[], bool]] = None,
    ):
        self._url = url
        self._headers = dict(headers or {})
        self._size = int(size)
        self._position = 0
        self._stop_requested = stop_requested
        self._session = requests.Session()

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self._position

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        if whence == io.SEEK_CUR:
            offset += self._position
        elif whence == io.SEEK_END:
            offset += self._size
        elif whence != io.SEEK_SET:
            raise ValueError(f"不支持的 seek whence：{whence}")
        if offset < 0:
            raise ValueError("seek 位置不能小于 0")
        self._position = min(int(offset), self._size)
        return self._position

    def read(self, size: int = -1) -> bytes:
        if self._stop_requested and self._stop_requested():
            raise InterruptedError
        if self._position >= self._size:
            return b""
        length = self._size - self._position if size is None or size < 0 else size
        end = min(self._size, self._position + int(length)) - 1
        if end < self._position:
            return b""
        start = self._position
        headers = dict(self._headers)
        headers["Range"] = f"bytes={start}-{end}"
        headers.setdefault("Accept-Encoding", "identity")
        for attempt in range(3):
            try:
                with self._session.get(
                        self._url, headers=headers, timeout=(15, 120), stream=True,
                ) as response:
                    if response.status_code != 206:
                        raise IOError(
                            f"源盘不支持 HTTP Range：HTTP {response.status_code}"
                        )
                    content_range = str(response.headers.get("Content-Range") or "")
                    if not content_range.lower().startswith(
                            f"bytes {start}-{end}/".lower()
                    ):
                        raise IOError(
                            f"源盘返回无效 Content-Range：{content_range or '空'}"
                        )
                    data = response.content
                break
            except requests.RequestException:
                if attempt >= 2:
                    raise
        expected = end - start + 1
        if len(data) != expected:
            raise IOError(f"源盘 Range 读取不完整：{len(data)}/{expected}")
        self._position += len(data)
        return data

    def read_range_sha1(self, value: str) -> str:
        """按 115 的二次校验范围读取源盘并返回大写 SHA1。"""
        try:
            start, end = (int(part) for part in str(value).split("-", 1))
        except (TypeError, ValueError) as error:
            raise ValueError(f"无效的 115 校验范围：{value}") from error
        if start < 0 or end < start or end >= self._size:
            raise ValueError(f"115 校验范围越界：{value}/{self._size}")
        current = self._position
        try:
            self.seek(start)
            content = self.read(end - start + 1)
        finally:
            self._position = current
        return hashlib.sha1(content).hexdigest().upper()

    def close(self) -> None:
        if not self.closed:
            self._session.close()
        super().close()
