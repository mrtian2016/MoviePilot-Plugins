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
from app.db.models.site import Site
from app.log import logger
from app.plugins import _PluginBase
from app.schemas import MediaInfo
from app.schemas.types import EventType, MediaType

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
    plugin_version = "1.0.7"
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
    _block_system_subscribe: bool = False  # 是否屏蔽系统订阅
    # 运行时对象
    _pansou_client: Optional[PanSouClient] = None
    _p115_manager: Optional[P115ClientManager] = None
    _nullbr_client: Optional[NullbrClient] = None

    def init_plugin(self, config: dict = None):
        """初始化插件"""
        # 停止现有任务
        self.stop_service()

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

        # 初始化 115 客户端
        if self._cookies:
            self._p115_manager = P115ClientManager(cookies=self._cookies)

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
            "exclude_subscribes": self._exclude_subscribes,
            "block_system_subscribe": self._block_system_subscribe
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

    def _check_and_finish_subscribe(self, subscribe, mediainfo, success_episodes):
        """
        检查订阅是否完成，如果完成则调用官方接口
        
        :param subscribe: 订阅对象
        :param mediainfo: 媒体信息
        :param success_episodes: 本次成功转存的集数列表（电影为[1]）
        """
        try:
            # 更新缺失集数
            current_lack = subscribe.lack_episode
            new_lack = max(0, current_lack - len(success_episodes))
            
            if new_lack != current_lack:
                SubscribeOper().update(subscribe.id, {
                    "lack_episode": new_lack
                })
                logger.info(f"更新订阅 {subscribe.name} 缺失集数：{current_lack} -> {new_lack}")
            
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
                    logger.info(f"Nullbr 优先模式未找到资源，将回退到 PanSou 搜索")
        
        # 2. 如果 NullBR 未找到，使用 PanSou
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
        if not self._pansou_client and not self._nullbr_client:
            logger.error("PanSou 和 Nullbr 客户端均未初始化，请至少启用一个搜索源")
            return
        
        if not self._p115_manager:
            logger.error("115 客户端未初始化，请检查 Cookie 配置")
            return

        # 验证 115 登录状态
        if not self._p115_manager.check_login():
            logger.error("115 登录失败，Cookie 可能已过期")
            if self._notify:
                self.post_message(
                    title="115网盘订阅追更",
                    text="115 登录失败，Cookie 可能已过期，请更新配置"
                )
            return

        logger.info("开始执行 115 网盘订阅追更...")

        # 获取所有订阅，状态为 R (订阅中) 的
        # 使用新会话避免 ScopedSession 缓存问题，确保获取最新数据
        with SessionFactory() as db:
            subscribes = SubscribeOper(db=db).list("N,R")
        
        if not subscribes:
            logger.info("没有订阅中的数据")
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

                # 遍历搜索结果，尝试找到并转存缺失剧集
                for resource in p115_results:
                    share_url = resource.get("url", "")
                    resource_title = resource.get("title", "")

                    if not share_url:
                        continue

                    # 简单的标题过滤
                    if mediainfo.title not in resource_title:
                        # logger.info(f"跳过不匹配的资源: {resource_title}")
                        # continue
                        pass

                    logger.info(f"检查分享：{resource_title} - {share_url}")

                    try:
                        # 先检查分享链接是否有效
                        share_status = self._p115_manager.check_share_status(share_url)
                        if not share_status.is_valid:
                            logger.warning(f"分享链接无效：{share_url}，原因：{share_status.status_text}")
                            continue

                        # 列出分享内容
                        share_files = self._p115_manager.list_share_files(share_url)
                        if not share_files:
                            logger.info(f"分享链接无内容：{share_url}")
                            continue

                        logger.info(f"分享包含 {len(share_files)} 个文件/目录")

                        # 匹配缺失剧集（应用过滤条件）
                        matched_count = 0
                        for episode in missing_episodes[:]:  # 使用切片复制，因为会修改列表
                            matched_file = FileMatcher.match_episode_file(
                                share_files,
                                mediainfo.title,
                                season,
                                episode,
                                subscribe_filter=subscribe_filter
                            )

                            if matched_file:
                                matched_count += 1
                                file_name = matched_file.get('name', '')
                                logger.info(f"找到匹配文件：{file_name} -> E{episode:02d}")

                                # 计算当前文件的过滤分数和是否完美匹配
                                _, current_score = subscribe_filter.match(file_name) if subscribe_filter.has_filters() else (True, 0)
                                is_perfect = subscribe_filter.is_perfect_match(file_name) if subscribe_filter.has_filters() else True

                                # 洗版模式下检查是否需要升级资源
                                if is_best_version and episode in episode_history_scores:
                                    old_score = episode_history_scores[episode]
                                    if current_score <= old_score:
                                        logger.info(f"E{episode:02d} 已有分数 {old_score}，当前 {current_score}，跳过")
                                        continue
                                    else:
                                        logger.info(f"E{episode:02d} 洗版：旧分数 {old_score} -> 新分数 {current_score}")

                                # 构建转存路径
                                save_dir = f"{self._save_path}/{mediainfo.title}/Season {season}"
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

                                    # 判断是否是洗版升级（在更新 episode_history_scores 之前判断）
                                    # 如果这集之前已有历史记录，说明是升级而非填补缺失
                                    is_upgrade = is_best_version and episode in episode_history_scores

                                    # 更新历史分数记录
                                    episode_history_scores[episode] = current_score
                                    # 只有在 missing_episodes 中才移除
                                    if episode in missing_episodes:
                                        missing_episodes.remove(episode)

                                    # 只有新转存（非洗版升级）的才加入 success_episodes
                                    # 洗版升级不应该减少 lack_episode
                                    if not is_upgrade:
                                        success_episodes.append(episode)

                                    score_info = f"(分数:{current_score}, 完美匹配:{is_perfect})" if subscribe_filter.has_filters() else ""
                                    upgrade_info = " [洗版升级]" if is_upgrade else ""
                                    logger.info(f"成功转存：{mediainfo.title} S{season:02d}E{episode:02d} {score_info}{upgrade_info}")
                                else:
                                    logger.error(f"转存失败：{mediainfo.title} S{season:02d}E{episode:02d}")

                        # 如果该分享没有匹配到任何剧集，打印提示
                        if matched_count == 0:
                            logger.info(f"该分享未匹配到 S{season} 的任何缺失剧集，可能是季数不匹配或文件名无法识别")

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

        if self._notify and transferred_count > 0:
            self.post_message(
                title="115网盘订阅追更",
                text=f"本次追更完成，共转存 {transferred_count} 个文件"
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
            channel=event_data.get("channel"),
            title="开始执行 115 网盘订阅追更...",
            userid=event_data.get("user")
        )


        self.sync_subscribes()

        self.post_message(
            channel=event_data.get("channel"),
            title="115 网盘订阅追更完成！",
            userid=event_data.get("user")
        )
