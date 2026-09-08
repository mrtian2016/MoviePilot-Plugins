"""搜索源测试与平台媒体候选查询 API。"""

import ast
import ipaddress
import re
import threading
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from app.core.metainfo import MetaInfo
from app.log import logger
from app.schemas import MediaInfo
from app.schemas.types import MediaType
from app.utils.string import StringUtils

from .page import _platform_image_url
from .. import OwnerDelegator, SearchCapability
from ..cloud import CloudDriveCapability
from ..config import UIConfig
from ..media import apply_media_identity, recognize_media
from ...search.hdhive import HDHIVE_DETAIL_RESOURCE_TYPES
from ...search.types import (
    PREVIEW_PROVIDER_KEYS,
    PREVIEW_RESOURCE_TYPES,
    RESOURCE_TYPE_PRIORITY,
    SUPPORTED_RESOURCE_TYPES,
    normalize_resource_type,
    resource_type_from_url,
    resource_type_name,
)
from ...utils import parse_magnet_metadata
from ...utils.http_client import (
    build_proxy_url,
    normalize_proxies,
    request_error_summary,
    requests,
    validate_proxy_address,
)


class SearchApi(OwnerDelegator):
    _PROXY_TEST_URL = "https://www.cloudflare.com/cdn-cgi/trace"
    _SEARCH_TEST_DISPLAY_LIMIT = 10
    _TEST_HDHIVE_CLIENT_LIMIT = 4
    _TEST_MEDIA_ID_FIELDS = (
        "tmdb_id", "imdb_id", "tvdb_id", "douban_id",
        "bangumi_id", "anilist_id",
    )

    @staticmethod
    def _display_size(item: Dict[str, Any]) -> Any:
        human = str(item.get("size_human") or "").strip()
        if human:
            return human
        value = item.get("size")
        if not isinstance(value, (int, float)) or value <= 0:
            return value or 0
        return StringUtils.format_size(int(value))

    @staticmethod
    def _sort_size(value: Any) -> float:
        """将候选资源大小转换为稳定的排序值，兼容带单位的文本。"""
        if isinstance(value, (int, float)):
            return float(value)
        text = str(value or "").strip().replace(",", "")
        if not text:
            return 0.0
        match = re.search(r"(\d+(?:\.\d+)?)\s*(B|KB|MB|GB|TB|PB)?", text, re.I)
        if not match:
            return 0.0
        number = float(match.group(1))
        unit = (match.group(2) or "B").upper()
        multipliers = {"B": 1, "KB": 1024, "MB": 1024 ** 2, "GB": 1024 ** 3, "TB": 1024 ** 4, "PB": 1024 ** 5}
        return number * multipliers.get(unit, 1)

    @staticmethod
    def _display_tags(item: Dict[str, Any]) -> List[str]:
        values = [item.get("tags") or []]
        values.extend(
            item.get(key)
            for key in (
                "resolution", "quality", "source_type", "codec",
                "audio_codec", "hdr_type", "subtitle",
            )
            if item.get(key)
        )

        tags: List[str] = []
        seen_tags = set()

        def append_tag(value: Any) -> None:
            if isinstance(value, dict):
                for nested in value.values():
                    append_tag(nested)
                return
            if isinstance(value, (list, tuple, set)):
                for nested in value:
                    append_tag(nested)
                return

            text = str(value or "").strip()
            if not text:
                return
            if text[:1] in ("[", "{") and text[-1:] in ("]", "}"):
                try:
                    parsed = ast.literal_eval(text)
                except (SyntaxError, ValueError):
                    parsed = None
                if isinstance(parsed, (dict, list, tuple, set)):
                    append_tag(parsed)
                    return
            if text in seen_tags:
                return
            seen_tags.add(text)
            tags.append(text)

        for value in values:
            append_tag(value)
        return tags
    _SEARCH_TEST_CONFIG_FIELDS = {
        "pansou": frozenset({
            "pansou_url", "pansou_username", "pansou_password",
            "pansou_auth_enabled", "pansou_channels", "pansou_plugins",
            "pansou_filter_include",
            "pansou_filter_exclude", "pansou_concurrency",
            "pansou_result_limit", "pansou_timeout",
        }),
        "piratebay": frozenset({
            "piratebay_base_url", "piratebay_result_limit", "piratebay_request_interval",
            "piratebay_timeout",
        }),
        "uindex": frozenset({
            "uindex_base_url", "uindex_result_limit", "uindex_request_interval",
            "uindex_timeout",
        }),
        "hdhive": frozenset({
            "hdhive_base_url",
            "hdhive_query_mode", "hdhive_api_key", "hdhive_client_id",
            "hdhive_access_token", "hdhive_refresh_token",
            "hdhive_token_expires_at", "hdhive_username", "hdhive_password",
            "hdhive_candidate_limit", "hdhive_request_interval",
            "hdhive_unlocks_per_minute", "hdhive_torrentclaw_enabled",
            "hdhive_torrentclaw_subtitle_languages",
        }),
        "dian115": frozenset({
            "dian115_base_url",
            "dian115_email", "dian115_password", "dian115_candidate_limit",
            "dian115_request_interval", "dian115_unlocks_per_minute",
        }),
        "juying": frozenset({
            "juying_base_url",
            "juying_username", "juying_password", "juying_result_limit",
            "juying_request_interval",
        }),
        "seedhub": frozenset({
            "seedhub_base_url", "seedhub_result_limit", "seedhub_request_interval",
            "seedhub_timeout",
        }),
        "pinglian": frozenset({
            "pinglian_username", "pinglian_password", "pinglian_result_limit",
            "pinglian_request_interval", "pinglian_timeout",
        }),
        "online_docs": frozenset({
            "online_docs", "online_docs_urls", "online_docs_resource_types"
        }),
    }

    def __init__(self, owner):
        super().__init__(owner)
        object.__setattr__(self, "_test_hdhive_clients_lock", threading.RLock())
        object.__setattr__(self, "_test_hdhive_clients", [])

    def close(self) -> None:
        """释放测试接口复用的 HDHive 认证连接。"""
        with self._test_hdhive_clients_lock:
            clients = list(self._test_hdhive_clients)
            self._test_hdhive_clients.clear()
        for client in clients:
            try:
                client.close()
            except Exception as error:
                logger.debug(f"关闭 HDHive 测试认证连接失败：{error}")

    def api_vue_test_search_proxy(self, payload: Dict[str, Any]) -> dict:
        """通过 Cloudflare Trace 测试搜索代理出口和请求延迟。"""
        payload = dict(payload or {})
        response = None
        try:
            proxy_address = validate_proxy_address(payload.get("proxy"))
            if not proxy_address:
                raise ValueError("请先填写搜索渠道代理地址")
            proxy = build_proxy_url(
                proxy_address,
                payload.get("username"),
                payload.get("password"),
            )
            started = time.perf_counter()
            response = requests.get(
                self._PROXY_TEST_URL,
                proxies=normalize_proxies(proxy),
                timeout=15,
                allow_redirects=True,
                impersonate="chrome",
            )
            latency_ms = max(0, round((time.perf_counter() - started) * 1000))
            if response.status_code != 200:
                raise RuntimeError(
                    f"Cloudflare Trace 返回 HTTP {response.status_code}"
                )
            trace = {}
            for line in str(response.text or "").splitlines():
                key, separator, value = line.partition("=")
                if separator and key:
                    trace[key.strip().lower()] = value.strip()
            ip_value = str(trace.get("ip") or "").strip()
            try:
                ipaddress.ip_address(ip_value)
            except ValueError as error:
                raise RuntimeError("Cloudflare Trace 未返回有效出口 IP") from error
            location = str(trace.get("loc") or "").strip().upper()
            colo = str(trace.get("colo") or "").strip().upper()
            return {
                "success": True,
                "message": "代理连接成功",
                "data": {
                    "latency_ms": latency_ms,
                    "ip": ip_value,
                    "loc": location if re.fullmatch(r"[A-Z]{2}", location) else "",
                    "colo": colo if re.fullmatch(r"[A-Z0-9-]{2,12}", colo) else "",
                },
            }
        except requests.exceptions.RequestException as error:
            return {
                "success": False,
                "message": f"代理测试失败：{request_error_summary(error)}",
            }
        except (ValueError, RuntimeError) as error:
            return {"success": False, "message": f"代理测试失败：{error}"}
        finally:
            if response is not None:
                response.close()

    def _get_test_hdhive_web_client(
            self,
            config: Dict[str, Any],
            proxy: Any,
    ) -> tuple:
        """按账号和网络配置复用测试连接及内存安全会话。"""
        from ...search.hdhive import HDHiveClient

        username = str(config.get("hdhive_username") or "")
        password = str(config.get("hdhive_password") or "")
        request_interval = float(
            config.get("hdhive_request_interval", 5) or 5
        )
        with self._test_hdhive_clients_lock:
            for client in self._test_hdhive_clients:
                if client.matches_config(
                        username,
                        password,
                        proxy,
                        request_interval,
                ):
                    return client, False
            client = HDHiveClient(
                username=username,
                password=password,
                proxy=proxy,
                request_interval=request_interval,
            )
            if len(self._test_hdhive_clients) >= self._TEST_HDHIVE_CLIENT_LIMIT:
                logger.debug(
                    "HDHive 测试连接配置超过缓存上限，本次使用临时认证连接"
                )
                return client, True
            self._test_hdhive_clients.append(client)
            logger.debug("HDHive 测试接口已建立可复用认证连接")
            return client, False

    @staticmethod
    def _preview_error_message(error: Exception) -> str:
        """优先返回第三方异常携带的结构化错误信息。"""
        for value in reversed(getattr(error, "args", ())):
            if not isinstance(value, dict):
                continue
            message = value.get("message") or value.get("msg") or value.get("error")
            if message:
                return str(message)
        return str(error) or error.__class__.__name__

    @staticmethod
    def _preview_file(item: Any) -> Dict[str, Any]:
        if not isinstance(item, dict):
            item = getattr(item, "__dict__", {}) or {}
        name = next((str(item.get(key) or "").strip() for key in
                     ("name", "path", "file_name", "filename", "fileName")
                     if item.get(key)), "")
        size = next((item.get(key) for key in
                     ("size", "file_size", "fileSize")
                     if item.get(key) is not None), 0)
        is_dir = bool(item.get("is_dir") or item.get("is_folder")
                      or item.get("isDirectory") or item.get("type") == "folder")
        file_id = next((str(item.get(key) or "").strip() for key in
                        ("id", "file_id", "fileId", "fid", "cid")
                        if item.get(key) is not None), "")
        return {
            "id": file_id,
            "name": name or "未命名文件",
            "size": size or 0,
            "is_dir": is_dir,
            "can_enter": bool(is_dir and file_id),
        }

    def api_vue_preview_search_resource(self, payload: Dict[str, Any]) -> dict:
        """只读获取测试资源的文件列表。"""
        payload = dict(payload or {})
        source = str(payload.get("source") or "").strip().lower()
        provider_data = (
            dict(payload.get("provider_data") or {})
            if isinstance(payload.get("provider_data"), dict) else {}
        )
        juying_resource_id = str(
            provider_data.get("resource_id") or ""
        ).strip()
        resource_type = normalize_resource_type(payload.get("resource_type"))
        url = str(payload.get("url") or "").strip()
        resource_ref = str(payload.get("resource_ref") or "").strip()
        is_unlocked = bool(payload.get("is_unlocked"))
        parent_id = str(payload.get("parent_id") or "").strip()
        pending_juying = source == "juying" and bool(juying_resource_id)
        pending_seedhub = (
                source == "seedhub"
                and not url
                and bool(payload.get("pending_resolution"))
                and bool(provider_data.get("kind"))
        )
        pending_pinglian = (
                source == "pinglian"
                and not url
                and bool(payload.get("pending_resolution"))
                and bool(provider_data.get("token"))
        )
        valid_hdhive_url = bool(
            url and "\\" not in url
            and resource_type_from_url(url) == resource_type
        )
        pending_hdhive = (
                source == "hdhive"
                and bool(resource_ref)
                and (not url or (is_unlocked and not valid_hdhive_url))
        )
        if (
                not url and not pending_juying and not pending_hdhive
                and not pending_seedhub and not pending_pinglian
        ) or len(url) > 8192:
            return {"success": False, "message": "资源链接无效"}
        if pending_juying and (
                len(juying_resource_id) > 32
                or not juying_resource_id.isdigit()
        ):
            return {"success": False, "message": "聚影资源标识无效"}
        if len(parent_id) > 256:
            return {"success": False, "message": "目录标识无效"}
        if (pending_seedhub or pending_pinglian) and parent_id:
            return {"success": False, "message": "待解析资源不支持目录导航"}
        if pending_hdhive and (
                resource_type not in HDHIVE_DETAIL_RESOURCE_TYPES
                or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", resource_ref)
        ):
            return {"success": False, "message": "HDHive 资源标识或类型无效"}
        try:
            if pending_seedhub or pending_pinglian:
                handler = self._build_test_search_handler(
                    source,
                    self._test_search_config(source, payload.get("config")),
                )
                try:
                    resolve_args = (
                        {
                            "kind": str(provider_data.get("kind") or ""),
                            "resource_type": resource_type,
                            "seed_id": str(provider_data.get("seed_id") or ""),
                            "path": str(provider_data.get("path") or ""),
                            "host": str(provider_data.get("host") or ""),
                        }
                        if pending_seedhub
                        else {
                            "token": str(provider_data.get("token") or ""),
                            "resource_type": resource_type,
                            "password": str(provider_data.get("password") or ""),
                        }
                    )
                    resolved = handler.resolve_source_resource(
                        source, **resolve_args
                    )
                finally:
                    handler.close(release_cache=False)
                url = str(resolved.get("url") or "").strip()
                resource_type = normalize_resource_type(
                    resolved.get("resource_type")
                )
                if not url:
                    raise RuntimeError("资源链接解析失败")
            if pending_hdhive:
                url = ""
                if parent_id:
                    return {
                        "success": False,
                        "message": "HDHive file-list 不支持目录导航",
                    }
                handler = self._build_test_search_handler(
                    "hdhive",
                    self._test_search_config("hdhive", payload.get("config")),
                )
                try:
                    candidate = {
                        "resource_ref": resource_ref,
                        "resource_type": resource_type,
                        "unlock_points": 0,
                        "is_unlocked": is_unlocked,
                        "target_season": payload.get("target_season"),
                        "target_episodes": payload.get("target_episodes"),
                        "supports_file_preview": payload.get(
                            "supports_file_preview"
                        ),
                        "provider_data": dict(
                            payload.get("provider_data") or {}
                        ),
                        "search_label": (
                            "测试已解锁预览" if is_unlocked else "测试只读预览"
                        ),
                    }
                    if is_unlocked:
                        url = handler.unlock_resource(
                            "hdhive", candidate,
                            search_label="测试已解锁预览",
                        )
                    else:
                        preview = handler.preview_resource(
                            "hdhive", candidate
                        )
                finally:
                    handler.close(release_cache=False)
                if is_unlocked:
                    url = str(url or "").strip()
                    if not url:
                        raise RuntimeError("HDHive 已解锁资源页未解析到分享链接")
                else:
                    files = [
                        {
                            **self._preview_file(item),
                            "can_enter": False,
                        }
                        for item in (preview.get("files") or [])
                    ][:500]
                    return {
                        "success": True,
                        "message": f"只读预览到 {len(files)} 个项目，未执行解锁",
                        "data": {
                            "items": files,
                            "count": len(files),
                            "provider_name": "HDHive",
                            "resource_type": resource_type,
                            "resource_type_name": resource_type_name(
                                resource_type, resource_type.upper()
                            ),
                            "share_url": "",
                            "parent_id": "",
                            "preview_episodes": preview.get("preview_episodes") or {},
                            "covers_target": preview.get("covers_target"),
                            "resource_validate_status": preview.get(
                                "resource_validate_status"
                            ) or "",
                            "resource_validate_message": preview.get(
                                "resource_validate_message"
                            ) or "",
                        },
                    }
            if pending_juying and not url:
                if parent_id:
                    return {"success": False, "message": "聚影资源链接已失效，请重新预览"}
                handler = self._build_test_search_handler(
                    "juying",
                    self._test_search_config("juying", payload.get("config")),
                )
                try:
                    resolved = handler.resolve_source_resource(
                        "juying", resource_id=juying_resource_id
                    )
                finally:
                    handler.close(release_cache=False)
                url = str(resolved.get("url") or "").strip()
                resource_type = normalize_resource_type(
                    resolved.get("resource_type")
                )
                if not url:
                    raise RuntimeError("聚影资源链接为空")
            if resource_type == "magnet":
                if parent_id:
                    return {"success": False, "message": "磁力链接不支持目录导航"}
                metadata = parse_magnet_metadata(url, fetch_info=True, timeout=12)
                files = [
                    {"name": str(entry.get("path") or entry.get("name") or "未命名文件"),
                     "size": int(entry.get("size") or 0), "is_dir": False}
                    for entry in (metadata.get("torrent_file_entries") or [])
                ]
                if not files:
                    raise RuntimeError(
                        "暂未获取到该磁力链接的 torrent 元数据"
                        f"（Info Hash: {metadata.get('info_hash') or '未知'}；"
                        "元数据地址未返回有效 torrent）"
                    )
                return {
                    "success": True,
                    "message": f"读取到 {len(files)} 个文件",
                    "data": {
                        "items": files, "count": len(files),
                        "provider_name": "",
                        "resource_type": "magnet",
                        "resource_type_name": resource_type_name("magnet"),
                        "share_url": url,
                        "parent_id": "",
                        "info_hash": metadata.get("info_hash"),
                        "display_name": metadata.get("display_name"),
                        "size": metadata.get("size") or 0
                    }
                }

            if "\\" in url or resource_type_from_url(url) != resource_type:
                return {"success": False, "message": "资源链接格式或类型无效"}

            provider_key = PREVIEW_PROVIDER_KEYS.get(resource_type)
            if not provider_key or not self._cloud_drive_registry:
                return {"success": False, "message": "当前资源类型暂不支持内容预览"}
            provider = self._cloud_drive_registry.get(provider_key)
            if not provider or not provider.supports(CloudDriveCapability.SHARE_TRANSFER):
                return {"success": False, "message": "对应网盘未配置或不支持分享预览"}
            service = provider.require(CloudDriveCapability.SHARE_TRANSFER)
            raw_files = service.list_share_directory(url, parent_id=parent_id) or []
            files = [self._preview_file(item) for item in raw_files][:500]
            return {
                "success": True,
                "message": f"读取到 {len(files)} 个项目",
                "data": {
                    "items": files, "count": len(files),
                    "provider_name": provider.name,
                    "resource_type": resource_type,
                    "resource_type_name": resource_type_name(
                        resource_type, provider.name
                    ),
                    "share_url": url,
                    "parent_id": parent_id,
                }
            }
        except Exception as error:
            message = self._preview_error_message(error)
            logger.warning(f"测试资源预览失败：{resource_type} - {message}")
            return {"success": False, "message": f"预览失败：{message}"}

    def api_vue_unlock_search_resource(self, payload: Dict[str, Any]) -> dict:
        payload = dict(payload or {})
        source = str(payload.get("source") or "").strip().lower()
        item = payload.get("item") if isinstance(payload.get("item"), dict) else {}
        try:
            points = max(0, int(item.get("unlock_points") or 0))
            free_hdhive_access = (
                    source == "hdhive"
                    and points == 0
                    and bool(item.get("need_access"))
                    and bool(item.get("is_free") or item.get("is_unlocked"))
            )
            zero_point_hdhive_unlock = (
                    source == "hdhive"
                    and points == 0
                    and bool(item.get("need_unlock"))
                    and bool(item.get("is_free"))
            )
            if points <= 0 and not (
                    free_hdhive_access or zero_point_hdhive_unlock
            ):
                return {"success": False, "message": "该资源不需要积分解锁"}
            resource_ref = str(
                item.get("resource_ref") or item.get("id") or ""
            ).strip()
            resource_type = normalize_resource_type(item.get("resource_type"))
            if source == "hdhive" and (
                    not resource_ref
                    or resource_type not in HDHIVE_DETAIL_RESOURCE_TYPES
            ):
                return {"success": False, "message": "HDHive 资源标识或类型无效"}
            handler = self._build_test_search_handler(
                source,
                self._test_search_config(source, payload.get("config")),
                confirmed_unlock_points=(
                    points if source in ("hdhive", "dian115") else 0
                ),
            )
            try:
                if not handler.supports(
                        source, SearchCapability.RESOURCE_UNLOCK
                ):
                    return {"success": False, "message": "当前搜索源不支持积分解锁"}
                candidate = dict(item)
                candidate.update({
                    "resource_ref": resource_ref,
                    "resource_type": resource_type,
                    "unlock_points": points,
                    "provider_data": dict(item.get("provider_data") or {}),
                })
                url = handler.unlock_resource(source, candidate)
                deducted_points = (
                    0
                    if free_hdhive_access
                       or bool(item.get("is_unlocked"))
                    else points
                )
            finally:
                handler.close(release_cache=False)
            if not url:
                message = "资源链接获取失败" if free_hdhive_access else "资源解锁失败"
                return {"success": False, "message": message}
            message = "资源链接已获取" if free_hdhive_access else "资源已解锁"
            return {
                "success": True,
                "message": message,
                "data": {
                    "url": url,
                    "deducted_points": deducted_points,
                },
            }
        except Exception as error:
            logger.warning(f"测试资源解锁失败：{source} - {error}")
            return {"success": False, "message": f"解锁失败：{error}"}

    def _test_search_config(
            self, source: str, overrides: Any
    ) -> Dict[str, Any]:
        """仅合并当前渠道测试真正需要的配置字段。"""
        base = dict(UIConfig.get_default_config())
        if isinstance(self._applied_config, dict):
            base.update(self._applied_config)
        allowed = self._SEARCH_TEST_CONFIG_FIELDS[source] | {
            "resource_type_order", "search_proxy", "search_proxy_username",
            "search_proxy_password",
        }
        if isinstance(overrides, dict):
            base.update({
                key: value for key, value in overrides.items() if key in allowed
            })
        return {key: base.get(key) for key in allowed}

    def _build_test_search_handler(
            self,
            source: str,
            config: Dict[str, Any],
            deadline: Optional[float] = None,
            confirmed_unlock_points: int = 0,
    ):
        """使用当前表单配置创建隔离搜索器，不修改已保存配置或运行中服务。"""
        from ...handlers.search import SearchHandler
        from ...search.hdhive import HDHiveOpenAPIClient
        from ...search.juying import JuyingClient
        from ...search.pansou import PanSouClient
        from ...search.pinglian import PinglianClient
        from ...search.seedhub import SeedHubClient
        from ...search.online_docs import OnlineDocumentClient
        from ...search.piratebay import PirateBayClient
        from ...search.uindex import UIndexClient

        def as_list(value: Any) -> list:
            if isinstance(value, list):
                return list(value)
            if value is None:
                return []
            return [value]

        proxy = build_proxy_url(
            config.get("search_proxy", ""),
            config.get("search_proxy_username", ""),
            config.get("search_proxy_password", ""),
        )
        hdhive_query_mode = str(config.get("hdhive_query_mode") or "web")
        if hdhive_query_mode not in {"api", "web"}:
            hdhive_query_mode = "web"
        hdhive_client = None
        if source == "hdhive" and hdhive_query_mode == "api":
            hdhive_client = HDHiveOpenAPIClient(
                app_secret=str(config.get("hdhive_api_key") or ""),
                client_id=str(config.get("hdhive_client_id") or ""),
                access_token=str(config.get("hdhive_access_token") or ""),
                refresh_token=str(config.get("hdhive_refresh_token") or ""),
                token_expires_at=float(
                    config.get("hdhive_token_expires_at") or 0
                ),
                proxy=proxy,
                request_interval=float(
                    config.get("hdhive_request_interval", 5) or 5
                ),
            )
        resource_type_order = list(dict.fromkeys(
            str(value).strip().lower()
            for value in as_list(config.get("resource_type_order"))
            if str(value).strip().lower()
            in {
                "115", "123", "quark", "guangya", "tianyi", "alipan",
                "ed2k", "magnet",
            }
        ))
        # 测试只验证渠道原始候选，不继承当前目标网盘的优先级或类型白名单。
        # 各渠道仍会自行识别真实资源类型，但不会因目标盘配置而丢弃候选。
        if source in {
            "hdhive", "dian115", "juying", "seedhub",
            "pinglian", "pansou", "piratebay", "uindex",
        }:
            resource_type_order = [
                "115", "123", "quark", "guangya", "tianyi", "alipan",
                "ed2k", "magnet",
            ]
        if not resource_type_order:
            raise ValueError("请至少选择一种资源类型")

        def require(*keys: str) -> None:
            if any(not str(config.get(key) or "").strip() for key in keys):
                raise ValueError("搜索渠道账号配置不完整")

        if source == "pansou":
            require("pansou_url")
            if bool(config.get("pansou_auth_enabled", False)):
                require("pansou_username", "pansou_password")
        elif source == "hdhive":
            if hdhive_query_mode == "api":
                require("hdhive_api_key", "hdhive_access_token")
            else:
                require("hdhive_username", "hdhive_password")
        elif source == "dian115":
            require("dian115_email", "dian115_password")
        elif source == "juying":
            require("juying_username", "juying_password")
        elif source == "pinglian":
            require("pinglian_username", "pinglian_password")

        hdhive_web_client = None
        hdhive_web_client_owned = True
        if (
                source == "hdhive"
                and hdhive_query_mode == "web"
                and deadline is None
        ):
            (
                hdhive_web_client,
                hdhive_web_client_owned,
            ) = self._get_test_hdhive_web_client(config, proxy)

        pansou_client = None
        pansou_timeout = 20
        if source == "pansou":
            pansou_url = str(config.get("pansou_url") or "").strip()
            if not pansou_url:
                raise ValueError("PanSou 服务地址为空")
            pansou_timeout = min(
                20, max(5, int(config.get("pansou_timeout", 30) or 30))
            )
            pansou_client = PanSouClient(
                base_url=pansou_url,
                username=str(config.get("pansou_username") or ""),
                password=str(config.get("pansou_password") or ""),
                auth_enabled=bool(config.get("pansou_auth_enabled", False)),
                proxy=proxy,
                search_timeout=pansou_timeout,
                get_data_func=self.get_data,
                save_data_func=self.save_data,
            )

        seedhub_client = SeedHubClient(
            base_url=str(config.get("seedhub_base_url") or ""),
            proxy=proxy,
            request_timeout=int(config.get("seedhub_timeout", 20) or 20),
            request_interval=float(
                config.get("seedhub_request_interval", 1) or 1
            ),
        ) if source == "seedhub" else None
        piratebay_client = PirateBayClient(
            base_url=str(config.get("piratebay_base_url") or ""),
            proxy=proxy,
            request_timeout=int(config.get("piratebay_timeout", 20) or 20),
            request_interval=float(
                config.get("piratebay_request_interval", 1) or 1
            ),
        ) if source == "piratebay" else None
        uindex_client = UIndexClient(
            base_url=str(config.get("uindex_base_url") or ""),
            proxy=proxy,
            request_timeout=int(config.get("uindex_timeout", 20) or 20),
            request_interval=float(
                config.get("uindex_request_interval", 1) or 1
            ),
        ) if source == "uindex" else None
        juying_client = None
        if source == "juying":
            juying_client = JuyingClient(
                base_url=str(config.get("juying_base_url") or ""),
                username=str(config.get("juying_username") or ""),
                password=str(config.get("juying_password") or ""),
                proxy=proxy,
                request_interval=float(
                    config.get("juying_request_interval", 1) or 1
                ),
                get_data_func=self.get_data,
                save_data_func=self.save_data,
                cache_namespace="test-preview",
            )
        pinglian_client = None
        online_docs_client = OnlineDocumentClient(
            config.get("online_docs") or config.get("online_docs_urls") or [],
            config.get("online_docs_resource_types") or [],
            proxy=proxy,
        ) if source == "online_docs" else None
        if source == "pinglian":
            pinglian_client = PinglianClient(
                base_url=str(config.get("pinglian_base_url") or ""),
                username=str(config.get("pinglian_username") or ""),
                password=str(config.get("pinglian_password") or ""),
                proxy=proxy,
                request_timeout=int(config.get("pinglian_timeout", 30) or 30),
                request_interval=float(
                    config.get("pinglian_request_interval", 2) or 2
                ),
                get_data_func=self.get_data,
                save_data_func=self.save_data,
            )
        confirmed_unlock_points = max(0, int(confirmed_unlock_points or 0))
        handler = SearchHandler(
            pansou_client=pansou_client,
            hdhive_client=hdhive_client,
            seedhub_client=seedhub_client,
            piratebay_client=piratebay_client,
            piratebay_enabled=source == "piratebay",
            piratebay_result_limit=int(config.get("piratebay_result_limit", 20) or 20),
            uindex_client=uindex_client,
            uindex_enabled=source == "uindex",
            uindex_result_limit=int(config.get("uindex_result_limit", 20) or 20),
            juying_client=juying_client,
            pinglian_client=pinglian_client,
            pansou_enabled=source == "pansou",
            hdhive_enabled=source == "hdhive",
            dian115_enabled=source == "dian115",
            seedhub_enabled=source == "seedhub",
            juying_enabled=source == "juying",
            pinglian_enabled=source == "pinglian",
            online_docs_client=online_docs_client,
            hdhive_web_client=hdhive_web_client,
            hdhive_web_client_owned=hdhive_web_client_owned,
            hdhive_username=str(config.get("hdhive_username") or ""),
            hdhive_password=str(config.get("hdhive_password") or ""),
            hdhive_query_mode=hdhive_query_mode,
            # HDHive 测试使用独立只读路径；显式关闭自动解锁能力。
            hdhive_auto_unlock=False,
            hdhive_max_unlock_points=confirmed_unlock_points,
            hdhive_max_points_per_sub=confirmed_unlock_points,
            dian115_email=str(config.get("dian115_email") or ""),
            dian115_password=str(config.get("dian115_password") or ""),
            dian115_auto_unlock=bool(
                config.get("dian115_auto_unlock", False)
            ),
            # 显式解锁时按用户确认的价格放行单次解锁；预览路径保持 0 预算。
            dian115_max_unlock_points=confirmed_unlock_points,
            dian115_max_points_per_sub=confirmed_unlock_points,
            pansou_channels=config.get("pansou_channels") or [],
            pansou_plugins=config.get("pansou_plugins") or [],
            pansou_cloud_types=resource_type_order,
            pansou_filter_include=config.get("pansou_filter_include") or [],
            pansou_filter_exclude=config.get("pansou_filter_exclude") or [],
            resource_type_order=resource_type_order,
            pansou_concurrency=config.get("pansou_concurrency") or None,
            pansou_result_limit=int(
                config.get("pansou_result_limit", 10) or 10
            ),
            pansou_refresh=False,
            pansou_timeout=pansou_timeout,
            seedhub_result_limit=int(
                config.get("seedhub_result_limit", 20) or 20
            ),
            juying_result_limit=int(
                config.get("juying_result_limit", 5) or 5
            ),
            pinglian_result_limit=int(
                config.get("pinglian_result_limit", 20) or 20
            ),
            search_source_order=[source],
            search_proxy=proxy,
            search_cache_enabled=False,
            search_concurrency=1,
            hdhive_candidate_limit=int(
                config.get("hdhive_candidate_limit", 4) or 4
            ),
            hdhive_request_interval=float(
                config.get("hdhive_request_interval", 5) or 5
            ),
            hdhive_unlocks_per_minute=int(
                config.get("hdhive_unlocks_per_minute", 2) or 2
            ),
            dian115_candidate_limit=int(
                config.get("dian115_candidate_limit", 4) or 4
            ),
            dian115_request_interval=float(
                config.get("dian115_request_interval", 1) or 1
            ),
            dian115_unlocks_per_minute=int(
                config.get("dian115_unlocks_per_minute", 6) or 6
            ),
            hdhive_torrentclaw_enabled=bool(
                config.get("hdhive_torrentclaw_enabled", False)
            ),
            hdhive_torrentclaw_subtitle_languages=as_list(
                config.get("hdhive_torrentclaw_subtitle_languages") or ["zh"]
            ),
            should_stop=(
                (lambda: time.monotonic() >= deadline) if deadline else None
            ),
        )
        handler.configure_point_storage(self.get_data, self.save_data)
        return handler

    def api_vue_search_tmdb_candidates(self, payload: Dict[str, Any]) -> dict:
        """按标题查询 TMDB 候选；指定电视剧 ID 时同时返回真实季。"""
        payload = dict(payload or {})
        title = str((payload or {}).get("title") or "").strip()
        if not title or len(title) > 100:
            return {"success": False, "message": "请输入 1 到 100 个字符的媒体名称"}
        try:
            requested_tmdb_id = int(payload.get("tmdb_id") or 0)
        except (TypeError, ValueError):
            requested_tmdb_id = 0
        requested_media_type = str(payload.get("media_type") or "").strip().lower()
        try:
            meta = MetaInfo(title)
            candidates = self.chain.search_medias(
                meta=meta,
                source="themoviedb",
            ) or []
        except Exception as error:
            logger.warning(f"[{title}][TMDB] 媒体候选查询失败：{error}")
            return {"success": False, "message": f"TMDB 查询失败：{error}"}

        items = []
        seen = set()
        for candidate in candidates:
            candidate_type = getattr(candidate, "type", None)
            media_type = (
                "movie" if candidate_type == MediaType.MOVIE
                else "tv" if candidate_type == MediaType.TV
                else ""
            )
            try:
                tmdb_id = int(getattr(candidate, "tmdb_id", 0) or 0)
            except (TypeError, ValueError):
                tmdb_id = 0
            identity = (media_type, tmdb_id)
            if not media_type or tmdb_id <= 0 or identity in seen:
                continue
            if requested_tmdb_id > 0 and tmdb_id != requested_tmdb_id:
                continue
            if requested_media_type in {"movie", "tv"} and media_type != requested_media_type:
                continue
            seen.add(identity)
            items.append({
                "tmdb_id": tmdb_id,
                "imdb_id": getattr(candidate, "imdb_id", None),
                "tvdb_id": getattr(candidate, "tvdb_id", None),
                "douban_id": getattr(candidate, "douban_id", None),
                "bangumi_id": getattr(candidate, "bangumi_id", None),
                "anilist_id": getattr(candidate, "anilist_id", None),
                "media_type": media_type,
                "media_type_name": "电影" if media_type == "movie" else "电视剧",
                "title": str(getattr(candidate, "title", None) or title),
                "original_title": str(
                    getattr(candidate, "original_title", None) or ""
                ),
                "year": getattr(candidate, "year", None),
                "poster": str(getattr(candidate, "poster_path", None) or ""),
                "poster_url": _platform_image_url(
                    getattr(candidate, "poster_path", None),
                    "w500",
                ),
                "vote_average": getattr(candidate, "vote_average", None),
            })
            if len(items) >= 20:
                break
        seasons = []
        if len(items) == 1 and items[0]["media_type"] == "tv" and requested_tmdb_id > 0:
            seasons = self._resolve_tmdb_seasons({**payload, **items[0]})
            items[0]["seasons"] = seasons
        return {
            "success": True,
            "message": f"TMDB 找到 {len(items)} 个候选",
            "data": {"items": items, "seasons": seasons},
        }

    def _resolve_tmdb_seasons(self, payload: Dict[str, Any]) -> List[int]:
        """读取指定 TMDB 电视剧的真实季号，排除特别篇。"""
        payload = dict(payload or {})
        try:
            tmdb_id = int(payload.get("tmdb_id") or 0)
        except (TypeError, ValueError):
            tmdb_id = 0
        title = str(payload.get("title") or "").strip()
        if tmdb_id <= 0 or not title:
            return []
        try:
            year = int(payload.get("year")) if str(payload.get("year") or "").strip() else None
        except (TypeError, ValueError):
            year = None
        mediainfo = self._resolve_test_media(
            payload=payload,
            title=title,
            original_title=str(payload.get("original_title") or ""),
            year=year,
            media_type=MediaType.TV,
            tmdb_id=tmdb_id,
            season=None,
        )
        seasons = set()
        raw_seasons = getattr(mediainfo, "seasons", None) or {}
        values = raw_seasons.keys() if isinstance(raw_seasons, dict) else raw_seasons
        for value in values or []:
            if isinstance(value, dict):
                value = value.get("season_number") or value.get("season")
            else:
                value = getattr(value, "season_number", value)
            try:
                season = int(value)
            except (TypeError, ValueError):
                continue
            if season > 0:
                seasons.add(season)
        if not seasons:
            total = int(getattr(mediainfo, "number_of_seasons", 0) or 0)
            seasons.update(range(1, total + 1))
        return sorted(seasons)

    def _resolve_test_media(
            self,
            payload: Dict[str, Any],
            title: str,
            original_title: str,
            year: Optional[int],
            media_type: MediaType,
            tmdb_id: int,
            season: Optional[int],
    ) -> MediaInfo:
        """通过平台识别一次取得测试搜索所需的完整媒体 ID。"""
        meta = MetaInfo(title)
        meta.type = media_type
        meta.year = year
        if season is not None:
            meta.begin_season = season
        try:
            mediainfo = recognize_media(
                self.chain,
                meta=meta,
                mtype=media_type,
                tmdb_id=tmdb_id,
                cache=True,
            )
        except Exception as error:
            logger.debug(f"测试搜索读取平台媒体信息失败，使用页面候选：{error}")
            mediainfo = None
        if not mediainfo:
            mediainfo = MediaInfo(
                type=media_type,
                title=title,
                year=str(year) if year is not None else None,
            )

        mediainfo.type = getattr(mediainfo, "type", None) or media_type
        mediainfo.title = getattr(mediainfo, "title", None) or title
        mediainfo.year = (
                getattr(mediainfo, "year", None)
                or (str(year) if year is not None else None)
        )
        mediainfo.tmdb_id = getattr(mediainfo, "tmdb_id", None) or tmdb_id
        apply_media_identity(mediainfo, "themoviedb", tmdb_id)
        mediainfo.original_title = (
                getattr(mediainfo, "original_title", None) or original_title
        )
        for media_field in self._TEST_MEDIA_ID_FIELDS:
            if getattr(mediainfo, media_field, None) not in (None, ""):
                continue
            value = payload.get(media_field)
            if value not in (None, ""):
                setattr(mediainfo, media_field, value)

        return mediainfo

    def api_vue_test_search_source(self, payload: Dict[str, Any]) -> dict:
        """使用页面输入执行隔离的单来源搜索，不触发下载、转存或历史写入。"""
        payload = dict(payload or {})
        source = str(payload.get("source") or "").strip().lower()
        source_names = {
            "hdhive": "HDHive",
            "dian115": "Dian115",
            "piratebay": "海盗湾",
            "uindex": "UIndex",
            "pansou": "PanSou",
            "juying": "聚影",
            "seedhub": "SeedHub",
            "pinglian": "盘链",
            "online_docs": "在线文档",
        }
        if source not in source_names:
            return {"success": False, "message": "不支持的搜索渠道"}
        title = str(payload.get("title") or "").strip()
        if not title or len(title) > 100:
            return {"success": False, "message": "请输入 1 到 100 个字符的媒体名称"}
        tmdb_id_value = str(payload.get("tmdb_id") or "").strip()
        try:
            tmdb_id = int(tmdb_id_value)
        except (TypeError, ValueError):
            return {"success": False, "message": "请先选择 TMDB 媒体条目"}
        if not 1 <= tmdb_id <= 999999999:
            return {"success": False, "message": "请先选择 TMDB 媒体条目"}
        media_type_value = str(payload.get("media_type") or "tv").strip().lower()
        if media_type_value not in {"movie", "tv"}:
            return {"success": False, "message": "媒体类型仅支持电影或电视剧"}
        media_type = MediaType.MOVIE if media_type_value == "movie" else MediaType.TV
        try:
            year = int(payload.get("year")) if str(payload.get("year") or "").strip() else None
        except (TypeError, ValueError):
            return {"success": False, "message": "年份必须是整数"}
        if year is not None and not 1900 <= year <= 2100:
            return {"success": False, "message": "年份必须在 1900 到 2100 之间"}
        try:
            season = int(payload.get("season") or 1) if media_type == MediaType.TV else None
        except (TypeError, ValueError):
            return {"success": False, "message": "季号必须是整数"}
        if season is not None and not 1 <= season <= 999:
            return {"success": False, "message": "季号必须在 1 到 999 之间"}
        config = self._test_search_config(source, payload.get("config"))
        original_title = str(payload.get("original_title") or "").strip()[:200]
        mediainfo = self._resolve_test_media(
            payload=payload,
            title=title,
            original_title=original_title,
            year=year,
            media_type=media_type,
            tmdb_id=tmdb_id,
            season=season,
        )
        media_ids = {
            field: getattr(mediainfo, field, None)
            for field in self._TEST_MEDIA_ID_FIELDS
            if getattr(mediainfo, field, None) not in (None, "")
        }

        test_started = time.monotonic()

        def run_test() -> list:
            handler = None
            try:
                handler = self._build_test_search_handler(
                    source, config
                )
                source_result_limit = handler.test_source_result_limit()
                results = handler.test_source(
                    source=source,
                    mediainfo=mediainfo,
                    media_type=media_type,
                    season=season,
                )
                return results, source_result_limit
            finally:
                if handler:
                    try:
                        handler.close(release_cache=False)
                    except Exception as close_error:
                        logger.debug(
                            f"[{source.upper()}] 测试搜索器关闭失败：{close_error}"
                        )

        try:
            results, source_result_limit = run_test()
        except Exception as error:
            logger.warning(
                f"[{title}{f' S{season:02d}' if season else ''}]"
                f"[{source.upper()}] 渠道测试失败：{error}"
            )
            return {
                "success": False,
                "message": f"{source_names[source]} 测试失败：{error}",
                "data": {
                    "source": source,
                    "elapsed_seconds": round(time.monotonic() - test_started, 2),
                },
            }
        supported_results = []
        for result in results or []:
            if not isinstance(result, dict):
                continue
            resource_type = normalize_resource_type(
                result.get("resource_type") or result.get("pan_type") or ""
            )
            if resource_type not in SUPPORTED_RESOURCE_TYPES:
                continue
            if result.get("resource_type") != resource_type:
                result = {**result, "resource_type": resource_type}
            supported_results.append(result)
        results = supported_results
        total_result_count = len(results)
        results = self._balanced_test_results(
            results, self._SEARCH_TEST_DISPLAY_LIMIT
        )
        displayed_result_count = len(results or [])
        items = []
        resource_type_counts: Dict[str, int] = {}

        def display_size(item: Dict[str, Any]) -> Any:
            human = str(item.get("size_human") or "").strip()
            if human:
                return human
            value = item.get("size")
            if not isinstance(value, (int, float)) or value <= 0:
                return value or 0
            return StringUtils.format_size(int(value))

        def display_tags(item: Dict[str, Any]) -> List[str]:
            values = [item.get("tags") or []]
            values.extend(
                item.get(key)
                for key in (
                    "resolution", "quality", "source_type", "codec",
                    "audio_codec", "hdr_type", "subtitle",
                )
                if item.get(key)
            )

            tags: List[str] = []
            seen_tags = set()

            def append_tag(value: Any) -> None:
                if isinstance(value, dict):
                    for nested in value.values():
                        append_tag(nested)
                    return
                if isinstance(value, (list, tuple, set)):
                    for nested in value:
                        append_tag(nested)
                    return

                text = str(value or "").strip()
                if not text:
                    return
                if text[:1] in ("[", "{") and text[-1:] in ("]", "}"):
                    try:
                        parsed = ast.literal_eval(text)
                    except (SyntaxError, ValueError):
                        parsed = None
                    if isinstance(parsed, (dict, list, tuple, set)):
                        append_tag(parsed)
                        return
                if text in seen_tags:
                    return
                seen_tags.add(text)
                tags.append(text)

            for value in values:
                append_tag(value)
            return tags

        for item in (results or [])[:self._SEARCH_TEST_DISPLAY_LIMIT]:
            source_url = ""
            for value in (item.get("source_url"), item.get("media_page_url")):
                candidate = str(value or "").strip()
                parsed = urlparse(candidate)
                if (parsed.scheme in {"http", "https"} and parsed.netloc
                        and (parsed.path.rstrip("/") or parsed.query)):
                    source_url = candidate
                    break
            resource_type = str(
                item.get("resource_type") or item.get("pan_type") or "unknown"
            ).strip().lower()
            try:
                unlock_points = max(0, int(item.get("unlock_points") or 0))
            except (TypeError, ValueError):
                unlock_points = 0
            provider_data = dict(item.get("provider_data") or {})
            items.append({
                "title": str(item.get("title") or "未命名资源"),
                "source": str(item.get("source") or source),
                "source_name": source_names.get(
                    str(item.get("source") or source), source_names[source]
                ),
                "resource_type": resource_type,
                "resource_type_name": resource_type_name(
                    resource_type, resource_type.upper() or "未知"
                ),
                "size": display_size(item),
                "size_bytes": item.get("size") or 0,
                "tags": display_tags(item),
                "description": str(item.get("description") or "").strip(),
                "source_url": source_url,
                "url": str(
                    item.get("url") or item.get("share_url")
                    or ""
                ).strip(),
                "resource_ref": str(item.get("resource_ref") or "").strip(),
                "provider_data": provider_data,
                "media_page_url": str(item.get("media_page_url") or "").strip(),
                "unlock_points": unlock_points,
                "need_unlock": bool(item.get("need_unlock")),
                "need_access": bool(item.get("need_access")),
                "is_unlocked": bool(item.get("is_unlocked")),
                "is_free": bool(item.get("is_free")),
                "target_season": item.get("target_season"),
                "target_episodes": item.get("target_episodes") or [],
                "preview_episodes": item.get("preview_episodes") or {},
                "pending_resolution": bool(item.get("pending_resolution")),
                "can_preview": resource_type in PREVIEW_RESOURCE_TYPES,
            })
            resource_type_counts[resource_type] = (
                    resource_type_counts.get(resource_type, 0) + 1
            )
        return {
            "success": True,
            "message": f"{source_names[source]} 测试完成，找到 {total_result_count} 个候选",
            "data": {
                "source": source,
                "source_name": source_names[source],
                "media_ids": media_ids,
                "media": (
                    f"{getattr(mediainfo, 'title', None) or title}"
                    + (f" ({getattr(mediainfo, 'year', None)})" if getattr(mediainfo, 'year', None) else "")
                    + (f" S{season:02d}" if season else "")
                ),
                "count": total_result_count,
                "displayed_count": displayed_result_count,
                "result_limit": source_result_limit,
                "display_limit": self._SEARCH_TEST_DISPLAY_LIMIT,
                "elapsed_seconds": round(time.monotonic() - test_started, 2),
                "items": items,
                "merged_by_type": {
                    resource_type: [item for item in items if item.get("resource_type") == resource_type]
                    for resource_type in resource_type_counts
                },
                "resource_types": [
                    {
                        "value": resource_type,
                        "title": resource_type_name(
                            resource_type, resource_type.upper() or "未知"
                        ),
                        "count": count,
                    }
                    for resource_type, count in sorted(
                        resource_type_counts.items(),
                        key=lambda pair: (
                            RESOURCE_TYPE_PRIORITY.get(pair[0], 99),
                            pair[0],
                        ),
                    )
                ],
            },
        }

    @staticmethod
    def _balanced_test_results(
            results: Any, limit: int
    ) -> List[Dict[str, Any]]:
        """按资源类型轮询选取测试候选，避免单一类型占满展示额度。"""
        groups: Dict[str, List[Dict[str, Any]]] = {}
        for item in results or []:
            if not isinstance(item, dict):
                continue
            resource_type = str(
                item.get("resource_type") or item.get("pan_type") or "unknown"
            ).strip().lower() or "unknown"
            groups.setdefault(resource_type, []).append(item)
        target = max(1, int(limit or 20))
        offsets = {resource_type: 0 for resource_type in groups}
        balanced = []
        while groups and len(balanced) < target:
            for resource_type in list(groups):
                rows = groups[resource_type]
                offset = offsets[resource_type]
                balanced.append(rows[offset])
                offset += 1
                offsets[resource_type] = offset
                if offset >= len(rows):
                    groups.pop(resource_type)
                    offsets.pop(resource_type, None)
                if len(balanced) >= target:
                    break
        return balanced

    def api_vue_resource_search_resources(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """为网盘资源详情页并发检索所有可用的搜索渠道（无需在设置中手动开启即可自动搜索）。"""
        data = payload or {}
        title = str(data.get("title") or "").strip()
        if not title:
            return {"success": False, "message": "媒体标题不能为空", "data": {"items": [], "sources": []}}

        original_title = str(data.get("original_title") or "").strip()
        year = str(data.get("year") or "").strip()
        media_type_str = str(data.get("media_type") or "movie").strip().lower()
        media_type = MediaType.TV if media_type_str in {"tv", "television", "teleplay"} else MediaType.MOVIE
        season = data.get("season")
        if season is not None:
            try:
                season = int(season)
            except (ValueError, TypeError):
                season = None

        mediainfo = MediaInfo()
        mediainfo.title = title
        mediainfo.type = media_type
        if original_title:
            mediainfo.original_title = original_title
        if year:
            mediainfo.year = str(year)
        if data.get("tmdb_id"):
            mediainfo.tmdb_id = data.get("tmdb_id")
        if data.get("imdb_id"):
            mediainfo.imdb_id = data.get("imdb_id")
        if data.get("tvdb_id"):
            mediainfo.tvdb_id = data.get("tvdb_id")
        if data.get("douban_id"):
            mediainfo.douban_id = data.get("douban_id")
        if data.get("bangumi_id"):
            mediainfo.bangumi_id = data.get("bangumi_id")
        if data.get("anilist_id"):
            mediainfo.anilist_id = data.get("anilist_id")
        if data.get("anidb_id"):
            mediainfo.anidb_id = data.get("anidb_id")

        # 资源页可能来自豆瓣、Bangumi、AniList 等非 TMDB 榜单；统一交由
        # MoviePilot 媒体识别链补齐 TMDB、IMDb、TVDB 及来源辅助 ID。
        identity_source = str(data.get("media_source") or "").strip().lower()
        if identity_source == "tmdb":
            identity_source = "themoviedb"
        identity_id = data.get("media_id")
        identity_fields = {
            "themoviedb": "tmdb_id",
            "douban": "douban_id",
            "bangumi": "bangumi_id",
            "anilist": "anilist_id",
            "imdb": "imdb_id",
            "tvdb": "tvdb_id",
        }
        if identity_source and not identity_id:
            identity_id = data.get(identity_fields.get(identity_source, ""))
        elif not identity_source:
            for source_name, field_name in (
                    ("themoviedb", "tmdb_id"),
                    ("douban", "douban_id"),
                    ("bangumi", "bangumi_id"),
                    ("anilist", "anilist_id"),
                    ("imdb", "imdb_id"),
                    ("tvdb", "tvdb_id"),
            ):
                value = data.get(field_name)
                if value not in (None, "", 0, "0"):
                    identity_source = source_name
                    identity_id = value
                    break
        if identity_source and identity_id:
            apply_media_identity(mediainfo, identity_source, identity_id)
            if any(
                    not getattr(mediainfo, field, None)
                    for field in (
                            "tmdb_id", "imdb_id", "tvdb_id", "douban_id",
                            "bangumi_id", "anilist_id",
                    )
            ):
                try:
                    meta = MetaInfo(title)
                    meta.type = media_type
                    meta.year = year or None
                    recognized = recognize_media(
                        self.chain,
                        meta=meta,
                        mtype=media_type,
                        media_source=identity_source,
                        media_id=str(identity_id),
                        cache=True,
                    )
                except Exception as error:
                    logger.debug(f"资源搜索补充媒体身份失败：{error}")
                    recognized = None
                if recognized:
                    for field in (
                            "tmdb_id", "imdb_id", "tvdb_id", "douban_id",
                            "bangumi_id", "anilist_id", "anidb_id",
                    ):
                        value = getattr(recognized, field, None)
                        if value and not getattr(mediainfo, field, None):
                            setattr(mediainfo, field, value)

        handler = getattr(self, "_search_handler", None)
        if not handler:
            return {"success": False, "message": "搜索服务未就绪", "data": {"items": [], "sources": []}}

        # 获取所有已注册的搜索渠道，无需用户在设置中开启
        registered_sources = handler.get_all_search_sources()
        if not registered_sources:
            logger.warning("[网盘资源嗅探] 当前无可用的已注册搜索渠道")
            return {"success": True, "message": "暂无可用搜索渠道",
                    "data": {"items": [], "sources": [], "available_sources": []}}

        # 权威渠道命名与顺序定义
        source_display_names = {
            "pansou": "PanSou",
            "hdhive": "HDHive",
            "dian115": "Dian115",
            "juying": "聚影",
            "pinglian": "盘链",
            "seedhub": "SeedHub",
            "piratebay": "海盗湾",
            "uindex": "UIndex",
            "online_docs": "在线文档",
        }
        # 优先读取用户配置的优先级顺序，其余按标准顺序排列
        configured_order = getattr(handler, "_search_source_order", []) or []
        default_pref = ["pansou", "hdhive", "dian115", "juying", "pinglian", "seedhub", "piratebay", "uindex"]
        merged_order = []
        for s in list(configured_order) + default_pref:
            s_clean = str(s).strip().lower()
            if s_clean in registered_sources and s_clean not in merged_order:
                merged_order.append(s_clean)
        for s in registered_sources:
            if s not in merged_order:
                merged_order.append(s)

        req_source = str(data.get("source") or "").strip().lower()
        force_refresh = bool(data.get("force") or data.get("force_refresh"))
        if req_source:
            sources_to_search = [req_source] if req_source in registered_sources else registered_sources
        else:
            sources_to_search = merged_order

        logger.debug(
            f"🔍 [网盘资源嗅探] 开始检索媒体《{title}》"
            f"（类型: {media_type.value}, 年份: {year or '未知'}, TMDB: {mediainfo.tmdb_id or '无'}, 豆瓣: {mediainfo.douban_id or '无'}），"
            f"目标渠道: {sources_to_search}，模式={'强制刷新' if force_refresh else '优先缓存'}"
        )

        started = time.monotonic()
        try:
            source_results = handler.search_sources(
                sources=sources_to_search,
                mediainfo=mediainfo,
                media_type=media_type,
                season=season,
                apply_platform_rules=False,  # 发现页展示全量候选，不走严苛的订阅自动下载规则过滤
                force_refresh=force_refresh,
                result_limit=50,
            )
        except Exception as error:
            logger.error(f"❌ [网盘资源嗅探] 渠道搜索发生异常：{error}", exc_info=True)
            source_results = {}

        all_items = []
        resource_type_counts: Dict[str, int] = {}
        for src, items in (source_results or {}).items():
            src_items_count = len(items) if isinstance(items, list) else 0
            logger.debug(
                f"📡 [网盘资源嗅探] 渠道 [{source_display_names.get(src, src)}] 返回 {src_items_count} 条候选资源")
            for item in items or []:
                if not isinstance(item, dict):
                    continue
                url = str(item.get("url") or item.get("share_url") or "").strip()
                raw_type = item.get("resource_type") or item.get("pan_type")
                r_type = resource_type_from_url(url) or normalize_resource_type(raw_type)
                # 资源弹窗保留各支持网盘的全部候选，但仍隐藏当前转存链路无法处理的类型。
                if r_type not in SUPPORTED_RESOURCE_TYPES:
                    continue

                item_copy = dict(item)
                item_copy["source"] = src
                item_copy["resource_type"] = r_type
                item_copy["resource_type_name"] = resource_type_name(
                    r_type, r_type.upper() if r_type else "未知"
                )
                item_copy["can_preview"] = bool(
                    item.get("can_preview") or (r_type in PREVIEW_RESOURCE_TYPES)
                )
                item_copy["tags"] = self._display_tags(item)
                if not item_copy.get("size_formatted"):
                    item_copy["size_formatted"] = self._display_size(item)
                all_items.append(item_copy)
                resource_type_counts[r_type] = resource_type_counts.get(r_type, 0) + 1

        # 排序：有做种数排前面，其次按大小降序
        all_items.sort(
            key=lambda x: (
                self._sort_size(x.get("seeders")),
                self._sort_size(x.get("size")),
            ),
            reverse=True,
        )
        elapsed = round(time.monotonic() - started, 2)
        logger.debug(
            f"✅ [网盘资源嗅探] 《{title}》检索完成，有效候选共 {len(all_items)} 条，总耗时 {elapsed}s"
        )

        # 构造网盘分类和资源类型 tabs 统计列表
        resource_types_list = [
            {
                "value": t_val,
                "title": resource_type_name(t_val, t_val.upper()),
                "count": t_cnt,
            }
            for t_val, t_cnt in sorted(
                resource_type_counts.items(),
                key=lambda x: x[1],
                reverse=True,
            )
        ]

        # 构造规范有序的渠道列表供前端渲染Tab
        available_channels_meta = [
            {
                "key": src_key,
                "name": source_display_names.get(src_key, src_key),
            }
            for src_key in merged_order
        ]

        # 构造当前系统可用且支持的目标网盘列表供跨盘转存选择
        available_drives = []
        registry = getattr(self, "_cloud_drive_registry", None)
        if registry:
            for drive in registry.available():
                if getattr(drive, "key", "") not in {"baidu", "xunlei"}:
                    available_drives.append({
                        "key": drive.key,
                        "name": drive.name,
                    })
        if not available_drives:
            main_key = getattr(getattr(self, "_cloud_drive", None), "key", "115")
            main_name = getattr(getattr(self, "_cloud_drive", None), "name", "115网盘")
            available_drives.append({"key": main_key, "name": main_name})

        return {
            "success": True,
            "message": f"共检索到 {len(all_items)} 条候选资源 (耗时 {elapsed}s)",
            "data": {
                "items": all_items,
                "sources": sources_to_search,
                "available_sources": available_channels_meta,
                "resource_types": resource_types_list,
                "available_drives": available_drives,
                "main_cloud_drive": getattr(getattr(self, "_cloud_drive", None), "key", "115"),
                "elapsed": elapsed,
            },
        }
