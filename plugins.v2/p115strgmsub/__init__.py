"""
115网盘订阅追更插件
结合MoviePilot订阅功能，自动搜索115网盘资源并转存缺失剧集
"""
import datetime
from pathlib import Path
from threading import Lock
from typing import Optional, Any, List, Dict, Tuple

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.core.config import settings, global_vars
from app.core.event import Event, eventmanager
from app.db import SessionFactory
from app.db.subscribe_oper import SubscribeOper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import EventType, MediaType, NotificationType

from .clients import PanSouClient, P115ClientManager, NullbrClient
from .handlers import SearchHandler, SyncHandler, SubscribeHandler, ApiHandler
from .ui import UIConfig
from .utils import (
    download_so_file,
    get_hdhive_token_info,
    check_hdhive_cookie_valid,
    refresh_hdhive_cookie_with_playwright,
)

lock = Lock()


class P115StrgmSub(_PluginBase):
    """115网盘订阅追更插件"""

    # 插件名称
    plugin_name = "115网盘订阅追更"
    # 插件描述
    plugin_desc = "结合MoviePilot订阅功能，自动搜索115网盘资源并转存缺失的电影和剧集。"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/jxxghp/MoviePilot-Plugins/main/icons/cloud.png"
    # 插件版本
    plugin_version = "1.1.5"
    # 插件作者
    plugin_author = "mrtian2016"
    # 作者主页
    author_url = "https://github.com/mrtian2016"
    # 插件配置项ID前缀
    plugin_config_prefix = "p115strgmsub_"
    # 加载顺序
    plugin_order = 20
    # 可使用的用户级别
    auth_level = 1

    # 私有变量
    _scheduler: Optional[BackgroundScheduler] = None

    # 配置属性
    _enabled: bool = False
    _onlyonce: bool = False
    _cron: str = "0 */8 * * *"
    _notify: bool = False
    _cookies: str = ""
    _pansou_enabled: bool = True
    _pansou_url: str = "https://so.252035.xyz"
    _pansou_username: str = ""
    _pansou_password: str = ""
    _pansou_auth_enabled: bool = False
    _pansou_channels: str = "QukanMovie"
    _save_path: str = "/我的接收/MoviePilot/TV"
    _movie_save_path: str = "/我的接收/MoviePilot/Movie"
    _only_115: bool = True
    _exclude_subscribes: List[int] = []
    _nullbr_enabled: bool = False
    _nullbr_appid: str = ""
    _nullbr_api_key: str = ""
    _hdhive_enabled: bool = False
    _hdhive_username: str = ""
    _hdhive_password: str = ""
    _hdhive_cookie: str = ""
    _hdhive_auto_refresh: bool = False
    _hdhive_refresh_before: int = 86400
    _hdhive_query_mode: str = "playwright"
    _block_system_subscribe: bool = False
    _max_transfer_per_sync: int = 50
    _batch_size: int = 20
    _skip_other_season_dirs: bool = True

    # 运行时对象
    _pansou_client: Optional[PanSouClient] = None
    _p115_manager: Optional[P115ClientManager] = None
    _nullbr_client: Optional[NullbrClient] = None
    _hdhive_client: Optional[Any] = None

    # 处理器
    _search_handler: Optional[SearchHandler] = None
    _subscribe_handler: Optional[SubscribeHandler] = None
    _sync_handler: Optional[SyncHandler] = None
    _api_handler: Optional[ApiHandler] = None

    def _check_and_refresh_hdhive_cookie(self) -> Optional[str]:
        """
        检查并刷新 HDHive Cookie（如果需要）

        :return: 有效的 Cookie 字符串，失败返回 None
        """
        if not self._hdhive_auto_refresh:
            return self._hdhive_cookie if self._hdhive_cookie else None

        if not self._hdhive_username or not self._hdhive_password:
            logger.warning("HDHive: 已启用自动刷新但未配置用户名/密码，无法刷新 Cookie")
            return self._hdhive_cookie if self._hdhive_cookie else None

        if self._hdhive_cookie:
            is_valid, reason = check_hdhive_cookie_valid(
                self._hdhive_cookie,
                self._hdhive_refresh_before
            )
            if is_valid:
                logger.info(f"HDHive: Cookie 检查通过 - {reason}")
                return self._hdhive_cookie
            else:
                logger.info(f"HDHive: Cookie 需要刷新 - {reason}")
        else:
            logger.info("HDHive: 未配置 Cookie，尝试登录获取")

        logger.info("HDHive: 开始刷新 Cookie...")
        new_cookie = refresh_hdhive_cookie_with_playwright(
            self._hdhive_username,
            self._hdhive_password
        )

        if new_cookie:
            token_info = get_hdhive_token_info(new_cookie)
            if token_info:
                logger.info(f"HDHive: 新 Cookie 信息 - 用户ID: {token_info['user_id']}, "
                           f"过期时间: {token_info['exp_time'].strftime('%Y-%m-%d %H:%M:%S')}, "
                           f"有效时间: {token_info['time_left'] / 3600:.1f} 小时")

            self._hdhive_cookie = new_cookie
            self.__update_config()
            logger.info("HDHive: Cookie 刷新成功并已保存到配置")
            return new_cookie
        else:
            logger.error("HDHive: Cookie 刷新失败")
            return self._hdhive_cookie if self._hdhive_cookie else None

    def init_plugin(self, config: dict = None):
        """初始化插件"""
        self.stop_service()

        download_so_file(Path(__file__).parent / "lib")

        if config:
            self._enabled = config.get("enabled", False)
            self._cron = config.get("cron", "30 */8 * * *")
            self._notify = config.get("notify", False)
            self._onlyonce = config.get("onlyonce", False)
            self._cookies = config.get("cookies", "")
            self._pansou_enabled = config.get("pansou_enabled", True)
            self._pansou_url = config.get("pansou_url", "https://so.252035.xyz/")
            self._pansou_username = config.get("pansou_username", "")
            self._pansou_password = config.get("pansou_password", "")
            self._pansou_auth_enabled = config.get("pansou_auth_enabled", False)
            self._pansou_channels = config.get("pansou_channels", "QukanMovie")
            self._save_path = config.get("save_path", "/我的接收/MoviePilot/TV")
            self._movie_save_path = config.get("movie_save_path", "/我的接收/MoviePilot/Movie")
            self._only_115 = config.get("only_115", True)
            self._exclude_subscribes = config.get("exclude_subscribes", []) or []
            self._nullbr_enabled = config.get("nullbr_enabled", False)
            self._nullbr_appid = config.get("nullbr_appid", "")
            self._nullbr_api_key = config.get("nullbr_api_key", "")
            self._hdhive_enabled = config.get("hdhive_enabled", False)
            self._hdhive_query_mode = config.get("hdhive_query_mode", "playwright")
            self._hdhive_username = config.get("hdhive_username", "")
            self._hdhive_password = config.get("hdhive_password", "")
            self._hdhive_cookie = config.get("hdhive_cookie", "")
            self._hdhive_auto_refresh = config.get("hdhive_auto_refresh", False)
            self._hdhive_refresh_before = int(config.get("hdhive_refresh_before", 86400) or 86400)
            self._max_transfer_per_sync = int(config.get("max_transfer_per_sync", 50) or 50)
            self._batch_size = int(config.get("batch_size", 20) or 20)
            self._skip_other_season_dirs = config.get("skip_other_season_dirs", True)

            new_block_state = config.get("block_system_subscribe", False)
            old_block_state = self._block_system_subscribe
            self._block_system_subscribe = new_block_state

            if new_block_state != old_block_state:
                self._init_subscribe_handler()
                self._subscribe_handler.update_subscribe_sites(new_block_state)

        self._init_clients()
        self._init_handlers()

        if self._enabled or self._onlyonce:
            if self._onlyonce:
                self._scheduler = BackgroundScheduler(timezone=settings.TZ)
                logger.info("115网盘订阅追更服务启动，立即运行一次")
                self._scheduler.add_job(
                    func=self.sync_subscribes,
                    trigger='date',
                    run_date=datetime.datetime.now(tz=pytz.timezone(settings.TZ)) + datetime.timedelta(seconds=3)
                )
                if self._scheduler.get_jobs():
                    self._scheduler.print_jobs()
                    self._scheduler.start()

            if self._onlyonce:
                self._onlyonce = False
                self.__update_config()

    def _init_clients(self):
        """初始化客户端"""
        proxy = settings.PROXY
        if proxy:
            logger.info(f"使用 MoviePilot PROXY: {proxy}")

        if self._pansou_enabled and self._pansou_url:
            self._pansou_client = PanSouClient(
                base_url=self._pansou_url,
                username=self._pansou_username,
                password=self._pansou_password,
                auth_enabled=self._pansou_auth_enabled,
                proxy=proxy
            )

        if self._nullbr_enabled:
            if not self._nullbr_appid or not self._nullbr_api_key:
                missing = []
                if not self._nullbr_appid:
                    missing.append("APP ID")
                if not self._nullbr_api_key:
                    missing.append("API Key")
                logger.warning(f"Nullbr 已启用但缺少必要配置：{', '.join(missing)}，将无法使用 Nullbr 查询功能")
                self._nullbr_client = None
            else:
                self._nullbr_client = NullbrClient(app_id=self._nullbr_appid, api_key=self._nullbr_api_key, proxy=proxy)
                logger.info("Nullbr 客户端初始化成功")

        if self._hdhive_enabled:
            if self._hdhive_query_mode == "playwright":
                if not self._hdhive_username or not self._hdhive_password:
                    logger.warning("HDHive Playwright 模式需要配置用户名和密码")
                    self._hdhive_client = None
                else:
                    logger.info("HDHive 配置已加载（Playwright 模式，搜索时动态创建客户端）")
                    self._hdhive_client = None
            else:
                if not self._hdhive_cookie and not (self._hdhive_username and self._hdhive_password):
                    logger.warning("HDHive API 模式需要配置 Cookie（或用户名/密码用于自动刷新）")
                    self._hdhive_client = None
                else:
                    effective_cookie = self._check_and_refresh_hdhive_cookie()

                    if effective_cookie:
                        try:
                            from .lib.hdhive import create_client as create_hdhive_client
                            logger.info(f"HDHive API 模式使用 PROXY: {proxy}")
                            self._hdhive_client = create_hdhive_client(cookie=effective_cookie, proxy=proxy)
                            logger.info("HDHive 客户端初始化成功（API 模式）")
                        except Exception as e:
                            logger.warning(f"HDHive 客户端初始化失败：{e}")
                            self._hdhive_client = None
                    else:
                        logger.warning("HDHive API 模式缺少有效 Cookie，请配置 Cookie 或启用自动刷新")
                        self._hdhive_client = None

        if self._cookies:
            self._p115_manager = P115ClientManager(cookies=self._cookies)

    def _init_subscribe_handler(self):
        """初始化订阅处理器"""
        self._subscribe_handler = SubscribeHandler(
            exclude_subscribes=self._exclude_subscribes,
            notify=self._notify,
            post_message_func=self.post_message
        )

    def _init_handlers(self):
        """初始化所有处理器"""
        self._search_handler = SearchHandler(
            pansou_client=self._pansou_client,
            nullbr_client=self._nullbr_client,
            hdhive_client=self._hdhive_client,
            pansou_enabled=self._pansou_enabled,
            nullbr_enabled=self._nullbr_enabled,
            hdhive_enabled=self._hdhive_enabled,
            hdhive_query_mode=self._hdhive_query_mode,
            hdhive_username=self._hdhive_username,
            hdhive_password=self._hdhive_password,
            hdhive_cookie=self._hdhive_cookie,
            only_115=self._only_115,
            pansou_channels=self._pansou_channels
        )

        self._init_subscribe_handler()

        self._sync_handler = SyncHandler(
            p115_manager=self._p115_manager,
            search_handler=self._search_handler,
            subscribe_handler=self._subscribe_handler,
            chain=self.chain,
            save_path=self._save_path,
            movie_save_path=self._movie_save_path,
            max_transfer_per_sync=self._max_transfer_per_sync,
            batch_size=self._batch_size,
            skip_other_season_dirs=self._skip_other_season_dirs,
            notify=self._notify,
            post_message_func=self.post_message,
            get_data_func=self.get_data,
            save_data_func=self.save_data
        )

        self._api_handler = ApiHandler(
            pansou_client=self._pansou_client,
            p115_manager=self._p115_manager,
            only_115=self._only_115,
            save_path=self._save_path,
            get_data_func=self.get_data,
            save_data_func=self.save_data
        )

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """定义远程控制命令"""
        return [{
            "cmd": "/p115_sync",
            "event": EventType.PluginAction,
            "desc": "115网盘订阅追更",
            "category": "订阅",
            "data": {
                "action": "p115_sync"
            }
        }]

    def get_api(self) -> List[Dict[str, Any]]:
        """获取插件API"""
        return [
            {
                "path": "/sync_subscribes",
                "endpoint": self.sync_subscribes,
                "methods": ["GET"],
                "summary": "执行同步订阅追更"
            },
            {
                "path": "/clear_history",
                "endpoint": self.api_clear_history,
                "methods": ["POST"],
                "summary": "清空历史记录"
            }
        ]

    def get_service(self) -> List[Dict[str, Any]]:
        """注册插件公共服务"""
        if self._enabled and self._cron:
            return [{
                "id": "P115StrgmSub",
                "name": "115网盘订阅追更服务",
                "trigger": CronTrigger.from_crontab(self._cron),
                "func": self.sync_subscribes,
                "kwargs": {}
            }]
        elif self._enabled:
            return [{
                "id": "P115StrgmSub",
                "name": "115网盘订阅追更服务",
                "trigger": "interval",
                "func": self.sync_subscribes,
                "kwargs": {"hours": 6}
            }]
        return []

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """拼装插件配置页面"""
        return UIConfig.get_form()

    def get_page(self) -> Optional[List[dict]]:
        """拼装插件详情页面"""
        history = self.get_data('history') or []
        return UIConfig.get_page(history)

    def __update_config(self):
        """更新配置"""
        self.update_config({
            "enabled": self._enabled,
            "notify": self._notify,
            "onlyonce": self._onlyonce,
            "only_115": self._only_115,
            "save_path": self._save_path,
            "movie_save_path": self._movie_save_path,
            "cookies": self._cookies,
            "pansou_enabled": self._pansou_enabled,
            "pansou_url": self._pansou_url,
            "pansou_username": self._pansou_username,
            "pansou_password": self._pansou_password,
            "pansou_auth_enabled": self._pansou_auth_enabled,
            "pansou_channels": self._pansou_channels,
            "nullbr_enabled": self._nullbr_enabled,
            "nullbr_appid": self._nullbr_appid,
            "nullbr_api_key": self._nullbr_api_key,
            "hdhive_enabled": self._hdhive_enabled,
            "hdhive_query_mode": self._hdhive_query_mode,
            "hdhive_username": self._hdhive_username,
            "hdhive_password": self._hdhive_password,
            "hdhive_cookie": self._hdhive_cookie,
            "hdhive_auto_refresh": self._hdhive_auto_refresh,
            "hdhive_refresh_before": self._hdhive_refresh_before,
            "exclude_subscribes": self._exclude_subscribes,
            "block_system_subscribe": self._block_system_subscribe,
            "max_transfer_per_sync": self._max_transfer_per_sync,
            "batch_size": self._batch_size,
            "skip_other_season_dirs": self._skip_other_season_dirs
        })

    def stop_service(self):
        """退出插件"""
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._scheduler.shutdown()
                self._scheduler = None
        except Exception as e:
            logger.error(f"退出插件失败：{str(e)}")

    def sync_subscribes(self):
        """同步订阅，搜索并转存缺失剧集"""
        with lock:
            self._do_sync()

    def _do_sync(self):
        """执行同步"""
        # 检查至少有一个搜索客户端可用
        if not self._pansou_enabled and not self._nullbr_enabled and not self._hdhive_enabled:
            logger.error("PanSou、Nullbr 和 HDHive 搜索源均未启用，请至少启用一个搜索源")
            if self._notify:
                self.post_message(
                    mtype=NotificationType.Plugin,
                    title="【115网盘订阅追更】配置错误",
                    text="PanSou、Nullbr 和 HDHive 搜索源均未启用，请至少启用一个搜索源。"
                )
            return

        # 检查已启用的搜索源是否成功初始化
        has_valid_client = False
        if self._pansou_enabled:
            if self._pansou_client:
                has_valid_client = True
            else:
                logger.warning("PanSou 已启用但客户端未初始化，请检查 PanSou URL 配置")

        if self._nullbr_enabled:
            if self._nullbr_client:
                has_valid_client = True
            else:
                logger.warning("Nullbr 已启用但客户端未初始化，请检查 APP ID 和 API Key 配置")

        if self._hdhive_enabled:
            if self._hdhive_client or (self._hdhive_username and self._hdhive_password):
                has_valid_client = True
            else:
                logger.warning("HDHive 已启用但缺少必要配置（Cookie 或 用户名/密码）")

        if not has_valid_client:
            logger.error("所有已启用的搜索源均初始化失败，请检查配置")
            if self._notify:
                self.post_message(
                    mtype=NotificationType.Plugin,
                    title="【115网盘订阅追更】配置错误",
                    text="所有已启用的搜索源均初始化失败，请检查 PanSou URL 或 Nullbr APP ID/API Key 配置。"
                )
            return

        if not self._p115_manager:
            logger.error("115 客户端未初始化，请检查 Cookie 配置")
            return

        # 验证 115 登录状态
        if not self._p115_manager.check_login():
            logger.error("115 登录失败，Cookie 可能已过期")
            if self._notify:
                self.post_message(
                    mtype=NotificationType.Manual,
                    title="【115网盘订阅追更】登录失败",
                    text="115 Cookie 可能已过期，请更新配置后重试。"
                )
            return

        logger.info("开始执行 115 网盘订阅追更...")
        if self._notify:
            self.post_message(
                mtype=NotificationType.Plugin,
                title="【115网盘订阅追更】开始执行",
                text="正在扫描订阅列表，搜索网盘资源并转存缺失内容..."
            )

        # 重置 API 调用计数器
        if self._p115_manager:
            self._p115_manager.reset_api_call_count()
        if self._pansou_client:
            self._pansou_client.reset_api_call_count()
        if self._nullbr_client:
            self._nullbr_client.reset_api_call_count()

        # 获取所有订阅
        with SessionFactory() as db:
            subscribes = SubscribeOper(db=db).list("N,R")

        if not subscribes:
            logger.info("没有订阅中的数据")
            if self._notify:
                self.post_message(
                    mtype=NotificationType.Plugin,
                    title="【115网盘订阅追更】执行完成",
                    text="当前没有订阅中的媒体数据。"
                )
            return

        # 分类订阅
        tv_subscribes = [s for s in subscribes if s.type == MediaType.TV.value]
        movie_subscribes = [s for s in subscribes if s.type == MediaType.MOVIE.value]

        if not tv_subscribes and not movie_subscribes:
            logger.info("没有电视剧或电影订阅")
            return

        logger.info(f"共有 {len(tv_subscribes)} 个电视剧订阅、{len(movie_subscribes)} 个电影订阅待处理")

        history: List[dict] = self.get_data('history') or []
        transferred_count = 0
        transfer_details: List[Dict[str, Any]] = []

        exclude_ids = set(self._exclude_subscribes) if self._exclude_subscribes else set()
        if exclude_ids:
            logger.info(f"排除订阅ID: {exclude_ids}")

        # 处理电影订阅
        for subscribe in movie_subscribes:
            if global_vars.is_system_stopped:
                break

            if subscribe.id in exclude_ids:
                logger.info(f"订阅 {subscribe.name} (ID:{subscribe.id}) 在排除列表中，跳过处理")
                continue

            transferred_count = self._sync_handler.process_movie_subscribe(
                subscribe=subscribe,
                history=history,
                transfer_details=transfer_details,
                transferred_count=transferred_count
            )

        # 处理电视剧订阅
        for subscribe in tv_subscribes:
            if global_vars.is_system_stopped:
                break

            if subscribe.id in exclude_ids:
                logger.info(f"订阅 {subscribe.name} (ID:{subscribe.id}) 在排除列表中，跳过处理")
                continue

            transferred_count = self._sync_handler.process_tv_subscribe(
                subscribe=subscribe,
                history=history,
                transfer_details=transfer_details,
                transferred_count=transferred_count,
                exclude_ids=exclude_ids
            )

        # 保存历史记录
        self.save_data('history', history[-500:])

        logger.info(f"115 网盘订阅追更完成，共转存 {transferred_count} 个文件")

        # 打印 API 调用统计
        api_stats = []
        if self._p115_manager:
            api_stats.append(f"115接口: {self._p115_manager.get_api_call_count()}次")
        if self._pansou_client:
            api_stats.append(f"PanSou接口: {self._pansou_client.get_api_call_count()}次")
        if self._nullbr_client:
            api_stats.append(f"Nullbr接口: {self._nullbr_client.get_api_call_count()}次")
        if api_stats:
            logger.info(f"本次同步 API 调用统计: {', '.join(api_stats)}")

        # 发送汇总通知
        if self._notify:
            if transferred_count > 0:
                self._sync_handler.send_transfer_notification(transfer_details, transferred_count)
            else:
                self.post_message(
                    mtype=NotificationType.Plugin,
                    title="【115网盘订阅追更】执行完成",
                    text=f"本次同步完成，共处理 {len(tv_subscribes)} 个电视剧订阅、{len(movie_subscribes)} 个电影订阅，未发现需要转存的新资源。"
                )

    def api_search(self, keyword: str, apikey: str) -> dict:
        """API: 搜索网盘资源"""
        return self._api_handler.search(keyword, apikey)

    def api_transfer(self, share_url: str, save_path: str, apikey: str) -> dict:
        """API: 转存分享链接"""
        return self._api_handler.transfer(share_url, save_path, apikey)

    def api_clear_history(self, apikey: str) -> dict:
        """API: 清空历史记录"""
        return self._api_handler.clear_history(apikey)

    def api_list_directories(self, path: str = "/", apikey: str = "") -> dict:
        """API: 列出115网盘指定路径下的目录"""
        return self._api_handler.list_directories(path, apikey)

    @eventmanager.register(EventType.PluginAction)
    def remote_sync(self, event: Event):
        """远程命令触发同步"""
        if not event:
            return
        event_data = event.event_data
        if not event_data or event_data.get("action") != "p115_sync":
            return

        logger.info("收到命令，开始执行 115 网盘订阅追更...")
        self.post_message(
            mtype=NotificationType.Plugin,
            channel=event_data.get("channel"),
            title="【115网盘订阅追更】开始执行",
            text="已收到远程命令，正在执行订阅追更任务...",
            userid=event_data.get("user")
        )

        self.sync_subscribes()

        self.post_message(
            mtype=NotificationType.Plugin,
            channel=event_data.get("channel"),
            title="【115网盘订阅追更】执行完成",
            text="远程触发的订阅追更任务已完成，详情请查看追更通知或历史记录。",
            userid=event_data.get("user")
        )
