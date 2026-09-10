"""
115网盘客户端封装
"""
import time
import threading
from pathlib import Path
from functools import wraps
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any, Tuple, Callable

from app.log import logger
try:
    from p115client import P115Client, check_response
    from p115client.util import share_extract_payload
    from p115client.tool.iterdir import share_iterdir
    P115_AVAILABLE = True
except ImportError:
    P115_AVAILABLE = False
    logger.warning("p115client 未安装，115网盘功能不可用，请安装: pip install p115client")


@dataclass
class ShareLinkStatus:
    """
    分享链接状态信息

    用于表示 115 分享链接的有效性和详细状态
    """
    is_valid: bool = False              # 链接是否有效可用
    is_expired: bool = False            # 链接是否已过期
    is_cancelled: bool = False          # 链接是否已被取消
    is_deleted: bool = False            # 分享的文件是否已删除
    error_code: int = 0                 # 错误码（0 表示无错误）
    error_message: str = ""             # 错误信息
    file_count: int = 0                 # 分享中的文件数量
    share_info: Dict[str, Any] = field(default_factory=dict)  # 分享详情

    @property
    def status_text(self) -> str:
        """获取状态的中文描述"""
        if self.is_valid:
            return "有效"
        if self.is_expired:
            return "已过期"
        if self.is_cancelled:
            return "已取消"
        if self.is_deleted:
            return "文件已删除"
        if self.error_message:
            return self.error_message
        return "未知状态"


class RateLimiter:
    """
    API 请求速率限制器
    确保请求之间有最小间隔，并添加随机抖动避免触发风控
    """

    def __init__(self, min_interval: float = 1.5, jitter_ratio: float = 0.3):
        """
        :param min_interval: 基础请求间隔（秒），实际间隔会在此基础上随机浮动
        :param jitter_ratio: 抖动比例，如 0.3 表示 ±30% 的随机浮动
        """
        self.min_interval = min_interval
        self.jitter_ratio = jitter_ratio
        self.last_request_time = 0.0
        self._lock = threading.Lock()

    def _get_jittered_interval(self) -> float:
        """获取带随机抖动的间隔时间"""
        import random
        jitter = self.min_interval * self.jitter_ratio
        return self.min_interval + random.uniform(-jitter, jitter)

    def wait(self):
        """等待直到可以发起下一次请求（带随机抖动）"""
        with self._lock:
            now = time.time()
            elapsed = now - self.last_request_time
            target_interval = self._get_jittered_interval()
            if elapsed < target_interval:
                sleep_time = target_interval - elapsed
                time.sleep(sleep_time)
            self.last_request_time = time.time()

    def acquire(self):
        """获取请求许可（wait 的别名）"""
        self.wait()


def retry_on_failure(
    max_retries: int = 3,
    initial_delay: float = 1.0,
    backoff_factor: float = 2.0,
    retryable_exceptions: tuple = (Exception,)
):
    """
    带指数退避的重试装饰器

    :param max_retries: 最大重试次数
    :param initial_delay: 初始延迟（秒）
    :param backoff_factor: 退避倍数
    :param retryable_exceptions: 可重试的异常类型
    """
    def decorator(func: Callable):
        @wraps(func)
        def wrapper(*args, **kwargs):
            delay = initial_delay
            last_exception = None

            for attempt in range(max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except retryable_exceptions as e:
                    last_exception = e
                    if attempt < max_retries:
                        logger.info(f"请求失败 (尝试 {attempt + 1}/{max_retries + 1}): {e}, {delay:.1f}秒后重试...")
                        time.sleep(delay)
                        delay *= backoff_factor
                    else:
                        logger.warning(f"请求失败，已达最大重试次数 ({max_retries + 1}): {e}")
                        raise

            raise last_exception
        return wrapper
    return decorator


class PathCache:
    """
    路径缓存，带 TTL（生存时间）支持
    """

    def __init__(self, default_ttl: int = 3600):
        """
        :param default_ttl: 默认缓存过期时间（秒）
        """
        self.default_ttl = default_ttl
        self._cache: Dict[str, Tuple[int, float]] = {}  # path -> (cid, timestamp)
        self._lock = threading.Lock()

    def get(self, path: str) -> Optional[int]:
        """获取缓存的 CID，如果缓存过期则返回 None"""
        with self._lock:
            if path not in self._cache:
                return None
            cid, timestamp = self._cache[path]
            if time.time() - timestamp > self.default_ttl:
                del self._cache[path]
                return None
            return cid

    def set(self, path: str, cid: int):
        """设置缓存"""
        with self._lock:
            self._cache[path] = (cid, time.time())

    def invalidate(self, path: str):
        """使缓存失效"""
        with self._lock:
            self._cache.pop(path, None)

    def clear(self):
        """清空缓存"""
        with self._lock:
            self._cache.clear()

    def __contains__(self, path: str) -> bool:
        return self.get(path) is not None


class P115ClientManager:
    """115网盘客户端管理器"""

    # 默认配置常量
    DEFAULT_MIN_INTERVAL = 1.5      # API 请求基础间隔（秒），实际会有 ±30% 随机浮动
    DEFAULT_RECURSION_DELAY = 1.0   # 递归遍历子目录延迟（秒）
    DEFAULT_PATH_CACHE_TTL = 3600   # 路径缓存过期时间（秒）
    DEFAULT_MAX_RETRIES = 3         # 最大重试次数
    DEFAULT_JITTER_RATIO = 0.3      # 请求间隔随机抖动比例（±30%）

    def __init__(
        self,
        cookies: str,
        user_agent: str = None,
        min_interval: float = None,
        recursion_delay: float = None,
        path_cache_ttl: int = None
    ):
        """
        初始化115客户端

        :param cookies: 115 Cookie
        :param user_agent: User-Agent
        :param min_interval: API 请求最小间隔（秒），默认 0.5
        :param recursion_delay: 递归遍历子目录延迟（秒），默认 0.3
        :param path_cache_ttl: 路径缓存过期时间（秒），默认 3600
        """
        # API 调用计数器
        self._api_call_count = 0

        self.cookies = cookies
        self.user_agent = user_agent or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        self.client: Optional[Any] = None

        # 速率限制
        _min_interval = min_interval if min_interval is not None else self.DEFAULT_MIN_INTERVAL
        self.rate_limiter = RateLimiter(min_interval=_min_interval)

        # 递归延迟
        self.recursion_delay = recursion_delay if recursion_delay is not None else self.DEFAULT_RECURSION_DELAY

        # 路径缓存（带 TTL）
        _path_cache_ttl = path_cache_ttl if path_cache_ttl is not None else self.DEFAULT_PATH_CACHE_TTL
        self.path_cache = PathCache(default_ttl=_path_cache_ttl)
        # 根目录始终缓存
        self.path_cache.set("/", 0)

        # 分享信息缓存（URL -> {share_code, receive_code}）
        self._share_info_cache: Dict[str, Dict[str, str]] = {}

        if P115_AVAILABLE and cookies:
            try:
                self.client = P115Client(cookies, app="web")
            except Exception as e:
                logger.error(f"初始化 P115Client 失败: {e}")

    def _rate_limited_call(self, func: Callable, *args, **kwargs):
        """
        带速率限制的 API 调用封装

        :param func: 要调用的函数
        :return: 函数返回值
        """
        self.rate_limiter.wait()
        return func(*args, **kwargs)

    def check_login(self) -> bool:
        """检查登录状态"""
        if not self.client:
            return False

        try:
            self.rate_limiter.wait()
            self._api_call_count += 1
            user_info = self.client.user_my_info()
            if user_info.get("state"):
                uname = user_info.get('data', {}).get('uname', '未知')
                logger.info(f"115 登录成功: {uname}")
                return True
            return False
        except Exception as e:
            logger.error(f"检查 115 登录状态失败: {e}")
            return False

    def get_pid_by_path(self, path: str, mkdir: bool = True) -> int:
        """
        通过文件夹路径获取 CID (Directory ID)

        :param path: 文件夹路径 (例如: /我的接收/电影)
        :param mkdir: 如果目录不存在，是否创建
        :return: 文件夹 ID，0 为根目录，-1 为获取失败
        """
        if not self.client:
            return -1

        # 规范化路径
        path = path.replace("\\", "/")
        if not path.startswith("/"):
            path = "/" + path
        path = path.rstrip("/")

        # 根目录
        if not path or path == "/":
            return 0

        # 尝试从缓存获取
        cached_cid = self.path_cache.get(path)
        if cached_cid is not None:
            return cached_cid

        # 尝试直接通过 API 获取完整路径
        try:
            self.rate_limiter.wait()
            self._api_call_count += 1
            resp = self.client.fs_dir_getid(path)
            if resp.get("id"):
                cid = int(resp["id"])
                self.path_cache.set(path, cid)
                return cid
        except Exception as e:
            logger.info(f"直接获取路径 ID 失败 ({path}): {e}")

        # 如果不创建，则返回失败
        if not mkdir:
            return -1

        # ===== 优化：创建模式下直接逐级创建，不再每层都先尝试获取 =====
        parts = [p for p in path.split("/") if p]
        parent_id = 0
        current_path = ""
        start_index = 0

        # 找到最近的已缓存父目录
        temp_path = ""
        for i, part in enumerate(parts):
            temp_path = f"{temp_path}/{part}"
            temp_cid = self.path_cache.get(temp_path)
            if temp_cid is not None:
                parent_id = temp_cid
                current_path = temp_path
                start_index = i + 1

        # 从未缓存的部分开始处理（优化：直接创建，不再先获取）
        for i in range(start_index, len(parts)):
            part = parts[i]
            current_path = f"{current_path}/{part}"

            # 再次检查缓存（可能在并发中被设置）
            cached = self.path_cache.get(current_path)
            if cached is not None:
                parent_id = cached
                continue

            # 直接创建目录（fs_makedirs_app 会自动处理已存在的情况）
            try:
                self.rate_limiter.wait()
                self._api_call_count += 1
                resp = self.client.fs_makedirs_app(part, pid=parent_id)
                check_response(resp)
                if resp.get("state"):
                    cid = int(resp["cid"])
                    self.path_cache.set(current_path, cid)
                    parent_id = cid
                    logger.info(f"创建目录成功: {current_path} -> {cid}")
                elif resp.get("errno") == 20004 or "已存在" in resp.get("error", ""):
                    # 目录已存在，尝试获取其 ID
                    try:
                        self.rate_limiter.wait()
                        self._api_call_count += 1
                        get_resp = self.client.fs_dir_getid(current_path)
                        if get_resp.get("id"):
                            cid = int(get_resp["id"])
                            self.path_cache.set(current_path, cid)
                            parent_id = cid
                            continue
                    except Exception:
                        pass
                    logger.error(f"目录已存在但无法获取ID: {current_path}")
                    return -1
                else:
                    logger.error(f"创建目录失败 {current_path}: {resp.get('error')}")
                    return -1
            except Exception as e:
                logger.error(f"创建目录异常 {current_path}: {e}")
                return -1

        return parent_id

    def extract_share_info(self, url: str) -> Dict[str, str]:
        """
        解析分享链接，获取 share_code 和 receive_code（带缓存）

        :param url: 115 分享链接
        :return: {"share_code": ..., "receive_code": ...}
        """
        if not P115_AVAILABLE:
            return {}

        # 检查缓存
        if url in self._share_info_cache:
            return self._share_info_cache[url]

        try:
            payload = share_extract_payload(url)
            result = {
                "share_code": payload.get("share_code", ""),
                "receive_code": payload.get("receive_code", "")
            }
            # 缓存结果
            self._share_info_cache[url] = result
            return result
        except Exception as e:
            logger.error(f"解析分享链接失败: {e}")
            return {}

    def check_share_status(self, share_url: str) -> ShareLinkStatus:
        """
        检查分享链接的状态（是否有效、过期、失效等）

        :param share_url: 115 分享链接
        :return: ShareLinkStatus 对象，包含详细的状态信息
        """
        # 默认返回无效状态
        status = ShareLinkStatus()

        if not self.client:
            status.error_message = "客户端未初始化"
            return status

        # 解析分享链接
        info = self.extract_share_info(share_url)
        share_code = info.get("share_code")
        receive_code = info.get("receive_code")

        if not share_code:
            status.error_message = "无效的分享链接格式"
            return status

        try:
            # 使用 share_snap 接口检查分享状态
            self.rate_limiter.wait()
            self._api_call_count += 1
            payload = {
                "share_code": share_code,
                "receive_code": receive_code or "",
                "cid": 0,
                "limit": 1,  # 只获取1条记录，用于验证
                "offset": 0,
            }
            resp = self.client.share_snap(payload)

            # 检查响应状态
            state = resp.get("state")

            if state is True or state == 1:
                # 分享有效
                status.is_valid = True
                status.error_code = 0

                # 获取分享信息
                data = resp.get("data", {})
                share_info = data.get("shareinfo", {})
                file_list = data.get("list", [])

                status.file_count = int(data.get("count", len(file_list)))
                status.share_info = {
                    "share_title": share_info.get("share_title", ""),
                    "share_state": share_info.get("share_state", ""),
                    "file_count": status.file_count,
                    "create_time": share_info.get("create_time", ""),
                    "expire_time": share_info.get("expire_time", ""),
                    "user_name": share_info.get("user_name", ""),
                }
            else:
                # 分享无效，解析错误信息
                status.is_valid = False
                status.error_code = resp.get("errno", resp.get("errcode", -1))
                status.error_message = resp.get("error", resp.get("message", "未知错误"))

                # 根据错误码判断具体原因
                error_msg_lower = status.error_message.lower()
                error_msg = status.error_message

                # 判断是否过期
                if "过期" in error_msg or "expired" in error_msg_lower:
                    status.is_expired = True

                # 判断是否取消
                if "取消" in error_msg or "cancel" in error_msg_lower:
                    status.is_cancelled = True

                # 判断是否删除
                if "删除" in error_msg or "不存在" in error_msg or "delete" in error_msg_lower:
                    status.is_deleted = True

                logger.info(f"分享链接无效: {status.error_message} (errno: {status.error_code})")

        except Exception as e:
            status.error_message = f"检查分享状态异常: {str(e)}"
            logger.error(status.error_message)

        return status

    def is_share_valid(self, share_url: str) -> bool:
        """
        快速检查分享链接是否有效

        :param share_url: 115 分享链接
        :return: True 表示有效，False 表示无效或失效
        """
        status = self.check_share_status(share_url)
        return status.is_valid

    def list_share_files(
            self,
            share_url: str,
            cid: int = 0,
            max_depth: int = 3,
            target_season: int = None
    ) -> List[dict]:
        """
        列出分享链接内的文件

        :param share_url: 115 分享链接
        :param cid: 目录 ID，0 为根目录
        :param max_depth: 最大递归深度
        :param target_season: 目标季数，用于优化递归（跳过明显不匹配的目录）
        :return: 文件列表
        """
        if not self.client:
            return []

        info = self.extract_share_info(share_url)
        share_code = info.get("share_code")
        receive_code = info.get("receive_code")

        if not share_code or not receive_code:
            logger.error("无效的分享链接或解析失败")
            return []

        return self._list_share_files_recursive(
            share_code=share_code,
            receive_code=receive_code,
            cid=cid,
            depth=1,
            max_depth=max_depth,
            target_season=target_season
        )

    def _list_share_files_recursive(
            self,
            share_code: str,
            receive_code: str,
            cid: int = 0,
            depth: int = 1,
            max_depth: int = 3,
            target_season: int = None
    ) -> List[dict]:
        """递归列出分享文件（带速率限制和季数过滤优化）"""
        if depth > max_depth:
            return []

        files = []
        try:
            # 速率限制
            self.rate_limiter.wait()
            self._api_call_count += 1

            iterator = share_iterdir(
                self.client,
                share_code=share_code,
                receive_code=receive_code,
                cid=cid,
                app="web",
            )

            for item in iterator:
                file_info = {
                    "id": str(item.get("id", "")),
                    "name": item.get("name", ""),
                    "size": item.get("size", 0),
                    "is_dir": item.get("is_dir", False),
                    "sha1": item.get("sha1", ""),
                    "pick_code": item.get("pick_code", ""),
                }

                # 递归获取子目录内容（带随机延迟）
                if file_info["is_dir"] and depth < max_depth:
                    dir_name = file_info["name"]

                    # 优化：如果指定了目标季数，跳过明显不匹配的季目录
                    if target_season is not None:
                        skip_dir = self._should_skip_season_dir(dir_name, target_season)
                        if skip_dir:
                            logger.info(f"跳过非目标季目录: {dir_name} (目标: S{target_season})")
                            files.append(file_info)  # 仍然记录目录信息，但不递归
                            continue

                    # 递归前增加随机延迟，避免频繁请求
                    if self.recursion_delay > 0:
                        import random
                        jitter = self.recursion_delay * 0.3  # ±30% 随机浮动
                        delay = self.recursion_delay + random.uniform(-jitter, jitter)
                        time.sleep(delay)

                    sub_cid = int(item.get("id", 0))
                    children = self._list_share_files_recursive(
                        share_code=share_code,
                        receive_code=receive_code,
                        cid=sub_cid,
                        depth=depth + 1,
                        max_depth=max_depth,
                        target_season=target_season
                    )
                    file_info["children"] = children

                files.append(file_info)

        except Exception as e:
            logger.error(f"列出分享文件失败: {e}")

        return files

    def _should_skip_season_dir(self, dir_name: str, target_season: int) -> bool:
        """
        判断是否应该跳过该目录（明显是其他季的目录）

        :param dir_name: 目录名
        :param target_season: 目标季数
        :return: True 表示应跳过，False 表示需要递归
        """
        import re

        # 常见的季数目录命名模式
        patterns = [
            r'[Ss]eason\s*(\d+)',      # Season 1, season1
            r'[Ss](\d+)',              # S1, s01
            r'第(\d+)季',              # 第1季
            r'第([一二三四五六七八九十]+)季',  # 第一季
        ]

        # 中文数字映射
        cn_num_map = {'一': 1, '二': 2, '三': 3, '四': 4, '五': 5,
                      '六': 6, '七': 7, '八': 8, '九': 9, '十': 10}

        for pattern in patterns:
            match = re.search(pattern, dir_name)
            if match:
                season_str = match.group(1)
                # 转换中文数字
                if season_str in cn_num_map:
                    found_season = cn_num_map[season_str]
                else:
                    try:
                        found_season = int(season_str)
                    except ValueError:
                        continue

                # 如果目录明确是其他季，跳过
                if found_season != target_season:
                    return True
                else:
                    # 明确是目标季，不跳过
                    return False

        # 目录名没有明显的季数标识，不跳过（可能包含多季或其他内容）
        return False

    def transfer_share(self, share_url: str, save_path: str) -> bool:
        """
        转存整个分享链接到指定目录

        :param share_url: 115 分享链接
        :param save_path: 保存路径
        :return: 是否成功
        """
        if not self.client:
            return False

        info = self.extract_share_info(share_url)
        share_code = info.get("share_code")
        receive_code = info.get("receive_code")

        if not share_code or not receive_code:
            logger.error("无效的分享链接或解析失败")
            return False

        # 获取目标目录 CID
        parent_id = self.get_pid_by_path(save_path, mkdir=True)
        if parent_id == -1:
            logger.error(f"无法获取或创建目标目录: {save_path}")
            return False

        logger.info(f"转存分享到目录 ID: {parent_id} ({save_path})")

        # 执行转存 (file_id=0 表示转存所有内容)
        return self._do_transfer(
            share_code=share_code,
            receive_code=receive_code,
            file_id="0",
            parent_id=parent_id,
            save_path=save_path
        )

    def transfer_file(
            self,
            share_url: str,
            file_id: str,
            save_path: str
    ) -> bool:
        """
        转存分享中的单个文件

        :param share_url: 115 分享链接
        :param file_id: 文件 ID
        :param save_path: 保存路径
        :return: 是否成功
        """
        if not self.client:
            return False

        info = self.extract_share_info(share_url)
        share_code = info.get("share_code")
        receive_code = info.get("receive_code")

        if not share_code or not receive_code:
            logger.error("无效的分享链接或解析失败")
            return False

        # 获取目标目录 CID
        parent_id = self.get_pid_by_path(save_path, mkdir=True)
        if parent_id == -1:
            logger.error(f"无法获取或创建目标目录: {save_path}")
            return False

        # 执行单文件转存
        return self._do_transfer(
            share_code=share_code,
            receive_code=receive_code,
            file_id=file_id,
            parent_id=parent_id,
            save_path=save_path
        )

    def transfer_files_batch(
            self,
            share_url: str,
            file_ids: List[str],
            save_path: str,
            batch_size: int = 20,
            batch_interval: float = 3.0
    ) -> Tuple[List[str], List[str]]:
        """
        批量转存分享中的多个文件，减少 API 调用次数以避免风控

        :param share_url: 115 分享链接
        :param file_ids: 文件 ID 列表
        :param save_path: 保存路径
        :param batch_size: 每批转存的文件数量，默认 20
        :param batch_interval: 批次之间的间隔时间（秒），默认 3 秒
        :return: (成功的 file_ids 列表, 失败的 file_ids 列表)
        """
        success_ids: List[str] = []
        failed_ids: List[str] = []
        batch_size = int(batch_size)

        if not self.client:
            return success_ids, file_ids

        if not file_ids:
            return success_ids, failed_ids

        info = self.extract_share_info(share_url)
        share_code = info.get("share_code")
        receive_code = info.get("receive_code")

        if not share_code or not receive_code:
            logger.error("无效的分享链接或解析失败")
            return success_ids, file_ids

        # 获取目标目录 CID（只需获取一次）
        parent_id = self.get_pid_by_path(save_path, mkdir=True)
        if parent_id == -1:
            logger.error(f"无法获取或创建目标目录: {save_path}")
            return success_ids, file_ids

        total_batches = (len(file_ids) + batch_size - 1) // batch_size
        logger.info(f"批量转存: 共 {len(file_ids)} 个文件，分 {total_batches} 批处理（每批 {batch_size} 个）")

        # 分批处理
        for batch_index in range(0, len(file_ids), batch_size):
            batch = file_ids[batch_index:batch_index + batch_size]
            batch_num = batch_index // batch_size + 1

            # 使用逗号分隔多个文件 ID
            file_id_str = ",".join(batch)

            logger.info(f"处理第 {batch_num}/{total_batches} 批，包含 {len(batch)} 个文件")

            success = self._do_transfer(
                share_code=share_code,
                receive_code=receive_code,
                file_id=file_id_str,
                parent_id=parent_id,
                save_path=save_path
            )

            if success:
                success_ids.extend(batch)
                logger.info(f"第 {batch_num} 批转存成功")
            else:
                # 批量失败时，尝试逐个转存以确定哪些失败
                logger.warning(f"第 {batch_num} 批批量转存失败，尝试逐个转存...")
                for fid in batch:
                    single_success = self._do_transfer(
                        share_code=share_code,
                        receive_code=receive_code,
                        file_id=fid,
                        parent_id=parent_id,
                        save_path=save_path
                    )
                    if single_success:
                        success_ids.append(fid)
                    else:
                        failed_ids.append(fid)

            # 批次之间添加间隔，避免触发风控
            if batch_index + batch_size < len(file_ids):
                import random
                jitter = batch_interval * 0.3
                actual_interval = batch_interval + random.uniform(-jitter, jitter)
                logger.info(f"批次间隔 {actual_interval:.1f} 秒")
                time.sleep(actual_interval)

        logger.info(f"批量转存完成: 成功 {len(success_ids)} 个，失败 {len(failed_ids)} 个")
        return success_ids, failed_ids

    def _do_transfer(
            self,
            share_code: str,
            receive_code: str,
            file_id: str,
            parent_id: int,
            save_path: str,
            max_retries: int = None
    ) -> bool:
        """
        执行实际转存操作（带重试）

        :param share_code: 分享码
        :param receive_code: 接收码
        :param file_id: 文件ID，"0" 表示转存全部
        :param parent_id: 目标目录 ID
        :param save_path: 保存路径（用于日志）
        :param max_retries: 最大重试次数
        :return: 是否成功
        """
        if max_retries is None:
            max_retries = self.DEFAULT_MAX_RETRIES

        payload = {
            "share_code": share_code,
            "receive_code": receive_code,
            "file_id": file_id,
            "cid": parent_id,
            "is_check": 0,
        }

        last_error = None
        for attempt in range(max_retries + 1):
            try:
                self.rate_limiter.wait()
                self._api_call_count += 1
                resp = self.client.share_receive(payload)

                if resp.get("state"):
                    if file_id == "0":
                        logger.info(f"转存成功！已保存到: {save_path}")
                    else:
                        logger.info(f"文件转存成功！文件ID: {file_id}, 保存到: {save_path}")
                    return True
                else:
                    error_msg = resp.get("error", "未知错误")
                    error_code = resp.get("errno", resp.get("errcode", 0))

                    # 检查是否是重复文件
                    if "重复" in error_msg or "已存在" in error_msg:
                        logger.info(f"文件已存在，跳过: {file_id}")
                        return True

                    # 检查是否是可重试的错误（如限流）
                    if error_code in (990001, 990002, 990009):  # 常见的限流错误码
                        if attempt < max_retries:
                            wait_time = (attempt + 1) * 2  # 递增等待时间
                            logger.warning(f"遇到限流，{wait_time}秒后重试 (尝试 {attempt + 1}/{max_retries + 1})")
                            time.sleep(wait_time)
                            continue

                    logger.error(f"转存失败: {error_msg} (错误码: {error_code})")
                    return False

            except Exception as e:
                last_error = e
                if attempt < max_retries:
                    wait_time = (attempt + 1) * 1.5
                    logger.warning(f"转存异常: {e}, {wait_time:.1f}秒后重试 (尝试 {attempt + 1}/{max_retries + 1})")
                    time.sleep(wait_time)
                else:
                    logger.error(f"转存过程中发生异常: {e}")
                    return False

        return False

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

        try:
            self.rate_limiter.wait()
            self._api_call_count += 1
            resp = self.client.fs_files({"cid": cid, "limit": 1000})
            if resp.get("state"):
                return resp.get("data", [])
            return []
        except Exception as e:
            logger.error(f"列出文件失败: {e}")
            return []

    def list_directories(self, path: str) -> List[dict]:
        """
        列出指定路径下的所有目录（不包含文件）

        :param path: 目录路径
        :return: 目录列表，每个目录包含 name 和 path 字段
        """
        files = self.list_files(path)

        # 过滤出目录（fid=0 表示目录）
        directories = []
        for f in files:
            if f.get("fid") == 0:  # 是目录
                dir_name = f.get("name", "")
                dir_path = f"{path.rstrip('/')}/{dir_name}" if path != "/" else f"/{dir_name}"
                directories.append({
                    "name": dir_name,
                    "path": dir_path,
                    "cid": f.get("cid", 0)
                })

        return directories

    def clear_path_cache(self):
        """清空路径缓存"""
        self.path_cache.clear()
        self.path_cache.set("/", 0)

    def clear_share_cache(self):
        """清空分享信息缓存"""
        self._share_info_cache.clear()

    def get_api_call_count(self) -> int:
        """获取 API 调用次数"""
        return self._api_call_count

    def reset_api_call_count(self):
        """重置 API 调用计数器"""
        self._api_call_count = 0