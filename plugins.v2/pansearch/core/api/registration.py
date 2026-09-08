""" 页面模式、API 路由、命令与定时服务注册。"""

import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple, Type

import pytz
from app.core.config import settings
from app.core.security import verify_resource_token
from app.log import logger
from app.schemas.types import EventType
from app.utils.timer import TimerUtils
from apscheduler.triggers.combining import OrTrigger
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from fastapi import Depends

from .form_content import FormContent
from .. import OwnerDelegator
from ..agent import (
    PanSearchCacheClearTool,
    PanSearchCheckinTool,
    PanSearchCheckinHistoryTool,
    PanSearchConfigUpdateTool,
    PanSearchLinksTool,
    PanSearchPerformanceTool,
    PanSearchResourceSearchTool,
    PanSearchResourceSelectTool,
    PanSearchStatusTool,
    PanSearchSyncTool,
)
from ..config import UIConfig


class _WorkflowActionHandler(dict):
    """兼顾动作执行与 FastAPI 动作列表序列化。"""

    def __init__(self, func: Callable[..., Tuple[bool, Any]]):
        super().__init__()
        self._func = func

    def __bool__(self) -> bool:
        return True

    def __call__(self, *args, **kwargs) -> Tuple[bool, Any]:
        return self._func(*args, **kwargs)


class MoviePilotRegistration(OwnerDelegator):
    """声明插件对暴露的注册信息。"""

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_render_mode() -> Tuple[str, str]:
        """使用 Vue 模块联邦渲染配置页与详情页。"""
        return "vue", "dist/assets"

    def get_form(self) -> Tuple[Optional[List[dict]], Dict[str, Any]]:
        return FormContent.form(self), UIConfig.get_default_config()

    def get_page(self) -> Optional[List[dict]]:
        return FormContent.page(self)

    def get_api(self) -> List[Dict[str, Any]]:
        return [
            {
                "path": "/auto_subscribe/run",
                "endpoint": self.api_run_auto_subscribe,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "立即执行榜单自动订阅",
            },
            {
                "path": "/auto_subscribe/test",
                "endpoint": self.api_test_auto_subscribe,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "测试榜单来源",
            },
            {
                "path": "/auto_subscribe/proxy/test",
                "endpoint": self.api_test_auto_subscribe_proxy,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "测试榜单代理",
            },
            {
                "path": "/overview",
                "endpoint": self.api_platform_overview,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "获取仪表盘与侧栏页面聚合状态",
            },
            {
                "path": "/page_data",
                "endpoint": self.api_vue_page_data,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "获取历史记录分页数据",
            },
            {
                "path": "/history/summary",
                "endpoint": self.api_vue_history_summary,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "获取历史记录聚合摘要",
            },
            {
                "path": "/ui_options",
                "endpoint": self.api_vue_ui_options,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "获取指定配置作用域选项",
            },
            {
                "path": "/account/refresh",
                "endpoint": self.api_vue_refresh_account,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "手动刷新单个账户信息卡片",
            },
            {
                "path": "/hdhive/oauth/start",
                "endpoint": self.api_vue_hdhive_oauth_start,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "生成HDHive OpenAPI用户授权链接",
            },
            {
                "path": "/hdhive/oauth/exchange",
                "endpoint": self.api_vue_hdhive_oauth_exchange,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "校验HDHive OAuth回调并换取用户Token",
            },
            {
                "path": "/checkin/overview",
                "endpoint": self.api_vue_checkin_overview,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "获取多渠道签到仪表盘",
            },
            {
                "path": "/checkin/{provider}",
                "endpoint": self.api_vue_checkin,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "立即执行提供方签到",
            },
            {
                "path": "/checkin/{provider}/history",
                "endpoint": self.api_vue_checkin_history,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "获取提供方签到历史",
            },
            {
                "path": "/cloud/directories",
                "endpoint": self.api_vue_cloud_directories,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "浏览当前网盘目录",
            },
            {
                "path": "/cloud/directories/create",
                "endpoint": self.api_vue_create_cloud_directory,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "创建网盘目录",
            },
            {
                "path": "/search/tmdb",
                "endpoint": self.api_vue_search_tmdb_candidates,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "按标题查询 TMDB 媒体候选",
            },
            {
                "path": "/search/test",
                "endpoint": self.api_vue_test_search_source,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "使用页面输入测试单个搜索渠道",
            },
            {
                "path": "/search/proxy/test",
                "endpoint": self.api_vue_test_search_proxy,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "通过Cloudflare Trace测试搜索代理",
            },
            {
                "path": "/search/preview",
                "endpoint": self.api_vue_preview_search_resource,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "只读预览搜索资源文件列表",
            },
            {
                "path": "/search/unlock",
                "endpoint": self.api_vue_unlock_search_resource,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "确认后解锁测试资源",
            },
            {
                "path": "/resource/recommend",
                "endpoint": self.api_vue_resource_recommend,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "获取平台推荐榜单媒体列表",
            },
            {
                "path": "/resource/detail",
                "endpoint": self.api_vue_resource_detail,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "获取资源详情媒体身份",
            },

            {
                "path": "/resource/search_resources",
                "endpoint": self.api_vue_resource_search_resources,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "后台并发搜索指定媒体的网盘及磁力资源",
            },
            {
                "path": "/config/save",
                "endpoint": self.api_vue_save_config,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "保存Vue配置页配置",
            },
            {
                "path": "/sync/start",
                "endpoint": self.api_vue_start_sync,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "启动全部或所选订阅搜索",
            },
            {
                "path": "/sync/manual",
                "endpoint": self.api_vue_start_manual_sync,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "按指定订阅手动添加资源链接",
            },
            {
                "path": "/sync/manual/resolve",
                "endpoint": self.api_vue_resolve_manual_links,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "识别手动资源对应的订阅或 TMDB 媒体",
            },
            {
                "path": "/sync/stop",
                "endpoint": self.api_vue_stop_sync,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "停止订阅追更",
            },
            {
                "path": "/sync/task/stop",
                "endpoint": self.api_vue_stop_sync_task,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "停止单个订阅任务",
            },
            {
                "path": "/runtime",
                "endpoint": self.api_vue_runtime_status,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "查看运行状态",
            },
            {
                "path": "/runtime/stream",
                "endpoint": self.api_vue_runtime_stream,
                "methods": ["GET"],
                "allow_anonymous": True,
                "dependencies": [Depends(verify_resource_token)],
                "summary": "实时推送运行状态",
            },
            {
                "path": "/offline",
                "endpoint": self.api_vue_offline_tasks,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "查看当前网盘离线任务",
            },
            {
                "path": "/offline/refresh",
                "endpoint": self.api_vue_refresh_offline_tasks,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "刷新当前网盘离线任务",
            },
            {
                "path": "/offline/delete",
                "endpoint": self.api_vue_delete_offline_task,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "删除当前网盘离线任务",
            },
            {
                "path": "/offline/delete_batch",
                "endpoint": self.api_vue_delete_offline_tasks,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "批量删除当前网盘离线任务",
            },
            {
                "path": "/offline/retry",
                "endpoint": self.api_vue_retry_offline_tasks,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "重试当前网盘离线下载或文件后处理任务",
            },
            {
                "path": "/history/clear",
                "endpoint": self.api_vue_clear_history,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "清空历史记录",
            },
            {
                "path": "/history/delete",
                "endpoint": self.api_vue_delete_history,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "删除单条终态历史记录",
            },
            {
                "path": "/history/delete_batch",
                "endpoint": self.api_vue_delete_history_batch,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "批量删除终态历史记录",
            },
            {
                "path": "/history/notify",
                "endpoint": self.api_vue_notify_history,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "手动补发单条成功记录通知",
            },
            {
                "path": "/history/upgrade",
                "endpoint": self.api_vue_upgrade_history,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "从历史记录或媒体服务器内容发起洗版",
            },
            {
                "path": "/media/servers",
                "endpoint": self.api_vue_media_servers,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "获取可用媒体服务器选项",
            },
            {
                "path": "/media/content",
                "endpoint": self.api_vue_media_server_content,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "搜索路径符合插件配置的媒体服务器内容",
            },
            {
                "path": "/history/play/{item_id}",
                "endpoint": self.api_vue_emby_play,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "获取历史媒体的Emby播放地址",
            },
            {
                "path": "/history/retry",
                "endpoint": self.api_vue_retry_history,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "重试单条失败转存记录",
            },
            {
                "path": "/cache/clear",
                "endpoint": self.api_vue_clear_cache,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "清空搜索和分享缓存",
            },
            {
                "path": "/qrcode",
                "endpoint": self.api_vue_get_qrcode,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "获取网盘登录二维码",
            },
            {
                "path": "/qrcode/check",
                "endpoint": self.api_vue_check_qrcode,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "检查网盘扫码登录状态",
            },
            {
                "path": "/qrcode/check",
                "endpoint": self.api_vue_check_qrcode_post,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "检查网盘扫码登录状态（POST）",
            },
            {
                "path": "/batch_re_score",
                "endpoint": self.api_batch_re_score,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "整理记录评分：对已选订阅已有strm批量评分写入episode_priority",
            },
            {
                "path": "/force_re_score",
                "endpoint": self.api_force_re_score,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "强制重评分：清空episode_priority缓存，重新扫描磁盘strm文件评分（覆盖旧评分）",
            }
        ]

    def get_dashboard_meta(self) -> List[Dict[str, str]]:
        if not self._enabled:
            return []
        return [
            {"key": "overview", "name": "网盘订阅助手"},
            {"key": "checkin", "name": "签到概览"},
        ]

    def get_dashboard(
            self, key: str = "overview", **kwargs
    ) -> Optional[Tuple[Dict[str, Any], Dict[str, Any], None]]:
        if not self._enabled:
            return None
        dashboards = {
            "overview": {
                "title": "网盘订阅助手",
                "subtitle": "订阅任务与转存概览",
                "dashboard": "overview",
            },
            "checkin": {
                "title": "签到概览",
                "subtitle": "多渠道每日签到与积分",
                "dashboard": "checkin",
            },
        }
        attrs = dashboards.get(key)
        if attrs is None:
            return None
        return (
            {"cols": 12, "sm": 6, "md": 4},
            {**attrs, "refresh": 30, "border": True},
            None,
        )

    def get_sidebar_nav(self) -> List[Dict[str, Any]]:
        if not self._enabled or not self._show_sidebar_nav:
            return []
        return [
            {
                "nav_key": "main",
                "title": "网盘订阅",
                "icon": "mdi-cloud-sync-outline",
                "section": "subscribe",
                "permission": "subscribe",
                "order": 30,
            },
            {
                "nav_key": "resource",
                "title": "网盘资源",
                "icon": "mdi-cloud-search-outline",
                "section": "discovery",
                "permission": "discovery",
                "order": 31,
            },
        ]

    def get_actions(self) -> List[Dict[str, Any]]:
        return [
            {
                "id": "pansearch_start_sync",
                "action_id": "pansearch_start_sync",
                "name": "执行网盘订阅搜索",
                "func": _WorkflowActionHandler(self.workflow_start_sync),
                "kwargs": {},
            },
            {
                "id": "pansearch_process_links",
                "action_id": "pansearch_process_links",
                "name": "处理网盘资源链接",
                "func": _WorkflowActionHandler(self.workflow_process_links),
                "kwargs": {},
            },
        ]

    def get_agent_tools(self) -> List[Type]:
        if not self._enabled or not self._agent_enabled:
            return []
        return [
            PanSearchStatusTool,
            PanSearchSyncTool,
            PanSearchCheckinTool,
            PanSearchCheckinHistoryTool,
            PanSearchLinksTool,
            PanSearchResourceSearchTool,
            PanSearchResourceSelectTool,
            PanSearchCacheClearTool,
            PanSearchConfigUpdateTool,
            PanSearchPerformanceTool,
        ]

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """定义通用远程控制命令。"""
        return [
            {
                "cmd": "/cloud_sync",
                "event": EventType.PluginAction,
                "desc": "执行网盘订阅搜索",
                "category": "网盘订阅",
                "data": {"action": "pansearch_sync"},
            },
            {
                "cmd": "/cloud_auto_subscribe",
                "event": EventType.PluginAction,
                "desc": "立即执行榜单自动订阅",
                "category": "网盘订阅",
                "data": {"action": "pansearch_auto_subscribe"},
            },
            {
                "cmd": "/cloud_status",
                "event": EventType.PluginAction,
                "desc": "查看网盘订阅状态",
                "category": "网盘订阅",
                "data": {"action": "pansearch_status"},
            },
            {
                "cmd": "/cloud_link",
                "event": EventType.PluginAction,
                "desc": "提交网盘链接：订阅ID或媒体名称 链接",
                "category": "网盘订阅",
                "data": {"action": "pansearch_links"},
            },
            {
                "cmd": "/cloud_checkin",
                "event": EventType.PluginAction,
                "desc": "立即执行搜索渠道签到",
                "category": "网盘订阅",
                "data": {"action": "pansearch_checkin"},
            },
            {
                "cmd": "/cloud_checkin_history",
                "event": EventType.PluginAction,
                "desc": "列举签到详情：渠道 数量",
                "category": "网盘订阅",
                "data": {"action": "pansearch_checkin_history"},
            },
            {
                "cmd": "/cloud_cache_clear",
                "event": EventType.PluginAction,
                "desc": "清理网盘订阅缓存",
                "category": "网盘订阅",
                "data": {"action": "pansearch_cache_clear"},
            },
        ]

    def get_service(self) -> List[Dict[str, Any]]:
        if not self._enabled:
            return []

        services = []

        if (
                bool(getattr(self, "_auto_subscribe_enabled", False))
                and self._cron_is_valid(getattr(self, "_auto_subscribe_cron", ""))
        ):
            services.append({
                "id": "PanSearch_AutoSubscribe",
                "name": "榜单自动订阅服务",
                "trigger": CronTrigger.from_crontab(self._auto_subscribe_cron),
                "func": self.run_auto_subscribe,
                "kwargs": {},
            })

        if self._cron and self._cron_is_valid(self._cron):
            try:
                services.append({
                    "id": "PanSearch",
                    "name": "网盘订阅助手服务",
                    "trigger": CronTrigger.from_crontab(self._cron),
                    "func": self.sync_subscribes,
                    "kwargs": {}
                })
            except Exception as e:
                logger.warning(f"Cron 表达式无效：{self._cron}，将回退默认 0 18-23 * * *。错误：{e}")
                services.append({
                    "id": "PanSearch",
                    "name": "网盘订阅助手服务",
                    "trigger": CronTrigger.from_crontab("0 18-23 * * *"),
                    "func": self.sync_subscribes,
                    "kwargs": {}
                })
        else:
            services.append({
                "id": "PanSearch",
                "name": "网盘订阅助手服务",
                "trigger": CronTrigger.from_crontab("0 18-23 * * *"),
                "func": self.sync_subscribes,
                "kwargs": {}
            })

        services.append({
            "id": "PanSearch_TakeoverInstall",
            "name": "网盘订阅接管初始化",
            "trigger": DateTrigger(
                run_date=(
                    datetime.datetime.now(tz=pytz.timezone(settings.TZ))
                    + datetime.timedelta(seconds=3)
                )
            ),
            "func": self._install_subscribe_search_takeover,
            "kwargs": {}
        })

        checkin_providers = [
            provider
            for provider in self.get_checkin_provider_specs()
            if (
                    bool(getattr(
                        self, f"_{provider['key']}_checkin_enabled", False
                    ))
                    and all(
                getattr(self, name, None)
                for name in provider["credential_attrs"]
            )
            )
        ]
        if checkin_providers and self._cron_is_valid(self._checkin_cron):
            timezone = pytz.timezone(settings.TZ)
            checkin_trigger = CronTrigger.from_crontab(
                self._checkin_cron,
                timezone=timezone,
            )
            if self._checkin_auto_retry:
                retry_times = TimerUtils.random_even_scheduler(
                    num_executions=self._configured_retry_count(),
                    begin_hour=self._RETRY_START_HOUR,
                    end_hour=self._RETRY_END_HOUR,
                )
                checkin_trigger = OrTrigger(
                    [checkin_trigger] + [
                        CronTrigger(
                            minute=retry_time.minute,
                            hour=retry_time.hour,
                            timezone=timezone,
                        )
                        for retry_time in retry_times
                    ]
                )
            services.append({
                "id": "PanSearch_Checkin",
                "name": "签到服务",
                "trigger": checkin_trigger,
                "func": self.run_scheduled_checkins,
                "kwargs": {},
            })

        return services

    def _refresh_platform_services(self) -> None:
        """配置保存后刷新当前插件在中注册的定时服务。"""
        try:
            from app.scheduler import Scheduler

            scheduler = Scheduler.get_existing_instance()
            if scheduler is None:
                return
            scheduler.update_plugin_job(self._owner.__class__.__name__)
        except Exception as error:
            logger.warning(f"刷新插件服务失败：{error}")
