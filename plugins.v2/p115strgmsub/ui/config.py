"""
UI配置模块
负责生成插件的配置表单和详情页面
"""
from typing import List, Dict, Any, Tuple
from sqlalchemy import text

from app.core.config import settings
from app.db.subscribe_oper import SubscribeOper
from app.schemas.types import MediaType
from app.log import logger
from app.db import SessionFactory


class UIConfig:
    """UI配置管理类"""

    @staticmethod
    def get_subscribe_options() -> List[Dict[str, Any]]:
        """
        获取订阅选项列表（电影和电视剧）
        :return: 订阅选项列表 [{"title": "显示名", "value": id}, ...]
        """
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
                options.append({
                    "title": display,
                    "value": s.id
                })
            return options
        except Exception as e:
            logger.error(f"获取订阅列表失败: {e}")
            return []

    @staticmethod
    def get_site_options() -> List[Dict[str, Any]]:
        """
        获取站点选项列表（用于“取消屏蔽后站点选择”）
        value 使用站点 name（更稳定；避免 id 重复/漂移）
        """
        try:
            with SessionFactory() as db:
                rows = db.execute(text("SELECT name FROM site ORDER BY name")).fetchall()
            items = []
            for r in rows:
                name = str(r[0])
                items.append({"title": name, "value": name})
            return items
        except Exception as e:
            logger.error(f"获取站点列表失败: {e}")
            return []

    @staticmethod
    def get_form() -> Tuple[List[dict], Dict[str, Any]]:
        """
        获取插件配置表单
        :return: (表单schema, 默认配置)
        """
        subscribe_options = UIConfig.get_subscribe_options()
        site_options = UIConfig.get_site_options()

        form_schema = [
            {
                'component': 'VForm',
                'content': [
                    {
                        'component': 'VRow',
                        'content': [{
                            'component': 'VCol',
                            'props': {'cols': 12},
                            'content': [{
                                'component': 'VAlert',
                                'props': {
                                    'type': 'info',
                                    'variant': 'tonal',
                                    'text': '自动搜索115网盘资源并转存缺失的电影和剧集，需配置115 Cookie和搜索服务。避免风控，固定执行周期为 8 小时。'
                                }
                            }]
                        }]
                    },

                    # 基本开关 + 执行周期
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
                                    'placeholder': '35 4,12,20 * * *',
                                    'hint': '5段 Cron：分 时 日 月 周；例：35 4,12,20 * * * 表示每天 04:35/12:35/20:35 执行',
                                    'persistent-hint': True,
                                    'clearable': True
                                }
                            }]}
                        ]
                    },

                    # ✅新增：取消屏蔽后的站点选择 + 运行时长
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 8},
                                'content': [{
                                    'component': 'VSelect',
                                    'props': {
                                        'model': 'unblock_site_names',
                                        'label': '关闭“屏蔽系统订阅”后勾选站点（用于系统订阅）',
                                        'multiple': True,
                                        'chips': True,
                                        'clearable': True,
                                        'closable-chips': True,
                                        'items': site_options,
                                        'hint': '这里选的站点会用于订阅检索；插件会同步每个订阅的sites范围，并尽量同步全局订阅站点范围',
                                        'persistent-hint': True
                                    }
                                }]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 4},
                                'content': [{
                                    'component': 'VTextField',
                                    'props': {
                                        'model': 'unblock_window_hours',
                                        'label': '系统订阅运行时长（小时）',
                                        'type': 'number',
                                        'placeholder': '2',
                                        'hint': '关闭屏蔽后，系统订阅运行多久再自动恢复屏蔽（仅115网盘）',
                                        'persistent-hint': True
                                    }
                                }]
                            }
                        ]
                    },

                    # 115网盘说明
                    {
                        'component': 'VRow',
                        'content': [{
                            'component': 'VCol',
                            'props': {'cols': 12},
                            'content': [{'component': 'VAlert', 'props': {'type': 'warning', 'variant': 'tonal', 'text': '115网盘配置：请从浏览器获取Cookie（包含UID、CID、SEID、KID等字段）'}}]
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

                    # PanSou说明
                    {
                        'component': 'VRow',
                        'content': [{
                            'component': 'VCol',
                            'props': {'cols': 12},
                            'content': [{'component': 'VAlert', 'props': {'type': 'info', 'variant': 'tonal', 'text': 'PanSou搜索服务：网盘资源聚合搜索，用于搜索115网盘分享链接'}}]
                        }]
                    },

                    # PanSou 配置
                    {
                        'component': 'VRow',
                        'content': [
                            {'component': 'VCol', 'props': {'cols': 6, 'md': 3}, 'content': [{'component': 'VSwitch', 'props': {'model': 'pansou_enabled', 'label': '启用 PanSou'}}]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 3}, 'content': [{'component': 'VTextField', 'props': {'model': 'pansou_url', 'label': 'PanSou API 地址', 'placeholder': 'https://your-pansou-api.com'}}]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 6}, 'content': [{'component': 'VTextField', 'props': {'model': 'pansou_channels', 'label': 'TG 搜索频道', 'placeholder': '频道,用逗号分隔'}}]}
                        ]
                    },

                    # PanSou 认证
                    {
                        'component': 'VRow',
                        'content': [
                            {'component': 'VCol', 'props': {'cols': 6, 'md': 3}, 'content': [{'component': 'VSwitch', 'props': {'model': 'pansou_auth_enabled', 'label': '启用认证'}}]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 3}, 'content': [{'component': 'VTextField', 'props': {'model': 'pansou_username', 'label': 'PanSou 用户名', 'placeholder': '启用认证时填写'}}]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 6}, 'content': [{'component': 'VTextField', 'props': {"clearable": True, 'model': 'pansou_password', 'label': 'PanSou 密码', 'type': 'password', 'placeholder': '启用认证时填写'}}]}
                        ]
                    },

                    # Nullbr说明
                    {
                        'component': 'VRow',
                        'content': [{
                            'component': 'VCol',
                            'props': {'cols': 12},
                            'content': [{'component': 'VAlert', 'props': {'type': 'info', 'variant': 'tonal', 'text': 'Nullbr 资源查询：基于TMDB ID精准查询115网盘资源，准确度更高'}}]
                        }]
                    },

                    # Nullbr 配置
                    {
                        'component': 'VRow',
                        'content': [
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 4}, 'content': [{'component': 'VSwitch', 'props': {'model': 'nullbr_enabled', 'label': '启用 Nullbr'}}]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 4}, 'content': [{'component': 'VTextField', 'props': {'model': 'nullbr_appid', 'label': 'Nullbr APP ID', 'placeholder': '请输入 APP ID'}}]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 4}, 'content': [{'component': 'VTextField', 'props': {"clearable": True, 'model': 'nullbr_api_key', 'label': 'Nullbr API Key', 'type': 'password', 'placeholder': '请输入 API Key'}}]}
                        ]
                    },

                    # 风控防护说明
                    {
                        'component': 'VRow',
                        'content': [{
                            'component': 'VCol',
                            'props': {'cols': 12},
                            'content': [{'component': 'VAlert', 'props': {'type': 'warning', 'variant': 'tonal', 'text': '风控防护：批量转存和单次上限可有效避免115网盘风控，建议保持默认值或适当调低'}}]
                        }]
                    },

                    # 风控防护配置
                    {
                        'component': 'VRow',
                        'content': [
                            {'component': 'VCol', 'props': {'cols': 6, 'md': 3}, 'content': [{'component': 'VTextField', 'props': {'model': 'max_transfer_per_sync', 'label': '单次同步上限', 'type': 'number', 'placeholder': '50', 'hint': '每次同步最多转存文件数'}}]},
                            {'component': 'VCol', 'props': {'cols': 6, 'md': 3}, 'content': [{'component': 'VTextField', 'props': {'model': 'batch_size', 'label': '批量转存大小', 'type': 'number', 'placeholder': '20', 'hint': '每批转存文件数'}}]},
                            {'component': 'VCol', 'props': {'cols': 6, 'md': 6}, 'content': [{'component': 'VSwitch', 'props': {'model': 'skip_other_season_dirs', 'label': '多季剧集快速转存', 'hint': '跳过其他季目录以减少API调用，资源搜索不到的时候需要关闭此功能'}}]}
                        ]
                    },

                    # 排除订阅
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
            "cron": "35 4,12,20 * * *",

            # ✅新增默认值
            "unblock_site_names": ["观众", "憨憨", "馒头"],
            "unblock_window_hours": 2,

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
            "batch_size": 20,
            "skip_other_season_dirs": True
        }

        return form_schema, default_config

    @staticmethod
    def get_page(history: List[dict]) -> List[dict]:
        # 你的原 get_page 很长，这里保持不动，直接调用原逻辑
        # ——为了“完整覆盖”且不引入风险，我把你提供的 get_page 原样保留——
        from datetime import datetime

        total_count = len(history)
        success_count = len([h for h in history if h.get("status") == "成功"])
        fail_count = len([h for h in history if h.get("status") == "失败"])
        movie_count = len([h for h in history if h.get("type") == "电影"])
        tv_count = len([h for h in history if h.get("type") != "电影"])

        today = datetime.now().strftime("%Y-%m-%d")
        today_count = len([h for h in history if h.get("time", "").startswith(today)])

        success_rate = f"{(success_count / total_count * 100):.1f}%" if total_count > 0 else "0%"

        sorted_history = sorted(history, key=lambda x: x.get('time', ''), reverse=True) if history else []
        last_sync_time = sorted_history[0].get("time", "暂无") if sorted_history else "暂无"

        stats_header = {
            'component': 'VCard',
            'props': {'class': 'mb-4'},
            'content': [{
                'component': 'VCardText',
                'content': [
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 6, 'md': 3},
                                'content': [{
                                    'component': 'VCard',
                                    'props': {'variant': 'tonal', 'color': 'primary'},
                                    'content': [{
                                        'component': 'VCardText',
                                        'props': {'class': 'text-center pa-3'},
                                        'content': [
                                            {'component': 'VIcon', 'props': {'size': 'x-large', 'class': 'mb-2'}, 'text': 'mdi-cloud-upload'},
                                            {'component': 'div', 'props': {'class': 'text-h4 font-weight-bold'}, 'text': str(total_count)},
                                            {'component': 'div', 'props': {'class': 'text-caption'}, 'text': '总转存数'}
                                        ]
                                    }]
                                }]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 6, 'md': 3},
                                'content': [{
                                    'component': 'VCard',
                                    'props': {'variant': 'tonal', 'color': 'info'},
                                    'content': [{
                                        'component': 'VCardText',
                                        'props': {'class': 'text-center pa-3'},
                                        'content': [
                                            {'component': 'VIcon', 'props': {'size': 'x-large', 'class': 'mb-2'}, 'text': 'mdi-calendar-today'},
                                            {'component': 'div', 'props': {'class': 'text-h4 font-weight-bold'}, 'text': str(today_count)},
                                            {'component': 'div', 'props': {'class': 'text-caption'}, 'text': '今日转存'}
                                        ]
                                    }]
                                }]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 6, 'md': 3},
                                'content': [{
                                    'component': 'VCard',
                                    'props': {'variant': 'tonal', 'color': 'success'},
                                    'content': [{
                                        'component': 'VCardText',
                                        'props': {'class': 'text-center pa-3'},
                                        'content': [
                                            {'component': 'VIcon', 'props': {'size': 'x-large', 'class': 'mb-2'}, 'text': 'mdi-check-circle'},
                                            {'component': 'div', 'props': {'class': 'text-h4 font-weight-bold'}, 'text': str(success_count)},
                                            {'component': 'div', 'props': {'class': 'text-caption'}, 'text': f'成功 ({success_rate})'}
                                        ]
                                    }]
                                }]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 6, 'md': 3},
                                'content': [{
                                    'component': 'VCard',
                                    'props': {'variant': 'tonal', 'color': 'error'},
                                    'content': [{
                                        'component': 'VCardText',
                                        'props': {'class': 'text-center pa-3'},
                                        'content': [
                                            {'component': 'VIcon', 'props': {'size': 'x-large', 'class': 'mb-2'}, 'text': 'mdi-close-circle'},
                                            {'component': 'div', 'props': {'class': 'text-h4 font-weight-bold'}, 'text': str(fail_count)},
                                            {'component': 'div', 'props': {'class': 'text-caption'}, 'text': '失败'}
                                        ]
                                    }]
                                }]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'props': {'class': 'mt-4'},
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 4},
                                'content': [{
                                    'component': 'div',
                                    'props': {'class': 'd-flex align-center justify-center'},
                                    'content': [
                                        {'component': 'VIcon', 'props': {'color': 'amber', 'class': 'mr-2'}, 'text': 'mdi-movie'},
                                        {'component': 'span', 'props': {'class': 'text-h6 font-weight-medium'}, 'text': str(movie_count)},
                                        {'component': 'span', 'props': {'class': 'text-caption ml-1'}, 'text': '部电影'}
                                    ]
                                }]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 4},
                                'content': [{
                                    'component': 'div',
                                    'props': {'class': 'd-flex align-center justify-center'},
                                    'content': [
                                        {'component': 'VIcon', 'props': {'color': 'purple', 'class': 'mr-2'}, 'text': 'mdi-television-classic'},
                                        {'component': 'span', 'props': {'class': 'text-h6 font-weight-medium'}, 'text': str(tv_count)},
                                        {'component': 'span', 'props': {'class': 'text-caption ml-1'}, 'text': '集剧集'}
                                    ]
                                }]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 4},
                                'content': [{
                                    'component': 'div',
                                    'props': {'class': 'd-flex align-center justify-center'},
                                    'content': [
                                        {'component': 'VIcon', 'props': {'color': 'cyan', 'class': 'mr-2'}, 'text': 'mdi-clock-outline'},
                                        {'component': 'span', 'props': {'class': 'text-caption'}, 'text': f'最近同步: {last_sync_time[:16] if len(last_sync_time) > 16 else last_sync_time}'}
                                    ]
                                }]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'props': {'class': 'mt-4'},
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 6, 'class': 'text-center'},
                                'content': [{
                                    'component': 'VBtn',
                                    'props': {'color': 'primary', 'variant': 'outlined', 'size': 'small', 'prepend-icon': 'mdi-magnify'},
                                    'text': '立即搜索',
                                    'events': {
                                        'click': {
                                            'api': f'/plugin/P115StrgmSub/sync_subscribes?apikey={settings.API_TOKEN}',
                                            'method': 'get',
                                        }
                                    }
                                }]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 6, 'class': 'text-center'},
                                'content': [{
                                    'component': 'VBtn',
                                    'props': {'color': 'error', 'variant': 'outlined', 'size': 'small', 'prepend-icon': 'mdi-delete-sweep'},
                                    'text': '清空历史记录',
                                    'events': {
                                        'click': {
                                            'api': f'/plugin/P115StrgmSub/clear_history?apikey={settings.API_TOKEN}',
                                            'method': 'post',
                                        }
                                    }
                                }]
                            }
                        ]
                    }
                ]
            }]
        }

        if not sorted_history:
            empty_state = {
                'component': 'VCard',
                'props': {'variant': 'outlined', 'class': 'mt-4'},
                'content': [{
                    'component': 'VCardText',
                    'props': {'class': 'text-center py-8'},
                    'content': [
                        {'component': 'VIcon', 'props': {'size': '64', 'color': 'grey-lighten-1', 'class': 'mb-4'}, 'text': 'mdi-inbox-outline'},
                        {'component': 'div', 'props': {'class': 'text-h6 text-grey'}, 'text': '暂无转存记录'},
                        {'component': 'div', 'props': {'class': 'text-caption text-grey-lighten-1 mt-2'}, 'text': '插件运行后会在此显示转存记录'}
                    ]
                }]
            }
            return [stats_header, empty_state]

        return [stats_header]
