"""Vue 页面需要的配置默认值和选项查询。"""

import datetime
from typing import Any, Dict, List
from urllib.parse import urlsplit, urlunsplit

from app.db import SessionFactory
from app.db.site_oper import SiteOper
from app.db.subscribe_oper import SubscribeOper
from app.helper.mediaserver import MediaServerHelper
from app.log import logger
from app.schemas.types import MediaType

from .media import tmdb_id_of

DEFAULT_AUTO_SUBSCRIBE_USERNAME = "网盘订阅助手"


class UIConfig:
    """提供 Vue 配置页所需的数据，不再保留旧 iframe/Vuetify 表单。"""

    @staticmethod
    def get_default_config() -> Dict[str, Any]:
        current_year = datetime.datetime.now().year
        current_month = datetime.datetime.now().month
        return {
            "enabled": False,
            "show_sidebar_nav": True,
            "agent_enabled": True,
            "direct_transfer_enabled": True,
            "notify": True,
            "notification_type": "Plugin",
            "webhook_enabled": False,
            "webhook_url": "",
            "webhook_method": "POST",
            "webhook_timeout": 10,
            "cron": "30 2,10,18 * * *",
            "auto_subscribe_enabled": False,
            "auto_subscribe_onlyonce": False,
            "auto_subscribe_cron": "0 8 * * *",
            "auto_subscribe_username": DEFAULT_AUTO_SUBSCRIBE_USERNAME,
            "auto_subscribe_notify": False,
            "auto_subscribe_skip_subscribed": True,
            "auto_subscribe_skip_history": True,
            "auto_subscribe_skip_library": True,
            "auto_subscribe_skip_season_zero": True,
            "auto_subscribe_proxy": "",
            "auto_subscribe_proxy_username": "",
            "auto_subscribe_proxy_password": "",
            "auto_subscribe_douban_enabled": False,
            "auto_subscribe_douban_ranks": ["movie-showing", "movie-hot"],
            "auto_subscribe_douban_rsshub_base": "https://rsshub.app",
            "auto_subscribe_douban_rss_urls": [],
            "auto_subscribe_douban_proxy": False,
            "auto_subscribe_douban_limit": 30,
            "auto_subscribe_douban_min_vote": 6,
            "auto_subscribe_douban_min_year": current_year,
            "auto_subscribe_douban_min_month": current_month,
            "auto_subscribe_douban_media_type": "all",
            "auto_subscribe_tmdb_enabled": False,
            "auto_subscribe_tmdb_ranks": ["trending", "movies", "tvs"],
            "auto_subscribe_tmdb_proxy": False,
            "auto_subscribe_tmdb_limit": 20,
            "auto_subscribe_tmdb_min_vote": 6,
            "auto_subscribe_tmdb_min_year": current_year,
            "auto_subscribe_tmdb_min_month": current_month,
            "auto_subscribe_tmdb_media_type": "all",
            "auto_subscribe_bangumi_enabled": False,
            "auto_subscribe_bangumi_proxy": False,
            "auto_subscribe_bangumi_limit": 50,
            "auto_subscribe_bangumi_min_vote": 6,
            "auto_subscribe_bangumi_min_year": current_year,
            "auto_subscribe_bangumi_min_month": current_month,
            "auto_subscribe_anilist_enabled": False,
            "auto_subscribe_anilist_ranks": ["popular_this_season", "trending"],
            "auto_subscribe_anilist_proxy": False,
            "auto_subscribe_anilist_limit": 30,
            "auto_subscribe_anilist_min_vote": 6,
            "auto_subscribe_anilist_min_year": current_year,
            "auto_subscribe_anilist_min_month": current_month,
            "auto_subscribe_maoyan_enabled": False,
            "auto_subscribe_maoyan_base_url": "https://piaofang.maoyan.com",
            "auto_subscribe_maoyan_movie_box": True,
            "auto_subscribe_maoyan_web_platform_map": {"all": ["tv"]},
            "auto_subscribe_maoyan_platforms": ["all"],
            "auto_subscribe_maoyan_categories": ["tv"],
            "auto_subscribe_maoyan_limit": 10,
            "auto_subscribe_maoyan_proxy": False,
            "auto_subscribe_maoyan_min_vote": 6,
            "auto_subscribe_maoyan_min_year": current_year,
            "auto_subscribe_maoyan_min_month": current_month,
            "auto_subscribe_maoyan_media_type": "all",
            "auto_subscribe_netflix_enabled": False,
            "auto_subscribe_netflix_base_url": "https://www.netflix.com",
            "auto_subscribe_netflix_global": True,
            "auto_subscribe_netflix_global_dataset": "weekly",
            "auto_subscribe_netflix_global_media_types": [
                "Films (English)", "Films (Non-English)",
                "TV (English)", "TV (Non-English)",
            ],
            "auto_subscribe_netflix_country_selections": {},
            "auto_subscribe_netflix_limit": 10,
            "auto_subscribe_netflix_proxy": False,
            "auto_subscribe_netflix_min_vote": 6,
            "auto_subscribe_netflix_min_year": current_year,
            "auto_subscribe_netflix_min_month": current_month,
            "auto_subscribe_netflix_rich_metadata": False,
            "auto_subscribe_netflix_max_workers": 4,
            "auto_subscribe_netflix_use_cache": True,
            "auto_subscribe_mikan_enabled": False,
            "auto_subscribe_mikan_year": current_year,
            "auto_subscribe_mikan_season": "当前",
            "auto_subscribe_mikan_resolve_bangumi_id": True,
            "auto_subscribe_mikan_proxy": False,
            "auto_subscribe_mikan_limit": 100,
            "auto_subscribe_mikan_min_vote": 6,
            "auto_subscribe_mikan_min_year": current_year,
            "auto_subscribe_mikan_min_month": current_month,
            "auto_subscribe_mikan_base_urls": [
                "https://mikanani.me", "https://mikanime.tv"
            ],
            "cookies": "",
            "p115_checkin_enabled": False,
            "p115_checkin_mode": "normal",
            "p123_token": "",
            "p123_request_timeout": 30,
            "quark_cookie": "",
            "quark_checkin_enabled": False,
            "quark_checkin_url": "",
            "quark_checkin_mode": "normal",
            "quark_request_timeout": 30,
            "guangya_access_token": "",
            "guangya_refresh_token": "",
            "guangya_client_id": "",
            "guangya_device_id": "",
            "guangya_request_timeout": 30,
            "tianyi_cookie": "",
            "tianyi_access_token": "",
            "tianyi_refresh_token": "",
            "tianyi_request_timeout": 60,
            "alipan_access_token": "",
            "alipan_refresh_token": "",
            "alipan_request_timeout": 60,
            "cloud_drive": "115",
            "strm_generate_enabled": True,
            "nfo_scrape_enabled": False,
            "image_scrape_enabled": False,
            "strm_base_url": "http://172.17.0.1:9527",
            "strm_url_template": "{base_url}/d/{pickcode}?/{file_name}",
            "media_server_refresh_enabled": False,
            "media_servers": [],
            "media_server_path_mappings": "",
            "media_server_refresh_delay": 0,
            "emby_mediainfo_enabled": False,
            "platform_media_sync_enabled": False,
            "platform_deep_delete_enabled": False,
            "platform_transfer_history_enabled": False,
            "timeout_enabled": True,
            "timeout_default_connect": 30,
            "timeout_default_pool": 15,
            "timeout_default_read": 60,
            "timeout_default_write": 60,
            "timeout_slow_connect": 30,
            "timeout_slow_pool": 15,
            "timeout_slow_read": 300,
            "timeout_slow_write": 300,
            "pansou_url": "https://so.252035.xyz/",
            "hdhive_base_url": "https://re0.me",
            "dian115_base_url": "https://m.dian115.com",
            "juying_base_url": "https://www.jying.top",
            "seedhub_base_url": "https://www.seedhub.cc",
            "piratebay_base_url": "https://apibay.org",
            "uindex_base_url": "https://uindex.org",
            "pinglian_base_url": "https://pinglian.lol",
            "online_docs_urls": [],
            "online_docs_resource_types": ["115", "123", "quark", "alipan"],
            "online_docs": [{"url": "", "resource_types": []}],
            "pansou_username": "",
            "pansou_password": "",
            "pansou_auth_enabled": False,
            "pansou_channels": [],
            "pansou_plugins": [],
            "pansou_filter_include": [],
            "pansou_filter_exclude": [],
            "resource_type_order": ["115", "ed2k"],
            "magnet_metadata_url_template": "https://itorrents.org/torrent/{info_hash}.torrent",
            "pansou_concurrency": None,
            "pansou_result_limit": 10,
            "pansou_refresh": True,
            "pansou_timeout": 30,
            "seedhub_result_limit": 20,
            "seedhub_request_interval": 1.0,
            "seedhub_timeout": 20,
            "piratebay_result_limit": 20,
            "piratebay_request_interval": 1.0,
            "piratebay_timeout": 20,
            "uindex_result_limit": 20,
            "uindex_request_interval": 1.0,
            "uindex_timeout": 20,
            "juying_username": "",
            "juying_password": "",
            "juying_checkin_enabled": False,
            "juying_result_limit": 5,
            "juying_request_interval": 1.0,
            "pinglian_username": "",
            "pinglian_password": "",
            "pinglian_result_limit": 20,
            "pinglian_request_interval": 1.0,
            "pinglian_timeout": 30,
            "hdhive_query_mode": "web",
            "hdhive_api_key": "",
            "hdhive_client_id": "",
            "hdhive_redirect_uri": "",
            "hdhive_response_mode": "redirect",
            "hdhive_auth_code": "",
            "hdhive_access_token": "",
            "hdhive_refresh_token": "",
            "hdhive_token_expires_at": 0,
            "hdhive_auto_unlock": False,
            "hdhive_max_unlock_points": 50,
            "hdhive_max_points_per_sub": 20,
            "hdhive_username": "",
            "hdhive_password": "",
            "hdhive_checkin_enabled": False,
            "hdhive_checkin_mode": "normal",
            "checkin_cron": "0 8 * * *",
            "checkin_auto_retry": True,
            "checkin_retry_count": 2,
            "dian115_email": "",
            "dian115_password": "",
            "dian115_checkin_enabled": False,
            "dian115_checkin_mode": "normal",
            "dian115_lottery_enabled": False,
            "dian115_lottery_count": 1,
            "dian115_auto_unlock": False,
            "dian115_max_unlock_points": 50,
            "dian115_max_points_per_sub": 20,
            "search_source_order": ["pansou"],
            "search_proxy": "",
            "search_proxy_username": "",
            "search_proxy_password": "",
            "search_cache_enabled": True,
            "search_cache_ttl_minutes": 30,
            "search_concurrency": 2,
            "hdhive_candidate_limit": 4,
            "hdhive_request_interval": 5,
            "hdhive_unlocks_per_minute": 2,
            "dian115_candidate_limit": 4,
            "dian115_request_interval": 1,
            "dian115_unlocks_per_minute": 6,
            "hdhive_torrentclaw_enabled": False,
            "hdhive_torrentclaw_subtitle_languages": ["zh"],
            "subscribe_filter_mode": "exclude",
            "exclude_subscribes": [],
            "include_subscribes": [],
            "block_system_subscribe": False,
            "takeover_new_subscribes": False,
            "platform_download_policy": "block",
            "block_start_time": "18:00",
            "block_end_time": "23:59",
            "transfer_task_batch_size": 50,
            "cross_transfer_enabled": False,
            "cross_transfer_media_types": ["movie", "tv"],
            "cross_transfer_download_path": "",
            "cross_transfer_download_threads": 5,
            "cross_transfer_max_concurrent": 2,
            "subscription_concurrency": 2,
            "batch_size": 20,
            "batch_interval": 3,
            "transfer_risk_cooldown": 1800,
            "skip_other_season_dirs": True,
            "enable_cloud_upgrade": False,
            "enable_pt_upgrade": False,
            "upgrade_mode": "largest",
            "upgrade_subscribe_ids": [],
            "local_resource_path": "",
            "cloud_transfer_path": "/",
            "p123_transfer_path": "/",
            "quark_transfer_path": "/",
            "guangya_transfer_path": "/",
            "tianyi_transfer_path": "/",
            "alipan_transfer_path": "/",
            "cloud_media_path": "/",
            "p123_media_path": "/",
            "quark_media_path": "/",
            "guangya_media_path": "/",
            "tianyi_media_path": "/",
            "alipan_media_path": "/",
            "self_heal_interval": 10,
        }

    @staticmethod
    def normalize_auto_subscribe_dates(config: Dict[str, Any]) -> None:
        current_year = datetime.datetime.now().year
        current_month = datetime.datetime.now().month
        for key in (
                "auto_subscribe_douban_min_year",
                "auto_subscribe_tmdb_min_year",
                "auto_subscribe_bangumi_min_year",
                "auto_subscribe_anilist_min_year",
                "auto_subscribe_maoyan_min_year",
                "auto_subscribe_netflix_min_year",
                "auto_subscribe_mikan_year",
                "auto_subscribe_mikan_min_year",
        ):
            try:
                if int(config.get(key) or 0) == 0:
                    config[key] = current_year
            except (TypeError, ValueError):
                config[key] = current_year
        for provider_id in (
                "douban", "tmdb", "bangumi", "anilist", "maoyan", "netflix", "mikan"
        ):
            key = f"auto_subscribe_{provider_id}_min_month"
            try:
                month = int(config.get(key) or current_month)
            except (TypeError, ValueError):
                month = current_month
            config[key] = month if 1 <= month <= 12 else current_month

    @staticmethod
    def _normalize_rsshub_instance_url(value: Any) -> str:
        """规范公告中的实例地址，拒绝维护者主页、查询参数和本地地址。"""
        try:
            parsed = urlsplit(str(value or "").strip())
            if parsed.scheme.lower() not in {"http", "https"}:
                return ""
            if (
                    not parsed.hostname
                    or parsed.username is not None
                    or parsed.password is not None
                    or parsed.query
                    or parsed.fragment
            ):
                return ""
            port = parsed.port
        except ValueError:
            return ""
        hostname = parsed.hostname.rstrip(".").lower()
        if (
                "." not in hostname
                or hostname == "localhost"
                or hostname.endswith(".local")
                or any(character.isspace() for character in parsed.path)
        ):
            return ""
        netloc = hostname if port is None else f"{hostname}:{port}"
        path = parsed.path.rstrip("/")
        return urlunsplit((parsed.scheme.lower(), netloc, path, "", ""))

    @staticmethod
    def _subscribes() -> list:
        try:
            with SessionFactory() as db:
                return SubscribeOper(db=db).list("N,R") or []
        except Exception as error:
            logger.error(f"获取订阅列表失败: {error}")
            return []

    @staticmethod
    def get_subscribe_options() -> List[Dict[str, Any]]:
        items = []
        for subscribe in UIConfig._subscribes():
            prefix = "[剧]" if subscribe.type == MediaType.TV.value else "[影]"
            suffix = f" ({subscribe.year})" if subscribe.year else ""
            season = f" S{subscribe.season or 1}" if subscribe.type == MediaType.TV.value else ""
            items.append({"title": f"{prefix} {subscribe.name}{suffix}{season}", "value": subscribe.id})
        return items

    @staticmethod
    def get_subscribe_options_grouped() -> List[Dict[str, Any]]:
        items = []
        for subscribe in UIConfig._subscribes():
            is_movie = subscribe.type == MediaType.MOVIE.value
            group = "电影订阅" if is_movie else "电视剧订阅"
            prefix = "[电影]" if is_movie else "[电视剧]"
            suffix = f" ({subscribe.year})" if subscribe.year else ""
            season = f" S{subscribe.season or 1}" if subscribe.type == MediaType.TV.value else ""
            items.append(
                {
                    "title": f"{prefix} {subscribe.name}{suffix}{season}",
                    "value": subscribe.id,
                    "group": group,
                    "name": subscribe.name,
                    "year": subscribe.year,
                    "media_type": "movie" if is_movie else "tv",
                    "tmdb_id": tmdb_id_of(subscribe),
                    "season": subscribe.season if not is_movie else None,
                }
            )
        return items

    @staticmethod
    def get_site_name_options() -> List[Dict[str, Any]]:
        try:
            with SessionFactory() as db:
                sites = SiteOper(db=db).list() or []
            names = sorted({str(site.name) for site in sites if site.name})
            return [{"title": name, "value": name} for name in names]
        except Exception as error:
            logger.error(f"获取站点列表失败: {error}")
            return []

    @staticmethod
    def get_media_server_options() -> List[Dict[str, Any]]:
        try:
            return [
                {"title": config.name, "value": config.name, "type": config.type}
                for config in MediaServerHelper().get_configs().values()
            ]
        except Exception as error:
            logger.error(f"获取媒体服务器列表失败: {error}")
            return []
