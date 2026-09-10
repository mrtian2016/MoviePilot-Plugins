"""订阅同步任务提交 API。"""

from concurrent.futures import ThreadPoolExecutor
from pathlib import PurePosixPath
from typing import Any, Dict, List, Optional
from urllib.parse import quote

from app.core.metainfo import MetaInfo
from app.db import SessionFactory
from app.db.subscribe_oper import SubscribeOper
from app.log import logger
from app.schemas.types import MediaType

from .. import CloudDriveCapability, OwnerDelegator
from ..media import recognize_media, tmdb_id_of
from ...search.types import normalize_resource_type
from ...utils.cache import create_platform_ttl_cache

_RECENT_MANUAL_SUBMITS = create_platform_ttl_cache(
    "sync:manual_submits", maxsize=256, ttl=4
)


class SyncApi(OwnerDelegator):
    def _resolve_manual_tmdb_media(
            self,
            tmdb_id: int,
            media_type: str,
    ):
        """以后端 TMDB ID 重新获取规范媒体信息，不信任前端标题。"""
        resolved_type = (
            MediaType.TV if media_type == "tv" else MediaType.MOVIE
        )
        meta = MetaInfo(str(tmdb_id))
        meta.type = resolved_type
        mediainfo = recognize_media(
            self.chain,
            meta=meta,
            mtype=resolved_type,
            tmdb_id=tmdb_id,
            cache=True,
        )
        if not mediainfo:
            raise ValueError(f"TMDB 媒体不存在：{tmdb_id}")
        return mediainfo

    @staticmethod
    def _manual_resource_type(link: str, default: str) -> str:
        value = str(link or "").lower()
        for marker, resource_type in (
                ("quark", "quark"), ("189.cn", "tianyi"),
                ("cloud.189", "tianyi"), ("guangya", "guangya"),
                ("123pan", "123"), ("123.cn", "123"),
                ("123684.com", "123"), ("123865.com", "123"),
                ("alipan.com", "alipan"), ("aliyundrive.com", "alipan"),
                ("pan.baidu.com", "baidu"), ("baidu.com", "baidu"),
                ("drive.uc.cn", "uc"), ("uc.cn", "uc"),
                ("115.com", "115"), ("anxia.com", "115"),
                ("pan.xunlei.com", "xunlei"), ("xunlei.com", "xunlei"),
                ("mypikpak.com", "pikpak"), ("pikpak", "pikpak"),
                ("115cdn.com", "115"),
        ):
            if marker in value:
                return resource_type
        return default

    def _manual_share_service(self, resource_type: str):
        registry = getattr(self, "_cloud_drive_registry", None)
        if registry:
            try:
                return registry.get({
                                        "189": "tianyi", "aliyun": "alipan"
                                    }.get(resource_type, resource_type)).require(
                    CloudDriveCapability.SHARE_TRANSFER
                )
            except (KeyError, RuntimeError):
                pass
        if self._cloud_drive and resource_type == self._cloud_drive.key:
            return self._share_transfer
        return None

    @staticmethod
    def _manual_share_info_valid(resource_type: str, share_info: Dict[str, Any]) -> bool:
        """分享提取码是可选字段，语法校验只要求可解析分享标识。"""
        return bool(share_info.get("share_code"))

    @staticmethod
    def _manual_resource_name(resource_type: str) -> str:
        return {
            "115": "115网盘",
            "123": "123网盘",
            "quark": "夸克网盘",
            "guangya": "光鸭网盘",
            "tianyi": "天翼云盘",
            "aliyun": "阿里云盘",
            "alipan": "阿里云盘",
            "baidu": "百度网盘",
            "uc": "UC网盘",
            "xunlei": "迅雷网盘",
            "pikpak": "PikPak",
        }.get(resource_type, (resource_type.upper() if resource_type else "未知网盘"))

    @staticmethod
    def _positive_ints(values: Any) -> List[int]:
        result = set()
        if isinstance(values, dict):
            values = values.keys()
        elif not isinstance(values, (list, tuple, set)):
            values = []
        for value in values:
            try:
                normalized = int(value or 0)
            except (TypeError, ValueError):
                continue
            if normalized > 0:
                result.add(normalized)
        return sorted(result)

    @classmethod
    def _normalize_history_search_targets(
            cls, targets: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """将历史媒体组展开为同步链可直接处理的电影或单季电视剧目标。"""
        media_type_values = {
            "tv": MediaType.TV.value,
            "电视剧": MediaType.TV.value,
            "movie": MediaType.MOVIE.value,
            "电影": MediaType.MOVIE.value,
        }
        normalized: Dict[tuple, Dict[str, Any]] = {}
        for target in targets:
            if not isinstance(target, dict):
                continue
            try:
                tmdb_id = int(target.get("tmdb_id") or 0)
            except (TypeError, ValueError):
                tmdb_id = 0
            title = str(target.get("title") or "").strip()
            media_type = media_type_values.get(
                str(target.get("media_type") or "").strip().lower(), ""
            )
            if tmdb_id <= 0 or not title or not media_type:
                continue

            base = {
                "tmdb_id": tmdb_id,
                "media_type": media_type,
                "title": title,
                "year": target.get("year") or "",
            }
            if media_type == MediaType.MOVIE.value:
                normalized.setdefault((media_type, tmdb_id, 0), base)
                continue

            season_episodes = target.get("season_episodes") or {}
            if not isinstance(season_episodes, dict):
                season_episodes = {}
            seasons = set(cls._positive_ints(target.get("seasons") or []))
            seasons.update(cls._positive_ints(season_episodes.keys()))
            try:
                single_season = int(target.get("season") or 0)
            except (TypeError, ValueError):
                single_season = 0
            if single_season > 0:
                seasons.add(single_season)
            fallback_episodes = cls._positive_ints(target.get("episodes") or [])
            for season in sorted(seasons):
                episodes = cls._positive_ints(
                    season_episodes.get(str(season))
                    or season_episodes.get(season)
                    or (fallback_episodes if len(seasons) == 1 else [])
                )
                if not episodes:
                    continue
                key = (media_type, tmdb_id, season)
                existing = normalized.get(key)
                if existing:
                    existing["episodes"] = sorted(
                        set(existing.get("episodes") or []) | set(episodes)
                    )
                    continue
                normalized[key] = {
                    **base,
                    "season": season,
                    "episodes": episodes,
                }
        return list(normalized.values())

    def api_vue_start_sync(
            self,
            payload: Optional[Dict[str, Any]] = None,
            wait: bool = False,
    ) -> dict:
        payload = payload or {}
        raw_subscribe_ids = payload.get("subscribe_ids") or []
        raw_targets = payload.get("history_targets") or []
        try:
            selected_count = max(0, int(payload.get("selected_count") or 0))
        except (TypeError, ValueError):
            selected_count = 0
        selection_requested = bool(
            selected_count or raw_subscribe_ids or raw_targets
        )
        if not isinstance(raw_subscribe_ids, list) or not isinstance(raw_targets, list):
            return {"success": False, "message": "立即搜索范围参数无效"}
        if len(raw_subscribe_ids) > 200 or len(raw_targets) > 200:
            return {"success": False, "message": "单次最多选择 200 个历史媒体"}

        subscribe_ids = set()
        history_search_targets: List[Dict[str, Any]] = []
        if selection_requested:
            with SessionFactory() as db:
                subscribes = SubscribeOper(db=db).list() or []
            supported_types = {MediaType.TV.value, MediaType.MOVIE.value}
            subscriptions_by_id = {
                int(subscribe.id): subscribe
                for subscribe in subscribes
                if int(getattr(subscribe, "id", 0) or 0) > 0
                   and getattr(subscribe, "type", None) in supported_types
            }
            for value in raw_subscribe_ids:
                try:
                    subscribe_id = int(value or 0)
                except (TypeError, ValueError):
                    continue
                if subscribe_id in subscriptions_by_id:
                    subscribe_ids.add(subscribe_id)

            normalized_targets = self._normalize_history_search_targets(raw_targets)
            if len(normalized_targets) > 500:
                return {"success": False, "message": "所选历史记录包含的媒体季数过多"}
            for target in normalized_targets:
                tmdb_id = int(target["tmdb_id"])
                media_type = str(target["media_type"])
                title = " ".join(
                    str(target.get("title") or "").strip().casefold().split()
                )
                year = str(target.get("year") or "").strip()
                season = int(target.get("season") or 0)
                matched_ids = set()
                for subscribe_id, subscribe in subscriptions_by_id.items():
                    if media_type and getattr(subscribe, "type", None) != media_type:
                        continue
                    try:
                        subscribe_tmdb_id = int(tmdb_id_of(subscribe) or 0)
                    except (TypeError, ValueError):
                        subscribe_tmdb_id = 0
                    if tmdb_id > 0:
                        if subscribe_tmdb_id != tmdb_id:
                            continue
                    else:
                        subscribe_title = " ".join(
                            str(getattr(subscribe, "name", "") or "")
                            .strip().casefold().split()
                        )
                        subscribe_year = str(
                            getattr(subscribe, "year", "") or ""
                        ).strip()
                        if not title or subscribe_title != title:
                            continue
                        if year and subscribe_year and subscribe_year != year:
                            continue
                    if media_type == MediaType.TV.value:
                        try:
                            subscribe_season = int(
                                getattr(subscribe, "season", 1) or 1
                            )
                        except (TypeError, ValueError):
                            subscribe_season = 1
                        if subscribe_season != season:
                            continue
                    matched_ids.add(subscribe_id)

                if matched_ids:
                    subscribe_ids.update(matched_ids)
                else:
                    history_search_targets.append(target)

            if not subscribe_ids and not history_search_targets:
                return {
                    "success": False,
                    "message": "所选历史记录缺少可搜索的媒体或集数信息",
                }

        selected_ids = sorted(subscribe_ids) if selection_requested else None
        sync_kwargs = {
            "subscribe_ids": selected_ids,
            "history_search_targets": history_search_targets or None,
        }
        history_target_count = len(history_search_targets)
        media_count = len(selected_ids or []) + history_target_count
        if wait:
            result: Dict[str, Any] = {}
            future = self._submit_sync_operation(
                {**sync_kwargs, "result": result},
                "页面订阅搜索",
            )
            future.result()
            data = dict(result.get("data") or {})
            data.update({
                "scope": "selected" if selection_requested else "all",
                "subscribe_count": len(selected_ids or []),
                "history_target_count": history_target_count,
                "media_count": media_count,
            })
            result["data"] = data
            return result
        self._submit_sync_operation(
            sync_kwargs,
            "页面订阅搜索",
        )
        if selection_requested:
            parts = []
            if selected_ids:
                parts.append(f"{len(selected_ids)} 个订阅")
            if history_target_count:
                parts.append(f"{history_target_count} 个历史媒体目标")
            message = f"已按所选历史记录提交{'和'.join(parts)}的搜索"
            scope = "selected"
        else:
            message = "全部订阅搜索任务已提交"
            scope = "all"
        return {
            "success": True,
            "message": message,
            "data": {
                "scope": scope,
                "subscribe_count": len(selected_ids or []),
                "history_target_count": history_target_count,
                "media_count": media_count,
            },
        }

    def api_vue_start_manual_sync(
            self, payload: Dict[str, Any], wait: bool = False
    ) -> dict:
        """校验指定订阅和资源链接后进入现有转存流程。"""
        try:
            return self._do_api_vue_start_manual_sync(payload, wait=wait)
        except Exception as error:
            logger.error(f"手动转存任务执行异常：{error}", exc_info=True)
            return {"success": False, "message": f"提交转存任务失败：{error}"}

    def _do_api_vue_start_manual_sync(
            self, payload: Dict[str, Any], wait: bool = False
    ) -> dict:
        # 短时间防重复点击保护
        try:
            sub_id_check = int((payload or {}).get("subscribe_id") or 0)
            raw_m = (payload or {}).get("media") or {}
            res_list = (payload or {}).get("resources") or (payload or {}).get("resource_links") or []
            first_key = ""
            if res_list and isinstance(res_list, list) and isinstance(res_list[0], dict):
                first_key = str(
                    res_list[0].get("url") or res_list[0].get("resource_ref") or res_list[0].get("title") or "")
            elif res_list and isinstance(res_list, list):
                first_key = str(res_list[0])
            fingerprint = f"{sub_id_check}:{raw_m.get('tmdb_id')}:{raw_m.get('media_type')}:{first_key}"
            if fingerprint in _RECENT_MANUAL_SUBMITS:
                logger.debug(f"拦截短时间内重复提交的转存请求：{fingerprint}")
                return {
                    "success": True,
                    "message": "转存任务已在处理中，请勿重复点击",
                }
            _RECENT_MANUAL_SUBMITS[fingerprint] = True
        except Exception:
            pass

        try:
            subscribe_id = int((payload or {}).get("subscribe_id") or 0)
        except (TypeError, ValueError):
            subscribe_id = 0
        media_target = None
        if subscribe_id <= 0:
            raw_media = (payload or {}).get("media") or {}
            try:
                tmdb_id = int(raw_media.get("tmdb_id") or 0)
                media_type = str(raw_media.get("media_type") or "").strip().lower()
            except (TypeError, ValueError):
                return {"success": False, "message": "TMDB 媒体信息格式错误"}
            if tmdb_id <= 0 or media_type not in {"movie", "tv"}:
                return {"success": False, "message": "请选择订阅或有效的 TMDB 媒体"}
            try:
                canonical_media = self._resolve_manual_tmdb_media(
                    tmdb_id=tmdb_id,
                    media_type=media_type,
                )
            except Exception as error:
                return {"success": False, "message": f"读取 TMDB 媒体信息失败：{error}"}
            canonical_title = str(getattr(canonical_media, "title", "") or "").strip()
            if not canonical_title:
                return {"success": False, "message": "TMDB 媒体缺少规范标题"}
            seasons = []
            if media_type == "tv":
                raw_seasons = raw_media.get("seasons")
                if raw_seasons is None:
                    raw_seasons = []
                elif not isinstance(raw_seasons, (list, tuple, set)):
                    return {"success": False, "message": "季数格式错误"}
                try:
                    seasons = sorted({
                        int(value) for value in raw_seasons
                        if int(value) > 0
                    })
                except (TypeError, ValueError):
                    return {"success": False, "message": "季数格式错误"}
                if not seasons:
                    raw_seasons = getattr(canonical_media, "seasons", None) or {}
                    values = (
                        raw_seasons.keys()
                        if isinstance(raw_seasons, dict) else raw_seasons
                    )
                    resolved_seasons = set()
                    for value in values or []:
                        if isinstance(value, dict):
                            value = value.get("season_number") or value.get("season")
                        try:
                            season = int(value)
                        except (TypeError, ValueError):
                            continue
                        if season > 0:
                            resolved_seasons.add(season)
                    seasons = sorted(resolved_seasons)
                    if not seasons:
                        total_seasons = int(getattr(canonical_media, "number_of_seasons", 0) or 0)
                        seasons = list(range(1, total_seasons + 1))
                if not seasons:
                    seasons = [1]
                if seasons[-1] > 999:
                    return {"success": False, "message": "请选择 1 到 999 之间的季"}
            media_target = {
                "tmdb_id": tmdb_id,
                "douban_id": raw_media.get("douban_id"),
                "bangumi_id": raw_media.get("bangumi_id"),
                "media_type": media_type,
                "title": canonical_title,
                "year": getattr(canonical_media, "year", None) or raw_media.get("year"),
                "seasons": seasons,
            }

        resource_list = (payload or {}).get("resources") or []
        if not isinstance(resource_list, list):
            resource_list = []

        # 资源列表提交时优先使用后端返回的渠道元数据。HDHive 的候选可能只有
        # resource_ref（或 URL 尚未回填），这里在已解锁/零积分场景由后端补齐链接。
        resource_items = []
        for value in resource_list:
            if not isinstance(value, dict):
                continue
            item = dict(value)
            source = str(item.get("source") or "").strip().lower()
            resource_ref = str(item.get("resource_ref") or "").strip()
            raw_item_url = item.get("url") or item.get("share_url")
            provider_data = (
                dict(item.get("provider_data") or {})
                if isinstance(item.get("provider_data"), dict) else {}
            )
            try:
                unlock_points = int(item.get("unlock_points") or 0)
            except (TypeError, ValueError):
                unlock_points = 0
            can_resolve = bool(
                self._search_handler
                and not raw_item_url
                and source in {"hdhive", "juying", "seedhub", "pinglian"}
                and (
                        resource_ref
                        or provider_data.get("resource_id")
                        or provider_data.get("seed_id")
                        or provider_data.get("token")
                )
            )
            # 付费 HDHive 仍需先解锁；已解锁、零积分及其它渠道的延迟资源，
            # 在提交转存时统一由后端解析为真实链接。
            can_resolve = can_resolve and (
                    source != "hdhive"
                    or bool(item.get("is_unlocked"))
                    or unlock_points <= 0
            )
            if can_resolve:
                try:
                    if source == "hdhive":
                        resolve_item = dict(item)
                        if bool(item.get("is_unlocked")):
                            resolve_item["unlock_points"] = 0
                        resolved = self._search_handler.unlock_resource(
                            source, resolve_item, search_label="资源列表转存"
                        )
                    elif source == "juying":
                        resolved = self._search_handler.resolve_source_resource(
                            source,
                            resource_id=str(
                                provider_data.get("resource_id") or resource_ref
                            ).strip(),
                        )
                    elif source == "seedhub":
                        resolved = self._search_handler.resolve_source_resource(
                            source,
                            kind=str(provider_data.get("kind") or ""),
                            resource_type=str(
                                item.get("resource_type") or ""
                            ),
                            seed_id=str(
                                provider_data.get("seed_id") or resource_ref
                            ),
                            path=str(provider_data.get("path") or ""),
                            host=str(provider_data.get("host") or ""),
                        )
                    else:
                        resolved = self._search_handler.resolve_source_resource(
                            source,
                            token=str(provider_data.get("token") or resource_ref),
                            resource_type=str(
                                item.get("resource_type") or ""
                            ),
                            password=str(provider_data.get("password") or ""),
                        )
                    resolved_url = (
                        resolved.get("url") if isinstance(resolved, dict) else resolved
                    )
                    if isinstance(resolved_url, (list, tuple, set)):
                        item["url"] = list(resolved_url)
                    else:
                        item["url"] = str(resolved_url or "").strip()
                    if isinstance(resolved, dict) and resolved.get("resource_type"):
                        item["resource_type"] = resolved["resource_type"]
                except Exception as error:
                    logger.warning(
                        f"资源列表后端解析 {source.upper()} 链接失败：{error}"
                    )
            resource_items.append(item)

        raw_links = (payload or {}).get("resource_links") or []
        if isinstance(raw_links, str):
            raw_links = raw_links.splitlines()
        if not isinstance(raw_links, list):
            return {"success": False, "message": "资源链接格式错误"}

        # 允许前端只提交 resources；实际链接仍由后端统一抽取和去重。
        if not raw_links:
            for item in resource_items:
                value = item.get("url") or item.get("share_url")
                values = value if isinstance(value, (list, tuple, set)) else [value]
                raw_links.extend(values)

        links = []
        for value in raw_links:
            link = str(value or "").strip()
            if link and link not in links:
                links.append(link)
        if len(links) > 50:
            return {"success": False, "message": "单次最多处理 50 个资源链接"}

        raw_cloud_path = str((payload or {}).get("cloud_path") or "").strip()
        cloud_path = ""
        cloud_provider = ""
        cloud_drive = None
        target_provider = ""
        if raw_cloud_path:
            cloud_parts = [
                part for part in raw_cloud_path.replace("\\", "/").split("/")
                if part
            ]
            if any(part in {".", ".."} for part in cloud_parts):
                return {"success": False, "message": "网盘资源路径格式错误"}
            cloud_path = str(PurePosixPath(
                "/" + "/".join(cloud_parts)
            ))
            target_cloud_key = str(
                (payload or {}).get("target_cloud")
                or (payload or {}).get("target_provider")
                or getattr(self._cloud_drive, "key", "115")
            ).strip().lower()
            target_drive = self._cloud_drive
            if target_cloud_key and self._cloud_drive_registry:
                try:
                    target_drive = self._cloud_drive_registry.get(target_cloud_key)
                except KeyError:
                    pass
            target_key = getattr(target_drive, "key", "115")
            target_provider = target_key
            cloud_provider = str(
                (payload or {}).get("cloud_provider") or target_provider
            ).strip().lower()
            try:
                cloud_drive = (
                    self._cloud_drive_registry.get(cloud_provider)
                    if self._cloud_drive_registry and cloud_provider
                    else target_drive
                )
            except KeyError:
                return {"success": False, "message": "所选网盘提供方不存在"}
            if not cloud_drive or not cloud_drive.supports(
                    CloudDriveCapability.DIRECTORY_READ
            ):
                return {"success": False, "message": "所选网盘不支持目录浏览"}
            if cloud_provider != target_provider:
                cross_ready = bool(
                    cloud_drive.supports(CloudDriveCapability.FILE_QUERY)
                    and cloud_drive.supports(CloudDriveCapability.FILE_DOWNLOAD)
                    and target_drive
                    and target_drive.supports(CloudDriveCapability.LOCAL_UPLOAD)
                    and target_drive.supports(CloudDriveCapability.FILE_QUERY)
                )
                if not cross_ready:
                    return {"success": False, "message": "所选网盘未满足跨盘转存条件"}
            directory_service = cloud_drive.require(
                CloudDriveCapability.DIRECTORY_READ
            )
            try:
                lookup = directory_service.resolve_directory(cloud_path)
            except Exception as error:
                return {
                    "success": False,
                    "message": f"读取网盘资源路径失败：{error}",
                }
            if not lookup.checked:
                return {"success": False, "message": "读取网盘资源路径失败"}
            if lookup.directory_id is None:
                return {"success": False, "message": "所选网盘资源路径不存在"}
        if not links and not cloud_path:
            return {"success": False, "message": "请填写资源链接或选择网盘路径"}

        subscribe = None
        if subscribe_id > 0:
            with SessionFactory() as db:
                subscribe = SubscribeOper(db=db).get(subscribe_id)
            if not subscribe:
                return {"success": False, "message": "指定订阅不存在"}
            if subscribe.type not in {MediaType.TV.value, MediaType.MOVIE.value}:
                return {"success": False, "message": "仅支持电影或电视剧订阅"}

        share_transfer = None
        offline_download = None
        if self._cloud_drive:
            if self._cloud_drive.supports(CloudDriveCapability.SHARE_TRANSFER):
                share_transfer = self._cloud_drive.require(
                    CloudDriveCapability.SHARE_TRANSFER
                )
            if self._cloud_drive.supports(CloudDriveCapability.OFFLINE_DOWNLOAD):
                offline_download = self._cloud_drive.require(
                    CloudDriveCapability.OFFLINE_DOWNLOAD
                )
        magnet_links = [
            link for link in links
            if offline_download and offline_download.is_magnet_url(link)
        ]
        magnet_info_by_url = {}
        if magnet_links:
            with ThreadPoolExecutor(
                    max_workers=min(3, len(magnet_links)),
                    thread_name_prefix="pansearch-magnet-metadata",
            ) as executor:
                results = executor.map(
                    lambda value: offline_download.parse_magnet_link(
                        value, fetch_metadata=True
                    ),
                    magnet_links,
                )
                magnet_info_by_url = dict(zip(magnet_links, results))

        resources = []
        skip_history = bool((payload or {}).get("skip_history"))
        resource_titles = (payload or {}).get("resource_titles") or {}
        title_map = {}
        if isinstance(resource_titles, dict):
            for k, v in resource_titles.items():
                if k and v:
                    title_map[str(k).strip()] = str(v).strip()
        resource_meta_by_url = {}
        for r in resource_items:
            values = r.get("url") or r.get("share_url")
            values = values if isinstance(values, (list, tuple, set)) else [values]
            for value in values:
                normalized = str(value or "").strip()
                if normalized:
                    resource_meta_by_url[normalized] = r
                    if r.get("title"):
                        title_map[normalized] = str(r["title"]).strip()

        if cloud_path:
            provider_name = str(getattr(cloud_drive, "name", cloud_provider) or cloud_provider)
            cloud_url = f"cloud://{cloud_provider}{quote(cloud_path, safe='/')}"
            resources.append({
                "url": cloud_url,
                "title": title_map.get(cloud_url) or f"{provider_name}路径 {cloud_path}",
                "resource_type": "cloud",
                "source": "manual",
                "cloud_path": cloud_path,
                "cloud_provider": cloud_provider,
                "unlock_points": 0,
                "skip_history": skip_history,
            })
        invalid_links = []
        for index, link in enumerate(links, start=1):
            metadata = resource_meta_by_url.get(link) or (
                resource_items[index - 1] if index <= len(resource_items) else {}
            )
            if offline_download and offline_download.is_ed2k_url(link):
                resource_type = "ed2k"
                valid = bool(offline_download.parse_ed2k_link(link))
            elif offline_download and offline_download.is_magnet_url(link):
                resource_type = "magnet"
                magnet_info = magnet_info_by_url.get(link)
                valid = bool(
                    magnet_info
                    and (magnet_info.get("metadata") or {}).get("metadata_available")
                )
            else:
                metadata_type = normalize_resource_type(
                    metadata.get("resource_type") or metadata.get("pan_type")
                )
                target_cloud_key = str(
                    (payload or {}).get("target_cloud")
                    or (payload or {}).get("target_provider")
                    or getattr(self._cloud_drive, "key", "115")
                ).strip().lower()
                target_key = target_cloud_key or getattr(self._cloud_drive, "key", "115")
                resource_type = metadata_type or self._manual_resource_type(link, target_key)
                share_service = self._manual_share_service(resource_type)
                if not share_service:
                    source_name = self._manual_resource_name(resource_type)
                    target_name = self._manual_resource_name(target_key)
                    if resource_type != target_key:
                        hint = (
                            f"检测到跨盘转存【{source_name}】资源到【{target_name}】。"
                            f"需要配置【{source_name}】账号凭据以提取源文件，请在插件设置中配置【{source_name}】凭据后重试"
                        )
                    else:
                        hint = f"{source_name}分享源尚未接入或未配置凭据，暂不支持手动转存"
                    invalid_links.append((index, hint))
                    continue
                share_info = share_service.extract_share_info(link)
                valid = self._manual_share_info_valid(resource_type, share_info)
            if not valid:
                reason = (
                    "Magnet 必须能解析出名称或完整文件元数据"
                    if resource_type == "magnet"
                    else (
                        f"无法解析有效的 {self._manual_resource_name(resource_type)}"
                        "分享链接"
                    )
                )
                invalid_links.append((index, reason))
                continue

            # 优先使用真实资源标题，以便 MoviePilot 刮削整理能准确识别集数、压制组与分辨率
            real_title = title_map.get(link)
            if not real_title and resource_type == "magnet" and magnet_info:
                real_title = (magnet_info.get("metadata") or {}).get("name")
            if not real_title:
                canonical_media_name = (media_target or {}).get("title")
                real_title = f"{canonical_media_name} - 资源 {index}" if canonical_media_name else f"手动添加 {index}"

            target_cloud_param = str((payload or {}).get("target_cloud") or "").strip().lower()
            metadata = resource_meta_by_url.get(link) or {}
            resources.append({
                "url": link,
                "title": real_title,
                "resource_type": metadata.get("resource_type") or resource_type,
                "source": metadata.get("source") or "manual",
                "unlock_points": 0,
                "skip_history": skip_history,
                "target_cloud": target_cloud_param,
                "is_cross": bool(target_cloud_param or (payload or {}).get("is_cross")),
                **{
                    key: metadata[key]
                    for key in (
                        "resource_ref", "provider_data", "media_page_url",
                        "is_unlocked", "preview_episodes", "target_season",
                        "target_episodes", "supports_file_preview",
                    )
                    if metadata.get(key) is not None
                },
                **(
                    {"magnet_metadata": magnet_info["metadata"]}
                    if resource_type == "magnet" and magnet_info else {}
                ),
            })
        if invalid_links:
            return {
                "success": False,
                "message": "；".join(
                    f"第 {index} 行资源无效：{reason}"
                    for index, reason in invalid_links
                ),
            }

        order = {value: index for index, value in enumerate(self._resource_type_order)}
        resources.sort(key=lambda item: order.get(item["resource_type"], len(order)))
        sync_kwargs = {
            "subscribe_id": subscribe_id or None,
            "manual_resources": resources,
            "manual_target": media_target,
            "manual_upgrade": bool((payload or {}).get("manual_upgrade")),
        }
        queue_media = media_target or {
            "title": getattr(subscribe, "name", "") if subscribe else "",
            "media_type": (
                "tv"
                if getattr(subscribe, "type", None) == MediaType.TV.value
                else "movie"
            ),
            "seasons": (
                [int(getattr(subscribe, "season", 1) or 1)]
                if subscribe
                   and getattr(subscribe, "type", None) == MediaType.TV.value
                else []
            ),
        }
        queue_title = str(queue_media.get("title") or "").strip()
        queue_seasons = self._positive_ints(queue_media.get("seasons") or [])
        season_text = (
            " " + "/".join(f"S{value:02d}" for value in queue_seasons)
            if queue_seasons else ""
        )
        queue_label = (
            f"手动添加资源：{queue_title}{season_text}"
            if queue_title else "手动添加资源"
        )
        if wait:
            result: Dict[str, Any] = {}
            future = self._submit_sync_operation(
                {**sync_kwargs, "result": result},
                queue_label,
            )
            future.result()
            data = dict(result.get("data") or {})
            data["resource_count"] = len(resources)
            if media_target:
                data["media"] = dict(media_target)
            result["data"] = data
            return result
        self._submit_sync_operation(
            sync_kwargs,
            queue_label,
        )
        return {
            "success": True,
            "message": (
                f"手动添加任务已提交，共 {len(resources)} 条资源"
                if subscribe_id
                else f"无订阅媒体任务已提交，共 {len(resources)} 条资源"
            ),
            "data": {
                **({"media": dict(media_target)} if media_target else {}),
            },
        }

    def start_selected_resources(
            self,
            subscribe_id: int,
            resources: list[Dict[str, Any]],
    ) -> dict:
        """将智能体会话缓存中的原始候选直接送入现有同步链。"""
        try:
            subscribe_id = int(subscribe_id or 0)
        except (TypeError, ValueError):
            subscribe_id = 0
        if subscribe_id <= 0:
            return {"success": False, "message": "请选择订阅"}
        with SessionFactory() as db:
            subscribe = SubscribeOper(db=db).get(subscribe_id)
        if not subscribe:
            return {"success": False, "message": "指定订阅不存在"}
        if subscribe.type not in {MediaType.TV.value, MediaType.MOVIE.value}:
            return {"success": False, "message": "仅支持电影或电视剧订阅"}

        selected = []
        for resource in list(resources or [])[:20]:
            item = dict(resource or {})
            resource_type = str(
                item.get("resource_type") or item.get("pan_type") or ""
            ).strip().lower()
            has_direct_url = bool(str(item.get("url") or "").strip())
            can_unlock = bool(
                item.get("need_unlock")
                and item.get("resource_ref")
            )
            if not has_direct_url and not can_unlock:
                continue
            item["resource_type"] = resource_type
            selected.append(item)
        if not selected:
            return {"success": False, "message": "没有可处理的候选资源"}

        self._submit_sync_operation(
            {
                "subscribe_id": subscribe_id,
                "manual_resources": selected,
            },
            f"候选资源处理：{subscribe.name}",
        )
        return {
            "success": True,
            "message": f"已提交 {len(selected)} 个候选资源，开始按现有规则处理",
            "data": {"submitted": len(selected)},
        }
