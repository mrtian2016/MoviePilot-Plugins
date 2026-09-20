"""
网盘搜索助手插件
结合订阅功能，自动搜索网盘资源并同步缺失内容
"""
import copy
import datetime
import re
from concurrent.futures import ThreadPoolExecutor
from threading import Event as ThreadEvent, Lock, RLock, local
from typing import Optional, Any, List, Dict, Tuple, Callable

import pytz
from app.api.endpoints.plugin import register_plugin_api
from app.core.config import settings
from app.core.event import Event, eventmanager
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import ChainEventType, EventType, NotificationType
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from .core import (
    CloudDriveProvider,
    CloudDriveRegistry,
    CloudDriveCapability,
    CrossTransferTaskManager,
    get_component,
    resolve_component,
)
from .core.api import (
    AccountApi,
    ConfigApi,
    CheckinApi,
    MoviePilotRegistration,
    HistoryApi,
    MediaLibraryApi,
    PageApi,
    QRCodeService,
    RuntimeApi,
    SearchApi,
    SyncApi,
)
from .core.hook import MessageRoutingHook, PluginEventHandler, SubscriptionSearchHook
from .core.services import (
    SubscriptionControlService,
    CheckinService,
    PlatformIntegrationService,
    SubscriptionScoringService,
    SyncExecutionService,
    SyncRuntimeService,
)
from .core.storage import PanSearchDataStore
from .core.subscribe import AutoSubscribeService
from .drive.alipan import AliPanClient, AliPanDrive, create_alipan_provider
from .drive.guangya import GuangyaClient, GuangyaDrive, create_guangya_provider
from .drive.p115 import P115ClientManager, create_p115_provider
from .drive.p123 import P123ClientManager, P123Drive, create_p123_provider
from .drive.quark import QuarkClient, QuarkDrive, create_quark_provider
from .drive.tianyi import TianyiClient, TianyiDrive, create_tianyi_provider
from .handlers import SearchHandler, SyncHandler, SubscribeHandler, WebhookHandler
from .search.hdhive import (
    HDHiveOpenAPIClient, HDHiveOpenAPIError,
)
from .search.juying import JuyingClient
from .search.online_docs import OnlineDocumentClient
from .search.pansou import PanSouClient
from .search.pinglian import PinglianClient
from .search.piratebay import PirateBayClient
from .search.seedhub import SeedHubClient
from .search.uindex import UIndexClient
from .subscribe import (  # noqa: F401 - 导入即注册自动订阅渠道
    create_anilist_provider,
    create_bangumi_provider,
    create_douban_provider,
    create_maoyan_provider,
    create_mikan_provider,
    create_netflix_provider,
    create_tmdb_provider,
)
from .utils import configure_magnet_metadata_url
from .utils.http_client import build_proxy_url, validate_proxy_address

_COMPONENT_TYPES = (
    PageApi,
    AccountApi,
    SearchApi,
    ConfigApi,
    CheckinApi,
    SyncApi,
    RuntimeApi,
    MediaLibraryApi,
    QRCodeService,
    HistoryApi,
    MessageRoutingHook,
    PluginEventHandler,
    MoviePilotRegistration,
    SyncRuntimeService,
    SyncExecutionService,
    SubscriptionScoringService,
    SubscriptionControlService,
    SubscriptionSearchHook,
    PlatformIntegrationService,
    CheckinService,
    AutoSubscribeService,
)


class PanSearch(_PluginBase):
    """网盘搜索助手插件。"""

    # 插件名称
    plugin_name = "网盘搜索助手"
    # 插件描述
    plugin_desc = "整合网盘能力与多渠道资源搜索，自动查找并补充订阅缺失的影视内容。（wukangxxx 二开增强版）"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/odomu/MoviePilot-Plugins/main/icons/cloud.png"
    # 插件版本
    # v1.5.5：媒体库目录分类修复——新增电影/电视剧媒体目录配置
    # （movie_media_path/tv_media_path，未设置回退媒体库目录兼容旧配置）；
    # _platform_classified_root 不再用 cloud_media_path 覆盖 MoviePilot
    # 目录配置 library_path（电视剧/电影类型层不再被抹掉，分类规则继续走
    # MoviePilot 系统规则），修复电视剧错落 /影视库/国产剧/ 导致目录乱；
    # 目录计算修正后存量处理中/下载中任务可自然推进终态；转存整理开关闭环
    # 确认（关闭时文件停在中转目录即完成，不建分类/不生成 STRM）；前端配置页
    # 新增电影/电视剧媒体目录字段并重建 dist。
    plugin_version = "1.5.14"
    # 插件作者
    plugin_author = "odomu"
    # 作者主页
    author_url = "https://github.com/odomu"
    # 插件配置项ID前缀
    plugin_config_prefix = "pansearch_"
    plugin_order = 21
    auth_level = 1

    # 私有变量
    _scheduler: Optional[BackgroundScheduler] = None
    _offline_scheduler: Optional[BackgroundScheduler] = None
    _offline_scheduler_lock: RLock = RLock()
    _offline_monitor_lock: Lock = Lock()

    # 配置属性
    _enabled: bool = False
    _show_sidebar_nav: bool = True
    _agent_enabled: bool = True
    _direct_transfer_enabled: bool = True
    _cron: str = "0 18-23 * * *"
    _auto_subscribe_enabled: bool = False
    _auto_subscribe_onlyonce: bool = False
    _auto_subscribe_cron: str = "0 8 * * *"
    _notify: bool = False
    _notification_type: NotificationType = NotificationType.Plugin
    _webhook_enabled: bool = False
    _webhook_url: str = ""
    _webhook_method: str = "POST"
    _webhook_timeout: int = 10

    _p115_cookies: str = ""
    _p115_checkin_enabled: bool = False
    _p115_checkin_mode: str = "normal"
    _p123_token: str = ""
    _p123_request_timeout: int = 30
    _quark_cookie: str = ""
    _quark_checkin_enabled: bool = False
    _quark_checkin_url: str = ""
    _quark_checkin_mode: str = "normal"
    _quark_request_timeout: int = 30
    _guangya_access_token: str = ""
    _guangya_refresh_token: str = ""
    _guangya_client_id: str = ""
    _guangya_device_id: str = ""
    _guangya_request_timeout: int = 30
    _tianyi_cookie: str = ""
    _tianyi_access_token: str = ""
    _tianyi_refresh_token: str = ""
    _tianyi_session_key: str = ""
    _tianyi_request_timeout: int = 60
    _tianyi_transfer_path: str = "/"
    _tianyi_media_path: str = "/"
    _alipan_access_token: str = ""
    _alipan_refresh_token: str = ""
    _alipan_request_timeout: int = 60
    _alipan_transfer_path: str = "/"
    _alipan_media_path: str = "/"
    _cloud_drive_key: str = "115"
    _pansou_enabled: bool = True
    _pansou_url: str = "https://so.252035.xyz"
    _pansou_username: str = ""
    _pansou_password: str = ""
    _pansou_auth_enabled: bool = False
    _pansou_channels: Any = None
    _pansou_plugins: Any = None
    _pansou_cloud_types: Any = None
    _pansou_filter_include: Any = None
    _pansou_filter_exclude: Any = None
    _resource_type_order: List[str] = ["115", "ed2k"]
    _magnet_metadata_url_template: str = "https://itorrents.org/torrent/{info_hash}.torrent"
    _pansou_concurrency: Optional[int] = None
    _pansou_result_limit: int = 10
    _pansou_refresh: bool = True
    _pansou_timeout: int = 30
    _pansou_check_enabled: bool = False
    _seedhub_enabled: bool = False
    _seedhub_base_url: str = "https://www.seedhub.cc"
    _seedhub_result_limit: int = 20
    _seedhub_request_interval: float = 1.0
    _seedhub_timeout: int = 20
    _piratebay_enabled: bool = False
    _piratebay_base_url: str = "https://apibay.org"
    _piratebay_result_limit: int = 20
    _piratebay_request_interval: float = 1.0
    _piratebay_timeout: int = 20
    _uindex_enabled: bool = False
    _uindex_base_url: str = "https://uindex.org"
    _uindex_result_limit: int = 20
    _uindex_request_interval: float = 1.0
    _uindex_timeout: int = 20
    _juying_enabled: bool = False
    _juying_username: str = ""
    _juying_password: str = ""
    _juying_checkin_enabled: bool = False
    _juying_result_limit: int = 5
    _juying_request_interval: float = 1.0
    _pinglian_enabled: bool = False
    _pinglian_username: str = ""
    _pinglian_password: str = ""
    _pinglian_result_limit: int = 20
    _pinglian_request_interval: float = 1.0
    _pinglian_timeout: int = 30
    _online_docs: List[Dict[str, Any]] = []

    # 订阅过滤模式："exclude" 排除模式（处理除勾选外的全部订阅）/ "include" 指定模式（仅处理勾选的订阅）
    _subscribe_filter_mode: str = "exclude"
    _exclude_subscribes: List[int] = []
    _include_subscribes: List[int] = []
    # 搜索源优先级（按列表顺序），为空时使用已启用来源的默认顺序
    _search_source_order: List[str] = []
    _search_proxy: Any = ""
    _search_proxy_address: str = ""
    _search_proxy_username: str = ""
    _search_proxy_password: str = ""
    _search_cache_enabled: bool = True
    _search_cache_ttl_minutes: int = 30
    _search_concurrency: int = 2
    _checkin_cron: str = "0 8 * * *"
    _checkin_auto_retry: bool = True
    _checkin_retry_count: int = 2

    _dian115_enabled: bool = False
    _dian115_email: str = ""
    _dian115_password: str = ""
    _dian115_checkin_enabled: bool = False
    _dian115_checkin_mode: str = "normal"
    _dian115_lottery_enabled: bool = False
    _dian115_lottery_count: int = 1
    _dian115_auto_unlock: bool = False
    _dian115_max_unlock_points: int = 50
    _dian115_max_points_per_sub: int = 20
    _dian115_candidate_limit: int = 4
    _dian115_request_interval: float = 1.0
    _dian115_unlocks_per_minute: int = 6

    _hdhive_enabled: bool = False
    _hdhive_base_url: str = "https://re0.me"
    _hdhive_username: str = ""
    _hdhive_password: str = ""
    _hdhive_query_mode: str = "web"
    _hdhive_api_key: str = ""
    _hdhive_client_id: str = ""
    _hdhive_redirect_uri: str = ""
    _hdhive_response_mode: str = "redirect"
    _hdhive_auth_code: str = ""
    _hdhive_access_token: str = ""
    _hdhive_refresh_token: str = ""
    _hdhive_token_expires_at: float = 0
    _hdhive_auto_unlock: bool = False
    _hdhive_max_unlock_points: int = 50
    _hdhive_max_points_per_sub: int = 20
    _hdhive_checkin_enabled: bool = False
    _hdhive_checkin_mode: str = "normal"
    _hdhive_candidate_limit: int = 4
    _hdhive_request_interval: float = 5.0
    _hdhive_unlocks_per_minute: int = 2
    _hdhive_torrentclaw_enabled: bool = False
    _hdhive_torrentclaw_subtitle_languages: List[str] = ["zh"]
    _hdhive_client: Optional[Any] = None

    # 是否屏蔽系统订阅（True=已屏蔽系统订阅，False=已恢复系统订阅）
    _block_system_subscribe: bool = False
    _takeover_new_subscribes: bool = False
    _platform_download_policy: str = "block"

    _transfer_task_batch_size: int = 50
    _cross_transfer_enabled: bool = False
    _cross_transfer_media_types: list = ["movie", "tv"]
    _cross_transfer_download_path: str = ""
    _cross_transfer_download_threads: int = 5
    _cross_transfer_max_concurrent: int = 2
    _subscription_concurrency: int = 2
    _batch_size: int = 20
    _batch_interval: float = 3
    _transfer_risk_cooldown: int = 1800
    _skip_other_season_dirs: bool = True

    # 洗版配置
    _upgrade_subscribe_ids: list = []
    _last_scored_ids_hash: str = ""  # 上次评分过的ids hash值，用于保存配置时防重复触发
    _self_heal_interval: int = 10
    _enable_cloud_upgrade: bool = False
    _enable_pt_upgrade: bool = False
    _upgrade_mode: str = "largest"
    _local_resource_path: str = ""  # 容器内本地或挂载媒体根路径
    _p115_transfer_path: str = "/"
    _p123_transfer_path: str = "/"
    _quark_transfer_path: str = "/"
    _guangya_transfer_path: str = "/"
    _cloud_transfer_path: str = "/"
    _p115_media_path: str = "/"
    _p123_media_path: str = "/"
    _quark_media_path: str = "/"
    _guangya_media_path: str = "/"
    _cloud_media_path: str = "/"
    _movie_media_path: str = "/"
    _tv_media_path: str = "/"
    _organize_after_transfer: bool = False
    _strm_generate_enabled: bool = True
    _nfo_scrape_enabled: bool = False
    _image_scrape_enabled: bool = False
    _strm_base_url: str = "http://172.17.0.1:9527"
    _strm_url_template: str = "{base_url}/d/{pickcode}?/{file_name}"
    _media_server_refresh_enabled: bool = False
    _media_servers: List[str] = []
    _media_server_path_mappings: str = ""
    _media_server_refresh_delay: int = 0
    _emby_mediainfo_enabled: bool = False
    _platform_media_sync_enabled: bool = False
    _platform_deep_delete_enabled: bool = False
    _platform_transfer_history_enabled: bool = False
    _timeout_enabled: bool = True
    _timeout_default_connect: float = 30
    _timeout_default_pool: float = 15
    _timeout_default_read: float = 60
    _timeout_default_write: float = 60
    _timeout_slow_connect: float = 30
    _timeout_slow_pool: float = 15
    _timeout_slow_read: float = 300
    _timeout_slow_write: float = 300
    # 屏蔽态时间段（block_system_subscribe=OFF 时生效）
    _block_start_time: str = "18:00"
    _block_end_time: str = "23:59"
    # 全局配置是否已应用（安装成功首次执行时才修改MP系统配置）
    _global_config_applied: bool = False

    # 运行时对象
    _pansou_client: Optional[PanSouClient] = None
    _seedhub_client: Optional[SeedHubClient] = None
    _piratebay_client: Optional[PirateBayClient] = None
    _uindex_client: Optional[UIndexClient] = None
    _juying_client: Optional[JuyingClient] = None
    _pinglian_client: Optional[PinglianClient] = None
    _online_docs_client: Optional[OnlineDocumentClient] = None
    _p115_manager: Optional[P115ClientManager] = None
    _p123_drive: Optional[P123Drive] = None
    _quark_drive: Optional[QuarkDrive] = None
    _guangya_drive: Optional[GuangyaDrive] = None
    _tianyi_drive: Optional[TianyiDrive] = None
    _alipan_drive: Optional[AliPanDrive] = None
    _cloud_drive_registry: Optional[CloudDriveRegistry] = None
    _cloud_drive: Optional[CloudDriveProvider] = None

    # 处理器
    _search_handler: Optional[SearchHandler] = None
    _subscribe_handler: Optional[SubscribeHandler] = None
    _sync_handler: Optional[SyncHandler] = None
    _webhook_handler: Optional[WebhookHandler] = None
    _subscribe_search_originals: Dict[str, Callable[..., Any]] = {}
    _platform_search_originals: Dict[str, Callable[..., Any]] = {}
    _stop_event: Optional[ThreadEvent] = None
    _sync_running: bool = False
    _sync_status: str = "idle"
    _sync_task_text: str = "当前没有订阅处理任务"
    _sync_progress: int = 0
    _sync_context: Dict[str, Any] = {}
    _sync_run_started_at: float = 0
    _sync_last_finished_at: float = 0
    _sync_last_elapsed_ms: int = 0
    _sync_tasks: Dict[str, Dict[str, Any]] = {}
    _sync_tasks_lock: Optional[RLock] = None
    _runtime_revision: int = 0
    _history_revision: int = 0
    _runtime_revision_lock: Lock = Lock()
    _task_local: Optional[local] = None
    _pending_config: Optional[Dict[str, Any]] = None
    _applied_config: Dict[str, Any] = {}
    _pending_config_lock: RLock = RLock()
    _subscribe_search_queue_lock: Optional[RLock] = None
    _subscribe_search_pending: Dict[Optional[int], bool] = {}
    _subscribe_search_active: Dict[Optional[int], bool] = {}
    _subscribe_search_queue_shutdown: Optional[ThreadEvent] = None
    _subscribe_search_coordinator_running: bool = False
    _subscribe_search_queue_revision: int = 0
    _sync_operation_executor: Optional[ThreadPoolExecutor] = None

    def _get_data_store(self) -> PanSearchDataStore:
        store = self.__dict__.get("_pansearch_data_store")
        if store is None:
            store = PanSearchDataStore(self)
            self.__dict__["_pansearch_data_store"] = store
        return store

    def get_data(self, key: Optional[str] = None, plugin_id: Optional[str] = None) -> Any:
        """读取插件业务数据与可恢复运行状态，统一使用私有库。"""
        target_plugin = plugin_id or self.__class__.__name__
        if target_plugin == self.__class__.__name__ and key:
            if PanSearchDataStore.handles(key):
                return self._get_data_store().load(key)
            return None
        return super().get_data(key=key, plugin_id=plugin_id)

    def save_data(
            self, key: str, value: Any, plugin_id: Optional[str] = None
    ) -> None:
        """保存插件数据"""
        target_plugin = plugin_id or self.__class__.__name__
        if target_plugin == self.__class__.__name__:
            if PanSearchDataStore.handles(key):
                self._get_data_store().save(key, value)
                return
            raise ValueError(f"未声明的数据键不能写入 PluginData：{key}")
        super().save_data(key=key, value=value, plugin_id=plugin_id)

    def _get_component(self, component_type):
        return get_component(self, component_type, "_plugin_components")

    def __getattr__(self, name):
        return resolve_component(self, _COMPONENT_TYPES, name, "_plugin_components")

    def get_state(self) -> bool:
        return self._get_component(MoviePilotRegistration).get_state()

    @staticmethod
    def get_render_mode() -> Tuple[str, str]:
        return MoviePilotRegistration.get_render_mode()

    def get_form(self) -> Tuple[Optional[List[dict]], Dict[str, Any]]:
        return self._get_component(MoviePilotRegistration).get_form()

    def get_page(self) -> Optional[List[dict]]:
        return self._get_component(MoviePilotRegistration).get_page()

    def get_api(self) -> List[Dict[str, Any]]:
        return self._get_component(MoviePilotRegistration).get_api()

    def get_command(self) -> List[Dict[str, Any]]:
        return self._get_component(MoviePilotRegistration).get_command()

    def get_service(self) -> List[Dict[str, Any]]:
        return self._get_component(MoviePilotRegistration).get_service()

    def get_dashboard_meta(self) -> List[Dict[str, str]]:
        return self._get_component(MoviePilotRegistration).get_dashboard_meta()

    def get_dashboard(self, key: str = "overview", **kwargs):
        return self._get_component(MoviePilotRegistration).get_dashboard(key, **kwargs)

    def get_sidebar_nav(self) -> List[Dict[str, Any]]:
        return self._get_component(MoviePilotRegistration).get_sidebar_nav()

    def get_actions(self) -> List[Dict[str, Any]]:
        return self._get_component(MoviePilotRegistration).get_actions()

    def get_agent_tools(self) -> List[type]:
        return self._get_component(MoviePilotRegistration).get_agent_tools()

    @eventmanager.register(EventType.SubscribeAdded)
    def on_subscribe_added(self, event: Event):
        return self._get_component(PluginEventHandler).on_subscribe_added(event)

    @eventmanager.register(EventType.SubscribeModified)
    def on_subscribe_modified(self, event: Event):
        return self._get_component(PluginEventHandler).on_subscribe_modified(event)

    @eventmanager.register(ChainEventType.ResourceDownload)
    def on_resource_download(self, event: Event):
        return self._get_component(PluginEventHandler).on_resource_download(event)

    @eventmanager.register(EventType.TransferComplete)
    def on_transfer_complete(self, event: Event):
        return self._get_component(PluginEventHandler).on_transfer_complete(event)

    @eventmanager.register(EventType.PluginAction)
    def on_plugin_action(self, event: Event):
        return self._get_component(PluginEventHandler).on_plugin_action(event)

    @eventmanager.register(EventType.MessageAction)
    def on_message_action(self, event: Event):
        return self._get_component(PluginEventHandler).on_message_action(event)

    @eventmanager.register(EventType.WebhookMessage)
    def on_media_server_webhook(self, event: Event):
        event_info = getattr(event, "event_data", None) if event else None
        return self._get_component(MediaLibraryApi).handle_platform_media_webhook(
            event_info
        )

    @staticmethod
    def _cron_is_valid(cron_expr: str) -> bool:
        """仅校验 cron 表达式是否合法,不再强制最小间隔"""
        cron_expr = (cron_expr or "").strip()
        if not cron_expr:
            return False
        try:
            tz = pytz.timezone(settings.TZ)
            CronTrigger.from_crontab(cron_expr, timezone=tz)
            return True
        except Exception:
            return False

    @staticmethod
    def _resolve_notification_type(value: Any) -> NotificationType:
        """将配置值解析为消息类型，无效值回退为插件消息。"""
        if isinstance(value, NotificationType):
            return value
        configured = str(value or NotificationType.Plugin.name).strip()
        if configured in NotificationType.__members__:
            return NotificationType[configured]
        for item in NotificationType:
            if item.value == configured:
                return item
        logger.warning(f"未知消息通知类型：{configured}，已回退为插件")
        return NotificationType.Plugin

    @staticmethod
    def _config_cloud_path(value: Any) -> str:
        path = str(value or "/").strip()
        if "://" in path:
            return "/"
        return f"/{path.strip('/')}" if path.strip("/") else "/"

    def init_plugin(self, config: dict = None):
        """宿主加载或重载插件时初始化完整运行环境。"""
        # 初始化独立数据库并修复历史分组键。
        self._get_data_store().initialize()
        self._apply_plugin_config(config, reset_runtime=True)

    def _apply_plugin_config(
            self, config: Optional[Dict[str, Any]], reset_runtime: bool = False
    ) -> None:
        """应用配置并重建相关服务；普通保存不重置同步任务状态。"""
        config = dict(config or {})
        original_config = dict(config)
        # 丢弃已移除搜索渠道的历史配置，避免旧字段继续进入运行态或保存结果。
        removed_config_prefixes = ("but" + "ailing_",)
        config = {
            key: value
            for key, value in config.items()
            if not str(key).lower().startswith(removed_config_prefixes)
        }
        legacy_download_keys = (
            "block_platform_downloads", "takeover_platform_downloads"
        )
        if (
                "platform_download_policy" not in config
                and any(key in config for key in legacy_download_keys)
        ):
            config["platform_download_policy"] = (
                "cloud"
                if bool(config.get("takeover_platform_downloads", False))
                else "block"
                if bool(config.get("block_platform_downloads", True))
                else "allow"
            )
        for deprecated_key in (
                *legacy_download_keys, "unblock_start_time", "unblock_end_time"
        ):
            config.pop(deprecated_key, None)
        if "platform_download_policy" in config:
            policy = str(config.get("platform_download_policy") or "block").strip().lower()
            if policy not in {"allow", "block", "cloud"}:
                logger.warning(f"未知平台下载策略：{policy}，已回退为阻止平台下载")
                policy = "block"
            config["platform_download_policy"] = policy
        if config != original_config:
            if self.update_config(config):
                logger.info("插件配置已清理并迁移到当前格式")
            else:
                logger.warning("订阅接管配置迁移持久化失败，本次运行仍使用迁移后配置")
        hot_keys = {
            "show_sidebar_nav",
            "agent_enabled",
            "direct_transfer_enabled",
            "notify",
            "notification_type",
            "webhook_enabled",
            "webhook_url",
            "webhook_method",
            "webhook_timeout",
            "media_server_refresh_enabled",
            "media_servers",
            "media_server_path_mappings",
            "media_server_refresh_delay",
            "emby_mediainfo_enabled",
            "platform_media_sync_enabled",
            "platform_deep_delete_enabled",
        }
        changed_keys = set()
        if not reset_runtime and self._applied_config:
            changed_keys = {
                key for key in set(self._applied_config) | set(config)
                if self._applied_config.get(key) != config.get(key)
            }
            if not changed_keys:
                return
            if changed_keys <= hot_keys:
                self._show_sidebar_nav = bool(config.get("show_sidebar_nav", True))
                self._agent_enabled = bool(config.get("agent_enabled", True))
                self._direct_transfer_enabled = bool(
                    config.get("direct_transfer_enabled", True)
                )
                self._apply_notification_config(config)
                self._get_component(MessageRoutingHook).install()
                self._applied_config = copy.deepcopy(config)
                from app.core.plugin import PluginManager
                PluginManager().clear_plugin_agent_tools_cache()
                logger.info("基础开关或通知配置已热更新，网盘与搜索客户端保持运行")
                return
        self.stop_service(preserve_subscribe_queue=not reset_runtime)
        self._stop_event = ThreadEvent()
        if reset_runtime:
            self._sync_running = False
            self._sync_status = "idle"
            self._sync_task_text = "当前没有订阅处理任务"
            self._sync_progress = 0
            self._sync_context = {}
            self._sync_run_started_at = 0
            self._sync_last_finished_at = 0
            self._sync_last_elapsed_ms = 0
            self._sync_tasks_lock = RLock()
            self._task_local = local()
            self._sync_tasks = {}
            with self._runtime_revision_lock:
                self._runtime_revision = 0
                self._history_revision = 0
            with self._pending_config_lock:
                self._pending_config = None
            self._subscribe_search_queue_lock = RLock()
            self._subscribe_search_pending = {}
            self._subscribe_search_active = {}
            self._subscribe_search_recent = {}
            self._subscribe_search_queue_shutdown = ThreadEvent()
            self._subscribe_search_coordinator_running = False
            self._subscribe_search_queue_revision = 0
            self._sync_operation_executor = ThreadPoolExecutor(
                max_workers=1,
                thread_name_prefix="pansearch-sync-operation",
            )
            self._subscribe_search_originals = {}
            self._platform_search_originals = {}
        else:
            if self._sync_tasks_lock is None:
                self._sync_tasks_lock = RLock()
            if self._task_local is None:
                self._task_local = local()
            if self._sync_operation_executor is None:
                self._sync_operation_executor = ThreadPoolExecutor(
                    max_workers=1,
                    thread_name_prefix="pansearch-sync-operation",
                )

        if config:
            self._enabled = config.get("enabled", False)
            self._show_sidebar_nav = bool(config.get("show_sidebar_nav", True))
            self._agent_enabled = bool(config.get("agent_enabled", True))
            self._direct_transfer_enabled = bool(
                config.get("direct_transfer_enabled", True)
            )

            self._cron = (config.get("cron", self._cron) or "").strip()
            if self._cron:
                if not self._cron_is_valid(self._cron):
                    logger.warning(
                        f"Cron 表达式无效：{self._cron}，已回退默认 0 18-23 * * *"
                    )
                    self._cron = "0 18-23 * * *"

            self._notify = config.get("notify", False)
            self._notification_type = self._resolve_notification_type(
                config.get("notification_type")
            )
            self._webhook_enabled = bool(config.get("webhook_enabled", False))
            self._webhook_url = str(config.get("webhook_url", "") or "").strip()
            self._webhook_method = str(config.get("webhook_method", "POST") or "POST").upper()
            if self._webhook_method not in ("POST", "GET"):
                self._webhook_method = "POST"
            self._webhook_timeout = max(1, min(int(config.get("webhook_timeout", 10) or 10), 120))
            self._auto_subscribe_enabled = bool(
                config.get("auto_subscribe_enabled", False)
            )
            self._auto_subscribe_onlyonce = bool(
                config.get("auto_subscribe_onlyonce", False)
            )
            self._auto_subscribe_cron = str(
                config.get("auto_subscribe_cron", "0 8 * * *") or "0 8 * * *"
            ).strip()
            self._p115_cookies = config.get("cookies", "")
            self._p115_checkin_enabled = bool(
                config.get("p115_checkin_enabled", False)
            )
            self._p115_checkin_mode = "normal"
            self._p123_token = str(config.get("p123_token", "") or "").strip()
            self._p123_request_timeout = max(
                5, min(int(config.get("p123_request_timeout", 30) or 30), 300)
            )
            self._quark_cookie = str(config.get("quark_cookie", "") or "").strip()
            self._quark_checkin_enabled = bool(
                config.get("quark_checkin_enabled", False)
            )
            self._quark_checkin_url = str(
                config.get("quark_checkin_url", "") or ""
            ).strip()
            self._quark_checkin_mode = "normal"
            self._quark_request_timeout = max(
                5, min(int(config.get("quark_request_timeout", 30) or 30), 300)
            )
            self._guangya_access_token = str(
                config.get("guangya_access_token", "") or ""
            ).strip()
            self._guangya_refresh_token = str(
                config.get("guangya_refresh_token", "") or ""
            ).strip()
            self._guangya_client_id = str(
                config.get("guangya_client_id", "") or ""
            ).strip()
            self._guangya_device_id = str(
                config.get("guangya_device_id", "") or ""
            ).strip()
            self._guangya_request_timeout = max(
                5, min(int(config.get("guangya_request_timeout", 30) or 30), 300)
            )
            self._tianyi_cookie = str(config.get("tianyi_cookie", "") or "").strip()
            self._tianyi_access_token = str(
                config.get("tianyi_access_token", "") or ""
            ).strip()
            self._tianyi_refresh_token = str(
                config.get("tianyi_refresh_token", "") or ""
            ).strip()
            self._tianyi_request_timeout = max(10, min(int(config.get("tianyi_request_timeout", 60) or 60), 300))
            self._tianyi_transfer_path = self._config_cloud_path(
                config.get("tianyi_transfer_path", "/")
            )
            self._tianyi_media_path = self._config_cloud_path(
                config.get("tianyi_media_path", "/")
            )
            self._alipan_access_token = str(
                config.get("alipan_access_token", "") or ""
            ).strip()
            self._alipan_refresh_token = str(
                config.get("alipan_refresh_token", "") or ""
            ).strip()
            self._alipan_request_timeout = max(
                10, min(int(config.get("alipan_request_timeout", 60) or 60), 300)
            )
            self._alipan_transfer_path = self._config_cloud_path(
                config.get("alipan_transfer_path", "/")
            )
            self._alipan_media_path = self._config_cloud_path(
                config.get("alipan_media_path", "/")
            )
            self._cloud_drive_key = str(
                config.get("cloud_drive", "115") or "115"
            ).strip().lower()

            source_names = (
                "hdhive", "dian115", "pansou", "juying", "seedhub",
                "pinglian", "piratebay", "uindex", "online_docs",
            )
            raw_order = config.get("search_source_order", []) or []
            if isinstance(raw_order, str):
                raw_order = raw_order.split(",")
            self._search_source_order = list(dict.fromkeys(
                str(value).strip().lower()
                for value in raw_order
                if str(value).strip().lower() in source_names
            ))
            selected_sources = set(self._search_source_order)
            raw_search_proxy = str(config.get("search_proxy", "") or "").strip()
            self._search_proxy_username = str(
                config.get("search_proxy_username", "") or ""
            ).strip()
            self._search_proxy_password = str(
                config.get("search_proxy_password", "") or ""
            )
            try:
                self._search_proxy_address = validate_proxy_address(
                    raw_search_proxy
                )
                self._search_proxy = build_proxy_url(
                    self._search_proxy_address,
                    self._search_proxy_username,
                    self._search_proxy_password,
                )
            except ValueError as error:
                logger.error(f"搜索渠道代理配置无效，本次使用直连：{error}")
                self._search_proxy_address = raw_search_proxy
                self._search_proxy = ""
            self._pansou_enabled = "pansou" in selected_sources
            self._pansou_url = config.get("pansou_url", "https://so.252035.xyz/")
            self._dian115_base_url = str(
                config.get("dian115_base_url", "https://m.dian115.com") or "https://m.dian115.com").strip()
            self._juying_base_url = str(
                config.get("juying_base_url", "https://www.jying.top") or "https://www.jying.top").strip()
            self._seedhub_base_url = str(
                config.get("seedhub_base_url", "https://www.seedhub.cc") or "https://www.seedhub.cc").strip()
            self._pinglian_base_url = str(
                config.get("pinglian_base_url", "https://pinglian.lol") or "https://pinglian.lol").strip()
            raw_doc_urls = config.get("online_docs") or config.get("online_docs_urls") or []
            if isinstance(raw_doc_urls, str):
                raw_doc_urls = re.split(r"[,，\n]+", raw_doc_urls)
            elif not isinstance(raw_doc_urls, (list, tuple)):
                raw_doc_urls = [raw_doc_urls]
            self._online_docs = list(raw_doc_urls)
            self._pansou_username = config.get("pansou_username", "")
            self._pansou_password = config.get("pansou_password", "")
            self._pansou_auth_enabled = config.get("pansou_auth_enabled", False)
            self._pansou_channels = config.get("pansou_channels") or []
            self._pansou_plugins = config.get("pansou_plugins") or []
            self._pansou_filter_include = config.get("pansou_filter_include") or []
            self._pansou_filter_exclude = config.get("pansou_filter_exclude") or []
            configured_resource_order = config.get(
                "resource_type_order",
                ["115", "ed2k"],
            )
            if not isinstance(configured_resource_order, list):
                configured_resource_order = [configured_resource_order]
            resource_order = list(dict.fromkeys(
                str(item).strip().lower()
                for item in configured_resource_order
                if str(item).strip().lower()
                in {
                    "115", "123", "quark", "guangya", "tianyi", "alipan",
                    "ed2k", "magnet",
                }
            ))
            self._resource_type_order = resource_order
            self._pansou_cloud_types = list(resource_order)
            configured_metadata_url = str(config.get(
                "magnet_metadata_url_template",
                "https://itorrents.org/torrent/{info_hash}.torrent",
            ) or "").strip()
            self._magnet_metadata_url_template = configure_magnet_metadata_url(
                configured_metadata_url
            )
            if self._magnet_metadata_url_template != configured_metadata_url:
                logger.warning("Magnet元数据地址模板无效，已恢复默认iTorrents地址")
            try:
                self._pansou_concurrency = (
                    max(1, min(int(config.get("pansou_concurrency")), 100))
                    if config.get("pansou_concurrency") else None
                )
            except (TypeError, ValueError):
                self._pansou_concurrency = None
            self._pansou_result_limit = max(
                1, min(int(config.get("pansou_result_limit", 10) or 10), 100)
            )
            self._pansou_refresh = bool(config.get("pansou_refresh", True))
            self._pansou_timeout = max(
                5, min(int(config.get("pansou_timeout", 30) or 30), 120)
            )
            self._pansou_check_enabled = bool(
                config.get("pansou_check_enabled", False)
            )
            self._seedhub_enabled = "seedhub" in selected_sources
            self._seedhub_base_url = str(
                config.get("seedhub_base_url", "https://www.seedhub.cc") or "https://www.seedhub.cc").strip()
            self._seedhub_result_limit = max(
                1, min(int(config.get("seedhub_result_limit", 20) or 20), 80)
            )
            self._seedhub_request_interval = max(
                1.0, min(float(config.get("seedhub_request_interval", 1) or 1), 10.0)
            )
            self._seedhub_timeout = max(
                5, min(int(config.get("seedhub_timeout", 20) or 20), 60)
            )
            self._piratebay_enabled = "piratebay" in selected_sources
            self._piratebay_base_url = str(
                config.get("piratebay_base_url", "https://apibay.org") or "https://apibay.org").strip()
            self._piratebay_result_limit = max(
                1, min(int(config.get("piratebay_result_limit", 20) or 20), 80)
            )
            self._piratebay_request_interval = max(
                0.2, min(float(config.get("piratebay_request_interval", 1) or 1), 10.0)
            )
            self._piratebay_timeout = max(
                5, min(int(config.get("piratebay_timeout", 20) or 20), 60)
            )
            self._uindex_enabled = "uindex" in selected_sources
            self._uindex_base_url = str(
                config.get("uindex_base_url", "https://uindex.org") or "https://uindex.org").strip()
            self._uindex_result_limit = max(
                1, min(int(config.get("uindex_result_limit", 20) or 20), 80)
            )
            self._uindex_request_interval = max(
                0.2, min(float(config.get("uindex_request_interval", 1) or 1), 10.0)
            )
            self._uindex_timeout = max(
                5, min(int(config.get("uindex_timeout", 20) or 20), 60)
            )
            self._juying_enabled = "juying" in selected_sources
            self._juying_username = str(
                config.get("juying_username", "") or ""
            ).strip()
            self._juying_password = str(config.get("juying_password", "") or "")
            self._juying_checkin_enabled = bool(
                config.get("juying_checkin_enabled", False)
            )
            self._juying_result_limit = max(
                1, min(int(config.get("juying_result_limit", 5) or 5), 20)
            )
            self._juying_request_interval = max(
                0.5, min(float(config.get("juying_request_interval", 1) or 1), 10.0)
            )
            self._pinglian_enabled = "pinglian" in selected_sources
            self._pinglian_username = str(
                config.get("pinglian_username", "") or ""
            ).strip()
            self._pinglian_password = str(config.get("pinglian_password", "") or "")
            self._pinglian_result_limit = max(
                1, min(int(config.get("pinglian_result_limit", 20) or 20), 80)
            )
            self._pinglian_request_interval = max(
                0.5,
                min(float(config.get("pinglian_request_interval", 1) or 1), 10.0),
            )
            self._pinglian_timeout = max(
                5, min(int(config.get("pinglian_timeout", 30) or 30), 120)
            )

            self._subscribe_filter_mode = config.get("subscribe_filter_mode", "exclude") or "exclude"
            self._exclude_subscribes = config.get("exclude_subscribes", []) or []
            self._include_subscribes = config.get("include_subscribes", []) or []
            if self._subscribe_filter_mode == "include":
                logger.info(f"订阅过滤模式：指定模式，仅处理 {len(self._include_subscribes)} 个勾选订阅")

            self._checkin_cron = str(
                config.get("checkin_cron")
                or "0 8 * * *"
            ).strip()
            self._checkin_auto_retry = bool(
                config.get("checkin_auto_retry", True)
            )
            self._checkin_retry_count = max(
                1,
                min(10, int(config.get("checkin_retry_count", 2) or 2)),
            )
            self._dian115_enabled = "dian115" in selected_sources
            self._dian115_email = str(config.get("dian115_email", "") or "").strip()
            self._dian115_password = str(config.get("dian115_password", "") or "")
            self._dian115_checkin_enabled = bool(
                config.get("dian115_checkin_enabled", False)
            )
            self._dian115_checkin_mode = str(
                config.get("dian115_checkin_mode", "normal") or "normal"
            ).strip().lower()
            if self._dian115_checkin_mode not in {"normal", "lucky"}:
                self._dian115_checkin_mode = "normal"
            self._dian115_lottery_enabled = bool(
                config.get("dian115_lottery_enabled", False)
            )
            self._dian115_lottery_count = max(
                1, min(20, int(config.get("dian115_lottery_count", 1) or 1))
            )
            self._dian115_auto_unlock = bool(
                config.get("dian115_auto_unlock", False)
            )
            self._dian115_max_unlock_points = max(
                0, int(config.get("dian115_max_unlock_points", 50) or 0)
            )
            self._dian115_max_points_per_sub = max(
                0, int(config.get("dian115_max_points_per_sub", 20) or 0)
            )

            self._hdhive_enabled = "hdhive" in selected_sources
            self._hdhive_base_url = str(
                config.get("hdhive_base_url", "https://re0.me") or "https://re0.me"
            ).strip()
            self._hdhive_query_mode = config.get("hdhive_query_mode", "web")
            self._hdhive_api_key = config.get("hdhive_api_key", "")
            self._hdhive_client_id = config.get("hdhive_client_id", "")
            self._hdhive_redirect_uri = config.get("hdhive_redirect_uri", "")
            self._hdhive_response_mode = config.get("hdhive_response_mode", "redirect")
            self._hdhive_auth_code = config.get("hdhive_auth_code", "")
            self._hdhive_access_token = config.get("hdhive_access_token", "")
            self._hdhive_refresh_token = config.get("hdhive_refresh_token", "")
            self._hdhive_token_expires_at = float(config.get("hdhive_token_expires_at", 0) or 0)
            self._hdhive_auto_unlock = config.get("hdhive_auto_unlock", False)
            self._hdhive_max_unlock_points = int(config.get("hdhive_max_unlock_points", 50) or 50)
            self._hdhive_max_points_per_sub = int(config.get("hdhive_max_points_per_sub", 20) or 20)
            self._hdhive_username = config.get("hdhive_username", "")
            self._hdhive_password = config.get("hdhive_password", "")
            self._hdhive_checkin_enabled = bool(
                config.get("hdhive_checkin_enabled", False)
            )
            self._hdhive_checkin_mode = str(
                config.get("hdhive_checkin_mode", "normal") or "normal"
            ).strip().lower()
            if self._hdhive_checkin_mode not in {"normal", "gambler"}:
                self._hdhive_checkin_mode = "normal"
            self._hdhive_candidate_limit = max(
                1, min(int(config.get("hdhive_candidate_limit", 4) or 4), 20)
            )
            self._hdhive_request_interval = max(
                2.0, min(float(config.get("hdhive_request_interval", 5) or 5), 10.0)
            )
            self._hdhive_unlocks_per_minute = max(
                1, min(int(config.get("hdhive_unlocks_per_minute", 2) or 2), 3)
            )
            self._hdhive_torrentclaw_enabled = bool(
                config.get("hdhive_torrentclaw_enabled", False)
            )
            raw_subtitle_languages = config.get("hdhive_torrentclaw_subtitle_languages")
            if isinstance(raw_subtitle_languages, str):
                raw_subtitle_languages = re.split(r"[,，\s]+", raw_subtitle_languages)
            self._hdhive_torrentclaw_subtitle_languages = [
                                                              str(language).strip().lower().replace("_", "-")
                                                              for language in (raw_subtitle_languages or [])
                                                              if str(language).strip()
                                                          ] or ["zh"]

            self._transfer_task_batch_size = int(
                config.get("transfer_task_batch_size", 50) or 50
            )
            self._cross_transfer_enabled = bool(config.get("cross_transfer_enabled", False))
            raw_cross_types = config.get("cross_transfer_media_types", ["movie", "tv"])
            if isinstance(raw_cross_types, str):
                raw_cross_types = [value.strip().lower() for value in raw_cross_types.split(",")]
            self._cross_transfer_media_types = [
                                                   value for value in (raw_cross_types or []) if
                                                   value in {"movie", "tv"}
                                               ] or ["movie", "tv"]
            self._cross_transfer_download_path = str(
                config.get("cross_transfer_download_path", "") or ""
            ).strip()
            self._cross_transfer_download_threads = max(
                1,
                min(
                    int(config.get("cross_transfer_download_threads", 5) or 5),
                    10,
                ),
            )
            self._cross_transfer_max_concurrent = max(1,
                                                      min(int(config.get("cross_transfer_max_concurrent", 2) or 2), 10))
            self._subscription_concurrency = max(
                1, min(int(config.get("subscription_concurrency", 2) or 2), 5)
            )
            self._batch_size = int(config.get("batch_size", 20) or 20)
            self._batch_interval = max(0, min(float(config.get("batch_interval", 3) or 0), 60))
            self._transfer_risk_cooldown = max(
                60, min(int(config.get("transfer_risk_cooldown", 1800) or 1800), 86400)
            )
            self._skip_other_season_dirs = config.get("skip_other_season_dirs", True)

            if self._search_source_order:
                logger.info(f"搜索资源优先级：{' > '.join(self._search_source_order)}")
            self._search_cache_enabled = bool(config.get("search_cache_enabled", True))
            self._search_cache_ttl_minutes = max(
                1, min(int(config.get("search_cache_ttl_minutes", 30) or 30), 1440)
            )
            self._search_concurrency = max(
                1, min(int(config.get("search_concurrency", 2) or 2), 5)
            )
            self._dian115_candidate_limit = max(
                1, min(int(config.get("dian115_candidate_limit", 4) or 4), 20)
            )
            self._dian115_request_interval = max(
                0.2, min(float(config.get("dian115_request_interval", 1) or 1), 10.0)
            )
            self._dian115_unlocks_per_minute = max(
                1, min(int(config.get("dian115_unlocks_per_minute", 6) or 6), 10)
            )

            # 洗版配置
            self._upgrade_subscribe_ids = config.get("upgrade_subscribe_ids", []) or []
            self._self_heal_interval = int(config.get("self_heal_interval", 10))
            self._enable_cloud_upgrade = bool(config.get("enable_cloud_upgrade", False))
            self._enable_pt_upgrade = bool(config.get("enable_pt_upgrade", False))
            self._upgrade_mode = str(
                config.get("upgrade_mode", "largest") or "largest"
            ).strip().lower()
            if self._upgrade_mode not in {"coexist", "replace", "largest", "smallest"}:
                self._upgrade_mode = "largest"
            self._local_resource_path = str(config.get("local_resource_path", "") or "").strip()
            self._p115_transfer_path = self._config_cloud_path(
                config.get("cloud_transfer_path", "/")
            )
            self._p123_transfer_path = self._config_cloud_path(
                config.get("p123_transfer_path", "/")
            )
            self._quark_transfer_path = self._config_cloud_path(
                config.get("quark_transfer_path", "/")
            )
            self._guangya_transfer_path = self._config_cloud_path(
                config.get("guangya_transfer_path", "/")
            )
            self._p115_media_path = self._config_cloud_path(
                config.get("cloud_media_path", "/")
            )
            self._p123_media_path = self._config_cloud_path(
                config.get("p123_media_path", "/")
            )
            self._quark_media_path = self._config_cloud_path(
                config.get("quark_media_path", "/")
            )
            self._guangya_media_path = self._config_cloud_path(
                config.get("guangya_media_path", "/")
            )
            self._cloud_transfer_path = {
                "115": self._p115_transfer_path,
                "123": self._p123_transfer_path,
                "quark": self._quark_transfer_path,
                "guangya": self._guangya_transfer_path,
                "tianyi": self._tianyi_transfer_path,
                "alipan": self._alipan_transfer_path,
            }.get(self._cloud_drive_key, "/")
            self._cloud_media_path = {
                "115": self._p115_media_path,
                "123": self._p123_media_path,
                "quark": self._quark_media_path,
                "guangya": self._guangya_media_path,
                "tianyi": self._tianyi_media_path,
                "alipan": self._alipan_media_path,
            }.get(self._cloud_drive_key, "/")
            self._movie_media_path = self._config_cloud_path(
                config.get("movie_media_path", "") or self._cloud_media_path
            )
            self._tv_media_path = self._config_cloud_path(
                config.get("tv_media_path", "") or self._cloud_media_path
            )
            self._organize_after_transfer = bool(
                config.get("organize_after_transfer", False)
            )
            try:
                self._max_transfer_links = max(
                    0, min(int(config.get("max_transfer_links", 5) or 5), 500)
                )
            except (TypeError, ValueError):
                self._max_transfer_links = 5
            # 离线下载超时分钟数；旧配置缺省该键时回落默认 120 分钟。
            try:
                self._offline_download_timeout_minutes = max(
                    1,
                    min(
                        int(config.get("offline_download_timeout_minutes", 120) or 120),
                        1440,
                    ),
                )
            except (TypeError, ValueError):
                self._offline_download_timeout_minutes = 120
            self._strm_generate_enabled = bool(config.get("strm_generate_enabled", True))
            self._nfo_scrape_enabled = bool(config.get("nfo_scrape_enabled", False))
            self._image_scrape_enabled = bool(config.get("image_scrape_enabled", False))
            self._strm_base_url = str(
                config.get("strm_base_url", "http://172.17.0.1:9527")
            ).strip().rstrip("/")
            self._strm_url_template = str(
                config.get("strm_url_template", "{base_url}/d/{pickcode}?/{file_name}")
            ).strip()
            self._media_server_refresh_enabled = bool(
                config.get("media_server_refresh_enabled", False)
            )
            self._media_servers = list(config.get("media_servers", []) or [])
            self._media_server_path_mappings = str(
                config.get("media_server_path_mappings", "") or ""
            ).strip()
            self._media_server_refresh_delay = max(
                0, int(config.get("media_server_refresh_delay", 0) or 0)
            )
            self._emby_mediainfo_enabled = bool(
                config.get("emby_mediainfo_enabled", False)
            )
            self._platform_media_sync_enabled = bool(
                config.get("platform_media_sync_enabled", False)
            )
            self._platform_deep_delete_enabled = bool(
                config.get("platform_deep_delete_enabled", False)
            )
            self._platform_transfer_history_enabled = bool(
                config.get("platform_transfer_history_enabled", False)
            )
            self._timeout_enabled = bool(config.get("timeout_enabled", True))
            for key, default in (
                    ("timeout_default_connect", 30),
                    ("timeout_default_pool", 15),
                    ("timeout_default_read", 60),
                    ("timeout_default_write", 60),
                    ("timeout_slow_connect", 30),
                    ("timeout_slow_pool", 15),
                    ("timeout_slow_read", 300),
                    ("timeout_slow_write", 300),
            ):
                setattr(
                    self,
                    f"_{key}",
                    max(0, float(config.get(key, default) or 0)),
                )

            # 订阅接管时段配置
            self._block_start_time = str(
                config.get("block_start_time", self._block_start_time) or self._block_start_time)
            self._block_end_time = str(config.get("block_end_time", self._block_end_time) or self._block_end_time)

            self._block_system_subscribe = bool(config.get("block_system_subscribe", False))
            self._takeover_new_subscribes = bool(
                config.get("takeover_new_subscribes", False)
            )
            self._platform_download_policy = str(
                config.get("platform_download_policy", "block") or "block"
            )

        # 初始化客户端/handlers
        self._init_clients()
        self._init_handlers()
        if self._sync_handler:
            self._sync_handler.sync_platform_transfer_history()
        self._update_offline_monitor(
            len(self._sync_handler.get_pending_finalize_tasks())
            if self._sync_handler else 0
        )
        service_config_keys = {
            "enabled", "cron", "auto_subscribe_enabled", "auto_subscribe_cron",
            "auto_subscribe_douban_enabled", "auto_subscribe_maoyan_enabled",
            "auto_subscribe_netflix_enabled", "auto_subscribe_mikan_enabled",
            "auto_subscribe_tmdb_enabled", "auto_subscribe_bangumi_enabled", "auto_subscribe_anilist_enabled",
            "checkin_cron", "checkin_auto_retry", "checkin_retry_count",
            "p115_checkin_enabled", "hdhive_checkin_enabled",
            "dian115_checkin_enabled", "juying_checkin_enabled",
            "quark_checkin_enabled", "quark_checkin_url",
            "takeover_new_subscribes",
            "block_start_time", "block_end_time", "block_system_subscribe",
            "platform_download_policy", "block_platform_downloads",
            "takeover_platform_downloads",
        }
        if reset_runtime or changed_keys & service_config_keys:
            self._refresh_platform_services()
        self._install_subscribe_search_takeover()
        self._get_component(MessageRoutingHook).install()

        action = "插件初始化" if reset_runtime else "插件配置已应用"
        self._applied_config = copy.deepcopy(config)
        from app.core.plugin import PluginManager
        PluginManager().clear_plugin_agent_tools_cache()
        logger.info(
            f"{action}：接管时段={self._block_start_time}~{self._block_end_time}, "
            f"平台下载策略={self._platform_download_policy}, "
            f"网盘洗版={'开启' if self._enable_cloud_upgrade else '关闭'}, "
            f"洗版模式={self._upgrade_mode}, "
            f"洗版范围={'指定订阅' if self._upgrade_subscribe_ids else '全部'}, "
            f"当前接管态={self._is_takeover_active()}")

        # 调度一次性任务。
        if self._auto_subscribe_onlyonce:
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)
            logger.info("榜单自动订阅服务启动，立即运行一次")
            self._scheduler.add_job(
                func=self._run_auto_subscribe_once,
                trigger="date",
                run_date=(
                    datetime.datetime.now(tz=pytz.timezone(settings.TZ))
                    + datetime.timedelta(seconds=3)
                ),
            )
            self._auto_subscribe_onlyonce = False
            self._persist_config_values(auto_subscribe_onlyonce=False)
            if self._scheduler.get_jobs():
                self._scheduler.start()

    def _apply_notification_config(self, config: Dict[str, Any]) -> None:
        self._notify = bool(config.get("notify", False))
        self._notification_type = self._resolve_notification_type(
            config.get("notification_type")
        )
        self._webhook_enabled = bool(config.get("webhook_enabled", False))
        self._webhook_url = str(config.get("webhook_url", "") or "").strip()
        self._webhook_method = str(config.get("webhook_method", "POST") or "POST").upper()
        if self._webhook_method not in {"POST", "GET"}:
            self._webhook_method = "POST"
        self._webhook_timeout = max(
            1, min(int(config.get("webhook_timeout", 10) or 10), 120)
        )
        self._media_server_refresh_enabled = bool(
            config.get("media_server_refresh_enabled", False)
        )
        self._media_servers = list(config.get("media_servers", []) or [])
        self._media_server_path_mappings = str(
            config.get("media_server_path_mappings", "") or ""
        ).strip()
        self._media_server_refresh_delay = max(
            0, int(config.get("media_server_refresh_delay", 0) or 0)
        )
        self._emby_mediainfo_enabled = bool(
            config.get("emby_mediainfo_enabled", False)
        )
        self._platform_media_sync_enabled = bool(
            config.get("platform_media_sync_enabled", False)
        )
        self._platform_deep_delete_enabled = bool(
            config.get("platform_deep_delete_enabled", False)
        )
        if self._subscribe_handler:
            self._subscribe_handler._notify = self._notify
            self._subscribe_handler._notification_type = self._notification_type
        if self._sync_handler:
            self._sync_handler.update_notification_config(
                notify=self._notify,
                notification_type=self._notification_type,
                media_server_refresh_enabled=self._media_server_refresh_enabled,
                media_servers=self._media_servers,
                media_server_path_mappings=self._media_server_path_mappings,
                media_server_refresh_delay=self._media_server_refresh_delay,
                emby_mediainfo_enabled=self._emby_mediainfo_enabled,
            )
        self._webhook_handler = WebhookHandler(
            enabled=self._webhook_enabled,
            url=self._webhook_url,
            method=self._webhook_method,
            timeout=self._webhook_timeout,
        )

    def _p115_timeout_kwargs(self) -> Dict[str, Any]:
        """组装 p115client 的普通与慢操作超时参数，0 表示不限制。"""

        def timeout_values(prefix: str) -> Dict[str, float]:
            values = {}
            for name in ("connect", "pool", "read", "write"):
                value = float(getattr(self, f"_timeout_{prefix}_{name}", 0) or 0)
                if value > 0:
                    values[name] = value
            return values

        return {
            "timeout_enabled": self._timeout_enabled,
            "default_timeout": timeout_values("default"),
            "slow_timeout": timeout_values("slow"),
        }

    def _init_clients(self):
        """初始化客户端"""
        self._p115_manager = None
        self._p123_drive = None
        self._quark_drive = None
        self._guangya_drive = None
        self._tianyi_drive = None
        self._alipan_drive = None
        self._cloud_drive = None
        self._cloud_drive_registry = CloudDriveRegistry()
        self._pansou_client = None
        self._seedhub_client = None
        self._juying_client = None
        self._pinglian_client = None
        self._online_docs_client = None
        proxy = self._search_proxy
        if proxy:
            logger.info("搜索渠道已启用统一请求代理")

        # 初始化 PanSou 客户端（网盘资源列表与自动订阅通用）
        pansou_url = self._pansou_url or "https://pansou.cc"
        self._pansou_client = PanSouClient(
            base_url=pansou_url,
            username=self._pansou_username,
            password=self._pansou_password,
            auth_enabled=bool(self._pansou_auth_enabled and self._pansou_username and self._pansou_password),
            proxy=proxy,
            search_timeout=self._pansou_timeout,
            get_data_func=self.get_data,
            save_data_func=self.save_data,
        )

        # 初始化 SeedHub 客户端
        self._seedhub_client = SeedHubClient(
            base_url=self._seedhub_base_url or "https://www.seedhub.cc",
            proxy=proxy,
            request_timeout=self._seedhub_timeout,
            request_interval=self._seedhub_request_interval,
        )

        # 初始化海盗湾客户端
        self._piratebay_client = PirateBayClient(
            base_url=self._piratebay_base_url or "https://apibay.org",
            proxy=proxy,
            timeout=self._piratebay_timeout,
            request_interval=self._piratebay_request_interval,
        )

        # 初始化 UIndex 客户端
        self._uindex_client = UIndexClient(
            base_url=self._uindex_base_url or "https://uindex.org",
            proxy=proxy,
            timeout=self._uindex_timeout,
            request_interval=self._uindex_request_interval,
        )
        if self._juying_enabled or self._juying_checkin_enabled:
            self._juying_client = JuyingClient(
                base_url=self._juying_base_url,
                username=self._juying_username,
                password=self._juying_password,
                proxy=proxy,
                request_interval=self._juying_request_interval,
                get_data_func=self.get_data,
                save_data_func=self.save_data,
            )
            if not self._juying_username or not self._juying_password:
                logger.warning("聚影已启用但未配置网页登录账号和密码，将无法使用搜索或签到")
        if self._pinglian_enabled:
            self._pinglian_client = PinglianClient(
                base_url=self._pinglian_base_url,
                username=self._pinglian_username,
                password=self._pinglian_password,
                proxy=proxy,
                request_timeout=self._pinglian_timeout,
                request_interval=self._pinglian_request_interval,
                get_data_func=self.get_data,
                save_data_func=self.save_data,
            )
            if not self._pinglian_username or not self._pinglian_password:
                logger.warning("盘链已启用但未配置网页登录账号和密码，将无法使用盘链搜索")
        if "online_docs" in self._search_source_order and self._online_docs:
            try:
                online_docs_cache_dir = self.get_data_path()
            except Exception as error:
                logger.debug(f"[PanSearch] 获取数据目录失败，在线文档缓存降级为内存模式：{error}")
                online_docs_cache_dir = None
            self._online_docs_client = OnlineDocumentClient(
                documents=self._online_docs,
                proxy=proxy,
                cache_dir=online_docs_cache_dir,
                cache_ttl_hours=6,
            )

        # OpenAPI 模式初始化官方客户端；WebAPI 由搜索服务按需创建唯一客户端。
        if self._hdhive_query_mode == "api":
            self._init_hdhive_openapi_client(proxy)
        else:
            self._hdhive_client = None
        if self._hdhive_enabled:
            if self._hdhive_query_mode == "web" and (not self._hdhive_username or not self._hdhive_password):
                logger.warning("HDHive WebAPI 已启用但未配置用户名和密码，将无法使用 HDHive 查询功能")
            elif self._hdhive_query_mode == "api" and (not self._hdhive_client or not self._hdhive_client.is_ready):
                logger.warning("HDHive (API 模式) 已启用但未完成 OpenAPI 应用配置和用户授权，将无法使用 HDHive 查询功能")
            else:
                logger.info(f"HDHive 配置已加载（模式：{self._hdhive_query_mode}）")

        self._p115_manager = P115ClientManager(
            cookies=self._p115_cookies,
            share_cache_ttl_minutes=self._search_cache_ttl_minutes,
            **self._p115_timeout_kwargs(),
        )
        if (
                self._p115_cookies
                and self._p115_manager.check_login()
                and not self._p115_manager.is_vip
        ):
            logger.warning("当前 115 账号不是会员，普通分享转存可用，ED2K 离线下载不可用")
        self._register_p115_provider()
        self._register_p123_provider()
        self._register_quark_provider()
        self._register_guangya_provider()
        self._register_tianyi_provider()
        self._register_alipan_provider()
        self._cross_transfer_manager = CrossTransferTaskManager(
            self._cloud_drive_registry.get,
            download_path=self._cross_transfer_download_path,
            download_threads=self._cross_transfer_download_threads,
            max_concurrent=self._cross_transfer_max_concurrent,
            on_change=self._mark_runtime_changed,
        )

        try:
            self._cloud_drive = self._cloud_drive_registry.get(self._cloud_drive_key)
        except KeyError:
            logger.warning(
                f"网盘提供方 {self._cloud_drive_key or '<empty>'} 未就绪，请检查对应账号配置"
            )
        if self._cloud_drive:
            aliases = {
                "189": "tianyi", "aliyun": "alipan"
            }

            def resource_supported(value: str) -> bool:
                if self._cloud_drive.supports_resource_type(value):
                    return True
                if not self._cross_transfer_enabled:
                    return False
                try:
                    source = self._cloud_drive_registry.get(
                        aliases.get(str(value).lower(), str(value).lower())
                    )
                except KeyError:
                    return False
                return (
                        source.supports(CloudDriveCapability.SHARE_TRANSFER)
                        and source.supports(CloudDriveCapability.FILE_DOWNLOAD)
                        and self._cloud_drive.supports(CloudDriveCapability.LOCAL_UPLOAD)
                )

            unsupported_types = [
                value for value in self._resource_type_order
                if not resource_supported(value)
            ]
            self._resource_type_order = [
                value for value in self._resource_type_order
                if resource_supported(value)
            ]
            if unsupported_types:
                logger.info(
                    f"当前转存网盘为 {self._cloud_drive.name}，已忽略不支持的资源类型："
                    f"{', '.join(unsupported_types)}"
                )

    def _register_p115_provider(self) -> None:
        """注册当前 115 客户端提供的能力，并更新活动提供方。"""
        if not self._p115_manager:
            return
        if self._cloud_drive_registry is None:
            self._cloud_drive_registry = CloudDriveRegistry()
        self._cloud_drive_registry.register(
            create_p115_provider(self._p115_manager), replace=True
        )
        if self._cloud_drive_key == "115":
            self._cloud_drive = self._cloud_drive_registry.get("115")

    def _on_quark_cookie_update(self, cookie: str) -> None:
        cookie = str(cookie or "").strip()
        if not cookie or cookie == self._quark_cookie:
            return
        self._quark_cookie = cookie
        self._persist_config_values(quark_cookie=cookie)

    def _register_p123_provider(self) -> None:
        """注册 123 网盘账号、分享转存和文件操作能力。"""
        if self._p123_drive:
            self._p123_drive.close()
        self._p123_drive = P123Drive(
            client=P123ClientManager(
                token=self._p123_token,
                timeout=self._p123_request_timeout,
            ),
        )
        if self._cloud_drive_registry is None:
            self._cloud_drive_registry = CloudDriveRegistry()
        self._cloud_drive_registry.register(
            create_p123_provider(self._p123_drive), replace=True
        )
        if self._cloud_drive_key == "123":
            self._cloud_drive = self._cloud_drive_registry.get("123")

    def _register_quark_provider(self) -> None:
        """注册夸克网盘，并允许空凭证实例提供扫码入口。"""
        if self._quark_drive:
            self._quark_drive.close()
        client = QuarkClient(
            cookie=self._quark_cookie,
            on_cookie_refresh=self._on_quark_cookie_update,
            timeout=self._quark_request_timeout,
        )
        client.risk_cooldown = self._transfer_risk_cooldown
        self._quark_drive = QuarkDrive(client=client)
        if self._cloud_drive_registry is None:
            self._cloud_drive_registry = CloudDriveRegistry()
        self._cloud_drive_registry.register(
            create_quark_provider(self._quark_drive), replace=True
        )
        if self._cloud_drive_key == "quark":
            self._cloud_drive = self._cloud_drive_registry.get("quark")

    def _on_guangya_token_update(
            self, access_token: str, refresh_token: str
    ) -> None:
        self._guangya_access_token = str(access_token or "").strip()
        self._guangya_refresh_token = str(refresh_token or "").strip()
        self._persist_config_values(
            guangya_access_token=self._guangya_access_token,
            guangya_refresh_token=self._guangya_refresh_token,
        )

    def _register_guangya_provider(self) -> None:
        """注册光鸭网盘，并允许空凭证实例提供扫码入口。"""
        if self._guangya_drive:
            self._guangya_drive.close()
        client = GuangyaClient(
            access_token=self._guangya_access_token,
            refresh_token=self._guangya_refresh_token,
            client_id=self._guangya_client_id,
            device_id=self._guangya_device_id,
            on_token_refresh=self._on_guangya_token_update,
            timeout=self._guangya_request_timeout,
        )
        self._guangya_client_id = client.client_id
        self._guangya_device_id = client.device_id
        self._guangya_drive = GuangyaDrive(
            client=client,
        )
        if self._cloud_drive_registry is None:
            self._cloud_drive_registry = CloudDriveRegistry()
        self._cloud_drive_registry.register(
            create_guangya_provider(self._guangya_drive), replace=True
        )
        if self._cloud_drive_key == "guangya":
            self._cloud_drive = self._cloud_drive_registry.get("guangya")

    def _register_tianyi_provider(self) -> None:
        if self._tianyi_drive:
            self._tianyi_drive.close()
        self._tianyi_drive = TianyiDrive(TianyiClient(
            cookie=self._tianyi_cookie,
            timeout=self._tianyi_request_timeout,
            access_token=self._tianyi_access_token,
            refresh_token=self._tianyi_refresh_token,
            session_key=self._tianyi_session_key,
            on_token_refresh=self._on_tianyi_token_update,
        ))
        self._cloud_drive_registry.register(create_tianyi_provider(self._tianyi_drive), replace=True)

    def _on_tianyi_token_update(
            self, access_token: str, refresh_token: str, session_key: str
    ) -> None:
        self._tianyi_access_token = str(access_token or "").strip()
        self._tianyi_refresh_token = str(refresh_token or "").strip()
        self._tianyi_session_key = str(session_key or "").strip()
        self._persist_config_values(
            tianyi_access_token=self._tianyi_access_token,
            tianyi_refresh_token=self._tianyi_refresh_token,
            tianyi_session_key=self._tianyi_session_key,
        )

    def _on_alipan_token_update(
            self, access_token: str, refresh_token: str
    ) -> None:
        self._alipan_access_token = str(access_token or "").strip()
        self._alipan_refresh_token = str(refresh_token or "").strip()
        self._persist_config_values(
            alipan_access_token=self._alipan_access_token,
            alipan_refresh_token=self._alipan_refresh_token,
        )

    def _register_alipan_provider(self) -> None:
        """注册阿里云盘，并允许空凭证实例提供扫码入口。"""
        if self._alipan_drive:
            self._alipan_drive.close()
        self._alipan_drive = AliPanDrive(AliPanClient(
            access_token=self._alipan_access_token,
            refresh_token=self._alipan_refresh_token,
            timeout=self._alipan_request_timeout,
            on_token_refresh=self._on_alipan_token_update,
        ))
        self._cloud_drive_registry.register(
            create_alipan_provider(self._alipan_drive), replace=True
        )
        if self._cloud_drive_key == "alipan":
            self._cloud_drive = self._cloud_drive_registry.get("alipan")

    def _on_hdhive_token_update(self, tokens: Dict[str, Any]):
        """Token 刷新后同步插件配置。"""
        self._hdhive_access_token = str(tokens.get("access_token") or self._hdhive_access_token).strip()
        self._hdhive_refresh_token = str(tokens.get("refresh_token") or self._hdhive_refresh_token).strip()
        self._hdhive_token_expires_at = float(tokens.get("token_expires_at") or self._hdhive_token_expires_at or 0)
        self._persist_config_values(
            hdhive_access_token=self._hdhive_access_token,
            hdhive_refresh_token=self._hdhive_refresh_token,
            hdhive_token_expires_at=self._hdhive_token_expires_at,
        )

    def _init_hdhive_openapi_client(self, proxy=None):
        """
        初始化 HDHive OpenAPI 客户端，并处理一次性授权码换 Token

        新版接入模型：
        1. 在 HDHive 创建 OpenAPI 应用，审核通过后获得 client_id 和应用 Secret
        2. 配置 client_id、应用 Secret、回调地址后保存，从日志中复制授权链接到浏览器完成授权
        3. 将回调地址中的 code 参数填入"授权码"并保存，插件自动换取用户 Token
        """
        self._hdhive_client = None

        client = HDHiveOpenAPIClient(
            app_secret=self._hdhive_api_key,
            client_id=self._hdhive_client_id,
            access_token=self._hdhive_access_token,
            refresh_token=self._hdhive_refresh_token,
            token_expires_at=self._hdhive_token_expires_at,
            proxy=proxy,
            request_interval=self._hdhive_request_interval,
            on_token_update=self._on_hdhive_token_update,
        )
        self._hdhive_client = client

        if not client.app_secret:
            if self._hdhive_enabled:
                logger.warning("HDHive OpenAPI: 缺少应用 Secret；Token 已加载但无法调用官方接口")
            return

        # 一次性授权码优先换取 Token；缺少回调地址时保留授权码，待补全配置后再处理。
        if self._hdhive_auth_code:
            if not self._hdhive_redirect_uri:
                logger.warning("HDHive OpenAPI: 已填写授权码但缺少回调地址，授权码已保留")
            else:
                try:
                    data = client.exchange_code(self._hdhive_auth_code, self._hdhive_redirect_uri)
                    self._hdhive_auth_code = ""
                    scopes = data.get("scope") or " ".join(data.get("scopes") or [])
                    logger.info(f"HDHive OpenAPI: 用户授权成功，已获取 Token（scope: {scopes}）")
                    self._persist_config_values(hdhive_auth_code="")
                except HDHiveOpenAPIError as e:
                    logger.error(
                        f"HDHive OpenAPI: 授权码换取 Token 失败，授权码已保留: "
                        f"[{e.code}] {e.message} {e.description}"
                    )
                except Exception as e:
                    logger.error(f"HDHive OpenAPI: 授权码换取 Token 异常，授权码已保留: {e}")

        # 文件或配置可只提供 Refresh Token，且没有待处理授权码时自动换取 Access Token。
        if not client.access_token and client.refresh_token:
            try:
                client.refresh_access_token()
            except HDHiveOpenAPIError as e:
                logger.error(
                    f"HDHive OpenAPI: 使用 Refresh Token 获取 Access Token 失败: [{e.code}] {e.message} {e.description}")

        # 未完成授权时，打印授权链接引导用户操作
        if not client.is_ready:
            if self._hdhive_client_id and self._hdhive_redirect_uri:
                authorize_url = client.build_authorize_url(
                    self._hdhive_redirect_uri,
                    response_mode=self._hdhive_response_mode,
                )
                logger.warning(
                    f"HDHive OpenAPI: 尚未完成用户授权，请在浏览器打开以下链接完成授权，"
                    f"然后将回调地址中的 code 参数填入插件配置的「授权码」并保存：\n{authorize_url}"
                )
            else:
                missing = []
                if not self._hdhive_client_id:
                    missing.append("Client ID")
                if not self._hdhive_redirect_uri:
                    missing.append("回调地址")
                if not client.access_token and not client.refresh_token:
                    missing.append("Access/Refresh Token")
                logger.warning(f"HDHive OpenAPI: 当前配置尚不能完成用户授权，缺少：{', '.join(missing)}")

    def _init_subscribe_handler(self):
        self._subscribe_handler = SubscribeHandler(
            exclude_subscribes=self._exclude_subscribes,
            is_excluded_func=self._is_subscribe_excluded
        )

    def _init_handlers(self):
        self._init_subscribe_handler()

        self._search_handler = SearchHandler(
            pansou_client=self._pansou_client,
            hdhive_client=self._hdhive_client,
            seedhub_client=self._seedhub_client,
            piratebay_client=self._piratebay_client,
            uindex_client=self._uindex_client,
            juying_client=self._juying_client,
            pinglian_client=self._pinglian_client,
            online_docs_client=self._online_docs_client,
            pansou_enabled=self._pansou_enabled,
            hdhive_enabled=self._hdhive_enabled,
            dian115_enabled=self._dian115_enabled,
            seedhub_enabled=self._seedhub_enabled,
            piratebay_enabled=self._piratebay_enabled,
            piratebay_result_limit=self._piratebay_result_limit,
            uindex_enabled=self._uindex_enabled,
            uindex_result_limit=self._uindex_result_limit,
            juying_enabled=self._juying_enabled,
            pinglian_enabled=self._pinglian_enabled,
            hdhive_query_mode=self._hdhive_query_mode,
            hdhive_auto_unlock=self._hdhive_auto_unlock,
            hdhive_max_unlock_points=self._hdhive_max_unlock_points,
            hdhive_max_points_per_sub=self._hdhive_max_points_per_sub,
            hdhive_username=self._hdhive_username,
            hdhive_password=self._hdhive_password,
            dian115_email=self._dian115_email,
            dian115_password=self._dian115_password,
            dian115_auto_unlock=self._dian115_auto_unlock,
            dian115_max_unlock_points=self._dian115_max_unlock_points,
            dian115_max_points_per_sub=self._dian115_max_points_per_sub,
            pansou_channels=self._pansou_channels,
            pansou_plugins=self._pansou_plugins,
            pansou_cloud_types=self._pansou_cloud_types,
            pansou_filter_include=self._pansou_filter_include,
            pansou_filter_exclude=self._pansou_filter_exclude,
            resource_type_order=self._resource_type_order,
            pansou_concurrency=self._pansou_concurrency,
            pansou_result_limit=self._pansou_result_limit,
            pansou_refresh=self._pansou_refresh,
            pansou_timeout=self._pansou_timeout,
            seedhub_result_limit=self._seedhub_result_limit,
            juying_result_limit=self._juying_result_limit,
            pinglian_result_limit=self._pinglian_result_limit,
            search_source_order=self._search_source_order,
            search_proxy=self._search_proxy,
            search_cache_enabled=self._search_cache_enabled,
            search_cache_ttl_minutes=self._search_cache_ttl_minutes,
            search_concurrency=self._search_concurrency,
            hdhive_candidate_limit=self._hdhive_candidate_limit,
            hdhive_request_interval=self._hdhive_request_interval,
            hdhive_unlocks_per_minute=self._hdhive_unlocks_per_minute,
            dian115_candidate_limit=self._dian115_candidate_limit,
            dian115_request_interval=self._dian115_request_interval,
            dian115_unlocks_per_minute=self._dian115_unlocks_per_minute,
            hdhive_torrentclaw_enabled=self._hdhive_torrentclaw_enabled,
            hdhive_torrentclaw_subtitle_languages=(
                self._hdhive_torrentclaw_subtitle_languages
            ),
            enable_cloud_upgrade=self._enable_cloud_upgrade,
            upgrade_subscribe_ids=self._upgrade_subscribe_ids,
            should_stop=self._stop_requested,
        )
        # 积分花费属于业务状态，不是可丢弃的搜索缓存。
        self._search_handler.configure_point_storage(
            self.get_data, self.save_data
        )
        self._sync_handler = SyncHandler(
            cloud_drive=self._cloud_drive,
            search_handler=self._search_handler,
            subscribe_handler=self._subscribe_handler,
            chain=self.chain,
            cloud_transfer_path=self._cloud_transfer_path,
            cloud_media_root=self._cloud_media_path,
            movie_media_root=self._movie_media_path,
            tv_media_root=self._tv_media_path,
            organize_after_transfer=self._organize_after_transfer,
            cloud_transfer_paths={
                "115": self._p115_transfer_path,
                "123": self._p123_transfer_path,
                "quark": self._quark_transfer_path,
                "guangya": self._guangya_transfer_path,
                "tianyi": self._tianyi_transfer_path,
                "alipan": self._alipan_transfer_path,
            },
            transfer_task_batch_size=self._transfer_task_batch_size,
            cross_transfer_enabled=self._cross_transfer_enabled,
            cross_transfer_media_types=self._cross_transfer_media_types,
            cloud_drive_registry=self._cloud_drive_registry,
            cross_transfer_manager=self._cross_transfer_manager,
            batch_size=self._batch_size,
            batch_interval=self._batch_interval,
            transfer_risk_cooldown=self._transfer_risk_cooldown,
            skip_other_season_dirs=self._skip_other_season_dirs,
            notify=self._notify,
            notification_type=self._notification_type,
            post_message_func=self.post_message,
            get_data_func=self.get_data,
            save_data_func=self.save_data,
            self_heal_interval=self._self_heal_interval,
            enable_cloud_upgrade=self._enable_cloud_upgrade,
            enable_pt_upgrade=self._enable_pt_upgrade,
            upgrade_mode=self._upgrade_mode,
            upgrade_subscribe_ids=self._upgrade_subscribe_ids,
            local_resource_path=self._local_resource_path,
            strm_generate_enabled=self._strm_generate_enabled,
            nfo_scrape_enabled=self._nfo_scrape_enabled,
            image_scrape_enabled=self._image_scrape_enabled,
            strm_base_url=self._strm_base_url,
            strm_url_template=self._strm_url_template,
            media_server_refresh_enabled=self._media_server_refresh_enabled,
            media_servers=self._media_servers,
            media_server_path_mappings=self._media_server_path_mappings,
            media_server_refresh_delay=self._media_server_refresh_delay,
            emby_mediainfo_enabled=self._emby_mediainfo_enabled,
            platform_transfer_history_enabled=(
                self._platform_transfer_history_enabled
            ),
            should_stop=self._stop_requested,
            offline_pending_changed=self._update_offline_monitor,
            history_changed=self._mark_history_changed,
            file_finalized=self._on_file_finalized,
            task_update=self._update_sync_task,
            task_context=self._current_task_context,
            pansou_client=getattr(self, "_pansou_client", None),
            pansou_check_enabled=self._pansou_check_enabled,
            max_transfer_links=self._max_transfer_links,
            offline_download_timeout_minutes=(
                self._offline_download_timeout_minutes
            ),
        )
        self._sync_handler.reconcile_orphaned_history()

        self._webhook_handler = WebhookHandler(
            enabled=self._webhook_enabled,
            url=self._webhook_url,
            method=self._webhook_method,
            timeout=self._webhook_timeout,
        )

    def _on_file_finalized(
            self,
            transfer_details: List[Dict[str, Any]],
            total_count: int,
    ) -> None:
        """离线文件真正就绪后发送一次汇总 Webhook。"""
        if not self._webhook_handler:
            return
        self._webhook_handler.send_transfer_complete(
            transfer_details=transfer_details,
            total_count=total_count,
        )

    def _persist_config_values(self, **updates: Any) -> None:
        """基于当前完整配置更新少量运行时值，避免遗漏其他配置项。"""
        config = copy.deepcopy(self.get_config() or self._applied_config or {})
        config.update(updates)
        if not self.update_config(config):
            logger.warning(f"插件运行时配置持久化失败：{', '.join(updates)}")
        self._applied_config = config

    def _run_auto_subscribe_once(self) -> None:
        try:
            self.run_auto_subscribe()
        except Exception as error:
            logger.error(f"榜单自动订阅立即执行失败：{error}")

    def stop_service(self, preserve_subscribe_queue: bool = False):
        """停止服务"""
        if not preserve_subscribe_queue and self._subscribe_search_queue_lock is not None:
            self.cancel_pending_subscribe_searches(shutdown=True)
            executor = self._sync_operation_executor
            self._sync_operation_executor = None
            if executor:
                executor.shutdown(wait=False, cancel_futures=True)
        if self._stop_event:
            self._stop_event.set()
        if self._sync_tasks_lock is not None:
            with self._sync_tasks_lock:
                for task in self._sync_tasks.values():
                    stop_event = task.get("stop_event")
                    if stop_event:
                        stop_event.set()
        self._restore_subscribe_search_takeover()
        try:
            components = self.__dict__.get("_plugin_components", {})
            message_hook = components.pop(MessageRoutingHook, None)
            if message_hook:
                message_hook.close()
        except Exception as error:
            logger.debug(f"恢复平台消息路由失败：{error}")
        try:
            components = self.__dict__.get("_plugin_components", {})
            search_api = components.get(SearchApi)
            if search_api:
                search_api.close()
        except Exception as error:
            logger.debug(f"关闭搜索测试连接失败：{error}")
        try:
            components = self.__dict__.get("_plugin_components", {})
            media_library_api = components.get(MediaLibraryApi)
            if media_library_api:
                media_library_api.close()
        except Exception as error:
            logger.debug(f"关闭媒体库 Webhook 同步调度器失败：{error}")
        try:
            if self._sync_handler:
                self._sync_handler.close()
        except Exception as error:
            logger.debug(f"关闭媒体服务器入库通知器失败：{error}")
        try:
            if self._search_handler:
                self._search_handler.close(release_cache=True)
        except Exception as error:
            logger.debug(f"关闭搜索客户端失败：{error}")
        try:
            if self._p115_manager:
                self._p115_manager.close()
        except Exception as error:
            logger.debug(f"关闭115客户端缓存失败：{error}")
        try:
            if self._p123_drive:
                self._p123_drive.close()
        except Exception as error:
            logger.debug(f"关闭123客户端失败：{error}")
        try:
            if self._quark_drive:
                self._quark_drive.close()
        except Exception as error:
            logger.debug(f"关闭夸克客户端失败：{error}")
        try:
            if self._guangya_drive:
                self._guangya_drive.close()
        except Exception as error:
            logger.debug(f"关闭光鸭客户端失败：{error}")
        for drive, name in (
                (self._tianyi_drive, "天翼"),
                (self._alipan_drive, "阿里云盘"),
        ):
            try:
                if drive:
                    drive.close()
            except Exception as error:
                logger.debug(f"关闭{name}客户端失败：{error}")
        try:
            components = self.__dict__.get("_plugin_components", {})
            platform_service = components.pop(PlatformIntegrationService, None)
            if platform_service:
                platform_service.close()
        except Exception as error:
            logger.debug(f"关闭平台缓存失败：{error}")
        # 注意：必须使用 shutdown(wait=False)。
        # 本方法可能在调度器自身的 job 线程内被间接触发
        # （monitor_offline_tasks → _refresh_postprocessing_sync_tasks
        #   → 插件同步 → PluginManager.init_config → stop → stop_service）。
        # 此时若按 APScheduler 默认 wait=True 关闭，BlockingScheduler 会在
        # 执行器线程池上 join 掉「包含当前线程」的 worker，形成自我等待，
        # 由于本方法跑在 uvicorn 主线程上，会直接冻结事件循环——
        # 表现为页面能开、所有 /api/v1 请求一直挂起、无法登录。
        # runtime.py 中清空队列处同样使用 wait=False，此处保持一致。
        #
        # v1.5.10：务必「先摘引用、再关调度器」。若先 shutdown(wait=False) 再置空，
        # 存在一个竞态窗口：shutdown 返回后 .running 仍可能短暂为 True，而 job 已被
        # remove_all_jobs() 摘除；此时若其他路径（_update_offline_monitor）读到该实例
        # 并调用 modify_job，会抛 JobLookupError 导致离线监控器再也无法重建。
        try:
            scheduler = self._scheduler
            self._scheduler = None
            if scheduler:
                scheduler.remove_all_jobs()
                if scheduler.running:
                    scheduler.shutdown(wait=False)
        except Exception:
            pass

        try:
            offline_scheduler = self._offline_scheduler
            self._offline_scheduler = None
            if offline_scheduler:
                offline_scheduler.remove_all_jobs()
                if offline_scheduler.running:
                    offline_scheduler.shutdown(wait=False)
        except Exception:
            pass

        try:
            store = self.__dict__.get("_pansearch_data_store")
            if store:
                store.close()
        except Exception as error:
            logger.debug(f"关闭插件独立数据库失败：{error}")

    @eventmanager.register(EventType.PluginReload)
    def reload_plugin_api(self, event: Event):
        """热重载完成后同步刷新本插件新增或变更的 API 路由。"""
        event_data = event.event_data if event else None
        if isinstance(event_data, dict) and event_data.get("plugin_id") == self.__class__.__name__:
            register_plugin_api(plugin_id=self.__class__.__name__)
