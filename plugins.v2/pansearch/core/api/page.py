"""配置页选项与详情页数据 API。"""

import copy
import datetime
import re
from urllib.parse import quote, urlencode

import pytz
from app.core.config import settings
from app.core.metainfo import MetaInfo
from app.log import logger
from app.schemas.types import MediaType, NotificationType

from .. import CloudDriveCapability, OwnerDelegator
from ..config import UIConfig
from ..media import call_with_supported_kwargs, recognize_media
from ...utils.cache import create_platform_ttl_cache

_UI_OPTIONS_CACHE = create_platform_ttl_cache(
    "ui:options", maxsize=16, ttl=2 * 60
)
_RESOURCE_RECOMMEND_CACHE = create_platform_ttl_cache(
    "resource:recommend", maxsize=64, ttl=15 * 60
)
_RESOURCE_DETAIL_CACHE = create_platform_ttl_cache(
    "resource:detail", maxsize=512, ttl=30 * 60
)


def _recognized_season_counts(recognized) -> dict[int, int]:
    """提取可缓存的季集总数，媒体库入库状态仍在每次打开时实时查询。"""
    season_counts: dict[int, int] = {}
    raw_seasons = getattr(recognized, "seasons", None) if recognized else None
    if isinstance(raw_seasons, dict):
        raw_seasons = raw_seasons.items()
    elif isinstance(raw_seasons, list):
        raw_seasons = [
            (
                item.get("season_number") or item.get("season"),
                item.get("episode_count")
                or item.get("total_episodes")
                or item.get("episodes"),
            )
            if isinstance(item, dict)
            else (
                getattr(item, "season_number", None)
                or getattr(item, "season", None),
                getattr(item, "episode_count", None)
                or getattr(item, "total_episodes", None)
                or getattr(item, "episodes", None),
            )
            for item in raw_seasons
        ]
    else:
        raw_seasons = []
    for season_number, episode_count in raw_seasons:
        try:
            season_number = int(season_number)
        except (TypeError, ValueError):
            continue
        if isinstance(episode_count, list):
            numbers = [int(item) for item in episode_count if str(item).isdigit()]
            episode_count = max(len(episode_count), max(numbers, default=0))
        try:
            episode_count = int(episode_count or 0)
        except (TypeError, ValueError):
            episode_count = 0
        if season_number > 0 and episode_count > 0:
            season_counts[season_number] = episode_count
    if not season_counts:
        try:
            total_episodes = int(
                getattr(recognized, "total_episodes", None) or 0
            )
        except (TypeError, ValueError):
            total_episodes = 0
        if total_episodes > 0:
            season_counts[1] = total_episodes
    return season_counts


def _platform_image_url(value: object, size: str = "w500") -> str:
    """使用 MoviePilot v2 图片代理生成前端可直接加载的地址。"""
    raw = str(value or "").strip()
    if not raw or raw.startswith(("data:", "blob:")):
        return raw
    if not raw.lower().startswith(("http://", "https://")):
        builder = getattr(settings, "TMDB_IMAGE_URL", None)
        raw = builder(raw, size) if callable(builder) else raw
    return f"/api/v1/system/img/1?cache=1&imgurl={quote(raw, safe='')}" if raw else ""


def _media_value(media: object, *names: str):
    """兼容平台媒体对象与原始字典，返回首个有效字段。"""
    for name in names:
        value = media.get(name) if isinstance(media, dict) else getattr(media, name, None)
        if value not in (None, "", 0, "0", [], {}):
            return value
    return None


def _merge_media_display(detail: dict, media: object, *, tmdb: bool = False) -> None:
    """合并媒体展示信息，避免只补齐跨平台 ID 而丢失详情字段。"""
    if not media:
        return
    aliases = {
        "vote_average": ("vote_average", "rating", "score"),
        "vote_count": ("vote_count", "votes"),
        "overview": ("overview", "description", "summary", "intro"),
        "genres": ("genres", "genre", "genre_ids"),
        "regions": ("regions", "origin_country", "countries", "production_countries"),
        "backdrop_path": ("backdrop_path", "backdrop", "backdrop_url"),
        "poster_path": ("poster_path", "poster", "poster_url"),
        "original_title": ("original_title", "original_name"),
    }
    for target, names in aliases.items():
        current_val = detail.get(target)
        # 如果当前字段为空，或者 overview 为仅包含空白的无效文本，允许被有效值覆盖
        if current_val in (None, "", 0, "0", [], {}) or (
                target == "overview" and isinstance(current_val, str) and not current_val.strip()
        ):
            value = _media_value(media, *names)
            if value not in (None, "", 0, "0", [], {}):
                if target == "overview" and isinstance(value, str):
                    value = value.strip()
                detail[target] = value

    # 中文标题与原始片名提取
    new_title = _media_value(media, "title", "name", "cn_title", "chinese_title")
    orig_title = _media_value(media, "original_title", "original_name")
    if orig_title and not detail.get("original_title"):
        detail["original_title"] = orig_title

    if new_title:
        curr_title = str(detail.get("title") or "").strip()
        has_curr_cn = bool(re.search(r'[\u4e00-\u9fa5]', curr_title))
        has_new_cn = bool(re.search(r'[\u4e00-\u9fa5]', str(new_title)))
        if has_new_cn:
            detail["cn_title"] = new_title
            if not has_curr_cn:
                if curr_title and not detail.get("original_title"):
                    detail["original_title"] = curr_title
                detail["title"] = new_title
        elif not curr_title:
            detail["title"] = new_title

    rating = detail.get("vote_average")
    if rating not in (None, "", 0, "0"):
        detail["rating"] = rating

    # 提取豆瓣评分与评价人数
    douban_rating = _media_value(media, "douban_rating")
    if douban_rating not in (None, "", 0, "0"):
        detail["douban_rating"] = douban_rating
    douban_votes = _media_value(media, "douban_vote_count", "douban_votes")
    if douban_votes not in (None, "", 0, "0"):
        detail["douban_vote_count"] = douban_votes

    # 提取 IMDb 评分
    imdb_rating = _media_value(media, "imdb_rating")
    if imdb_rating not in (None, "", 0, "0"):
        detail["imdb_rating"] = imdb_rating
    imdb_votes = _media_value(media, "imdb_vote_count", "imdb_votes")
    if imdb_votes not in (None, "", 0, "0"):
        detail["imdb_vote_count"] = imdb_votes

    if tmdb:
        tmdb_rating = _media_value(media, "tmdb_rating", "vote_average", "rating", "score")
        tmdb_votes = _media_value(media, "tmdb_vote_count", "vote_count", "votes")
        if tmdb_rating not in (None, "", 0, "0"):
            detail["tmdb_rating"] = tmdb_rating
        if tmdb_votes not in (None, "", 0, "0"):
            detail["tmdb_vote_count"] = tmdb_votes
    if detail.get("genres") in (None, "", []):
        detail["genres"] = detail.get("genre_ids") or []

    poster = detail.get("poster_path")
    backdrop = detail.get("backdrop_path")
    if poster and not detail.get("poster_url"):
        detail["poster_url"] = _platform_image_url(poster, "w500")
    if (backdrop or poster) and not detail.get("backdrop_url"):
        detail["backdrop_url"] = _platform_image_url(backdrop or poster, "w1280")


class PageApi(OwnerDelegator):
    @staticmethod
    def _history_filter_values(value: str) -> set[str]:
        return {
            item.strip().lower()
            for item in str(value or "").split(",")
            if item.strip()
        }

    def api_vue_page_data(
            self,
            page: int = 1,
            page_size: int = 10,
            keyword: str = "",
            resource_types: str = "",
            sources: str = "",
            task_types: str = "",
            statuses: str = "",
    ) -> dict:
        page_result = self._get_data_store().query_history_page(
            page=page,
            page_size=page_size,
            keyword=keyword,
            resource_types=self._history_filter_values(resource_types),
            sources=self._history_filter_values(sources),
            task_types=self._history_filter_values(task_types),
            statuses=self._history_filter_values(statuses),
        )
        page_groups = page_result.pop("groups", [])
        page_history = [
            record for group in page_groups
            for record in group.get("records", [])
        ]
        if self._sync_handler:
            page_history = self._sync_handler.prepare_history_records(
                page_history
            )
            prepared_by_id = {
                str(record.get("record_id") or ""): record
                for record in page_history
            }
            history_groups = []
            for group in page_groups:
                group["records"] = [
                    prepared_by_id.get(
                        str(record.get("record_id") or ""), record
                    )
                    for record in group.get("records", [])
                ]
                history_groups.append(
                    self._sync_handler.prepare_history_group(group)
                )
        else:
            page_history = [copy.deepcopy(item) for item in page_history]
            history_groups = [copy.deepcopy(group) for group in page_groups]
        return {
            "success": True,
            "data": {
                "history_groups": history_groups,
                "history_page": {
                    "page": page_result["page"],
                    "page_size": page_result["page_size"],
                    "total": page_result["total"],
                    "total_pages": page_result["total_pages"],
                    "filter_options": page_result["filter_options"],
                    "enable_cloud_upgrade": bool(
                        getattr(self, "_enable_cloud_upgrade", False)
                    ),
                },
                "emby_play_items": self._history_emby_play_items(page_history),
            },
        }

    def api_vue_history_summary(self) -> dict:
        today = datetime.datetime.now(
            tz=pytz.timezone(settings.TZ)
        ).strftime("%Y-%m-%d")
        return {
            "success": True,
            "data": self._get_data_store().history_summary(today),
        }

    def api_vue_ui_options(self, scope: str = "base") -> dict:
        normalized_scope = str(scope or "base").strip().lower()
        normalized_scope = {
            "transfer": "subscriptions",
            "upgrade": "subscriptions",
            "manual": "subscriptions",
        }.get(normalized_scope, normalized_scope)
        if normalized_scope not in {
            "base", "subscriptions", "drive", "search", "notify"
        }:
            return {"success": False, "message": "未知的配置选项范围"}
        cache_key = f"instance:{id(self)}:{normalized_scope}"
        cached = _UI_OPTIONS_CACHE.get(cache_key)
        if isinstance(cached, dict):
            return copy.deepcopy(cached)

        if normalized_scope == "subscriptions":
            providers = (
                self._cloud_drive_registry.available()
                if self._cloud_drive_registry else []
            )
            target_key = str(
                getattr(self._cloud_drive, "key", "")
                or getattr(self, "_cloud_drive_key", "")
            ).strip().lower()
            target_accepts_cross_transfer = bool(
                self._cloud_drive
                and self._cloud_drive.supports(CloudDriveCapability.LOCAL_UPLOAD)
                and self._cloud_drive.supports(CloudDriveCapability.FILE_QUERY)
            )
            cloud_drives = []
            for provider in providers:
                if not provider.supports(CloudDriveCapability.DIRECTORY_READ):
                    continue
                direct = provider.key == target_key
                cross = bool(
                    not direct
                    and bool(getattr(self, "_cross_transfer_enabled", False))
                    and target_accepts_cross_transfer
                    and provider.supports(CloudDriveCapability.FILE_QUERY)
                    and provider.supports(CloudDriveCapability.FILE_DOWNLOAD)
                )
                if not direct and not cross:
                    continue
                cloud_drives.append({
                    "title": provider.name,
                    "value": provider.key,
                    "mode": "direct" if direct else "cross",
                })
            result = {
                "success": True,
                "data": {
                    "subscribes": UIConfig.get_subscribe_options_grouped(),
                    "cloud_drives": cloud_drives,
                    "target_cloud_drive": target_key,
                    "enable_cloud_upgrade": bool(
                        getattr(self, "_enable_cloud_upgrade", False)
                    ),
                    "cross_transfer_media_types": sorted(
                        str(value) for value in getattr(
                            self, "_cross_transfer_media_types", set()
                        )
                    ),
                },
            }
            _UI_OPTIONS_CACHE.set(cache_key, copy.deepcopy(result))
            return result

        if normalized_scope in {"base", "drive"}:
            providers = (
                self._cloud_drive_registry.available()
                if self._cloud_drive_registry else []
            )
            if normalized_scope == "base":
                result = {
                    "success": True,
                    "data": {
                        "defaults": UIConfig.get_default_config(),
                        "cloud_drives": [
                            {
                                "title": provider.name,
                                "value": provider.key,
                                "capabilities": sorted(
                                    capability.value
                                    for capability in provider.capabilities
                                ),
                                "resource_types": sorted(provider.resource_types),
                                "policy": {
                                    "pagination_mode": provider.policy.pagination_mode,
                                    "max_page_size": provider.policy.max_page_size,
                                    "supports_batch": provider.policy.supports_batch,
                                    "max_batch_size": provider.policy.max_batch_size,
                                    "supports_cancel": provider.policy.supports_cancel,
                                    "max_concurrency": provider.policy.max_concurrency,
                                    "cache_ttl_seconds": dict(
                                        provider.policy.cache_ttl_seconds
                                    ),
                                },
                            }
                            for provider in providers
                        ],
                    },
                }
            else:
                accounts = {}
                for provider in providers:
                    if not provider.supports(CloudDriveCapability.ACCOUNT):
                        continue
                    accounts[provider.key] = self._cached_account_info(
                        f"drive:{provider.key}",
                        {
                            "connected": False,
                            "error": "点击刷新按钮读取账户信息",
                        },
                    )
                result = {
                    "success": True,
                    "data": {
                        "account": accounts.get(self._cloud_drive_key, {
                            "connected": False,
                            "error": "请先配置当前网盘账号",
                        }),
                        "accounts": accounts,
                    },
                }
            _UI_OPTIONS_CACHE.set(cache_key, copy.deepcopy(result))
            return result

        if normalized_scope == "notify":
            mediaservers = UIConfig.get_media_server_options()
            result = {
                "success": True,
                "data": {
                    "mediaservers": mediaservers,
                    "media_library_webhook_urls": {
                        str(item.get("value") or ""): (
                                "/api/v1/webhook/?"
                                + urlencode({
                            "token": settings.API_TOKEN,
                            "source": str(item.get("value") or ""),
                        })
                        )
                        for item in mediaservers
                        if str(item.get("type") or "").strip().lower() == "emby"
                           and str(item.get("value") or "").strip()
                    },
                    "notification_types": [
                        {"title": item.value, "value": item.name}
                        for item in NotificationType
                    ],
                },
            }
            _UI_OPTIONS_CACHE.set(cache_key, copy.deepcopy(result))
            return result

        from ...search.pansou import PanSouClient
        from ...search.types import PANSOU_RESOURCE_TYPES, resource_type_name

        search_accounts = {
            "hdhive": {
                "connected": False,
                "error": "配置并保存 HDHive 账户后读取账户信息",
            },
            "dian115": {
                "connected": False,
                "error": "配置并保存 Dian115 账户后读取账户信息",
            },
            "juying": {
                "connected": False,
                "error": "配置并保存聚影账户后读取账户信息",
            },
            "pinglian": {
                "connected": False,
                "error": "配置并保存盘链账户后读取账户信息",
            },
        }

        for source in search_accounts:
            search_accounts[source] = self._cached_account_info(
                f"search:{source}", search_accounts[source]
            )
        pansou_options = {
            "status": "unavailable",
            "plugins": [],
            "channels": [],
            "cloud_types": [
                {
                    "title": resource_type_name(value, value),
                    "value": value,
                }
                for value in PANSOU_RESOURCE_TYPES
            ],
        }
        pansou_url = str(getattr(self, "_pansou_url", "") or "").strip()
        if pansou_url:
            client = self._pansou_client or PanSouClient(
                base_url=pansou_url,
                auth_enabled=False,
                proxy=self._search_proxy,
                search_timeout=5,
            )
            health = client.health(timeout=3)
            pansou_options.update({
                "status": str(health.get("status") or "error"),
                "error": str(health.get("error") or ""),
                "plugins": [
                    {"title": value, "value": value}
                    for value in health.get("plugins", [])
                ],
                "channels": [
                    {"title": value, "value": value}
                    for value in health.get("channels", [])
                ],
            })
        result = {
            "success": True,
            "data": {
                "search_accounts": search_accounts,
                "pansou": pansou_options,
            },
        }
        _UI_OPTIONS_CACHE.set(cache_key, copy.deepcopy(result))
        return result

    def api_vue_cloud_directories(
            self, path: str = "/", provider: str = "", refresh: bool = False
    ) -> dict:
        """列出指定或当前网盘目录，供配置页选择转存路径。"""
        normalized_path = str(path or "/").strip()
        if not normalized_path.startswith("/"):
            normalized_path = f"/{normalized_path}"
        normalized_path = normalized_path.rstrip("/") or "/"
        drive = self._cloud_drive
        provider_key = str(provider or "").strip().lower()
        if provider_key and self._cloud_drive_registry:
            try:
                drive = self._cloud_drive_registry.get(provider_key)
            except KeyError:
                return {"success": False, "message": "网盘提供方不存在"}
        if not drive or not drive.supports(
                CloudDriveCapability.DIRECTORY_READ
        ):
            return {"success": False, "message": "当前网盘不支持目录浏览"}
        try:
            service = drive.require(CloudDriveCapability.DIRECTORY_READ)
            if refresh:
                service.refresh_directories()
            directories = service.list_directories(normalized_path)
            breadcrumbs = [{"name": "根目录", "path": "/"}]
            current_path = ""
            for part in (item for item in normalized_path.split("/") if item):
                current_path = f"{current_path}/{part}"
                breadcrumbs.append({"name": part, "path": current_path})
            return {
                "success": True,
                "data": {
                    "path": normalized_path,
                    "breadcrumbs": breadcrumbs,
                    "directories": directories,
                },
            }
        except Exception as error:
            logger.error(f"读取网盘目录失败：{normalized_path}，{error}")
            return {"success": False, "message": f"读取网盘目录失败：{error}"}

    def api_vue_create_cloud_directory(self, payload: dict) -> dict:
        """在目录选择器当前目录创建子文件夹。"""
        request = payload or {}
        path = request.get("path", "/")
        name = request.get("name", "")
        provider = request.get("provider", "")
        normalized_path = str(path or "/").strip() or "/"
        if not normalized_path.startswith("/"):
            normalized_path = f"/{normalized_path}"
        normalized_path = normalized_path.rstrip("/") or "/"
        folder_name = str(name or "").strip()
        if (
                not folder_name
                or folder_name in {".", ".."}
                or "/" in folder_name
                or "\\" in folder_name
        ):
            return {"success": False, "message": "文件夹名称无效"}
        drive = self._cloud_drive
        provider_key = str(provider or "").strip().lower()
        if provider_key and self._cloud_drive_registry:
            try:
                drive = self._cloud_drive_registry.get(provider_key)
            except KeyError:
                return {"success": False, "message": "网盘提供方不存在"}
        if not drive or not drive.supports(CloudDriveCapability.DIRECTORY_READ):
            return {"success": False, "message": "当前网盘不支持目录操作"}
        try:
            service = drive.require(CloudDriveCapability.DIRECTORY_READ)
            target_path = f"{normalized_path.rstrip('/')}/{folder_name}" if normalized_path != "/" else f"/{folder_name}"
            lookup = service.resolve_directory(target_path, create=True)
            if not lookup.checked or lookup.directory_id is None:
                return {"success": False, "message": "创建文件夹失败"}
            return {"success": True, "data": {"path": target_path}}
        except Exception as error:
            logger.error(f"创建网盘目录失败：{target_path if 'target_path' in locals() else folder_name}，{error}")
            return {"success": False, "message": f"创建文件夹失败：{error}"}

    def api_vue_resource_recommend(
            self,
            source: str = "tmdb_trending",
            page: int = 1,
            count: int = 30,
            force: bool = False,
            resolve_identity: bool = False,
    ) -> dict:
        """获取平台推荐榜单媒体列表（供网盘资源页面展示，支持平台级 TTL 缓存）。"""
        source_key = str(source or "tmdb_trending").strip().lower()
        page = max(1, int(page or 1))
        count = max(10, min(60, int(count or 24)))
        cache_key = f"{source_key}:{page}:{count}:identity={int(bool(resolve_identity))}"

        if not force:
            cached = _RESOURCE_RECOMMEND_CACHE.get(cache_key)
            if cached is not None and isinstance(cached, dict):
                return copy.deepcopy(cached)

        from app.chain.recommend import RecommendChain
        chain = RecommendChain()
        media_chain = self.chain
        native_paged = False

        def _call(func):
            nonlocal native_paged
            if not func:
                return []
            try:
                res = func(page=page, count=count)
                native_paged = True
                return res or []
            except TypeError:
                try:
                    res = func(page=page)
                    native_paged = True
                    return res or []
                except TypeError:
                    return func() or []

        # 针对不同榜单源精准传参，避免异常重试
        items = []
        try:
            if source_key == "tmdb_trending":
                items = chain.tmdb_trending(page=page) or []
                native_paged = True
            elif source_key == "douban_movie_showing":
                items = chain.douban_movie_showing(page=page, count=count) or []
                native_paged = True
            elif source_key == "douban_movie_hot":
                items = chain.douban_movie_hot(page=page, count=count) or []
                native_paged = True
            elif source_key == "douban_tv_hot":
                items = chain.douban_tv_hot(page=page, count=count) or []
                native_paged = True
            elif source_key == "bangumi_calendar":
                items = chain.bangumi_calendar(page=page, count=count) or []
                native_paged = True
            elif source_key == "anilist_popular":
                from app.chain.anilist import AniListChain
                items = AniListChain().popular_this_season() or []
                native_paged = False
            else:
                method = getattr(chain, source_key, None)
                if method:
                    try:
                        items = method(page=page, count=count) or []
                        native_paged = True
                    except TypeError:
                        try:
                            items = method(page=page) or []
                            native_paged = True
                        except TypeError:
                            items = method() or []
                            native_paged = False
        except Exception as error:
            logger.error(f"获取推荐榜单失败 [{source_key}]：{error}")

        # 对于底层一次性返回列表的数据源，按 page 与 count 切片分页
        has_more = True
        if not native_paged and items:
            total_items = len(items)
            start_idx = (page - 1) * count
            end_idx = start_idx + count
            items = items[start_idx:end_idx]
            has_more = end_idx < total_items
        elif native_paged:
            has_more = len(items) >= (count if source_key != "tmdb_trending" else 20)
        else:
            has_more = False

        media_items = []
        for item in items:
            if hasattr(item, "to_dict"):
                data = item.to_dict()
            elif hasattr(item, "dict"):
                data = item.dict()
            elif hasattr(item, "model_dump"):
                data = item.model_dump()
            elif isinstance(item, dict):
                data = item
            else:
                data = getattr(item, "__dict__", {}) or {}

            title = str(
                data.get("title") or data.get("name")
                or getattr(item, "title", None) or getattr(item, "name", None) or ""
            ).strip()
            if not title:
                continue

            image_data = data.get("images") if isinstance(data.get("images"), dict) else {}

            def _image_value(*values):
                for value in values:
                    if isinstance(value, dict):
                        value = value.get("url") or value.get("src") or value.get("large") or value.get(
                            "medium") or value.get("small")
                    if value:
                        return str(value).strip()
                return ""

            poster_path = str(
                _image_value(
                    data.get("poster_path"), data.get("poster"), data.get("pic_url"),
                    data.get("cover"), data.get("image"), data.get("image_url"),
                    data.get("cover_url"), image_data.get("poster"),
                    image_data.get("poster_path"), image_data.get("large"),
                    image_data.get("medium"), image_data.get("small"),
                    getattr(item, "poster_path", None), getattr(item, "poster", None),
                    getattr(item, "pic_url", None), getattr(item, "cover", None),
                )
            )
            backdrop_path = str(
                _image_value(
                    data.get("backdrop_path"), data.get("backdrop"), data.get("fanart"),
                    data.get("banner"), data.get("backdrop_url"), image_data.get("backdrop"),
                    image_data.get("backdrop_path"), getattr(item, "backdrop_path", None),
                    getattr(item, "backdrop", None), getattr(item, "fanart", None),
                )
            )
            try:
                vote_average = float(
                    data.get("vote_average") or data.get("rating") or data.get("score")
                    or getattr(item, "vote_average", 0) or getattr(item, "rating", 0)
                    or getattr(item, "score", 0) or 0.0
                )
            except (TypeError, ValueError):
                vote_average = 0.0
            media_type = str(
                data.get("type") or data.get("media_type")
                or getattr(item, "type", None) or getattr(item, "media_type", None)
                or "movie"
            ).lower()
            if media_type in {"电视剧", "series", "show", "television", "anime"}:
                media_type = "tv"
            elif media_type not in {"movie", "tv"}:
                media_type = "movie"

            media_items.append({
                "title": title,
                "original_title": str(
                    data.get("original_title") or data.get("original_name")
                    or getattr(item, "original_title", None) or getattr(item, "original_name", None) or ""
                ).strip(),
                "year": str(data.get("year") or getattr(item, "year", None) or "").strip(),
                "media_type": media_type,
                "rating": vote_average,
                "vote_average": vote_average,
                "poster": poster_path,
                "poster_path": poster_path,
                "poster_url": _platform_image_url(poster_path, "w500"),
                "backdrop": backdrop_path,
                "backdrop_path": backdrop_path,
                "backdrop_url": _platform_image_url(
                    backdrop_path or poster_path,
                    "w1280",
                ),
                "overview": str(
                    data.get("overview") or data.get("description")
                    or getattr(item, "overview", None) or getattr(item, "description", None) or ""
                ).strip(),
                "category": data.get("category") or data.get("metadata_category") or data.get(
                    "media_category") or getattr(item, "category", None) or "",
                "genres": data.get("genres") or data.get("genre") or getattr(item, "genres", None) or [],
                "regions": data.get("regions") or data.get("origin_country") or data.get("countries") or data.get(
                    "production_countries") or getattr(item, "origin_country", None) or [],
                "tmdb_id": data.get("tmdb_id") or getattr(item, "tmdb_id", None),
                "douban_id": data.get("douban_id") or data.get("doubanid") or getattr(item, "douban_id", None),
                "douban_rating": (
                    vote_average
                    if (data.get("douban_id") or data.get("doubanid") or getattr(item, "douban_id",
                                                                                 None) or source_key.startswith(
                        "douban_")) and vote_average > 0
                    else (data.get("douban_rating") or getattr(item, "douban_rating", None))
                ),
                "douban_vote_count": data.get("douban_vote_count") or data.get("douban_votes") or (
                    data.get("vote_count") if source_key.startswith("douban_") else None),
                "tmdb_rating": (
                    vote_average
                    if source_key.startswith("tmdb_") and vote_average > 0
                    else (data.get("tmdb_rating") or getattr(item, "tmdb_rating", None))
                ),
                "bangumi_id": data.get("bangumi_id") or getattr(item, "bangumi_id", None),
                "anilist_id": data.get("anilist_id") or getattr(item, "anilist_id", None),
                "imdb_id": data.get("imdb_id") or data.get("imdbid") or getattr(item, "imdb_id", None),
                "tvdb_id": data.get("tvdb_id") or data.get("tvdbid") or getattr(item, "tvdb_id", None),
                "anidb_id": data.get("anidb_id") or data.get("anidbid") or getattr(item, "anidb_id", None),
                "media_source": data.get("media_source") or getattr(item, "media_source", None),
                "media_id": data.get("media_id") or data.get("mediaid") or getattr(item, "media_id", None),
            })

        # 非 TMDB 榜单只携带来源原生 ID，交由平台媒体识别链补齐完整身份。
        source_aliases = {
            "tmdb_trending": "themoviedb",
            "douban_movie_showing": "douban",
            "douban_movie_hot": "douban",
            "douban_tv_hot": "douban",
            "bangumi_calendar": "bangumi",
            "anilist_popular": "anilist",
        }
        identity_cache = {}
        for media_item in media_items if resolve_identity else []:
            source_alias = source_aliases.get(source_key)
            if not source_alias:
                if source_key.startswith("douban_"):
                    source_alias = "douban"
                elif source_key.startswith("bangumi"):
                    source_alias = "bangumi"
                elif source_key.startswith("anilist"):
                    source_alias = "anilist"
            source_name = str(
                media_item.get("media_source") or source_alias or ""
            ).strip().lower()
            source_name = {
                "tmdb": "themoviedb",
                "themoviedb": "themoviedb",
            }.get(source_name, source_name)
            source_id = media_item.get("media_id")
            if not source_id:
                source_id = {
                    "douban": media_item.get("douban_id"),
                    "bangumi": media_item.get("bangumi_id"),
                    "anilist": media_item.get("anilist_id"),
                    "themoviedb": media_item.get("tmdb_id"),
                }.get(source_name)
            if not source_name or not source_id:
                continue

            cache_key = (source_name, str(source_id))
            if cache_key not in identity_cache:
                try:
                    meta_obj = MetaInfo(str(media_item.get("title") or ""))
                    media_type = (
                        MediaType.TV
                        if media_item.get("media_type") == "tv"
                        else MediaType.MOVIE
                    )
                    identity_cache[cache_key] = recognize_media(
                        media_chain,
                        meta=meta_obj,
                        mtype=media_type,
                        media_source=source_name,
                        media_id=str(source_id),
                        cache=True,
                    )
                except Exception as error:
                    logger.debug(
                        f"补充推荐媒体身份失败 [{source_name}:{source_id}]：{error}"
                    )
                    identity_cache[cache_key] = None

            recognized = identity_cache.get(cache_key)
            if not recognized:
                continue
            for field in (
                    "tmdb_id", "imdb_id", "tvdb_id", "douban_id",
                    "bangumi_id", "anilist_id", "anidb_id",
            ):
                value = getattr(recognized, field, None)
                if value and not media_item.get(field):
                    media_item[field] = value
            for field in ("category", "genres", "regions"):
                if not media_item.get(field):
                    value = getattr(recognized, field, None)
                    if value:
                        media_item[field] = value
            if not media_item.get("media_source"):
                recognized_source = getattr(recognized, "media_source", None)
                media_item["media_source"] = getattr(
                    recognized_source, "value", recognized_source
                )
            if not media_item.get("media_id"):
                media_item["media_id"] = getattr(recognized, "media_id", None)

        # 2. 榜单卡片和详情共用这份媒体库摘要（按 TMDB、豆瓣、Bangumi 联合检索）
        library_by_tmdb = {}
        library_by_douban = {}
        library_by_bangumi = {}
        try:
            from .media_library import MediaLibraryApi
            library_api = self._get_component(MediaLibraryApi)
            library_by_tmdb = library_api.lookup_media_library(
                [
                    {
                        "tmdb_id": item.get("tmdb_id"),
                        "media_type": item.get("media_type"),
                    }
                    for item in media_items
                    if str(item.get("tmdb_id") or "").isdigit()
                ]
            )
            library_by_douban = library_api.lookup_media_library_by_douban(
                [item.get("douban_id") for item in media_items if item.get("douban_id")]
            )
            library_by_bangumi = library_api.lookup_media_library_by_bangumi(
                [item.get("bangumi_id") for item in media_items if item.get("bangumi_id")]
            )
        except Exception as error:
            logger.debug(f"读取推荐榜单媒体库状态失败：{error}")

        for media_item in media_items:
            matches = library_by_tmdb.get(str(media_item.get("tmdb_id")), [])
            if not matches:
                matches = library_by_douban.get(str(media_item.get("douban_id")), [])
            if not matches:
                matches = library_by_bangumi.get(str(media_item.get("bangumi_id")), [])

            media_item["in_library"] = bool(matches)
            media_item["library_items"] = matches
            media_item["library_servers"] = sorted({
                str(entry.get("server") or "媒体服务器") for entry in matches
            })

            # 后端处理完成：为电视剧组装标准化的各季连续数字集列表与入库标记（含未入库官方总集数）
            if media_item.get("media_type") == "tv":
                meta_seasons_info = {}
                try:
                    raw_seasons = media_item.pop("_rec_seasons", None)
                    tot_ep = media_item.pop("_rec_total_episodes", None)
                    if not raw_seasons and not tot_ep:
                        t_id = media_item.get("tmdb_id")
                        if t_id and str(t_id).isdigit():
                            meta_obj = MetaInfo(str(media_item.get("title") or ""))
                            meta_obj.type = MediaType.TV
                            m_info = recognize_media(
                                media_chain,
                                meta=meta_obj,
                                mtype=MediaType.TV,
                                tmdb_id=int(t_id),
                                cache=True,
                            )
                            if m_info:
                                raw_seasons = getattr(m_info, "seasons", None)
                                tot_ep = getattr(m_info, "total_episodes", None)
                            if isinstance(raw_seasons, dict):
                                for s_k, s_v in raw_seasons.items():
                                    try:
                                        meta_seasons_info[int(s_k)] = s_v
                                    except Exception:
                                        pass
                            elif isinstance(raw_seasons, list):
                                for s_item in raw_seasons:
                                    if isinstance(s_item, dict):
                                        s_n = s_item.get("season_number") if s_item.get(
                                            "season_number") is not None else s_item.get("season")
                                        ep_c = s_item.get("episode_count") or s_item.get(
                                            "total_episodes") or s_item.get("episodes")
                                    else:
                                        s_n = getattr(s_item, "season_number", None) or getattr(s_item, "season", None)
                                        ep_c = getattr(s_item, "episode_count", None) or getattr(s_item,
                                                                                                 "total_episodes",
                                                                                                 None) or getattr(
                                            s_item, "episodes", None)
                                    if s_n is not None:
                                        try:
                                            meta_seasons_info[int(s_n)] = ep_c
                                        except Exception:
                                            pass
                            # 若各季未列出具体集数，尝试全局总集数作为兜底
                            if not meta_seasons_info:
                                tot_ep = getattr(m_info, "total_episodes", None) or getattr(m_info, "episodes_count",
                                                                                            None)
                                if tot_ep and str(tot_ep).isdigit() and int(tot_ep) > 0:
                                    meta_seasons_info[1] = int(tot_ep)
                except Exception:
                    pass

                # 从标题中智能识别季数（如《碧蓝之海 第三季》=> 3）
                raw_title_for_s = str(media_item.get("title") or media_item.get("name") or "")
                title_s_num = 0
                import re
                cn_num_map = {
                    "一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
                    "十一": 11, "十二": 12, "十三": 13, "十四": 14, "十五": 15
                }
                m_cn = re.search(r'第\s*([0-9一二三四五六七八九十]+)\s*[季部期]', raw_title_for_s)
                if m_cn:
                    val_str = m_cn.group(1).strip()
                    if val_str.isdigit():
                        title_s_num = int(val_str)
                    elif val_str in cn_num_map:
                        title_s_num = cn_num_map[val_str]
                if not title_s_num:
                    m_en = re.search(r'\b(?:Season|S)\s*0*([1-9]\d?)\b', raw_title_for_s, re.IGNORECASE)
                    if m_en:
                        title_s_num = int(m_en.group(1))

                # 若从标题明确识别出季数（>1），且 meta_seasons_info 仅有第 1 季（通常为独立分季条目），进行校正
                if title_s_num > 1:
                    if not meta_seasons_info:
                        pass
                    elif list(meta_seasons_info.keys()) == [1]:
                        meta_seasons_info = {title_s_num: meta_seasons_info[1]}

                # 豆瓣/外部榜单的单季总集数字段兜底
                if not meta_seasons_info:
                    ext_eps = media_item.get("episodes_count") or media_item.get("total_episodes")
                    if ext_eps and str(ext_eps).isdigit() and int(ext_eps) > 0:
                        meta_seasons_info[title_s_num or 1] = int(ext_eps)

                seasons_dict = {}  # { season_num: set(episodes) }
                for entry in matches:
                    season_info = entry.get("seasoninfo") or {}
                    if isinstance(season_info, str):
                        try:
                            import json
                            season_info = json.loads(season_info)
                        except Exception:
                            season_info = {}
                    if isinstance(season_info, dict):
                        for s_key, eps in season_info.items():
                            try:
                                s_num = int(s_key)
                            except (ValueError, TypeError):
                                s_num = 1
                            if s_num not in seasons_dict:
                                seasons_dict[s_num] = set()
                            if isinstance(eps, list):
                                for ep in eps:
                                    try:
                                        ep_num = int(ep)
                                        if ep_num > 0:
                                            seasons_dict[s_num].add(ep_num)
                                    except (ValueError, TypeError):
                                        continue
                            elif isinstance(eps, int) and eps > 0:
                                seasons_dict[s_num].add(eps)

                # 检查是否存在离散跳跃季号（如死神 TVDB 分类导致的千年血战篇被标为第 17 季）：
                raw_lib_seasons = sorted([k for k in seasons_dict.keys() if k > 0])
                if len(raw_lib_seasons) >= 2:
                    has_huge_jump = any(
                        raw_lib_seasons[i] - raw_lib_seasons[i - 1] >= 3 for i in range(1, len(raw_lib_seasons)))
                    meta_max_s = max(meta_seasons_info.keys()) if meta_seasons_info else len(raw_lib_seasons)
                    if has_huge_jump and raw_lib_seasons[-1] > max(meta_max_s, len(raw_lib_seasons)):
                        normalized_seasons_dict = {}
                        for idx, old_s in enumerate(raw_lib_seasons):
                            new_s = idx + 1
                            normalized_seasons_dict[new_s] = seasons_dict.get(old_s, set())
                        seasons_dict = normalized_seasons_dict

                all_season_nums = set(seasons_dict.keys())
                if isinstance(meta_seasons_info, dict):
                    for s_k in meta_seasons_info.keys():
                        try:
                            s_int = int(s_k)
                            if s_int > 0:
                                all_season_nums.add(s_int)
                        except (ValueError, TypeError):
                            pass

                # 若未入库且标题带季数，修正单季的季号
                if title_s_num > 1 and not seasons_dict:
                    if not all_season_nums or all_season_nums == {1}:
                        all_season_nums = {title_s_num}

                if not all_season_nums:
                    all_season_nums.add(1)

                seasons_list = []
                for s_num in sorted(all_season_nums):
                    in_eps = seasons_dict.get(s_num, set())
                    meta_eps_raw = None
                    if isinstance(meta_seasons_info, dict):
                        meta_eps_raw = meta_seasons_info.get(s_num) or meta_seasons_info.get(str(s_num))

                    official_total = 0
                    if isinstance(meta_eps_raw, list):
                        official_total = len(meta_eps_raw)
                        if meta_eps_raw and isinstance(meta_eps_raw[0], int):
                            official_total = max(official_total, max(meta_eps_raw))
                    elif isinstance(meta_eps_raw, int) and meta_eps_raw > 0:
                        official_total = meta_eps_raw

                    max_ep = max(max(in_eps) if in_eps else 0, official_total)
                    if max_ep < 1:
                        max_ep = 1

                    episodes_list = []
                    for ep_idx in range(1, max_ep + 1):
                        episodes_list.append({
                            "episode": ep_idx,
                            "in_library": ep_idx in in_eps,
                        })
                    seasons_list.append({
                        "season_number": s_num,
                        "season_name": f"第 {s_num} 季",
                        "total_episodes": len(episodes_list),
                        "library_episodes_count": len(in_eps),
                        "episodes": episodes_list,
                    })

                media_item["seasons"] = seasons_list
                media_item["library_episodes_total"] = sum(len(eps) for eps in seasons_dict.values())
                has_missing = any(
                    any(not ep.get("in_library") for ep in s.get("episodes", []))
                    for s in seasons_list
                )
                media_item["has_missing_episodes"] = has_missing
            else:
                media_item["seasons"] = []
                media_item["library_episodes_total"] = 0
                media_item["has_missing_episodes"] = False

        result = {
            "success": True,
            "data": {
                "source": source_key,
                "page": page,
                "count": len(media_items),
                "has_more": has_more,
                "items": media_items,
            },
        }
        if media_items:
            _RESOURCE_RECOMMEND_CACHE.set(cache_key, copy.deepcopy(result))
        return result

    def api_vue_resource_detail(self, payload: dict) -> dict:
        """在打开资源详情时补全媒体身份和电视剧季集信息。"""
        payload = dict(payload or {})
        title = str(payload.get("title") or payload.get("name") or "").strip()
        if not title:
            return {"success": False, "message": "媒体标题不能为空"}

        media_type = (
            MediaType.TV
            if str(payload.get("media_type") or "").strip().lower() == "tv"
            else MediaType.MOVIE
        )
        year = str(payload.get("year") or "").strip() or None
        meta = MetaInfo(title)
        meta.type = media_type
        if year:
            meta.year = year
        for canonical, aliases in {
            "tmdb_id": ("tmdbid",),
            "douban_id": ("doubanid",),
            "bangumi_id": ("bangumiid",),
            "anilist_id": ("anilistid",),
            "imdb_id": ("imdbid",),
            "tvdb_id": ("tvdbid",),
            "anidb_id": ("anidbid",),
        }.items():
            if payload.get(canonical) in (None, "", 0, "0"):
                for alias in aliases:
                    value = payload.get(alias)
                    if value not in (None, "", 0, "0"):
                        payload[canonical] = value
                        break
        source = str(payload.get("media_source") or "").strip().lower()
        source = {
            "tmdb": "themoviedb",
            "themoviedb": "themoviedb",
            "tmdbid": "themoviedb",
            "doubanid": "douban",
            "bangumiid": "bangumi",
            "anilistid": "anilist",
            "imdbid": "imdb",
            "tvdbid": "tvdb",
        }.get(source, source)
        source_id = payload.get("media_id") or payload.get("mediaid")
        source_fields = {
            "themoviedb": ("tmdb_id", "tmdbid"),
            "douban": ("douban_id", "doubanid"),
            "bangumi": ("bangumi_id", "bangumiid"),
            "anilist": ("anilist_id", "anilistid"),
        }
        if not source or not source_id:
            for candidate_source, fields in source_fields.items():
                for field in fields:
                    value = payload.get(field)
                    if value not in (None, "", 0, "0"):
                        source = candidate_source
                        source_id = value
                        break
                if source and source_id:
                    break

        detail = copy.deepcopy(payload)
        detail_cache_key = {
            "version": 2,
            "source": source,
            "source_id": str(source_id or ""),
            "title": title.casefold(),
            "year": year or "",
            "media_type": "tv" if media_type == MediaType.TV else "movie",
        }
        cached_detail = _RESOURCE_DETAIL_CACHE.get(detail_cache_key)
        season_counts: dict[int, int] = {}
        recognized = None
        if cached_detail is not None:
            detail.update(copy.deepcopy(cached_detail.get("identity") or {}))
            season_counts = {
                int(key): int(value)
                for key, value in (cached_detail.get("season_counts") or {}).items()
                if str(key).isdigit() and str(value).isdigit()
            }
        elif source and source_id:
            try:
                recognized = recognize_media(
                    self.chain,
                    meta=meta,
                    mtype=media_type,
                    media_source=source,
                    media_id=str(source_id),
                    cache=True,
                )
            except Exception as error:
                logger.warning(f"详情媒体身份识别失败 [{source}:{source_id}]：{error}")

        if recognized:
            for field in (
                    "tmdb_id", "imdb_id", "tvdb_id", "douban_id",
                    "bangumi_id", "anilist_id", "anidb_id",
            ):
                value = getattr(recognized, field, None)
                if value not in (None, "", 0, "0"):
                    detail[field] = value
            _merge_media_display(detail, recognized, tmdb=source == "themoviedb")
            recognized_source = getattr(recognized, "media_source", None)
            detail["media_source"] = getattr(
                recognized_source, "value", recognized_source
            ) or source
            detail["media_id"] = getattr(recognized, "media_id", None) or str(source_id)

        # 保留豆瓣原生评分，避免转为 themoviedb 来源后丢失
        douban_id = detail.get("douban_id") or payload.get("douban_id") or payload.get("doubanid")
        if douban_id:
            detail["douban_id"] = douban_id
            if detail.get("douban_rating") in (None, "", 0, "0"):
                initial_rating = (
                        payload.get("douban_rating")
                        or (payload.get("vote_average") if source == "douban" else None)
                        or (payload.get("rating") if source == "douban" else None)
                )
                if initial_rating not in (None, "", 0, "0"):
                    try:
                        detail["douban_rating"] = float(initial_rating)
                    except (TypeError, ValueError):
                        pass

        # v2 豆瓣/Bangumi 识别接口只返回原生来源身份，不会自动产生 TMDB。
        # 使用平台现有的标题匹配和 TMDB 识别链补齐跨来源 ID，避免前端自行猜测映射。
        if (
                cached_detail is None
                and str(detail.get("tmdb_id") or "").strip() in {"", "0"}
                and source in {"douban", "bangumi", "anilist"}
        ):
            tmdb_info = None
            lookup_methods = {
                "douban": ("get_tmdbinfo_by_doubanid", {"doubanid": str(source_id), "mtype": media_type}),
                "bangumi": ("get_tmdbinfo_by_bangumiid",
                            {"bangumiid": int(source_id) if str(source_id).isdigit() else source_id}),
            }
            lookup_spec = lookup_methods.get(source)
            if lookup_spec:
                lookup = getattr(self.chain, lookup_spec[0], None)
                if callable(lookup):
                    try:
                        tmdb_info = call_with_supported_kwargs(lookup, lookup_spec[1])
                    except Exception as error:
                        logger.debug(f"详情媒体来源 ID 映射 TMDB 失败 [{source}:{source_id}]：{error}")

            # 直接来源映射缺失时，调用平台已有 TMDB 标题匹配接口，并带上年份与媒体类型。
            if not tmdb_info:
                matcher = getattr(self.chain, "match_tmdbinfo", None)
                if callable(matcher):
                    try:
                        tmdb_info = call_with_supported_kwargs(
                            matcher,
                            {
                                "name": title,
                                "mtype": media_type,
                                "year": year,
                            },
                        )
                    except Exception as error:
                        logger.debug(f"详情媒体标题匹配 TMDB 失败 [{title}]：{error}")

            tmdb_id = (
                tmdb_info.get("id") or tmdb_info.get("tmdb_id")
                if isinstance(tmdb_info, dict)
                else getattr(tmdb_info, "id", None) or getattr(tmdb_info, "tmdb_id", None)
            )
            if tmdb_id not in (None, "", 0, "0"):
                detail["tmdb_id"] = tmdb_id
                detail["media_source"] = "themoviedb"
                detail["media_id"] = str(tmdb_id)
                _merge_media_display(detail, tmdb_info, tmdb=True)
                external_ids = (
                                   tmdb_info.get("external_ids")
                                   if isinstance(tmdb_info, dict)
                                   else getattr(tmdb_info, "external_ids", None)
                               ) or {}
                for field in ("imdb_id", "tvdb_id"):
                    value = external_ids.get(field) if isinstance(external_ids, dict) else getattr(external_ids, field,
                                                                                                   None)
                    if value not in (None, "", 0, "0") and not detail.get(field):
                        detail[field] = value

                # TMDB 详情通常包含 IMDb/TVDB；再次走识别链可补齐平台支持的其他来源字段。
                try:
                    tmdb_recognized = recognize_media(
                        self.chain,
                        meta=meta,
                        mtype=media_type,
                        tmdb_id=tmdb_id,
                        cache=True,
                    )
                except Exception as error:
                    logger.debug(f"详情 TMDB 身份补全失败 [{tmdb_id}]：{error}")
                    tmdb_recognized = None
                if tmdb_recognized:
                    for field in (
                            "tmdb_id", "imdb_id", "tvdb_id", "bangumi_id",
                            "anilist_id", "anidb_id",
                    ):
                        value = getattr(tmdb_recognized, field, None)
                        if value not in (None, "", 0, "0") and not detail.get(field):
                            detail[field] = value
                    _merge_media_display(detail, tmdb_recognized, tmdb=True)
                logger.debug(
                    f"详情媒体身份补全 [{source}:{source_id}] -> "
                    f"TMDB:{detail.get('tmdb_id') or '无'}, "
                    f"IMDb:{detail.get('imdb_id') or '无'}, "
                    f"TVDB:{detail.get('tvdb_id') or '无'}"
                )

        # 若已识别出 TMDB ID 但缺少简介或展示评分，通过 TMDB 详情接口补全
        current_tmdb_id = detail.get("tmdb_id")
        if (
                cached_detail is None
                and current_tmdb_id
                and (
                not str(detail.get("overview") or "").strip()
                or detail.get("vote_average") in (None, "", 0, "0")
                or not detail.get("backdrop_path")
        )
        ):
            for fetcher_name in ("tmdb_info", "get_tmdbinfo", "obtain_specific_media"):
                fetcher = getattr(self.chain, fetcher_name, None)
                if callable(fetcher):
                    try:
                        full_tmdb = call_with_supported_kwargs(
                            fetcher,
                            {
                                "tmdbid": int(current_tmdb_id) if str(current_tmdb_id).isdigit() else current_tmdb_id,
                                "mediaid": str(current_tmdb_id),
                                "mtype": media_type,
                            },
                        )
                        if full_tmdb:
                            _merge_media_display(detail, full_tmdb, tmdb=True)
                            break
                    except Exception as error:
                        logger.debug(f"补全 TMDB 详情失败 [{current_tmdb_id}]：{error}")

        # 若依然缺少简介且具备豆瓣 ID，调用豆瓣详情接口补全
        if douban_id and not str(detail.get("overview") or "").strip():
            for method_name in ("douban_info", "get_doubaninfo", "douban_media_info"):
                douban_func = getattr(self.chain, method_name, None)
                if callable(douban_func):
                    try:
                        douban_detail = call_with_supported_kwargs(
                            douban_func,
                            {"doubanid": str(douban_id), "mtype": media_type},
                        )
                        if douban_detail:
                            _merge_media_display(detail, douban_detail)
                            break
                    except Exception as error:
                        logger.debug(f"通过豆瓣 ID 获取详情失败 [{douban_id}]：{error}")

        if cached_detail is None:
            season_counts = _recognized_season_counts(recognized)
            identity_fields = (
                "tmdb_id", "imdb_id", "tvdb_id", "douban_id",
                "bangumi_id", "anilist_id", "anidb_id",
                "media_source", "media_id",
                "vote_average", "rating", "vote_count",
                "tmdb_rating", "tmdb_vote_count",
                "douban_rating", "douban_vote_count",
                "imdb_rating", "imdb_vote_count",
                "overview",
                "genres", "genre_ids", "regions", "category",
                "backdrop_path", "backdrop_url", "poster_path", "poster_url",
                "original_title", "cn_title", "title",
            )
            _RESOURCE_DETAIL_CACHE.set(detail_cache_key, {
                "identity": {
                    field: detail.get(field)
                    for field in identity_fields
                    if detail.get(field) not in (None, "", 0, "0")
                },
                "season_counts": season_counts,
            })

        matches = []
        try:
            tmdb_id = detail.get("tmdb_id")
            if tmdb_id and str(tmdb_id).isdigit():
                from .media_library import MediaLibraryApi
                library_api = self._get_component(MediaLibraryApi)
                library_map = library_api.lookup_media_library([{
                    "tmdb_id": tmdb_id,
                    "media_type": "tv" if media_type == MediaType.TV else "movie",
                }])
                matches = library_map.get(str(tmdb_id), [])
        except Exception as error:
            logger.debug(f"读取详情媒体库状态失败：{error}")
        detail["in_library"] = bool(matches)
        detail["library_items"] = matches
        detail["library_servers"] = sorted({
            str(entry.get("server") or "媒体服务器") for entry in matches
        })

        if media_type == MediaType.TV:
            library_episodes = {}
            for entry in matches:
                season_info = entry.get("seasoninfo") or {}
                if isinstance(season_info, str):
                    try:
                        import json
                        season_info = json.loads(season_info)
                    except Exception:
                        season_info = {}
                if not isinstance(season_info, dict):
                    continue
                for season_number, episodes in season_info.items():
                    try:
                        season_number = int(season_number)
                    except (TypeError, ValueError):
                        continue
                    values = episodes if isinstance(episodes, list) else [episodes]
                    library_episodes.setdefault(season_number, set()).update(
                        int(item) for item in values if str(item).isdigit() and int(item) > 0
                    )

            season_numbers = set(season_counts) | set(library_episodes)
            detail["seasons"] = [
                {
                    "season_number": season_number,
                    "season_name": f"第 {season_number} 季",
                    "total_episodes": max(season_counts.get(season_number, 0),
                                          max(library_episodes.get(season_number, set()), default=0)),
                    "library_episodes_count": len(library_episodes.get(season_number, set())),
                    "episodes": [
                        {
                            "episode": episode,
                            "in_library": episode in library_episodes.get(season_number, set()),
                        }
                        for episode in range(1, max(season_counts.get(season_number, 0),
                                                    max(library_episodes.get(season_number, set()), default=0)) + 1)
                    ],
                }
                for season_number in sorted(season_numbers)
            ]
            detail["library_episodes_total"] = sum(
                len(episodes) for episodes in library_episodes.values()
            )
        else:
            detail["seasons"] = []
            detail["library_episodes_total"] = 0

        return {"success": True, "data": {"item": detail}}


def clear_ui_options_cache() -> int:
    caches = (
        _UI_OPTIONS_CACHE,
        _RESOURCE_RECOMMEND_CACHE,
        _RESOURCE_DETAIL_CACHE,
    )
    count = sum(len(list(cache.items())) for cache in caches)
    for cache in caches:
        cache.clear()
    return count
