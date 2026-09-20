"""
115网盘订阅搜索插件
结合MoviePilot订阅功能，自动搜索115网盘资源并转存缺失剧集
"""
import asyncio
import copy
import datetime
import time
from pathlib import Path
from threading import Lock, RLock
from typing import Optional, Any, List, Dict, Tuple

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import text

from app.core.config import settings, global_vars
from app.core.event import Event, eventmanager
from app.db import SessionFactory
from app.db.subscribe_oper import SubscribeOper
from app.db.models.site import Site
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import EventType, MediaType, NotificationType

from .clients import (PanSouClient, P115ClientManager, NullbrClient,
                      KDocsClient, KDocsError, Dian115Client, Dian115Error,
                      LeaderboardClient, LeaderboardError, resolve_source_ids,
                      PinglianClient)
from .handlers import (SearchHandler, SyncHandler, SubscribeHandler, ApiHandler,
                       CheckinHandler, LeaderboardHandler, ShareLinkHandler)
from .ui import UIConfig

lock = Lock()

# ---------------------------------------------------------------------------
# 账户信息卡片缓存（v1.8.3，移植自网盘搜索助手 core/api/account.py）
#
# 三层结构，缺一不可：
#   1. 内存 TTL 缓存（5 分钟）—— 挡住同一次会话内的重复读取；
#   2. 刷新冷却守卫（30 秒）—— 挡住用户连点刷新按钮打爆第三方接口；
#   3. 独立数据库快照 —— 进程重启后配置页仍能秒出上次的账户信息。
#
# 用普通 dict + 时间戳实现，不依赖 MoviePilot 的 TTLCache（保持本插件
# 「零平台内部依赖」的既有约定）。
# ---------------------------------------------------------------------------
_ACCOUNT_INFO_TTL_SECONDS = 5 * 60
_ACCOUNT_REFRESH_COOLDOWN_SECONDS = 30
_ACCOUNT_INFO_LOCK = RLock()
# key -> (expire_ts, payload)
_ACCOUNT_INFO_CACHE: Dict[str, Tuple[float, Dict[str, Any]]] = {}
# key -> expire_ts
_ACCOUNT_REFRESH_GUARD: Dict[str, float] = {}


def _account_cache_get(key: str) -> Optional[Dict[str, Any]]:
    """读内存 TTL 缓存；过期即视为未命中并顺手清理。"""
    now = time.monotonic()
    with _ACCOUNT_INFO_LOCK:
        entry = _ACCOUNT_INFO_CACHE.get(key)
        if not entry:
            return None
        expire_at, payload = entry
        if expire_at <= now:
            _ACCOUNT_INFO_CACHE.pop(key, None)
            return None
        return copy.deepcopy(payload)


def _account_cache_set(key: str, payload: Dict[str, Any]) -> None:
    """写内存 TTL 缓存。"""
    with _ACCOUNT_INFO_LOCK:
        _ACCOUNT_INFO_CACHE[key] = (
            time.monotonic() + _ACCOUNT_INFO_TTL_SECONDS,
            copy.deepcopy(payload),
        )


def _account_guard_active(key: str) -> bool:
    """刷新冷却是否仍在生效。"""
    now = time.monotonic()
    with _ACCOUNT_INFO_LOCK:
        expire_at = _ACCOUNT_REFRESH_GUARD.get(key)
        if not expire_at:
            return False
        if expire_at <= now:
            _ACCOUNT_REFRESH_GUARD.pop(key, None)
            return False
        return True


def _account_guard_arm(key: str) -> None:
    """进入刷新冷却。"""
    with _ACCOUNT_INFO_LOCK:
        _ACCOUNT_REFRESH_GUARD[key] = (
            time.monotonic() + _ACCOUNT_REFRESH_COOLDOWN_SECONDS
        )


def _human_size(value: Any) -> str:
    """把字节数格式化成可读容量；无法解析时返回「—」。"""
    try:
        size = float(value)
    except (TypeError, ValueError):
        return "—"
    if size <= 0:
        return "—"
    units = ("B", "KB", "MB", "GB", "TB", "PB")
    index = 0
    while size >= 1024 and index < len(units) - 1:
        size /= 1024.0
        index += 1
    if index == 0:
        return f"{int(size)} {units[index]}"
    return f"{size:.2f} {units[index]}"


def clear_account_cache(account_key: str = "") -> None:
    """清空账户内存缓存与冷却守卫（发布/调试用）。"""
    normalized = str(account_key or "").strip().lower()
    with _ACCOUNT_INFO_LOCK:
        if normalized:
            _ACCOUNT_INFO_CACHE.pop(normalized, None)
            _ACCOUNT_REFRESH_GUARD.pop(normalized, None)
        else:
            _ACCOUNT_INFO_CACHE.clear()
            _ACCOUNT_REFRESH_GUARD.clear()



class P115SubSearch(_PluginBase):
    """115网盘订阅搜索插件"""

    # 插件名称
    plugin_name = "115网盘订阅搜索"
    # 插件描述
    plugin_desc = "结合MoviePilot订阅功能，自动搜索115网盘资源并转存缺失的电影和剧集。"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/jxxghp/MoviePilot-Plugins/main/icons/cloud.png"
    # 插件版本
    plugin_version = "1.9.1"
    # 版本历史（v1.9.1）：
    #   * 首次使用榜单默认启用 TMDB 流行趋势，并提供数据页初始化入口；
    #   * 数据页新增 115 盘链解析卡片，复用既有脱敏解析 API。
    # v1.9.0：
    #   * 新增「榜单」Tab：原生适配 MoviePilot RecommendChain（TMDB / 豆瓣榜单），
    #     支持榜单浏览、媒体条目展示与一键订阅入口；来源为空或异常时优雅降级。
    #   * 新增盘链能力：识别 / 解析用户输入或发送的 115 分享链接并给出明确状态，
    #     非法、失效、需要访问码均有可操作中文提示，且不泄露任何敏感字段。
    #   * 声明式界面接入：设置页新增「榜单」页签（来源多选 + 分页 / 缓存 /
    #     后台刷新间隔），数据页新增只读榜单卡片与「刷新榜单」按钮，空状态与
    #     降级提示均为中文文案；页面渲染保持零网络开销。
    #   * 配置读写对称：榜单相关 5 个配置项在类属性 / init_plugin / UI 控件 /
    #     update_config 四处齐全；原有 KDocs、PanSou 检测、多链接转存、
    #     系统订阅屏蔽、dian115 签到 / 抽奖 / 离线处理能力全部保留。
    # 插件作者
    plugin_author = "mrtian2016"
    # 作者主页
    author_url = "https://github.com/mrtian2016"
    # 插件配置项ID前缀
    plugin_config_prefix = "p115subsearch_"
    plugin_order = 20
    auth_level = 1

    # 私有变量
    _scheduler: Optional[BackgroundScheduler] = None
    _toggle_scheduler: Optional[BackgroundScheduler] = None  # 用于延迟切换/窗口切换
    _checkin_scheduler: Optional[BackgroundScheduler] = None  # 签到独立周期（v1.8.0）

    # 配置属性
    _enabled: bool = False
    _onlyonce: bool = False
    _cron: str = "30 2,10,18 * * *"
    _notify: bool = False

    _cookies: str = ""
    _pansou_enabled: bool = True
    _pansou_url: str = "https://so.252035.xyz"
    _pansou_username: str = ""
    _pansou_password: str = ""
    _pansou_auth_enabled: bool = False
    _pansou_channels: str = "QukanMovie"
    # PanSou 链接有效性检测层（全渠道）
    _pansou_check_enabled: bool = True
    _max_transfer_links: int = 5

    _save_path: str = "/我的接收/MoviePilot/TV"
    _movie_save_path: str = "/我的接收/MoviePilot/Movie"
    _only_115: bool = True
    # 订阅过滤模式："exclude" 排除模式（处理除勾选外的全部订阅）/ "include" 指定模式（仅处理勾选的订阅）
    _subscribe_filter_mode: str = "exclude"
    _exclude_subscribes: List[int] = []
    _include_subscribes: List[int] = []
    # 搜索源优先级（按列表顺序），为空时默认 Nullbr > PanSou
    _search_source_order: List[str] = []
    _pinglian_enabled: bool = False
    _pinglian_url: str = ""

    _nullbr_enabled: bool = False
    _nullbr_appid: str = ""
    _nullbr_api_key: str = ""

    # KDocs 在线文档库
    _kdocs_enabled: bool = False
    _kdocs_token: str = ""
    _kdocs_doc_urls: str = ""
    _kdocs_cache_ttl_hours: int = 6
    _kdocs_batch_rows: int = 1000
    _kdocs_cookie: str = ""

    # 癫影（Dian115）—— 搜索源 + 签到/转盘（v1.8.0）
    _dian115_enabled: bool = False
    _dian115_email: str = ""
    _dian115_password: str = ""
    # v1.8.1：自动登录（cloakbrowser 本地解 Cloudflare Turnstile），
    # 开启后账号密码即可全自动登录，不再需要每 24 小时手工换 Token
    _dian115_auto_login: bool = True
    # 浏览器独立代理（留空则复用插件全局代理）；Cloudflare 验证需海外出口时使用
    _dian115_browser_proxy: str = ""
    # 会话保活周期（Cron）；留空则仅在任务执行时按需登录
    _dian115_login_cron: str = ""
    # 账户卡片自动刷新间隔（分钟）；0=关闭。后台定时刷新 115/癫影账户快照，
    # 配置页打开即见近况，癫影同时兼做会话保活（v1.8.4）
    _account_refresh_minutes: int = 30
    # 浏览器登录后复制的 __Host-portal_token（可选兜底；此前是唯一认证方式）
    _dian115_token: str = ""
    # 积分解锁与预算（默认关闭，不开就绝不扣分）
    _dian115_auto_unlock: bool = False
    _dian115_max_unlock_points: int = 50
    _dian115_max_points_per_sub: int = 20

    # 签到总控（v1.8.0）
    _checkin_enabled: bool = False
    _checkin_notify: bool = True
    _checkin_onlyonce: bool = False
    _checkin_cron: str = ""
    _p115_checkin_enabled: bool = True
    _dian115_checkin_enabled: bool = False
    _dian115_checkin_mode: str = "normal"
    _dian115_lottery_enabled: bool = False
    _dian115_lottery_count: int = 0

    # 榜单订阅（v1.9.0）—— 榜单浏览 / 订阅入口，数据来自 MoviePilot 原生推荐链
    _leaderboard_enabled: bool = False
    _leaderboard_sources: List[str] = []
    _leaderboard_page_size: int = 20
    _leaderboard_cache_minutes: int = 30
    _leaderboard_refresh_minutes: int = 0

    # 是否屏蔽系统订阅（True=已屏蔽系统订阅，False=已恢复系统订阅）
    _block_system_subscribe: bool = False

    _max_transfer_per_sync: int = 50
    _batch_size: int = 20
    _skip_other_season_dirs: bool = True

    # 窗口配置：站点/延迟/窗口期
    _unblock_site_ids: List[int] = []
    _unblock_site_names: List[str] = []
    _unblock_delay_minutes: int = 5          # -1 禁用触发条件1（并视为禁用窗口）
    _system_subscribe_window_hours: float = 1.0  # 0 禁用窗口

    # 系统订阅站点 RssSites 备份（屏蔽时备份原值，恢复时还原；None=无备份）
    _rss_sites_backup: Optional[List[int]] = None

    # 运行时对象
    _pansou_client: Optional[PanSouClient] = None
    _p115_manager: Optional[P115ClientManager] = None
    _nullbr_client: Optional[NullbrClient] = None
    _dian115_client: Optional[Any] = None

    # 处理器
    _search_handler: Optional[SearchHandler] = None
    _subscribe_handler: Optional[SubscribeHandler] = None
    _sync_handler: Optional[SyncHandler] = None
    _checkin_handler: Optional[CheckinHandler] = None
    _api_handler: Optional[ApiHandler] = None
    # v1.9.0：榜单订阅与盘链处理器
    _leaderboard_client: Optional[LeaderboardClient] = None
    _leaderboard_handler: Optional[LeaderboardHandler] = None
    _share_link_handler: Optional[ShareLinkHandler] = None

    # v1.7.3 恢复 cron 最小间隔限制（v1.7.2 曾取消，阈值从 8 小时调整为 4 小时）
    _MIN_INTERVAL_HOURS: int = 4
    # 间隔不足时的回退默认 cron（同 4 小时周期）
    _FALLBACK_CRON: str = "30 */4 * * *"

    # ------------------ 调度器 ------------------

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

    # ------------------ cron间隔校验 ------------------

    @staticmethod
    def _cron_interval_ge_min_hours(cron_expr: str, min_hours: int) -> bool:
        """
        校验 cron 表达式的实际最小触发间隔是否 >= min_hours 小时
        通过计算未来 12 次触发时间，取相邻两次的最小间隔判断
        （v1.7.3 恢复，阈值 4 小时）
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

    # ------------------ 站点解析 ------------------

    def _load_site_records(self) -> List[Dict[str, Any]]:
        with SessionFactory() as db:
            rows = db.execute(text("SELECT id, name, is_active FROM site")).fetchall()
        out = []
        for r in rows:
            out.append({"id": int(r[0]), "name": str(r[1]), "is_active": bool(r[2])})
        return out

    def _resolve_site_ids(self, ids: Optional[List[int]] = None, names: Optional[List[str]] = None) -> List[int]:
        ids = ids or []
        names = names or []

        site_records = self._load_site_records()
        by_name = {s["name"]: s for s in site_records}
        by_id = {s["id"]: s for s in site_records}

        final_ids: List[int] = []
        for sid in ids:
            if sid in by_id:
                final_ids.append(sid)
            else:
                logger.warning(f"站点ID不存在：id={sid}（将跳过）")

        for nm in names:
            rec = by_name.get(nm)
            if not rec:
                logger.warning(f"站点名称不存在：name={nm}（将跳过）")
                continue
            final_ids.append(int(rec["id"]))

        seen = set()
        uniq = []
        for x in final_ids:
            if x not in seen:
                seen.add(x)
                uniq.append(x)

        mapped = []
        for x in uniq:
            rec = by_id.get(x, {})
            mapped.append(f"{rec.get('name','?')}({x})")
        logger.info(f"订阅站点解析结果：ids={uniq} | 映射={mapped}")
        return uniq

    def _ensure_115_site_id(self, db=None) -> int:
        """
        确保 115网盘 站点存在并返回 ID
        :param db: 可选的数据库会话，若未传入则创建新会话
        """
        def _do_ensure(session):
            row = session.execute(text("SELECT id FROM site WHERE name=:n LIMIT 1"), {"n": "115网盘"}).fetchone()
            if row and row[0] is not None:
                return int(row[0])

            # existing = Site.get(session, -1)
            row_ex = session.execute(text("SELECT id FROM site WHERE id=:i"), {"i": -1}).fetchone()
            if not row_ex:
                session.execute(
                    text(
                        "INSERT INTO site (id, name, url, is_active, limit_interval, limit_count, limit_seconds, timeout) "
                        "VALUES (:id, :name, :url, :is_active, :limit_interval ,:limit_count, :limit_seconds, :timeout)"
                    ),
                    {
                        "id": -1,
                        "name": "115网盘",
                        "url": "https://115.com",
                        "is_active": True,
                        "limit_interval": 10000000,
                        "limit_count": 1,
                        "limit_seconds": 10000000,
                        "timeout": 1
                    }
                )
                session.commit()
                logger.info("已插入站点记录：115网盘(id=-1)")
            return -1

        if db is not None:
            return _do_ensure(db)
        else:
            with SessionFactory() as new_db:
                return _do_ensure(new_db)

    def _is_subscribe_excluded(self, subscribe_id: int) -> bool:
        """
        按订阅过滤模式判断订阅是否不归本插件处理

        - exclude 排除模式：勾选的订阅被排除，其余全部处理
        - include 指定模式：仅处理勾选的订阅，其余全部排除
        """
        if self._subscribe_filter_mode == "include":
            return subscribe_id not in set(self._include_subscribes or [])
        return subscribe_id in set(self._exclude_subscribes or [])

    def _apply_sites_to_all_subscribes(self, site_ids: List[int], reason: str):
        """ 应用站点ID到所有订阅 """
        with SessionFactory() as db:
            # 复用 SubscribeOper 实例，避免循环中重复创建
            subscribe_oper = SubscribeOper(db=db)
            subs = subscribe_oper.list() or []
            updated = 0
            excluded = 0
            for s in subs:
                if self._is_subscribe_excluded(s.id):
                    excluded += 1
                    continue
                subscribe_oper.update(s.id, {"sites": site_ids})
                updated += 1
        logger.info(f"{reason}：已更新 {updated} 个订阅（跳过 {excluded} 个排除订阅）")

    # ------------------ 禁用窗口判断 ------------------

    def _window_disabled(self) -> bool:
        # 站点空 / 窗口=0 / delay=-1 => 始终保持屏蔽，不安排任何进入已恢复状态任务
        if not self._unblock_site_names:
            return True
        if float(self._system_subscribe_window_hours or 0) <= 0:
            return True
        if int(self._unblock_delay_minutes) < 0:
            return True
        return False

    def _window_enabled(self) -> bool:
        return not self._window_disabled()

    # ------------------ 系统默认订阅站点：只在已恢复系统订阅时尝试 ------------------

    def _try_set_default_sites_for_unblocked(self, site_ids: List[int]):
        """
        只在“已恢复系统订阅”时尝试设置系统默认订阅站点为窗口站点。
        若系统不存在对应key，会静默失败，不影响订阅 sites 已更新。
        """
        try:
            from app.db.systemconfig_oper import SystemConfigOper
        except Exception:
            return

        def _build_oper(db):
            try:
                return SystemConfigOper(db)
            except Exception:
                try:
                    return SystemConfigOper(db=db)
                except Exception:
                    return None

        candidate_keys = [
            "subscribe_sites",
            "subscribe_site_ids",
            "system_subscribe_sites",
            "system_subscribe_site_ids",
            "subscribe_sites_selected",
        ]

        with SessionFactory() as db:
            oper = _build_oper(db)
            if not oper:
                return
            get_fn = getattr(oper, "get", None) or getattr(oper, "get_by_key", None)
            set_fn = getattr(oper, "set", None) or getattr(oper, "set_by_key", None)
            if not get_fn or not set_fn:
                return

            for k in candidate_keys:
                try:
                    cur = get_fn(k)
                except Exception:
                    cur = None
                if cur is None:
                    continue
                try:
                    set_fn(k, site_ids)
                    logger.info(f"已恢复系统订阅：已尝试同步默认订阅站点 key={k} value={site_ids}")
                    break
                except Exception:
                    continue

    # ------------------ 系统 RssSites 同步管控（方案 A1） ------------------

    @staticmethod
    def _build_system_config_oper():
        """构建 SystemConfigOper 实例，失败返回 None（兼容不同 MP 版本签名）"""
        try:
            from app.db.systemconfig_oper import SystemConfigOper
        except Exception:
            return None
        try:
            return SystemConfigOper()
        except Exception:
            pass
        try:
            return SystemConfigOper(db=None)
        except Exception:
            return None

    def _get_rss_sites(self) -> Optional[List[int]]:
        """读取系统 RssSites，失败返回 None"""
        oper = self._build_system_config_oper()
        if not oper:
            return None
        try:
            return oper.get('RssSites')
        except Exception as e:
            logger.warning(f"读取系统 RssSites 失败（不影响插件运行）: {e}")
            return None

    def _set_rss_sites(self, value: List[int]) -> bool:
        """写系统 RssSites，失败返回 False"""
        oper = self._build_system_config_oper()
        if not oper:
            return False
        try:
            oper.set('RssSites', value)
            return True
        except Exception as e:
            logger.warning(f"写入系统 RssSites 失败（不影响插件运行）: {e}")
            return False

    def _backup_and_block_rss_sites(self):
        """
        进入屏蔽态：备份 RssSites 原值（仅首次，幂等）并写为 [-1]。
        - 备份 key 已存在时不覆盖（防止 [-1] 污染原值）
        - 当前值就是 [-1]（已污染/用户本就屏蔽）时不备份
        """
        current = self._get_rss_sites()
        if current is not None and list(current) != [-1] and self._rss_sites_backup is None:
            self._rss_sites_backup = list(current)
            logger.info(f"已备份系统订阅站点 RssSites 原值（{len(current)} 个站点），RssSites 将切换为 [-1]")
        self._set_rss_sites([-1])

    def _restore_rss_sites_backup(self) -> bool:
        """
        进入恢复态：从备份还原 RssSites 原值并清除备份。
        返回是否实际还原（还原优先，调用方据此跳过默认站点尝试）。
        """
        if self._rss_sites_backup is None:
            return False
        restored = self._set_rss_sites(list(self._rss_sites_backup))
        if restored:
            logger.info(f"已还原系统订阅站点 RssSites 原值（{len(self._rss_sites_backup)} 个站点）")
        self._rss_sites_backup = None
        return restored

    def _selfheal_rss_sites_on_start(self):
        """
        启动自愈（init_plugin 早期调用）：
        - 屏蔽态且 RssSites != [-1] -> 补写 [-1]
        - 非屏蔽态且存在备份 -> 还原备份并清除备份
        - 干净态 -> 不动
        """
        try:
            if self._block_system_subscribe:
                current = self._get_rss_sites()
                if current is None or list(current) == [-1]:
                    return
                self._backup_and_block_rss_sites()
                logger.info("启动自愈：当前为屏蔽态，已补写系统 RssSites=[-1]")
            else:
                if self._rss_sites_backup is None:
                    return
                self._restore_rss_sites_backup()
                logger.info("启动自愈：当前为非屏蔽态，已还原系统 RssSites 备份")
        except Exception as e:
            logger.warning(f"启动自愈 RssSites 失败（不影响插件运行）: {e}")

    # ------------------ 两态切换（日志统一） ------------------

    def _enter_blocked(self, reason: str):
        """
        已屏蔽系统订阅：
        - 全量订阅 sites=仅115
        - 同步管控系统 RssSites：备份原值（仅首次）并写为 [-1]（方案 A1）
        - 不再尝试设置屏蔽态默认站点=115（依赖 SubscribeAdded 兜底）
        - 取消所有窗口任务
        """
        self._ensure_toggle_scheduler()
        self._cancel_toggle_jobs()
        self._init_subscribe_handler()

        self._subscribe_handler.set_blocked_sites_only_115()
        self._backup_and_block_rss_sites()
        self._block_system_subscribe = True
        self.__update_config()
        logger.info(f"已屏蔽系统订阅（仅115网盘）：{reason}")

    def _enter_unblocked(self, reason: str):
        """
        已恢复系统订阅：
        - 全量订阅 sites=UI站点
        - 还原系统 RssSites 备份（优先）；无备份时尽力设置系统默认订阅站点=UI站点
        - 从进入时刻计窗口，到期切回屏蔽
        """
        if not self._window_enabled():
            self._block_system_subscribe = True
            self.__update_config()
            self._enter_blocked(reason=f"{reason}（窗口禁用）")
            return

        self._ensure_toggle_scheduler()
        self._cancel_toggle_jobs()
        self._init_subscribe_handler()

        site_ids = self._resolve_site_ids(ids=self._unblock_site_ids, names=self._unblock_site_names)
        if not site_ids:
            self._block_system_subscribe = True
            self.__update_config()
            self._enter_blocked(reason=f"{reason}（站点解析失败）")
            return

        self._apply_sites_to_all_subscribes(site_ids, reason="已恢复系统订阅：全量同步站点")
        # 还原备份优先：有备份时还原 RssSites 原值，跳过默认站点尝试（二者不得互相覆盖）
        if not self._restore_rss_sites_backup():
            self._try_set_default_sites_for_unblocked(site_ids)

        self._block_system_subscribe = False
        self.__update_config()
        logger.info(f"已恢复系统订阅：站点={self._unblock_site_names} 窗口期={self._system_subscribe_window_hours}h（{reason}）")

        self._schedule_reblock_after_window()

    def _schedule_reblock_after_window(self):
        hours = float(self._system_subscribe_window_hours or 0)
        if hours <= 0:
            return

        tz = pytz.timezone(settings.TZ)
        now = datetime.datetime.now(tz=tz)
        run_date = now + datetime.timedelta(hours=hours)

        self._toggle_scheduler.add_job(
            func=lambda: self._enter_blocked(reason="窗口到期"),
            trigger="date",
            run_date=run_date,
            id="p115_reblock_job",
            replace_existing=True
        )
        logger.info(f"已安排：{run_date} 切换为已屏蔽系统订阅（仅115网盘）")

    def _schedule_unblock_after_delay(self, base_time: datetime.datetime):
        delay = int(self._unblock_delay_minutes)
        if delay < 0:
            return
        if not self._window_enabled():
            return

        self._ensure_toggle_scheduler()
        self._cancel_toggle_jobs()

        tz = pytz.timezone(settings.TZ)
        base_time = base_time.astimezone(tz)
        run_date = base_time + datetime.timedelta(minutes=delay)

        self._toggle_scheduler.add_job(
            func=lambda: self._enter_unblocked(reason="触发条件1：最后一次任务"),
            trigger="date",
            run_date=run_date,
            id="p115_unblock_job",
            replace_existing=True
        )
        logger.info(f"已安排：{run_date} 切换为已恢复系统订阅（延迟={delay}min）")

    # ------------------ 触发条件1：最后一次任务判断 ------------------

    def _is_last_run_today(self, run_start: datetime.datetime) -> bool:
        """判断当前运行是否是今天的最后一次任务"""
        try:
            tz = pytz.timezone(settings.TZ)
            run_start = run_start.astimezone(tz)
            trigger = CronTrigger.from_crontab(self._cron, timezone=tz)
            nxt = trigger.get_next_fire_time(None, run_start + datetime.timedelta(seconds=1))
            if not nxt:
                logger.debug(f"判断最后一次任务：无下次触发时间，返回 False")
                return False
            is_last = nxt.date() != run_start.date()
            logger.debug(f"判断最后一次任务：当前={run_start.strftime('%Y-%m-%d %H:%M')}, 下次={nxt.strftime('%Y-%m-%d %H:%M')}, 是否最后一次={is_last}")
            return is_last
        except Exception as e:
            logger.warning(f"判断是否当天最后一次触发失败：{e}，按 23:00 兜底")
            return run_start.hour == 23 and run_start.minute == 00

    # ------------------ 事件兜底：SubscribeAdded 保留，SubscribeModified 禁用写入 ------------------

    def _get_subscribe_id_from_event(self, event: Event) -> Optional[int]:
        if not event or not event.event_data:
            return None
        data = event.event_data or {}
        subscribe_id = data.get("subscribe_id") or data.get("id")
        if not subscribe_id and isinstance(data.get("subscribe"), dict):
            subscribe_id = data["subscribe"].get("id")
        try:
            return int(subscribe_id) if subscribe_id is not None else None
        except Exception:
            return None

    @eventmanager.register(EventType.SubscribeAdded)
    def on_subscribe_added(self, event: Event):
        """
        保留：新订阅兜底
        - 已屏蔽系统订阅时：新订阅必拉回仅115
        - 已恢复系统订阅时：新订阅同步窗口站点（保持一致）
        """
        sid = self._get_subscribe_id_from_event(event)
        if not sid:
            return
        if self._is_subscribe_excluded(sid):
            logger.info(f"新增订阅不在本插件处理范围（订阅过滤模式：{self._subscribe_filter_mode}），跳过站点同步（subscribe_id={sid}）")
            return
        try:
            self._init_subscribe_handler()

            if self._block_system_subscribe:
                if hasattr(self._subscribe_handler, "set_sites_for_subscribe_only_115"):
                    self._subscribe_handler.set_sites_for_subscribe_only_115(sid)
                else:
                    # 兜底：使用统一的 db session
                    with SessionFactory() as db:
                        site_id_115 = self._ensure_115_site_id(db)
                        SubscribeOper(db=db).update(sid, {"sites": [site_id_115]})
                logger.info(f"已屏蔽系统订阅：新增订阅已拉回仅115（subscribe_id={sid}）")
            else:
                if self._window_enabled() and hasattr(self._subscribe_handler, "set_sites_for_subscribe_by_names"):
                    self._subscribe_handler.set_sites_for_subscribe_by_names(sid, self._unblock_site_names)
                    logger.info(f"已恢复系统订阅：新增订阅已同步窗口站点（subscribe_id={sid})")

        except Exception as e:
            logger.error(f"SubscribeAdded 兜底失败：{e}")

    @eventmanager.register(EventType.SubscribeModified)
    def on_subscribe_modified(self, event: Event):
        """
        禁用：不再对 subscribe.modified 做拉回写入
        目的：用户手动修改订阅站点时，不再被自动拉回仅115
        """
        sid = self._get_subscribe_id_from_event(event)
        if not sid:
            return
        if self._block_system_subscribe:
            logger.info(f"已屏蔽系统订阅：检测到订阅改动，按规则不自动拉回（subscribe_id={sid}）")
        return

    # ------------------ init_plugin ------------------

    @staticmethod
    def _migrate_legacy_config():
        """
        首次运行一次性迁移：若新前缀(p115subsearch_)无配置而旧前缀(p115strgmsub_)有，
        则将旧插件「115网盘订阅追更」的配置复制到新前缀下（仅复制新插件支持的键）。
        """
        try:
            from app.core.plugin import PluginManager
            new_conf = PluginManager().get_plugin_config("P115SubSearch")
            if new_conf:
                return
            old_conf = PluginManager().get_plugin_config("P115StrgmSub")
            if not old_conf:
                return
            PluginManager().save_plugin_config("P115SubSearch", old_conf)
            logger.info("已自动迁移原「115网盘订阅追更」插件配置到「115网盘订阅搜索」")
        except Exception as e:
            logger.warning(f"旧插件配置迁移失败（不影响使用）: {e}")

    def init_plugin(self, config: dict = None):
        self._migrate_legacy_config()
        self.stop_service()
        self._ensure_toggle_scheduler()

        if config:
            self._enabled = config.get("enabled", False)

            self._cron = (config.get("cron", self._cron) or "").strip()
            # v1.7.3 恢复最小间隔校验（阈值 4 小时）：不足时回退默认 4 小时周期
            if self._cron:
                ok = self._cron_interval_ge_min_hours(self._cron, self._MIN_INTERVAL_HOURS)
                if not ok:
                    logger.warning(
                        f"Cron 过于频繁（要求间隔 >= {self._MIN_INTERVAL_HOURS}h）：{self._cron}，已回退默认 {self._FALLBACK_CRON}"
                    )
                    self._cron = self._FALLBACK_CRON

            self._notify = config.get("notify", False)
            self._onlyonce = config.get("onlyonce", False)
            self._cookies = config.get("cookies", "")

            self._pansou_enabled = config.get("pansou_enabled", True)
            self._pansou_url = config.get("pansou_url", "https://so.252035.xyz/")
            self._pansou_username = config.get("pansou_username", "")
            self._pansou_password = config.get("pansou_password", "")
            self._pansou_auth_enabled = config.get("pansou_auth_enabled", False)
            self._pansou_channels = config.get("pansou_channels", "QukanMovie")
            # PanSou 链接有效性检测层（全渠道，独立于 pansou_enabled 搜索渠道开关）
            self._pansou_check_enabled = config.get("pansou_check_enabled", True)
            try:
                self._max_transfer_links = max(1, int(config.get("max_transfer_links", 5) or 5))
            except (ValueError, TypeError):
                self._max_transfer_links = 5

            self._save_path = config.get("save_path", "/我的接收/MoviePilot/TV")
            self._movie_save_path = config.get("movie_save_path", "/我的接收/MoviePilot/Movie")
            self._only_115 = config.get("only_115", True)
            self._subscribe_filter_mode = config.get("subscribe_filter_mode", "exclude") or "exclude"
            self._exclude_subscribes = config.get("exclude_subscribes", []) or []
            self._include_subscribes = config.get("include_subscribes", []) or []
            if self._subscribe_filter_mode == "include":
                logger.info(f"订阅过滤模式：指定模式，仅处理 {len(self._include_subscribes)} 个勾选订阅")

            self._nullbr_enabled = config.get("nullbr_enabled", False)
            self._nullbr_appid = config.get("nullbr_appid", "")
            self._nullbr_api_key = config.get("nullbr_api_key", "")

            # KDocs 在线文档库配置
            self._kdocs_enabled = config.get("kdocs_enabled", False)
            self._kdocs_token = (config.get("kdocs_token", "") or "").strip()
            self._kdocs_doc_urls = config.get("kdocs_doc_urls", "") or ""
            self._kdocs_cache_ttl_hours = int(config.get("kdocs_cache_ttl_hours", 6) or 6)
            self._kdocs_batch_rows = int(config.get("kdocs_batch_rows", 1000) or 1000)
            self._kdocs_cookie = config.get("kdocs_cookie", "") or ""

            # 癫影（Dian115）配置（v1.8.0 / v1.8.1 增自动登录）
            self._dian115_enabled = config.get("dian115_enabled", False)
            self._dian115_email = (config.get("dian115_email", "") or "").strip()
            self._dian115_password = config.get("dian115_password", "") or ""
            self._dian115_auto_login = config.get("dian115_auto_login", True)
            self._dian115_browser_proxy = (config.get("dian115_browser_proxy", "") or "").strip()
            self._dian115_login_cron = (config.get("dian115_login_cron", "") or "").strip()
            # 账户卡片自动刷新间隔（v1.8.4；0=关闭）
            try:
                self._account_refresh_minutes = max(0, min(
                    int(config.get("account_refresh_minutes", 30) or 30), 1440
                ))
            except (TypeError, ValueError):
                self._account_refresh_minutes = 30
            self._dian115_token = (config.get("dian115_token", "") or "").strip()
            # 积分解锁配置（v1.8.2 修复：此前漏读，导致开关保存后被默认值覆盖，永远打不开）
            self._dian115_auto_unlock = config.get("dian115_auto_unlock", False)
            self._dian115_max_unlock_points = int(config.get("dian115_max_unlock_points", 50) or 50)
            self._dian115_max_points_per_sub = int(config.get("dian115_max_points_per_sub", 20) or 20)
            self._pinglian_enabled = bool(config.get("pinglian_enabled", False))
            self._pinglian_url = (config.get("pinglian_url", "") or "").strip()

            # 签到配置（v1.8.0）
            self._checkin_enabled = config.get("checkin_enabled", False)
            self._checkin_notify = config.get("checkin_notify", True)
            self._checkin_onlyonce = config.get("checkin_onlyonce", False)
            self._checkin_cron = (config.get("checkin_cron", "") or "").strip()
            self._p115_checkin_enabled = config.get("p115_checkin_enabled", True)
            self._dian115_checkin_enabled = config.get("dian115_checkin_enabled", False)
            self._dian115_checkin_mode = config.get("dian115_checkin_mode", "normal") or "normal"
            self._dian115_lottery_enabled = config.get("dian115_lottery_enabled", False)
            self._dian115_lottery_count = max(0, int(config.get("dian115_lottery_count", 0) or 0))

            self._max_transfer_per_sync = int(config.get("max_transfer_per_sync", 50) or 50)
            self._batch_size = int(config.get("batch_size", 20) or 20)
            self._skip_other_season_dirs = config.get("skip_other_season_dirs", True)

            # 搜索源优先级（兼容逗号分隔字符串）
            raw_order = config.get("search_source_order", []) or []
            if isinstance(raw_order, str):
                self._search_source_order = [x.strip() for x in raw_order.split(",") if x.strip()]
            else:
                self._search_source_order = list(raw_order)
            if self._search_source_order:
                logger.info(f"搜索源自定义优先级：{' > '.join(self._search_source_order)}")

            # UI新增配置
            self._unblock_site_ids = config.get("unblock_site_ids", []) or []
            raw_sites = config.get("unblock_site_names", self._unblock_site_names)
            if isinstance(raw_sites, str):
                self._unblock_site_names = [x.strip() for x in raw_sites.split(",") if x.strip()]
            else:
                self._unblock_site_names = raw_sites or []

            self._unblock_delay_minutes = int(config.get("unblock_delay_minutes", self._unblock_delay_minutes))
            self._system_subscribe_window_hours = float(
                config.get("unblock_window_hours", config.get("system_subscribe_window_hours", self._system_subscribe_window_hours))
            )

            self._block_system_subscribe = bool(config.get("block_system_subscribe", False))
            # RssSites 备份（屏蔽态持久化，恢复时还原）
            backup = config.get("rss_sites_backup")
            self._rss_sites_backup = list(backup) if isinstance(backup, (list, tuple)) and backup else None

            # 榜单订阅（v1.9.0 / v1.9.1）：榜单来源为 MoviePilot 推荐链方法名，
            # 只做浏览与订阅入口；抓取一律走缓存 / 后台刷新，不进搜索热路径。
            # v1.9.1 first-use：从未保存过榜单开关时默认开启；启用但未配置
            # （或配置项全部无效）来源时回落到安全默认来源（TMDB 流行趋势），
            # 避免「已启用却没有任何来源」的不可用空白状态。
            raw_leaderboard_enabled = config.get("leaderboard_enabled")
            if raw_leaderboard_enabled is None:
                raw_leaderboard_enabled = True
            self._leaderboard_enabled = bool(raw_leaderboard_enabled)
            self._leaderboard_sources = resolve_source_ids(
                config.get("leaderboard_sources"), enabled=self._leaderboard_enabled
            )
            try:
                self._leaderboard_page_size = max(1, min(int(config.get("leaderboard_page_size") or 20), 100))
            except (TypeError, ValueError):
                self._leaderboard_page_size = 20
            try:
                self._leaderboard_cache_minutes = max(1, min(int(config.get("leaderboard_cache_minutes") or 30), 1440))
            except (TypeError, ValueError):
                self._leaderboard_cache_minutes = 30
            try:
                self._leaderboard_refresh_minutes = max(0, min(int(config.get("leaderboard_refresh_minutes") or 0), 1440))
            except (TypeError, ValueError):
                self._leaderboard_refresh_minutes = 0

        # 初始化客户端/handlers
        self._init_clients()
        self._init_handlers()

        # 启动自愈：屏蔽态补写 RssSites=[-1]；非屏蔽态还原遗留备份
        self._selfheal_rss_sites_on_start()

        # 配置立即生效
        if self._block_system_subscribe:
            self._enter_blocked(reason="配置应用")
        else:
            # 用户手动关闭屏蔽：应用站点并取消窗口任务（不自动回弹）
            self._cancel_toggle_jobs()
            if self._unblock_site_names:
                site_ids = self._resolve_site_ids(ids=self._unblock_site_ids, names=self._unblock_site_names)
                if site_ids:
                    self._apply_sites_to_all_subscribes(site_ids, reason="用户关闭屏蔽：全量同步站点")
                    # 还原备份优先；无备份时回退默认站点尝试
                    if not self._restore_rss_sites_backup():
                        self._try_set_default_sites_for_unblocked(site_ids)
            else:
                # 未配置窗口站点也必须还原备份，不能把用户 RssSites 留在 [-1] 态
                self._restore_rss_sites_backup()
            self.__update_config()
            logger.info("用户已关闭屏蔽系统订阅（配置应用）")

        # 立即运行一次
        if self._enabled or self._onlyonce:
            if self._onlyonce:
                self._scheduler = BackgroundScheduler(timezone=settings.TZ)
                self._scheduler.add_job(
                    func=self.sync_subscribes,
                    trigger='date',
                    run_date=datetime.datetime.now(tz=pytz.timezone(settings.TZ)) + datetime.timedelta(seconds=3)
                )
                if self._scheduler.get_jobs():
                    self._scheduler.start()

            if self._onlyonce:
                self._onlyonce = False
                self.__update_config()

        # 立即签到一次（v1.8.0）：独立于订阅搜索的「立即运行」，
        # 因此放在 enabled/onlyonce 分支之外，单独按需创建调度器。
        if self._checkin_onlyonce:
            self._checkin_onlyonce = False
            self.__update_config()
            try:
                if self._scheduler is None:
                    self._scheduler = BackgroundScheduler(timezone=settings.TZ)
                self._scheduler.add_job(
                    func=self.run_checkin,
                    trigger='date',
                    run_date=datetime.datetime.now(tz=pytz.timezone(settings.TZ)) + datetime.timedelta(seconds=3),
                    kwargs={"manual": True}
                )
                if self._scheduler.get_jobs() and not self._scheduler.running:
                    self._scheduler.start()
            except Exception as e:
                logger.error(f"创建立即签到任务失败：{e}")

    # ------------------ init clients/handlers ------------------

    def _get_kdocs_data_dir(self):
        """KDocs 缓存目录：插件数据目录"""
        try:
            return self.get_data_path()
        except Exception:
            return None

    def _init_clients(self):
        """初始化客户端"""
        proxy = settings.PROXY
        if proxy:
            logger.info(f"使用 MoviePilot PROXY: {proxy}")

        # 只要配置了 PanSou 地址就初始化客户端（检测层独立于搜索渠道开关 pansou_enabled）
        if self._pansou_url:
            self._pansou_client = PanSouClient(
                base_url=self._pansou_url,
                username=self._pansou_username,
                password=self._pansou_password,
                auth_enabled=self._pansou_auth_enabled,
                proxy=proxy
            )

        self._pinglian_client = None
        if self._pinglian_enabled and self._pinglian_url:
            self._pinglian_client = PinglianClient(base_url=self._pinglian_url)

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

        # KDocs 在线文档库客户端初始化
        self._kdocs_client = None
        if self._kdocs_enabled:
            if not self._kdocs_token:
                logger.warning("KDocs 在线文档库已启用但未配置 Token，将无法使用 KDocs 查询功能")
            else:
                self._kdocs_client = KDocsClient(
                    token=self._kdocs_token,
                    doc_urls=self._kdocs_doc_urls,
                    cache_ttl_hours=self._kdocs_cache_ttl_hours,
                    batch_rows=self._kdocs_batch_rows,
                    cookie=self._kdocs_cookie,
                    data_dir=self._get_kdocs_data_dir(),
                    timeout=30
                )
                logger.info("KDocs 客户端初始化成功，启动后台数据预载")
                try:
                    self._kdocs_client.start_background_refresh()
                except Exception as exc:
                    logger.warning(f"KDocs 后台预载启动失败: {exc.__class__.__name__}")
        elif self._kdocs_cookie:
            logger.info("KDocs: 已配置 Cookie 但未启用在线文档库源，仅保存配置")

        if self._cookies:
            self._p115_manager = P115ClientManager(cookies=self._cookies)

        # 癫影（Dian115）客户端：搜索源与签到共用同一实例（v1.8.0）
        self._init_dian115_client(proxy)

    def _init_dian115_client(self, proxy=None):
        """初始化癫影（Dian115）客户端。

        搜索与签到共用同一实例，登录态通过插件数据持久化复用。

        认证方式（v1.8.1 起双通道）：
            1. **自动登录**（推荐）：填写邮箱+密码并保持「自动登录」开启，
               插件用内置 cloakbrowser 本地解 Cloudflare Turnstile，
               全自动登录，无需任何手工操作；
            2. **手工 Token**（兜底）：粘贴浏览器 Cookie 中的
               ``__Host-portal_token``，环境缺浏览器时仍可用（约 24 小时）。
        """
        self._dian115_client = None
        need_dian115 = self._dian115_enabled or self._dian115_checkin_enabled
        if not need_dian115:
            return

        if not self._dian115_email or not self._dian115_password:
            # 无账号密码时只能靠手工 Token；两者都没有则不必创建实例
            if not self._dian115_token:
                logger.warning(
                    "癫影已启用（搜索或签到）但既未配置账号密码、也未配置手工 Token，"
                    "无法建立会话；请在「癫影」页签填写账号密码（推荐）或粘贴 __Host-portal_token"
                )
                return
            if self._dian115_auto_login:
                logger.info("癫影未配置账号密码，本实例仅使用手工 Token 认证")

        try:
            self._dian115_client = Dian115Client(
                email=self._dian115_email,
                password=self._dian115_password,
                token=self._dian115_token,
                auto_login=self._dian115_auto_login,
                browser_proxy=self._dian115_browser_proxy or None,
                proxy=proxy,
                get_data_func=self.get_data,
                save_data_func=self.save_data
            )
        except Exception as exc:
            logger.error(f"癫影客户端初始化失败: {exc.__class__.__name__} - {exc}")
            self._dian115_client = None
            return

        if getattr(self._dian115_client, "error_type", None):
            logger.warning(
                f"癫影客户端缺少依赖 {self._dian115_client.error_type}，"
                f"请在插件依赖中安装后重试"
            )
            self._dian115_client = None
            return

        # 认证路径自检：账号密码 + 自动登录 或 手工 Token，二者至少有一个
        status = self._dian115_client.login_status()
        if status.get("has_token"):
            remaining = status.get("token_remaining_hours")
            if remaining:
                logger.info(f"癫影（Dian115）客户端就绪：使用手工 Token，剩余约 {remaining} 小时")
            else:
                logger.info("癫影（Dian115）客户端就绪：使用手工 Token")
        elif self._dian115_email and self._dian115_password and self._dian115_auto_login:
            if status.get("browser_available"):
                logger.info(
                    "癫影（Dian115）客户端就绪：自动登录已启用（本地解 Cloudflare 验证，"
                    "首次登录约需 10 秒）"
                )
            else:
                logger.warning(
                    "癫影自动登录不可用（%s），且未配置手工 Token；"
                    "癫影相关功能会失败。请改用 MoviePilot 环境或手工粘贴 __Host-portal_token"
                    % (status.get("browser_error") or "未知原因")
                )
        else:
            logger.warning(
                "癫影（Dian115）客户端已创建，但缺少可用认证：请填写账号密码并开启自动登录，"
                "或粘贴 __Host-portal_token"
            )

    def _init_subscribe_handler(self):
        self._subscribe_handler = SubscribeHandler(
            exclude_subscribes=self._exclude_subscribes,
            notify=self._notify,
            post_message_func=self.post_message,
            is_excluded_func=self._is_subscribe_excluded
        )

    def _init_handlers(self):
        self._init_subscribe_handler()

        self._search_handler = SearchHandler(
            pansou_client=self._pansou_client,
            nullbr_client=self._nullbr_client,
            pansou_enabled=self._pansou_enabled,
            nullbr_enabled=self._nullbr_enabled,
            only_115=self._only_115,
            pansou_channels=self._pansou_channels,
            search_source_order=self._search_source_order,
            kdocs_client=self._kdocs_client,
            kdocs_enabled=self._kdocs_enabled,
            dian115_client=self._dian115_client,
            dian115_enabled=self._dian115_enabled,
            dian115_auto_unlock=self._dian115_auto_unlock,
            dian115_max_unlock_points=self._dian115_max_unlock_points,
            dian115_max_points_per_sub=self._dian115_max_points_per_sub,
            pinglian_client=self._pinglian_client,
            pinglian_enabled=self._pinglian_enabled
        )
        # 设置持久化函数，用于保存订阅的历史积分花费
        self._search_handler.set_data_funcs(self.get_data, self.save_data)

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
            save_data_func=self.save_data,
            pansou_client=self._pansou_client,
            pansou_check_enabled=self._pansou_check_enabled,
            max_transfer_links=self._max_transfer_links
        )

        self._checkin_handler = CheckinHandler(
            p115_manager=self._p115_manager,
            dian115_client=self._dian115_client,
            p115_checkin_enabled=self._p115_checkin_enabled,
            dian115_checkin_enabled=self._dian115_checkin_enabled,
            dian115_checkin_mode=self._dian115_checkin_mode,
            dian115_lottery_enabled=self._dian115_lottery_enabled,
            dian115_lottery_count=self._dian115_lottery_count,
            notify_func=self._notify_checkin,
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

        # v1.9.0：榜单订阅（缓存 + 订阅入口）与盘链能力（只读识别/校验）
        # 订阅链不在此处 new：LeaderboardHandler 内部按需构造 SubscribeChain()，
        # 与 handlers/subscribe.py 的既有用法保持一致（PluginChian 不暴露 subscribe）
        self._leaderboard_client = LeaderboardClient(
            cache_ttl_seconds=max(1, self._leaderboard_cache_minutes) * 60,
            page_size=self._leaderboard_page_size
        )
        self._leaderboard_handler = LeaderboardHandler(
            client=self._leaderboard_client,
            subscribe_oper=SubscribeOper(),
            get_data_func=self.get_data,
            save_data_func=self.save_data
        )
        self._share_link_handler = ShareLinkHandler(p115_manager=self._p115_manager)

    # ------------------ 配置写回 ------------------

    def __update_config(self):
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
            "pansou_check_enabled": self._pansou_check_enabled,
            "max_transfer_links": self._max_transfer_links,
            "nullbr_enabled": self._nullbr_enabled,
            "nullbr_appid": self._nullbr_appid,
            "nullbr_api_key": self._nullbr_api_key,
            # KDocs 配置
            "kdocs_enabled": self._kdocs_enabled,
            "kdocs_token": self._kdocs_token,
            "kdocs_doc_urls": self._kdocs_doc_urls,
            "kdocs_cache_ttl_hours": self._kdocs_cache_ttl_hours,
            "kdocs_batch_rows": self._kdocs_batch_rows,
            "kdocs_cookie": self._kdocs_cookie,
            # 癫影配置（v1.8.0 / v1.8.1 增自动登录）
            "dian115_enabled": self._dian115_enabled,
            "dian115_email": self._dian115_email,
            "dian115_password": self._dian115_password,
            "dian115_auto_login": self._dian115_auto_login,
            "dian115_browser_proxy": self._dian115_browser_proxy,
            "dian115_login_cron": self._dian115_login_cron,
            "account_refresh_minutes": self._account_refresh_minutes,
            "dian115_token": self._dian115_token,
            "dian115_auto_unlock": self._dian115_auto_unlock,
            "dian115_max_unlock_points": self._dian115_max_unlock_points,
            "dian115_max_points_per_sub": self._dian115_max_points_per_sub,
            # 签到配置（v1.8.0）
            "checkin_enabled": self._checkin_enabled,
            "checkin_notify": self._checkin_notify,
            "checkin_onlyonce": self._checkin_onlyonce,
            "checkin_cron": self._checkin_cron,
            "p115_checkin_enabled": self._p115_checkin_enabled,
            "dian115_checkin_enabled": self._dian115_checkin_enabled,
            "dian115_checkin_mode": self._dian115_checkin_mode,
            "dian115_lottery_enabled": self._dian115_lottery_enabled,
            "dian115_lottery_count": self._dian115_lottery_count,
            # 榜单订阅（v1.9.0）
            "leaderboard_enabled": bool(self._leaderboard_enabled),
            "leaderboard_sources": list(self._leaderboard_sources or []),
            "leaderboard_page_size": int(self._leaderboard_page_size),
            "leaderboard_cache_minutes": int(self._leaderboard_cache_minutes),
            "leaderboard_refresh_minutes": int(self._leaderboard_refresh_minutes),
            # 其他配置
            "search_source_order": self._search_source_order,
            "pinglian_enabled": self._pinglian_enabled,
            "pinglian_url": self._pinglian_url,
            "subscribe_filter_mode": self._subscribe_filter_mode,
            "exclude_subscribes": self._exclude_subscribes,
            "include_subscribes": self._include_subscribes,
            "block_system_subscribe": self._block_system_subscribe,
            "max_transfer_per_sync": self._max_transfer_per_sync,
            "batch_size": self._batch_size,
            "skip_other_season_dirs": self._skip_other_season_dirs,
            "unblock_site_ids": self._unblock_site_ids,
            "unblock_site_names": self._unblock_site_names,
            "unblock_delay_minutes": self._unblock_delay_minutes,
            "system_subscribe_window_hours": self._system_subscribe_window_hours,
            "unblock_window_hours": self._system_subscribe_window_hours,
            "rss_sites_backup": self._rss_sites_backup,
        })

    # ------------------ 签到（v1.8.0） ------------------

    def _notify_checkin(self, text: str) -> None:
        """签到结果通知（受「签到通知」开关控制）。"""
        if not self._checkin_notify:
            return
        try:
            self.post_message(
                mtype=NotificationType.Plugin,
                title="【115网盘搜索助手】签到",
                text=text
            )
        except Exception as e:
            logger.warning(f"签到通知发送失败: {e}")

    def run_checkin(self, providers: Optional[List[str]] = None, manual: bool = False) -> Dict[str, Any]:
        """
        执行一次签到

        :param providers: 指定签到源（["p115"] / ["dian115"]）；为空时按配置自动判定
        :param manual: 是否手动触发（手动触发不受 checkin_enabled 限制）
        :return: CheckinHandler.run_checkin 的结果
        """
        empty = {"success": False, "total": 0, "success_count": 0, "fail_count": 0, "records": []}

        if not manual and not self._checkin_enabled:
            logger.info("签到未启用，跳过")
            return empty

        if not self._checkin_handler:
            logger.warning("签到处理器未初始化，跳过签到")
            return empty

        try:
            result = self._checkin_handler.run_checkin(providers)
        except Exception as e:
            logger.error(f"签到执行异常：{e}")
            return empty

        logger.info(
            f"签到完成：成功 {result.get('success_count')} 项，失败 {result.get('fail_count')} 项"
        )
        # 用签到结果回写账户快照，避免配置页为拿最新积分再请求一次第三方（v1.8.3）
        self._sync_account_snapshot_after_checkin(result)
        return result

    def _sync_account_snapshot_after_checkin(self, result: Dict[str, Any]) -> None:
        """
        签到完成后，用结果更新账户快照的积分与签到天数（v1.8.3）。

        只改本地快照，**不发任何网络请求**。这样配置页无需为了拿最新积分
        再打一次第三方接口。
        """
        try:
            for record in list((result or {}).get("records") or []):
                if not isinstance(record, dict):
                    continue
                provider = str(record.get("provider") or "").strip().lower()
                if not record.get("success"):
                    continue
                if provider == "dian115":
                    self.update_account_points(
                        self.ACCOUNT_KEY_DIAN115,
                        record.get("points"),
                        record.get("days"),
                    )
                elif provider == "p115":
                    self.update_account_points(
                        self.ACCOUNT_KEY_P115,
                        record.get("points"),
                        None,
                    )
        except Exception as error:
            logger.debug(f"签到后回写账户快照失败：{error}")

    def checkin_service(self):
        """签到服务入口（供 get_service 注册独立周期使用）。"""
        try:
            self.run_checkin()
        except Exception as e:
            logger.error(f"签到服务异常：{e}")

    # ------------------ stop ------------------

    def stop_service(self):
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    # wait=False：避免在自身 job 线程上 join() 自己导致永久冻结
                    self._scheduler.shutdown(wait=False)
                self._scheduler = None
        except Exception:
            pass

        try:
            if self._toggle_scheduler:
                self._toggle_scheduler.remove_all_jobs()
                if self._toggle_scheduler.running:
                    self._toggle_scheduler.shutdown(wait=False)
                self._toggle_scheduler = None
        except Exception:
            pass

        try:
            if getattr(self, "_checkin_scheduler", None):
                self._checkin_scheduler.remove_all_jobs()
                if self._checkin_scheduler.running:
                    self._checkin_scheduler.shutdown(wait=False)
                self._checkin_scheduler = None
        except Exception:
            pass

    # ======================================================================
    # 必备：get_state / get_form / get_page / get_api / get_service
    # ======================================================================

    def get_state(self) -> bool:
        return self._enabled

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        return UIConfig.get_form(self._collect_account_status())

    # ==================================================================
    # 账户信息卡片（v1.8.3，移植自网盘搜索助手）
    #
    # 产出统一的账户卡片契约，与网盘搜索助手的 AccountInfo 组件一一对应：
    #     {
    #       "connected": bool,
    #       "user": {"name", "avatar", "membership_supported", "badge",
    #                "is_vip", "vip_label", "vip_expire_date",
    #                "is_forever_vip"},
    #       "points": {"label", "available"},
    #       "storage": {"used", "total", "remaining"},   # 仅网盘
    #       "details": [{"label", "value"}, ...],
    #       "error": str,        # connected=False 时给用户看的提示
    #       "refreshed_at": int, # 快照时间戳
    #     }
    #
    # 数据来源分三条路：
    #   1. _cached_account_status() —— 配置页专用，**零第三方请求**；
    #   2. _account_info(key, refresh) —— 手动刷新 / 任务路径，带缓存与冷却；
    #   3. update_account_points() —— 签到结果回写快照，避免二次请求。
    # ==================================================================

    # 账户卡片标识：与网盘搜索助手同构（category:source）
    ACCOUNT_KEY_P115 = "drive:p115"
    ACCOUNT_KEY_DIAN115 = "search:dian115"

    # 卡片快照落盘键前缀
    ACCOUNT_SNAPSHOT_PREFIX = "account_snapshot:"

    @staticmethod
    def _normalize_account_key(account_key: str) -> str:
        """校验并规范化账户卡片标识。"""
        normalized = str(account_key or "").strip().lower()
        if ":" not in normalized:
            raise ValueError("账户卡片标识无效")
        category, source = normalized.split(":", 1)
        if category not in {"drive", "search"} or not source:
            raise ValueError("账户卡片标识无效")
        return normalized

    @staticmethod
    def _account_card_placeholder(error: str) -> Dict[str, Any]:
        """未连接态卡片。"""
        return {
            "connected": False,
            "user": {},
            "points": {},
            "details": [],
            "error": str(error or "请填写登录凭证并保存配置"),
        }

    def _p115_account_card(self, silent: bool = False) -> Dict[str, Any]:
        """把 115 账户信息转换为通用卡片契约。

        :param silent: 透传给客户端，后台定时刷新时只写 debug 日志。
        """
        manager = self._p115_manager
        if manager is None:
            return self._account_card_placeholder("115 客户端未初始化，请检查 Cookie 配置")
        if not getattr(manager, "client", None):
            has_cookie = bool((self._cookies or "").strip())
            return self._account_card_placeholder(
                "115 客户端不可用（依赖缺失）" if has_cookie else "请填写 115 Cookie 并保存配置"
            )

        raw_cookie = (self._cookies or "").strip()
        missing_keys = [
            key for key in ("UID", "CID", "SEID", "KID")
            if not (raw_cookie and f"{key}=" in raw_cookie)
        ]
        if missing_keys:
            return self._account_card_placeholder(
                f"115 Cookie 不完整，缺少：{'/'.join(missing_keys)}"
            )

        info = manager.get_account_info(silent=silent)
        if not isinstance(info, dict) or not info.get("connected"):
            message = info.get("error") if isinstance(info, dict) else None
            return self._account_card_placeholder(
                str(message or "115 账户信息读取失败，请检查 Cookie 或稍后重试")
            )

        name = str(info.get("name") or "115 用户").strip()
        vip_name = str(info.get("vip_name") or "").strip()
        is_vip = bool(info.get("vip"))
        remain_days = info.get("expire")
        if is_vip and remain_days:
            vip_label = f"{vip_name or 'VIP'}（剩余 {int(remain_days)} 天）"
        elif is_vip:
            vip_label = vip_name or "VIP"
        else:
            vip_label = ""

        details: List[Dict[str, str]] = []

        def add_detail(label: str, value: Any) -> None:
            text = str(value if value is not None else "").strip()
            if text and text != "0":
                details.append({"label": label, "value": text})

        add_detail("会员状态", vip_label or ("VIP" if is_vip else "普通用户"))
        add_detail("用户 ID", info.get("user_id"))
        add_detail("状态更新时间", time.strftime(
            "%Y-%m-%d %H:%M:%S", time.localtime()
        ))

        storage = {
            "used": _human_size(info.get("space_used")),
            "total": _human_size(info.get("space_total")),
        }
        if storage["used"] == "—":
            storage = {}

        return {
            "connected": True,
            "user": {
                "name": name,
                "avatar": "",
                "membership_supported": True,
                "badge": "",
                "is_vip": is_vip,
                "vip_label": vip_label,
                "vip_expire_date": "",
                "is_forever_vip": False,
            },
            "points": {},
            "storage": storage,
            "details": details,
            "refreshed_at": int(time.time()),
        }

    def _dian115_account_card(self, allow_browser_login: bool = True) -> Dict[str, Any]:
        """把癫影账户信息转换为通用卡片契约。

        :param allow_browser_login: token 过期时是否允许触发浏览器自动登录。
            手动刷新（前端 API）必须传 False——浏览器登录可能耗时 30 秒以上，
            会让 HTTP 请求超时；后台自动刷新服务传 True，兼做会话保活。
        """
        client = self._dian115_client
        if client is None:
            if not self._dian115_enabled and not self._dian115_checkin_enabled:
                return self._account_card_placeholder("癫影未启用")
            if not self._dian115_email or not self._dian115_password:
                return self._account_card_placeholder("请填写癫影邮箱和密码并保存配置")
            return self._account_card_placeholder(
                "癫影客户端未初始化（缺少账号密码或浏览器环境不可用）"
            )

        status = client.login_status() or {}
        if not status.get("has_token") and not status.get("auto_login"):
            return self._account_card_placeholder("癫影未登录，请填写账号密码或手工 Token")

        try:
            info = client.get_account_info(allow_browser_login=allow_browser_login)
        except Exception as error:
            if allow_browser_login:
                raise
            return self._account_card_placeholder(
                "癫影登录态已过期，插件会自动重新登录，请稍后再刷新或等待自动刷新"
            )
        if not isinstance(info, dict):
            return self._account_card_placeholder("癫影账户信息读取失败，请稍后重试")
        if info.get("error") and not info.get("points") and not info.get("name"):
            return self._account_card_placeholder(str(info.get("error")))

        name = str(info.get("name") or "").strip()
        email = str(info.get("email") or "").strip().lower()
        if not name or "@" in name or (email and name.lower() == email):
            name = "Dian115 用户"

        details: List[Dict[str, str]] = []

        def add_detail(label: str, value: Any) -> None:
            text = str(value if value is not None else "").strip()
            if text:
                details.append({"label": label, "value": text})

        add_detail("会员状态", "VIP" if info.get("is_vip") else "普通用户")
        add_detail("连续签到", f"{int(info.get('consecutive_signin') or 0)} 天")
        add_detail("已解锁", f"{int(info.get('unlock_count') or 0)} 次")

        auth_mode = "手工 Token"
        if not status.get("has_token"):
            auth_mode = "自动登录"
        add_detail("认证方式", auth_mode)

        return {
            "connected": True,
            "user": {
                "name": name,
                "avatar": str(info.get("avatar") or ""),
                "membership_supported": True,
                "badge": str(info.get("role") or "").strip(),
                "is_vip": bool(info.get("is_vip")),
                "vip_label": str(info.get("vip_until") or "").strip(),
                "vip_expire_date": "",
                "is_forever_vip": False,
            },
            "points": {
                "label": "可用积分",
                "available": max(0, int(info.get("points") or 0)),
            },
            "details": details,
            "refreshed_at": int(time.time()),
        }

    # ---- 快照读写（独立数据库键，与网盘搜索助手 load_account/save_account 同义） ----

    def _load_account_snapshot(self, account_key: str) -> Dict[str, Any]:
        """读取账户快照（内存缓存 → 落盘快照 → 空）。"""
        try:
            normalized = self._normalize_account_key(account_key)
        except ValueError:
            return {}
        cached = _account_cache_get(normalized)
        if cached:
            return cached
        try:
            stored = self.get_data(f"{self.ACCOUNT_SNAPSHOT_PREFIX}{normalized}")
        except Exception as error:
            logger.debug(f"读取账户快照失败（{normalized}）：{error}")
            return {}
        if isinstance(stored, dict) and stored:
            _account_cache_set(normalized, stored)
            return stored
        return {}

    def _save_account_snapshot(self, account_key: str, account: Dict[str, Any]) -> None:
        """写入账户快照（内存缓存 + 落盘）。"""
        try:
            normalized = self._normalize_account_key(account_key)
        except ValueError:
            return
        _account_cache_set(normalized, account)
        try:
            self.save_data(
                f"{self.ACCOUNT_SNAPSHOT_PREFIX}{normalized}",
                copy.deepcopy(account),
            )
        except Exception as error:
            logger.debug(f"保存账户快照失败（{normalized}）：{error}")

    # ---- 核心：读卡片（带缓存与冷却） ----

    def _load_account(
            self, account_key: str, allow_browser_login: bool = True,
            silent: bool = False,
    ) -> Dict[str, Any]:
        """真正去取一次账户信息（会发网络请求）。"""
        normalized = self._normalize_account_key(account_key)
        if normalized == self.ACCOUNT_KEY_P115:
            return self._p115_account_card(silent=silent)
        if normalized == self.ACCOUNT_KEY_DIAN115:
            return self._dian115_account_card(allow_browser_login=allow_browser_login)
        raise ValueError(f"不支持的账户卡片：{normalized}")

    def _account_info(
            self, account_key: str, refresh: bool = False,
            allow_browser_login: bool = True, silent: bool = False,
    ) -> Tuple[Dict[str, Any], bool]:
        """
        读取单卡片信息，使用独立快照并实施刷新冷却。

        :param allow_browser_login: 癫影 token 过期时是否允许浏览器自动登录
            （手动刷新必须 False，避免 HTTP 长阻塞；后台刷新用 True）。
        :param silent: True 时不输出 info 级日志（后台/页面自动刷新用）。
        :return: (account, limited)；limited=True 表示因冷却未真正刷新。
            **异常路径不覆盖既有快照**——临时网络抖动不应把好快照冲成占位卡。
        """
        normalized = self._normalize_account_key(account_key)
        snapshot = self._load_account_snapshot(normalized)

        if refresh and _account_guard_active(normalized):
            return snapshot or self._account_card_placeholder("账户信息正在冷却，请稍后再刷新"), True
        if not refresh and snapshot:
            return snapshot, False

        _account_guard_arm(normalized)
        try:
            account = self._load_account(
                normalized, allow_browser_login=allow_browser_login, silent=silent
            )
        except Exception as error:
            logger.debug(f"读取账户信息失败（{normalized}）：{error}")
            placeholder = self._account_card_placeholder(
                "账户信息读取失败，请检查登录凭据或稍后重试"
            )
            placeholder["refreshed_at"] = int(time.time())
            return placeholder, False
        account["refreshed_at"] = int(time.time())
        # 只有成功拿到已连接卡片才覆盖快照：网络抖动/登录态过期等
        # 失败结果一律不落盘，避免把好快照冲成占位卡
        if account.get("connected"):
            self._save_account_snapshot(normalized, account)
        return account, False

    def _cached_account_status(self) -> Dict[str, Any]:
        """
        配置页专用只读路径：只读内存缓存与落盘快照，**绝不访问第三方接口**。

        翻配置页是高频操作，绝不能触发浏览器冷启动或 HTTP 请求。
        没有快照时返回占位卡片，由用户点「刷新」按钮主动拉取。
        """
        cards: List[Dict[str, Any]] = []
        for account_key, title, placeholder in (
            (self.ACCOUNT_KEY_P115, "115 网盘",
             self._p115_account_card_placeholder()),
            (self.ACCOUNT_KEY_DIAN115, "癫影",
             self._dian115_account_card_placeholder()),
        ):
            account = self._load_account_snapshot(account_key)
            if not account:
                account = placeholder
                account.setdefault("refreshed_at", 0)
            cards.append({"key": account_key, "title": title, "account": account})
        return {"cards": cards}

    def _p115_account_card_placeholder(self) -> Dict[str, Any]:
        """115 卡片的未连接占位（不发请求）。"""
        if self._p115_manager is None:
            return self._account_card_placeholder("115 客户端未初始化，请检查 Cookie 配置")
        if not getattr(self._p115_manager, "client", None):
            has_cookie = bool((self._cookies or "").strip())
            return self._account_card_placeholder(
                "115 客户端不可用（依赖缺失）" if has_cookie else "请填写 115 Cookie 并保存配置"
            )
        return self._account_card_placeholder("点击右上角刷新获取 115 账户信息")

    def _dian115_account_card_placeholder(self) -> Dict[str, Any]:
        """癫影卡片的未连接占位（不发请求）。"""
        if self._dian115_client is None:
            if not self._dian115_enabled and not self._dian115_checkin_enabled:
                return self._account_card_placeholder("癫影未启用")
            if not self._dian115_email or not self._dian115_password:
                return self._account_card_placeholder("请填写癫影邮箱和密码并保存配置")
            return self._account_card_placeholder("癫影客户端未初始化（浏览器环境不可用）")
        return self._account_card_placeholder("点击右上角刷新获取癫影账户信息")

    def _collect_account_status(self) -> Dict[str, Any]:
        """
        采集账户卡片，供配置页渲染（v1.8.3 改为网盘搜索助手的卡片契约）。

        **只读本地快照，不发网络请求**。任何异常都降级为占位卡片，
        绝不允许影响配置表单渲染。
        """
        try:
            return self._cached_account_status()
        except Exception as error:
            logger.debug(f"采集账户状态失败：{error}")
            return {"cards": []}

    def refresh_account(
            self, account_key: str, allow_browser_login: bool = False
    ) -> Tuple[Dict[str, Any], bool]:
        """
        同步刷新单个账户卡片（供 API 路由用 asyncio.to_thread 调用）。

        默认 allow_browser_login=False：手动刷新是同步 HTTP 请求，
        癫影浏览器自动登录可能耗时 30 秒以上导致前端超时；
        token 过期时返回友好提示，由自动刷新服务负责重新登录。
        """
        account, limited = self._account_info(
            account_key, True, allow_browser_login=allow_browser_login
        )
        normalized = self._normalize_account_key(account_key)
        # 快照变化后清掉配置页的 options 缓存，下次打开即可见新值
        try:
            clear_account_cache(normalized)
        except Exception:
            pass
        return account, limited

    def update_account_points(
            self, account_key: str, points: Any, signin_days: Any = None
    ) -> bool:
        """
        用签到结果更新账户快照，避免配置页重复请求第三方接口。

        只改 points.available 与 details 里的「累计签到 / 连续签到」项。
        """
        try:
            normalized = self._normalize_account_key(account_key)
        except ValueError:
            return False
        try:
            normalized_points = max(0, int(points))
        except (TypeError, ValueError):
            return False
        try:
            normalized_days = (
                max(0, int(signin_days)) if signin_days is not None else None
            )
        except (TypeError, ValueError):
            normalized_days = None

        account = self._load_account_snapshot(normalized)
        if not isinstance(account, dict) or not account.get("connected"):
            return False

        account = copy.deepcopy(account)
        point_info = dict(account.get("points") or {})
        point_info.setdefault("label", "可用积分")
        point_info["available"] = normalized_points
        account["points"] = point_info

        if normalized_days is not None:
            details = list(account.get("details") or [])
            for item in details:
                if (
                        isinstance(item, dict)
                        and item.get("label") in {"累计签到", "连续签到"}
                ):
                    item["value"] = f"{normalized_days} 天"
                    break
            account["details"] = details

        account["refreshed_at"] = int(time.time())
        self._save_account_snapshot(normalized, account)
        return True

    def api_refresh_account(self, key: str = "") -> dict:
        """
        手动刷新单个账户信息卡片（插件 API 路由，POST）。

        鉴权由 MoviePilot 统一的 verify_apikey 依赖完成（plugin.py L158-167），
        前端 events.click.api 调用不携带 apikey，此处不再重复校验。
        """
        account_key = str(key or "").strip()
        if not account_key:
            return {"success": False, "message": "缺少账户卡片标识"}
        try:
            # 手动刷新禁止触发浏览器自动登录（可能 30s+，HTTP 会超时）
            account, limited = self.refresh_account(account_key)
        except ValueError as error:
            return {"success": False, "message": str(error)}
        except Exception as error:
            logger.error(f"刷新账户信息失败：{error}")
            return {"success": False, "message": f"刷新账户信息失败：{error}"}
        normalized = self._normalize_account_key(account_key)
        error_text = str(account.get("error") or "").strip()
        if error_text and not account.get("connected"):
            return {"success": False, "message": error_text, "data": {"key": normalized}}
        return {
            "success": True,
            "message": "刷新过于频繁，已显示最近一次账户信息" if limited else "账户信息已刷新",
            "data": {
                "key": normalized,
                "account": account,
                "limited": limited,
            },
        }

    # ------------------ 榜单订阅 / 盘链 API（v1.9.0） ------------------

    def api_leaderboard_browse(self, source: str = "", page: int = 1,
                               refresh: bool = False) -> dict:
        """浏览榜单：默认读缓存，refresh=true 才回源（仍由客户端自降级）。"""
        if not self._leaderboard_handler:
            return {"success": False, "message": "榜单功能未初始化，请重启插件后重试",
                    "data": {"items": [], "empty": True}}
        return self._leaderboard_handler.browse({
            "source": source, "page": page, "refresh": refresh,
        })

    def api_leaderboard_subscribe(self, payload: Optional[dict] = None) -> dict:
        """订阅榜单条目：复用 MoviePilot 原生 SubscribeChain 完成识别与建订。"""
        if not self._leaderboard_handler:
            return {"success": False, "message": "榜单功能未初始化，请重启插件后重试",
                    "data": {"results": []}}
        return self._leaderboard_handler.subscribe(payload or {})

    def api_resolve_share_link(self, text: str = "") -> dict:
        """解析并校验 115 分享链接；响应不包含访问码明文。"""
        if not self._share_link_handler:
            return {"success": False, "message": "盘链功能未初始化，请重启插件后重试",
                    "data": {"status": "error", "is_share_link": False}}
        return self._share_link_handler.resolve(text or "")

    def api_refresh_leaderboard(self) -> dict:
        """手动刷新榜单快照（v1.9.0）。

        榜单是慢源，**绝不在请求线程里同步抓取**：这里只把一个一次性任务丢进
        后台调度器后立即返回，抓取结果由后台写入本地快照，用户稍后重新打开
        数据页即可看到。调度失败也返回结构化中文提示，不抛异常。
        """
        if not self._leaderboard_handler:
            return {"success": False, "message": "榜单功能未初始化，请重启插件后重试",
                    "data": {"scheduled": False}}
        if not self._leaderboard_enabled:
            return {"success": False,
                    "message": "榜单订阅未启用，请先在插件设置页「榜单」页签打开「启用榜单订阅」并保存",
                    "data": {"scheduled": False}}
        if not self._leaderboard_sources:
            return {"success": False,
                    "message": "尚未勾选榜单来源，请先在「榜单」页签选择来源并保存配置",
                    "data": {"scheduled": False}}
        try:
            self._ensure_toggle_scheduler()
            self._toggle_scheduler.add_job(
                func=self._refresh_leaderboard_snapshot,
                trigger="date",
                run_date=datetime.datetime.now(pytz.timezone(settings.TZ)),
                id="p115_leaderboard_refresh_job",
                replace_existing=True
            )
        except Exception as error:  # noqa: BLE001
            logger.warning(f"榜单刷新任务调度失败：{error}")
            return {"success": False, "message": f"榜单刷新任务调度失败：{error}",
                    "data": {"scheduled": False}}
        return {"success": True, "message": "已提交榜单刷新任务，请稍后重新打开本页查看结果",
                "data": {"scheduled": True}}

    def _refresh_leaderboard_snapshot(self) -> Optional[Dict[str, Any]]:
        """后台预热榜单缓存（v1.9.0）。

        只在后台调度器 / 独立 cron 服务里跑：外部榜单源是慢源，**严禁**进入
        订阅/搜索同步热路径。任何异常都在 handler 内部消化，这里再兜一层，
        绝不影响主流程。返回值仅供手动刷新的 API 汇总，cron 服务忽略它。
        """
        if not self._leaderboard_handler:
            return None
        sources = list(self._leaderboard_sources or [])
        if not sources:
            return None
        try:
            return self._leaderboard_handler.refresh_snapshot(sources)
        except Exception as error:  # noqa: BLE001
            logger.debug(f"榜单后台刷新失败：{error}")
            return None

    def _build_leaderboard_service(self) -> List[Dict[str, Any]]:
        """榜单后台刷新服务：仅当开启榜单且刷新间隔 > 0 时注册。"""
        if not self._leaderboard_enabled or self._leaderboard_refresh_minutes <= 0:
            return []
        try:
            return [{
                "id": "P115SubSearchLeaderboard",
                "name": "115网盘订阅搜索 榜单刷新服务",
                "trigger": "interval",
                "func": self._refresh_leaderboard_snapshot,
                "kwargs": {"minutes": self._leaderboard_refresh_minutes}
            }]
        except Exception as error:  # noqa: BLE001
            logger.warning(f"榜单刷新服务注册失败：{error}")
            return []

    def _refresh_p115_account_snapshot(self) -> None:
        """
        登录校验通过后刷新 115 账户快照，供配置页展示（v1.8.3）。

        复用刚完成的 check_login 结果，因此不会额外引入一次登录请求。
        """
        try:
            account = self._p115_account_card()
            if not account.get("connected"):
                return
            self._save_account_snapshot(self.ACCOUNT_KEY_P115, account)
        except Exception as error:
            logger.debug(f"刷新 115 账户快照失败：{error}")

    def _leaderboard_page_state(self) -> Dict[str, Any]:
        """数据页榜单卡片所需的**非敏感**状态（v1.9.1）。

        只输出开关、来源 id / 中文名与刷新间隔，零网络、零密钥：
        访问码、Cookie、Token 等敏感信息绝不进入渲染数据。
        """
        sources = list(self._leaderboard_sources or [])
        names: List[str] = []
        for source_id in sources:
            try:
                name = (self._leaderboard_client.source_name(source_id)
                        if self._leaderboard_client else source_id)
            except Exception:  # noqa: BLE001 - 展示名缺失不影响页面渲染
                name = source_id
            names.append(str(name or source_id))
        return {
            "enabled": bool(self._leaderboard_enabled),
            "sources": sources,
            "source_names": names,
            "refresh_minutes": int(self._leaderboard_refresh_minutes or 0),
        }

    def get_page(self) -> Optional[List[dict]]:
        # v1.8.5：打开设置页时真实刷新一次 115 网盘 / 癫影账户，
        # 页面渲染读的是快照，所以刷新必须在渲染之前完成。
        self._refresh_accounts_on_page_open()
        history = self.get_data('history') or []
        # v1.9.0：榜单页签只读本地快照（零网络），榜单数据由后台刷新服务预热
        # v1.9.1：额外传入非敏感状态，数据页展示启用状态 / 已配置来源 / 初始化入口
        leaderboard_snapshot = []
        if self._leaderboard_handler:
            try:
                leaderboard_snapshot = self._leaderboard_handler.snapshot()
            except Exception as error:  # noqa: BLE001
                logger.debug(f"读取榜单快照失败：{error}")
        return UIConfig.get_page(history, leaderboard_snapshot, self._leaderboard_page_state())

    def _refresh_accounts_on_page_open(self) -> None:
        """打开配置页时刷新账户（静默 + 冷却，失败不影响页面渲染）。

        - 115 与癫影各刷一次，单账户失败不影响另一个；
        - 癫影禁止浏览器登录（冷启动 30s+ 会把设置页拖超时），
          token 过期时交给后台自动刷新去重新登录；
        - 命中 30 秒冷却时直接跳过，避免反复开关页面打接口。
        """
        for key in (self.ACCOUNT_KEY_P115, self.ACCOUNT_KEY_DIAN115):
            try:
                self._account_info(
                    key, refresh=True, allow_browser_login=False, silent=True
                )
            except Exception as error:  # noqa: BLE001
                logger.debug(f"打开配置页刷新账户失败（{key}）：{error}")

    def get_api(self) -> List[Dict[str, Any]]:
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
            },
            {
                "path": "/refresh_account",
                "endpoint": self.api_refresh_account,
                "methods": ["POST"],
                "summary": "刷新账户信息卡片"
            },
            {
                "path": "/leaderboard/browse",
                "endpoint": self.api_leaderboard_browse,
                "methods": ["GET", "POST"],
                "summary": "浏览榜单（走本地缓存，失败优雅降级）"
            },
            {
                "path": "/leaderboard/subscribe",
                "endpoint": self.api_leaderboard_subscribe,
                "methods": ["POST"],
                "summary": "订阅榜单媒体条目"
            },
            {
                "path": "/leaderboard/refresh",
                "endpoint": self.api_refresh_leaderboard,
                "methods": ["GET", "POST"],
                "summary": "后台刷新榜单快照（不阻塞请求线程）"
            },
            {
                "path": "/resolve_share_link",
                "endpoint": self.api_resolve_share_link,
                "methods": ["POST"],
                "summary": "解析并校验 115 分享链接状态"
            }
        ]
    
    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """定义远程控制命令"""
        return [{
            "cmd": "/p115_sub_action",
            "event": EventType.PluginAction,
            "desc": "115网盘订阅搜索",
            "category": "订阅",
            "data": {
                "action": "p115_sub_action"
            }
        }]


    def _build_checkin_service(self) -> List[Dict[str, Any]]:
        """构建签到服务；未配置独立周期时返回空列表（此时跟随主周期执行）。"""
        if not self._checkin_enabled:
            return []
        if not self._checkin_cron:
            logger.info("签到未配置独立周期，将跟随主周期执行")
            return []

        try:
            trigger = CronTrigger.from_crontab(self._checkin_cron)
        except Exception as e:
            logger.warning(f"签到 Cron 表达式无效：{self._checkin_cron}，本次不注册签到服务。错误：{e}")
            return []

        return [{
            "id": "P115SubSearchCheckin",
            "name": "115网盘签到服务",
            "trigger": trigger,
            "func": self.checkin_service,
            "kwargs": {}
        }]

    def _build_dian115_login_service(self) -> List[Dict[str, Any]]:
        """构建癫影会话保活服务（v1.8.1）。

        仅有在「自动登录」开启、账号密码齐备、且用户填了保活周期时才注册。
        目的：在没有搜索/签到任务的时段也定期刷新一次登录态，
        让任务执行时无需等待浏览器冷启动（首次约 10 秒）。
        """
        if not self._dian115_login_cron:
            return []
        if not (self._dian115_auto_login and self._dian115_email and self._dian115_password):
            logger.info("癫影保活周期已配置，但自动登录未就绪（缺账号密码或开关关闭），不注册保活服务")
            return []
        if not (self._dian115_enabled or self._dian115_checkin_enabled):
            return []

        try:
            trigger = CronTrigger.from_crontab(self._dian115_login_cron)
        except Exception as e:
            logger.warning(
                f"癫影保活 Cron 表达式无效：{self._dian115_login_cron}，本次不注册。错误：{e}"
            )
            return []

        return [{
            "id": "P115SubSearchDian115Login",
            "name": "癫影会话保活服务",
            "trigger": trigger,
            "func": self.dian115_login_service,
            "kwargs": {}
        }]

    def dian115_login_service(self):
        """定时刷新癫影登录态（保活）。

        失败不抛异常——保活失败不应污染 MoviePilot 的服务调度日志，
        真正的搜索/签到任务会自行再登录一次。
        """
        client = self._dian115_client
        if not client:
            return
        try:
            info = client.get_account_info()
            logger.info(
                "癫影会话保活成功：%s，积分 %s"
                % (info.get("name") or "未知", info.get("points") or 0)
            )
        except Exception as e:
            logger.warning(f"癫影会话保活失败（不影响后续任务）: {e}")

    def account_snapshot_refresh_service(self) -> None:
        """
        定时后台刷新账户快照（v1.8.4，满足配置页「自动刷新」诉求）。

        - 115 与癫影逐个刷新，单账户失败不影响另一个；
        - **成功才覆盖快照**——临时网络抖动不会把好快照冲成占位卡；
        - 癫影走 get_account_info(allow_browser_login=True)，token 过期时
          自动重新登录并持久化，相当于同时完成会话保活；
        - 配置页保持零网络请求：页面只读快照，本服务保证快照足够新鲜。
        - v1.8.5：全程 silent，后台刷新**不产生 info 级日志**。
        """
        for key in (self.ACCOUNT_KEY_P115, self.ACCOUNT_KEY_DIAN115):
            try:
                account = self._load_account(key, allow_browser_login=True, silent=True)
            except Exception as error:
                logger.debug(f"自动刷新账户快照失败（{key}）：{error}")
                continue
            if not isinstance(account, dict) or not account.get("connected"):
                continue
            account["refreshed_at"] = int(time.time())
            self._save_account_snapshot(key, account)
        logger.debug("账户快照自动刷新完成")

    def _build_account_refresh_service(self) -> List[Dict[str, Any]]:
        """构建账户快照自动刷新服务（v1.8.4；间隔 0 表示关闭）。"""
        minutes = self._account_refresh_minutes
        if minutes <= 0:
            return []
        has_p115 = self._p115_manager is not None and bool((self._cookies or "").strip())
        has_dian115 = bool(self._dian115_email and self._dian115_password)
        if not (has_p115 or has_dian115):
            return []
        return [{
            "id": "P115SubSearchAccountRefresh",
            "name": "账户信息自动刷新",
            "trigger": "interval",
            "func": self.account_snapshot_refresh_service,
            "kwargs": {"minutes": minutes}
        }]

    def get_service(self) -> List[Dict[str, Any]]:
        services = []

        # 签到服务独立于订阅搜索主开关：只要启用签到就注册（v1.8.0）
        services.extend(self._build_checkin_service())

        # 癫影会话保活（v1.8.1）：同样独立于主开关，只看自身配置
        services.extend(self._build_dian115_login_service())

        # 账户快照自动刷新（v1.8.4）：配置页「自动刷新」的数据来源
        services.extend(self._build_account_refresh_service())

        # 榜单后台刷新（v1.9.0）：慢源只走后台，配置页只读本地快照
        services.extend(self._build_leaderboard_service())

        if not self._enabled:
            return services

        # v1.7.3 恢复注册时最小间隔校验（双保险：init_plugin 已校验，此处防止
        # 运行中配置被外部改动导致过密触发）；不满足时回退 4 小时周期
        effective_cron = self._cron
        if effective_cron and not self._cron_interval_ge_min_hours(effective_cron, self._MIN_INTERVAL_HOURS):
            logger.warning(
                f"注册服务时 Cron 过密（要求间隔 >= {self._MIN_INTERVAL_HOURS}h）：{effective_cron}，回退 {self._FALLBACK_CRON}"
            )
            effective_cron = self._FALLBACK_CRON

        if effective_cron:
            try:
                services.append({
                    "id": "P115SubSearch",
                    "name": "115网盘订阅搜索服务",
                    "trigger": CronTrigger.from_crontab(effective_cron),
                    "func": self.sync_subscribes,
                    "kwargs": {}
                })
            except Exception as e:
                logger.warning(f"Cron 表达式无效：{effective_cron}，将回退 interval=4h。错误：{e}")
                services.append({
                    "id": "P115SubSearch",
                    "name": "115网盘订阅搜索服务",
                    "trigger": "interval",
                    "func": self.sync_subscribes,
                    "kwargs": {"hours": self._MIN_INTERVAL_HOURS}
                })
        else:
            services.append({
                "id": "P115SubSearch",
                "name": "115网盘订阅搜索服务",
                "trigger": "interval",
                "func": self.sync_subscribes,
                "kwargs": {"hours": self._MIN_INTERVAL_HOURS}
            })

        return services

    # ======================================================================
    # 必备：_do_sync（返回 bool）
    # ======================================================================

    def _do_sync(self) -> bool:
        # 至少启用一个搜索源
        if not self._pansou_enabled and not self._nullbr_enabled:
            logger.error("搜索源均未启用（PanSou/Nullbr），无法执行")
            if self._notify:
                self.post_message(
                    mtype=NotificationType.Plugin,
                    title="【115网盘订阅搜索】配置错误",
                    text="PanSou、Nullbr 均未启用，请至少启用一个搜索源。"
                )
            return False

        # 115 客户端检查
        if not self._p115_manager:
            logger.error("115 客户端未初始化，请检查 Cookie 配置")
            return False

        if not self._p115_manager.check_login():
            logger.error("115 登录失败，Cookie 可能已过期")
            if self._notify:
                self.post_message(
                    mtype=NotificationType.Manual,
                    title="【115网盘订阅搜索】登录失败",
                    text="115 Cookie 可能已过期，请更新后重试。"
                )
            return False

        # 登录校验已通过，刷新一次 115 账户快照供配置页展示（v1.8.3）
        self._refresh_p115_account_snapshot()
        logger.info("开始执行 115 网盘订阅同步...")
        if self._notify:
            self.post_message(
                mtype=NotificationType.Plugin,
                title="【115网盘订阅搜索】开始执行",
                text="正在扫描订阅列表并同步缺失内容..."
            )

        # reset api counters
        try:
            self._p115_manager.reset_api_call_count()
        except Exception:
            pass
        try:
            if self._pansou_client:
                self._pansou_client.reset_api_call_count()
        except Exception:
            pass
        try:
            if self._nullbr_client:
                self._nullbr_client.reset_api_call_count()
        except Exception:
            pass
        try:
            if self._search_handler:
                self._search_handler.reset_task_spent_points()
        except Exception:
            pass

        # 获取订阅
        with SessionFactory() as db:
            subscribes = SubscribeOper(db=db).list("N,R")

        if not subscribes:
            logger.info("无订阅数据")
            if self._notify:
                self.post_message(
                    mtype=NotificationType.Plugin,
                    title="【115网盘订阅搜索】执行完成",
                    text="当前无订阅数据。"
                )
            return True

        tv_subscribes = [s for s in subscribes if s.type == MediaType.TV.value]
        movie_subscribes = [s for s in subscribes if s.type == MediaType.MOVIE.value]

        if not tv_subscribes and not movie_subscribes:
            logger.info("无电影/剧集订阅")
            return True

        history: List[dict] = self.get_data('history') or []
        transfer_details: List[Dict[str, Any]] = []
        transferred_count = 0

        exclude_ids = set(self._exclude_subscribes or [])
        skipped_count = 0

        # 处理电影
        for subscribe in movie_subscribes:
            if global_vars.is_system_stopped:
                break
            if self._is_subscribe_excluded(subscribe.id):
                skipped_count += 1
                continue
            transferred_count = self._sync_handler.process_movie_subscribe(
                subscribe=subscribe,
                history=history,
                transfer_details=transfer_details,
                transferred_count=transferred_count
            )

        # 处理剧集
        for subscribe in tv_subscribes:
            if global_vars.is_system_stopped:
                break
            if self._is_subscribe_excluded(subscribe.id):
                skipped_count += 1
                continue
            transferred_count = self._sync_handler.process_tv_subscribe(
                subscribe=subscribe,
                history=history,
                transfer_details=transfer_details,
                transferred_count=transferred_count,
                exclude_ids=exclude_ids
            )

        if skipped_count:
            mode_label = "指定模式" if self._subscribe_filter_mode == "include" else "排除模式"
            logger.info(f"订阅过滤（{mode_label}）：本次跳过 {skipped_count} 个不在处理范围的订阅")

        self.save_data('history', history)

        logger.info(f"115 网盘订阅同步完成，共转存 {transferred_count} 个文件")

        if self._notify:
            if transferred_count > 0:
                self._sync_handler.send_transfer_notification(transfer_details, transferred_count)
            else:
                self.post_message(
                    mtype=NotificationType.Plugin,
                    title="【115网盘订阅搜索】执行完成",
                    text="本次同步未发现需要转存的新资源。"
                )

        return True

    # ------------------ API包装（用于 get_api） ------------------

    def api_clear_history(self) -> dict:
        """API: 清空历史记录（鉴权由 MoviePilot 统一 verify_apikey 完成）。"""
        return self._api_handler.clear_history()

    # ------------------ 同步入口（触发条件1） ------------------

    def sync_subscribes(self):
        with lock:
            tz = pytz.timezone(settings.TZ)
            run_start = datetime.datetime.now(tz=tz)

            success = False
            try:
                success = self._do_sync()
            except Exception as e:
                logger.error(f"同步任务异常：{e}")
                success = False
            finally:
                # 签到：未配置独立周期时跟随主周期执行（v1.8.0）
                if self._checkin_enabled and not self._checkin_cron:
                    try:
                        self.run_checkin()
                    except Exception as e:
                        logger.error(f"跟随主周期签到失败：{e}")

                # 仅在用户开启了屏蔽系统订阅时，才执行自动窗口切换逻辑
                if success and self._block_system_subscribe and self._is_last_run_today(run_start):
                    if int(self._unblock_delay_minutes) < 0 or (not self._window_enabled()):
                        self._enter_blocked(reason="触发条件1")
                    else:
                        self._schedule_unblock_after_delay(datetime.datetime.now(tz=pytz.timezone(settings.TZ)))

    # ------------------ 业务 API（保留） ------------------

    def api_search(self, keyword: str, apikey: str) -> dict:
        return self._api_handler.search(keyword, apikey)

    def api_transfer(self, share_url: str, save_path: str, apikey: str) -> dict:
        return self._api_handler.transfer(share_url, save_path, apikey)

    def api_list_directories(self, path: str = "/", apikey: str = "") -> dict:
        return self._api_handler.list_directories(path, apikey)

    @eventmanager.register(EventType.PluginAction)
    def remote_sync(self, event: Event):
        if not event:
            return
        event_data = event.event_data
        if not event_data or event_data.get("action") != "p115_sub_action":
            return

        logger.info("收到命令，开始执行追更任务")
        self.post_message(
            mtype=NotificationType.Plugin,
            channel=event_data.get("channel"),
            title="【115网盘订阅搜索】开始执行",
            text="已收到远程命令，正在执行追更任务...",
            userid=event_data.get("user")
        )

        self.sync_subscribes()

        self.post_message(
            mtype=NotificationType.Plugin,
            channel=event_data.get("channel"),
            title="【115网盘订阅搜索】执行完成",
            text="远程触发的追更任务已完成。",
            userid=event_data.get("user")
        )
