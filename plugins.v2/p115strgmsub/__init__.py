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
from app.db.subscribe_oper import SubscribeOper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas import MediaInfo
from app.schemas.types import EventType, MediaType

from .pansou import PanSouClient
from .p115client import P115ClientManager
from .ui_config import UIConfig
from .file_matcher import FileMatcher

lock = Lock()


class ClearHistoryRequest(BaseModel):
    apikey: Optional[str] = None


class P115StrgmSub(_PluginBase):
    """115网盘订阅追更插件"""

    # 插件名称
    plugin_name = "115网盘订阅追更"
    # 插件描述
    plugin_desc = "结合MoviePilot订阅功能，自动搜索115网盘资源并转存缺失剧集。"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/jxxghp/MoviePilot-Plugins/main/icons/cloud.png"
    # 插件版本
    plugin_version = "1.0.0"
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
    _cron: str = "*/30 * * * *"
    _notify: bool = False
    _cookies: str = ""
    _pansou_url: str = "https://so.252035.xyz"
    _pansou_username: str = ""
    _pansou_password: str = ""
    _pansou_auth_enabled: bool = True
    _pansou_channels: str = ""  # TG搜索频道列表,用逗号分隔
    _save_path: str = "/我的接收/MoviePilot"
    _only_115: bool = True  # 只搜索115网盘资源
    # 运行时对象
    _pansou_client: Optional[PanSouClient] = None
    _p115_manager: Optional[P115ClientManager] = None

    def init_plugin(self, config: dict = None):
        """初始化插件"""
        # 停止现有任务
        self.stop_service()

        # 加载配置
        if config:
            self._enabled = config.get("enabled", False)
            self._cron = config.get("cron", "")
            self._notify = config.get("notify", False)
            self._onlyonce = config.get("onlyonce", False)
            self._cookies = config.get("cookies", "")
            self._pansou_url = config.get("pansou_url", "")
            self._pansou_username = config.get("pansou_username", "")
            self._pansou_password = config.get("pansou_password", "")
            self._pansou_auth_enabled = config.get("pansou_auth_enabled", True)
            self._pansou_channels = config.get("pansou_channels", "")
            self._save_path = config.get("save_path", "/我的接收/MoviePilot")
            self._only_115 = config.get("only_115", True)

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
        if self._pansou_url:
            self._pansou_client = PanSouClient(
                base_url=self._pansou_url,
                username=self._pansou_username,
                password=self._pansou_password,
                auth_enabled=self._pansou_auth_enabled
            )

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
            "cookies": self._cookies,
            "pansou_url": self._pansou_url,
            "pansou_username": self._pansou_username,
            "pansou_password": self._pansou_password,
            "pansou_auth_enabled": self._pansou_auth_enabled,
            "pansou_channels": self._pansou_channels
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
        if not self._pansou_client:
            logger.error("PanSou 客户端未初始化，请检查配置")
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
        # 注意：SubscribeOper().list() 在不传参数时返回所有。
        # 这里建议只处理 'R' 状态的订阅，或者 'N,R'
        subscribes = SubscribeOper().list("R")
        if not subscribes:
            logger.info("没有订阅中的数据")
            return

        # 只处理电视剧订阅
        tv_subscribes = [s for s in subscribes if s.type == MediaType.TV.value]
        if not tv_subscribes:
            logger.info("没有电视剧订阅")
            return

        logger.info(f"共有 {len(tv_subscribes)} 个电视剧订阅待处理")

        downloadchain = DownloadChain()
        history: List[dict] = self.get_data('history') or []
        transferred_count = 0

        for subscribe in tv_subscribes:
            # 检查系统是否停止
            if global_vars.is_system_stopped:
                break
                
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

                # 从历史记录中排除已成功转存的集数，避免重复转存
                transferred_episodes = set()
                for h in history:
                    if (h.get("title") == mediainfo.title
                            and h.get("season") == season
                            and h.get("status") == "成功"):
                        transferred_episodes.add(h.get("episode"))
                
                # 构建转存路径（提前构建以便检查网盘目录）
                save_dir = f"{self._save_path}/{mediainfo.title}/Season {season}"
                
                # 检查网盘目录中已存在的剧集
                existing_episodes_in_cloud = FileMatcher.check_existing_episodes(
                    self._p115_manager, mediainfo, season, save_dir
                )
                
                # 合并历史记录和网盘已存在的集数
                all_existing = transferred_episodes | existing_episodes_in_cloud
                
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
                # 优化搜索关键字：如果有年份，加上年份会更准确，但网盘资源命名不一定规范
                # 尝试格式：Title Season
                search_keyword = f"{mediainfo.title} 第{season}季"
                if season == 1:
                        # 第一季有时候不带季号，或者带年份
                        search_keyword = f"{mediainfo.title}"
                
                cloud_types = ["115"] if self._only_115 else None
                
                # 解析TG频道列表
                channels = None
                if self._pansou_channels and self._pansou_channels.strip():
                    channels = [ch.strip() for ch in self._pansou_channels.split(',') if ch.strip()]

                search_results = self._pansou_client.search(
                    keyword=search_keyword,
                    cloud_types=cloud_types,
                    channels=channels,
                    limit=20
                )
                if not search_results or search_results.get("error"):
                    logger.warn(f"搜索 {search_keyword} 无结果或出错：{search_results.get('error', '未知')}")
                    continue

                results = search_results.get("results", {})
                p115_results = results.get("115网盘", [])

                if not p115_results:
                    logger.info(f"未找到 {search_keyword} 的 115 网盘资源")
                    continue

                logger.info(f"找到 {len(p115_results)} 个 115 网盘资源")
                
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
                        # logger.debug(f"跳过不匹配的资源: {resource_title}")
                        # continue
                        pass

                    logger.info(f"检查分享：{resource_title} - {share_url}")

                    try:
                        # 列出分享内容
                        share_files = self._p115_manager.list_share_files(share_url)
                        if not share_files:
                            logger.debug(f"分享链接无内容或已失效：{share_url}")
                            continue

                        # 匹配缺失剧集
                        for episode in missing_episodes[:]:  # 使用切片复制，因为会修改列表
                            matched_file = FileMatcher.match_episode_file(
                                share_files,
                                mediainfo.title,
                                season,
                                episode
                            )

                            if matched_file:
                                logger.info(f"找到匹配文件：{matched_file.get('name')} -> E{episode:02d}")

                                # 构建转存路径
                                save_dir = f"{self._save_path}/{mediainfo.title}/Season {season}"
                                logger.info(f"转存目标路径: {save_dir}")

                                # 执行转存
                                success = self._p115_manager.transfer_file(
                                    share_url=share_url,
                                    file_id=matched_file.get("id"),
                                    save_path=save_dir
                                )

                                # 记录历史
                                history_item = {
                                    "title": mediainfo.title,
                                    "season": season,
                                    "episode": episode,
                                    "status": "成功" if success else "失败",
                                    "share_url": share_url,
                                    "file_name": matched_file.get("name"),
                                                                                                            "time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                                }
                                history.append(history_item)

                                if success:
                                    transferred_count += 1
                                    missing_episodes.remove(episode)
                                    success_episodes.append(episode)
                                    logger.info(f"成功转存：{mediainfo.title} S{season:02d}E{episode:02d}")
                                else:
                                    logger.error(f"转存失败：{mediainfo.title} S{season:02d}E{episode:02d}")

                        # 如果所有缺失剧集都已转存，跳出循环
                        if not missing_episodes:
                            break

                    except Exception as e:
                        logger.error(f"处理分享链接出错：{share_url}, 错误：{str(e)}")
                        continue

                # 更新订阅的缺失集数
                # 只有当确实转存成功了一些集数，才更新
                if success_episodes:
                    # 重新计算剩余缺失数
                    # 这里有点复杂，因为 no_exists 是基于媒体库的。
                    # 我们转存到网盘后，媒体库并不会立即有（除非开了挂载和自动扫描）。
                    # 但为了 UI 上能看到进度，或者为了逻辑正确，可以减少 lack_episode。
                    current_lack = subscribe.lack_episode
                    new_lack = max(0, current_lack - len(success_episodes))
                    
                    if new_lack != current_lack:
                        SubscribeOper().update(subscribe.id, {
                            "lack_episode": new_lack
                        })
                        logger.info(f"更新订阅 {subscribe.name} 缺失集数：{current_lack} -> {new_lack}")

            except Exception as e:
                logger.error(f"处理订阅 {subscribe.name} 出错：{str(e)}")
                continue

        # 保存历史记录
        self.save_data('history', history[-500:])  # 只保留最近500条

        logger.info(f"115 网盘订阅追更完成，共转存 {transferred_count} 个文件")

        if self._notify and transferred_count > 0:
            self.post_message(
                title="115网盘订阅追更",
                text=f"本次追更完成，共转存 {transferred_count} 个剧集文件"
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

    def api_clear_history(self, request: ClearHistoryRequest) -> dict:
        """API: 清空历史记录"""
        apikey = request.apikey
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
