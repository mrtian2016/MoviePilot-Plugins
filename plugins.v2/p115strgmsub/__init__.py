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
    plugin_version = "1.2.0"
    # 插件作者
    plugin_author = "frankk007"
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
    _toggle_scheduler: Optional[BackgroundScheduler] = None  # ✅新增：用于5min/2h的自动开关任务

    # 配置属性
    _enabled: bool = False
    _onlyonce: bool = False
    _cron: str = "30 */8 * * *"  # ✅ 默认：每8小时一次（30分）
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

    # ✅你图里“取消屏蔽时勾选的站点”
    # 你要改成别的站点，就改这三个名字
    _UNBLOCK_SITE_NAMES: List[str] = ["观众", "憨憨", "馒头"]
    # ✅恢复屏蔽时只勾选 115网盘
    _BLOCK_SITE_NAMES: List[str] = ["115网盘"]

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

    # ✅ 最小触发间隔（小时）
    _MIN_INTERVAL_HOURS: int = 8

    @staticmethod
    def _cron_interval_ge_min_hours(cron_expr: str, min_hours: int) -> bool:
        """
        校验 cron 的最小触发间隔是否 >= min_hours
        做法：用 APScheduler CronTrigger 推演未来多次触发时间，计算相邻间隔的最小值
        """
        cron_expr = (cron_expr or "").strip()
        if not cron_expr:
            return False

        try:
            tz = pytz.timezone(settings.TZ)
            trigger = CronTrigger.from_crontab(cron_expr, timezone=tz)
        except Exception:
            return False

        now = datetime.datetime.now(tz=pytz.timezone(settings.TZ))

        fire_times: List[datetime.datetime] = []
        prev = None
        current = now

        for _ in range(12):
            nxt = trigger.get_next_fire_time(prev, current)
            if not nxt:
                break
            fire_times.append(nxt)
            prev = nxt
            current = nxt + datetime.timedelta(seconds=1)

        if len(fire_times) < 2:
            return True

        min_delta = min(fire_times[i + 1] - fire_times[i] for i in range(len(fire_times) - 1))
        return min_delta >= datetime.timedelta(hours=min_hours)

    # ========= ✅新增：自动开关的核心方法 =========

    def _ensure_toggle_scheduler(self):
        if not self._toggle_scheduler:
            self._toggle_scheduler = BackgroundScheduler(timezone=settings.TZ)
            self._toggle_scheduler.start()

    def _cancel_toggle_jobs(self):
        if not self._toggle_scheduler:
            return
        for job_id in ["p115_unblock_job", "p115_reblock_job"]:
            try:
                self._toggle_scheduler.remove_job(job_id)
            except Exception:
                pass

    def _schedule_reblock_in_2h(self, base_time: datetime.datetime):
        """
        在 base_time 基础上 2小时后恢复屏蔽（只勾选 115网盘）
        """
        self._ensure_toggle_scheduler()
        tz = pytz.timezone(settings.TZ)
        base_time = base_time.astimezone(tz)
        run_date = base_time + datetime.timedelta(hours=2)

        self._toggle_scheduler.add_job(
            func=self._do_reblock,
            trigger="date",
            run_date=run_date,
            id="p115_reblock_job",
            replace_existing=True
        )
        logger.info(f"已安排：{run_date} 恢复屏蔽系统订阅（仅 115网盘）")

    def _schedule_unblock_in_5min_then_reblock(self, base_time: datetime.datetime):
        """
        base_time + 5分钟：取消屏蔽（勾选图中站点）
        再 +2小时：恢复屏蔽（仅115网盘）
        """
        self._ensure_toggle_scheduler()
        tz = pytz.timezone(settings.TZ)
        base_time = base_time.astimezone(tz)

        unblock_time = base_time + datetime.timedelta(minutes=5)

        self._toggle_scheduler.add_job(
            func=self._do_unblock,
            trigger="date",
            run_date=unblock_time,
            id="p115_unblock_job",
            replace_existing=True
        )
        logger.info(f"已安排：{unblock_time} 取消屏蔽系统订阅（勾选图中站点），并将在其后2小时恢复屏蔽")

        # reblock 在 unblock 真执行时再精确安排（保证“取消屏蔽后2小时”）
        # 所以这里不直接 schedule reblock，避免因任务耗时造成偏差

    def _do_unblock(self):
        """
        执行取消屏蔽：
        - 插件开关置为 False
        - 订阅 sites 勾选图中站点（观众/憨憨/馒头）
        - 并安排 2小时后恢复屏蔽（仅 115网盘）
        """
        try:
            self._init_subscribe_handler()
            # 1) 设置订阅站点（图中勾选）
            self._subscribe_handler.set_unblocked_sites(self._UNBLOCK_SITE_NAMES)

            # 2) 更新插件开关 + 写回配置
            self._block_system_subscribe = False
            self.__update_config()
            logger.info("已取消屏蔽系统订阅：block_system_subscribe=False")

            # 3) 取消屏蔽后 2小时恢复屏蔽
            tz = pytz.timezone(settings.TZ)
            now = datetime.datetime.now(tz=tz)
            self._schedule_reblock_in_2h(now)
        except Exception as e:
            logger.error(f"取消屏蔽执行失败：{e}")

    def _do_reblock(self):
        """
        执行恢复屏蔽：
        - 插件开关置为 True
        - 订阅 sites 只勾选 115网盘
        """
        try:
            self._init_subscribe_handler()
            self._subscribe_handler.set_blocked_sites_only_115()

            self._block_system_subscribe = True
            self.__update_config()
            logger.info("已恢复屏蔽系统订阅：block_system_subscribe=True（仅115网盘）")
        except Exception as e:
            logger.error(f"恢复屏蔽执行失败：{e}")

    def _is_last_run_today(self, run_start: datetime.datetime) -> bool:
        """
        判断本次任务是否为“当天最后一次触发”
        用 cron 推算下一次触发时间，若跨天则本次为最后一次
        """
        try:
            tz = pytz.timezone(settings.TZ)
            run_start = run_start.astimezone(tz)

            trigger = CronTrigger.from_crontab(self._cron, timezone=tz)
            nxt = trigger.get_next_fire_time(None, run_start + datetime.timedelta(seconds=1))
            if not nxt:
                return False
            return nxt.date() != run_start.date()
        except Exception as e:
            logger.warning(f"判断是否当天最后一次触发失败：{e}，按 20:35 兜底")
            return run_start.hour == 20 and run_start.minute == 35

    # ========= 原逻辑：HDHive cookie =========

    def _check_and_refresh_hdhive_cookie(self) -> Optional[str]:
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
                logger.info(
                    f"HDHive: 新 Cookie 信息 - 用户ID: {token_info['user_id']}, "
                    f"过期时间: {token_info['exp_time'].strftime('%Y-%m-%d %H:%M:%S')}, "
                    f"有效时间: {token_info['time_left'] / 3600:.1f} 小时"
                )

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
        self._ensure_toggle_scheduler()

        download_so_file(Path(__file__).parent / "lib")

        if config:
            self._enabled = config.get("enabled", False)

            self._cron = (config.get("cron", self._cron) or "").strip()

            if self._cron:
                ok = self._cron_interval_ge_min_hours(self._cron, self._MIN_INTERVAL_HOURS)
                if not ok:
                    logger.warning(
                        f"Cron 过于频繁（要求间隔>= {self._MIN_INTERVAL_HOURS}h）：{self._cron}，已回退默认 30 */8 * * *"
                    )
                    self._cron = "30 */8 * * *"

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

            # ✅捕捉“插件面板手动关闭屏蔽系统订阅”
            new_block_state = config.get("block_system_subscribe", False)
            old_block_state = self._block_system_subscribe
            self._block_system_subscribe = new_block_state

            if new_block_state != old_block_state:
                self._init_subscribe_handler()

                if new_block_state is False:
                    # 手动关闭屏蔽：立刻勾选图中订阅站点，并在2小时后恢复屏蔽（仅115）
                    logger.info("检测到面板手动关闭“屏蔽系统订阅”：立即勾选图中站点，并安排2小时后恢复屏蔽")
                    self._cancel_toggle_jobs()
                    self._subscribe_handler.set_unblocked_sites(self._UNBLOCK_SITE_NAMES)

                    tz = pytz.timezone(settings.TZ)
                    now = datetime.datetime.now(tz=tz)
                    self._schedule_reblock_in_2h(now)
                else:
                    # 手动开启屏蔽：立即只勾选115，并取消窗口任务
                    logger.info("检测到面板手动开启“屏蔽系统订阅”：立即只勾选115，并取消窗口任务")
                    self._cancel_toggle_jobs()
                    self._subscribe_handler.set_blocked_sites_only_115()

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
        """注册插件公共服务（强制最小间隔>=8小时）"""
        if self._enabled and self._cron:
            if not self._cron_interval_ge_min_hours(self._cron, self._MIN_INTERVAL_HOURS):
                logger.warning(
                    f"Cron 过于频繁（要求间隔>= {self._MIN_INTERVAL_HOURS}h）：{self._cron}，将回退 interval=8h"
                )
            else:
                try:
                    return [{
                        "id": "P115StrgmSub",
                        "name": "115网盘订阅追更服务",
                        "trigger": CronTrigger.from_crontab(self._cron),
                        "func": self.sync_subscribes,
                        "kwargs": {}
                    }]
                except Exception as e:
                    logger.warning(f"Cron 表达式无效：{self._cron}，将回退 interval=8h。错误：{e}")

        if self._enabled:
            return [{
                "id": "P115StrgmSub",
                "name": "115网盘订阅追更服务",
                "trigger": "interval",
                "func": self.sync_subscribes,
                "kwargs": {"hours": 8}
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
            "cron": self._cron,
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

        try:
            if self._toggle_scheduler:
                self._toggle_scheduler.remove_all_jobs()
                if self._toggle_scheduler.running:
                    self._toggle_scheduler.shutdown()
                self._toggle_scheduler = None
        except Exception as e:
            logger.error(f"退出开关调度器失败：{str(e)}")

    def sync_subscribes(self):
        """同步订阅，搜索并转存缺失剧集"""
        with lock:
            tz = pytz.timezone(settings.TZ)
            run_start = datetime.datetime.now(tz=tz)

            success = False
            try:
                success = self._do_sync()  # ✅严格 success：_do_sync 返回 True 才算成功
            except Exception as e:
                logger.error(f"同步任务异常：{e}")
                success = False
            finally:
                run_end = datetime.datetime.now(tz=tz)

                # 触发条件1：每天最后一次任务成功跑完 -> 5分钟后取消屏蔽
                if success and self._is_last_run_today(run_start):
                    logger.info("检测到当天最后一次任务成功：安排5分钟后取消屏蔽，并在取消后2小时恢复屏蔽")
                    self._cancel_toggle_jobs()
                    self._schedule_unblock_in_5min_then_reblock(run_end)

    def _do_sync(self) -> bool:
        """执行同步。返回 True 表示成功跑完；返回 False 表示失败/提前退出。"""

        # 检查至少有一个搜索客户端可用
        if not self._pansou_enabled and not self._nullbr_enabled and not self._hdhive_enabled:
            logger.error("PanSou、Nullbr 和 HDHive 搜索源均未启用，请至少启用一个搜索源")
            if self._notify:
                self.post_message(
                    mtype=NotificationType.Plugin,
                    title="【115网盘订阅追更】配置错误",
                    text="PanSou、Nullbr 和 HDHive 搜索源均未启用，请至少启用一个搜索源。"
                )
            return False

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
            return False

        if not self._p115_manager:
            logger.error("115 客户端未初始化，请检查 Cookie 配置")
            return False

        # 验证 115 登录状态
        if not self._p115_manager.check_login():
            logger.error("115 登录失败，Cookie 可能已过期")
            if self._notify:
                self.post_message(
                    mtype=NotificationType.Manual,
                    title="【115网盘订阅追更】登录失败",
                    text="115 Cookie 可能已过期，请更新配置后重试。"
                )
            return False

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
            return True  # 没有订阅也算“成功跑完”

        # 分类订阅
        tv_subscribes = [s for s in subscribes if s.type == MediaType.TV.value]
        movie_subscribes = [s for s in subscribes if s.type == MediaType.MOVIE.value]

        if not tv_subscribes and not movie_subscribes:
            logger.info("没有电视剧或电影订阅")
            return True

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

        return True

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

