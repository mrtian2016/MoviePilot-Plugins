"""
UI配置模块
负责生成插件的配置表单和详情页面
"""
from typing import List, Dict, Any, Tuple
from app.core.config import settings
from app.db.subscribe_oper import SubscribeOper
from app.schemas.types import MediaType
from app.log import logger
from app.db import SessionFactory
from sqlalchemy import text


class UIConfig:
    """UI配置管理类"""

    @staticmethod
    def get_subscribe_options() -> List[Dict[str, Any]]:
        try:
            with SessionFactory() as db:
                subscribes = SubscribeOper(db=db).list("N,R")
            if not subscribes:
                return []

            options = []
            for s in subscribes:
                type_label = "[剧]" if s.type == MediaType.TV.value else "[影]"
                if s.type == MediaType.TV.value:
                    display = f"{type_label} {s.name} ({s.year}) S{s.season or 1}" if s.year else f"{type_label} {s.name} S{s.season or 1}"
                else:
                    display = f"{type_label} {s.name} ({s.year})" if s.year else f"{type_label} {s.name}"
                options.append({"title": display, "value": s.id})
            return options
        except Exception as e:
            logger.error(f"获取订阅列表失败: {e}")
            return []

    @staticmethod
    def get_site_name_options() -> List[Dict[str, Any]]:
        """获取站点名称列表（用于多选），value=站点名"""
        try:
            with SessionFactory() as db:
                rows = db.execute(text("SELECT name FROM site ORDER BY name")).fetchall()
            items = []
            for r in rows:
                name = str(r[0])
                if not name:
                    continue
                items.append({"title": name, "value": name})
            return items
        except Exception as e:
            logger.error(f"获取站点列表失败: {e}")
            return []

    @staticmethod
    def get_form() -> Tuple[List[dict], Dict[str, Any]]:
        subscribe_options = UIConfig.get_subscribe_options()
        site_items = UIConfig.get_site_name_options()

        form_schema = [
            {
                'component': 'VForm',
                'content': [
                    {
                        'component': 'VRow',
                        'content': [{
                            'component': 'VCol',
                            'props': {'cols': 12},
                            'content': [{'component': 'VAlert', 'props': {'type': 'info', 'variant': 'tonal',
                                'text': '自动搜索115网盘资源并转存缺失的电影和剧集，需配置115 Cookie和搜索服务。避免风控，固定执行周期为 8 小时。'}}]
                        }]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 2}, 'content': [{'component': 'VSwitch', 'props': {'model': 'enabled', 'label': '启用插件'}}]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 2}, 'content': [{'component': 'VSwitch', 'props': {'model': 'notify', 'label': '发送通知'}}]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 2}, 'content': [{'component': 'VSwitch', 'props': {'model': 'block_system_subscribe', 'label': '屏蔽系统订阅'}}]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 2}, 'content': [{'component': 'VSwitch', 'props': {'model': 'onlyonce', 'label': '立即运行'}}]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 4}, 'content': [{
                                'component': 'VTextField',
                                'props': {
                                    'model': 'cron',
                                    'label': '执行周期（Cron）',
                                    'placeholder': '30 */8 * * *',
                                    'hint': '5段 Cron：分 时 日 月 周；例：30 */8 * * * 表示每8小时的30分执行',
                                    'persistent-hint': True,
                                    'clearable': True
                                }
                            }]}
                        ]
                    },

                    # ✅新增：取消屏蔽后站点/窗口/延迟（-1禁用触发条件1，0禁用窗口）
                    {
                        'component': 'VRow',
                        'content': [
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 6}, 'content': [{
                                'component': 'VSelect',
                                'props': {
                                    'model': 'unblock_site_names',
                                    'label': '取消屏蔽后订阅站点选择（多选）',
                                    'items': site_items,
                                    'multiple': True,
                                    'chips': True,
                                    'clearable': True,
                                    'closable-chips': True,
                                    'hint': '为空表示不进入窗口期（保持屏蔽，仅115）',
                                    'persistent-hint': True
                                }
                            }]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 3}, 'content': [{
                                'component': 'VTextField',
                                'props': {
                                    'model': 'unblock_window_hours',
                                    'label': '取消屏蔽窗口期（小时）',
                                    'type': 'number',
                                    'placeholder': '2',
                                    'hint': '设为0表示不进入窗口期（保持屏蔽，仅115）',
                                    'persistent-hint': True,
                                    'clearable': True
                                }
                            }]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 3}, 'content': [{
                                'component': 'VTextField',
                                'props': {
                                    'model': 'unblock_delay_minutes',
                                    'label': '最后一次任务后延迟（分钟）',
                                    'type': 'number',
                                    'placeholder': '5',
                                    'hint': '设为-1表示禁用触发条件1（保持屏蔽，仅115）',
                                    'persistent-hint': True,
                                    'clearable': True
                                }
                            }]}
                        ]
                    },

                    # 115网盘说明
                    {
                        'component': 'VRow',
                        'content': [{
                            'component': 'VCol',
                            'props': {'cols': 12},
                            'content': [{'component': 'VAlert', 'props': {'type': 'warning', 'variant': 'tonal',
                                'text': '115网盘配置：请从浏览器获取Cookie（包含UID、CID、SEID、KID等字段）'}}]
                        }]
                    },
                    # 转存目录 + 115 Cookie
                    {
                        'component': 'VRow',
                        'content': [
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 4}, 'content': [{'component': 'VTextField', 'props': {'model': 'save_path', 'label': '电视剧转存目录', 'placeholder': '/我的接收/MoviePilot/TV'}}]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 4}, 'content': [{'component': 'VTextField', 'props': {'model': 'movie_save_path', 'label': '电影转存目录', 'placeholder': '/我的接收/MoviePilot/Movie'}}]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 4}, 'content': [{'component': 'VTextField', 'props': {'model': 'cookies', 'label': '115 Cookie', 'type': 'password', 'placeholder': 'UID=xxx; CID=xxx; SEID=xxx'}}]}
                        ]
                    },

                    # 下面保持原表单（PanSou/Nullbr/HDHive/风控/排除订阅）不变
                    {
                        'component': 'VRow',
                        'content': [{
                            'component': 'VCol',
                            'props': {'cols': 12},
                            'content': [{'component': 'VSelect', 'props': {'model': 'exclude_subscribes', 'label': '排除订阅（选择不需要本插件处理的订阅）', 'multiple': True, 'chips': True, 'clearable': True, 'closable-chips': True, 'items': subscribe_options}}]
                        }]
                    }
                ]
            }
        ]

        default_config = {
            "enabled": False,
            "notify": True,
            "onlyonce": False,
            "only_115": True,
            "cron": "30 */8 * * *",

            "unblock_site_names": ["观众", "憨憨", "馒头"],
            "unblock_window_hours": 2,
            "unblock_delay_minutes": 5,

            "save_path": "/我的接收/MoviePilot/TV",
            "movie_save_path": "/我的接收/MoviePilot/Movie",
            "cookies": "",
            "pansou_enabled": True,
            "pansou_url": "https://so.252035.xyz/",
            "pansou_username": "",
            "pansou_password": "",
            "pansou_auth_enabled": False,
            "pansou_channels": "QukanMovie",
            "nullbr_enabled": False,
            "nullbr_appid": "",
            "nullbr_api_key": "",
            "hdhive_enabled": False,
            "hdhive_query_mode": "playwright",
            "hdhive_username": "",
            "hdhive_password": "",
            "hdhive_cookie": "",
            "hdhive_auto_refresh": False,
            "hdhive_refresh_before": 86400,
            "exclude_subscribes": [],
            "block_system_subscribe": False,
            "max_transfer_per_sync": 50,
            "batch_size": 20
        }

        return form_schema, default_config

    @staticmethod
    def get_page(history: List[dict]) -> List[dict]:
        # 你的原 get_page 很长，不影响本次需求，保持你原实现即可
        return []
