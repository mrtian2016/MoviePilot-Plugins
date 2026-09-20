"""
工具函数模块
包含不涉及业务逻辑的通用工具函数
"""
import base64
import datetime
import json
import os
import platform
import urllib.request
import urllib.error
from pathlib import Path
from typing import Optional, Any, List, Dict, Tuple

from app.core.config import settings
from app.log import logger


def _parse_proxy_url(proxy) -> Optional[Dict[str, str]]:
    """
    解析代理URL，支持 http://user:password@ip:port 格式
    
    :param proxy: 代理配置，可以是字符串或字典
    :return: Playwright 格式的代理配置 {"server": "...", "username": "...", "password": "..."}
    """
    if not proxy:
        return None
    
    # 如果是字典格式，取 http 或 https
    if isinstance(proxy, dict):
        proxy_url = proxy.get("http") or proxy.get("https")
    else:
        proxy_url = str(proxy)
    
    if not proxy_url:
        return None
    
    try:
        from urllib.parse import urlparse
        parsed = urlparse(proxy_url)
        
        # 构建不带认证的服务器地址
        if parsed.port:
            server = f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"
        else:
            server = f"{parsed.scheme}://{parsed.hostname}"
        
        result = {"server": server}
        
        # 如果有用户名和密码
        if parsed.username:
            result["username"] = parsed.username
        if parsed.password:
            result["password"] = parsed.password
        
        return result
    except Exception as e:
        logger.debug(f"解析代理URL失败: {e}，将直接使用原始URL")
        return {"server": proxy_url}




def convert_nullbr_to_pansou_format(nullbr_resources: List[Dict]) -> List[Dict]:
    """
    将 Nullbr 资源格式转换为统一的资源格式

    Nullbr 格式: {"title": "...", "share_link": "...", "size": "...", "resolution": "...", "season_list": [...]}
    统一格式: {"url": "...", "title": "...", "update_time": ""}

    :param nullbr_resources: Nullbr 返回的资源列表
    :return: 统一格式的资源列表
    """
    converted = []
    for resource in nullbr_resources:
        converted.append({
            "url": resource.get("share_link", ""),
            "title": resource.get("title", ""),
            "update_time": ""  # Nullbr 没有更新时间字段
        })
    return converted




class SimpleTTLCache:
    """极简线程安全 TTL 缓存。

    v1.8.0 新增，用于 Dian115 已解锁链接的幂等复用：
    同一轮同步任务内，同一个分享不允许被重复解锁扣分。

    刻意不引入 MoviePilot 内部的缓存组件，保证插件可独立测试。
    """

    def __init__(self, ttl: int = 3600, maxsize: int = 512):
        import threading
        import time
        self._ttl = max(1, int(ttl or 3600))
        self._maxsize = max(1, int(maxsize or 512))
        self._lock = threading.RLock()
        self._store: Dict[str, Tuple[float, Any]] = {}
        self._time = time.monotonic

    def _purge_locked(self) -> None:
        now = self._time()
        expired = [k for k, (ts, _) in self._store.items() if now - ts >= self._ttl]
        for key in expired:
            self._store.pop(key, None)

    def get(self, key: Any, default: Any = None) -> Any:
        with self._lock:
            item = self._store.get(str(key))
            if not item:
                return default
            ts, value = item
            if self._time() - ts >= self._ttl:
                self._store.pop(str(key), None)
                return default
            return value

    def set(self, key: Any, value: Any) -> None:
        with self._lock:
            self._purge_locked()
            if len(self._store) >= self._maxsize:
                oldest = min(self._store.items(), key=lambda kv: kv[1][0])[0]
                self._store.pop(oldest, None)
            self._store[str(key)] = (self._time(), value)

    def pop(self, key: Any, default: Any = None) -> Any:
        with self._lock:
            item = self._store.pop(str(key), None)
            return item[1] if item else default

    def clear(self) -> None:
        with self._lock:
            self._store.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._store)
