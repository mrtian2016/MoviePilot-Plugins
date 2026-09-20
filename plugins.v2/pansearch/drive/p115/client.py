"""
115网盘客户端封装
"""
import copy
import hashlib
import threading
from base64 import b64encode
from inspect import signature as inspect_signature
from io import BytesIO
from typing import Optional, List, Dict, Any, Callable

import qrcode
from app.log import logger

from .files import P115FileService
from .offline import OfflineDownloadService
from .share import ShareService
from .upload import P115UploadService
from ..common import DriveRateLimiter, create_directory_path_cache
from ...core import get_component, resolve_component
from ...utils.cache import create_platform_ttl_cache

try:
    from p115client import P115Client, check_response
    from p115client.const import APP_TO_SSOENT

    PAVAILABLE = True
except ImportError:
    PAVAILABLE = False
    logger.warning("p115client 未安装，115网盘功能不可用，请安装: pip install p115client")

IOS_USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5_1 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 "
    "115wangpan_ios/36.2.20"
)


def _accepts_extra_kwargs(func: Callable) -> bool:
    try:
        return any(
            parameter.kind == parameter.VAR_KEYWORD
            for parameter in inspect_signature(func).parameters.values()
        )
    except (TypeError, ValueError):
        return False


class P115ClientWithTimeout(P115Client if PAVAILABLE else object):
    """参考 p115disk，为 p115client API 统一注入连接和读取超时。"""

    SLOW_METHODS = {
        "share_receive",
        "share_snap",
        "clouddownload_task_add_urls",
        "upload_file",
    }
    NO_TIMEOUT_METHODS = {
        "login_qrcode",
        "login_qrcode_token",
        "login_qrcode_scan_status",
        "login_qrcode_scan_result",
    }

    def __init__(
            self,
            cookies: str,
            default_timeout: Optional[Dict[str, float]] = None,
            slow_timeout: Optional[Dict[str, float]] = None,
    ):
        super().__init__(cookies)
        self._default_timeout = default_timeout
        self._slow_timeout = slow_timeout

    def __getattribute__(self, name: str):
        if name.startswith("_") or name in {"__class__", "__dict__"}:
            return object.__getattribute__(self, name)
        attr = object.__getattribute__(self, name)
        if name in object.__getattribute__(self, "NO_TIMEOUT_METHODS"):
            return attr
        if not callable(attr) or not _accepts_extra_kwargs(attr):
            return attr
        try:
            timeout = (
                object.__getattribute__(self, "_slow_timeout")
                if name in object.__getattribute__(self, "SLOW_METHODS")
                else object.__getattribute__(self, "_default_timeout")
            )
        except AttributeError:
            return attr

        def wrapper(*args, **kwargs):
            if timeout:
                extensions = kwargs.get("extensions")
                if not isinstance(extensions, dict):
                    extensions = {}
                    kwargs["extensions"] = extensions
                extensions.setdefault("timeout", timeout)
            return attr(*args, **kwargs)

        return wrapper


_COMPONENT_TYPES = (
    ShareService, OfflineDownloadService, P115FileService, P115UploadService
)


class P115ClientManager:
    """115网盘客户端管理器"""

    # 默认配置常量
    DEFAULT_MIN_INTERVAL = 1.5  # API 请求基础间隔（秒），实际会有 ±30% 随机浮动
    DEFAULT_PATH_CACHE_TTL = 3600  # 路径缓存过期时间（秒）
    DEFAULT_MAX_RETRIES = 3  # 最大重试次数
    DEFAULT_JITTER_RATIO = 0.3  # 请求间隔随机抖动比例（±30%）
    _login_rate_limiter = DriveRateLimiter(min_interval=0.8)
    SHARE_TRANSFER_PAGE_SIZE = 115  # 分享接收接口单页文件数
    OFFLINE_TASK_CACHE_TTL = 600  # 115 离线任务接口最短刷新间隔（秒）

    QR_CLIENT_TYPES = {
        "web": "网页",
        "tv": "TV",
        "115ios": "苹果",
        "115android": "安卓",
        "115ipad": "平板",
        "os_windows": "Windows",
        "os_mac": "macOS",
        "os_linux": "Linux",
        "wechatmini": "微信",
        "alipaymini": "支付宝",
        "harmony": "鸿蒙",
    }

    def _get_component(self, component_type):
        return get_component(self, component_type, "_client_components")

    def __getattr__(self, name):
        return resolve_component(self, _COMPONENT_TYPES, name, "_client_components")

    @classmethod
    def normalize_qrcode_client_type(cls, client_type: Any) -> str:
        normalized = str(client_type or "").strip().lower()
        if normalized in cls.QR_CLIENT_TYPES and normalized in APP_TO_SSOENT:
            return normalized
        return "alipaymini"

    @classmethod
    def create_qrcode_login(cls, client_type: str = "alipaymini") -> Dict[str, Any]:
        """创建指定渠道的115扫码登录会话。"""
        if not PAVAILABLE:
            raise RuntimeError("p115client 未安装")
        final_client_type = cls.normalize_qrcode_client_type(client_type)
        response = cls._login_rate_limiter.call(P115Client.login_qrcode_token)
        check_response(response)
        data = response.get("data") or {}
        uid = str(data.get("uid") or "")
        qrcode_time = str(data.get("time") or "")
        sign = str(data.get("sign") or "")
        if not uid or not qrcode_time or not sign:
            raise RuntimeError("115返回的二维码登录参数不完整")
        # login_qrcode 图片接口因客户端渠道变化而返回 405 或非图片内容。
        qrcode_content = str(data.get("qrcode") or "").strip()
        if not qrcode_content:
            qrcode_content = f"https://115.com/scan/dg-{uid}"
        image = qrcode.make(qrcode_content)
        output = BytesIO()
        image.save(output, format="PNG")
        return {
            "uid": uid,
            "time": qrcode_time,
            "sign": sign,
            "client_type": final_client_type,
            "channel_name": cls.QR_CLIENT_TYPES[final_client_type],
            "qrcode": (
                "data:image/png;base64,"
                f"{b64encode(output.getvalue()).decode('ascii')}"
            ),
        }

    @classmethod
    def check_qrcode_login(
            cls,
            uid: str,
            qrcode_time: str,
            sign: str,
            client_type: str = "alipaymini",
    ) -> Dict[str, Any]:
        """检查扫码状态，确认后返回对应渠道 Cookie。"""
        if not PAVAILABLE:
            raise RuntimeError("p115client 未安装")
        if not uid or not qrcode_time or not sign:
            raise ValueError("二维码登录参数不完整")
        final_client_type = cls.normalize_qrcode_client_type(client_type)
        response = cls._login_rate_limiter.call(
            P115Client.login_qrcode_scan_status,
            {"uid": uid, "time": qrcode_time, "sign": sign},
        )
        check_response(response)
        status_code = (response.get("data") or {}).get("status")
        if status_code == 0 or status_code is None:
            return {"status": "waiting", "message": "等待扫码"}
        if status_code == 1:
            return {"status": "scanned", "message": "已扫码，等待确认"}
        if status_code == -1:
            return {"status": "expired", "message": "二维码已过期"}
        if status_code == -2:
            return {"status": "cancelled", "message": "用户取消登录"}
        if status_code != 2:
            return {"status": "unknown", "message": f"未知二维码状态：{status_code}"}

        result = cls._login_rate_limiter.call(
            P115Client.login_qrcode_scan_result,
            uid,
            app=final_client_type,
        )
        check_response(result)
        cookie_data = (result.get("data") or {}).get("cookie")
        if not isinstance(cookie_data, dict):
            raise RuntimeError("登录成功但115未返回 Cookie")
        cookie = "; ".join(
            f"{name}={value}" for name, value in cookie_data.items() if name and value
        ).strip()
        required_keys = {"UID", "CID", "SEID"}
        cookie_keys = {
            part.split("=", 1)[0].strip()
            for part in cookie.split(";") if "=" in part
        }
        if not cookie or not required_keys.issubset(cookie_keys):
            raise RuntimeError("115返回的 Cookie 缺少 UID、CID 或 SEID")
        return {
            "status": "success",
            "message": f"{cls.QR_CLIENT_TYPES[final_client_type]}渠道登录成功",
            "cookie": cookie,
            "client_type": final_client_type,
        }

    def __init__(
            self,
            cookies: str,
            user_agent: str = None,
            min_interval: float = None,
            path_cache_ttl: int = None,
            share_cache_ttl_minutes: int = 30,
            timeout_enabled: bool = True,
            default_timeout: Optional[Dict[str, float]] = None,
            slow_timeout: Optional[Dict[str, float]] = None,
    ):
        """
        初始化115客户端

        :param cookies: 115 Cookie
        :param user_agent: User-Agent
        :param min_interval: API 请求最小间隔（秒），默认 1.5
        :param path_cache_ttl: 路径缓存过期时间（秒），默认 3600
        """
        self.cookies = cookies
        cache_scope = hashlib.sha1(cookies.encode("utf-8")).hexdigest()[:12]
        self.user_agent = user_agent or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        self.client: Optional[Any] = None
        self._login_checked = False
        self._login_check_lock = threading.Lock()
        self.is_vip = False
        self.vip_expire_date = ""

        # 速率限制
        _min_interval = min_interval if min_interval is not None else self.DEFAULT_MIN_INTERVAL
        self.rate_limiter = DriveRateLimiter.shared(
            "p115", cookies, min_interval=_min_interval
        )

        # 路径缓存（带 TTL）
        _path_cache_ttl = path_cache_ttl if path_cache_ttl is not None else self.DEFAULT_PATH_CACHE_TTL
        self.path_cache = create_directory_path_cache(
            "p115",
            cache_scope,
            0,
            ttl=_path_cache_ttl,
        )

        # 分享信息缓存（URL -> {share_code, receive_code}）
        self._share_info_cache_limit = 500
        self._share_cache_ttl = max(60, int(share_cache_ttl_minutes or 30) * 60)
        self._share_status_cache_limit = 500
        self._share_file_cache_limit = 300
        self._share_info_cache = create_platform_ttl_cache(
            "p115:share_info",
            cache_scope,
            maxsize=self._share_info_cache_limit,
            ttl=self._share_cache_ttl,
        )
        self._share_status_cache = create_platform_ttl_cache(
            "p115:share_status",
            cache_scope,
            maxsize=self._share_status_cache_limit,
            ttl=self._share_cache_ttl,
        )
        self._share_file_cache = create_platform_ttl_cache(
            "p115:share_files",
            cache_scope,
            maxsize=self._share_file_cache_limit,
            ttl=max(self._share_cache_ttl, 24 * 60 * 60),
        )
        self._share_cache_lock = threading.RLock()
        self._offline_task_cache: List[Dict[str, Any]] = []
        self._offline_task_cache_time = 0.0
        self._offline_task_status: Dict[str, str] = {}
        self._offline_task_refresh_ok = False
        self._offline_task_lock = threading.RLock()
        self._offline_task_condition = threading.Condition(self._offline_task_lock)
        self._offline_task_refreshing = False
        self._offline_task_cache_revision = 0
        self._offline_quota_cache: Dict[str, Any] = {}
        self._offline_quota_cache_time = 0.0
        self._offline_quota_refreshing = False
        self._target_file_cache = create_platform_ttl_cache(
            "p115:target_files",
            cache_scope,
            maxsize=200,
            ttl=60,
        )
        self._account_info_cache = create_platform_ttl_cache(
            "p115:account_info",
            cache_scope,
            maxsize=2,
            ttl=3600,
        )
        self._account_info_lock = threading.Lock()

        if PAVAILABLE and cookies:
            try:
                if timeout_enabled and default_timeout is None:
                    default_timeout = {
                        "connect": 30,
                        "pool": 15,
                        "read": 60,
                        "write": 60,
                    }
                if timeout_enabled and slow_timeout is None:
                    slow_timeout = {
                        "connect": 30,
                        "pool": 15,
                        "read": 300,
                        "write": 300,
                    }
                self.client = P115ClientWithTimeout(
                    cookies,
                    default_timeout=default_timeout if timeout_enabled else None,
                    slow_timeout=slow_timeout if timeout_enabled else None,
                )
            except Exception as e:
                logger.error(f"初始化 P115Client 失败: {e}")

    def _rate_limited_call(self, func: Callable, *args, **kwargs):
        """
        带速率限制的 API 调用封装

        :param func: 要调用的函数
        :return: 函数返回值
        """
        return self.rate_limiter.call(func, *args, **kwargs)

    def close(self) -> None:
        pass

    @staticmethod
    def _ios_request_kwargs(app: bool = True) -> Dict[str, Any]:
        kwargs: Dict[str, Any] = {"headers": {"user-agent": IOS_USER_AGENT}}
        if app:
            kwargs["app"] = "ios"
        return kwargs

    @staticmethod
    def _as_bool(value: Any) -> bool:
        """兼容 115 接口可能返回的布尔、数字和字符串标记。"""
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    @staticmethod
    def _http_status_code(error: Exception) -> Optional[int]:
        """尽力提取 HTTP 状态码；诊断代码自身不得遮蔽原始异常。"""
        response = getattr(error, "response", None)
        candidates = (
            getattr(error, "status_code", None),
            getattr(response, "status_code", None),
        )
        for value in candidates:
            try:
                return int(value) if value not in (None, "") else None
            except (TypeError, ValueError):
                continue
        return None

    def _login_ssoent(self) -> str:
        """返回当前客户端渠道标识，不读取或输出 Cookie 内容。"""
        client = self.client
        for name in ("ssoent", "login_ssoent", "app"):
            try:
                value = getattr(client, name, None)
            except Exception:
                continue
            if value not in (None, "") and not callable(value):
                return str(value)
        return str(APP_TO_SSOENT.get("ios") or "ios") if PAVAILABLE else "unknown"

    @staticmethod
    def _error_summary(error: Exception) -> str:
        """生成有界错误摘要，兼容 p115client 的字典型异常参数。"""
        try:
            message = str(error).strip()
        except Exception:
            message = ""
        name = type(error).__name__
        summary = f"{name}: {message}" if message else name
        return summary[:500]

    def check_login(self) -> bool:
        """检查登录状态，并缓存会员状态供离线下载判断。"""
        if self._login_checked:
            return True
        with self._login_check_lock:
            if self._login_checked:
                return True
            self.is_vip = False
            self.vip_expire_date = ""
            if not self.client:
                return False

            try:
                user_info = self._rate_limited_call(self.client.user_my_info)
                if user_info.get("state"):
                    data = user_info.get("data") or {}
                    vip_data = data.get("vip") or {}
                    uname = data.get("uname", "未知")
                    self.is_vip = (
                            self._as_bool(vip_data.get("is_vip"))
                            or self._as_bool(vip_data.get("is_forever"))
                    )
                    self.vip_expire_date = "永久" if self._as_bool(vip_data.get("is_forever")) \
                        else str(vip_data.get("expire_str") or "")
                    vip_text = "会员" if self.is_vip else "非会员"
                    if self.is_vip and self.vip_expire_date:
                        vip_text = f"{vip_text}（有效期：{self.vip_expire_date}）"
                    self._login_checked = True
                    logger.info(f"115 登录成功: {uname}，会员状态: {vip_text}")
                    return True
                logger.error(
                    f"115 登录状态无效：ssoent={self._login_ssoent()}，"
                    f"{user_info.get('error') or user_info.get('message') or '接口未返回原因'}"
                )
            except Exception as e:
                logger.error(
                    f"检查 115 登录状态失败："
                    f"HTTP={self._http_status_code(e) or 'unknown'}，"
                    f"ssoent={self._login_ssoent()}，{self._error_summary(e)}"
                )
            return False

    def get_account_info(self, cache_ttl: int = 3600) -> Dict[str, Any]:
        """返回配置页展示所需的脱敏账号和容量信息。"""
        if not self.client:
            return {"connected": False, "error": "115客户端未连接"}

        cached = self._account_info_cache.get("account") if cache_ttl > 0 else None
        if isinstance(cached, dict):
            return copy.deepcopy(cached)
        with self._account_info_lock:
            cached = self._account_info_cache.get("account") if cache_ttl > 0 else None
            if isinstance(cached, dict):
                return copy.deepcopy(cached)

            try:
                user_response = self._rate_limited_call(self.client.user_my_info)
                check_response(user_response)
                user_data = user_response.get("data") or {}
                vip_data = user_data.get("vip") or {}
                face_data = user_data.get("face") or {}

                space_data = {}
                try:
                    space_response = self._rate_limited_call(
                        self.client.fs_index_info, payload=0
                    )
                    check_response(space_response)
                    space_data = (
                            (space_response.get("data") or {}).get("space_info") or {}
                    )
                except Exception as error:
                    logger.warning(
                        f"获取115空间信息失败但登录仍有效："
                        f"HTTP={self._http_status_code(error) or 'unknown'}，"
                        f"ssoent={self._login_ssoent()}，{self._error_summary(error)}"
                    )

                is_forever = self._as_bool(vip_data.get("is_forever"))
                account_info = {
                    "connected": True,
                    "user": {
                        "name": str(user_data.get("uname") or "未知用户"),
                        "avatar": str(face_data.get("face_s") or ""),
                        "is_vip": self._as_bool(vip_data.get("is_vip")) or is_forever,
                        "is_forever_vip": is_forever,
                        "vip_expire_date": (
                            "永久" if is_forever
                            else str(vip_data.get("expire_str") or "")
                        ),
                    },
                    "storage": {
                        "total": str(
                            (space_data.get("all_total") or {}).get("size_format") or ""
                        ),
                        "used": str(
                            (space_data.get("all_use") or {}).get("size_format") or ""
                        ),
                        "remaining": str(
                            (space_data.get("all_remain") or {}).get("size_format") or ""
                        ),
                    },
                }
            except Exception as error:
                logger.debug(f"获取115账号信息失败：{self._error_summary(error)}")
                account_info = {
                    "connected": False,
                    "error": "账号信息不可用，请检查 Cookie 后重试",
                }

            if cache_ttl > 0:
                self._account_info_cache.set(
                    "account",
                    account_info,
                    ttl=max(1, int(cache_ttl)),
                )
            return copy.deepcopy(account_info)

    def get_cache_stats(self) -> Dict[str, Any]:
        """返回各类115缓存的实时占用，不触发远端请求。"""
        with self._share_cache_lock:
            share_info = len(self._share_info_cache)
            share_status = len(self._share_status_cache)
            share_files = len(self._share_file_cache)
        target_files = len(list(self._target_file_cache.items()))
        with self._offline_task_lock:
            offline_tasks = len(self._offline_task_cache)
        return {
            "path": self.path_cache.stats(),
            "share_info": share_info,
            "share_status": share_status,
            "share_files": share_files,
            "target_files": target_files,
            "offline_tasks": offline_tasks,
        }

    def clear_cache(self) -> Dict[str, int]:
        """清空当前提供方的可重建缓存。"""
        counts = self.clear_share_cache()
        path_count = int(self.path_cache.stats().get("entries") or 0)
        self.clear_path_cache()
        target_count = len(list(self._target_file_cache.items()))
        self._target_file_cache.clear()
        with self._offline_task_lock:
            offline_task_count = len(self._offline_task_cache)
            offline_quota_count = bool(self._offline_quota_cache)
            self._offline_task_cache = []
            self._offline_task_cache_time = 0.0
            self._offline_task_status = {}
            self._offline_task_refresh_ok = False
            self._offline_task_cache_revision += 1
            self._offline_quota_cache = {}
            self._offline_quota_cache_time = 0.0
        counts.update({
            "path": path_count,
            "target_files": target_count,
            "offline_tasks": offline_task_count,
            "offline_quota": int(offline_quota_count),
        })
        return counts
