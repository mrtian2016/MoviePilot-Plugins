"""
115网盘客户端封装
"""

from __future__ import annotations

import random
import threading
import time
from collections import OrderedDict, defaultdict
from dataclasses import dataclass
from typing import Optional, List, Dict, Any, Callable, Tuple

from app.log import logger

try:
    from p115client import P115Client, check_response
    from p115client.util import share_extract_payload
    from p115client.tool.iterdir import share_iterdir
    P115_AVAILABLE = True
except ImportError:
    P115_AVAILABLE = False
    logger.warning("p115client 未安装，115网盘功能不可用，请安装: pip install p115client")


# ----------------------------
# 工具：线程安全 LRU + TTL 缓存（含负缓存）
# ----------------------------

@dataclass
class _CacheItem:
    value: Any
    expire_at: float


class _LRUTTLCache:
    def __init__(self, maxsize: int = 5000, ttl_sec: float = 1800.0):
        self.maxsize = maxsize
        self.ttl_sec = ttl_sec
        self._data: OrderedDict[str, _CacheItem] = OrderedDict()
        self._lock = threading.RLock()

    def get(self, key: str) -> Tuple[bool, Any]:
        now = time.time()
        with self._lock:
            item = self._data.get(key)
            if not item:
                return False, None
            if item.expire_at < now:
                self._data.pop(key, None)
                return False, None
            self._data.move_to_end(key)
            return True, item.value

    def set(self, key: str, value: Any, ttl_sec: Optional[float] = None) -> None:
        now = time.time()
        exp = now + (self.ttl_sec if ttl_sec is None else ttl_sec)
        with self._lock:
            self._data[key] = _CacheItem(value=value, expire_at=exp)
            self._data.move_to_end(key)
            while len(self._data) > self.maxsize:
                self._data.popitem(last=False)

    def delete(self, key: str) -> None:
        with self._lock:
            self._data.pop(key, None)


# ----------------------------
# 工具：请求节流 + 抖动
# ----------------------------

class _RateLimiter:
    def __init__(self, rps: float, jitter_sec: float = 0.25):
        self.rps = max(0.0001, float(rps))
        self.min_interval = 1.0 / self.rps
        self.jitter_sec = max(0.0, float(jitter_sec))
        self._lock = threading.Lock()
        self._next_ts = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.time()
            if now < self._next_ts:
                time.sleep(self._next_ts - now)
            self._next_ts = time.time() + self.min_interval + random.uniform(0.0, self.jitter_sec)


# ----------------------------
# 工具：singleflight（同 key 并发合并）
# ----------------------------

@dataclass
class _SFSlot:
    event: threading.Event
    result: Any = None
    exc: Optional[BaseException] = None
    started_at: float = 0.0


class _SingleFlight:
    """
    同 key 的并发请求只允许一个执行，其它等待结果。
    """
    def __init__(self):
        self._lock = threading.Lock()
        self._inflight: Dict[str, _SFSlot] = {}

    def do(self, key: str, fn: Callable[[], Any]) -> Any:
        with self._lock:
            slot = self._inflight.get(key)
            if slot is None:
                slot = _SFSlot(event=threading.Event(), started_at=time.time())
                self._inflight[key] = slot
                leader = True
            else:
                leader = False

        if not leader:
            slot.event.wait()
            if slot.exc:
                raise slot.exc
            return slot.result

        try:
            res = fn()
            slot.result = res
            return res
        except BaseException as e:
            slot.exc = e
            raise
        finally:
            slot.event.set()
            with self._lock:
                self._inflight.pop(key, None)


# ----------------------------
# 工具：指数退避重试 + 风控/限频识别
# ----------------------------

def _stringify_exc(e: Exception) -> str:
    try:
        return f"{type(e).__name__}: {e}"
    except Exception:
        return repr(e)


def _is_risk_control_text(s: str) -> bool:
    if not s:
        return False
    s = s.lower()
    keywords = [
        "risk", "风控", "频繁", "太快", "too fast", "limit", "rate", "throttle",
        "验证码", "verify", "validation", "安全校验", "请稍后", "稍后再试", "系统繁忙",
        "429", "403", "forbidden", "denied"
    ]
    return any(k in s for k in keywords)


def _is_risk_control_error(resp_or_exc: Any) -> bool:
    if isinstance(resp_or_exc, Exception):
        return _is_risk_control_text(_stringify_exc(resp_or_exc))
    if isinstance(resp_or_exc, dict):
        parts = []
        for k in ("error", "msg", "message", "errno", "errcode", "code"):
            v = resp_or_exc.get(k)
            if v is not None:
                parts.append(str(v))
        return _is_risk_control_text(" | ".join(parts))
    return _is_risk_control_text(str(resp_or_exc))


# ----------------------------
# 主类（外部接口兼容）
# ----------------------------

class P115ClientManager:
    """115网盘客户端管理器（singleflight + 统计 + 风控友好）"""

    _DEFAULT_LIMITS = {
        "dir_getid": (2.0, 0.25),
        "fs_files": (1.0, 0.35),
        "share_iter": (1.2, 0.30),
        "share_receive": (0.7, 0.45),
        "user_my_info": (0.5, 0.20),
        "fs_makedirs": (0.8, 0.35),
    }

    def __init__(self, cookies: str, user_agent: str = None):
        self.cookies = cookies
        self.user_agent = user_agent or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        self.client: Optional[Any] = None

        # 兼容原字段
        self.path_cache: Dict[str, int] = {"/": 0}

        # 强缓存
        self._pid_cache = _LRUTTLCache(maxsize=8000, ttl_sec=60 * 60)
        self._pid_cache.set("/", 0, ttl_sec=24 * 3600)

        self._files_cache = _LRUTTLCache(maxsize=2000, ttl_sec=15.0)

        # 同路径/同转存 串行化 lock
        self._locks = defaultdict(threading.Lock)

        # limiter
        self._limiters: Dict[str, _RateLimiter] = {}
        for name, (rps, jitter) in self._DEFAULT_LIMITS.items():
            self._limiters[name] = _RateLimiter(rps=rps, jitter_sec=jitter)

        # singleflight：用于 fs_dir_getid(path)
        self._sf_dir_getid = _SingleFlight()

        # 请求计数
        self._req_count = defaultdict(int)
        self._req_count_lock = threading.Lock()
        self._log_req_every = 5  # 每累计 N 次请求自动打印一次统计
        self._req_total = 0

        if P115_AVAILABLE and cookies:
            try:
                self.client = P115Client(cookies, app="web")
            except Exception as e:
                logger.error(f"初始化 P115Client 失败: {e}")

    # ----------------------------
    # 统计：对外可选调用（不影响现有业务）
    # ----------------------------

    def get_request_stats(self) -> Dict[str, int]:
        """获取接口请求次数统计（含 total）"""
        with self._req_count_lock:
            d = dict(self._req_count)
            d["total"] = self._req_total
            return d

    def log_request_stats(self, level: str = "info") -> None:
        """打印当前请求次数统计"""
        stats = self.get_request_stats()
        items = sorted(((k, v) for k, v in stats.items() if k != "total"), key=lambda x: -x[1])
        msg = "115 API 请求统计：total={total} | ".format(total=stats.get("total", 0)) + ", ".join([f"{k}={v}" for k, v in items[:12]])
        getattr(logger, level, logger.info)(msg)

    def _inc_req(self, api_name: str) -> None:
        with self._req_count_lock:
            self._req_count[api_name] += 1
            self._req_total += 1
            if self._log_req_every > 0 and (self._req_total % self._log_req_every == 0):
                # 定期输出一次，避免刷屏
                self.log_request_stats(level="info")

    # ----------------------------
    # 内部：统一请求入口（节流 + 退避重试 + 计数）
    # ----------------------------

    def _call(
        self,
        api_name: str,
        fn: Callable,
        *args,
        max_retry: int = 5,
        base_delay: float = 0.8,
        must_check_state: bool = False,
        **kwargs
    ):
        limiter = self._limiters.get(api_name)
        last_exc = None

        for i in range(max_retry + 1):
            try:
                if limiter:
                    limiter.wait()

                # 计数：只统计“实际发出”的调用次数
                self._inc_req(api_name)
                resp = fn(*args, **kwargs)

                if must_check_state and isinstance(resp, dict):
                    if not resp.get("state", False) and _is_risk_control_error(resp):
                        raise RuntimeError(f"{api_name} risk-control resp: {resp}")
                return resp

            except Exception as e:
                last_exc = e
                if i >= max_retry:
                    raise

                if not _is_risk_control_error(e):
                    raise

                delay = base_delay * (2 ** i) + random.uniform(0.0, 0.65)
                logger.warning(f"{api_name} 疑似风控/限频，退避 {delay:.2f}s 重试({i+1}/{max_retry})：{_stringify_exc(e)}")
                time.sleep(delay)

        if last_exc:
            raise last_exc

    # ----------------------------
    # 对外方法（签名/返回兼容）
    # ----------------------------

    def check_login(self) -> bool:
        if not self.client:
            return False
        try:
            user_info = self._call("user_my_info", self.client.user_my_info, max_retry=3, base_delay=0.8)
            if isinstance(user_info, dict) and user_info.get("state"):
                uname = user_info.get("data", {}).get("uname", "未知")
                logger.info(f"115 登录成功: {uname}")
                return True
            return False
        except Exception as e:
            logger.error(f"检查 115 登录状态失败: {e}")
            return False

    def _dir_getid_singleflight(self, path: str) -> Dict[str, Any]:
        """
        对 fs_dir_getid(path) 做 singleflight 合并。
        """
        key = f"dir_getid:{path}"
        return self._sf_dir_getid.do(
            key,
            lambda: self._call("dir_getid", self.client.fs_dir_getid, path, max_retry=4, base_delay=0.9)
        )

    def get_pid_by_path(self, path: str, mkdir: bool = True) -> int:
        if not self.client:
            return -1

        path = (path or "").replace("\\", "/")
        if not path.startswith("/"):
            path = "/" + path
        path = path.rstrip("/")

        if not path or path == "/":
            return 0

        if path in self.path_cache:
            return self.path_cache[path]

        hit, cached = self._pid_cache.get(path)
        if hit:
            if isinstance(cached, int) and cached >= 0:
                self.path_cache[path] = cached
                return cached
            if not mkdir and cached == -1:
                return -1

        # 同一路径串行化（创建目录时尤其重要）
        lock = self._locks[f"pid:{path}"]
        with lock:
            if path in self.path_cache:
                return self.path_cache[path]

            hit, cached = self._pid_cache.get(path)
            if hit:
                if isinstance(cached, int) and cached >= 0:
                    self.path_cache[path] = cached
                    return cached
                if not mkdir and cached == -1:
                    return -1

            # 1) singleflight 的 getid
            try:
                resp = self._dir_getid_singleflight(path)
                if isinstance(resp, dict) and resp.get("id"):
                    cid = int(resp["id"])
                    self.path_cache[path] = cid
                    self._pid_cache.set(path, cid, ttl_sec=2 * 3600)
                    return cid
            except Exception:
                pass

            if not mkdir:
                self._pid_cache.set(path, -1, ttl_sec=120.0)
                return -1

            # 2) 逐级创建/查找（每级也用 singleflight）
            parent_id = 0
            current_path = ""
            parts = [p for p in path.split("/") if p]

            for part in parts:
                current_path = f"{current_path}/{part}"

                if current_path in self.path_cache:
                    parent_id = self.path_cache[current_path]
                    continue

                hit, cached = self._pid_cache.get(current_path)
                if hit and isinstance(cached, int) and cached >= 0:
                    self.path_cache[current_path] = cached
                    parent_id = cached
                    continue

                got = False
                try:
                    resp = self._dir_getid_singleflight(current_path)
                    if isinstance(resp, dict) and resp.get("id"):
                        cid = int(resp["id"])
                        self.path_cache[current_path] = cid
                        self._pid_cache.set(current_path, cid, ttl_sec=2 * 3600)
                        parent_id = cid
                        got = True
                except Exception:
                    got = False

                if got:
                    continue

                # 创建目录（在 pid:{path} 的 lock 内，避免并发重复创建）
                try:
                    resp = self._call(
                        "fs_makedirs",
                        self.client.fs_makedirs_app,
                        part,
                        pid=parent_id,
                        max_retry=5,
                        base_delay=1.0,
                        must_check_state=True,
                    )
                    check_response(resp)
                    if resp.get("state"):
                        cid = int(resp["cid"])
                        self.path_cache[current_path] = cid
                        self._pid_cache.set(current_path, cid, ttl_sec=2 * 3600)
                        parent_id = cid
                        logger.info(f"创建目录成功: {current_path} -> {cid}")
                    else:
                        logger.error(f"创建目录失败 {current_path}: {resp.get('error')}")
                        self._pid_cache.set(current_path, -1, ttl_sec=120.0)
                        return -1
                except Exception as e:
                    logger.error(f"创建目录异常 {current_path}: {e}")
                    self._pid_cache.set(current_path, -1, ttl_sec=120.0)
                    return -1

            self.path_cache[path] = parent_id
            self._pid_cache.set(path, parent_id, ttl_sec=2 * 3600)
            return parent_id

    def extract_share_info(self, url: str) -> Dict[str, str]:
        if not P115_AVAILABLE:
            return {}
        try:
            payload = share_extract_payload(url)
            return {
                "share_code": payload.get("share_code", ""),
                "receive_code": payload.get("receive_code", "")
            }
        except Exception as e:
            logger.error(f"解析分享链接失败: {e}")
            return {}

    def list_share_files(self, share_url: str, cid: int = 0, max_depth: int = 3) -> List[dict]:
        if not self.client:
            return []

        info = self.extract_share_info(share_url)
        share_code = info.get("share_code")
        receive_code = info.get("receive_code")
        if not share_code or not receive_code:
            logger.error("无效的分享链接或解析失败")
            return []

        return self._list_share_files_recursive(share_code, receive_code, cid=cid, depth=1, max_depth=max_depth)

    def _list_share_files_recursive(self, share_code: str, receive_code: str, cid: int = 0, depth: int = 1, max_depth: int = 3) -> List[dict]:
        if depth > max_depth:
            return []

        files: List[dict] = []
        try:
            if self._limiters.get("share_iter"):
                self._limiters["share_iter"].wait()

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

                if file_info["is_dir"] and depth < max_depth:
                    sub_cid = int(item.get("id", 0))
                    file_info["children"] = self._list_share_files_recursive(
                        share_code, receive_code, cid=sub_cid, depth=depth + 1, max_depth=max_depth
                    )

                files.append(file_info)

        except Exception as e:
            if _is_risk_control_error(e):
                delay = 1.5 + random.uniform(0.0, 1.0)
                logger.warning(f"列出分享文件疑似风控，延迟 {delay:.2f}s 后放弃本层：{_stringify_exc(e)}")
                time.sleep(delay)
            else:
                logger.error(f"列出分享文件失败: {e}")

        return files

    def transfer_share(self, share_url: str, save_path: str) -> bool:
        if not self.client:
            return False

        info = self.extract_share_info(share_url)
        share_code = info.get("share_code")
        receive_code = info.get("receive_code")
        if not share_code or not receive_code:
            logger.error("无效的分享链接或解析失败")
            return False

        parent_id = self.get_pid_by_path(save_path, mkdir=True)
        if parent_id == -1:
            logger.error(f"无法获取或创建目标目录: {save_path}")
            return False

        logger.info(f"转存分享到目录 ID: {parent_id} ({save_path})")

        payload = {
            "share_code": share_code,
            "receive_code": receive_code,
            "file_id": "0",
            "cid": parent_id,
            "is_check": 0,
        }

        lock = self._locks[f"receive_all:{share_code}:{parent_id}"]
        with lock:
            try:
                resp = self._call(
                    "share_receive",
                    self.client.share_receive,
                    payload,
                    max_retry=6,
                    base_delay=1.2,
                    must_check_state=True,
                )
                if resp.get("state"):
                    logger.info(f"转存成功！已保存到: {save_path}")
                    return True
                error_msg = resp.get("error", "未知错误")
                if "重复" in str(error_msg) or "已存在" in str(error_msg):
                    logger.info("转存内容疑似已存在，视为成功")
                    return True
                logger.error(f"转存失败: {error_msg}")
                return False
            except Exception as e:
                logger.error(f"转存过程中发生异常: {e}")
                return False

    def transfer_file(self, share_url: str, file_id: str, save_path: str) -> bool:
        if not self.client:
            return False

        info = self.extract_share_info(share_url)
        share_code = info.get("share_code")
        receive_code = info.get("receive_code")
        if not share_code or not receive_code:
            logger.error("无效的分享链接或解析失败")
            return False

        parent_id = self.get_pid_by_path(save_path, mkdir=True)
        if parent_id == -1:
            logger.error(f"无法获取或创建目标目录: {save_path}")
            return False

        payload = {
            "share_code": share_code,
            "receive_code": receive_code,
            "file_id": file_id,
            "cid": parent_id,
            "is_check": 0,
        }

        lock = self._locks[f"receive_one:{share_code}:{file_id}:{parent_id}"]
        with lock:
            try:
                resp = self._call(
                    "share_receive",
                    self.client.share_receive,
                    payload,
                    max_retry=6,
                    base_delay=1.2,
                    must_check_state=True,
                )
                if resp.get("state"):
                    logger.info(f"文件转存成功！文件ID: {file_id}, 保存到: {save_path}")
                    return True

                error_msg = resp.get("error", "未知错误")
                if "重复" in str(error_msg) or "已存在" in str(error_msg):
                    logger.info(f"文件已存在，跳过: {file_id}")
                    return True

                logger.error(f"文件转存失败: {error_msg}")
                return False
            except Exception as e:
                logger.error(f"文件转存过程中发生异常: {e}")
                return False

    def list_files(self, path: str) -> List[dict]:
        if not self.client:
            return []

        cid = self.get_pid_by_path(path, mkdir=False)
        if cid == -1:
            return []

        cache_key = f"files:{cid}"
        hit, cached = self._files_cache.get(cache_key)
        if hit and isinstance(cached, list):
            return cached

        try:
            all_data: List[dict] = []
            limit = 500
            offset = 0
            max_pages = 20

            for _ in range(max_pages):
                params = {"cid": cid, "limit": limit, "offset": offset}
                resp = self._call("fs_files", self.client.fs_files, params, max_retry=4, base_delay=1.0, must_check_state=True)
                if not (isinstance(resp, dict) and resp.get("state")):
                    break
                data = resp.get("data", []) or []
                if not isinstance(data, list):
                    break
                all_data.extend(data)
                if len(data) < limit:
                    break
                offset += limit

            if not all_data:
                resp = self._call("fs_files", self.client.fs_files, {"cid": cid, "limit": 1000}, max_retry=4, base_delay=1.0, must_check_state=True)
                if isinstance(resp, dict) and resp.get("state"):
                    all_data = resp.get("data", []) or []

            if isinstance(all_data, list):
                self._files_cache.set(cache_key, all_data, ttl_sec=15.0)
                return all_data
            return []
        except Exception as e:
            logger.error(f"列出文件失败: {e}")
            return []

    def list_directories(self, path: str) -> List[dict]:
        files = self.list_files(path)
        directories: List[dict] = []
        for f in files:
            if f.get("fid") == 0:
                dir_name = f.get("name", "")
                dir_path = f"{path.rstrip('/')}/{dir_name}" if path != "/" else f"/{dir_name}"
                directories.append({"name": dir_name, "path": dir_path, "cid": f.get("cid", 0)})
        return directories
