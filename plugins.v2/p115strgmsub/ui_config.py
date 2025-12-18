"""
UI配置模块
负责生成插件的配置表单和详情页面
"""
from typing import List, Dict, Any, Tuple
from app.core.config import settings
from app.db.subscribe_oper import SubscribeOper
from app.schemas.types import MediaType
from app.log import logger


class UIConfig:
    """UI配置管理类"""

    @staticmethod
    def get_subscribe_options() -> List[Dict[str, Any]]:
        """
        获取订阅选项列表（电影和电视剧）
        :return: 订阅选项列表 [{"title": "显示名", "value": id}, ...]
        """
        try:
            subscribes = SubscribeOper().list('R')
            if not subscribes:
                return []

            options = []
            for s in subscribes:
                # 根据类型显示不同格式
                type_label = "[剧]" if s.type == MediaType.TV.value else "[影]"
                if s.type == MediaType.TV.value:
                    # 电视剧显示季号
                    display = f"{type_label} {s.name} ({s.year}) S{s.season or 1}" if s.year else f"{type_label} {s.name} S{s.season or 1}"
                else:
                    # 电影不显示季号
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
    def get_form() -> Tuple[List[dict], Dict[str, Any]]:
        """
        获取插件配置表单
        :return: (表单schema, 默认配置)
        """
        # 获取订阅选项
        subscribe_options = UIConfig.get_subscribe_options()

        form_schema = [
            {
                'component': 'VForm',
                'content': [
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12},
                                'content': [{
                                    'component': 'VAlert',
                                    'props': {
                                        'type': 'info',
                                        'variant': 'tonal',
                                        'text': '本插件会自动获取 MoviePilot 中的电影和电视剧订阅，搜索 115 网盘资源，' \
                                                '并将缺失的电影和剧集转存到您的 115 网盘中。需要配置 115 Cookie 和 PanSou 搜索服务。'
                                    }
                                }]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 3},
                                'content': [{
                                    'component': 'VSwitch',
                                    'props': {'model': 'enabled', 'label': '启用插件'}
                                }]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 3},
                                'content': [{
                                    'component': 'VSwitch',
                                    'props': {'model': 'notify', 'label': '发送通知'}
                                }]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 3},
                                'content': [{
                                    'component': 'VSwitch',
                                    'props': {'model': 'onlyonce', 'label': '立即运行一次'}
                                }]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 4},
                                'content': [{
                                    'component': 'VCronField',
                                    'props': {
                                        'model': 'cron',
                                        'label': '执行周期',
                                        'placeholder': '5位cron表达式，留空默认每6小时'
                                    }
                                }]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 4},
                                'content': [{
                                    'component': 'VTextField',
                                    'props': {
                                        'model': 'save_path',
                                        'label': '电视剧转存目录',
                                        'placeholder': '/我的接收/MoviePilot/TV'
                                    }
                                }]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 4},
                                'content': [{
                                    'component': 'VTextField',
                                    'props': {
                                        'model': 'movie_save_path',
                                        'label': '电影转存目录',
                                        'placeholder': '/我的接收/MoviePilot/Movie'
                                    }
                                }]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12},
                                'content': [{
                                    'component': 'VAlert',
                                    'props': {
                                        'type': 'warning',
                                        'variant': 'tonal',
                                        'title': '115 网盘配置',
                                        'text': '请从浏览器中获取 115 网盘的 Cookie，包含 UID、CID、SEID、KID 等字段。'
                                    }
                                }]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12},
                                'content': [{
                                    'component': 'VTextField',
                                    'props': {
                                        'model': 'cookies',
                                        'label': '115 Cookie',
                                        'rows': 1,
                                        'type': 'password',
                                        'placeholder': 'UID=xxx; CID=xxx; SEID=xxx; KID=xxx'
                                    }
                                }]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12},
                                'content': [{
                                    'component': 'VAlert',
                                    'props': {
                                        'type': 'info',
                                        'variant': 'tonal',
                                        'title': 'PanSou 搜索服务配置',
                                        'text': 'PanSou 是网盘资源聚合搜索服务，用于搜索 115 网盘分享链接。' \
                                                '如果您的 PanSou 服务需要登录认证，请填写用户名和密码。'
                                    }
                                }]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 4},
                                'content': [{
                                    'component': 'VSwitch',
                                    'props': {'model': 'pansou_enabled', 'label': '启用 PanSou 搜索'}
                                }]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 4},
                                'content': [{
                                    'component': 'VSwitch',
                                    'props': {'model': 'pansou_auth_enabled', 'label': '启用认证'}
                                }]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 4},
                                'content': [{
                                    'component': 'VTextField',
                                    'props': {
                                        'model': 'pansou_url',
                                        'label': 'PanSou API 地址',
                                        'placeholder': 'https://your-pansou-api.com'
                                    }
                                }]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 6},
                                'content': [{
                                    'component': 'VTextField',
                                    'props': {
                                        'model': 'pansou_username',
                                        'label': 'PanSou 用户名',
                                        'placeholder': '用户名（启用认证时填写）'
                                    }
                                }]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 6},
                                'content': [{
                                    'component': 'VTextField',
                                    'props': {
                                        'model': 'pansou_password',
                                        'label': 'PanSou 密码',
                                        'type': 'password',
                                        'placeholder': '密码（启用认证时填写）'
                                    }
                                }]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12},
                                'content': [{
                                    'component': 'VTextField',
                                    'props': {
                                        'model': 'pansou_channels',
                                        'label': 'TG 搜索频道',
                                        'placeholder': '频道列表，用逗号分隔，例如: channel1,channel2,channel3'
                                    }
                                }]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12},
                                'content': [{
                                    'component': 'VAlert',
                                    'props': {
                                        'type': 'info',
                                        'variant': 'tonal',
                                        'title': 'Nullbr 资源查询配置',
                                        'text': 'Nullbr 是基于 TMDB ID 的精准资源查询服务，可通过 TMDB ID 直接查询 115 网盘资源，准确度较高。需要配置 APP ID 和 API Key。'
                                    }
                                }]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 3},
                                'content': [{
                                    'component': 'VSwitch',
                                    'props': {
                                        'model': 'nullbr_enabled',
                                        'label': '启用 Nullbr'
                                    }
                                }]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 3},
                                'content': [{
                                    'component': 'VSwitch',
                                    'props': {
                                        'model': 'nullbr_priority',
                                        'label': 'Nullbr 优先'
                                    }
                                }]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 6},
                                'content': [{
                                    'component': 'VTextField',
                                    'props': {
                                        'model': 'nullbr_api_key',
                                        'label': 'Nullbr API Key',
                                        'placeholder': '请输入 API Key',
                                        'type': 'password'
                                    }
                                }]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12},
                                'content': [{
                                    'component': 'VAlert',
                                    'props': {
                                        'type': 'info',
                                        'variant': 'tonal',
                                        'title': '订阅过滤配置',
                                        'text': '选择不需要本插件处理的订阅，这些订阅将被跳过。'
                                    }
                                }]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12},
                                'content': [{
                                    'component': 'VSelect',
                                    'props': {
                                        'model': 'exclude_subscribes',
                                        'label': '排除订阅',
                                        'multiple': True,
                                        'chips': True,
                                        'clearable': True,
                                        'closable-chips': True,
                                        'hint': '选择不需要处理的订阅（电影或电视剧）',
                                        'persistent-hint': True,
                                        'items': subscribe_options
                                    }
                                }]
                            }
                        ]
                    }
                ]
            }
        ]

        default_config = {
            "enabled": False,
            "notify": True,
            "onlyonce": False,
            "only_115": True,
            "cron": "30 * * * *",
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
            "nullbr_app_id": "",
            "nullbr_api_key": "",
            "nullbr_priority": True,
            "exclude_subscribes": []
        }
        
        return form_schema, default_config
    
    @staticmethod
    def get_page(history: List[dict]) -> List[dict]:
        """
        获取插件详情页面
        :param history: 历史记录列表
        :return: 页面schema
        """
        # 统计信息
        total_count = len(history)
        success_count = len([h for h in history if h.get("status") == "成功"])
        fail_count = len([h for h in history if h.get("status") == "失败"])
        
        # 头部统计卡片
        header = {
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
                                'props': {'cols': 4},
                                'content': [{
                                    'component': 'div',
                                    'props': {'class': 'text-center'},
                                    'content': [
                                        {'component': 'div', 'props': {'class': 'text-h5 font-weight-bold'}, 'text': str(total_count)},
                                        {'component': 'div', 'props': {'class': 'text-caption'}, 'text': '总记录'}
                                    ]
                                }]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 4},
                                'content': [{
                                    'component': 'div',
                                    'props': {'class': 'text-center'},
                                    'content': [
                                        {'component': 'div', 'props': {'class': 'text-h5 font-weight-bold text-success'}, 'text': str(success_count)},
                                        {'component': 'div', 'props': {'class': 'text-caption'}, 'text': '成功'}
                                    ]
                                }]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 4},
                                'content': [{
                                    'component': 'div',
                                    'props': {'class': 'text-center'},
                                    'content': [
                                        {'component': 'div', 'props': {'class': 'text-h5 font-weight-bold text-error'}, 'text': str(fail_count)},
                                        {'component': 'div', 'props': {'class': 'text-caption'}, 'text': '失败'}
                                    ]
                                }]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'props': {'class': 'mt-2'},
                        'content': [{
                            'component': 'VCol',
                            'props': {'cols': 12, 'class': 'text-center'},
                            'content': [{
                                'component': 'VBtn',
                                'props': {'color': 'error', 'variant': 'outlined', 'size': 'small', 'prepend-icon': 'mdi-delete'},
                                'text': '清空历史记录',
                                'events': {
                                    'click': {
                                        'api': '/plugin/P115StrgmSub/clear_history',
                                        'method': 'post',
                                        'params': {'apikey': settings.API_TOKEN}
                                    }
                                }
                            }]
                        }]
                    }
                ]
            }]
        }
        
        if not history:
            return [header, {'component': 'div', 'text': '暂无转存记录', 'props': {'class': 'text-center text-grey mt-4'}}]
        
        # 数据按时间降序排序
        history = sorted(history, key=lambda x: x.get('time', ''), reverse=True)
        
        # 拼装历史记录列表
        contents = []
        for h in history[:50]:  # 只显示最近50条
            status = h.get("status", "")
            status_color = "success" if status == "成功" else "error" if status == "失败" else "warning"
            status_icon = "mdi-check-circle" if status == "成功" else "mdi-close-circle" if status == "失败" else "mdi-help-circle"
            
            file_name = h.get("file_name", "")
            content_items = [
                {
                    'component': 'div',
                    'props': {'class': 'd-flex justify-space-between align-center'},
                    'content': [
                        {
                            'component': 'div',
                            'props': {'class': 'd-flex align-center'},
                            'content': [
                                {'component': 'VIcon', 'props': {'color': status_color, 'size': 'small', 'class': 'mr-2'}, 'text': status_icon},
                                {'component': 'span', 'props': {'class': 'font-weight-bold'}, 
                                 'text': f'{h.get("title", "")} S{h.get("season", 0):02d}E{h.get("episode", 0):02d}'}
                            ]
                        },
                        {'component': 'VChip', 'props': {'color': status_color, 'size': 'x-small', 'variant': 'flat'}, 'text': status}
                    ]
                },
                {'component': 'div', 'props': {'class': 'text-caption text-grey mt-1'}, 'text': h.get("time", "")}
            ]
            
            if file_name:
                content_items.append(
                    {'component': 'div', 'props': {'class': 'text-caption text-grey text-truncate'}, 'text': f'文件:{file_name}'}
                )
            
            contents.append({
                'component': 'VCard',
                'props': {'class': 'mb-2', 'variant': 'outlined'},
                'content': [{'component': 'VCardText', 'props': {'class': 'py-2'}, 'content': content_items}]
            })
        
        return [
            header,
            {'component': 'div', 'props': {'class': 'text-subtitle-2 mb-2'}, 'text': f'最近 {min(len(history), 50)} 条记录'},
            {'component': 'div', 'content': contents}
        ]
