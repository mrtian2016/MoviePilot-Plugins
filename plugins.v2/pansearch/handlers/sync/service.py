"""
同步处理模块
负责核心的同步逻辑：处理电影订阅、处理电视剧订阅
"""
import copy
import datetime
import hashlib
import re
import threading
import time
from concurrent.futures import CancelledError, Future, ThreadPoolExecutor, as_completed
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from typing import List, Dict, Any, Set, Optional, Callable, Tuple, Mapping

from app.core.config import global_vars
from app.core.context import MediaInfo
from app.core.metainfo import MetaInfo
from app.db import SessionFactory
from app.db.subscribe_oper import SubscribeOper
from app.helper.directory import DirectoryHelper
from app.log import logger
from app.modules.filemanager import FileManagerModule
from app.modules.filemanager.transhandler import TransHandler
from app.schemas.types import MediaType, NotificationType
from app.utils.http import RequestUtils

from .baseline import UpgradeBaselineService
from .history import HistoryService
from .matching import FileMatchingService
from .movie import MovieSyncProcessor
from .postprocess import PostprocessService
from .pt_upgrade import PtUpgradeService
from .resources import ResourceTransferService
from .rule_scoring import UpgradeRuleScoringService
from .subtitles import SubtitleService
from .television import TelevisionSyncProcessor
from .upgrade import UpgradeService
from ..notification import EmbyMediaResolver, MediaServerNotifier
from ..search import SearchHandler
from ..subscription import SubscribeHandler
from ...core import (
    CloudDriveCapability,
    CloudFile,
    CloudDriveProvider,
    get_component,
    resolve_component,
    MediaScraper,
)
from ...core.media import (
    apply_media_identity,
    legacy_media_ids,
    list_subscribes_by_tmdb_id,
    media_identity,
    recognize_media,
    tmdb_id_of,
    tmdb_identity_update,
)
from ...utils import FileMatcher, MediaFileParser, StrmGenerator, StrmTemplateError
from ...utils.cache import create_platform_ttl_cache, normalize_platform_cache_key

_COMPONENT_TYPES = (
    MovieSyncProcessor,
    TelevisionSyncProcessor,
    HistoryService,
    FileMatchingService,
    PostprocessService,
    UpgradeBaselineService,
    UpgradeRuleScoringService,
    ResourceTransferService,
    SubtitleService,
    UpgradeService,
    PtUpgradeService,
)


class _TmdbSeasonPageParser(HTMLParser):
    """解析 TMDB 季页面中服务端渲染的剧集卡片。"""

    def __init__(self, season: int):
        super().__init__(convert_charrefs=True)
        self.season = int(season)
        self.episodes: Dict[int, str] = {}
        self._card_depth = 0
        self._card_episode = 0
        self._episode_depth = 0
        self._date_depth = 0
        self._text: List[str] = []
        self._field = ""

    @staticmethod
    def _classes(attrs) -> Set[str]:
        return set(str(dict(attrs).get("class") or "").split())

    def _finish_card(self) -> None:
        if self._card_episode > 0 and self._text:
            raw = "".join(self._text).strip()
            match = re.search(r"(\d{4})\s*[年/-]\s*(\d{1,2})\s*[月/-]\s*(\d{1,2})", raw)
            if match:
                self.episodes[self._card_episode] = (
                    f"{int(match.group(1)):04d}-{int(match.group(2)):02d}-{int(match.group(3)):02d}"
                )
        self._card_episode = 0
        self._text = []
        self._field = ""

    def handle_starttag(self, tag, attrs):
        attrs_dict = dict(attrs)
        classes = self._classes(attrs)
        if tag == "div" and "card" in classes:
            if self._card_depth:
                self._finish_card()
            self._card_depth = 1
            url = str(attrs_dict.get("data-url") or "")
            match = re.search(r"/season/(\d+)/episode/(\d+)", url)
            self._card_episode = int(match.group(2)) if match and int(match.group(1)) == self.season else 0
            return
        if not self._card_depth:
            return
        if tag == "div":
            self._card_depth += 1
        if tag in {"span", "div"} and "episode_number" in classes:
            self._episode_depth = self._card_depth
            self._field = "episode"
            self._text = []
        elif tag in {"span", "div"} and "date" in classes:
            self._date_depth = self._card_depth
            self._field = "date"
            self._text = []

    def handle_endtag(self, tag):
        if not self._card_depth:
            return
        if self._field == "episode" and self._card_depth == self._episode_depth:
            try:
                self._card_episode = int("".join(self._text).strip())
            except ValueError:
                self._card_episode = 0
            self._field = ""
        elif self._field == "date" and self._card_depth == self._date_depth:
            self._field = ""
        if tag == "div":
            self._card_depth -= 1
            if not self._card_depth:
                self._finish_card()

    def handle_data(self, data):
        if self._field in {"episode", "date"}:
            self._text.append(data)

    def close(self):
        super().close()
        self._finish_card()


class SyncHandler:
    """同步处理器"""

    _OFFLINE_PENDING_KEY = "pending_offline_strm"
    _OFFLINE_CHECK_DELAYS = (10, 20, 40, 60, 120, 300)
    # 离线下载超时兜底默认（分钟）；实际以实例配置
    # offline_download_timeout_minutes 覆盖（见 __init__）。
    _OFFLINE_TIMEOUT = 120 * 60
    # 文件终审窗口兜底默认；与离线下载超时同源同配置，不再独立硬编码 30 分钟，
    # 避免网盘已保存但定位稍慢的文件被第二处窗口误判为失败。
    _FILE_FINALIZE_TIMEOUT = 120 * 60
    # 终审判死前全盘兜底检索的递归深度。
    _FULL_PAN_SEARCH_DEPTH = 6
    _OFFLINE_MONITOR_LEASE_SECONDS = 15 * 60
    _MEDIA_RECOGNITION_CACHE_LIMIT = 256
    _PLATFORM_ROOT_CACHE_LIMIT = 256
    _RESOURCE_SEASON_DIR_CACHE_LIMIT = 256
    _SUBSCRIBE_DEFER_CACHE_LIMIT = 512
    _SUBSCRIBE_CALENDAR_CACHE_LIMIT = 512
    _RUNTIME_CACHE_TTL = 6 * 60 * 60
    _SUBSCRIBE_DEFER_CACHE_TTL = 32 * 24 * 60 * 60
    _SUBSCRIBE_CALENDAR_CACHE_TTL = 26 * 60 * 60
    _NOTIFICATION_BATCH_WINDOW_SECONDS = 2
    _CLOUD_MEDIA_ROOT = "/"
    _MOVIE_MEDIA_ROOT = "/"
    _TV_MEDIA_ROOT = "/"
    _OFFLINE_RESOURCE_URL_RE = re.compile(
        r"ed2k://\|file\|[^|\r\n]+\|\d+\|[0-9A-Fa-f]{32}"
        r"(?:\|(?:h|p)=[^|\r\n]+)*\|/|magnet:\?[^\s\r\n]+",
        re.IGNORECASE,
    )

    def _get_component(self, component_type):
        return get_component(self, component_type, "_handler_components")

    def __getattr__(self, name):
        return resolve_component(self, _COMPONENT_TYPES, name, "_handler_components")

    def __init__(
            self,
            cloud_drive: Optional[CloudDriveProvider],
            search_handler: SearchHandler,
            subscribe_handler: SubscribeHandler,
            chain,
            cloud_transfer_path: str,
            cloud_media_root: str = "/",
            movie_media_root: str = "/",
            tv_media_root: str = "/",
            organize_after_transfer: bool = False,
            cloud_transfer_paths: Optional[Mapping[str, str]] = None,
            transfer_task_batch_size: int = 50,
            cross_transfer_enabled: bool = False,
            cross_transfer_media_types: Optional[List[str]] = None,
            cloud_drive_registry=None,
            cross_transfer_manager=None,
            batch_size: int = 20,
            batch_interval: float = 3,
            transfer_risk_cooldown: int = 1800,
            skip_other_season_dirs: bool = True,
            notify: bool = False,
            notification_type: NotificationType = NotificationType.Plugin,
            post_message_func: Callable = None,
            get_data_func: Callable = None,
            save_data_func: Callable = None,
            self_heal_interval: int = 10,
            enable_cloud_upgrade: bool = False,
            enable_pt_upgrade: bool = False,
            upgrade_mode: str = "largest",
            upgrade_subscribe_ids: Optional[List[int]] = None,
            local_resource_path: str = "",
            strm_generate_enabled: bool = True,
            nfo_scrape_enabled: bool = False,
            image_scrape_enabled: bool = False,
            strm_base_url: str = StrmGenerator.DEFAULT_BASE_URL,
            strm_url_template: str = StrmGenerator.DEFAULT_TEMPLATE,
            media_server_refresh_enabled: bool = False,
            media_servers: Optional[List[str]] = None,
            media_server_path_mappings: str = "",
            media_server_refresh_delay: int = 0,
            emby_mediainfo_enabled: bool = False,
            platform_transfer_history_enabled: bool = False,
            should_stop: Callable[[], bool] = None,
            offline_pending_changed: Callable[[int], None] = None,
            history_changed: Callable[[], None] = None,
            file_finalized: Callable[[List[Dict[str, Any]], int], None] = None,
            task_update: Callable[..., None] = None,
            task_context: Callable[[], Tuple[str, Any]] = None,
            pansou_client: Any = None,
            pansou_check_enabled: bool = False,
            max_transfer_links: int = 0,
            offline_download_timeout_minutes: int = 120,
    ):
        """
        初始化同步处理器

        :param cloud_drive: 当前网盘提供方；各操作按能力服务分别获取
        :param search_handler: 搜索处理器
        :param subscribe_handler: 订阅处理器
        :param chain: MediaChain 实例
        :param cloud_transfer_path: 当前网盘转存暂存路径
        :param cloud_media_root: 当前网盘媒体库分类根目录
        :param movie_media_root: 电影媒体根目录；未配置时回退媒体库根目录
        :param tv_media_root: 电视剧媒体根目录；未配置时回退媒体库根目录
        :param organize_after_transfer: 转存后是否继续整理进媒体库；关闭时文件停在中转目录即完成
        :param cloud_transfer_paths: 各网盘提供方的转存暂存路径
        :param transfer_task_batch_size: 同一任务内每批处理的最大文件数
        :param batch_size: 批量转存每批文件数
        :param skip_other_season_dirs: 跳过其他季目录
        :param notify: 是否发送通知
        :param notification_type: 消息通知类型
        :param post_message_func: 发送消息的函数
        :param get_data_func: 获取数据的函数
        :param save_data_func: 保存数据的函数
        :param self_heal_interval: 自愈检查间隔（分钟）
        :param enable_cloud_upgrade: 启用网盘洗版
        :param enable_pt_upgrade: 启用PT 整理后上传洗版
        :param upgrade_mode: 洗版文件处理模式
        :param local_resource_path: 容器内可访问的本地或挂载媒体根路径
        :param strm_generate_enabled: 转存成功后是否直接生成 STRM
        :param strm_base_url: STRM 模板中的 base_url
        :param strm_url_template: STRM 内容模板
        :param platform_transfer_history_enabled: 是否写入整理历史
        :param should_stop: 当前同步任务是否已请求停止
        :param offline_pending_changed: 待后处理任务数量变化回调
        :param file_finalized: 文件真正完成后的通知回调
        :param task_update: 订阅任务阶段更新回调
        :param task_context: 当前订阅任务标识与停止事件回调
        :param pansou_client: PanSou 客户端（用于链接有效性检测，渠道无关）
        :param pansou_check_enabled: 是否启用 PanSou 链接有效性检测层
        :param max_transfer_links: 单订阅单轮累计成功转存链接数上限，0 表示不限制
        :param offline_download_timeout_minutes: 离线下载超时分钟数，慢下载不再按 30 分钟误判；
            同时作为文件终审窗口（_FILE_FINALIZE_TIMEOUT）时长
        """
        self._cloud_drive = cloud_drive
        self._cross_transfer_enabled = bool(cross_transfer_enabled)
        self._cross_transfer_media_types = {
            self._normalize_cross_transfer_media_type(value)
            for value in (cross_transfer_media_types or ("movie", "tv"))
        }
        self._cloud_drive_registry = cloud_drive_registry
        self._cross_transfer_manager = cross_transfer_manager
        self._cloud_auth = self._optional_cloud_service(
            CloudDriveCapability.AUTHENTICATION
        )
        self._cloud_account = self._optional_cloud_service(
            CloudDriveCapability.ACCOUNT
        )
        self._share_transfer = self._optional_cloud_service(
            CloudDriveCapability.SHARE_TRANSFER
        )
        self._offline_download = self._optional_cloud_service(
            CloudDriveCapability.OFFLINE_DOWNLOAD
        )
        self._cloud_directories = self._optional_cloud_service(
            CloudDriveCapability.DIRECTORY_READ
        )
        self._cloud_query = self._optional_cloud_service(
            CloudDriveCapability.FILE_QUERY
        )
        self._cloud_mutations = self._optional_cloud_service(
            CloudDriveCapability.FILE_MUTATION
        )
        self._checksum_rename = self._optional_cloud_service(
            CloudDriveCapability.CHECKSUM_RENAME
        )
        self._cloud_batch_mutations = self._optional_cloud_service(
            CloudDriveCapability.BATCH_FILE_MUTATION
        )
        self._playback_reference = self._optional_cloud_service(
            CloudDriveCapability.PLAYBACK_REFERENCE
        )
        self._offline_tasks = self._optional_cloud_service(
            CloudDriveCapability.OFFLINE_TASKS
        )
        self._cloud_upload = self._optional_cloud_service(
            CloudDriveCapability.LOCAL_UPLOAD
        )
        self._search_handler = search_handler
        self._subscribe_handler = subscribe_handler
        self._chain = chain
        self._transfer_task_batch_size = max(
            1, min(int(transfer_task_batch_size or 50), 1000)
        )
        policy = cloud_drive.policy if cloud_drive else None
        configured_batch_size = max(1, int(batch_size or 1))
        self._batch_size = min(
            configured_batch_size,
            policy.max_batch_size if policy and policy.supports_batch else configured_batch_size,
        )
        self._batch_interval = max(0.0, min(float(batch_interval or 0), 60.0))
        self._transfer_risk_cooldown = max(
            60, min(int(transfer_risk_cooldown or 1800), 86400)
        )
        self._share_transfer_risk_lock = threading.Lock()
        self._share_transfer_blocked_until: Dict[str, float] = {}
        self._offline_blacklist = create_platform_ttl_cache(
            "offline_blacklist",
            ttl=86400,
            maxsize=10000,
        )
        self._skip_other_season_dirs = skip_other_season_dirs
        self._notify = notify
        self._notification_type = notification_type
        self._post_message = post_message_func
        self._get_data = get_data_func
        self._save_data = save_data_func
        self._pansou_client = pansou_client
        self._pansou_check_enabled = bool(pansou_check_enabled)
        self._max_transfer_links = max(0, int(max_transfer_links or 0))
        try:
            timeout_minutes = max(
                1, min(int(offline_download_timeout_minutes or 120), 1440)
            )
        except (TypeError, ValueError):
            timeout_minutes = 120
        # 实例配置覆盖类常量；旧调用方/测试未传参时回落 120 分钟。
        self._OFFLINE_TIMEOUT = timeout_minutes * 60
        # 终审窗口与离线下载超时同源，避免第二处 30 分钟硬编码误判。
        self._FILE_FINALIZE_TIMEOUT = timeout_minutes * 60
        self._self_heal_interval = self_heal_interval
        self._enable_cloud_upgrade = enable_cloud_upgrade
        self._enable_pt_upgrade = bool(enable_pt_upgrade)
        if self._enable_pt_upgrade and not self._cloud_upload:
            logger.warning("PT洗版已启用，但当前网盘不支持本地文件上传")
        self._upgrade_subscribe_ids = list(upgrade_subscribe_ids or [])
        self._upgrade_subscribe_id_set = {
            str(value) for value in self._upgrade_subscribe_ids
        }
        self._upgrade_mode = (
            str(upgrade_mode or "largest").strip().lower()
            if str(upgrade_mode or "largest").strip().lower()
               in {"coexist", "replace", "largest", "smallest"}
            else "largest"
        )
        self._local_resource_path = str(local_resource_path or "").strip()
        self._cloud_transfer_path = (
                str(cloud_transfer_path or "/").strip().rstrip("/") or "/"
        )
        self._CLOUD_MEDIA_ROOT = self._normalize_cloud_path(cloud_media_root)
        self._MOVIE_MEDIA_ROOT = self._normalize_cloud_path(movie_media_root)
        self._TV_MEDIA_ROOT = self._normalize_cloud_path(tv_media_root)
        self._organize_after_transfer = bool(organize_after_transfer)
        self._cloud_transfer_paths = {
            str(key).strip().lower(): self._normalize_cloud_path(value)
            for key, value in dict(cloud_transfer_paths or {}).items()
            if str(key).strip()
        }
        if self._cloud_drive:
            self._cloud_transfer_paths.setdefault(
                self._cloud_drive.key, self._cloud_transfer_path
            )
        self._strm_generate_enabled = bool(strm_generate_enabled)
        self._nfo_scrape_enabled = bool(nfo_scrape_enabled)
        self._image_scrape_enabled = bool(image_scrape_enabled)
        self._platform_transfer_history_enabled = bool(
            platform_transfer_history_enabled
        )
        self._metadata_scraper = (
            MediaScraper(
                nfo_enabled=self._nfo_scrape_enabled,
                image_enabled=self._image_scrape_enabled,
            )
            if self._nfo_scrape_enabled or self._image_scrape_enabled
            else None
        )
        if self._metadata_scraper:
            enabled_types = "、".join(
                name for enabled, name in (
                    (self._nfo_scrape_enabled, "NFO"),
                    (self._image_scrape_enabled, "图片"),
                ) if enabled
            )
            if self._local_resource_path:
                logger.info(
                    f"元数据刮削已启用：{enabled_types}，"
                    f"本地资源目录={self._local_resource_path}"
                )
            else:
                logger.warning(
                    f"元数据刮削已启用：{enabled_types}，但未配置本地资源目录，"
                    "无法生成 NFO 或图片"
                )
        self._path_mapper = StrmGenerator(
            StrmGenerator.DEFAULT_BASE_URL, StrmGenerator.DEFAULT_TEMPLATE
        )
        self._strm_generator = None
        if self._strm_generate_enabled:
            if not self._playback_reference:
                logger.error(
                    "当前网盘不支持播放引用，已停止直接生成 STRM"
                )
                self._strm_generate_enabled = False
            else:
                try:
                    self._strm_generator = StrmGenerator(
                        strm_base_url,
                        strm_url_template,
                        provider_variables=self._playback_reference.template_variables,
                    )
                except StrmTemplateError as error:
                    logger.error(f"STRM 生成配置无效，已停止直接生成：{error}")
        self._media_server_notifier = MediaServerNotifier(
            enabled=media_server_refresh_enabled,
            mediaservers=media_servers,
            path_mappings=media_server_path_mappings,
            delay_seconds=media_server_refresh_delay,
            emby_mediainfo_enabled=emby_mediainfo_enabled,
        )
        self._notification_delay_seconds = max(
            0, int(media_server_refresh_delay or 0)
        )
        self._notification_batch_lock = threading.RLock()
        self._notification_batch: List[Dict[str, Any]] = []
        self._notification_batch_timer: Optional[threading.Timer] = None
        self._emby_media_resolver = EmbyMediaResolver()
        self._should_stop = should_stop
        self._offline_pending_changed = offline_pending_changed
        self._history_changed = history_changed
        self._file_finalized = file_finalized
        self._task_update = task_update
        self._task_context = task_context
        self._offline_pending_lock = threading.RLock()
        self._pt_upgrade_lock = threading.RLock()
        self._pt_upgrade_active = set()
        self._platform_history_lock = threading.RLock()
        self._sync_metrics_lock = threading.RLock()
        self._sync_metrics: Dict[str, Dict[str, int]] = {}
        self._media_recognition_lock = threading.RLock()
        self._platform_media_recognition_lock = threading.Lock()
        self._media_recognition_cache = create_platform_ttl_cache(
            "sync:media_recognition", self,
            maxsize=self._MEDIA_RECOGNITION_CACHE_LIMIT,
            ttl=self._RUNTIME_CACHE_TTL,
        )
        self._media_recognition_inflight: Dict[Tuple[Any, ...], Future] = {}
        self._resource_season_dir_lock = threading.RLock()
        self._resource_season_dir_cache = create_platform_ttl_cache(
            "sync:resource_season_dirs", self,
            maxsize=self._RESOURCE_SEASON_DIR_CACHE_LIMIT,
            ttl=self._RUNTIME_CACHE_TTL,
        )
        self._platform_root_lock = threading.RLock()
        self._platform_root_cache = create_platform_ttl_cache(
            "sync:platform_roots", self,
            maxsize=self._PLATFORM_ROOT_CACHE_LIMIT,
            ttl=self._RUNTIME_CACHE_TTL,
        )
        self._subscribe_defer_lock = threading.RLock()
        self._subscribe_defer_cache = create_platform_ttl_cache(
            "sync:subscribe_defer", self,
            maxsize=self._SUBSCRIBE_DEFER_CACHE_LIMIT,
            ttl=self._SUBSCRIBE_DEFER_CACHE_TTL,
        )
        self._subscribe_calendar_cache = create_platform_ttl_cache(
            "sync:subscribe_calendar", self,
            maxsize=self._SUBSCRIBE_CALENDAR_CACHE_LIMIT,
            ttl=self._SUBSCRIBE_CALENDAR_CACHE_TTL,
        )
        self._baseline_cache_lock = threading.RLock()
        self._baseline_transfer_cache = create_platform_ttl_cache(
            "sync:baseline_transfer", self, maxsize=256,
            ttl=self._RUNTIME_CACHE_TTL,
        )
        self._baseline_plugin_cache = create_platform_ttl_cache(
            "sync:baseline_plugin", self, maxsize=256,
            ttl=self._RUNTIME_CACHE_TTL,
        )
        self._baseline_emby_cache = create_platform_ttl_cache(
            "sync:baseline_emby", self, maxsize=256,
            ttl=self._RUNTIME_CACHE_TTL,
        )

    def append_history_records(
            self,
            records: List[Dict[str, Any]],
            reopen_terminal: bool = False,
    ) -> int:
        """写入历史后通知运行态订阅者，避免前端等待整批任务结束。"""
        count = self._get_component(HistoryService).append_history_records(
            records, reopen_terminal=reopen_terminal
        )
        if count and self._history_changed:
            self._history_changed()
        return count

    def _is_cloud_upgrade_subscribe(self, subscribe: Any) -> bool:
        """判断订阅是否属于插件网盘洗版范围。"""
        if self._enable_cloud_upgrade and bool(
                getattr(subscribe, "_manual_upgrade", False)
        ):
            return True
        if (
                not self._enable_cloud_upgrade
                or not subscribe
                or not bool(getattr(subscribe, "best_version", False))
        ):
            return False
        selected_ids = self._upgrade_subscribe_id_set
        return not selected_ids or str(getattr(subscribe, "id", "")) in selected_ids

    @staticmethod
    def subscription_budget_key(
            subscribe: Any, media_type: Optional[MediaType] = None
    ) -> str:
        """生成普通转存和洗版共用的订阅积分键。"""
        resolved_type = media_type or {
            MediaType.MOVIE.value: MediaType.MOVIE,
            MediaType.TV.value: MediaType.TV,
        }.get(str(getattr(subscribe, "type", "") or ""))
        tmdb_id = str(tmdb_id_of(subscribe) or "")
        identity = (
            f"tmdb_{tmdb_id}"
            if tmdb_id
            else str(getattr(subscribe, "name", "") or "").strip()
        )
        if resolved_type == MediaType.MOVIE:
            return f"{identity}_movie"
        season = max(1, int(getattr(subscribe, "season", 1) or 1))
        return f"{identity}_S{season}"

    def _optional_cloud_service(self, capability: CloudDriveCapability):
        if not self._cloud_drive or not self._cloud_drive.supports(capability):
            return None
        return self._cloud_drive.require(capability)

    def clear_runtime_cache(self) -> Dict[str, int]:
        """清空同步过程中可重建的计算缓存。"""
        with self._media_recognition_lock:
            media_recognition = len(self._media_recognition_cache)
            self._media_recognition_cache.clear()
        with self._resource_season_dir_lock:
            resource_season_dirs = len(self._resource_season_dir_cache)
            self._resource_season_dir_cache.clear()
        with self._platform_root_lock:
            platform_roots = len(self._platform_root_cache)
            self._platform_root_cache.clear()
        with self._subscribe_defer_lock:
            subscribe_defer = len(self._subscribe_defer_cache)
            subscribe_calendar = len(self._subscribe_calendar_cache)
            self._subscribe_defer_cache.clear()
            self._subscribe_calendar_cache.clear()
        with self._baseline_cache_lock:
            baseline_transfer = len(self._baseline_transfer_cache)
            baseline_plugin = len(self._baseline_plugin_cache)
            baseline_emby = len(self._baseline_emby_cache)
            self._baseline_transfer_cache.clear()
            self._baseline_plugin_cache.clear()
            self._baseline_emby_cache.clear()
        return {
            "media_recognition": media_recognition,
            "resource_season_dirs": resource_season_dirs,
            "platform_roots": platform_roots,
            "subscribe_defer": subscribe_defer,
            "subscribe_calendar": subscribe_calendar,
            "baseline_transfer": baseline_transfer,
            "baseline_plugin": baseline_plugin,
            "baseline_emby": baseline_emby,
        }

    def reset_sync_metrics(self) -> None:
        with self._sync_metrics_lock:
            self._sync_metrics = {}
        self.clear_runtime_cache()

    @staticmethod
    def _calendar_date(value: Any) -> Optional[datetime.date]:
        normalized = str(value or "").strip()[:10]
        if not normalized:
            return None
        try:
            return datetime.date.fromisoformat(normalized)
        except ValueError:
            return None

    def _subscribe_defer_key(self, subscribe: Any) -> Tuple[Any, ...]:
        media_type = str(getattr(subscribe, "type", "") or "")
        is_tv = media_type == MediaType.TV.value
        return (
            int(getattr(subscribe, "id", 0) or 0),
            media_type,
            media_identity(subscribe),
            str(getattr(subscribe, "name", "") or ""),
            str(getattr(subscribe, "year", "") or ""),
            int(getattr(subscribe, "season", 1) or 1) if is_tv else 0,
            int(getattr(subscribe, "start_episode", 1) or 1) if is_tv else 0,
            int(getattr(subscribe, "total_episode", 0) or 0) if is_tv else 0,
            self._is_cloud_upgrade_subscribe(subscribe),
        )

    def defer_subscribe_until(
            self,
            subscribe: Any,
            defer_until: datetime.date,
            reason: str,
    ) -> bool:
        """缓存明确的未来上映/播出日期，日期到达后自动失效。"""
        if not defer_until or defer_until <= datetime.date.today():
            return False
        cache_key = normalize_platform_cache_key(
            self._subscribe_defer_key(subscribe)
        )
        with self._subscribe_defer_lock:
            self._subscribe_defer_cache.set(cache_key, {
                "defer_until": defer_until.isoformat(),
                "reason": str(reason or "尚未上映或播出"),
            })
        logger.debug(
            f"订阅已延期至 {defer_until.isoformat()}："
            f"{getattr(subscribe, 'name', '')}，{reason}"
        )
        return True

    def get_subscribe_defer(self, subscribe: Any) -> Optional[Dict[str, str]]:
        """返回仍有效的订阅延期信息；订阅范围变化或日期到达时立即失效。"""
        cache_key = normalize_platform_cache_key(
            self._subscribe_defer_key(subscribe)
        )
        today = datetime.date.today()
        with self._subscribe_defer_lock:
            entry = self._subscribe_defer_cache.get(cache_key)
            if entry:
                defer_until = self._calendar_date(entry.get("defer_until"))
                if defer_until and defer_until > today:
                    return dict(entry)
                self._subscribe_defer_cache.delete(cache_key)
        return None

    def _tmdb_season_web_episodes(self, tmdb_id: int, season: int) -> Dict[int, str]:
        """读取 TMDB 季网页的真实卡片，绕过 API/平台缓存的滞后。"""
        url = f"https://www.themoviedb.org/tv/{int(tmdb_id)}/season/{int(season)}"
        response = RequestUtils(
            timeout=20,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                              "AppleWebKit/537.36 Chrome/136.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            },
        ).get_res(url=url)
        status = int(getattr(response, "status_code", 0) or 0)
        if not response or status != 200:
            logger.debug(
                f"TMDB 季网页请求失败：S{season:02d}，HTTP {status or '-'}"
            )
            return {}
        parser = _TmdbSeasonPageParser(season)
        parser.feed(str(getattr(response, "text", "") or ""))
        parser.close()
        logger.debug(
            f"TMDB 季网页解析完成：TV {tmdb_id} S{season:02d}，"
            f"获取 {len(parser.episodes)} 集，最大集数 E{max(parser.episodes, default=0):02d}"
        )
        return parser.episodes

    def get_tv_subscribe_calendar(
            self,
            subscribe: Any,
            tmdb_id: Optional[int] = None,
    ) -> Optional[Dict[str, Any]]:
        """读取 TMDB 季网页并缓存当前订阅目标集的播出状态。"""
        if str(getattr(subscribe, "type", "") or "") != MediaType.TV.value:
            return None
        tmdb_id = int(tmdb_id or tmdb_id_of(subscribe) or 0)
        season = int(getattr(subscribe, "season", 1) or 1)
        start_episode = int(getattr(subscribe, "start_episode", 1) or 1)
        total_episode = int(getattr(subscribe, "total_episode", 0) or 0)
        if tmdb_id <= 0 or total_episode < start_episode:
            return None

        cache_key = normalize_platform_cache_key(
            (*self._subscribe_defer_key(subscribe), tmdb_id)
        )
        today = datetime.date.today()
        checked_on = today.isoformat()
        with self._subscribe_defer_lock:
            entry = self._subscribe_calendar_cache.get(cache_key)
            if (
                    entry
                    and entry.get("checked_on") == checked_on
                    and entry.get("source") == "tmdb_web"
            ):
                return dict(entry)
            if entry:
                self._subscribe_calendar_cache.delete(cache_key)

        try:
            web_air_dates = self._timed_sync_call(
                "tmdb_season_web",
                self._tmdb_season_web_episodes,
                tmdb_id,
                season,
            )
        except Exception as error:
            logger.warning(
                f"{getattr(subscribe, 'name', '')} S{season:02d} "
                f"读取 TMDB 季网页失败：{error}"
            )
            return None
        if not web_air_dates:
            logger.warning(
                f"{getattr(subscribe, 'name', '')} S{season:02d} "
                "TMDB 季网页未解析到剧集播出日期，跳过播出过滤"
            )
            return None

        expected_episodes = set(range(start_episode, total_episode + 1))
        season_known_air_dates: Dict[int, str] = {}
        season_aired_episodes: Set[int] = set()
        known_air_dates: Dict[int, str] = {}
        aired_episodes: Set[int] = set()
        for episode_number, raw_air_date in web_air_dates.items():
            try:
                episode_number = int(episode_number)
            except (TypeError, ValueError):
                continue
            air_date = self._calendar_date(raw_air_date)
            if episode_number <= 0 or not air_date:
                continue
            season_known_air_dates[episode_number] = air_date.isoformat()
            if air_date <= today:
                season_aired_episodes.add(episode_number)
            if episode_number not in expected_episodes:
                continue
            known_air_dates[episode_number] = air_date.isoformat()
            if air_date <= today:
                aired_episodes.add(episode_number)

        future_air_dates = {
            episode: air_date
            for episode, value in known_air_dates.items()
            if (air_date := self._calendar_date(value)) and air_date > today
        }
        last_aired_episode = max(season_aired_episodes, default=0)
        future_boundary_episode = min(
            (
                episode
                for episode in future_air_dates
                if episode > last_aired_episode
            ),
            default=0,
        )
        # TMDB 只返回到当前已公布集数时，订阅总集数后面的未知尾部同样不能搜索。
        # 只在至少存在一条可靠播出日期时建立边界，避免 TMDB 整季无数据时误跳过。
        unreleased_boundary_episode = min(
            (
                episode
                for episode in expected_episodes
                if season_known_air_dates and episode > last_aired_episode
            ),
            default=0,
        )
        boundary_reason = ""
        if unreleased_boundary_episode:
            boundary_reason = (
                "future"
                if unreleased_boundary_episode in future_air_dates
                else "unknown_tail"
            )
        unreleased_episodes = {
            episode
            for episode in expected_episodes
            if episode in future_air_dates
               or (
                       unreleased_boundary_episode > 0
                       and episode >= unreleased_boundary_episode
               )
        }
        all_targets_future = bool(
            expected_episodes and unreleased_episodes == expected_episodes
        )
        next_air_date = min(future_air_dates.values(), default=None)
        defer_until = next_air_date if all_targets_future else None
        entry = {
            "source": "tmdb_web",
            "checked_on": checked_on,
            "known_air_dates": known_air_dates,
            "aired_episodes": sorted(aired_episodes),
            "aired_episode_air_dates": {
                episode: known_air_dates[episode]
                for episode in sorted(aired_episodes)
            },
            "unknown_episodes": sorted(expected_episodes - set(known_air_dates)),
            "unreleased_episodes": sorted(unreleased_episodes),
            "future_boundary_episode": future_boundary_episode,
            "unreleased_boundary_episode": unreleased_boundary_episode,
            "unreleased_boundary_reason": boundary_reason,
            "next_air_date": next_air_date.isoformat() if next_air_date else "",
            "all_targets_future": all_targets_future,
            "defer_until": defer_until.isoformat() if defer_until else "",
        }
        with self._subscribe_defer_lock:
            self._subscribe_calendar_cache.set(cache_key, entry)

        if defer_until:
            self.defer_subscribe_until(
                subscribe,
                defer_until,
                f"目标剧集最早于 {defer_until.isoformat()} 播出",
            )
        return dict(entry)

    def _record_sync_metric(self, name: str, elapsed_ms: int) -> None:
        with self._sync_metrics_lock:
            metric = self._sync_metrics.setdefault(
                name, {"calls": 0, "elapsed_ms": 0}
            )
            metric["calls"] += 1
            metric["elapsed_ms"] += max(0, int(elapsed_ms or 0))

    def _timed_sync_call(self, name: str, func: Callable, *args, **kwargs):
        started = time.monotonic()
        try:
            return func(*args, **kwargs)
        finally:
            self._record_sync_metric(
                name, int((time.monotonic() - started) * 1000)
            )

    @staticmethod
    def _tmdb_id_from_media(value: Any) -> int:
        raw_id = (
            value.get("id") or value.get("tmdb_id")
            if isinstance(value, dict)
            else getattr(value, "tmdb_id", None)
        )
        try:
            return max(0, int(raw_id or 0))
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _normalized_media_title(value: Any) -> str:
        return re.sub(r"[\W_]+", "", str(value or "").casefold())

    @classmethod
    def _tmdb_title_variants(cls, value: Any, media_type: MediaType) -> Set[str]:
        """生成可用于订阅回填的标题变体，去掉剧集季标记和常见宣传后缀。"""
        raw = str(value or "").strip()
        if not raw:
            return set()
        values = {raw}
        if media_type == MediaType.TV:
            values.add(re.sub(
                r"(?:\s*第\s*\d+\s*季|\s*第[一二三四五六七八九十百]+季|\s*season\s*\d+|\s*s\d{1,2})$",
                "",
                raw,
                flags=re.IGNORECASE,
            ).strip())
        return {
            cls._normalized_media_title(item)
            for item in values
            if cls._normalized_media_title(item)
        }

    def _match_tmdb_search_candidate(
            self,
            subscribe: Any,
            media_type: MediaType,
            candidates: List[Any],
    ) -> int:
        """按类型、年份和标题别名评分，只有最高分唯一时才回填。"""
        expected_titles = self._tmdb_title_variants(
            getattr(subscribe, "name", ""), media_type
        )
        expected_year = str(getattr(subscribe, "year", "") or "").strip()
        subscribe_meta = MetaInfo(str(getattr(subscribe, "name", "") or ""))
        season_specific_tv = (
                media_type == MediaType.TV
                and subscribe_meta.begin_season is not None
        )
        scores: Dict[int, int] = {}
        for candidate in candidates or []:
            candidate_type = getattr(candidate, "type", None)
            if candidate_type != media_type:
                continue
            candidate_year = str(getattr(candidate, "year", "") or "").strip()
            if (
                    not season_specific_tv
                    and expected_year
                    and candidate_year
                    and candidate_year != expected_year
            ):
                continue
            candidate_titles: Set[str] = set()
            for field in (
                    "title", "original_title", "en_title", "hk_title",
                    "tw_title", "sg_title", "original_name", "name",
            ):
                candidate_titles.update(
                    self._tmdb_title_variants(getattr(candidate, field, ""), media_type)
                )
            names = getattr(candidate, "names", None) or []
            for name in names:
                candidate_titles.update(self._tmdb_title_variants(name, media_type))
            if not expected_titles or not expected_titles.intersection(candidate_titles):
                continue
            if tmdb_id := self._tmdb_id_from_media(candidate):
                score = 3 if self._tmdb_title_variants(
                    getattr(candidate, "title", ""), media_type
                ).intersection(expected_titles) else 2
                scores[tmdb_id] = max(scores.get(tmdb_id, 0), score)
        if not scores:
            return 0
        best_score = max(scores.values())
        best_ids = [tmdb_id for tmdb_id, score in scores.items() if score == best_score]
        return best_ids[0] if len(best_ids) == 1 else 0

    def repair_subscribe_tmdb_id(self, subscribe: Any) -> bool:
        """在订阅收集阶段使用平台媒体链修复缺失的 TMDB ID。"""
        if tmdb_id_of(subscribe):
            return True

        media_type = {
            MediaType.MOVIE.value: MediaType.MOVIE,
            MediaType.TV.value: MediaType.TV,
        }.get(str(getattr(subscribe, "type", "") or ""))
        subscribe_id = int(getattr(subscribe, "id", 0) or 0)
        if not media_type or subscribe_id <= 0:
            return False

        tmdb_id = 0
        candidates: List[Any] = []
        # 同一豆瓣身份可能已有其他订阅完成 TMDB 回填，优先复用该稳定映射，
        # 避免被不同语言标题、季标题或年份差异误判为无匹配。
        source_douban_id = str(
            legacy_media_ids(subscribe).get("doubanid") or ""
        ).strip()
        if source_douban_id:
            for candidate in SubscribeOper().list() or []:
                if int(getattr(candidate, "id", 0) or 0) == subscribe_id:
                    continue
                candidate_douban_id = str(
                    legacy_media_ids(candidate).get("doubanid") or ""
                ).strip()
                if candidate_douban_id != source_douban_id:
                    continue
                candidate_type = str(getattr(candidate, "type", "") or "")
                if candidate_type != getattr(subscribe, "type", ""):
                    continue
                tmdb_id = self._tmdb_id_from_media({
                    "id": tmdb_id_of(candidate)
                })
                if tmdb_id:
                    logger.debug(
                        f"订阅复用同豆瓣身份的 TMDB 映射："
                        f"{getattr(subscribe, 'name', '')} -> {tmdb_id}"
                    )
                    break

        source_lookups = (
            (
                "doubanid",
                "get_tmdbinfo_by_doubanid",
                legacy_media_ids(subscribe).get("doubanid"),
            ),
            (
                "bangumiid",
                "get_tmdbinfo_by_bangumiid",
                legacy_media_ids(subscribe).get("bangumiid"),
            ),
        )
        for source_name, method_name, source_id in source_lookups:
            if tmdb_id:
                break
            lookup = getattr(self._chain, method_name, None)
            if not source_id or not callable(lookup):
                continue
            try:
                kwargs = (
                    {"doubanid": str(source_id), "mtype": media_type}
                    if source_name == "doubanid"
                    else {"bangumiid": int(source_id)}
                )
                result = self._timed_sync_call(
                    "subscribe_tmdb_repair", lookup, **kwargs
                )
                tmdb_id = self._tmdb_id_from_media(result)
            except Exception as error:
                logger.warning(
                    f"订阅 TMDB ID 自动修复的 {source_name} 映射失败："
                    f"{getattr(subscribe, 'name', '')} - {error}"
                )
            if tmdb_id:
                break

        if not tmdb_id:
            meta = MetaInfo(str(getattr(subscribe, "name", "") or ""))
            meta.year = getattr(subscribe, "year", None)
            meta.type = media_type
            try:
                search_metas = [meta]
                if meta.year:
                    relaxed_meta = MetaInfo(
                        str(getattr(subscribe, "name", "") or "")
                    )
                    relaxed_meta.type = media_type
                    search_metas.append(relaxed_meta)
                seen_ids = set()
                for search_meta in search_metas:
                    rows = self._timed_sync_call(
                        "subscribe_tmdb_repair",
                        self._chain.search_medias,
                        meta=search_meta,
                        source="themoviedb",
                    ) or []
                    for row in rows:
                        row_id = self._tmdb_id_from_media(row)
                        if row_id and row_id not in seen_ids:
                            seen_ids.add(row_id)
                            candidates.append(row)
                tmdb_id = self._match_tmdb_search_candidate(
                    subscribe, media_type, candidates
                )
            except Exception as error:
                logger.warning(
                    f"订阅 TMDB ID 自动修复的标题查询失败："
                    f"{getattr(subscribe, 'name', '')} - {error}"
                )

        # 同步准备阶段可能早于平台搜索缓存建立；识别链是同一套平台
        # 能力，但会按标题/年份直接返回唯一 MediaInfo，作为最后兜底。
        if not tmdb_id:
            try:
                recognized = self._recognize_media_once(
                    (
                        "subscribe_tmdb_repair",
                        media_type.value,
                        getattr(subscribe, "name", ""),
                        getattr(subscribe, "year", None),
                    ),
                    meta=meta,
                    mtype=media_type,
                    tmdbid=None,
                    doubanid=legacy_media_ids(subscribe).get("doubanid"),
                    cache=True,
                )
                tmdb_id = self._tmdb_id_from_media(recognized)
            except Exception as error:
                logger.warning(
                    f"订阅 TMDB ID 自动修复的媒体识别失败："
                    f"{getattr(subscribe, 'name', '')} - {error}"
                )

        if not tmdb_id:
            logger.debug(
                f"订阅 TMDB ID 自动修复未找到安全匹配："
                f"{getattr(subscribe, 'name', '')} "
                f"({getattr(subscribe, 'year', '')})，"
                f"标题候选={len(candidates)}"
            )
            return False

        identity_update = tmdb_identity_update(subscribe, tmdb_id)
        try:
            updated = SubscribeOper().update(subscribe_id, identity_update)
        except Exception as error:
            logger.warning(
                f"订阅 TMDB ID 自动回填失败："
                f"{getattr(subscribe, 'name', '')} -> {tmdb_id} - {error}"
            )
            return False
        if not updated:
            logger.warning(f"订阅 TMDB ID 自动回填失败：订阅 {subscribe_id} 不存在")
            return False

        for field, value in identity_update.items():
            setattr(subscribe, field, value)
        logger.info(
            f"订阅 TMDB 身份已自动回填："
            f"{getattr(subscribe, 'name', '')} -> {tmdb_id}"
        )
        return True

    def _set_task_phase(self, subscribe: Any, phase: str, progress: int) -> None:
        """回写订阅任务的真实处理阶段。"""
        if self._task_update:
            self._task_update(
                f"subscribe:{getattr(subscribe, 'id', '')}",
                phase=phase,
                progress=max(0, min(100, int(progress))),
            )

    def _subscribe_mediainfo(
            self,
            subscribe: Any,
            media_type: MediaType,
            *,
            cache: bool = True,
    ) -> Optional[MediaInfo]:
        """优先复用订阅卡片信息，仅在关键字段缺失时回退媒体识别。"""
        title = str(getattr(subscribe, "name", "") or "").strip()
        try:
            tmdb_id = int(tmdb_id_of(subscribe) or 0)
        except (TypeError, ValueError):
            tmdb_id = 0
        media_category = str(
            getattr(subscribe, "media_category", "") or ""
        ).strip()
        if title and tmdb_id > 0 and media_category:
            try:
                mediainfo = MediaInfo(
                    type=media_type,
                    title=title,
                    year=getattr(subscribe, "year", None),
                    tmdb_id=tmdb_id,
                )
                apply_media_identity(mediainfo, "themoviedb", tmdb_id)
                for source_field, media_field in (
                        ("doubanid", "douban_id"),
                        ("bangumiid", "bangumi_id"),
                        ("anilistid", "anilist_id"),
                        ("original_title", "original_title"),
                        ("poster", "poster_path"),
                        ("backdrop", "backdrop_path"),
                        ("description", "overview"),
                        ("vote", "vote_average"),
                        ("release_date", "release_date"),
                        ("media_category", "category"),
                ):
                    value = (
                        legacy_media_ids(subscribe).get(source_field)
                        if source_field in {
                            "doubanid", "bangumiid", "anilistid"
                        }
                        else getattr(subscribe, source_field, None)
                    )
                    if value in (None, "") or not hasattr(
                            mediainfo, media_field
                    ):
                        continue
                    try:
                        setattr(mediainfo, media_field, value)
                    except (AttributeError, TypeError, ValueError):
                        pass
                logger.debug(
                    f"复用订阅卡片媒体信息：{title}（TMDB={tmdb_id}），"
                    "跳过 TMDB 详情查询"
                )
                return mediainfo
            except (TypeError, ValueError) as error:
                logger.debug(
                    f"订阅卡片媒体信息无效，回退平台识别：{title} - {error}"
                )

        meta = MetaInfo(title)
        meta.year = getattr(subscribe, "year", None)
        meta.type = media_type
        season = (
            int(getattr(subscribe, "season", 0) or 1)
            if media_type == MediaType.TV else 0
        )
        if season:
            meta.begin_season = season
        source, media_id = media_identity(subscribe)
        legacy_ids = legacy_media_ids(subscribe)
        return self._recognize_media_once(
            (
                "subscribe_fallback", media_type.value,
                source, media_id, title,
                getattr(subscribe, "year", None), season, bool(cache),
            ),
            meta=meta,
            mtype=media_type,
            media_source=source,
            media_id=media_id,
            **legacy_ids,
            cache=cache,
        )

    def _recognize_media_once(self, key: Tuple[Any, ...], **kwargs: Any):
        cache_key = normalize_platform_cache_key(key)
        with self._media_recognition_lock:
            cached = self._media_recognition_cache.get(cache_key)
            if cached is not None:
                return cached
            future = self._media_recognition_inflight.get(key)
            owner = future is None
            if owner:
                future = Future()
                self._media_recognition_inflight[key] = future

        if not owner:
            return future.result()

        try:
            # 的媒体识别链包含非线程安全的远端客户端游标，不并发调用。
            with self._platform_media_recognition_lock:
                mediainfo = self._timed_sync_call(
                    "media_recognition", recognize_media, self._chain, **kwargs
                )
        except BaseException as error:
            future.set_exception(error)
            with self._media_recognition_lock:
                if self._media_recognition_inflight.get(key) is future:
                    self._media_recognition_inflight.pop(key, None)
            raise

        if mediainfo:
            with self._media_recognition_lock:
                self._media_recognition_cache.set(cache_key, mediainfo)
        future.set_result(mediainfo)
        with self._media_recognition_lock:
            if self._media_recognition_inflight.get(key) is future:
                self._media_recognition_inflight.pop(key, None)
        return mediainfo

    def get_sync_metrics(self) -> Dict[str, Dict[str, int]]:
        with self._sync_metrics_lock:
            return copy.deepcopy(self._sync_metrics)

    def _is_offline_url(self, url: str) -> bool:
        return bool(
            self._offline_download
            and self._offline_download.is_offline_url(url)
        )

    def _is_ed2k_url(self, url: str) -> bool:
        return bool(
            self._offline_download and self._offline_download.is_ed2k_url(url)
        )

    def _is_magnet_url(self, url: str) -> bool:
        return bool(
            self._offline_download and self._offline_download.is_magnet_url(url)
        )

    # ------------------ PanSou 链接有效性检测层（全渠道前置过滤） ------------------

    def _pansou_check_disk_type(
            self, resource: Dict[str, Any], share_url: str
    ) -> str:
        """从资源与分享链接推导 PanSou 检测所需的 disk_type（渠道无关）。"""
        try:
            disk_type = str(
                self._supported_resource_type(resource, share_url) or ""
            ).strip().lower()
        except Exception:
            disk_type = ""
        if not disk_type or disk_type in {"cloud", "ed2k", "magnet"}:
            return "115"
        return {"189": "tianyi", "aliyun": "alipan"}.get(disk_type, disk_type)

    def _build_pansou_check_map(
            self, resources: List[Dict[str, Any]]
    ) -> Optional[Dict[str, str]]:
        """
        对一批搜索结果批量调用 PanSou 链接有效性检测（渠道无关，覆盖全部搜索源）

        :param resources: 搜索结果列表（含 url/password 字段）
        :return: url -> state 映射；检测未启用或降级失败时返回 None（调用方回退原有校验流程）
        """
        if not self._pansou_check_enabled or not self._pansou_client:
            return None

        items = []
        for resource in resources:
            url = str(resource.get("url") or "")
            # 访问码拼接逻辑与转存循环保持一致（password 必达原则）
            password = str(resource.get("password") or "")
            if password and url and "password=" not in url:
                url = f"{url}?password={password}"
            if not url:
                continue
            items.append({
                "disk_type": self._pansou_check_disk_type(resource, url),
                "url": url,
                "password": password,
            })

        if not items:
            return None

        try:
            results = self._pansou_client.check_links(items)
        except Exception as e:
            logger.warning(f"PanSou 链接有效性检测异常，降级为原有校验流程: {e}")
            return None

        if not results or len(results) != len(items):
            logger.warning("PanSou 链接有效性检测无结果或数量不匹配，降级为原有校验流程")
            return None

        state_map: Dict[str, str] = {}
        for item, result in zip(items, results):
            if isinstance(result, dict):
                state_map[item["url"]] = str(
                    result.get("state") or ""
                ).strip().lower()

        ok_count = sum(1 for s in state_map.values() if s == "ok")
        bad_count = sum(1 for s in state_map.values() if s == "bad")
        logger.info(
            f"PanSou 链接有效性检测完成: 共 {len(state_map)} 个链接, "
            f"有效 {ok_count} 个, 失效 {bad_count} 个"
        )
        return state_map

    def _pansou_check_single(self, share_url: str, password: str = "") -> str:
        """
        对单个链接补做 PanSou 有效性检测（用于批量检测未覆盖的链接）

        :param share_url: 分享链接
        :param password: 提取码（可为空）
        :return: state 字符串；检测未启用或降级失败时返回空字符串（调用方回退原有校验流程）
        """
        if not self._pansou_check_enabled or not self._pansou_client or not share_url:
            return ""
        try:
            results = self._pansou_client.check_links([{
                "disk_type": self._pansou_check_disk_type({}, share_url),
                "url": share_url,
                "password": password,
            }])
            if results and isinstance(results[0], dict):
                return str(results[0].get("state") or "").strip().lower()
        except Exception as e:
            logger.warning(f"PanSou 链接有效性检测异常，降级为原有校验流程: {e}")
        return ""

    def close(self) -> None:
        """提交尚未发送的完成通知并释放通知定时器。"""
        self._flush_transfer_notifications()
        self._media_server_notifier.close(flush=True)

    def update_notification_config(
            self,
            notify: bool,
            notification_type: NotificationType,
            media_server_refresh_enabled: bool,
            media_servers: List[str],
            media_server_path_mappings: str,
            media_server_refresh_delay: int,
            emby_mediainfo_enabled: bool,
    ) -> None:
        self._notify = bool(notify)
        self._notification_type = notification_type
        self._notification_delay_seconds = max(
            0, int(media_server_refresh_delay or 0)
        )
        old_notifier = self._media_server_notifier
        self._media_server_notifier = MediaServerNotifier(
            enabled=media_server_refresh_enabled,
            mediaservers=media_servers,
            path_mappings=media_server_path_mappings,
            delay_seconds=media_server_refresh_delay,
            emby_mediainfo_enabled=emby_mediainfo_enabled,
        )
        old_notifier.close(flush=True)

    def begin_notification_batch(self) -> bool:
        """开始一次同步任务的媒体目录通知聚合。"""
        return self._media_server_notifier.begin_task_batch()

    def finish_notification_batch(self) -> bool:
        """同步任务收尾后统一提交媒体目录通知。"""
        return self._media_server_notifier.finish_task_batch()

    def _stop_requested(self) -> bool:
        try:
            return bool(self._should_stop and self._should_stop())
        except Exception as err:
            logger.warning(f"读取停止状态失败：{err}")
            return False

    def _current_task_context(self) -> Tuple[str, Any]:
        if not self._task_context:
            return "", None
        try:
            task_id, stop_event = self._task_context()
            return str(task_id or ""), stop_event
        except Exception as error:
            logger.debug(f"读取当前订阅任务上下文失败：{error}")
            return "", None

    def _ensure_share_transfer_available(self, provider_key: str) -> None:
        key = str(provider_key or "default").lower()
        with self._share_transfer_risk_lock:
            remaining = self._share_transfer_blocked_until.get(key, 0.0) - time.monotonic()
        if remaining > 0:
            raise RuntimeError(f"{key} 分享转存处于风控冷却期，剩余 {int(remaining)} 秒")

    def _activate_share_transfer_cooldown(self, provider_key: str) -> None:
        key = str(provider_key or "default").lower()
        with self._share_transfer_risk_lock:
            self._share_transfer_blocked_until[key] = max(
                self._share_transfer_blocked_until.get(key, 0.0),
                time.monotonic() + self._transfer_risk_cooldown,
            )
        logger.warning(
            f"{key} 分享转存检测到风控，冷却 {self._transfer_risk_cooldown} 秒"
        )

    def _transfer_episode_items(
            self,
            matched_items: List[Dict[str, Any]],
            share_url: str,
            mediainfo: MediaInfo,
            subscribe,
            season: int,
            sub_key: str,
            track_subscription: bool = True,
            transient_target: bool = False,
    ) -> List[Dict[str, Any]]:
        """在同一任务内拆批完成全部剧集转存。"""
        items = list(matched_items or [])
        if not items:
            return []
        batch_size = self._transfer_task_batch_size
        batch_count = (len(items) + batch_size - 1) // batch_size
        if batch_count > 1:
            logger.info(
                f"匹配 {len(items)} 个文件，将按每批最多 {batch_size} 个"
                f"分 {batch_count} 批在当前任务内处理"
            )
        results = []
        for batch_index, offset in enumerate(range(0, len(items), batch_size), 1):
            if self._stop_requested():
                break
            batch_items = items[offset:offset + batch_size]
            if batch_count > 1:
                logger.debug(
                    f"开始处理转存批次 {batch_index}/{batch_count}："
                    f"文件={len(batch_items)}"
                )
            batch_results = self._transfer_episode_batch(
                batch_items,
                share_url,
                mediainfo,
                subscribe,
                season,
                sub_key,
                track_subscription=track_subscription,
                transient_target=transient_target,
            )
            results.extend(batch_results)
            if self._stop_requested():
                break
            if batch_index < batch_count and not self._wait_transfer_batch_interval():
                break
        return results

    def _wait_transfer_batch_interval(self) -> bool:
        """在任务内批次之间等待，并允许停止请求及时中断。"""
        deadline = time.monotonic() + self._batch_interval
        while time.monotonic() < deadline:
            if self._stop_requested():
                return False
            time.sleep(min(0.25, max(0.0, deadline - time.monotonic())))
        return True

    def _transfer_episode_batch(
            self,
            matched_items: List[Dict[str, Any]],
            share_url: str,
            mediainfo: MediaInfo,
            subscribe,
            season: int,
            sub_key: str,
            track_subscription: bool = True,
            transient_target: bool = False,
    ) -> List[Dict[str, Any]]:
        """执行一个剧集转存批次及对应后处理。"""
        selected_items = list(matched_items or [])
        if not selected_items:
            return []
        if self._stop_requested():
            return []
        # file_id -> 失败原因，随 results 透出供历史落库 failure_reason（v1.5.3 T3）。
        failure_reasons: Dict[str, str] = {}

        file_ids = [str(item["file"]["id"]) for item in selected_items]
        cloud_resource = self._is_cloud_resource_url(share_url)
        direct_cloud_resource = (
                cloud_resource and self._is_direct_cloud_resource_url(share_url)
        )
        rename_items = {}
        for item in selected_items:
            file_item = item["file"]
            item_url = str(file_item.get("url") or share_url).strip()
            rename_items[str(file_item["id"])] = {
                "sha1": file_item.get("sha1"),
                "target_name": (
                    None if self._is_offline_url(item_url) else item["target_name"]
                ),
                "url": item_url,
            }
        try:
            source_provider = self._resource_provider_for_url(share_url)
            provider_key = getattr(source_provider, "key", "") or getattr(
                self._cloud_drive, "key", "default"
            )
            if not cloud_resource:
                self._ensure_share_transfer_available(provider_key)
            # 手动资源标记保存在匹配项的 resource 元数据中；这里不能引用不存在的 resources。
            is_manual_cross = any(
                bool(
                    (item.get("resource") or {}).get("source") == "manual"
                    or (item.get("resource") or {}).get("is_cross")
                    or (item.get("resource") or {}).get("_manual")
                    or item.get("source") == "manual"
                    or item.get("is_cross")
                    or item.get("_manual")
                )
                for item in selected_items
            )
            cross_batch = bool(
                (self._cross_transfer_enabled or is_manual_cross) and source_provider
                and self._cloud_drive and source_provider.key != self._cloud_drive.key
            )
            if cross_batch:
                parent_task_id, task_stop_event = self._current_task_context()
                source_abort_event = threading.Event()

                def batch_stop_requested() -> bool:
                    return bool(
                        global_vars.is_system_stopped
                        or self._stop_requested()
                        or source_abort_event.is_set()
                        or (task_stop_event and task_stop_event.is_set())
                    )

                def transfer_one(item: Dict[str, Any]) -> Tuple[str, Optional[bool]]:
                    file_id = str(item["file"]["id"])
                    if batch_stop_requested():
                        return file_id, None
                    try:
                        success = self._transfer_file(
                            str(item["file"].get("url") or share_url),
                            item["file"],
                            self._cloud_transfer_path,
                            item["target_name"],
                            str(item["file"].get("sha1") or ""),
                            parent_task_id=parent_task_id,
                            stop_requested=batch_stop_requested,
                            media_type=getattr(getattr(mediainfo, "type", None), "name", ""),
                        )
                    except Exception as error:
                        if batch_stop_requested():
                            return file_id, None
                        error_text = str(error)
                        if any(marker in error_text for marker in (
                                "封禁转存", "风控", "未返回下载地址",
                                "No space left on device", "磁盘可用空间不足",
                        )):
                            source_abort_event.set()
                            logger.error(
                                f"跨盘转存批次已熔断：{item['target_name']}，{error_text}"
                            )
                            failure_reasons[file_id] = f"跨盘转存批次熔断：{error_text}"
                            return file_id, False
                        logger.error(
                            f"跨盘转存文件失败：{item['target_name']}，{error}"
                        )
                        failure_reasons[file_id] = f"跨盘转存异常：{error}"
                        return file_id, False
                    if not success:
                        failure_reasons[file_id] = (
                            str(item["file"].get("transfer_failure_reason") or "").strip()
                            or "跨盘转存失败"
                        )
                    if not success and batch_stop_requested():
                        return file_id, None
                    return file_id, success

                provider_limits = [3]
                for provider in (source_provider, self._cloud_drive):
                    limit = int(
                        getattr(getattr(provider, "policy", None), "max_concurrency", 1)
                        or 1
                    )
                    provider_limits.append(limit)
                worker_count = min(len(selected_items), *provider_limits)
                outcomes: Dict[str, bool] = {}
                executor = ThreadPoolExecutor(
                    max_workers=max(1, worker_count),
                    thread_name_prefix="pansearch-file-download",
                )
                futures = {
                    executor.submit(transfer_one, item): str(item["file"]["id"])
                    for item in selected_items
                }
                try:
                    for future in as_completed(futures):
                        try:
                            file_id, success = future.result()
                        except CancelledError:
                            continue
                        if success is not None:
                            outcomes[file_id] = success
                        if batch_stop_requested():
                            for pending in futures:
                                pending.cancel()
                finally:
                    executor.shutdown(wait=True, cancel_futures=True)
                processed_items = [
                    item for item in selected_items
                    if str(item["file"]["id"]) in outcomes
                ]
                success_ids = [
                    file_id for file_id, success in outcomes.items() if success
                ]
            elif direct_cloud_resource:
                processed_items = selected_items
                success_ids = file_ids
            else:
                processed_items = selected_items
                success_ids, failed_ids = self._timed_sync_call(
                    "share_transfer",
                    self._share_transfer.transfer_files_batch,
                    share_url=share_url,
                    file_ids=file_ids,
                    save_path=self._cloud_transfer_path,
                    batch_size=self._batch_size,
                    batch_interval=self._batch_interval,
                    risk_cooldown=self._transfer_risk_cooldown,
                    rename_items=rename_items,
                )
                if failed_ids:
                    self._blacklist_dead_link_share(self._share_transfer, share_url)
                    risk_blocked = bool(getattr(
                        self._share_transfer, "transfer_risk_blocked", False
                    )) and not success_ids
                    batch_fail_reason = (
                        "分享批量转存触发风控，已进入冷却"
                        if risk_blocked else "分享批量转存失败：链接可能已失效"
                    )
                    for failed_id in failed_ids:
                        failure_reasons[str(failed_id)] = batch_fail_reason
                if (
                        failed_ids and not success_ids
                        and bool(getattr(
                    self._share_transfer, "transfer_risk_blocked", False
                ))
                ):
                    self._activate_share_transfer_cooldown(provider_key)
        except Exception as error:
            message = str(error)
            if any(marker in message.lower() for marker in (
                    "风控", "封禁", "受限", "频繁", "rate limit", "too many", "429",
            )):
                self._activate_share_transfer_cooldown(locals().get("provider_key", "default"))
            raise

        success_id_set = {str(file_id) for file_id in (success_ids or [])}
        transferred_subtitle_ids = set()
        for item in processed_items:
            if str(item["file"]["id"]) not in success_id_set:
                continue
            subtitle_files = item.get("subtitle_files") or []
            item["subtitles"] = self._transfer_companion_subtitles(
                share_url=str(item["file"].get("url") or share_url),
                files=[item["file"], *subtitle_files],
                video_file=item["file"],
                target_video_name=item["target_name"],
                media_type="tv",
                season=season,
                episode=item.get("episode"),
                transferred_ids=transferred_subtitle_ids,
            ) if subtitle_files else []
        batch_strm_results = self._generate_or_queue_strm_batch(
            [
                {
                    "result_key": str(item["file"]["id"]),
                    "share_url": str(item["file"].get("url") or share_url),
                    "cloud_dir": item["target_dir"],
                    "file_name": item["target_name"],
                    "staging_dir": self._resource_staging_dir(
                        str(item["file"].get("url") or share_url), item["file"]
                    ),
                    "staging_name": (
                            item["file"].get("staging_name")
                            or item["file"]["name"]
                    ),
                    "source_sha1": item["file"].get("sha1"),
                    "file_size": item["file"].get("size") or 0,
                    "success_episodes": item.get(
                        "success_episodes",
                        [] if item.get("is_upgrade") or not track_subscription
                        else [item["episode"]],
                    ),
                    "notification_episodes": item.get(
                        "notification_episodes", [item["episode"]]
                    ),
                    "upgrade": item.get("is_upgrade"),
                    "upgrade_mode": self._upgrade_mode,
                    "upgrade_old_cloud_dir": item.get("upgrade_old_cloud_dir"),
                    "upgrade_old_file_name": item.get("upgrade_old_file_name"),
                    "upgrade_old_file_id": item.get("upgrade_old_file_id"),
                    "upgrade_old_size": item.get("upgrade_old_size") or 0,
                    "subtitles": item.get("subtitles") or [],
                    "skip_history": bool(
                        (item.get("resource") or {}).get("skip_history")
                    ),
                }
                for item in processed_items
                if str(item["file"]["id"]) in success_id_set
            ],
            mediainfo,
            subscribe_id=(
                getattr(subscribe, "id", None) if track_subscription else None
            ),
            season=season,
            sub_key=sub_key,
            transient_target=transient_target,
            target_subscribe=(
                self._serialize_pending_target_subscribe(subscribe)
                if transient_target else None
            ),
        )
        results = []
        for item in processed_items:
            file_id = str(item["file"]["id"])
            success = file_id in success_id_set
            strm_path, pending_key = batch_strm_results.get(file_id, (None, ""))
            if success and not strm_path and not pending_key:
                logger.error(
                    f"文件已转存但后处理任务登记失败：{item['target_name']}"
                )
                success = False
                failure_reasons[file_id] = "文件已转存但后处理任务登记失败"
            if success and strm_path:
                self._media_server_notifier.notify(
                    path=strm_path,
                    mediainfo=mediainfo,
                    file_name=item["target_name"],
                )
            results.append({
                "item": item,
                "file_id": file_id,
                "success": success,
                "pending_key": pending_key,
                "reason": failure_reasons.get(file_id, ""),
            })
        return results

    @staticmethod
    def _cloud_entry_name(entry: Any) -> str:
        """兼容 CloudFile 与提供方原始 dict 两种条目形态读取文件名。"""
        value = getattr(entry, "name", None)
        if not value and isinstance(entry, Mapping):
            value = entry.get("name") or entry.get("n") or entry.get("file_name")
        return str(value or "").strip()

    @staticmethod
    def _cloud_entry_sha1(entry: Any) -> str:
        value = getattr(entry, "sha1", None)
        if not value and isinstance(entry, Mapping):
            value = entry.get("sha1") or entry.get("sha")
        return str(value or "").strip().upper()

    @staticmethod
    def _cloud_entry_is_directory(entry: Any) -> bool:
        value = getattr(entry, "is_directory", None)
        if value is None and isinstance(entry, Mapping):
            value = entry.get("is_dir")
        return bool(value)

    @staticmethod
    def _cloud_entry_dir(entry: Any, fallback: str) -> str:
        """递归结果携带的父目录（p115 递归条目写入 _cloud_dir），缺失时回落检索根。"""
        native = getattr(entry, "native", None)
        if not isinstance(native, Mapping) and isinstance(entry, Mapping):
            native = entry
        if isinstance(native, Mapping):
            value = str(native.get("_cloud_dir") or "").strip()
            if value:
                return value.rstrip("/") or "/"
        return str(fallback or "/").rstrip("/") or "/"

    def _full_pan_search_roots(self, item: Mapping[str, Any]) -> List[str]:
        """全盘兜底检索根：中转目录 -> 目标目录 -> 转存暂存根 -> 媒体库根 -> 网盘根。

        已被更浅的根递归覆盖的子目录不重复检索；出现网盘根后不再追加。
        旧 pending 载荷缺 staging_dir/cloud_dir 等键时按空值跳过。
        """
        candidates = (
            item.get("staging_dir"),
            item.get("cloud_dir"),
            getattr(self, "_cloud_transfer_path", ""),
            getattr(self, "_CLOUD_MEDIA_ROOT", ""),
            # 电影/电视剧媒体根目录可能配置在媒体库根之外，一并纳入兜底检索候选。
            getattr(self, "_MOVIE_MEDIA_ROOT", ""),
            getattr(self, "_TV_MEDIA_ROOT", ""),
            "/",
        )
        roots: List[str] = []
        for candidate in candidates:
            normalized = str(candidate or "").strip().rstrip("/") or "/"
            if "/" in roots:
                break
            if normalized in roots:
                continue
            if any(
                    normalized == root or normalized.startswith(f"{root}/")
                    for root in roots if root != "/"
            ):
                continue
            roots.append(normalized)
        return roots

    def _full_pan_locate_file(
            self,
            item: Mapping[str, Any],
            file_name: str = "",
            source_sha1: str = "",
    ) -> Optional[Tuple[Any, str]]:
        """网盘全盘兜底检索：优先 source_sha1，其次文件名。

        返回 (文件条目, 所在目录)，未命中返回 None。当前网盘不具备递归
        查询能力（其他提供方无 list_files_recursive）时优雅降级返回
        None，调用方沿用原判定，绝不影响转存主流程。
        sha1 已知时只接受 sha1 相同或 sha1 未知的同名文件，避免把同名
        的其他版本误认成目标文件。
        """
        query = getattr(self, "_cloud_query", None)
        recursive = getattr(query, "list_files_recursive", None) if query else None
        if not callable(recursive):
            logger.debug("当前网盘不支持递归检索，跳过终审全盘兜底")
            return None
        target_sha1 = str(
            source_sha1 or item.get("source_sha1") or ""
        ).strip().upper()
        names = {
            str(value).strip().lower()
            for value in (
                file_name,
                item.get("file_name"),
                item.get("staging_name"),
            )
            if str(value or "").strip()
        }
        if not target_sha1 and not names:
            return None
        name_hit: Optional[Tuple[Any, str]] = None
        for root in self._full_pan_search_roots(item):
            try:
                entries = recursive(root, max_depth=self._FULL_PAN_SEARCH_DEPTH)
            except TypeError:
                # 不接受 max_depth 的提供方按其默认深度检索。
                try:
                    entries = recursive(root)
                except Exception as error:
                    logger.debug(f"全盘检索目录失败：{root}，{error}")
                    continue
            except Exception as error:
                logger.debug(f"全盘检索目录失败：{root}，{error}")
                continue
            for entry in entries or []:
                if self._cloud_entry_is_directory(entry):
                    continue
                entry_name = self._cloud_entry_name(entry)
                if not entry_name:
                    continue
                entry_sha1 = self._cloud_entry_sha1(entry)
                entry_dir = self._cloud_entry_dir(entry, root)
                if target_sha1 and entry_sha1 == target_sha1:
                    logger.info(
                        f"全盘检索按 sha1 命中文件：{entry_dir}/{entry_name}"
                    )
                    return entry, entry_dir
                if (
                        name_hit is None
                        and entry_name.lower() in names
                        and not (target_sha1 and entry_sha1)
                ):
                    name_hit = (entry, entry_dir)
        if name_hit:
            logger.info(
                f"全盘检索按文件名命中文件："
                f"{name_hit[1]}/{self._cloud_entry_name(name_hit[0])}"
            )
        return name_hit

    def _generate_strm(
            self,
            cloud_dir: str,
            file_name: str,
            target_file: Optional[CloudFile] = None,
            lookup_target: bool = True,
            log_success: bool = True,
    ) -> Optional[Path]:
        """使用网盘提供方的播放引用值生成 STRM。"""
        if not self._strm_generate_enabled:
            return None
        if not self._strm_generator:
            return None
        if not self._local_resource_path:
            logger.warning("已启用 STRM 直接生成，但未配置本地/挂载媒体根路径")
            return None

        if not target_file and lookup_target:
            target_file = self._cloud_query.find_file(
                cloud_dir, file_name
            )
        if not target_file:
            if log_success:
                logger.debug(
                    f"网盘目标文件尚未就绪，暂不生成 STRM："
                    f"{cloud_dir.rstrip('/')}/{file_name}"
                )
            return None
        template_values = self._playback_reference.reference_values(target_file)
        try:
            strm_path, content = self._strm_generator.write(
                local_root=self._local_resource_path,
                cloud_root=self._CLOUD_MEDIA_ROOT,
                cloud_dir=cloud_dir,
                file_name=file_name,
                template_values=template_values,
            )
            if log_success:
                logger.debug(f"STRM 已生成：{strm_path} -> {content}")
            return strm_path
        except (OSError, StrmTemplateError) as error:
            logger.error(f"生成 STRM 失败：{file_name}，原因：{error}")
            return None

    def _scrape_metadata(
            self,
            cloud_dir: str,
            file_name: str,
            mediainfo: MediaInfo,
            season: Optional[int] = None,
            episode: Optional[int] = None,
    ) -> Optional[Path]:
        """在最终分类路径补齐元数据；失败不影响网盘文件处理。"""
        if not self._metadata_scraper or not self._local_resource_path:
            return None
        try:
            mapped_path = self._path_mapper.local_path(
                local_root=self._local_resource_path,
                cloud_root=self._CLOUD_MEDIA_ROOT,
                cloud_dir=cloud_dir,
                file_name=file_name,
            )
            media_path = mapped_path.with_suffix(Path(file_name).suffix)
            scrape_items = self._metadata_scraper.filter_missing_items([{
                "media_path": media_path,
                "season": season,
                "episode": episode,
            }], mediainfo)
            if not scrape_items:
                logger.debug(f"跳过元数据刮削：{media_path.parent}，NFO 和图片均已存在")
                return media_path
            logger.info(
                f"开始元数据刮削：{media_path}，"
                f"NFO={'是' if self._nfo_scrape_enabled else '否'}，"
                f"图片={'是' if self._image_scrape_enabled else '否'}"
            )
            created = self._metadata_scraper.scrape_batch(scrape_items, mediainfo)
            if created:
                logger.info(f"元数据刮削完成：{media_path.parent}，新增 {created} 个文件")
            else:
                logger.info(
                    f"元数据刮削完成但无新增文件：{media_path.parent}，"
                    f"目标文件可能已存在或 TMDB 未返回内容"
                )
            return media_path
        except Exception as error:
            logger.warning(f"元数据刮削失败：{file_name}，{error}")
            return None

    def _scrape_metadata_batch(
            self,
            items: List[Dict[str, Any]],
            mediainfo: MediaInfo,
            season: Optional[int] = None,
    ) -> None:
        """按一次转存批次刮削，避免重复请求剧根与季元数据。"""
        if not self._metadata_scraper or not self._local_resource_path or not items:
            return
        scrape_items = []
        try:
            for item in items:
                mapped_path = self._path_mapper.local_path(
                    local_root=self._local_resource_path,
                    cloud_root=self._CLOUD_MEDIA_ROOT,
                    cloud_dir=item["cloud_dir"],
                    file_name=item["file_name"],
                )
                episode = next(iter(
                    item.get("notification_episodes")
                    or item.get("success_episodes")
                    or []
                ), None)
                scrape_items.append({
                    "media_path": mapped_path.with_suffix(Path(item["file_name"]).suffix),
                    "season": season,
                    "episode": episode,
                })
            scrape_items = self._metadata_scraper.filter_missing_items(
                scrape_items, mediainfo
            )
            if not scrape_items:
                logger.debug(
                    f"跳过批量元数据刮削：{mediainfo.title_year}，"
                    "NFO 和图片均已存在"
                )
                return
            logger.debug(
                f"开始批量元数据刮削：{mediainfo.title_year}，"
                f"{len(scrape_items)} 个媒体文件，"
                f"NFO={'是' if self._nfo_scrape_enabled else '否'}，"
                f"图片={'是' if self._image_scrape_enabled else '否'}"
            )
            created = self._metadata_scraper.scrape_batch(scrape_items, mediainfo)
            if created:
                logger.debug(
                    f"批量元数据刮削完成：{mediainfo.title_year}，"
                    f"{len(scrape_items)} 个媒体文件，新增 {created} 个文件"
                )
            else:
                logger.debug(
                    f"批量元数据刮削完成但无新增文件：{mediainfo.title_year}，"
                    f"{len(scrape_items)} 个媒体文件，"
                    "目标文件可能已存在或 TMDB 未返回内容"
                )
        except Exception as error:
            logger.warning(f"批量元数据刮削失败：{error}")

    def _offline_hash(self, share_url: str) -> str:
        match = re.search(r"\|([0-9A-Fa-f]{32})(?:\|[^|]*)*\|/$", str(share_url or ""))
        if match:
            return match.group(1).upper()
        magnet = self._offline_download.parse_magnet_link(share_url)
        return str((magnet or {}).get("hash") or "").upper()

    def _resource_log_reference(self, share_url: str) -> str:
        """Magnet 日志仅展示 infoHash，避免输出完整 Tracker 参数。"""
        if self._is_magnet_url(share_url):
            return f"infoHash={self._offline_hash(share_url) or '未知'}"
        return str(share_url or "")

    def _add_offline_blacklist(self, key_or_url: str, reason: str = "") -> None:
        """将失败或超时的离线任务/资源加入黑名单（1天TTL）。"""
        if not key_or_url or not hasattr(self, "_offline_blacklist"):
            return
        text = str(key_or_url).strip()
        if text.startswith(("subscribe:", "media:")):
            # 兜底任务ID覆盖整个订阅或媒体，粒度不可接受，拒绝入黑名单。
            logger.warning(f"离线黑名单拒绝订阅级兜底键，已跳过：{text}")
            return
        keys = set()
        if text:
            keys.add(text)
        info_hash = self._offline_hash(text) if hasattr(self, "_offline_hash") else ""
        if info_hash:
            keys.add(info_hash.upper())
        now = time.time()
        for k in keys:
            if k:
                self._offline_blacklist[k] = {
                    "reason": reason,
                    "time": now,
                }
        logger.info(f"🚫 离线资源已加入黑名单（1天过期）：{info_hash or text}，原因：{reason}")

    def _remove_offline_blacklist(self, key_or_url: str) -> None:
        """从黑名单中移除指定的离线任务。"""
        if not key_or_url or not hasattr(self, "_offline_blacklist"):
            return
        keys = [str(key_or_url).strip()]
        info_hash = self._offline_hash(key_or_url) if hasattr(self, "_offline_hash") else ""
        if info_hash:
            keys.append(info_hash.upper())
        for k in keys:
            if k and k in self._offline_blacklist:
                try:
                    del self._offline_blacklist[k]
                except Exception:
                    pass

    def _is_offline_blacklisted(self, resource: Optional[Dict[str, Any]] = None, share_url: str = "") -> bool:
        """检查离线资源是否在黑名单中。"""
        if not hasattr(self, "_offline_blacklist"):
            return False
        url = share_url or (str((resource or {}).get("url") or (resource or {}).get("link") or "") if resource else "")
        info_hash = self._offline_hash(url) if (url and hasattr(self, "_offline_hash")) else ""
        if info_hash and info_hash in self._offline_blacklist:
            return True
        if url and url in self._offline_blacklist:
            return True
        if resource:
            res_hash = str(
                resource.get("info_hash") or resource.get("hash") or (resource.get("magnet_metadata") or {}).get(
                    "hash") or "").upper()
            if res_hash and res_hash in self._offline_blacklist:
                return True
        return False

    def _blacklist_dead_link_share(
            self, share_service: Any, share_url: str
    ) -> None:
        """转存失败后核对确定性死链标记，命中则以单资源粒度拉黑一天。

        分享服务在 _do_transfer 中记录 errno 4100018 / 链接过期等确定性
        失败；这里取走标记并复用离线黑名单（拒绝订阅级兜底键），
        避免每轮重新搜到同一死链反复失败。
        """
        consume = getattr(share_service, "consume_dead_link_failure", None)
        if not consume or not share_url:
            return
        try:
            failure = consume(share_url)
        except Exception as error:
            logger.debug(f"读取分享死链标记失败：{error}")
            return
        if not failure:
            return
        error_code = failure.get("error_code")
        reason = (
            f"分享链接已过期（错误码 {error_code}）"
            if error_code not in (None, "")
            else "分享链接已过期"
        )
        logger.info(
            f"分享链接为确定性死链，已加入黑名单避免重复转存："
            f"{self._resource_log_reference(share_url)}，"
            f"{failure.get('error_msg') or reason}"
        )
        self._add_offline_blacklist(str(share_url), reason)

    def _queue_magnet_package(
            self,
            resource: Dict[str, Any],
            share_url: str,
            subscribe: Any,
            mediainfo: MediaInfo,
            season: Optional[int] = None,
            target_episodes: Optional[List[int]] = None,
            sub_key: str = "",
            upgrade: bool = False,
            upgrade_mode: str = "",
            upgrade_baseline: Optional[Dict[str, Any]] = None,
            transient_target: bool = False,
    ) -> str:
        """提交 Magnet 到隔离目录；下载完成后再按真实文件树匹配。"""
        info_hash = self._offline_hash(share_url)
        magnet_title = self._prepare_magnet_resource(resource, share_url)
        metadata = resource.get("magnet_metadata") or {}
        title_seasons = self._magnet_title_seasons(resource)
        if season is not None and title_seasons and int(season) not in title_seasons:
            logger.debug(
                "Magnet 标题预过滤排除，未请求远端内容元数据："
                f"标题季数={','.join(f'S{value:02d}' for value in sorted(title_seasons))}，"
                f"目标季数=S{int(season):02d}，标题={magnet_title or info_hash}"
            )
            return ""
        title_episodes = self._magnet_title_episodes(
            resource, int(season or 1)
        )
        preview_episodes = (
                title_episodes
                or self._resource_preview_episodes(resource, int(season or 1))
        )
        target_episode_set = {
            int(value) for value in (target_episodes or []) if int(value) > 0
        }
        if season is not None and target_episode_set and title_episodes:
            confirmed_targets = target_episode_set & title_episodes
            if not confirmed_targets:
                logger.debug(
                    "Magnet 标题预过滤排除，未请求远端内容元数据："
                    f"标题集数={self._format_episode_ranges(title_episodes)}，"
                    f"目标集数={self._format_episode_ranges(target_episode_set)}，"
                    f"标题={magnet_title or info_hash}"
                )
                return ""
            target_episodes[:] = sorted(confirmed_targets)
            logger.debug(
                "Magnet 标题预过滤命中，跳过远端内容元数据获取："
                f"标题集数={self._format_episode_ranges(title_episodes)}，"
                f"目标集数={self._format_episode_ranges(target_episode_set)}，"
                f"标题={magnet_title or info_hash}"
            )
        if (
                not title_episodes
                and not preview_episodes
                and not metadata.get("torrent_files")
        ):
            logger.debug(
                "Magnet 标题未识别明确集数，开始获取远端内容元数据："
                f"{magnet_title or info_hash}"
            )
            magnet_info = self._offline_download.parse_magnet_link(
                share_url, fetch_metadata=True
            )
            fetched_metadata = (magnet_info or {}).get("metadata") or {}
            if fetched_metadata:
                metadata = {
                    **metadata,
                    **{
                        key: value for key, value in fetched_metadata.items()
                        if value not in (None, "", [], {})
                    },
                }
                resource["magnet_metadata"] = metadata
                magnet_title = self._prepare_magnet_resource(resource, share_url)
                title_seasons = self._magnet_title_seasons(resource)
                if (
                        season is not None
                        and title_seasons
                        and int(season) not in title_seasons
                ):
                    logger.debug(
                        "Magnet 远端内容元数据季数不匹配，已跳过："
                        f"内容季数={','.join(f'S{value:02d}' for value in sorted(title_seasons))}，"
                        f"目标季数=S{int(season):02d}，标题={magnet_title or info_hash}"
                    )
                    return ""
                title_episodes = self._magnet_title_episodes(
                    resource, int(season or 1)
                )
                metadata_preview = metadata.get("preview_episodes") or {}
                if metadata_preview:
                    resource["preview_episodes"] = metadata_preview
                preview_episodes = (
                        title_episodes
                        or self._resource_preview_episodes(
                    resource, int(season or 1)
                )
                )
        if (
                not info_hash
                or not self._get_data
                or (
                not bool(metadata.get("metadata_available"))
                and not bool(title_episodes)
                and not bool(preview_episodes)
        )
        ):
            logger.debug(
                "Magnet 标题和远端内容元数据均未提供可确认内容，已跳过："
                f"{magnet_title or info_hash}"
            )
            return ""
        if season is not None and target_episodes:
            target_episode_set = {
                int(value) for value in target_episodes if int(value) > 0
            }
            confirmed_targets = target_episode_set & preview_episodes
            if not confirmed_targets:
                logger.debug(
                    "Magnet 内容确认未覆盖目标集数，已跳过网盘离线下载候选："
                    f"内容集数={self._format_episode_ranges(preview_episodes)}，"
                    f"目标集数={self._format_episode_ranges(target_episode_set)}，"
                    f"标题={magnet_title or info_hash}"
                )
                return ""
            target_episodes[:] = sorted(confirmed_targets)
        subscribe_id = int(getattr(subscribe, "id", 0) or 0)
        pending_key = f"magnet:{info_hash}:{subscribe_id}"
        staging_dir = f"{self._cloud_transfer_path.rstrip('/')}"
        with self._offline_pending_lock:
            pending = self._get_data(self._OFFLINE_PENDING_KEY) or {}
            if pending_key in pending:
                return pending_key
        submit_handle = self._offline_download.add_offline_download(
            share_url, staging_dir
        )
        if not submit_handle:
            self._add_offline_blacklist(share_url, "提交离线下载失败")
            return ""
        if not info_hash:
            # 提交成功但本地无法解析出 info_hash：使用平台返回句柄兜底并
            # 记录警告，避免后处理回退到 subscribe:* 级别的兜底任务ID。
            logger.warning(
                f"Magnet 本地未能解析 info_hash，已使用平台返回句柄："
                f"{magnet_title or share_url}"
            )
        real_task_id = submit_handle or info_hash
        # 提交后立即强制刷新一次任务列表，让后处理 task_map 能命中新任务。
        try:
            self._offline_download.get_offline_tasks(force=True)
        except Exception as error:
            logger.warning(f"115 离线任务列表刷新失败，将继续使用缓存：{error}")
        no_handle = not bool(real_task_id)
        now = time.time()
        metadata = resource.get("magnet_metadata") or {}
        file_name = str(
            metadata.get("display_name")
            or resource.get("title") or info_hash
        )
        target_episode_list = sorted({
            int(value) for value in (target_episodes or []) if int(value) > 0
        })
        # 手动提交（/cloud_link）与订阅后处理两条通道统一走
        # _build_pending_record，补齐 info_hash/source_sha1/staging_dir/
        # staging_name/file_size/success_episodes 等定位字段，避免因缺字段
        # 导致后续轮次无法拾取或无法定位（v1.5.4 T4）。resource/
        # target_episodes/upgrade_baseline 等磁力专属键经 current 原样保留。
        source_sha1 = str(
            resource.get("source_sha1") or metadata.get("sha1") or ""
        ).upper()
        record = self._build_pending_record(
            current={
                "resource": dict(resource),
                "target_episodes": target_episode_list,
                "upgrade_baseline": dict(upgrade_baseline or {}),
                "history_ready": True,
            },
            pending_key=pending_key,
            task_type="magnet",
            info_hash=info_hash,
            source_hash=source_sha1,
            share_url=share_url,
            cloud_dir=staging_dir,
            file_name=file_name,
            staging_dir=staging_dir,
            staging_name=file_name,
            file_size=self._resource_size_bytes(
                resource.get("size") or metadata.get("size")
            ),
            now=now,
            mediainfo=mediainfo,
            subscribe_id=subscribe_id,
            success_episodes=target_episode_list,
            notification_episodes=target_episode_list,
            season=season,
            sub_key=sub_key,
            upgrade=upgrade,
            upgrade_mode=upgrade_mode,
            transient_target=transient_target,
            target_subscribe=(
                self._serialize_pending_target_subscribe(subscribe)
                if transient_target else None
            ),
        )
        record.update({
            "task_id": real_task_id,
            "no_handle": no_handle,
            # 手动磁力通道的历史记录已在提交后立即写入，登记即可激活，
            # 无需等待 _activate_persisted_pending_tasks 再延迟一轮。
            "history_ready": True,
        })
        with self._offline_pending_lock:
            pending = self._get_data(self._OFFLINE_PENDING_KEY) or {}
            pending[pending_key] = record
            self._save_offline_pending(pending)
            pending_count = len(pending)
        self._notify_offline_pending_changed(pending_count)
        logger.info(
            f"⏳ Magnet 已提交115隔离目录，完成后按真实文件匹配：{pending[pending_key]['file_name']}"
        )
        return pending_key

    @staticmethod
    def _serialize_mediainfo(mediainfo: MediaInfo) -> Dict[str, Any]:
        if not mediainfo:
            return {}
        try:
            if hasattr(mediainfo, "to_dict"):
                return mediainfo.to_dict()
            if hasattr(mediainfo, "model_dump"):
                return mediainfo.model_dump(mode="json")
            if hasattr(mediainfo, "dict"):
                return mediainfo.dict()
        except Exception as error:
            logger.debug(f"序列化媒体信息失败，将仅生成 STRM：{error}")
        return {}

    @staticmethod
    def _deserialize_mediainfo(media_data: Dict[str, Any]) -> Optional[MediaInfo]:
        if not media_data:
            return None
        mediainfo = MediaInfo()
        if hasattr(mediainfo, "from_dict"):
            mediainfo.from_dict(dict(media_data))
            return mediainfo
        return MediaInfo(**media_data)

    def _save_offline_pending(self, pending: Dict[str, Dict[str, Any]]) -> None:
        if self._save_data:
            self._save_data(self._OFFLINE_PENDING_KEY, pending)

    def _notify_offline_pending_changed(self, pending_count: int) -> None:
        try:
            if self._offline_pending_changed:
                self._offline_pending_changed(max(0, int(pending_count or 0)))
        except Exception as error:
            logger.warning(f"更新网盘文件后处理监控状态失败：{error}")

    @staticmethod
    def _finalize_source_identity(
            source_sha1: str, staging_dir: str, staging_name: str,
            file_size: int,
    ) -> Tuple[str, str]:
        source_hash = re.sub(
            r"[^0-9A-Fa-f]", "", str(source_sha1 or "")
        ).upper()
        if len(source_hash) != 40:
            source_hash = ""
        identity = source_hash or hashlib.sha1(
            "\0".join((
                str(staging_dir or "/").rstrip("/") or "/",
                str(staging_name or ""),
                str(max(0, int(file_size or 0))),
            )).encode("utf-8")
        ).hexdigest().upper()
        return source_hash, identity

    def _pending_identity(
            self,
            share_url: str,
            cloud_dir: str,
            file_name: str,
            source_identity: str,
    ) -> Tuple[str, str, str]:
        """统一计算后处理键，避免单项和批量流程产生不同规则。"""
        info_hash = self._offline_hash(share_url)
        if info_hash:
            return (
                info_hash,
                "magnet" if self._is_magnet_url(share_url) else "ed2k",
                info_hash,
            )
        path_digest = hashlib.sha1(
            f"{str(cloud_dir or '').rstrip('/')}/{file_name}".encode("utf-8")
        ).hexdigest()[:12]
        provider_key = str(
            getattr(self._cloud_drive, "key", "cloud") or "cloud"
        )
        cloud_resource = self._is_cloud_resource_url(share_url)
        task_type = (
            "cloud"
            if cloud_resource and self._is_direct_cloud_resource_url(share_url)
            else "cross_cloud"
            if cloud_resource
            else "share"
        )
        return (
            f"{provider_key}:{source_identity}:{path_digest}",
            task_type,
            "",
        )

    @staticmethod
    def _serialize_pending_target_subscribe(subscribe: Any) -> Dict[str, Any]:
        return {
            "name": str(getattr(subscribe, "name", "") or ""),
            "year": getattr(subscribe, "year", None),
            "type": str(getattr(subscribe, "type", "") or ""),
            "tmdbid": tmdb_id_of(subscribe),
            "doubanid": legacy_media_ids(subscribe).get("doubanid"),
            "season": getattr(subscribe, "season", None),
            "start_episode": getattr(subscribe, "start_episode", None),
            "total_episode": getattr(subscribe, "total_episode", None),
            "media_category": getattr(subscribe, "media_category", None),
            "episode_group": getattr(subscribe, "episode_group", None),
            "filter_groups": getattr(subscribe, "filter_groups", None),
            "best_version": bool(getattr(subscribe, "best_version", False)),
            "_manual_upgrade": bool(getattr(subscribe, "_manual_upgrade", False)),
        }

    def _build_pending_record(
            self,
            *,
            current: Dict[str, Any],
            pending_key: str,
            task_type: str,
            info_hash: str,
            source_hash: str,
            share_url: str,
            cloud_dir: str,
            file_name: str,
            staging_dir: str,
            staging_name: str,
            file_size: int,
            now: float,
            mediainfo: MediaInfo,
            media_data: Optional[Dict[str, Any]] = None,
            subscribe_id: Optional[int] = None,
            success_episodes: Optional[List[int]] = None,
            notification_episodes: Optional[List[int]] = None,
            season: Optional[int] = None,
            sub_key: str = "",
            transient_target: bool = False,
            target_subscribe: Optional[Dict[str, Any]] = None,
            upgrade: bool = False,
            upgrade_mode: str = "",
            upgrade_old_cloud_dir: str = "",
            upgrade_old_file_name: str = "",
            upgrade_old_file_id: str = "",
            upgrade_old_size: int = 0,
            subtitles: Optional[List[Dict[str, Any]]] = None,
            skip_history: bool = False,
    ) -> Dict[str, Any]:
        """构造单个后处理记录；单项和批量入口共用同一字段规则。"""
        current = current or {}
        is_transient_target = bool(
            transient_target or current.get("transient_target")
        )
        success_values = success_episodes or current.get("success_episodes") or []
        notification_values = (
                notification_episodes
                or current.get("notification_episodes")
                or success_values
                or current.get("success_episodes")
                or []
        )
        pending_task_id = str(
            info_hash
            or current.get("task_id")
            or (
                f"subscribe:{int(subscribe_id)}"
                if subscribe_id
                else f"media:{sub_key}" if sub_key else ""
            )
        )
        # 终审所需指纹：ED2K 为文件哈希、Magnet 为 info_hash；旧 pending
        # 记录缺该键时按空串兼容，消费端一律用 .get 读取（v1.5.4 T2）。
        pending_info_hash = str(
            info_hash or current.get("info_hash") or ""
        ).upper()
        # 哈希型（磁力/ed2k）记录必须携带真实哈希句柄；解析不到时显式落
        # 无句柄标记并告警，消费端超时终审会按文件反查兜底。
        no_handle = bool(
            task_type in ("magnet", "ed2k")
            and not re.fullmatch(r"[0-9A-Fa-f]{32,40}", pending_task_id)
        )
        if no_handle:
            logger.warning(
                f"哈希型离线任务未能解析真实句柄，超时终审将按文件反查兜底："
                f"{file_name}（task_id={pending_task_id or '空'}）"
            )
        return {
            **current,
            "pending_key": pending_key,
            "task_type": task_type,
            "task_id": pending_task_id,
            "info_hash": pending_info_hash,
            # 统一 pending schema 的状态字段：供 offline_pending_tasks.status
            # 列与前端展示使用；旧记录缺键时按任务类型兜底（v1.5.4 T4）。
            "status": str(
                current.get("status")
                or ("下载中" if task_type in ("magnet", "ed2k") else "处理中")
            ),
            "no_handle": no_handle,
            "source_sha1": source_hash,
            "share_url": share_url,
            "cloud_dir": cloud_dir,
            "file_name": file_name,
            "staging_dir": staging_dir,
            "staging_name": staging_name,
            "file_size": int(file_size or current.get("file_size") or 0),
            "upgrade": bool(upgrade or current.get("upgrade")),
            "upgrade_mode": str(
                upgrade_mode or current.get("upgrade_mode") or self._upgrade_mode
            ),
            "upgrade_old_cloud_dir": str(
                upgrade_old_cloud_dir
                or current.get("upgrade_old_cloud_dir")
                or ""
            ),
            "upgrade_old_file_name": str(
                upgrade_old_file_name
                or current.get("upgrade_old_file_name")
                or ""
            ),
            "upgrade_old_file_id": str(
                upgrade_old_file_id or current.get("upgrade_old_file_id") or ""
            ),
            "upgrade_old_size": int(
                upgrade_old_size or current.get("upgrade_old_size") or 0
            ),
            "created_at": float(current.get("created_at") or now),
            "next_check_at": now + self._OFFLINE_CHECK_DELAYS[0],
            "check_index": int(current.get("check_index") or 0),
            "history_ready": bool(skip_history or current.get("skip_history")),
            "skip_history": bool(skip_history or current.get("skip_history")),
            "mediainfo": (
                media_data
                if media_data is not None
                else self._serialize_mediainfo(mediainfo)
            ),
            "subscribe_id": subscribe_id or current.get("subscribe_id"),
            "success_episodes": sorted(
                {
                    int(episode)
                    for episode in success_values
                    if int(episode) > 0
                }
            ),
            "season": (
                max(1, int(season or current.get("season") or 1))
                if getattr(mediainfo, "type", None) == MediaType.TV
                else None
            ),
            "episode": next(iter(notification_episodes or success_episodes or []), None),
            "notification_episodes": sorted(
                {
                    int(episode)
                    for episode in notification_values
                    if int(episode) > 0
                }
            ),
            "sub_key": str(sub_key or current.get("sub_key") or ""),
            "transient_target": is_transient_target,
            "target_subscribe": (
                copy.deepcopy(target_subscribe or current.get("target_subscribe") or {})
                if is_transient_target else {}
            ),
            "subtitles": copy.deepcopy(
                subtitles if subtitles is not None else current.get("subtitles") or []
            ),
        }

    def _queue_file_finalize(
            self,
            share_url: str,
            cloud_dir: str,
            file_name: str,
            mediainfo: MediaInfo,
            source_sha1: str = "",
            file_size: int = 0,
            subscribe_id: Optional[int] = None,
            success_episodes: Optional[List[int]] = None,
            season: Optional[int] = None,
            notification_episodes: Optional[List[int]] = None,
            sub_key: str = "",
            staging_dir: str = "",
            staging_name: str = "",
            upgrade: bool = False,
            upgrade_mode: str = "",
            upgrade_old_cloud_dir: str = "",
            upgrade_old_file_name: str = "",
            upgrade_old_file_id: str = "",
            upgrade_old_size: int = 0,
            subtitles: Optional[List[Dict[str, Any]]] = None,
            transient_target: bool = False,
            target_subscribe: Optional[Dict[str, Any]] = None,
            skip_history: bool = False,
    ) -> str:
        effective_staging_dir = str(staging_dir or cloud_dir).rstrip("/") or "/"
        effective_staging_name = str(staging_name or file_name)
        source_hash, source_identity = self._finalize_source_identity(
            source_sha1, effective_staging_dir, effective_staging_name, file_size
        )
        if not self._get_data:
            logger.error(f"无法登记文件后处理任务：{file_name}")
            return ""
        now = time.time()
        pending_key, task_type, info_hash = self._pending_identity(
            share_url, cloud_dir, file_name, source_identity
        )
        with self._offline_pending_lock:
            pending = self._get_data(self._OFFLINE_PENDING_KEY) or {}
            current = pending.get(pending_key) or {}
            pending[pending_key] = self._build_pending_record(
                current=current,
                pending_key=pending_key,
                task_type=task_type,
                info_hash=info_hash,
                source_hash=source_hash,
                share_url=share_url,
                cloud_dir=cloud_dir,
                file_name=file_name,
                staging_dir=effective_staging_dir,
                staging_name=effective_staging_name,
                file_size=file_size,
                now=now,
                mediainfo=mediainfo,
                subscribe_id=subscribe_id,
                success_episodes=success_episodes,
                notification_episodes=notification_episodes,
                season=season,
                sub_key=sub_key,
                upgrade=upgrade,
                upgrade_mode=upgrade_mode,
                upgrade_old_cloud_dir=upgrade_old_cloud_dir,
                upgrade_old_file_name=upgrade_old_file_name,
                upgrade_old_file_id=upgrade_old_file_id,
                upgrade_old_size=upgrade_old_size,
                subtitles=subtitles,
                transient_target=transient_target,
                target_subscribe=target_subscribe,
                skip_history=skip_history,
            )
            self._save_offline_pending(pending)
            pending_count = len(pending)
        self._notify_offline_pending_changed(pending_count)
        if info_hash:
            logger.info(f"⏳ 已登记离线完成监控：{file_name}")
        else:
            logger.info(f"⏳ 文件仍在115系统处理中，已登记重命名与STRM后处理：{file_name}")
        return pending_key

    def _generate_or_queue_strm(
            self,
            share_url: str,
            cloud_dir: str,
            file_name: str,
            mediainfo: MediaInfo,
            source_sha1: str = "",
            file_size: int = 0,
            subscribe_id: Optional[int] = None,
            success_episodes: Optional[List[int]] = None,
            season: Optional[int] = None,
            notification_episodes: Optional[List[int]] = None,
            sub_key: str = "",
            target_file: Optional[CloudFile] = None,
            lookup_target: bool = True,
            log_success: bool = True,
            staging_dir: str = "",
            staging_name: str = "",
            upgrade: bool = False,
            upgrade_mode: str = "",
            upgrade_old_cloud_dir: str = "",
            upgrade_old_file_name: str = "",
            upgrade_old_file_id: str = "",
            upgrade_old_size: int = 0,
            subtitles: Optional[List[Dict[str, Any]]] = None,
            transient_target: bool = False,
            target_subscribe: Optional[Dict[str, Any]] = None,
            skip_history: bool = False,
    ) -> Tuple[Optional[Path], str]:
        strm_path = None
        # 云下载类资源（ED2K/Magnet）提交成功只代表任务已进入115离线队列，
        # 绝不能凭终审前的 STRM 命中直接落“成功”，必须统一登记 offline_pending
        # 交由后处理实证文件落盘（v1.5.4 T2）。
        if not staging_dir and not self._is_offline_url(share_url):
            strm_path = self._generate_strm(
                cloud_dir,
                file_name,
                target_file=target_file,
                lookup_target=lookup_target,
                log_success=log_success,
            )
            self._scrape_metadata(
                cloud_dir,
                file_name,
                mediainfo,
                season=season,
                episode=next(iter(notification_episodes or success_episodes or []), None),
            )
        if strm_path:
            return strm_path, ""
        pending_key = self._queue_file_finalize(
            share_url=share_url,
            cloud_dir=cloud_dir,
            file_name=file_name,
            mediainfo=mediainfo,
            source_sha1=source_sha1,
            file_size=file_size,
            subscribe_id=subscribe_id,
            success_episodes=success_episodes,
            season=season,
            notification_episodes=notification_episodes,
            sub_key=sub_key,
            staging_dir=staging_dir,
            staging_name=staging_name,
            upgrade=upgrade,
            upgrade_mode=upgrade_mode,
            upgrade_old_cloud_dir=upgrade_old_cloud_dir,
            upgrade_old_file_name=upgrade_old_file_name,
            upgrade_old_file_id=upgrade_old_file_id,
            upgrade_old_size=upgrade_old_size,
            subtitles=subtitles,
            transient_target=transient_target,
            target_subscribe=target_subscribe,
            skip_history=skip_history,
        )
        return None, pending_key

    def _queue_file_finalize_batch(
            self,
            items: List[Dict[str, Any]],
            mediainfo: MediaInfo,
            subscribe_id: Optional[int] = None,
            season: Optional[int] = None,
            sub_key: str = "",
            transient_target: bool = False,
            target_subscribe: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, str]:
        """一次持久化整批未就绪文件，避免逐项读写插件数据。"""
        if not items or not self._get_data:
            return {}
        now = time.time()
        media_data = self._serialize_mediainfo(mediainfo)
        result: Dict[str, str] = {}
        with self._offline_pending_lock:
            pending = self._get_data(self._OFFLINE_PENDING_KEY) or {}
            for item in items:
                share_url = item["share_url"]
                cloud_dir = item["cloud_dir"]
                file_name = item["file_name"]
                staging_dir = str(
                    item.get("staging_dir") or cloud_dir
                ).rstrip("/") or "/"
                staging_name = str(item.get("staging_name") or file_name)
                source_hash, source_identity = self._finalize_source_identity(
                    item.get("source_sha1") or "",
                    staging_dir,
                    staging_name,
                    item.get("file_size") or 0,
                )
                pending_key, task_type, info_hash = self._pending_identity(
                    share_url, cloud_dir, file_name, source_identity
                )
                current = pending.get(pending_key) or {}
                pending[pending_key] = self._build_pending_record(
                    current=current,
                    pending_key=pending_key,
                    task_type=task_type,
                    info_hash=info_hash,
                    source_hash=source_hash,
                    share_url=share_url,
                    cloud_dir=cloud_dir,
                    file_name=file_name,
                    staging_dir=staging_dir,
                    staging_name=staging_name,
                    file_size=item.get("file_size") or 0,
                    now=now,
                    mediainfo=mediainfo,
                    media_data=media_data,
                    subscribe_id=subscribe_id,
                    success_episodes=item.get("success_episodes"),
                    notification_episodes=item.get("notification_episodes"),
                    season=season,
                    sub_key=sub_key,
                    upgrade=bool(item.get("upgrade")),
                    upgrade_mode=item.get("upgrade_mode") or "",
                    upgrade_old_cloud_dir=item.get("upgrade_old_cloud_dir") or "",
                    upgrade_old_file_name=item.get("upgrade_old_file_name") or "",
                    upgrade_old_file_id=item.get("upgrade_old_file_id") or "",
                    upgrade_old_size=item.get("upgrade_old_size") or 0,
                    subtitles=item.get("subtitles") or [],
                    transient_target=transient_target,
                    target_subscribe=target_subscribe,
                    skip_history=bool(item.get("skip_history")),
                )
                result[str(item["result_key"])] = pending_key
            if result:
                self._save_offline_pending(pending)
            pending_count = len(pending)
        self._notify_offline_pending_changed(pending_count)
        return result

    def _generate_or_queue_strm_batch(
            self,
            items: List[Dict[str, Any]],
            mediainfo: MediaInfo,
            subscribe_id: Optional[int] = None,
            season: Optional[int] = None,
            sub_key: str = "",
            transient_target: bool = False,
            target_subscribe: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Tuple[Optional[Path], str]]:
        """复用批量重命名缓存生成 STRM，避免逐文件查询115。"""
        results: Dict[str, Tuple[Optional[Path], str]] = {}
        generated = 0
        queued_items: List[Dict[str, Any]] = []
        ready_items: List[Dict[str, Any]] = []
        for item in items:
            result_key = str(item["result_key"])
            # 离线下载资源必须先登记 pending（同 Magnet 路径），不得走
            # STRM 快速通道提前落终态成功（v1.5.4 T2）。
            if item.get("staging_dir") or self._is_offline_url(
                    str(item.get("share_url") or "")
            ):
                queued_items.append(item)
                continue
            cloud_dir = item["cloud_dir"]
            file_name = item["file_name"]
            target_file = self._cloud_query.get_cached_file(
                cloud_dir, file_name
            )
            strm_path = self._generate_strm(
                cloud_dir,
                file_name,
                target_file=target_file,
                lookup_target=False,
                log_success=False,
            )
            ready_items.append(item)
            if strm_path:
                results[result_key] = (strm_path, "")
                generated += 1
            else:
                queued_items.append(item)
        self._scrape_metadata_batch(ready_items, mediainfo, season=season)
        pending_keys = self._queue_file_finalize_batch(
            queued_items,
            mediainfo,
            subscribe_id=subscribe_id,
            season=season,
            sub_key=sub_key,
            transient_target=transient_target,
            target_subscribe=target_subscribe,
        )
        for item in queued_items:
            result_key = str(item["result_key"])
            results[result_key] = (None, pending_keys.get(result_key, ""))
        if items:
            logger.debug(
                f"批量文件终态检查完成：即时生成 STRM {generated} 个，"
                f"待移动或文件就绪 {len(pending_keys)} 个"
            )
        return results

    def _finish_pending_subscription(
            self,
            item: Dict[str, Any],
            media_data: Dict[str, Any],
            mediainfo: Optional[MediaInfo] = None,
    ) -> None:
        """文件最终就绪后再更新订阅进度并执行完结。"""
        if item.get("transient_target"):
            return
        task_type = str(item.get("task_type") or "share").strip().lower()
        provider_name = str(
            getattr(self._cloud_drive, "name", "网盘") or "网盘"
        )
        completion_source = {
            "share": f"{provider_name}分享转存",
            "cloud": f"{provider_name}路径整理",
            "cross_cloud": f"跨盘转存到{provider_name}后整理",
            "ed2k": "ED2K离线下载",
            "magnet": "Magnet离线下载",
        }.get(task_type, "文件后处理")
        subscribe_id = int(item.get("subscribe_id") or 0)
        episode_values = (
                item.get("success_episodes")
                or item.get("notification_episodes")
                or ([item.get("episode")] if item.get("episode") else [])
        )
        success_episodes = [
            int(episode)
            for episode in episode_values
            if int(episode) > 0
        ]
        if mediainfo is None and media_data:
            try:
                mediainfo = self._deserialize_mediainfo(media_data)
            except Exception as error:
                logger.warning(f"后处理订阅进度媒体信息恢复失败：{error}")
        if not mediainfo or not success_episodes:
            logger.warning(
                f"跳过后处理订阅进度更新：媒体信息={'有' if mediainfo else '无'}，"
                f"完成集数={success_episodes or '无'}"
            )
            return
        try:
            subscribe = None
            if subscribe_id:
                with SessionFactory() as db:
                    subscribe = SubscribeOper(db=db).get(subscribe_id)
            if not subscribe and mediainfo.tmdb_id:
                season = (
                    max(1, int(item.get("season") or 1))
                    if mediainfo.type == MediaType.TV else None
                )
                candidates = list_subscribes_by_tmdb_id(
                    SubscribeOper(), mediainfo.tmdb_id, season
                )
                subscribe = next(
                    (
                        candidate for candidate in candidates
                        if str(getattr(candidate, "type", "")) == mediainfo.type.value
                    ),
                    None,
                )
                if subscribe:
                    subscribe_id = int(subscribe.id)
                    item["subscribe_id"] = subscribe_id
                    logger.info(
                        f"后处理任务已重新关联订阅：{subscribe.name}，"
                        f"订阅ID={subscribe_id}"
                    )
            if not subscribe:
                logger.warning(
                    f"后处理完成时未找到对应订阅：订阅ID={subscribe_id or '无'}，"
                    f"TMDB={mediainfo.tmdb_id}，季={item.get('season') or '-'}"
                )
                return
            self._subscribe_handler.check_and_finish_subscribe(
                subscribe=subscribe,
                mediainfo=mediainfo,
                success_episodes=success_episodes,
            )
            with SessionFactory() as db:
                remaining_subscribe = SubscribeOper(db=db).get(subscribe_id)
            if remaining_subscribe:
                downloaded = {
                    int(episode)
                    for episode in (getattr(remaining_subscribe, "note", None) or [])
                    if str(episode).isdigit()
                }
                total_ep = int(
                    getattr(remaining_subscribe, "total_episode", 0) or 0
                )
                start_ep = int(
                    getattr(remaining_subscribe, "start_episode", 1) or 1
                )
                expected_count = max(0, total_ep - start_ep + 1)
                completed_count = len({
                    episode for episode in downloaded
                    if start_ep <= episode <= total_ep
                }) if expected_count else len(downloaded)
                progress = (
                    int(completed_count * 100 / expected_count)
                    if expected_count else 0
                )
                self._set_task_phase(
                    remaining_subscribe,
                    f"{completion_source}完成，订阅进度 "
                    f"{completed_count}/{expected_count or '-'}",
                    progress,
                )
                logger.debug(
                    f"{completion_source}完成后订阅进度已更新："
                    f"{remaining_subscribe.name}，"
                    f"已完成 {completed_count}/{expected_count or '-'}，"
                    f"缺失 {int(getattr(remaining_subscribe, 'lack_episode', 0) or 0)} 集"
                )
            else:
                self._set_task_phase(subscribe, "订阅已完成并移至历史", 100)
                logger.debug(
                    f"{completion_source}完成后订阅已完结并移至历史："
                    f"{subscribe.name}"
                )
            sub_key = str(item.get("sub_key") or "")
            should_clear_points = mediainfo.type == MediaType.MOVIE
            if mediainfo.type == MediaType.TV:
                total_ep = int(getattr(subscribe, "total_episode", 0) or 0)
                start_ep = int(getattr(subscribe, "start_episode", 1) or 1)
                if total_ep >= start_ep:
                    expected = set(range(start_ep, total_ep + 1))
                    downloaded = set(getattr(subscribe, "note", None) or [])
                    downloaded.update(success_episodes)
                    should_clear_points = not (expected - downloaded)
            if (
                    should_clear_points
                    and sub_key
                    and hasattr(self._search_handler, "clear_subscription_budgets")
            ):
                self._search_handler.clear_subscription_budgets(sub_key)
        except Exception as error:
            logger.error(f"文件后处理完成后更新订阅失败：{subscribe_id}，{error}")

    def _select_media_root(self, root_path: str, mediainfo: MediaInfo) -> str:
        """按媒体类型选择电影/电视剧媒体根目录；未配置时回退传入的媒体库根目录。

        仅当调用方传入的根目录是云盘媒体库根（_CLOUD_MEDIA_ROOT）时才应用
        电影/电视剧目录覆盖；本地资源根等其它根目录原样返回，避免劫持
        本地通知/STRM 的路径解析。
        """
        # 本地资源根等其他根目录不参与电影/电视剧目录覆盖。
        if str(root_path or "/") != self._CLOUD_MEDIA_ROOT:
            return root_path or "/"
        media_type_value = getattr(getattr(mediainfo, "type", None), "value", None)
        if media_type_value == MediaType.MOVIE.value:
            return self._MOVIE_MEDIA_ROOT or root_path or "/"
        if media_type_value == MediaType.TV.value:
            return self._TV_MEDIA_ROOT or root_path or "/"
        return root_path or "/"

    def _platform_classified_root(
            self,
            root_path: str,
            subscribe,
            mediainfo: MediaInfo,
    ) -> Optional[Path]:
        """缓存分类根目录，避免逐集重复执行相同目录规则。"""
        media_source, media_id = media_identity(mediainfo)
        key = (
            str(root_path),
            media_source,
            media_id,
            getattr(mediainfo, "tmdb_id", None),
            getattr(mediainfo, "title", None),
            getattr(mediainfo, "year", None),
            getattr(mediainfo, "type", None),
            getattr(mediainfo, "category", None),
            getattr(subscribe, "id", None),
            getattr(subscribe, "media_category", None),
        )
        cache_key = normalize_platform_cache_key(key)
        with self._platform_root_lock:
            if cache_key in self._platform_root_cache:
                return self._platform_root_cache.get(cache_key)

        # 整理目标必须命中平台配置的媒体分类目录。
        # include_unsorted=True 会把“未分类”目录（通常就是媒体根目录）
        # 当作有效结果，导致电视剧绕过“电视剧”分类文件夹直接落到根目录。
        directory = DirectoryHelper().get_dir(media=mediainfo, include_unsorted=False)
        resolved = None
        if directory:
            # 不再覆盖 MoviePilot 目录配置的 library_path：
            # 类型层（电视剧/电影）由目录配置决定，插件只在未命中时按类型兜底。
            classified_root = TransHandler().get_dest_dir(
                mediainfo=mediainfo,
                target_dir=directory,
            )
            if classified_root:
                resolved = Path(classified_root)
        if resolved is None:
            # 平台未配置媒体分类目录（如媒体库目录未在 MoviePilot 中登记）时，
            # 按 MoviePilot 标准分类兜底：<媒体库>/电影|电视剧，避免全部落到根目录。
            media_type_value = getattr(
                getattr(mediainfo, "type", None), "value", None
            )
            if media_type_value in {
                MediaType.MOVIE.value,
                MediaType.TV.value,
            }:
                resolved = Path(str(root_path or "/")) / media_type_value

        with self._platform_root_lock:
            self._platform_root_cache.set(cache_key, resolved)
        return resolved

    def _platform_rename_path(
            self,
            root_path: str,
            subscribe,
            mediainfo: MediaInfo,
            source_name: str,
            season: int = None,
            episode: int = None,
    ) -> Optional[Path]:
        """使用当前分类目录和重命名模板生成完整目标路径。"""
        effective_media = self._effective_mediainfo(subscribe, mediainfo)
        media_root = self._select_media_root(root_path, effective_media)
        classified_root = self._platform_classified_root(
            media_root, subscribe, effective_media
        )
        if not classified_root:
            return None
        meta = MetaInfo(source_name)
        meta.type = effective_media.type
        meta.year = getattr(subscribe, "year", None) or effective_media.year
        if season is not None:
            meta.begin_season = season
        if episode is not None:
            meta.begin_episode = episode
        relative_name = FileManagerModule.recommend_name(meta, effective_media)
        if not relative_name:
            return None
        return classified_root / Path(relative_name)

    def _platform_target(
            self,
            root_path: str,
            subscribe,
            mediainfo: MediaInfo,
            source_name: str,
            season: int = None,
            episode: int = None,
    ) -> Tuple[str, str]:
        """生成平台分类后的目标目录和规范文件名。"""
        target_path = self._platform_rename_path(
            root_path, subscribe, mediainfo, source_name, season, episode
        )
        if not target_path:
            raise ValueError(f"MoviePilot 未生成目标路径：{mediainfo.title_year}")
        return target_path.parent.as_posix(), target_path.name

    @staticmethod
    def _effective_mediainfo(subscribe, mediainfo: MediaInfo) -> MediaInfo:
        """使用订阅卡片的展示信息生成整理专用媒体副本。"""
        effective_media = copy.deepcopy(mediainfo)
        subscribe_title = str(getattr(subscribe, "name", "") or "").strip()
        if subscribe_title:
            effective_media.title = subscribe_title
        subscribe_year = getattr(subscribe, "year", None)
        if subscribe_year:
            effective_media.year = subscribe_year
        media_category = getattr(subscribe, "media_category", None)
        if media_category:
            effective_media.category = media_category
        return effective_media

    def _resolve_resource_season_dir(
            self,
            resource_root: str,
            subscribe,
            mediainfo: MediaInfo,
            season: int
    ) -> Optional[Path]:
        """使用的目录分类和命名规则生成媒体季目录。"""
        if not resource_root or not mediainfo:
            return None

        media_type = getattr(getattr(mediainfo, "type", None), "value", None)
        media_source, media_id = media_identity(mediainfo)
        cache_key = (
            str(resource_root),
            media_type or str(getattr(mediainfo, "type", "") or ""),
            media_source,
            media_id,
            getattr(mediainfo, "tmdb_id", None),
            getattr(mediainfo, "title", None),
            getattr(mediainfo, "year", None),
            getattr(mediainfo, "category", None),
            getattr(subscribe, "id", None),
            getattr(subscribe, "name", None),
            getattr(subscribe, "year", None),
            getattr(subscribe, "media_category", None),
            int(season or 0),
        )
        platform_cache_key = normalize_platform_cache_key(cache_key)
        with self._resource_season_dir_lock:
            if platform_cache_key in self._resource_season_dir_cache:
                return self._resource_season_dir_cache.get(platform_cache_key)
            resolved = None
            try:
                rename_path = self._platform_rename_path(
                    root_path=resource_root,
                    subscribe=subscribe,
                    mediainfo=mediainfo,
                    source_name=getattr(subscribe, "name", None) or mediainfo.title,
                    season=season,
                    episode=1,
                )
                if rename_path:
                    resolved = rename_path.parent
            except Exception as error:
                logger.warning(f"资源路径解析失败：{mediainfo.title_year}，{error}")
            self._resource_season_dir_cache.set(platform_cache_key, resolved)
            return resolved

    def _get_local_resource_files(
            self,
            subscribe,
            mediainfo: MediaInfo,
            season: int
    ) -> List[Path]:
        """获取平台规则生成的季目录中的本地或挂载媒体文件。"""
        season_dir = self._resolve_resource_season_dir(
            self._local_resource_path, subscribe, mediainfo, season
        )
        if not season_dir:
            return []
        if not season_dir.is_dir():
            logger.debug(f"资源季目录不存在，跳过扫描: {season_dir}")
            return []
        try:
            allowed_extensions = set(MediaFileParser.VIDEO_EXTENSIONS) | {".strm"}
            return [
                item for item in season_dir.iterdir()
                if item.is_file() and item.suffix.lower() in allowed_extensions
            ]
        except OSError as error:
            logger.warning(f"资源季目录读取失败 {season_dir}: {error}")
            return []

    def _scan_local_resource_episodes(
            self,
            subscribe,
            mediainfo: MediaInfo,
            season: int,
            start_episode: Optional[int] = None,
            total_episode: Optional[int] = None
    ) -> Set[int]:
        """按元数据解析器识别已落盘或已挂载的剧集。"""
        resource_files = self._get_local_resource_files(subscribe, mediainfo, season)
        found_episodes = self._parse_resource_episode_names(
            (resource_file.name for resource_file in resource_files),
            season=season,
            start_episode=start_episode,
            total_episode=total_episode,
        )

        if found_episodes:
            logger.info(
                f"媒体路径检查：{getattr(subscribe, 'name', '?')} S{season:02d} "
                f"识别到 {len(found_episodes)} 集"
            )
        return found_episodes

    @staticmethod
    def _parse_resource_episode_names(
            file_names,
            season: int,
            start_episode: Optional[int] = None,
            total_episode: Optional[int] = None,
    ) -> Set[int]:
        """使用元数据解析器从文件名提取目标季集数。"""
        found_episodes = set()
        for file_name in file_names:
            file_meta = MetaInfo(Path(str(file_name)).stem)
            file_season = file_meta.begin_season or season
            if file_season != season:
                continue
            episodes = list(getattr(file_meta, "episode_list", None) or [])
            if not episodes and file_meta.begin_episode:
                episodes = [file_meta.begin_episode]
            for episode in episodes:
                if start_episode is not None and episode < start_episode:
                    continue
                if total_episode and episode > total_episode:
                    continue
                found_episodes.add(int(episode))
        return found_episodes

    def _scan_cloud_resource_episode_files(
            self,
            subscribe,
            mediainfo: MediaInfo,
            season: int,
            start_episode: int,
            total_episode: int,
    ) -> Tuple[bool, Dict[int, CloudFile], str]:
        """一次读取目标季目录，返回真实存在的逐集网盘文件。"""
        cloud_dir = self._resolve_resource_season_dir(
            self._CLOUD_MEDIA_ROOT, subscribe, mediainfo, season
        )
        if not cloud_dir:
            return False, {}, ""
        cloud_path = cloud_dir.as_posix()
        lookup = self._cloud_directories.resolve_directory(cloud_path)
        if not lookup.checked:
            return False, {}, cloud_path
        if lookup.directory_id is None:
            return True, {}, cloud_path

        listing = self._cloud_directories.list_directory(lookup.directory_id)
        if not listing.checked:
            return False, {}, cloud_path
        episode_files: Dict[int, CloudFile] = {}
        for item in listing.files:
            if item.is_directory:
                continue
            name = item.name
            if not MediaFileParser.is_video(name):
                continue
            episodes = self._parse_resource_episode_names(
                [name], season, start_episode, total_episode
            )
            for episode in episodes:
                current = episode_files.get(episode)
                if not current:
                    episode_files[episode] = item
                    continue
                current_size = int(getattr(current, "size", 0) or 0)
                candidate_size = int(getattr(item, "size", 0) or 0)
                prefer_candidate = (
                    candidate_size < current_size
                    if self._upgrade_mode == "smallest"
                    else candidate_size > current_size
                )
                if prefer_candidate:
                    episode_files[episode] = item
        return True, episode_files, cloud_path

    def _scan_cloud_resource_episodes(
            self,
            subscribe,
            mediainfo: MediaInfo,
            season: int,
            start_episode: int,
            total_episode: int,
    ) -> Tuple[bool, Set[int], str]:
        """扫描平台规则生成的网盘季目录；目录不存在时不创建。"""
        valid, episode_files, cloud_path = self._scan_cloud_resource_episode_files(
            subscribe=subscribe,
            mediainfo=mediainfo,
            season=season,
            start_episode=start_episode,
            total_episode=total_episode,
        )
        label = f"115媒体路径 {cloud_path}" if cloud_path else ""
        return valid, set(episode_files), label

    def _find_cloud_movie_file(
            self,
            subscribe,
            mediainfo: MediaInfo,
    ) -> Optional[Tuple[str, str, CloudFile]]:
        """只检查平台规则生成的网盘电影目录，不递归扫描其他路径。"""
        try:
            cloud_dir, expected_name = self._platform_target(
                self._CLOUD_MEDIA_ROOT,
                subscribe,
                mediainfo,
                f"{getattr(subscribe, 'name', None) or mediainfo.title}.mkv",
            )
        except Exception as error:
            logger.warning(f"115电影目标路径计算失败：{mediainfo.title_year}，{error}")
            return None
        lookup = self._cloud_directories.resolve_directory(cloud_dir)
        if not lookup.checked or lookup.directory_id is None:
            return None
        expected_stem = Path(expected_name).stem
        listing = self._cloud_directories.list_directory(lookup.directory_id)
        if not listing.checked:
            return None
        for item in listing.files:
            if item.is_directory:
                continue
            name = item.name
            path = Path(name)
            if not MediaFileParser.is_video(name):
                continue
            if path.stem == expected_stem:
                return cloud_dir, name, item
        return None

    @staticmethod
    def _summarize_share_episodes(
            files: List[dict], season: int, mediainfo: Optional[MediaInfo] = None
    ) -> Tuple[int, Set[int]]:
        """递归统计分享中的实际视频数量和目标季集数。"""
        video_count = 0
        episodes = set()

        def walk(items: List[dict]):
            nonlocal video_count
            for item in items or []:
                if item.get("is_dir"):
                    walk(item.get("children") or [])
                    continue
                name = str(item.get("name") or "")
                if not MediaFileParser.is_video(name):
                    continue
                video_count += 1
                episode = FileMatcher.episode_from_file(item, season, mediainfo)
                if episode is not None:
                    episodes.add(episode)

        walk(files)
        return video_count, episodes

    @staticmethod
    def _format_episode_ranges(episodes: Set[int]) -> str:
        """把集数集合压缩为 E01-E03、E05 形式，避免日志刷屏。"""
        numbers = sorted({int(episode) for episode in episodes})
        if not numbers:
            return "无"
        ranges = []
        start = previous = numbers[0]
        for number in numbers[1:]:
            if number == previous + 1:
                previous = number
                continue
            ranges.append(f"E{start:02d}" if start == previous else f"E{start:02d}-E{previous:02d}")
            start = previous = number
        ranges.append(f"E{start:02d}" if start == previous else f"E{start:02d}-E{previous:02d}")
        return "、".join(ranges)

    @staticmethod
    def _normalize_cloud_path(path: str) -> str:
        return str(PurePosixPath("/" + str(path or "/").strip().lstrip("/")))

    def _cross_transfer_staging_path(self, provider_key: str) -> str:
        base_path = self._cloud_transfer_paths.get(
            str(provider_key or "").strip().lower(), "/"
        )
        # 直接复用已配置的转存目录，不为跨盘任务创建额外目录。
        return str(PurePosixPath(base_path))

    @staticmethod
    def _cleanup_cross_transfer_staging(
            source: CloudDriveProvider, staged_path: str,
            item: Optional[CloudFile] = None,
    ) -> None:
        if not source.supports(CloudDriveCapability.FILE_MUTATION):
            return
        mutation = source.require(CloudDriveCapability.FILE_MUTATION)
        if staged_path and source.supports(CloudDriveCapability.DIRECTORY_READ):
            try:
                lookup = source.require(
                    CloudDriveCapability.DIRECTORY_READ
                ).resolve_directory(staged_path)
                if lookup.checked and lookup.directory_id is not None:
                    if mutation.delete_file(lookup.directory_id):
                        return
            except Exception as error:
                logger.warning(f"清理源盘跨盘临时目录失败：{error}")
        if item:
            try:
                mutation.delete_file(item.id)
            except Exception as error:
                logger.warning(f"清理源盘跨盘临时文件失败：{error}")

    def _transfer_file(
            self, share_url: str, file_item: Dict[str, Any], save_path: str,
            target_name: str, source_sha1: str = "",
            parent_task_id: str = "",
            stop_requested: Optional[Callable[[], bool]] = None,
            media_type: str = "",
    ) -> bool:
        # 转存失败原因回写 file_item，供调用方落 history failure_reason（v1.5.3 T3）。
        file_item.pop("transfer_failure_reason", None)
        should_stop = stop_requested or self._stop_requested
        cloud_resource = self._is_cloud_resource_url(share_url)
        source = self._resource_provider_for_url(share_url)
        cross_provider = bool(
            source and self._cloud_drive and source.key != self._cloud_drive.key
        )
        if cross_provider:
            item_media_type = self._normalize_cross_transfer_media_type(
                file_item.get("media_type") or media_type
            )
            is_manual_override = bool(
                file_item.get("source") == "manual"
                or file_item.get("is_cross")
                or file_item.get("_manual")
            )
            if not is_manual_override:
                if item_media_type and item_media_type not in self._cross_transfer_media_types:
                    logger.debug(
                        f"跨盘转存跳过：{source.name} -> {self._cloud_drive.name}，"
                        f"媒体类型 {item_media_type} 未启用"
                    )
                    file_item["transfer_failure_reason"] = (
                        f"跨盘转存未启用媒体类型 {item_media_type}"
                    )
                    return False
            required = (
                    (self._cross_transfer_enabled or is_manual_override)
                    and self._cross_transfer_manager
                    and (
                            cloud_resource
                            or source.supports(CloudDriveCapability.SHARE_TRANSFER)
                    )
                    and source.supports(CloudDriveCapability.FILE_QUERY)
                    and source.supports(CloudDriveCapability.FILE_DOWNLOAD)
                    and self._cloud_drive.supports(CloudDriveCapability.LOCAL_UPLOAD)
                    and self._cloud_drive.supports(CloudDriveCapability.FILE_QUERY)
            )
            if not required:
                logger.warning(
                    f"无法跨盘转存单个文件：{source.name} -> "
                    f"{self._cloud_drive.name}，请检查跨盘开关和网盘能力"
                )
                file_item["transfer_failure_reason"] = "跨盘转存开关未开启或网盘能力不满足"
                return False
            if cloud_resource:
                item = self._cloud_file_from_dict(file_item)
                if not item.id:
                    logger.warning(f"无法跨盘整理文件：{source.name} 文件 ID 为空")
                    file_item["transfer_failure_reason"] = "源网盘文件 ID 为空"
                    return False
            else:
                source_file_id = str(file_item.get("id") or "").strip()
                if not source_file_id:
                    logger.warning(f"无法跨盘转存单个文件：{source.name} 文件 ID 为空")
                    file_item["transfer_failure_reason"] = "源网盘文件 ID 为空"
                    return False
                staged_path = self._cross_transfer_staging_path(source.key)
                source_share = source.require(CloudDriveCapability.SHARE_TRANSFER)
                try:
                    staged = source_share.transfer_file(
                        share_url=share_url, file_id=source_file_id,
                        save_path=staged_path,
                        target_name=file_item.get("name") or target_name,
                    )
                except Exception:
                    self._cleanup_cross_transfer_staging(source, "")
                    raise
                if not staged:
                    self._blacklist_dead_link_share(source_share, share_url)
                    self._cleanup_cross_transfer_staging(source, "")
                    file_item["transfer_failure_reason"] = (
                        f"分享转存到源盘暂存失败：{source.name}"
                    )
                    return False
                source_files = source.require(CloudDriveCapability.FILE_QUERY)
                staged_name = file_item.get("name") or target_name
                item = None
                for attempt in range(10):
                    item = source_files.find_file(staged_path, staged_name)
                    if item or should_stop():
                        break
                    time.sleep(min(0.5 + attempt * 0.25, 2.0))
                if not item:
                    logger.warning(
                        f"跨盘临时文件尚未可见：{source.name} "
                        f"{staged_path}/{staged_name}"
                    )
                    self._cleanup_cross_transfer_staging(source, "")
                    file_item["transfer_failure_reason"] = (
                        f"源盘暂存文件不可见：{staged_path}/{staged_name}"
                    )
                    return False
            if source_sha1 and not item.sha1:
                item = CloudFile(
                    item.id,
                    item.name,
                    False,
                    item.size,
                    source_sha1,
                    item.md5,
                    playback_values=item.playback_values,
                    native=item.native,
                )
            try:
                if not parent_task_id:
                    parent_task_id, _ = self._current_task_context()
                task = self._cross_transfer_manager.create_from_cloud_file(
                    source.key, item, self._cloud_drive.key, save_path, target_name,
                    fallback=True,
                    parent_task_id=parent_task_id,
                )
                success = self._cross_transfer_manager.wait(
                    task["id"], cancel_check=should_stop
                )
                completed_task = next(
                    (
                        value for value in self._cross_transfer_manager.list()
                        if value.get("id") == task["id"]
                    ),
                    {},
                )
                if success:
                    result_name = str(
                        completed_task.get("result_file_name")
                        or target_name or item.name
                    ).strip()
                    if result_name:
                        file_item["staging_name"] = result_name
                    result_sha1 = str(
                        completed_task.get("result_sha1") or ""
                    ).strip()
                    result_md5 = str(
                        completed_task.get("result_md5") or ""
                    ).strip()
                    if result_sha1:
                        file_item["sha1"] = result_sha1
                    if result_md5:
                        file_item["md5"] = result_md5
                    result_size = int(
                        completed_task.get("result_file_size") or 0
                    )
                    if result_size > 0:
                        file_item["size"] = result_size
                if not success:
                    if (
                            should_stop()
                            or completed_task.get("status") in {"canceled", "stopping"}
                    ):
                        logger.info(
                            f"跨盘转存已由用户停止：{source.name} -> "
                            f"{self._cloud_drive.name}"
                        )
                        file_item["transfer_failure_reason"] = "跨盘转存已由用户停止"
                    else:
                        cross_reason = (
                                f"跨盘转存失败：{source.name} -> {self._cloud_drive.name}，"
                                f"阶段={completed_task.get('phase') or 'unknown'}，"
                                f"原因={completed_task.get('error') or completed_task.get('message') or '未知错误'}"
                        )
                        logger.error(cross_reason)
                        file_item["transfer_failure_reason"] = cross_reason
                return success
            finally:
                if not cloud_resource:
                    # 只清理分享转存产生的源盘暂存文件，绝不删除用户选择的网盘文件。
                    self._cleanup_cross_transfer_staging(source, "", item)
        service = source.require(CloudDriveCapability.SHARE_TRANSFER) if source else self._share_transfer
        try:
            transferred = bool(service.transfer_file(
                share_url=share_url, file_id=file_item.get("id"),
                save_path=save_path, target_name=target_name,
                source_sha1=source_sha1,
            ))
        except Exception as error:
            file_item["transfer_failure_reason"] = f"分享转存异常：{error}"
            raise
        if not transferred:
            self._blacklist_dead_link_share(service, share_url)
            file_item["transfer_failure_reason"] = (
                "分享转存失败：链接可能已失效或触发风控"
            )
        return transferred

    @staticmethod
    def _reconcile_subscribe_physical_episodes(
            subscribe,
            episodes: Set[int],
            start_episode: int,
            total_episode: int,
    ) -> Dict[str, Any]:
        """以 Emby 与115实际数据纠正订阅进度，包括移除误标集数。"""
        expected = set(range(start_episode, total_episode + 1))
        verified = {int(episode) for episode in episodes} & expected
        current = {
            int(episode) for episode in (subscribe.note or [])
            if str(episode).isdigit()
        }
        new_note = sorted(verified)
        new_lack = len(expected - verified)
        update_data = {}
        if current != verified:
            update_data["note"] = new_note
        if int(subscribe.lack_episode or 0) != new_lack:
            update_data["lack_episode"] = new_lack
        if update_data:
            SubscribeOper().update(subscribe.id, update_data)
            subscribe.note = new_note
            subscribe.lack_episode = new_lack
        return {
            "added": sorted(verified - current),
            "removed": sorted(current - verified),
            "missing": sorted(expected - verified),
            "updated": bool(update_data),
        }

    def send_transfer_notification(
            self, transfer_details: List[Dict[str, Any]], total_count: int
    ) -> None:
        """完成即入队，并按延迟窗口合并相邻任务通知。"""
        if not transfer_details or not self._post_message:
            return
        with self._notification_batch_lock:
            self._notification_batch.extend(copy.deepcopy(transfer_details))
            if self._notification_batch_timer:
                self._notification_batch_timer.cancel()
            wait_seconds = max(
                self._notification_delay_seconds,
                self._NOTIFICATION_BATCH_WINDOW_SECONDS,
            )
            self._notification_batch_timer = threading.Timer(
                wait_seconds, self._flush_transfer_notifications
            )
            self._notification_batch_timer.daemon = True
            self._notification_batch_timer.start()
        logger.debug(
            f"完成通知已入队：{total_count} 个文件，"
            f"静默 {wait_seconds} 秒后合并发送"
        )

    def _flush_transfer_notifications(self) -> None:
        with self._notification_batch_lock:
            timer = self._notification_batch_timer
            self._notification_batch_timer = None
            if timer and timer is not threading.current_thread():
                timer.cancel()
            transfer_details = self._notification_batch
            self._notification_batch = []
        if not transfer_details or not self._post_message or not self._notify:
            return
        try:
            self._send_transfer_notification_now(transfer_details)
        except Exception as error:
            logger.warning(f"完成通知发送失败：{error}")

    def _send_transfer_notification_now(
            self, transfer_details: List[Dict[str, Any]]
    ) -> None:
        """按普通转存、跨盘转存和洗版分别发送聚合后的完成通知。"""
        kind_config = {
            "transfer": ("【网盘搜索助手】转存完成", "转存"),
            "cross_transfer": ("【网盘搜索助手】跨盘转存完成", "跨盘转存"),
            "upgrade": ("【网盘洗版】洗版完成", "洗版"),
        }
        grouped: Dict[str, List[Dict[str, Any]]] = {}
        for detail in transfer_details:
            kind = str(detail.get("notification_kind") or "transfer")
            grouped.setdefault(kind if kind in kind_config else "transfer", []).append(detail)

        for kind, details in grouped.items():
            text_lines = []
            first_image = None
            file_count = 0
            for detail in details:
                if detail.get("type") == "电影":
                    title = detail.get("title", "未知")
                    year = detail.get("year", "")
                    text_lines.append(f"{title} ({year})")
                    file_count += 1
                else:
                    title = detail.get("title", "未知")
                    season = max(1, int(detail.get("season") or 1))
                    episodes = sorted(detail.get("episodes") or [])
                    file_count += len(episodes)
                    if len(episodes) <= 5:
                        ep_str = ", ".join(f"E{episode:02d}" for episode in episodes)
                    else:
                        ep_str = (
                            f"E{episodes[0]:02d}-E{episodes[-1]:02d} "
                            f"共{len(episodes)}集"
                        )
                    text_lines.append(f"{title} S{season:02d} {ep_str}")
                if not first_image and detail.get("image"):
                    first_image = detail.get("image")
            if len(text_lines) > 10:
                text_lines = text_lines[:10]
                text_lines.append(f"... 等共 {len(details)} 项")
            notification_title, action = kind_config[kind]
            self._post_message(
                mtype=self._notification_type,
                title=notification_title,
                text=f"本次共{action} {file_count} 个文件\n\n" + "\n".join(text_lines),
                image=first_image,
            )
            logger.info(
                f"{action}完成通知已发送：{file_count} 个文件，"
                f"{len(details)} 个媒体项"
            )

    def guardian_check(self, all_subs) -> int:
        """
        集数守护 & 日历修复：扫描媒体库 strm 文件，同步订阅 note/lack_episode。

        修复场景：
        - PT bypass、115直搜、洗版模式等非标准路径下载后 note 未更新
        - 日历显示"未入库"但文件实际已在媒体库中
        - 订阅进度与实际文件不一致

        :param all_subs: 所有订阅列表（SubscribeOper().list() 结果）
        :return: 本次完成的订阅数（新增的 lack_episode=0 的个数）
        """
        from app.db.subscribe_oper import SubscribeOper
        from app.schemas.types import MediaType

        completed_count = 0

        for subscribe in all_subs:
            try:
                # 只处理活跃的电视剧订阅
                if getattr(subscribe, 'state', None) == 'D':
                    continue
                sub_type = getattr(subscribe, 'type', None)
                if sub_type != MediaType.TV.value:
                    continue

                season = subscribe.season or 1
                total_ep = subscribe.total_episode or 0
                start_ep = subscribe.start_episode or 1

                if total_ep <= 0:
                    continue

                mediainfo = self._subscribe_mediainfo(
                    subscribe, MediaType.TV
                )
                if not mediainfo:
                    continue

                found_episodes = self._scan_local_resource_episodes(
                    subscribe=subscribe,
                    mediainfo=mediainfo,
                    season=season,
                    start_episode=start_ep,
                    total_episode=total_ep,
                )
                if not found_episodes:
                    continue

                remaining_lack = self._subscribe_handler.check_and_finish_subscribe(
                    subscribe=subscribe,
                    mediainfo=mediainfo,
                    success_episodes=sorted(found_episodes),
                )
                if remaining_lack == 0:
                    completed_count += 1

            except Exception as e:
                logger.warning(f"订阅完结检查异常 {getattr(subscribe, 'name', '?')}：{e}")
                import traceback
                logger.debug(traceback.format_exc())

        return completed_count
