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
                    # 插件说明
                    {
                        'component': 'VRow',
                        'content': [{
                            'component': 'VCol',
                            'props': {'cols': 12},
                            'content': [{'component': 'VAlert', 'props': {'type': 'info', 'variant': 'tonal', 'text': '自动搜索115网盘资源并转存缺失的电影和剧集，需配置115 Cookie和搜索服务。避免风控，固定执行周期为 8 小时。'}}]
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
                    
                            # ✅ 新增：cron 输入框
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
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 6}, 'content': [{'component': 'VTextField', 'props': {"clearable": True,'model': 'pansou_password', 'label': 'PanSou 密码', 'type': 'password', 'placeholder': '启用认证时填写'}}]}
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
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 4}, 'content': [{'component': 'VTextField', 'props': {"clearable": True,'model': 'nullbr_api_key', 'label': 'Nullbr API Key', 'type': 'password', 'placeholder': '请输入 API Key'}}]}
                        ]
                    },
                    # HDHive说明
                    {
                        'component': 'VRow',
                        'content': [{
                            'component': 'VCol',
                            'props': {'cols': 12},
                            'content': [{'component': 'VAlert', 'props': {'type': 'info', 'variant': 'tonal', 'text': 'HDHive资源查询：基于TMDB ID查询115网盘资源。API模式使用Cookie直接请求；Playwright模式使用浏览器模拟获取分享链接（需安装 playwright 和 firefox）'}}]
                        }]
                    },
                    # HDHive 配置
                    {
                        'component': 'VRow',
                        'content': [
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 4}, 'content': [{'component': 'VSwitch', 'props': {'model': 'hdhive_enabled', 'label': '启用 HDHive'}}]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 4}, 'content': [{'component': 'VSelect', 'props': {'model': 'hdhive_query_mode', 'label': '查询模式', 'items': [{'title': 'Playwright 模式', 'value': 'playwright'}, {'title': 'API 模式', 'value': 'api'}]}}]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 4}, 'content': [{'component': 'VSwitch', 'props': {'model': 'hdhive_auto_refresh', 'label': 'Cookie 自动刷新'}}]},
                        ]
                    },
                    # HDHive Cookie 配置
                    {
                        'component': 'VRow',
                        'content': [
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 3}, 'content': [{'component': 'VTextField', 'props': {'model': 'hdhive_username', 'label': 'HDHive 用户名', 'placeholder': 'HDHive 用户名'}}]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 3}, 'content': [{'component': 'VTextField', 'props': {"clearable": True, 'model': 'hdhive_password', 'label': 'HDHive 密码', 'type': 'password', 'placeholder': 'HDHive 密码'}}]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 6}, 'content': [{'component': 'VTextField', 'props': {"clearable": True, 'model': 'hdhive_cookie', 'label': 'HDHive Cookie', 'type': 'password', 'placeholder': 'token=xxx; csrf_access_token=xxx（启用自动刷新后会自动更新）'}}]}
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
                            {'component': 'VCol', 'props': {'cols': 6, 'md': 6}, 'content': [{'component': 'VSwitch', 'props': {'model': 'skip_other_season_dirs', 'label': '多季剧集快速转存', 'type': 'number', 'placeholder': '20', 'hint': '跳过其他季目录以减少API调用，资源搜索不到的时候需要关闭此功能'}}]}
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
            "cron": "30 */8 * * *",
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
        """
        获取插件详情页面
        :param history: 历史记录列表
        :return: 页面schema
        """
        from datetime import datetime
        
        # 统计信息
        total_count = len(history)
        success_count = len([h for h in history if h.get("status") == "成功"])
        fail_count = len([h for h in history if h.get("status") == "失败"])
        movie_count = len([h for h in history if h.get("type") == "电影"])
        tv_count = len([h for h in history if h.get("type") != "电影"])
        
        # 计算今日转存数
        today = datetime.now().strftime("%Y-%m-%d")
        today_count = len([h for h in history if h.get("time", "").startswith(today)])
        
        # 计算成功率
        success_rate = f"{(success_count / total_count * 100):.1f}%" if total_count > 0 else "0%"
        
        # 最近一次转存时间
        sorted_history = sorted(history, key=lambda x: x.get('time', ''), reverse=True) if history else []
        last_sync_time = sorted_history[0].get("time", "暂无") if sorted_history else "暂无"
        
        # 头部统计卡片 - 主要统计
        stats_header = {
            'component': 'VCard',
            'props': {'class': 'mb-4'},
            'content': [{
                'component': 'VCardText',
                'content': [
                    # 第一行：核心统计数据
                    {
                        'component': 'VRow',
                        'content': [
                            # 总转存
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
                            # 今日转存
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
                            # 成功数
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
                            # 失败数
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
                    # 第二行：媒体类型统计和最近同步时间
                    {
                        'component': 'VRow',
                        'props': {'class': 'mt-4'},
                        'content': [
                            # 电影数
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
                            # 电视剧数
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
                            # 最近同步
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
                    # 操作按钮
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
        
        # 分离电影和剧集历史
        movie_history = [h for h in sorted_history if h.get("type") == "电影"][:50]
        tv_history = [h for h in sorted_history if h.get("type") != "电影"][:50]
        all_history = sorted_history[:50]
        
        # 构建单个历史记录卡片的辅助函数
        def build_history_item(h: dict) -> dict:
            status = h.get("status", "")
            media_type = h.get("type", "")
            status_color = "success" if status == "成功" else "error" if status == "失败" else "warning"
            status_icon = "mdi-check-circle" if status == "成功" else "mdi-close-circle" if status == "失败" else "mdi-help-circle"
            
            # 媒体类型图标和颜色
            type_icon = "mdi-movie" if media_type == "电影" else "mdi-television-classic"
            type_color = "amber" if media_type == "电影" else "purple"
            
            file_name = h.get("file_name", "")
            
            # 构建标题文本
            if media_type == "电影":
                title_text = f'{h.get("title", "")} ({h.get("year", "")})'
            else:
                season = h.get("season", 0) or 0
                episode = h.get("episode", 0) or 0
                title_text = f'{h.get("title", "")} S{season:02d}E{episode:02d}'
            
            content_items = [
                # 第一行：媒体类型图标 + 标题 + 状态标签
                {
                    'component': 'div',
                    'props': {'class': 'd-flex justify-space-between align-center'},
                    'content': [
                        {
                            'component': 'div',
                            'props': {'class': 'd-flex align-center'},
                            'content': [
                                {'component': 'VIcon', 'props': {'color': type_color, 'size': 'small', 'class': 'mr-2'}, 'text': type_icon},
                                {'component': 'span', 'props': {'class': 'font-weight-bold'}, 'text': title_text}
                            ]
                        },
                        {
                            'component': 'div',
                            'props': {'class': 'd-flex align-center'},
                            'content': [
                                {'component': 'VIcon', 'props': {'color': status_color, 'size': 'x-small', 'class': 'mr-1'}, 'text': status_icon},
                                {'component': 'VChip', 'props': {'color': status_color, 'size': 'x-small', 'variant': 'flat'}, 'text': status}
                            ]
                        }
                    ]
                },
                # 第二行：时间信息
                {
                    'component': 'div',
                    'props': {'class': 'd-flex align-center mt-1'},
                    'content': [
                        {'component': 'VIcon', 'props': {'size': 'x-small', 'color': 'grey', 'class': 'mr-1'}, 'text': 'mdi-clock-outline'},
                        {'component': 'span', 'props': {'class': 'text-caption text-grey'}, 'text': h.get("time", "")}
                    ]
                }
            ]
            
            # 如果有文件名，显示文件信息
            if file_name:
                content_items.append({
                    'component': 'div',
                    'props': {'class': 'd-flex align-center mt-1'},
                    'content': [
                        {'component': 'VIcon', 'props': {'size': 'x-small', 'color': 'grey', 'class': 'mr-1'}, 'text': 'mdi-file-video'},
                        {'component': 'span', 'props': {'class': 'text-caption text-grey text-truncate'}, 'text': file_name}
                    ]
                })
            
            # 使用不同的边框颜色来区分状态
            border_style = f'border-left: 3px solid var(--v-theme-{status_color}) !important;'
            
            return {
                'component': 'VCard',
                'props': {'class': 'mb-2', 'variant': 'outlined', 'style': border_style},
                'content': [{'component': 'VCardText', 'props': {'class': 'py-2 px-3'}, 'content': content_items}]
            }
        
        # 构建带空状态的历史列表
        def build_history_list(items: List[dict], empty_text: str) -> List[dict]:
            if not items:
                return [{
                    'component': 'div',
                    'props': {'class': 'text-center py-8'},
                    'content': [
                        {'component': 'VIcon', 'props': {'size': '48', 'color': 'grey-lighten-1', 'class': 'mb-2'}, 'text': 'mdi-inbox-outline'},
                        {'component': 'div', 'props': {'class': 'text-grey'}, 'text': empty_text}
                    ]
                }]
            return [build_history_item(h) for h in items]
        
        # 使用折叠面板分类显示
        expansion_panels = {
            'component': 'VExpansionPanels',
            'props': {'variant': 'accordion', 'class': 'mt-4'},
            'content': [
                # 电影分类
                {
                    'component': 'VExpansionPanel',
                    'content': [
                        {
                            'component': 'VExpansionPanelTitle',
                            'content': [
                                {'component': 'VIcon', 'props': {'color': 'amber', 'class': 'mr-3'}, 'text': 'mdi-movie'},
                                {'component': 'span', 'props': {'class': 'font-weight-bold'}, 'text': f'电影 ({len(movie_history)})'}
                            ]
                        },
                        {
                            'component': 'VExpansionPanelText',
                            'content': build_history_list(movie_history, '暂无电影转存记录')
                        }
                    ]
                },
                # 剧集分类
                {
                    'component': 'VExpansionPanel',
                    'content': [
                        {
                            'component': 'VExpansionPanelTitle',
                            'content': [
                                {'component': 'VIcon', 'props': {'color': 'purple', 'class': 'mr-3'}, 'text': 'mdi-television-classic'},
                                {'component': 'span', 'props': {'class': 'font-weight-bold'}, 'text': f'剧集 ({len(tv_history)})'}
                            ]
                        },
                        {
                            'component': 'VExpansionPanelText',
                            'content': build_history_list(tv_history, '暂无剧集转存记录')
                        }
                    ]
                }
            ]
        }
        
        return [stats_header, expansion_panels]
