"""
115网盘订阅追更插件
结合MoviePilot订阅功能，自动搜索115网盘资源并转存缺失剧集
"""
import re
import datetime
from pathlib import Path
from threading import Lock
from typing import Optional, Any, List, Dict, Tuple
from pydantic import BaseModel

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.core.config import settings, global_vars
from app.core.event import Event, eventmanager
from app.core.metainfo import MetaInfo
from app.chain.download import DownloadChain
from app.chain.subscribe import SubscribeChain
from app.db import SessionFactory
from app.db.subscribe_oper import SubscribeOper
from app.db.downloadhistory_oper import DownloadHistoryOper
from app.db.models.site import Site
from app.log import logger
from app.plugins import _PluginBase
from app.schemas import MediaInfo
from app.schemas.types import EventType, MediaType, NotificationType
from app.utils.string import StringUtils

from .pansou import PanSouClient
from .p115client import P115ClientManager
from .nullbr import NullbrClient
from .ui_config import UIConfig
from .file_matcher import FileMatcher, SubscribeFilter

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
    plugin_version = "1.0.9"
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
    _cron: str = "30 */8 * * *"
    _notify: bool = False
    _cookies: str = ""
    _pansou_enabled: bool = True  # 是否启用 PanSou 搜索
    _pansou_url: str = "https://so.252035.xyz"
    _pansou_username: str = ""
    _pansou_password: str = ""
    _pansou_auth_enabled: bool = False
    _pansou_channels: str = "QukanMovie"  # TG搜索频道列表,用逗号分隔
    _save_path: str = "/我的接收/MoviePilot/TV"  # 电视剧转存目录
    _movie_save_path: str = "/我的接收/MoviePilot/Movie"  # 电影转存目录
    _only_115: bool = True  # 只搜索115网盘资源
    _exclude_subscribes: List[int] = []  # 排除的订阅ID列表
    _nullbr_enabled: bool = False  # 是否启用 Nullbr 查询
    _nullbr_appid: str = ""  # Nullbr APP ID（新字段名，避免加载旧配置）
    _nullbr_api_key: str = ""  # Nullbr API Key
    _nullbr_priority: bool = True  # Nullbr 优先（True: 优先使用 Nullbr，False: 优先使用 PanSou）
    _hdhive_enabled: bool = False  # 是否启用 HDHive 查询
    _hdhive_username: str = ""  # HDHive 用户名
    _hdhive_password: str = ""  # HDHive 密码
    _hdhive_cookie: str = ""  # HDHive Cookie
    _block_system_subscribe: bool = False  # 是否屏蔽系统订阅
    _max_transfer_per_sync: int = 50  # 单次同步最大转存数量，防止风控
    _batch_size: int = 20  # 批量转存每批文件数
    _skip_other_season_dirs: bool = True  # 跳过其他季目录以减少API调用
    # 运行时对象
    _pansou_client: Optional[PanSouClient] = None
    _p115_manager: Optional[P115ClientManager] = None
    _nullbr_client: Optional[NullbrClient] = None
    _hdhive_client: Optional[Any] = None  # HDHive 客户端

    def _download_so_file(self):
        """
        下载 hdhive .so 文件到 lib 目录
        
        从 GitHub 下载编译好的 .so 文件，用于 HDHive 功能
        """
        import platform
        import urllib.request
        import urllib.error
        
        # 确定目标目录
        lib_dir = Path(__file__).parent / "lib"
        lib_dir.mkdir(parents=True, exist_ok=True)
        
        # 确定系统架构和平台
        machine = platform.machine().lower()
        system = platform.system().lower()
        
        # 映射常见的架构名称
        arch_map = {
            "x86_64": "x86_64",
            "amd64": "x86_64",
            "aarch64": "aarch64",
            "arm64": "aarch64",
        }
        arch = arch_map.get(machine, machine)
        
        # 映射平台名称
        platform_map = {
            "linux": "linux-gnu",
            "darwin": "darwin",
        }
        plat = platform_map.get(system, system)
        
        # 构建文件名（Linux: hdhive.cpython-312-x86_64-linux-gnu.so）
        # 目前只支持 Linux x86_64
        so_filename = f"hdhive.cpython-312-{arch}-{plat}.so"
        target_path = lib_dir / so_filename
        
        # 如果文件已存在，跳过下载
        if target_path.exists():
            logger.debug(f"hdhive .so 文件已存在: {target_path}")
            return
        
        # GitHub 原始文件 URL
        
        base_url = "https://ghfast.top/https://raw.githubusercontent.com/mrtian2016/hdhive_resource/main/"
        download_url = f"{base_url}/{so_filename}"
        
        logger.info(f"开始下载 hdhive .so 文件: {download_url}")
        
        try:
            # 下载文件
            with urllib.request.urlopen(download_url, timeout=120) as response:
                content = response.read()
            
            # 保存到本地
            with open(target_path, "wb") as f:
                f.write(content)
            
            # 设置可执行权限
            import os
            os.chmod(target_path, 0o755)
            
            logger.info(f"✓ hdhive .so 文件下载成功: {target_path}")
            
        except urllib.error.HTTPError as e:
            if e.code == 404:
                logger.warning(f"⚠️ hdhive .so 文件不存在（当前平台: {system}/{arch}），HDHive 功能可能无法使用")
            else:
                logger.error(f"下载 hdhive .so 文件失败 (HTTP {e.code}): {e}")
        except urllib.error.URLError as e:
            logger.error(f"下载 hdhive .so 文件失败（网络错误）: {e}")
        except Exception as e:
            logger.error(f"下载 hdhive .so 文件失败: {e}")

    def init_plugin(self, config: dict = None):
        """初始化插件"""
        # 停止现有任务
        self.stop_service()

        # 下载.so文件
        self._download_so_file()

        # 加载配置
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
            self._nullbr_priority = config.get("nullbr_priority", True)
            self._hdhive_enabled = config.get("hdhive_enabled", False)
            self._hdhive_username = config.get("hdhive_username", "")
            self._hdhive_password = config.get("hdhive_password", "")
            self._hdhive_cookie = config.get("hdhive_cookie", "")
            self._max_transfer_per_sync = int(config.get("max_transfer_per_sync", 50) or 50)
            self._batch_size = int(config.get("batch_size", 20) or 20)
            self._skip_other_season_dirs = config.get("skip_other_season_dirs", True)

            # 处理屏蔽系统订阅开关
            new_block_state = config.get("block_system_subscribe", False)
            old_block_state = self._block_system_subscribe
            self._block_system_subscribe = new_block_state
            
            # 开关状态变化时，更新所有订阅的sites字段
            if new_block_state != old_block_state:
                self._update_subscribe_sites(new_block_state)

        # 初始化客户端
        self._init_clients()

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
        # 初始化 PanSou 客户端
        if self._pansou_enabled and self._pansou_url:
            self._pansou_client = PanSouClient(
                base_url=self._pansou_url,
                username=self._pansou_username,
                password=self._pansou_password,
                auth_enabled=self._pansou_auth_enabled
            )

        # 初始化 Nullbr 客户端
        if self._nullbr_enabled:
            if not self._nullbr_appid or not self._nullbr_api_key:
                missing = []
                if not self._nullbr_appid:
                    missing.append("APP ID")
                if not self._nullbr_api_key:
                    missing.append("API Key")
                logger.warning(f"⚠️ Nullbr 已启用但缺少必要配置：{', '.join(missing)}，将无法使用 Nullbr 查询功能")
                self._nullbr_client = None
            else:
                self._nullbr_client = NullbrClient(app_id=self._nullbr_appid, api_key=self._nullbr_api_key)
                logger.info("✓ Nullbr 客户端初始化成功")

        # 初始化 HDHive 客户端
        if self._hdhive_enabled:
            if not self._hdhive_cookie and not (self._hdhive_username and self._hdhive_password):
                logger.warning("⚠️ HDHive 已启用但缺少必要配置（Cookie 或 用户名/密码），将无法使用 HDHive 查询功能")
                self._hdhive_client = None
            elif self._hdhive_cookie:
                # 优先使用 Cookie 初始化同步客户端
                try:
                    import os
                    from .lib.hdhive import create_client as create_hdhive_client
                    proxy_host = os.environ.get("PROXY_HOST")
                    proxy = {"http": proxy_host, "https": proxy_host} if proxy_host else None
                    self._hdhive_client = create_hdhive_client(cookie=self._hdhive_cookie, proxy=proxy)
                    logger.info("✓ HDHive 客户端初始化成功（Cookie 模式）")
                except Exception as e:
                    logger.warning(f"⚠️ HDHive 客户端初始化失败：{e}")
                    self._hdhive_client = None
            else:
                # 没有 Cookie，仅记录用户名密码，用于异步回退
                logger.info("✓ HDHive 配置已加载（用户名/密码模式，将使用异步客户端）")
                self._hdhive_client = None  # 异步客户端在搜索时动态创建

         # 初始化 115 客户端
        if self._cookies:
            self._p115_manager = P115ClientManager(cookies=self._cookies)

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
        # return []
        return [
            {
                "path": "/search",
                "endpoint": self.api_search,
                "methods": ["GET"],
                "summary": "搜索网盘资源"
            },
            {
                "path": "/transfer",
                "endpoint": self.api_transfer,
                "methods": ["POST"],
                "summary": "转存分享链接"
            },
            {
                "path": "/clear_history",
                "endpoint": self.api_clear_history,
                "methods": ["POST"],
                "summary": "清空历史记录"
            },
            {
                "path": "/directories",
                "endpoint": self.api_list_directories,
                "methods": ["GET"],
                "summary": "列出115网盘目录"
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
            "cron": self._cron,
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
            "nullbr_priority": self._nullbr_priority,
            "hdhive_enabled": self._hdhive_enabled,
            "hdhive_username": self._hdhive_username,
            "hdhive_password": self._hdhive_password,
            "hdhive_cookie": self._hdhive_cookie,
            "exclude_subscribes": self._exclude_subscribes,
            "block_system_subscribe": self._block_system_subscribe,
            "max_transfer_per_sync": self._max_transfer_per_sync,
            "batch_size": self._batch_size,
            "skip_other_season_dirs": self._skip_other_season_dirs
        })

    def _update_subscribe_sites(self, block: bool):
        """
        屏蔽/恢复系统订阅
        
        :param block: True 表示屏蔽（添加id=-1的115网盘站点，并更新订阅sites为[-1]），
                      False 表示恢复（删除该记录，并恢复订阅sites为[]）
        """
        try:
            from sqlalchemy import text
            
            sites_value = [-1] if block else []
            action = "屏蔽" if block else "恢复"
            
            with SessionFactory() as db:
                # 1. 更新所有订阅的sites字段
                subscribes = SubscribeOper(db=db).list()
                updated_count = 0
                excluded_count = 0
                if subscribes:
                    for subscribe in subscribes:
                        if subscribe.id in self._exclude_subscribes:
                            excluded_count += 1
                            continue
                        SubscribeOper(db=db).update(subscribe.id, {"sites": sites_value})
                        updated_count += 1
                    if excluded_count:
                        logger.info(f"跳过 {excluded_count} 个排除订阅")
                    logger.info(f"系统订阅{action}完成，已更新 {updated_count} 个订阅的sites字段为 {sites_value}")
                
                # 2. 添加或删除id=-1的站点记录
                if block:
                    # 开启屏蔽：添加id=-1的站点记录
                    existing = Site.get(db, -1)
                    if not existing:
                        # 使用原生SQL插入，因为需要指定id为-1
                        db.execute(
                            text(
                                "INSERT INTO site (id, name, url, is_active, limit_interval, limit_count, limit_seconds, timeout) VALUES (:id, :name, :url, :is_active, :limit_interval ,:limit_count, :limit_seconds, :timeout)"
                            ),
                            {"id": -1, "name": "115网盘", "url": "https://115.com", "is_active": True,"limit_interval":10000000, "limit_count": 1, "limit_seconds": 10000000, "timeout": 1}
                        )
                        db.commit()
                        logger.info("已添加屏蔽站点记录 (id=-1, name=115网盘, is_active=True)")
                    else:
                        logger.info("屏蔽站点记录已存在，跳过添加")
                else:
                    # 关闭屏蔽：删除id=-1的站点记录
                    existing = Site.get(db, -1)
                    if existing:
                        Site.delete(db, -1)
                        logger.info("已删除屏蔽站点记录 (id=-1)")
                    else:
                        logger.info("屏蔽站点记录不存在，跳过删除")
                        
        except Exception as e:
            logger.error(f"更新屏蔽站点记录失败: {e}")

    def _convert_nullbr_to_pansou_format(self, nullbr_resources: List[Dict]) -> List[Dict]:
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

    def _convert_hdhive_to_pansou_format(self, hdhive_resources: List[Any]) -> List[Dict]:
        """
        将 HDHive 资源格式转换为统一的资源格式
        
        HDHive ResourceInfo: title, share_url, share_size, website, is_free, unlock_points 等
        统一格式: {"url": "...", "title": "...", "update_time": ""}
        
        :param hdhive_resources: HDHive 返回的资源列表
        :return: 统一格式的资源列表
        """
        converted = []
        for resource in hdhive_resources:
            # HDHive 资源可能是对象或字典
            if hasattr(resource, 'url'):
                url = resource.url or ""
            elif isinstance(resource, dict):
                url = resource.get("url", "") or resource.get("share_url", "")
            else:
                url = ""
            
            if hasattr(resource, 'title'):
                title = resource.title or ""
            elif isinstance(resource, dict):
                title = resource.get("title", "")
            else:
                title = ""
            
            if url:  # 只添加有 URL 的资源
                converted.append({
                    "url": url,
                    "title": title,
                    "update_time": ""
                })
        return converted

    def _search_hdhive(
        self,
        mediainfo: MediaInfo,
        media_type: MediaType,
        season: Optional[int] = None
    ) -> List[Dict]:
        """
        使用 HDHive 搜索资源
        优先使用同步版本，异常时回退到异步版本
        
        :param mediainfo: 媒体信息
        :param media_type: 媒体类型（MOVIE 或 TV）
        :param season: 季号（电视剧时使用）
        :return: 115网盘资源列表（统一格式）
        """
        from .lib.hdhive import MediaType as HDHiveMediaType
        if not mediainfo.tmdb_id:
            logger.warning(f"⚠️ {mediainfo.title} 缺少 TMDB ID，无法使用 HDHive 查询")
            return []

        hdhive_media_type = HDHiveMediaType.MOVIE if media_type == MediaType.MOVIE else HDHiveMediaType.TV
        
        
        
        # # 方法2: 回退到异步客户端（用户名/密码模式）
        # if self._hdhive_username and self._hdhive_password:
        #     try:
        #         import asyncio
        #         import os
        #         from .lib.hdhive import create_async_client as create_hdhive_async_client
                
        #         proxy_host = os.environ.get("PROXY_HOST")
        #         proxy = {"http": proxy_host, "https": proxy_host} if proxy_host else None
                
        #         logger.info(f"使用 HDHive (Async) 查询: {mediainfo.title} (TMDB ID: {mediainfo.tmdb_id})")
                
        #         async def async_search():
        #             async with create_hdhive_async_client(
        #                 username=self._hdhive_username,
        #                 password=self._hdhive_password,
        #                 headless=True,
        #                 proxy=proxy
        #             ) as client:
        #                 # 获取媒体信息
        #                 media = await client.get_media_by_tmdb_id(mediainfo.tmdb_id, hdhive_media_type)
        #                 if not media:
        #                     return []
                        
        #                 # 获取资源列表
        #                 resources_result = await client.get_resources(media.slug, hdhive_media_type, media_id=media.id)
        #                 if not resources_result or not resources_result.success:
        #                     return []
                        
        #                 # 过滤免费的 115 资源并获取分享链接
        #                 free_115_resources = []
        #                 for res in resources_result.resources:
        #                     if hasattr(res, 'website') and res.website.value == '115' and res.is_free:
        #                         share_result = await client.get_share_url_by_click(res.slug)
        #                         if share_result and share_result.url:
        #                             free_115_resources.append({
        #                                 "url": share_result.url,
        #                                 "title": res.title,
        #                                 "update_time": ""
        #                             })
                        
        #                 return free_115_resources
                
        #         # 运行异步任务
        #         loop = asyncio.new_event_loop()
        #         asyncio.set_event_loop(loop)
        #         try:
        #             results = loop.run_until_complete(async_search())
        #         finally:
        #             loop.close()
                
        #         if results:
        #             logger.info(f"HDHive (Async) 找到 {len(results)} 个免费 115 资源")
        #         else:
        #             logger.info(f"HDHive (Async) 未找到免费 115 资源")
        #         return results
                
        #     except Exception as e:
        #         logger.error(f"HDHive (Async) 查询失败: {e}")
        #         return []
        
        # 方法1: 尝试使用同步客户端（Cookie 模式）
        if self._hdhive_client:
            try:
                logger.info(f"使用 HDHive (Sync) 查询: {mediainfo.title} (TMDB ID: {mediainfo.tmdb_id})")
                
                # 获取媒体信息
                with self._hdhive_client as client:
                    media = client.get_media_by_tmdb_id(mediainfo.tmdb_id, hdhive_media_type)
                    if not media:
                        logger.info(f"HDHive (Sync) 未找到媒体: {mediainfo.title}")
                        return []
                    
                    # 获取资源列表
                    resources_result = client.get_resources(media.slug, hdhive_media_type, media_id=media.id)
                    if not resources_result or not resources_result.success:
                        logger.info(f"HDHive (Sync) 获取资源列表失败: {mediainfo.title}")
                        return []
                    
                    # 过滤免费的 115 资源
                    free_115_resources = []
                    for res in resources_result.resources:
                        if hasattr(res, 'website') and res.website.value == '115' and res.is_free:
                            # 获取分享链接
                            share_result = client.get_share_url(res.slug)
                            if share_result and share_result.url:
                                free_115_resources.append({
                                    "url": share_result.url,
                                    "title": res.title,
                                    "update_time": ""
                                })
                    
                    if free_115_resources:
                        logger.info(f"HDHive (Sync) 找到 {len(free_115_resources)} 个免费 115 资源")
                        return free_115_resources
                    else:
                        logger.info(f"HDHive (Sync) 未找到免费 115 资源")
                        return []
                        
            except Exception as e:
                logger.warning(f"HDHive (Sync) 查询失败: {e}，尝试异步模式...")
        return []

    def _check_and_finish_subscribe(self, subscribe, mediainfo, success_episodes):
        """
        检查订阅是否完成，如果完成则调用官方接口
        
        :param subscribe: 订阅对象
        :param mediainfo: 媒体信息
        :param success_episodes: 本次成功转存的集数列表（电影为[1]）
        """
        try:
            # 1. 更新 note 字段（记录已下载集数，与系统订阅检查兼容）
            # 系统会读取 note 字段来判断哪些集已下载，避免 lack_episode 被重置
            current_note = subscribe.note or []
            if mediainfo.type == MediaType.TV:
                new_note = list(set(current_note).union(set(success_episodes)))
            else:
                # 电影用 [1] 表示已下载
                new_note = list(set(current_note).union({1}))
            
            # 2. 更新缺失集数
            current_lack = subscribe.lack_episode
            new_lack = max(0, current_lack - len(success_episodes))
            
            # 3. 一次性更新 note 和 lack_episode
            update_data = {}
            if new_note != current_note:
                update_data["note"] = new_note
                logger.info(f"更新订阅 {subscribe.name} note 字段：{current_note} -> {new_note}")
            if new_lack != current_lack:
                update_data["lack_episode"] = new_lack
                logger.info(f"更新订阅 {subscribe.name} 缺失集数：{current_lack} -> {new_lack}")
            
            if update_data:
                SubscribeOper().update(subscribe.id, update_data)
            
            # 检查是否完成
            if new_lack == 0:
                logger.info(f"订阅 {subscribe.name} 所有内容已转存完成，准备完成订阅")
                
                # 生成元数据
                meta = MetaInfo(subscribe.name)
                meta.year = subscribe.year
                meta.begin_season = subscribe.season or None
                try:
                    meta.type = MediaType(subscribe.type)
                except ValueError:
                    logger.error(f'订阅 {subscribe.name} 类型错误：{subscribe.type}')
                    return
                
                # 调用官方完成订阅接口
                try:
                    SubscribeChain().finish_subscribe_or_not(
                        subscribe=subscribe,
                        meta=meta,
                        mediainfo=mediainfo,
                        downloads=None,  # 我们已经更新了 lack_episode
                        lefts={},        # 没有剩余集数
                        force=True       # 强制完成
                    )
                    logger.info(f"✅ 订阅 {subscribe.name} 已完成并移至历史记录")
                    # 发送订阅完成通知
                    if self._notify:
                        season_text = f" 第{subscribe.season}季" if subscribe.type == MediaType.TV.value and subscribe.season else ""
                        self.post_message(
                            mtype=NotificationType.Plugin,
                            title="【115网盘订阅追更】订阅完成",
                            text=f"🎉 {subscribe.name}{season_text} 所有内容已转存完成，订阅已移至历史记录。"
                        )
                except Exception as e:
                    logger.error(f"完成订阅时出错：{e}", exc_info=True)
        
        except Exception as e:
            logger.error(f"检查订阅完成状态时出错：{e}", exc_info=True)

    def _pansou_search(self, keyword: str) -> List[Dict]:
        """
        PanSou 搜索的通用逻辑
        
        :param keyword: 搜索关键词
        :return: 115网盘资源列表
        """
        cloud_types = ["115"] if self._only_115 else None
        
        channels = None
        if self._pansou_channels and self._pansou_channels.strip():
            channels = [ch.strip() for ch in self._pansou_channels.split(',') if ch.strip()]
        
        search_results = self._pansou_client.search(
            keyword=keyword,
            cloud_types=cloud_types,
            channels=channels,
            limit=20
        )
        
        results = search_results.get("results", {}) if search_results and not search_results.get("error") else {}
        return results.get("115网盘", [])

    def _search_resources(
        self,
        mediainfo: MediaInfo,
        media_type: MediaType,
        season: Optional[int] = None
    ) -> List[Dict]:
        """
        统一的资源搜索方法，支持电影和电视剧
        搜索优先级: Nullbr > HDHive > PanSou
        
        :param mediainfo: 媒体信息
        :param media_type: 媒体类型（MOVIE 或 TV）
        :param season: 季号（电视剧必需）
        :return: 115网盘资源列表
        """
        p115_results = []

        # 1. 优先使用 NullBR
        if self._nullbr_priority and self._nullbr_enabled:
            # 检查客户端是否已初始化
            if not self._nullbr_client:
                logger.warning(f"⚠️ Nullbr 已启用但未初始化（缺少 APP ID 或 API Key），跳过 Nullbr 查询")
            elif not mediainfo.tmdb_id:
                logger.warning(f"⚠️ {mediainfo.title} 缺少 TMDB ID，无法使用 Nullbr 查询")
            else:
                if media_type == MediaType.MOVIE:
                    logger.info(f"使用 Nullbr 查询电影资源: {mediainfo.title} (TMDB ID: {mediainfo.tmdb_id})")
                    nullbr_resources = self._nullbr_client.get_movie_resources(mediainfo.tmdb_id)
                else:  # MediaType.TV
                    logger.info(f"使用 Nullbr 查询电视剧资源: {mediainfo.title} S{season} (TMDB ID: {mediainfo.tmdb_id})")
                    nullbr_resources = self._nullbr_client.get_tv_resources(mediainfo.tmdb_id, season)

                if nullbr_resources:
                    p115_results = self._convert_nullbr_to_pansou_format(nullbr_resources)
                    logger.info(f"Nullbr 找到 {len(p115_results)} 个资源")
                else:
                    logger.info(f"Nullbr 优先模式未找到资源，将回退到 HDHive/PanSou 搜索")
        
        # 2. 如果 Nullbr 未找到，使用 HDHive
        if not p115_results and self._hdhive_enabled:
            hdhive_results = self._search_hdhive(mediainfo, media_type, season)
            if hdhive_results:
                p115_results = hdhive_results
                logger.info(f"HDHive 找到 {len(p115_results)} 个资源")
            else:
                logger.info(f"HDHive 未找到资源，将回退到 PanSou 搜索")
        
        # 3. 如果 HDHive 也未找到，使用 PanSou
        if not p115_results and self._pansou_enabled and self._pansou_client:
            # 构建搜索关键词
            if media_type == MediaType.MOVIE:
                logger.info(f"使用 PanSou 搜索电影资源: {mediainfo.title}")
                search_keyword = f"{mediainfo.title} {mediainfo.year}" if mediainfo.year else mediainfo.title
                # 执行搜索
                p115_results = self._pansou_search(search_keyword)
            else:  # MediaType.TV
                # 电视剧使用降级搜索策略
                # 搜索关键词列表，按优先级排序
                search_keywords = [
                    f"{mediainfo.title}第{season}季",  # 中文季号格式
                    mediainfo.title
                ]

                for keyword in search_keywords:
                    logger.info(f"使用 PanSou 搜索电视剧资源: {mediainfo.title} S{season}，关键词: '{keyword}'")
                    p115_results = self._pansou_search(keyword)
                    if p115_results:
                        logger.info(f"关键词 '{keyword}' 搜索到 {len(p115_results)} 个结果")
                        break
                    else:
                        logger.info(f"关键词 '{keyword}' 无结果，尝试下一个降级关键词")
        
        return p115_results

    def _send_transfer_notification(self, transfer_details: List[Dict[str, Any]], total_count: int):
        """
        发送转存完成通知

        :param transfer_details: 转存详情列表
        :param total_count: 转存总数
        """
        if not transfer_details:
            return

        # 构建通知文本
        text_lines = []
        first_image = None

        for detail in transfer_details:
            if detail.get("type") == "电影":
                title = detail.get("title", "未知")
                year = detail.get("year", "")
                text_lines.append(f"🎬 {title} ({year})")
                if not first_image and detail.get("image"):
                    first_image = detail.get("image")
            else:
                title = detail.get("title", "未知")
                season = detail.get("season", 1)
                episodes = detail.get("episodes", [])
                episodes.sort()
                # 格式化集数显示
                if len(episodes) <= 5:
                    ep_str = ", ".join([f"E{e:02d}" for e in episodes])
                else:
                    ep_str = f"E{episodes[0]:02d}-E{episodes[-1]:02d} 共{len(episodes)}集"
                text_lines.append(f"📺 {title} S{season:02d} {ep_str}")
                if not first_image and detail.get("image"):
                    first_image = detail.get("image")

        # 限制显示数量，避免通知过长
        if len(text_lines) > 10:
            text_lines = text_lines[:10]
            text_lines.append(f"... 等共 {len(transfer_details)} 项")

        self.post_message(
            mtype=NotificationType.Plugin,
            title=f"【115网盘订阅追更】转存完成",
            text=f"本次共转存 {total_count} 个文件\n\n" + "\n".join(text_lines)
        )

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
            # HDHive 可以有同步客户端或仅配置用户名密码（用于异步回退）
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

        # 获取所有订阅，状态为 R (订阅中) 的
        # 使用新会话避免 ScopedSession 缓存问题，确保获取最新数据
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

        downloadchain = DownloadChain()
        history: List[dict] = self.get_data('history') or []
        transferred_count = 0
        # 用于通知的转存详情列表
        transfer_details: List[Dict[str, Any]] = []

        # 排除订阅ID列表
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

            try:
                logger.info(f"处理电影订阅：{subscribe.name} ({subscribe.year})")

                # 检查历史记录是否已成功转存
                # 非严格模式下，只跳过完美匹配的，低分的可以继续洗版
                movie_history_score = -1  # -1 表示未转存过
                movie_perfect_match = False
                for h in history:
                    if (h.get("title") == subscribe.name
                            and h.get("type") == "电影"
                            and h.get("status") == "成功"):
                        score = h.get("filter_score", 0)
                        perfect = h.get("perfect_match", False)
                        if score > movie_history_score:
                            movie_history_score = score
                            movie_perfect_match = perfect

                # best_version=1 表示开启洗版（非严格模式）
                is_best_version = bool(subscribe.best_version)

                if movie_history_score >= 0:
                    if not is_best_version or movie_perfect_match:
                        logger.info(f"电影 {subscribe.name} 已在历史记录中(洗版:{is_best_version}, 完美匹配:{movie_perfect_match})，跳过")
                        continue
                    else:
                        logger.info(f"电影 {subscribe.name} 洗版中，历史分数 {movie_history_score}，尝试寻找更优资源")

                # 生成元数据
                meta = MetaInfo(subscribe.name)
                meta.year = subscribe.year
                meta.type = MediaType.MOVIE

                # 识别媒体信息
                mediainfo: MediaInfo = self.chain.recognize_media(
                    meta=meta,
                    mtype=MediaType.MOVIE,
                    tmdbid=subscribe.tmdbid,
                    doubanid=subscribe.doubanid,
                    cache=True
                )
                if not mediainfo:
                    logger.warn(f"无法识别媒体信息：{subscribe.name}")
                    continue

                # 搜索网盘资源
                p115_results = self._search_resources(
                    mediainfo=mediainfo,
                    media_type=MediaType.MOVIE
                )

                if not p115_results:
                    logger.info(f"未找到电影 {mediainfo.title} 的 115 网盘资源")
                    continue

                logger.info(f"找到 {len(p115_results)} 个 115 网盘资源")

                # 创建订阅过滤条件（best_version=1 时为非严格模式/洗版模式）
                subscribe_filter = SubscribeFilter(
                    quality=subscribe.quality,
                    resolution=subscribe.resolution,
                    effect=subscribe.effect,
                    strict=not is_best_version
                )
                if subscribe_filter.has_filters():
                    mode_text = "洗版模式" if is_best_version else "严格模式"
                    logger.info(f"电影 {subscribe.name} 过滤条件({mode_text}) - 质量: {subscribe.quality}, 分辨率: {subscribe.resolution}, 特效: {subscribe.effect}")

                # 遍历搜索结果，尝试找到并转存电影
                movie_transferred = False
                for resource in p115_results:
                    if movie_transferred:
                        break

                    share_url = resource.get("url", "")
                    resource_title = resource.get("title", "")

                    if not share_url:
                        continue

                    logger.info(f"检查分享：{resource_title} - {share_url}")

                    try:
                        # 先检查分享链接是否有效
                        share_status = self._p115_manager.check_share_status(share_url)
                        if not share_status.is_valid:
                            logger.warning(f"分享链接无效：{share_url}，原因：{share_status.status_text}")
                            continue

                        share_files = self._p115_manager.list_share_files(share_url)
                        if not share_files:
                            logger.info(f"分享链接无内容：{share_url}")
                            continue

                        # 匹配电影文件（查找最大的视频文件，应用过滤条件）
                        matched_file = FileMatcher.match_movie_file(
                            share_files, mediainfo.title,
                            subscribe_filter=subscribe_filter
                        )

                        if matched_file:
                            file_name = matched_file.get('name', '')
                            logger.info(f"找到匹配文件：{file_name}")

                            # 计算当前文件的过滤分数和是否完美匹配
                            _, current_score = subscribe_filter.match(file_name) if subscribe_filter.has_filters() else (True, 0)
                            is_perfect = subscribe_filter.is_perfect_match(file_name) if subscribe_filter.has_filters() else True

                            # 洗版模式下检查是否需要升级资源
                            if is_best_version and movie_history_score >= 0:
                                if current_score <= movie_history_score:
                                    logger.info(f"电影 {mediainfo.title} 已有分数 {movie_history_score}，当前 {current_score}，跳过")
                                    continue
                                else:
                                    logger.info(f"电影 {mediainfo.title} 洗版：旧分数 {movie_history_score} -> 新分数 {current_score}")

                            # 构建转存路径
                            save_dir = f"{self._movie_save_path}/{mediainfo.title} ({mediainfo.year})" if mediainfo.year else f"{self._movie_save_path}/{mediainfo.title}"
                            logger.info(f"转存目标路径: {save_dir}")

                            # 执行转存
                            success = self._p115_manager.transfer_file(
                                share_url=share_url,
                                file_id=matched_file.get("id"),
                                save_path=save_dir
                            )

                            # 记录历史（包含分数信息）
                            history_item = {
                                "title": mediainfo.title,
                                "year": mediainfo.year,
                                "type": "电影",
                                "status": "成功" if success else "失败",
                                "share_url": share_url,
                                "file_name": file_name,
                                "filter_score": current_score,
                                "perfect_match": is_perfect,
                                "time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                            }
                            history.append(history_item)

                            if success:
                                transferred_count += 1
                                movie_transferred = True
                                movie_history_score = current_score  # 更新历史分数
                                score_info = f"(分数:{current_score}, 完美匹配:{is_perfect})" if subscribe_filter.has_filters() else ""
                                logger.info(f"成功转存电影：{mediainfo.title} {score_info}")

                                # 收集转存详情用于通知
                                transfer_details.append({
                                    "type": "电影",
                                    "title": mediainfo.title,
                                    "year": mediainfo.year,
                                    "image": mediainfo.get_poster_image(),
                                    "file_name": file_name
                                })

                                # 添加下载历史记录
                                try:
                                    DownloadHistoryOper().add(
                                        path=save_dir,
                                        type=mediainfo.type.value,
                                        title=mediainfo.title,
                                        year=mediainfo.year,
                                        tmdbid=mediainfo.tmdb_id,
                                        imdbid=mediainfo.imdb_id,
                                        tvdbid=mediainfo.tvdb_id,
                                        doubanid=mediainfo.douban_id,
                                        image=mediainfo.get_poster_image(),
                                        downloader="115网盘",
                                        download_hash=matched_file.get("id"),
                                        torrent_name=resource_title,
                                        torrent_description=file_name,
                                        torrent_site="115网盘",
                                        username="P115StrgmSub",
                                        date=datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                        note={"source": f"Subscribe|{subscribe.name}", "share_url": share_url}
                                    )
                                    logger.debug(f"已记录电影 {mediainfo.title} 下载历史")
                                except Exception as e:
                                    logger.warning(f"记录下载历史失败：{e}")

                                # 电影转存成功后完成订阅
                                self._check_and_finish_subscribe(
                                    subscribe=subscribe,
                                    mediainfo=mediainfo,
                                    success_episodes=[1]  # 电影用 [1] 表示
                                )
                            else:
                                logger.error(f"转存失败：{mediainfo.title}")

                    except Exception as e:
                        logger.error(f"处理分享链接出错：{share_url}, 错误：{str(e)}")
                        continue

            except Exception as e:
                logger.error(f"处理电影订阅 {subscribe.name} 出错：{str(e)}")
                continue

        # 处理电视剧订阅
        for subscribe in tv_subscribes:
            # 检查系统是否停止
            if global_vars.is_system_stopped:
                break

            # 检查是否在排除列表中
            if subscribe.id in exclude_ids:
                logger.info(f"订阅 {subscribe.name} (ID:{subscribe.id}) 在排除列表中，跳过处理")
                continue

            try:
                logger.info(f"订阅信息：{subscribe.name}，开始集数：{subscribe.start_episode}, 总集数：{subscribe.total_episode}, 缺失集数：{subscribe.lack_episode}")
                logger.info(f"处理订阅：{subscribe.name} (S{subscribe.season or 1})")
                
                # 早期检查：如果订阅显示没有缺失集数，且媒体库已完整，跳过处理
                if subscribe.lack_episode == 0:
                    logger.info(f"{subscribe.name} S{subscribe.season or 1} 订阅显示媒体库已完整(lack_episode=0)，跳过")
                    continue

                # 生成元数据
                meta = MetaInfo(subscribe.name)
                meta.year = subscribe.year
                meta.begin_season = subscribe.season or 1
                meta.type = MediaType.TV

                # 识别媒体信息
                mediainfo: MediaInfo = self.chain.recognize_media(
                    meta=meta,
                    mtype=MediaType.TV,
                    tmdbid=subscribe.tmdbid,
                    doubanid=subscribe.doubanid,
                    cache=True
                )

                if not mediainfo:
                    logger.warn(f"无法识别媒体信息：{subscribe.name}")
                    continue

                # 构造总集数信息，用于 get_no_exists_info
                totals = {}
                if subscribe.season and subscribe.total_episode:
                    totals = {subscribe.season: subscribe.total_episode}

                # 获取缺失剧集
                exist_flag, no_exists = downloadchain.get_no_exists_info(
                    meta=meta,
                    mediainfo=mediainfo,
                    totals=totals
                )
                
                if exist_flag:
                    logger.info(f"{mediainfo.title_year} S{meta.begin_season} 媒体库中已完整存在")
                    # 如果缺失集数不为0，修正为0
                    if subscribe.lack_episode != 0:
                            SubscribeOper().update(subscribe.id, {"lack_episode": 0})
                    continue

                # 获取缺失的集数列表
                season = meta.begin_season or 1
                missing_episodes = []
                # 兼容 TMDB 和 豆瓣 ID
                mediakey = mediainfo.tmdb_id or mediainfo.douban_id
                
                if no_exists and mediakey:
                    season_info = no_exists.get(mediakey, {})
                    not_exist_info = season_info.get(season)
                    if not_exist_info:
                        # NotExistMediaInfo 对象，需要获取其 episodes 属性
                        missing_episodes = not_exist_info.episodes or []
                        # 如果 episodes 为空但有 total_episode，说明缺失整季，需要生成集数列表
                        if not missing_episodes and not_exist_info.total_episode:
                            start_ep = not_exist_info.start_episode or 1
                            missing_episodes = list(range(start_ep, not_exist_info.total_episode + 1))

                if not missing_episodes:
                    logger.info(f"{mediainfo.title_year} S{season} 没有缺失剧集信息")
                    continue
                
                # 过滤掉小于开始集数的剧集
                if subscribe.start_episode:
                    original_count = len(missing_episodes)
                    missing_episodes = [ep for ep in missing_episodes if ep >= subscribe.start_episode]
                    if len(missing_episodes) < original_count:
                                                                        logger.info(f"根据订阅设置，过滤掉小于 {subscribe.start_episode} 的剧集")

                # best_version=1 表示开启洗版（非严格模式）
                is_best_version = bool(subscribe.best_version)

                # 从历史记录中排除已成功转存的集数，避免重复转存
                # 洗版模式下，只排除完美匹配的集数，低分集数可以继续搜索更优资源
                transferred_episodes = set()
                # 记录每集的历史转存分数，用于洗版模式下判断是否需要升级
                episode_history_scores: Dict[int, int] = {}
                for h in history:
                    if (h.get("title") == mediainfo.title
                            and h.get("season") == season
                            and h.get("status") == "成功"):
                        ep = h.get("episode")
                        score = h.get("filter_score", 0)
                        perfect = h.get("perfect_match", False)

                        if not is_best_version:
                            # 非洗版模式（严格模式）：所有成功的都跳过
                            transferred_episodes.add(ep)
                        else:
                            # 洗版模式：只跳过完美匹配的，低分的可以继续洗版
                            if perfect:
                                transferred_episodes.add(ep)
                            else:
                                # 记录当前最高分数
                                if ep not in episode_history_scores or score > episode_history_scores[ep]:
                                    episode_history_scores[ep] = score
                
                # 构建转存路径（提前构建以便检查网盘目录）
                save_dir = f"{self._save_path}/{mediainfo.title}/Season {season}"
                
                # 检查网盘目录中已存在的剧集
                existing_episodes_in_cloud = FileMatcher.check_existing_episodes(
                    self._p115_manager, mediainfo, season, save_dir
                )
                
                # 合并历史记录和网盘已存在的集数
                all_existing = transferred_episodes | existing_episodes_in_cloud

                # 洗版模式下，需要升级的集数（有历史记录但非完美匹配）不应该被排除
                # 这些集数需要继续搜索更好的资源
                if is_best_version and episode_history_scores:
                    episodes_to_upgrade = set(episode_history_scores.keys())
                    all_existing = all_existing - episodes_to_upgrade
                    if episodes_to_upgrade:
                        logger.info(f"{mediainfo.title_year} S{season} 洗版模式：{len(episodes_to_upgrade)} 集待升级")

                if all_existing:
                    missing_episodes = [ep for ep in missing_episodes if ep not in all_existing]
                    logger.info(
                        f"{mediainfo.title_year} S{season} 跳过已存在的 {len(all_existing)} 集 "
                        f"(历史记录:{len(transferred_episodes)}, 网盘:{len(existing_episodes_in_cloud)})"
                    )

                if not missing_episodes:
                    logger.info(f"{mediainfo.title_year} S{season} 所有缺失剧集已转存")
                    # 更新订阅缺失集数为 0（因为虽然媒体库没有，但网盘已经转存了）
                    # 注意：这里不能直接设为0，否则主流程可能会认为已完成。
                    # 但为了不重复搜索，暂时不处理 lack_episode，只依赖 history 过滤
                    continue

                logger.info(f"{mediainfo.title_year} S{season} 待转存剧集：{missing_episodes}")

                # 搜索网盘资源
                p115_results = self._search_resources(
                    mediainfo=mediainfo,
                    media_type=MediaType.TV,
                    season=season
                )

                if not p115_results:
                    logger.info(f"未找到 {mediainfo.title} S{season} 的 115 网盘资源")
                    continue

                logger.info(f"找到 {len(p115_results)} 个 115 网盘资源")

                # 创建订阅过滤条件
                subscribe_filter = SubscribeFilter(
                    quality=subscribe.quality,
                    resolution=subscribe.resolution,
                    effect=subscribe.effect,
                    strict=not is_best_version
                )
                if subscribe_filter.has_filters():
                    mode_text = "洗版模式" if is_best_version else "严格模式"
                    logger.info(f"{mediainfo.title} S{season} 过滤条件({mode_text}) - 质量: {subscribe.quality}, 分辨率: {subscribe.resolution}, 特效: {subscribe.effect}")

                # 成功转存的集数列表
                success_episodes = []

                # 构建转存路径（提前构建，批量转存共用）
                save_dir = f"{self._save_path}/{mediainfo.title}/Season {season}"

                # 遍历搜索结果，尝试找到并转存缺失剧集
                for resource in p115_results:
                    # 检查单次同步上限
                    if transferred_count >= self._max_transfer_per_sync:
                        logger.info(f"已达单次同步上限 {self._max_transfer_per_sync}，剩余 {len(missing_episodes)} 集将在下次同步处理")
                        break

                    share_url = resource.get("url", "")
                    resource_title = resource.get("title", "")

                    if not share_url:
                        continue

                    # 简单的标题过滤
                    if mediainfo.title not in resource_title:
                        pass

                    logger.info(f"检查分享：{resource_title} - {share_url}")

                    try:
                        # 先检查分享链接是否有效
                        share_status = self._p115_manager.check_share_status(share_url)
                        if not share_status.is_valid:
                            logger.warning(f"分享链接无效：{share_url}，原因：{share_status.status_text}")
                            continue

                        # 列出分享内容
                        share_files = self._p115_manager.list_share_files(share_url,target_season=(season if self._skip_other_season_dirs else None))
                        if not share_files:
                            logger.info(f"分享链接无内容：{share_url}")
                            continue

                        logger.info(f"分享包含 {len(share_files)} 个文件/目录")

                        # 第一阶段：收集该分享中所有匹配的文件（不立即转存）
                        matched_items = []  # 存储匹配信息: {file, episode, score, is_perfect, is_upgrade}

                        for episode in missing_episodes[:]:
                            matched_file = FileMatcher.match_episode_file(
                                share_files,
                                mediainfo.title,
                                season,
                                episode,
                                subscribe_filter=subscribe_filter
                            )

                            if matched_file:
                                file_name = matched_file.get('name', '')
                                logger.info(f"找到匹配文件：{file_name} -> E{episode:02d}")

                                # 计算当前文件的过滤分数和是否完美匹配
                                _, current_score = subscribe_filter.match(file_name) if subscribe_filter.has_filters() else (True, 0)
                                is_perfect = subscribe_filter.is_perfect_match(file_name) if subscribe_filter.has_filters() else True

                                # 洗版模式下检查是否需要升级资源
                                is_upgrade = False
                                if is_best_version and episode in episode_history_scores:
                                    old_score = episode_history_scores[episode]
                                    if current_score <= old_score:
                                        logger.info(f"E{episode:02d} 已有分数 {old_score}，当前 {current_score}，跳过")
                                        continue
                                    else:
                                        logger.info(f"E{episode:02d} 洗版：旧分数 {old_score} -> 新分数 {current_score}")
                                        is_upgrade = True

                                matched_items.append({
                                    "file": matched_file,
                                    "episode": episode,
                                    "score": current_score,
                                    "is_perfect": is_perfect,
                                    "is_upgrade": is_upgrade
                                })

                        # 如果该分享没有匹配到任何剧集，跳过
                        if not matched_items:
                            logger.info(f"该分享未匹配到 S{season} 的任何缺失剧集，可能是季数不匹配或文件名无法识别")
                            continue

                        # 检查转存配额限制
                        remaining_quota = self._max_transfer_per_sync - transferred_count
                        if len(matched_items) > remaining_quota:
                            logger.info(f"匹配 {len(matched_items)} 集，但受配额限制仅转存 {remaining_quota} 集")
                            matched_items = matched_items[:remaining_quota]

                        # 第二阶段：批量转存
                        file_ids = [item["file"]["id"] for item in matched_items]
                        logger.info(f"准备批量转存 {len(file_ids)} 个文件到: {save_dir}")

                        success_ids, failed_ids = self._p115_manager.transfer_files_batch(
                            share_url=share_url,
                            file_ids=file_ids,
                            save_path=save_dir,
                            batch_size=self._batch_size
                        )

                        # 将成功的 file_id 转为集合，便于快速查找
                        success_id_set = set(success_ids)
                        # 收集本次批量转存成功的剧集号（用于下载历史记录）
                        batch_success_episodes = []

                        # 第三阶段：根据批量转存结果处理历史记录和状态
                        for item in matched_items:
                            file_id = item["file"]["id"]
                            episode = item["episode"]
                            file_name = item["file"]["name"]
                            current_score = item["score"]
                            is_perfect = item["is_perfect"]
                            is_upgrade = item["is_upgrade"]
                            success = file_id in success_id_set

                            # 记录历史
                            history_item = {
                                "title": mediainfo.title,
                                "season": season,
                                "episode": episode,
                                "status": "成功" if success else "失败",
                                "share_url": share_url,
                                "file_name": file_name,
                                "filter_score": current_score,
                                "perfect_match": is_perfect,
                                "time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                            }
                            history.append(history_item)

                            if success:
                                transferred_count += 1

                                # 更新历史分数记录
                                episode_history_scores[episode] = current_score
                                # 从缺失列表移除
                                if episode in missing_episodes:
                                    missing_episodes.remove(episode)

                                # 只有新转存（非洗版升级）的才加入 success_episodes
                                if not is_upgrade:
                                    success_episodes.append(episode)

                                score_info = f"(分数:{current_score}, 完美匹配:{is_perfect})" if subscribe_filter.has_filters() else ""
                                upgrade_info = " [洗版升级]" if is_upgrade else ""
                                logger.info(f"成功转存：{mediainfo.title} S{season:02d}E{episode:02d} {score_info}{upgrade_info}")

                                # 收集转存详情用于通知（按媒体聚合）
                                existing_detail = next(
                                    (d for d in transfer_details
                                     if d.get("title") == mediainfo.title and d.get("season") == season),
                                    None
                                )
                                if existing_detail:
                                    existing_detail["episodes"].append(episode)
                                else:
                                    transfer_details.append({
                                        "type": "电视剧",
                                        "title": mediainfo.title,
                                        "year": mediainfo.year,
                                        "season": season,
                                        "episodes": [episode],
                                        "image": mediainfo.get_poster_image()
                                    })

                                # 收集成功的剧集号
                                batch_success_episodes.append(episode)
                            else:
                                logger.error(f"转存失败：{mediainfo.title} S{season:02d}E{episode:02d}")

                        # 批量转存完成后，汇总记录下载历史
                        if batch_success_episodes:
                            try:
                                # 使用 StringUtils.format_ep 格式化剧集列表，如 [15, 16] -> "E15-E16"
                                episodes_str = StringUtils.format_ep(batch_success_episodes)
                                DownloadHistoryOper().add(
                                    path=save_dir,
                                    type=mediainfo.type.value,
                                    title=mediainfo.title,
                                    year=mediainfo.year,
                                    tmdbid=mediainfo.tmdb_id,
                                    imdbid=mediainfo.imdb_id,
                                    tvdbid=mediainfo.tvdb_id,
                                    doubanid=mediainfo.douban_id,
                                    seasons=f"S{season:02d}",
                                    episodes=episodes_str,
                                    image=mediainfo.get_poster_image(),
                                    downloader="115网盘",
                                    download_hash=share_url,  # 使用分享链接作为唯一标识
                                    torrent_name=resource_title,
                                    torrent_site="115网盘",
                                    username="P115StrgmSub",
                                    date=datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                    note={"source": f"Subscribe|{subscribe.name}", "share_url": share_url}
                                )
                                logger.debug(f"已记录 {mediainfo.title} S{season:02d} {episodes_str} 下载历史")
                            except Exception as e:
                                logger.warning(f"记录下载历史失败：{e}")

                        # 如果所有缺失剧集都已转存，跳出循环
                        if not missing_episodes:
                            break

                    except Exception as e:
                        logger.error(f"处理分享链接出错：{share_url}, 错误：{str(e)}")
                        continue

                # 更新订阅的缺失集数并检查是否完成
                if success_episodes:
                    self._check_and_finish_subscribe(
                        subscribe=subscribe,
                        mediainfo=mediainfo,
                        success_episodes=success_episodes
                    )

            except Exception as e:
                logger.error(f"处理订阅 {subscribe.name} 出错：{str(e)}")
                continue

        # 保存历史记录
        self.save_data('history', history[-500:])  # 只保留最近500条

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
                self._send_transfer_notification(transfer_details, transferred_count)
            else:
                # 无转存时也发送通知
                self.post_message(
                    mtype=NotificationType.Plugin,
                    title="【115网盘订阅追更】执行完成",
                    text=f"本次同步完成，共处理 {len(tv_subscribes)} 个电视剧订阅、{len(movie_subscribes)} 个电影订阅，未发现需要转存的新资源。"
                )

    def api_search(self, keyword: str, apikey: str) -> dict:
        """API: 搜索网盘资源"""
        if apikey != settings.API_TOKEN:
            return {"error": "API密钥错误"}

        if not self._pansou_client:
            return {"error": "PanSou 客户端未初始化"}

        cloud_types = ["115"] if self._only_115 else None
        return self._pansou_client.search(keyword=keyword, cloud_types=cloud_types, limit=10)

    def api_transfer(self, share_url: str, save_path: str, apikey: str) -> dict:
        """API: 转存分享链接"""
        if apikey != settings.API_TOKEN:
            return {"success": False, "error": "API密钥错误"}

        if not self._p115_manager:
            return {"success": False, "error": "115 客户端未初始化"}

        success = self._p115_manager.transfer_share(share_url, save_path or self._save_path)
        return {"success": success}

    def api_clear_history(self, apikey: str) -> dict:
        """API: 清空历史记录"""
        if apikey != settings.API_TOKEN:
            return {"success": False, "message": "API密钥错误"}

        self.save_data('history', [])
        logger.info("115网盘订阅追更历史记录已清空")
        return {"success": True, "message": "历史记录已清空"}

    def api_list_directories(self, path: str = "/", apikey: str = "") -> dict:
        """API: 列出115网盘指定路径下的目录"""
        if apikey != settings.API_TOKEN:
            return {"success": False, "error": "API密钥错误"}
        
        if not self._p115_manager:
            return {"success": False, "error": "115客户端未初始化"}
        
        try:
            # 列出目录
            directories = self._p115_manager.list_directories(path)
            
            # 构建面包屑导航
            breadcrumbs = []
            if path and path != "/":
                parts = [p for p in path.split("/") if p]
                current_path = ""
                breadcrumbs.append({"name": "根目录", "path": "/"})
                for part in parts:
                    current_path = f"{current_path}/{part}"
                    breadcrumbs.append({"name": part, "path": current_path})
            else:
                breadcrumbs.append({"name": "根目录", "path": "/"})
            
            return {
                "success": True,
                "path": path,
                "breadcrumbs": breadcrumbs,
                "directories": directories
            }
        except Exception as e:
            logger.error(f"列出115目录失败: {e}")
            return {"success": False, "error": str(e)}

   

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
