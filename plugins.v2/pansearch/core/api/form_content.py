"""MoviePilotLite 兼容渲染。"""

import re
from typing import Any, Dict, List, Optional

from app.schemas.types import NotificationType

from ..config import UIConfig


class FormContent:
    _SENSITIVE = (
        "cookie", "password", "token", "secret", "api_key", "access_key",
        "checkin_url",
        "refresh_token", "auth_code", "client_id", "device_id",
    )

    _LABELS = {
        "enabled": "启用插件", "show_sidebar_nav": "启用左侧导航",
        "notify": "发送消息通知", "notification_type": "消息通知类型",
        "webhook_enabled": "启用 Webhook", "webhook_url": "Webhook 地址",
        "webhook_method": "请求方法", "webhook_timeout": "超时（秒）",
        "agent_enabled": "启用智能体工具", "direct_transfer_enabled": "链接直达转存",
        "platform_transfer_history_enabled": "写入整理历史", "takeover_new_subscribes": "拦截新增订阅",
        "cron": "订阅执行周期", "block_system_subscribe": "始终接管系统订阅",
        "platform_download_policy": "平台下载策略", "block_start_time": "接管开始",
        "block_end_time": "接管结束", "cloud_drive": "当前转存网盘",
        "p115_checkin_enabled": "115 网盘启用每日签到",
        "auto_subscribe_enabled": "启用榜单自动订阅", "auto_subscribe_onlyonce": "保存后立即运行一次",
        "auto_subscribe_notify": "发送运行结果通知", "auto_subscribe_skip_season_zero": "跳过第 0 季",
        "auto_subscribe_skip_subscribed": "跳过已有活动订阅", "auto_subscribe_skip_history": "跳过历史订阅",
        "auto_subscribe_skip_library": "跳过媒体库已有内容", "auto_subscribe_cron": "榜单订阅执行周期",
        "auto_subscribe_username": "订阅用户名", "auto_subscribe_proxy": "代理地址",
        "auto_subscribe_douban_rsshub_base": "RSSHub 服务地址", "auto_subscribe_douban_rss_urls": "自定义 RSS 地址",
        "auto_subscribe_douban_proxy": "豆瓣启用代理",
        "auto_subscribe_douban_enabled": "启用豆瓣榜单",
        "auto_subscribe_douban_limit": "豆瓣每榜条数",
        "auto_subscribe_douban_ranks": "豆瓣热门榜单", "auto_subscribe_douban_media_type": "豆瓣媒体类型",
        "auto_subscribe_douban_min_vote": "豆瓣最低评分", "auto_subscribe_douban_min_year": "豆瓣最低年份",
        "auto_subscribe_douban_min_month": "豆瓣起始月份",
        "auto_subscribe_maoyan_enabled": "启用猫眼榜单",
        "auto_subscribe_maoyan_base_url": "猫眼服务地址", "auto_subscribe_maoyan_movie_box": "猫眼电影票房榜",
        "auto_subscribe_maoyan_platforms": "猫眼网播平台", "auto_subscribe_maoyan_categories": "猫眼网播类型",
        "auto_subscribe_maoyan_limit": "猫眼每榜条数", "auto_subscribe_maoyan_media_type": "猫眼媒体类型",
        "auto_subscribe_maoyan_min_vote": "猫眼最低评分", "auto_subscribe_maoyan_min_year": "猫眼最低年份",
        "auto_subscribe_maoyan_min_month": "猫眼起始月份",
        "auto_subscribe_netflix_enabled": "启用 Netflix 榜单",
        "auto_subscribe_netflix_base_url": "Netflix 服务地址", "auto_subscribe_netflix_global": "Netflix 全球榜",
        "auto_subscribe_netflix_global_dataset": "Netflix 全球数据源",
        "auto_subscribe_netflix_limit": "Netflix 每榜取前 N",
        "auto_subscribe_netflix_global_media_types": "Netflix 全球榜媒体类型",
        "auto_subscribe_netflix_rich_metadata": "Netflix 抓取详细元数据",
        "auto_subscribe_netflix_max_workers": "Netflix 元数据并发数",
        "auto_subscribe_netflix_use_cache": "Netflix 启用周更缓存",
        "auto_subscribe_netflix_min_vote": "Netflix 最低评分", "auto_subscribe_netflix_min_year": "Netflix 最低年份",
        "auto_subscribe_netflix_min_month": "Netflix 起始月份",
        "auto_subscribe_tmdb_limit": "TMDB 每榜条数",
        "auto_subscribe_tmdb_min_month": "TMDB 起始月份",
        "auto_subscribe_bangumi_limit": "Bangumi 每榜条数",
        "auto_subscribe_bangumi_min_month": "Bangumi 起始月份",
        "auto_subscribe_anilist_limit": "AniList 每榜条数",
        "auto_subscribe_anilist_min_month": "AniList 起始月份",
        "auto_subscribe_mikan_enabled": "启用 Mikan 新番",
        "auto_subscribe_mikan_limit": "Mikan 每榜条数",
        "auto_subscribe_mikan_year": "Mikan 番组年份", "auto_subscribe_mikan_season": "Mikan 季度",
        "auto_subscribe_mikan_base_urls": "Mikan 服务地址",
        "auto_subscribe_mikan_resolve_bangumi_id": "抓取 Bangumi ID",
        "auto_subscribe_mikan_proxy": "Mikan 启用代理",
        "auto_subscribe_mikan_min_vote": "Mikan 最低评分", "auto_subscribe_mikan_min_year": "Mikan 最低年份",
        "auto_subscribe_mikan_min_month": "Mikan 起始月份",
        "subscribe_filter_mode": "订阅筛选模式", "exclude_subscribes": "排除订阅", "include_subscribes": "指定订阅",
        "cross_transfer_enabled": "跨盘资源自动转存", "skip_other_season_dirs": "跳过其他季目录",
        "transfer_task_batch_size": "任务内每批处理文件数", "subscription_concurrency": "订阅并发数",
        "batch_size": "分享转存每批数量", "batch_interval": "转存批次间隔（秒）",
        "cross_transfer_media_types": "允许跨盘转存类型", "local_resource_path": "本地媒体根路径",
        "cross_transfer_download_path": "跨盘中继缓存目录", "cross_transfer_download_threads": "下载线程数",
        "cross_transfer_max_concurrent": "同时跨盘任务数", "transfer_risk_cooldown": "转存风控冷却（秒）",
        "strm_generate_enabled": "转存后直接生成 STRM", "nfo_scrape_enabled": "刮削生成 NFO",
        "image_scrape_enabled": "刮削生成图片", "strm_base_url": "STRM 基础地址",
        "strm_url_template": "STRM URL 模板", "media_server_refresh_enabled": "启用入库通知",
        "media_server_refresh_delay": "延迟通知（秒）", "media_servers": "通知媒体服务器",
        "media_server_path_mappings": "媒体服务器路径映射", "platform_media_sync_enabled": "接收媒体库通知",
        "platform_deep_delete_enabled": "神医深度删除联动", "notify": "发送消息通知",
        "notification_type": "消息通知类型", "webhook_enabled": "启用 Webhook",
        "webhook_url": "Webhook 地址", "webhook_method": "请求方法", "webhook_timeout": "超时（秒）",
        "search_source_order": "搜索资源优先级", "resource_type_order": "资源类型优先级",
        "magnet_metadata_url_template": "Magnet 元数据地址模板", "search_proxy": "代理地址",
        "search_cache_enabled": "启用搜索缓存", "search_cache_ttl_minutes": "缓存时间（分钟）",
        "search_concurrency": "搜索并发数", "pansou_url": "PanSou 服务地址",
        "pansou_auth_enabled": "启用 PanSou 身份认证", "pansou_channels": "PanSou 限定频道",
        "pansou_plugins": "PanSou 限定插件", "pansou_result_limit": "PanSou 每类结果数量",
        "pansou_refresh": "PanSou 强制刷新", "pansou_timeout": "PanSou 搜索超时（秒）",
        "hdhive_base_url": "HDHive 服务地址", "hdhive_query_mode": "HDHive 查询模式",
        "hdhive_response_mode": "HDHive OAuth 回调模式", "hdhive_auto_unlock": "HDHive 允许积分解锁",
        "hdhive_max_unlock_points": "HDHive 单次积分总预算", "hdhive_max_points_per_sub": "HDHive 单订阅解锁预算",
        "hdhive_candidate_limit": "HDHive 候选上限", "hdhive_request_interval": "HDHive 请求访问间隔",
        "hdhive_torrentclaw_enabled": "获取 TorrentClaw Magnet", "hdhive_checkin_enabled": "HDHive 启用每日签到",
        "hdhive_username": "HDHive 用户名", "hdhive_torrentclaw_subtitle_languages": "Magnet 字幕语言筛选",
        "hdhive_unlocks_per_minute": "HDHive 每分钟解锁数",
        "hdhive_checkin_mode": "HDHive 签到模式", "dian115_base_url": "Dian115 服务地址",
        "dian115_email": "Dian115 登录邮箱", "dian115_lottery_enabled": "Dian115 启用幸运转盘",
        "dian115_lottery_count": "Dian115 每日转盘次数", "dian115_max_unlock_points": "Dian115 单次积分总预算",
        "dian115_max_points_per_sub": "Dian115 单订阅积分预算", "dian115_unlocks_per_minute": "Dian115 每分钟解锁数",
        "dian115_auto_unlock": "Dian115 允许积分解锁", "dian115_candidate_limit": "Dian115 候选上限",
        "dian115_request_interval": "Dian115 请求间隔", "dian115_checkin_enabled": "Dian115 启用每日签到",
        "dian115_checkin_mode": "Dian115 签到模式", "juying_base_url": "聚影服务地址",
        "quark_checkin_enabled": "夸克网盘启用每日签到", "quark_checkin_url": "夸克签到 URL",
        "juying_username": "聚影网页登录账号", "pinglian_username": "盘链网页登录账号",
        "juying_result_limit": "聚影候选上限", "juying_request_interval": "聚影请求间隔",
        "juying_checkin_enabled": "聚影启用每日签到", "pinglian_base_url": "盘链服务地址",
        "pinglian_result_limit": "盘链候选上限", "pinglian_request_interval": "盘链请求间隔",
        "pinglian_timeout": "盘链请求超时", "seedhub_base_url": "SeedHub 服务地址",
        "seedhub_result_limit": "SeedHub 候选上限", "seedhub_request_interval": "SeedHub 请求间隔",
        "seedhub_timeout": "SeedHub 请求超时", "online_docs": "在线文档",
        "checkin_cron": "签到执行周期", "checkin_auto_retry": "签到失败自动重试", "checkin_retry_count": "签到重试次数",
        "emby_mediainfo_enabled": "启用 Emby 媒体信息提取",
        "timeout_default_connect": "普通连接超时（秒）", "timeout_default_pool": "普通连接池超时（秒）",
        "timeout_default_read": "普通读取超时（秒）", "timeout_default_write": "普通写入超时（秒）",
        "timeout_slow_connect": "慢操作连接超时（秒）", "timeout_slow_pool": "慢操作连接池超时（秒）",
        "timeout_slow_read": "慢操作读取超时（秒）", "timeout_slow_write": "慢操作写入超时（秒）",
        "online_docs_urls": "在线文档地址", "online_docs_resource_types": "在线文档资源类型",
        "enable_cloud_upgrade": "启用网盘洗版", "enable_pt_upgrade": "启用 PT 洗版",
        "upgrade_mode": "洗版模式", "upgrade_subscribe_ids": "单独开启洗版的订阅",
        "cloud_transfer_path": "115 转存目录", "p123_transfer_path": "123 转存目录",
        "quark_transfer_path": "夸克转存目录", "guangya_transfer_path": "光鸭转存目录",
        "tianyi_transfer_path": "天翼转存目录", "alipan_transfer_path": "阿里云盘转存目录",
        "cloud_media_path": "115 媒体目录", "p123_media_path": "123 媒体目录",
        "quark_media_path": "夸克媒体目录", "guangya_media_path": "光鸭媒体目录",
        "tianyi_media_path": "天翼媒体目录", "alipan_media_path": "阿里云盘媒体目录",
        "self_heal_interval": "评分自愈间隔（分钟）", "timeout_enabled": "启用请求超时控制",
    }

    _SELECTS = {
        "cloud_drive": [("115 网盘", "115"), ("123 网盘", "123"), ("夸克网盘", "quark"), ("光鸭网盘", "guangya"),
                        ("天翼云盘", "tianyi"), ("阿里云盘", "alipan")],
        "platform_download_policy": [("允许下载并整理", "allow"), ("阻止搜索及下载", "block"),
                                     ("转为网盘离线下载", "cloud")],
        "subscribe_filter_mode": [("排除所选订阅", "exclude"), ("仅处理所选订阅", "include")],
        "cross_transfer_media_types": [("电影", "movie"), ("电视剧", "tv")],
        "webhook_method": [("POST", "POST"), ("GET", "GET")],
        "hdhive_query_mode": [("WebAPI", "web"), ("OpenAPI", "api")],
        "hdhive_response_mode": [("Redirect（复制回调 URL）", "redirect"), ("PostMessage（弹窗自动回传）", "postmessage")],
        "hdhive_checkin_mode": [("普通签到", "normal"), ("赌狗签到", "gambler")],
        "dian115_checkin_mode": [("普通签到", "normal"), ("运气签到", "lucky")],
        "upgrade_mode": [("保留最大文件", "largest"), ("保留最小文件", "smallest"), ("直接替换", "replace"),
                         ("新旧共存", "coexist")],
        "auto_subscribe_douban_media_type": [("全部", "all"), ("电影", "movie"), ("电视剧", "tv")],
        "auto_subscribe_maoyan_media_type": [("全部", "all"), ("电影", "movie"), ("电视剧", "tv")],
        "auto_subscribe_netflix_global_dataset": [("周榜数据", "weekly"), ("全球数据", "global")],
        "auto_subscribe_netflix_global_media_types": [
            ("电影（英文）", "Films (English)"), ("电影（非英文）", "Films (Non-English)"),
            ("电视剧（英文）", "TV (English)"), ("电视剧（非英文）", "TV (Non-English)"),
        ],
        "auto_subscribe_maoyan_platforms": [("全部平台", "all"), ("Netflix", "netflix"), ("爱奇艺", "iqiyi"),
                                            ("腾讯视频", "tencent")],
        "auto_subscribe_maoyan_categories": [("电视剧", "tv"), ("电影", "movie"), ("综艺", "show")],
        "auto_subscribe_mikan_season": [("当前季度", "当前"), ("上一季度", "上一季度"), ("下一季度", "下一季度")],
        "online_docs_resource_types": [
            ("115 分享", "115"), ("123 分享", "123"), ("夸克分享", "quark"), ("阿里云盘", "alipan"),
        ],
        "hdhive_torrentclaw_subtitle_languages": [("中文", "zh"), ("英文", "en"), ("日文", "ja")],
    }

    _MULTIPLE = {
        "exclude_subscribes", "include_subscribes", "upgrade_subscribe_ids",
        "cross_transfer_media_types", "search_source_order", "resource_type_order",
        "auto_subscribe_douban_ranks", "auto_subscribe_netflix_global_media_types",
        "auto_subscribe_maoyan_platforms", "auto_subscribe_maoyan_categories",
        "online_docs_resource_types", "hdhive_torrentclaw_subtitle_languages",
    }

    _KEY_PART_LABELS = {
        "auto_subscribe": "自动订阅", "douban": "豆瓣", "maoyan": "猫眼",
        "netflix": "Netflix", "mikan": "Mikan", "enabled": "启用",
        "cron": "执行周期", "proxy": "代理", "base_url": "服务地址",
        "request_interval": "请求间隔", "timeout": "超时", "limit": "数量上限",
        "min_vote": "最低评分", "min_year": "最低年份", "min_month": "起始月份", "checkin": "签到",
        "retry": "重试", "count": "次数", "search": "搜索", "source": "来源",
        "order": "优先级", "resource": "资源", "type": "类型", "media": "媒体",
        "path": "目录", "transfer": "转存", "download": "下载", "threads": "线程",
        "concurrent": "并发", "cache": "缓存", "interval": "间隔", "default": "默认",
        "providers": "渠道", "ranks": "榜单", "rsshub": "RSSHub", "urls": "地址列表",
        "platforms": "平台", "categories": "分类", "global": "全球", "dataset": "数据源",
        "media_types": "媒体类型", "selections": "选择", "web_platform": "网播平台",
        "map": "映射", "username": "用户名", "method": "方式", "mode": "模式",
        "policy": "策略", "start": "开始", "end": "结束", "skip": "跳过",
        "history": "历史", "library": "媒体库", "season": "季度", "zero": "零季",
        "onlyonce": "单次", "notify": "通知", "filter": "筛选", "include": "包含",
        "exclude": "排除", "channels": "频道", "plugins": "插件", "concurrency": "并发数",
    }

    @classmethod
    def _label(cls, key: str) -> str:
        label = cls._LABELS.get(key)
        if label:
            return label
        parts = [cls._KEY_PART_LABELS.get(part, part) for part in key.split("_")]
        readable = " ".join(part for part in parts if part)
        return f"其他设置：{readable or '未命名'}"

    @classmethod
    def _sensitive(cls, key: str) -> bool:
        lowered = key.lower()
        return any(part in lowered for part in cls._SENSITIVE)

    @classmethod
    def _items(cls, key: str, owner: Any) -> List[dict]:
        if key in {"exclude_subscribes", "include_subscribes", "upgrade_subscribe_ids"}:
            try:
                return list(UIConfig.get_subscribe_options())
            except Exception:
                return []
        if key == "media_servers":
            try:
                return list(UIConfig.get_media_server_options())
            except Exception:
                return []
        if key == "notification_type":
            try:
                return [{"title": item.value, "value": item.name} for item in NotificationType]
            except Exception:
                return [{"title": "插件消息", "value": "Plugin"}]
        if key == "search_source_order":
            return [{"title": title, "value": value} for title, value in (
                ("HDHive", "hdhive"), ("Dian115", "dian115"), ("PanSou", "pansou"),
                ("聚影", "juying"), ("SeedHub", "seedhub"),
                ("盘链", "pinglian"), ("在线文档", "online_docs"),
            )]
        if key == "resource_type_order":
            return [{"title": title, "value": value} for title, value in (
                ("115 分享", "115"), ("123 分享", "123"), ("夸克分享", "quark"),
                ("光鸭分享", "guangya"), ("天翼云盘", "tianyi"), ("阿里云盘", "alipan"),
                ("ED2K", "ed2k"), ("Magnet", "magnet"),
            )]
        if key == "auto_subscribe_douban_ranks":
            return [{"title": title, "value": value} for title, value in (
                ("北美票房榜", "movie-ustop"), ("一周口碑电影榜", "movie-weekly"),
                ("实时热门电影", "movie-real-time"), ("热门综艺", "show-domestic"),
                ("热门电影", "movie-hot-gaia"), ("热门电视剧", "tv-hot"),
            )]
        return [{"title": title, "value": value} for title, value in cls._SELECTS.get(key, [])]

    @classmethod
    def form(cls, owner: Any) -> List[dict]:
        defaults = UIConfig.get_default_config()
        cron_fields = {key for key in defaults if key.endswith("_cron") or key == "cron"}
        textarea_fields = {"strm_url_template", "media_server_path_mappings", "magnet_metadata_url_template"}
        select_fields = set(cls._SELECTS) | cls._MULTIPLE | {
            "exclude_subscribes", "include_subscribes", "upgrade_subscribe_ids", "media_servers",
            "notification_type", "search_source_order", "resource_type_order", "auto_subscribe_douban_ranks",
        }
        fields = []
        for key, value in defaults.items():
            if cls._sensitive(key):
                continue
            if key not in select_fields and not isinstance(value, (bool, int, float, str)):
                continue
            if key in select_fields:
                component = "VSelect"
            elif key in cron_fields:
                component = "cron"
            elif key in textarea_fields:
                component = "VTextarea"
            else:
                component = "VSwitch" if isinstance(value, bool) else "VTextField"
            props = {"model": key, "label": cls._label(key), "value": value}
            if component == "VSelect":
                props["items"] = cls._items(key, owner)
                props["multiple"] = key in cls._MULTIPLE
            if component == "cron":
                props["hint"] = "支持标准 Cron 表达式"
            if key in {"cron", "auto_subscribe_cron"}:
                props["hint"] = "支持标准 Cron 表达式"
            fields.append({"component": component, "props": props})
        groups = {
            "基础设置": [],
            "订阅与榜单": [],
            "转存与媒体库": [],
            "搜索资源": [],
            "通知与高级": [],
        }
        for field in fields:
            key = str(field.get("props", {}).get("model") or "")
            if key.startswith("auto_subscribe_") or key in {"subscribe_filter_mode", "exclude_subscribes",
                                                            "include_subscribes"}:
                group = "订阅与榜单"
            elif any(token in key for token in
                     ("transfer", "strm", "nfo", "image_scrape", "media_server", "platform_media")):
                group = "转存与媒体库"
            elif any(token in key for token in
                     ("search", "pansou", "dian115", "juying", "pinglian", "seedhub",
                      "online_docs", "magnet")):
                group = "搜索资源"
            elif any(token in key for token in
                     ("notify", "webhook", "checkin", "agent", "timeout", "self_heal", "upgrade")):
                group = "通知与高级"
            else:
                group = "基础设置"
            groups[group].append(field)
        # pagex 只支持静态节点树，分组直接展开，避免使用需要状态绑定的标签页组件。
        content = []
        for title, nodes in groups.items():
            if not nodes:
                continue
            content.append({
                "component": "VCard",
                "props": {"variant": "outlined", "class": "mb-3"},
                "content": [
                    {"component": "VCardTitle", "text": title},
                    {"component": "VCardText", "content": nodes},
                ],
            })
        return [{"component": "VForm", "content": content}]

    @staticmethod
    def _text(value: Any) -> str:
        text = str(value if value is not None else "").strip()
        return text or "-"

    @staticmethod
    def _mapping(value: Any) -> Dict[str, Any]:
        return dict(value) if isinstance(value, dict) else {}

    @staticmethod
    def _progress(value: Any) -> int:
        try:
            return max(0, min(100, int(float(value or 0))))
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _button(label: str, api: str, method: str = "post", params: Optional[Dict[str, Any]] = None,
                color: str = "primary") -> dict:
        return {"component": "VBtn", "text": label, "props": {"color": color, "size": "small"},
                "events": {"click": {"api": api, "method": method, "params": params or {}}}}

    @classmethod
    def _table(cls, headers: List[str], rows: List[List[Any]]) -> dict:
        return {"component": "VTable", "content": [
            {"component": "thead",
             "content": [{"component": "tr", "content": [{"component": "th", "text": header} for header in headers]}]},
            {"component": "tbody", "content": [
                {"component": "tr", "content": [{"component": "td", "text": cls._text(value)} for value in row]} for row
                in rows]},
        ]}

    @classmethod
    def page(cls, owner: Any) -> List[dict]:
        """构造始终可渲染的静态页面；运行态/历史接口失败不影响页面骨架。"""
        runtime: Dict[str, Any] = {}
        history_groups: List[Dict[str, Any]] = []
        try:
            runtime = cls._mapping(owner._runtime_snapshot())
        except Exception:
            runtime = {}
        try:
            response = cls._mapping(owner.api_vue_page_data(page=1, page_size=5))
            data = cls._mapping(response.get("data"))
            history_groups = [
                cls._mapping(group)
                for group in (data.get("history_groups") or [])
                if isinstance(group, dict)
            ]
        except Exception:
            history_groups = []
        tasks = [
            cls._mapping(item)
            for item in (runtime.get("tasks") or [])
            if isinstance(item, dict)
        ][:5]
        task_rows = [
            [
                item.get("title"),
                item.get("media_type"),
                item.get("phase") or item.get("status"),
                f"{cls._progress(item.get('progress'))}%",
            ]
            for item in tasks
        ]
        history_rows: List[List[Any]] = []
        for group in history_groups[:5]:
            records = [
                cls._mapping(record)
                for record in (group.get("records") or [])
                if isinstance(record, dict)
            ]
            title = str(
                group.get("title")
                or group.get("name")
                or (records[0].get("title") if records else "未命名媒体")
            )
            seasons = {
                int(value)
                for value in (group.get("seasons") or [])
                if str(value).isdigit() and int(value) > 0
            }
            episodes_by_season: Dict[int, set[int]] = {}
            statuses: List[str] = []
            for record in records:
                season_value = record.get("season")
                season = int(season_value) if str(season_value).isdigit() and int(season_value) > 0 else 0
                if season:
                    seasons.add(season)
                values = [record.get("episode")]
                for key in ("episodes", "success_episodes", "notification_episodes", "target_episodes"):
                    value = record.get(key)
                    values.extend(
                        value if isinstance(value, (list, tuple, set)) else re.findall(r"\d+", str(value or "")))
                if season:
                    episodes_by_season.setdefault(season, set()).update(
                        int(value) for value in values if str(value).isdigit() and int(value) > 0
                    )
                status = str(record.get("status") or "").strip()
                if status and status not in statuses:
                    statuses.append(status)
            season_text = "、".join(f"S{season:02d}" for season in sorted(seasons)) or "-"
            episode_count = sum(len(values) for values in episodes_by_season.values())
            media_type = str(
                group.get("type")
                or (records[0].get("type") if records else "")
                or (records[0].get("media_type") if records else "")
            ).strip().lower()
            is_tv = media_type in {"tv", "电视剧", "television"}
            media_label = title
            if is_tv:
                details = [season_text] if seasons else []
                if episode_count:
                    details.append(f"{episode_count} 集")
                if details:
                    media_label = f"{title} · {' · '.join(details)}"
            history_rows.append([
                media_label,
                "、".join(statuses) or "-",
            ])
        runtime_status = str(runtime.get("status") or "idle").strip().lower()
        runtime_context = cls._mapping(runtime.get("context"))
        runtime_phase = cls._text(
            runtime_context.get("phase") or runtime.get("task")
        )
        collecting = runtime_status in {"starting", "running", "stopping"}
        empty_task_text = runtime_phase if collecting else "当前没有运行中的任务。"
        task_content = {"component": "VCard", "props": {"variant": "outlined", "class": "mb-3"}, "content": [
            {"component": "VCardTitle", "text": "订阅任务"},
            {"component": "VCardText", "content": [
                cls._table(["媒体", "类型", "阶段", "进度"], task_rows)
                if task_rows
                else {"component": "VAlert", "props": {"type": "secondary", "text": empty_task_text}}
            ]},
        ]}
        history_content = {"component": "VCard", "props": {"variant": "outlined"}, "content": [
            {"component": "VCardTitle", "text": "历史记录"},
            {"component": "VCardText", "content": [
                cls._table(["媒体", "状态"], history_rows)
                if history_rows
                else {"component": "VAlert", "props": {"type": "secondary", "text": "暂无历史记录。"}}
            ]},
        ]}
        return [
            {"component": "VCard", "content": [{"component": "VCardTitle", "text": "常用操作"},
                                               {"component": "VCardText", "content": [
                                                   {"component": "div", "props": {"class": "d-flex"}, "content": [
                                                       cls._button("立即搜索", "plugin/PanSearch/sync/start"),
                                                       cls._button("停止任务", "plugin/PanSearch/sync/stop",
                                                                   color="error"),
                                                       cls._button("刷新快照", "plugin/PanSearch/runtime",
                                                                   method="get"),
                                                   ]}]}]},
            task_content,
            history_content,
        ]
