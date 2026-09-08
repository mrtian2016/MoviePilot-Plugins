"""
PanSou 网盘搜索客户端
用于搜索各类网盘资源
"""
import json
import threading
import time
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional

from app.log import logger

from ..http_client import RequestGate, gated_request, normalize_proxies, requests
from ..types import PANSOU_RESOURCE_TYPES


class PanSouClient:
    """网盘搜索客户端"""

    MAX_RAW_RESULTS = 100
    _SESSION_DATA_KEY = "pansou_auth_session"
    _LOGIN_LOCK = threading.RLock()

    @staticmethod
    def _sanitize_json_strings(value: Any) -> Any:
        if isinstance(value, str):
            return value.encode("utf-8", errors="replace").decode("utf-8")
        if isinstance(value, list):
            return [PanSouClient._sanitize_json_strings(item) for item in value]
        if isinstance(value, dict):
            return {
                PanSouClient._sanitize_json_strings(key):
                    PanSouClient._sanitize_json_strings(item)
                for key, item in value.items()
            }
        return value

    @classmethod
    def _response_json(cls, response) -> Dict[str, Any]:
        """兼容 PanSou 个别结果中的非法 UTF-8 surrogate。"""
        try:
            payload = response.json()
        except (UnicodeError, ValueError):
            raw = getattr(response, "content", b"")
            if isinstance(raw, bytes):
                text = raw.decode("utf-8", errors="replace")
            else:
                text = str(raw).encode("utf-8", errors="replace").decode("utf-8")
            payload = json.loads(text)
        payload = cls._sanitize_json_strings(payload)
        if not isinstance(payload, dict):
            raise ValueError("响应不是 JSON 对象")
        return payload

    def __init__(
            self,
            base_url: str,
            username: str = "",
            password: str = "",
            auth_enabled: bool = True,
            proxy: str = None,
            search_timeout: int = 30,
            get_data_func: Optional[Callable] = None,
            save_data_func: Optional[Callable] = None,
    ):
        """
        初始化 PanSou 客户端

        :param base_url: API 基础地址
        :param username: 用户名
        :param password: 密码
        :param auth_enabled: 是否启用认证
        :param proxy: 代理地址，如 http://127.0.0.1:7890
        """
        self.base_url = base_url.rstrip("/") if base_url else ""
        self.username = username
        self.password = password
        self.auth_enabled = auth_enabled
        self.search_timeout = max(5, min(int(search_timeout or 30), 120))
        self._token: Optional[str] = None
        self._token_expires: Optional[datetime] = None
        self._get_data_func = get_data_func
        self._save_data_func = save_data_func
        # 代理设置（兼容字符串和字典格式）
        self._proxies = normalize_proxies(proxy)
        self._auth_lock = threading.RLock()
        self._request_gate = RequestGate.shared(
            "PanSou",
            f"{self.base_url}|{self.username.casefold()}|{self._proxies}",
            request_interval=0.2,
            minimum_interval=0.1,
        )
        self._restore_token()

    def _restore_token(self) -> None:
        if not self.auth_enabled or not self._get_data_func:
            return
        try:
            data = self._get_data_func(self._SESSION_DATA_KEY) or {}
            if (
                    not isinstance(data, dict)
                    or str(data.get("base_url") or "").rstrip("/") != self.base_url
                    or str(data.get("username") or "") != self.username
            ):
                return
            expires_at = float(data.get("expires_at") or 0)
            token = str(data.get("token") or "").strip()
            if token and expires_at > time.time() + 300:
                self._token = token
                self._token_expires = datetime.fromtimestamp(expires_at)
                logger.debug("PanSou 已恢复持久化登录状态")
        except Exception as error:
            logger.debug(f"PanSou 恢复持久化登录状态失败：{error}")

    def _save_token(self) -> None:
        if not self._save_data_func:
            return
        try:
            self._save_data_func(
                self._SESSION_DATA_KEY,
                {
                    "base_url": self.base_url,
                    "username": self.username,
                    "token": self._token,
                    "expires_at": self._token_expires.timestamp(),
                } if self._token and self._token_expires else {},
            )
        except Exception as error:
            logger.debug(f"PanSou 持久化登录状态失败：{error}")

    def _clear_token(self) -> None:
        self._token = None
        self._token_expires = None
        self._save_token()

    def _get_token(self) -> Optional[str]:
        with self._auth_lock:
            return self._get_token_locked()

    def _get_token_locked(self) -> Optional[str]:
        """获取或刷新 Token"""
        if not self.base_url:
            return None

        if not self.auth_enabled:
            return None

        if not all([self.username, self.password]):
            logger.warning("PanSou 认证已启用但未配置用户名密码")
            return None

        now = datetime.now()
        if self._token and self._token_expires:
            if now < self._token_expires - timedelta(minutes=5):
                return self._token

        with self._LOGIN_LOCK:
            self._restore_token()
            now = datetime.now()
            if self._token and self._token_expires:
                if now < self._token_expires - timedelta(minutes=5):
                    return self._token
            try:
                login_url = f"{self.base_url}/api/auth/login"
                response = gated_request(
                    self._request_gate,
                    requests.post,
                    login_url,
                    impersonate="chrome",
                    json={"username": self.username, "password": self.password},
                    timeout=10,
                    proxies=self._proxies,
                )
                if response.status_code != 200:
                    logger.error(f"PanSou 登录失败: HTTP {response.status_code}")
                    return None
                data = self._response_json(response)
                self._token = str(data.get("token") or "").strip() or None
                expires_at = data.get("expires_at")
                self._token_expires = (
                    datetime.fromtimestamp(float(expires_at))
                    if expires_at else now + timedelta(hours=24)
                )
                self._save_token()
                logger.debug("PanSou Token 获取成功")
                return self._token
            except Exception as error:
                logger.error(f"PanSou 登录失败: {error}")
                return None

    def health(self, timeout: int = 5) -> Dict[str, Any]:
        """读取 PanSou 公开健康信息，用于配置频道和插件选项。"""
        if not self.base_url:
            return {"status": "error", "error": "未配置 PanSou API 地址"}
        try:
            response = gated_request(
                self._request_gate,
                requests.get,
                f"{self.base_url}/api/health",
                impersonate="chrome",
                timeout=max(2, min(int(timeout or 5), 10)),
                proxies=self._proxies,
            )
            if response.status_code != 200:
                return {
                    "status": "error",
                    "error": f"健康检查失败: HTTP {response.status_code}",
                }
            payload = self._response_json(response)
            payload["plugins"] = list(dict.fromkeys(
                str(value).strip() for value in (payload.get("plugins") or [])
                if str(value or "").strip()
            ))
            payload["channels"] = list(dict.fromkeys(
                str(value).strip() for value in (payload.get("channels") or [])
                if str(value or "").strip()
            ))
            return payload
        except (requests.exceptions.RequestException, ValueError) as error:
            return {
                "status": "error",
                "error": f"健康检查失败: {type(error).__name__}",
            }

    def request_search(
            self,
            keyword: str,
            cloud_types: List[str] = None,
            channels: List[str] = None,
            plugins: List[str] = None,
            filter_config: Optional[Dict[str, List[str]]] = None,
            refresh: bool = False,
            concurrency: Optional[int] = None,
            response_mode: str = "results",
    ) -> Dict[str, Any]:
        """
        搜索网盘资源

        :param keyword: 搜索关键词
        :param cloud_types: 网盘类型列表；不传或为空时返回全部类型
        :param channels: TG搜索频道列表
        :param plugins: 插件列表；不传或为空时搜索全部插件
        :param filter_config: include/exclude 关键词过滤配置
        :param refresh: 是否绕过 PanSou 服务端缓存
        :param concurrency: PanSou 内部并发数；不传时由服务端自动设置
        :return: 搜索结果
        """
        if not keyword or not keyword.strip():
            return {
                "error": "搜索关键词不能为空",
                "keyword": keyword
            }

        keyword = keyword.strip()

        if not self.base_url:
            return {
                "error": "未配置 PanSou API 地址",
                "keyword": keyword
            }

        try:
            effective_concurrency = (
                min(max(int(concurrency), 1), 100) if concurrency else None
            )
        except (ValueError, TypeError):
            effective_concurrency = None
        requested_cloud_types = cloud_types or []
        effective_cloud_types = list(dict.fromkeys(
            str(item).strip().lower()
            for item in requested_cloud_types
            if str(item).strip().lower() in PANSOU_RESOURCE_TYPES
        ))

        try:
            headers = {"Content-Type": "application/json"}

            # 如果启用认证，获取 Token
            if self.auth_enabled:
                token = self._get_token()
                if not token:
                    return {
                        "error": "PanSou API 认证失败，请检查用户名和密码配置",
                        "keyword": keyword
                    }
                headers["Authorization"] = f"Bearer {token}"

            # 构建请求参数
            search_url = f"{self.base_url}/api/search"
            payload = {
                "kw": keyword,
                "refresh": bool(refresh),
                "res": "merge" if str(response_mode).strip().lower() == "merge" else "results",
                "src": "all",
            }
            if effective_concurrency:
                payload["conc"] = effective_concurrency
            if channels:
                payload["channels"] = channels

            if plugins:
                payload["plugins"] = plugins

            if effective_cloud_types:
                payload["cloud_types"] = effective_cloud_types

            normalized_filter = {
                key: list(dict.fromkeys(
                    str(value).strip()
                    for value in (filter_config or {}).get(key, [])
                    if str(value or "").strip()
                ))
                for key in ("include", "exclude")
            }
            normalized_filter = {
                key: values for key, values in normalized_filter.items() if values
            }
            if normalized_filter:
                payload["filter"] = normalized_filter

            logger.debug(f"PanSou 搜索: {payload}")
            started_at = time.monotonic()
            response = gated_request(
                self._request_gate,
                requests.post,
                search_url,
                impersonate="chrome",
                json=payload,
                headers=headers,
                timeout=self.search_timeout,
                proxies=self._proxies,
            )

            # Token 失效重试
            if response.status_code == 401 and self.auth_enabled:
                self._clear_token()

                token = self._get_token()
                if token:
                    headers["Authorization"] = f"Bearer {token}"
                    response = gated_request(
                        self._request_gate,
                        requests.post,
                        search_url,
                        impersonate="chrome",
                        json=payload,
                        headers=headers,
                        timeout=self.search_timeout,
                        proxies=self._proxies,
                    )

            if response.status_code != 200:
                return {
                    "error": f"搜索请求失败: HTTP {response.status_code}",
                    "keyword": keyword
                }

            resp_data = self._response_json(response)
            elapsed = time.monotonic() - started_at

            # 检查响应状态码
            if resp_data.get("code") != 0:
                return {
                    "error": resp_data.get("message", "搜索失败"),
                    "keyword": keyword
                }

            # 获取 data 字段
            data = resp_data.get("data", {})
            total = data.get("total", 0)
            merged_by_type = data.get("merged_by_type")
            results_list = data.get("results", [])
            if not isinstance(results_list, list):
                results_list = []
            if str(response_mode).strip().lower() == "merge" and isinstance(merged_by_type, dict):
                # merge 响应按网盘分类轮询，避免磁力等大分类占满总上限。
                typed_rows = {
                    resource_type: [item for item in values if isinstance(item, dict)]
                    for resource_type, values in merged_by_type.items()
                    if isinstance(values, list)
                }
                results_list = []
                while typed_rows and len(results_list) < self.MAX_RAW_RESULTS:
                    for resource_type in list(typed_rows):
                        values = typed_rows[resource_type]
                        if not values:
                            typed_rows.pop(resource_type, None)
                            continue
                        item = values.pop(0)
                        results_list.append({
                            "title": str(item.get("note") or "").strip(),
                            "datetime": item.get("datetime") or "",
                            "source": item.get("source") or "",
                            "links": [{
                                "url": item.get("url") or "",
                                "password": item.get("password") or "",
                                "work_title": item.get("note") or "",
                                "description": item.get("note") or "",
                                "type": resource_type,
                            }],
                        })
                        if len(results_list) >= self.MAX_RAW_RESULTS:
                            break
            raw_count = len(results_list)
            results_list = results_list[:self.MAX_RAW_RESULTS]
            processed_count = len(results_list)

            return {
                "keyword": keyword,
                "total": total,
                "raw_count": raw_count,
                "processed_count": processed_count,
                "elapsed_ms": int(elapsed * 1000),
                "results": results_list,
                "merged_by_type": merged_by_type if isinstance(merged_by_type, dict) else {},
            }

        except requests.exceptions.Timeout:
            logger.warning(
                f"PanSou 请求超时：关键词 '{keyword}'，超时 {self.search_timeout} 秒，"
                f"并发数 {effective_concurrency or '自动'}，"
                f"强制刷新={bool(refresh)}"
            )
            return {
                "error": "搜索请求超时，请稍后重试",
                "keyword": keyword
            }
        except Exception as e:
            logger.error(f"搜索网盘资源失败: {str(e)}")
            return {
                "error": f"搜索网盘资源失败: {str(e)}",
                "keyword": keyword
            }
