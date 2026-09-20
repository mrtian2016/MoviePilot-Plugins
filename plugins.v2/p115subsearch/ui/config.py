"""
UI配置模块
负责生成插件的配置表单和详情页面
"""
from typing import List, Dict, Any, Tuple, Optional
from app.db.subscribe_oper import SubscribeOper
from app.schemas.types import MediaType
from app.log import logger
from app.db import SessionFactory
from sqlalchemy import text

try:
    # 榜单来源目录与后端共用同一份定义，避免 UI 与客户端清单漂移（v1.9.0）
    # v1.9.1：默认来源同样取后端常量，保证「首次使用」默认值与来源目录同源
    from ..clients.leaderboard import LEADERBOARD_SOURCES, DEFAULT_LEADERBOARD_SOURCES
except Exception:  # pragma: no cover - 脱离插件包单独加载时退化为内置常量
    LEADERBOARD_SOURCES = []
    DEFAULT_LEADERBOARD_SOURCES = ["tmdb_trending"]


class UIConfig:
    """UI配置管理类"""

    @staticmethod
    def get_leaderboard_source_options() -> List[Dict[str, Any]]:
        """
        获取榜单来源多选项（v1.9.0）
        :return: [{"title": "TMDB 热门电影", "value": "tmdb_movies"}, ...]
        """
        options = []
        for item in LEADERBOARD_SOURCES or []:
            if not isinstance(item, dict):
                continue
            source_id = str(item.get("id") or "").strip()
            if not source_id:
                continue
            options.append({"title": str(item.get("name") or source_id), "value": source_id})
        return options

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
                options.append({"title": display, "value": s.id})
            return options
        except Exception as e:
            logger.error(f"获取订阅列表失败: {e}")
            return []

    @staticmethod
    def get_site_name_options() -> List[Dict[str, Any]]:
        """
        获取站点名称列表（用于多选）
        items: [{'title': '站点名', 'value': '站点名'}]
        """
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
    def _section_title(text: str) -> Dict[str, Any]:
        """生成一个区块小标题行（用于主界面的分区标识）。"""
        return {
            'component': 'VRow',
            'content': [{
                'component': 'VCol',
                'props': {'cols': 12},
                'content': [{
                    'component': 'div',
                    'props': {'class': 'text-h6 font-weight-bold mt-2'},
                    'text': text
                }]
            }]
        }

    @staticmethod
    def _account_card(entry: Dict[str, Any]) -> Dict[str, Any]:
        """
        渲染一张账户信息卡片（v1.8.3，对齐网盘搜索助手的 AccountInfo 组件）。

        卡片结构：
            头像 + 用户名 + Badge + VIP chip + 刷新按钮
            积分行（有积分时）
            容量行（网盘卡片）
            两列 details 网格（有 details 时）
            未连接时显示红色提示文案

        :param entry: {"key": "drive:p115", "title": "115 网盘", "account": {...}}
        """
        account = entry.get('account') if isinstance(entry.get('account'), dict) else {}
        account_key = str(entry.get('key') or '')
        title = str(entry.get('title') or '账户')

        user = account.get('user') if isinstance(account.get('user'), dict) else {}
        points = account.get('points') if isinstance(account.get('points'), dict) else {}
        storage = account.get('storage') if isinstance(account.get('storage'), dict) else {}
        details = account.get('details') if isinstance(account.get('details'), list) else []
        connected = bool(account.get('connected'))

        # ---- 标题行：账号名 + Badge + VIP chip + 刷新按钮 ----
        heading: List[Dict[str, Any]] = []

        name_text = str(user.get('name') or '').strip()
        if not connected:
            name_text = f'{title}：账号未连接'
        elif not name_text:
            name_text = '未知用户'
        heading.append({
            'component': 'div',
            'props': {'class': 'text-body-1 font-weight-medium'},
            'text': name_text
        })

        badge = str(user.get('badge') or '').strip()
        if connected and badge:
            heading.append({
                'component': 'VChip',
                'props': {'color': 'primary', 'size': 'x-small', 'variant': 'tonal', 'class': 'ml-2'},
                'text': badge
            })

        if connected and user.get('membership_supported') is not False:
            is_vip = bool(user.get('is_vip'))
            vip_text = str(user.get('vip_label') or '').strip()
            if not vip_text:
                vip_text = 'VIP' if is_vip else '非VIP'
            heading.append({
                'component': 'VChip',
                'props': {
                    'color': 'amber-darken-2' if is_vip else 'grey',
                    'size': 'x-small',
                    'variant': 'tonal',
                    'class': 'ml-1'
                },
                'text': vip_text
            })

        heading_row: Dict[str, Any] = {
            'component': 'VRow',
            'props': {'dense': True, 'align': 'center'},
            'content': [
                {
                    'component': 'VCol',
                    'props': {'cols': 12, 'md': 10},
                    'content': [{
                        'component': 'div',
                        'props': {'class': 'd-flex align-center flex-wrap ga-1'},
                        'content': heading
                    }]
                },
                {
                    'component': 'VCol',
                    'props': {'cols': 12, 'md': 2, 'class': 'text-right'},
                    'content': [{
                        'component': 'VBtn',
                        'props': {
                            'size': 'small',
                            'variant': 'text',
                            'color': 'primary',
                            'icon': 'mdi-refresh',
                            'title': '刷新账户信息'
                        },
                        # 官方约定（README FAQ 7）：api 为相对路径（不带斜杠、
                        # 不带 apikey，鉴权由前端统一附加），参数走 params。
                        'events': {
                            'click': {
                                'api': 'plugin/P115SubSearch/refresh_account',
                                'method': 'post',
                                'params': {'key': account_key}
                            }
                        }
                    }]
                }
            ]
        }

        body: List[Dict[str, Any]] = [heading_row]

        # ---- 积分行 ----
        available = points.get('available')
        if connected and available is not None:
            try:
                points_text = f"{int(available):,}"
            except (TypeError, ValueError):
                points_text = str(available)
            body.append({
                'component': 'div',
                'props': {'class': 'text-caption text-medium-emphasis mt-1'},
                'text': f"{str(points.get('label') or '可用积分')}：{points_text}"
            })

        # ---- 容量行（网盘）----
        used = str(storage.get('used') or '').strip()
        total = str(storage.get('total') or '').strip()
        if connected and (used or total):
            body.append({
                'component': 'div',
                'props': {'class': 'text-caption text-medium-emphasis mt-1'},
                'text': f"已用 {used or '未知'} / {total or '未知'}"
            })

        # ---- details 两列网格 ----
        if connected and details:
            cells: List[Dict[str, Any]] = []
            for item in details:
                if not isinstance(item, dict):
                    continue
                cells.append({
                    'component': 'VCol',
                    'props': {'cols': 12, 'md': 6},
                    'content': [{
                        'component': 'div',
                        'props': {'class': 'd-flex justify-space-between text-caption'},
                        'content': [
                            {
                                'component': 'span',
                                'props': {'class': 'text-medium-emphasis'},
                                'text': str(item.get('label') or '')
                            },
                            {
                                'component': 'span',
                                'props': {'class': 'font-weight-medium text-right'},
                                'text': str(item.get('value') or '—')
                            }
                        ]
                    }]
                })
            body.append({
                'component': 'VRow',
                'props': {'dense': True, 'class': 'mt-2 pt-2 account-details-grid'},
                'content': cells
            })

        # ---- 未连接提示 ----
        if not connected:
            body.append({
                'component': 'div',
                'props': {'class': 'text-caption text-warning mt-1'},
                'text': str(account.get('error') or '请填写登录凭证并保存配置')
            })

        # ---- 刷新说明 ----
        body.append({
            'component': 'div',
            'props': {'class': 'text-caption text-medium-emphasis mt-2'},
            'text': '点击右上角刷新立即拉取；插件也会定时自动刷新。'
                    '刷新完成后如未立即更新，请重新打开配置页。'
        })

        return {
            'component': 'VRow',
            'content': [{
                'component': 'VCol',
                'props': {'cols': 12},
                'content': [{
                    'component': 'VCard',
                    'props': {
                        'variant': 'tonal',
                        'color': 'success' if connected else 'warning',
                        'rounded': 'lg'
                    },
                    'content': [{
                        'component': 'VCardText',
                        'props': {'class': 'py-3'},
                        'content': body
                    }]
                }]
            }]
        }

    @staticmethod
    def _account_cards(account_status: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """把账户状态快照渲染成一组账户卡片（v1.8.3）。"""
        cards = []
        if account_status:
            for entry in list(account_status.get('cards') or []):
                if isinstance(entry, dict):
                    cards.append(UIConfig._account_card(entry))
        return {
            'component': 'VRow',
            'content': [{
                'component': 'VCol',
                'props': {'cols': 12},
                'content': cards
            }]
        }

    @staticmethod
    def _alert(text: str, atype: str = 'info') -> Dict[str, Any]:
        """生成一个整宽提示条。"""
        return {
            'component': 'VRow',
            'content': [{
                'component': 'VCol',
                'props': {'cols': 12},
                'content': [{
                    'component': 'VAlert',
                    'props': {'type': atype, 'variant': 'tonal', 'text': text}
                }]
            }]
        }

    @staticmethod
    def _tab_item(value: str, rows: List[Dict[str, Any]]) -> Dict[str, Any]:
        """把若干行包装成一个 Tab 页内容。"""
        return {
            'component': 'VWindowItem',
            'props': {'value': value},
            'content': list(rows)
        }

    @staticmethod
    def get_form(account_status: Optional[Dict[str, Any]] = None) -> Tuple[List[dict], Dict[str, Any]]:
        """
        获取插件配置表单

        v1.8.0 版式：
            主界面 = 插件基本功能配置 + 115网盘信息配置
            其下按「订阅 / 签到 / 盘搜 / 癫影 / 影巢 / 金山文档」分 Tab 管理

        v1.8.2 新增：
            account_status 传入后，在主界面顶部渲染账户状态卡片。

        v1.8.3 改版：
            账户卡片改为网盘搜索助手的 AccountInfo 契约
            （头像 + Badge + VIP chip + 积分 + 两列 details 网格 + 刷新按钮），
            115 网盘与癫影各一张，只读本地快照、零第三方请求。

        :param account_status: 由插件实例采集的账户状态快照，可为 None
        :return: (表单schema, 默认配置)
        """
        subscribe_options = UIConfig.get_subscribe_options()
        site_name_items = UIConfig.get_site_name_options()
        leaderboard_options = UIConfig.get_leaderboard_source_options()

        # ============ 主界面：插件基本功能配置 ============
        basic_rows: List[Dict[str, Any]] = []

        # 账户信息卡片（v1.8.3：115 网盘 + 癫影，对齐网盘搜索助手）
        if account_status:
            basic_rows.append(UIConfig._account_cards(account_status))

        basic_rows.extend([
            # 基本开关 + 执行周期
            {
                'component': 'VRow',
                'content': [
                    {'component': 'VCol', 'props': {'cols': 12, 'md': 2},
                     'content': [{'component': 'VSwitch', 'props': {'model': 'enabled', 'label': '启用插件'}}]},
                    {'component': 'VCol', 'props': {'cols': 12, 'md': 2},
                     'content': [{'component': 'VSwitch', 'props': {'model': 'notify', 'label': '发送通知'}}]},
                    {'component': 'VCol', 'props': {'cols': 12, 'md': 2},
                     'content': [{'component': 'VSwitch', 'props': {'model': 'block_system_subscribe', 'label': '屏蔽系统订阅'}}]},
                    {'component': 'VCol', 'props': {'cols': 12, 'md': 2},
                     'content': [{'component': 'VSwitch', 'props': {'model': 'onlyonce', 'label': '立即运行'}}]},
                    {'component': 'VCol', 'props': {'cols': 12, 'md': 4},
                     'content': [{
                         'component': 'VCronField',
                         'props': {
                             'model': 'cron',
                             'label': '执行周期（Cron）',
                             'placeholder': '30 2,10,18 * * *',
                             'hint': '5段 Cron：分 时 日 月 周；最小间隔 4 小时（低于 4 小时自动回退 30 */4 * * *）。例：30 2,10,18 * * * 表示2点、10点、18点的30分执行',
                             'persistent-hint': True,
                             'clearable': True
                         }
                     }]}
                ]
            },
            # 账户信息自动刷新（v1.8.4）
            {
                'component': 'VRow',
                'content': [
                    {'component': 'VCol', 'props': {'cols': 12, 'md': 4},
                     'content': [{
                         'component': 'VTextField',
                         'props': {
                             'model': 'account_refresh_minutes',
                             'label': '账户信息自动刷新间隔（分钟）',
                             'type': 'number',
                             'placeholder': '30',
                             'hint': '每 N 分钟后台自动刷新 115 与癫影账户卡片，癫影同时保活登录态；设为 0 关闭',
                             'persistent-hint': True
                         }
                     }]}
                ]
            },
            # 风控防护（批量转存与单次上限）
            {
                'component': 'VRow',
                'content': [
                    {'component': 'VCol', 'props': {'cols': 6, 'md': 3},
                     'content': [{'component': 'VTextField', 'props': {'model': 'max_transfer_per_sync', 'label': '单次同步上限', 'type': 'number', 'placeholder': '50', 'hint': '每次同步最多转存文件数', 'persistent-hint': True}}]},
                    {'component': 'VCol', 'props': {'cols': 6, 'md': 3},
                     'content': [{'component': 'VTextField', 'props': {'model': 'batch_size', 'label': '批量转存大小', 'type': 'number', 'placeholder': '20', 'hint': '每批转存文件数', 'persistent-hint': True}}]},
                    {'component': 'VCol', 'props': {'cols': 6, 'md': 3},
                     'content': [{'component': 'VTextField', 'props': {'model': 'max_transfer_links', 'label': '单订阅最大转存链接数', 'type': 'number', 'placeholder': '5', 'hint': '转存前先检测链接有效性，对有效链接最多转存这么多个', 'persistent-hint': True}}]},
                    {'component': 'VCol', 'props': {'cols': 6, 'md': 3},
                     'content': [{'component': 'VSwitch', 'props': {'model': 'skip_other_season_dirs', 'label': '多季剧集快速转存', 'hint': '跳过其他季目录以减少API调用，资源搜索不到时需要关闭', 'persistent-hint': True}}]}
                ]
            }
        ])

        # ============ 主界面：115网盘信息配置 ============
        p115_rows: List[Dict[str, Any]] = [
            UIConfig._alert('115网盘配置：请从浏览器获取Cookie（包含UID、CID、SEID、KID等字段）', 'warning'),
            {
                'component': 'VRow',
                'content': [
                    {'component': 'VCol', 'props': {'cols': 12, 'md': 4},
                     'content': [{'component': 'VTextField', 'props': {'model': 'save_path', 'label': '电视剧转存目录', 'placeholder': '/我的接收/MoviePilot/TV'}}]},
                    {'component': 'VCol', 'props': {'cols': 12, 'md': 4},
                     'content': [{'component': 'VTextField', 'props': {'model': 'movie_save_path', 'label': '电影转存目录', 'placeholder': '/我的接收/MoviePilot/Movie'}}]},
                    {'component': 'VCol', 'props': {'cols': 12, 'md': 4},
                     'content': [{'component': 'VTextField', 'props': {'model': 'cookies', 'label': '115 Cookie', 'type': 'password', 'placeholder': 'UID=xxx; CID=xxx; SEID=xxx'}}]}
                ]
            }
        ]

        # ============ Tab 1：订阅 ============
        subscribe_tab: List[Dict[str, Any]] = [
            UIConfig._alert('订阅：决定本插件处理哪些 MoviePilot 订阅，以及任务期间如何临时屏蔽系统订阅。'),
            # 订阅过滤模式
            {
                'component': 'VRow',
                'content': [{
                    'component': 'VCol',
                    'props': {'cols': 12, 'md': 4},
                    'content': [{
                        'component': 'VSelect',
                        'props': {
                            'model': 'subscribe_filter_mode',
                            'label': '订阅过滤模式',
                            'items': [
                                {'title': '排除模式（处理除勾选外的全部订阅）', 'value': 'exclude'},
                                {'title': '指定模式（仅处理勾选的订阅）', 'value': 'include'}
                            ],
                            'hint': '以PT订阅为主、网盘为辅时建议用指定模式，只勾选少数需要网盘补充的订阅',
                            'persistent-hint': True
                        }
                    }]
                }]
            },
            # 排除订阅
            {
                'component': 'VRow',
                'content': [{
                    'component': 'VCol',
                    'props': {'cols': 12},
                    'content': [{'component': 'VSelect', 'props': {'model': 'exclude_subscribes', 'label': '排除订阅（排除模式下生效：选择不需要本插件处理的订阅）',
                        'multiple': True, 'chips': True, 'clearable': True, 'closable-chips': True, 'items': subscribe_options}}]
                }]
            },
            # 指定订阅
            {
                'component': 'VRow',
                'content': [{
                    'component': 'VCol',
                    'props': {'cols': 12},
                    'content': [{'component': 'VSelect', 'props': {'model': 'include_subscribes', 'label': '指定订阅（指定模式下生效：仅勾选的订阅由本插件处理）',
                        'multiple': True, 'chips': True, 'clearable': True, 'closable-chips': True, 'items': subscribe_options}}]
                }]
            },
            # 取消屏蔽后的站点选择 / 窗口期 / 延迟分钟
            {
                'component': 'VRow',
                'content': [
                    {
                        'component': 'VCol',
                        'props': {'cols': 12, 'md': 6},
                        'content': [{
                            'component': 'VSelect',
                            'props': {
                                'model': 'unblock_site_names',
                                'label': '取消屏蔽后订阅站点选择（多选）',
                                'items': site_name_items,
                                'multiple': True,
                                'chips': True,
                                'clearable': True,
                                'closable-chips': True,
                                'hint': '为空表示禁用窗口：始终保持屏蔽（仅115网盘）',
                                'persistent-hint': True
                            }
                        }]
                    },
                    {
                        'component': 'VCol',
                        'props': {'cols': 12, 'md': 3},
                        'content': [{
                            'component': 'VTextField',
                            'props': {
                                'model': 'unblock_window_hours',
                                'label': '取消屏蔽窗口期（小时）',
                                'type': 'number',
                                'placeholder': '2',
                                'hint': '设为0表示禁用窗口：始终保持屏蔽（仅115网盘）',
                                'persistent-hint': True,
                                'clearable': True
                            }
                        }]
                    },
                    {
                        'component': 'VCol',
                        'props': {'cols': 12, 'md': 3},
                        'content': [{
                            'component': 'VTextField',
                            'props': {
                                'model': 'unblock_delay_minutes',
                                'label': '每天最后一次任务后延迟（分钟）',
                                'type': 'number',
                                'placeholder': '5',
                                'hint': '设为-1表示禁用窗口：始终保持屏蔽（仅115网盘）；否则23:00兜底恢复系统订阅',
                                'persistent-hint': True,
                                'clearable': True
                            }
                        }]
                    }
                ]
            },
            # Pinglian 盘链搜索
            {
                'component': 'VRow',
                'content': [
                    {'component': 'VCol', 'props': {'cols': 12, 'md': 3},
                     'content': [{'component': 'VSwitch', 'props': {'model': 'pinglian_enabled', 'label': '启用 Pinglian 盘链'}}]},
                    {'component': 'VCol', 'props': {'cols': 12, 'md': 9},
                     'content': [{'component': 'VTextField', 'props': {'model': 'pinglian_url', 'label': 'Pinglian 地址', 'placeholder': 'https://example.invalid'}}]}
                ]
            },
            # 搜索源优先级
            {
                'component': 'VRow',
                'content': [{
                    'component': 'VCol',
                    'props': {'cols': 12},
                    'content': [{
                        'component': 'VSelect',
                        'props': {
                            'model': 'search_source_order',
                            'label': '搜索源优先级（按选择顺序排序）',
                            'items': [
                                {'title': '癫影 (Dian115)', 'value': 'dian115'},
                                {'title': 'PanSou (盘搜)', 'value': 'pansou'},
                                {'title': 'KDocs (在线文档库)', 'value': 'kdocs'},
                                {'title': 'Pinglian (盘链)', 'value': 'pinglian'}
                            ],
                            'multiple': True,
                            'chips': True,
                            'clearable': True,
                            'closable-chips': True,
                            'hint': '按选择的先后顺序依次搜索，前面的源搜到结果就不再查询后面的；留空使用默认优先级 癫影 > 盘搜 > 金山文档；未选入的已启用源会自动排在末尾',
                            'persistent-hint': True
                        }
                    }]
                }]
            }
        ]

        # ============ Tab 2：签到 ============
        checkin_tab: List[Dict[str, Any]] = [
            UIConfig._alert('签到：独立于订阅搜索的每日签到任务，支持 115 网盘签到与癫影签到/转盘抽奖。'
                            '癫影签到需要在「癫影」页签配置账号，115 签到使用上方「115网盘信息配置」的 Cookie。'),
            {
                'component': 'VRow',
                'content': [
                    {'component': 'VCol', 'props': {'cols': 6, 'md': 2},
                     'content': [{'component': 'VSwitch', 'props': {'model': 'checkin_enabled', 'label': '启用签到'}}]},
                    {'component': 'VCol', 'props': {'cols': 6, 'md': 2},
                     'content': [{'component': 'VSwitch', 'props': {'model': 'checkin_notify', 'label': '签到通知'}}]},
                    {'component': 'VCol', 'props': {'cols': 6, 'md': 2},
                     'content': [{'component': 'VSwitch', 'props': {'model': 'checkin_onlyonce', 'label': '立即签到'}}]},
                    {'component': 'VCol', 'props': {'cols': 12, 'md': 6},
                     'content': [{
                         'component': 'VCronField',
                         'props': {
                             'model': 'checkin_cron',
                             'label': '签到周期（Cron）',
                             'placeholder': '30 8 * * *',
                             'hint': '留空则跟随插件主周期执行；建议每天固定时间执行一次即可',
                             'persistent-hint': True,
                             'clearable': True
                         }
                     }]}
                ]
            },
            UIConfig._alert('115 网盘签到：每日签到领取枫叶，需要已配置 115 Cookie。'),
            {
                'component': 'VRow',
                'content': [
                    {'component': 'VCol', 'props': {'cols': 12, 'md': 4},
                     'content': [{'component': 'VSwitch', 'props': {'model': 'p115_checkin_enabled', 'label': '启用 115 签到'}}]},
                    {'component': 'VCol', 'props': {'cols': 12, 'md': 8},
                     'content': [{'component': 'VAlert', 'props': {'type': 'info', 'variant': 'tonal',
                         'text': '115 签到依赖上方「115网盘信息配置」中的 Cookie，无需额外账号。'}}]}
                ]
            },
            UIConfig._alert('癫影签到与转盘：需要已在「癫影」页签填写账号密码；转盘会消耗积分，请按需设置次数。'),
            {
                'component': 'VRow',
                'content': [
                    {'component': 'VCol', 'props': {'cols': 12, 'md': 3},
                     'content': [{'component': 'VSwitch', 'props': {'model': 'dian115_checkin_enabled', 'label': '启用癫影签到'}}]},
                    {'component': 'VCol', 'props': {'cols': 12, 'md': 3},
                     'content': [{'component': 'VSelect', 'props': {
                         'model': 'dian115_checkin_mode',
                         'label': '签到模式',
                         'items': [
                             {'title': '普通签到', 'value': 'normal'},
                             {'title': '运气签到（随机倍数，积分波动大）', 'value': 'lucky'}
                         ],
                         'hint': '运气签到会按随机倍数结算积分，可能高于或低于普通签到',
                         'persistent-hint': True
                     }}]},
                    {'component': 'VCol', 'props': {'cols': 12, 'md': 3},
                     'content': [{'component': 'VSwitch', 'props': {'model': 'dian115_lottery_enabled', 'label': '启用转盘抽奖'}}]},
                    {'component': 'VCol', 'props': {'cols': 12, 'md': 3},
                     'content': [{'component': 'VTextField', 'props': {
                         'model': 'dian115_lottery_count',
                         'label': '转盘目标次数（当日）',
                         'type': 'number',
                         'placeholder': '0',
                         'hint': '当日累计目标：例如设为 5，当天已抽 3 次则本次再抽 2 次；上限 20 次',
                         'persistent-hint': True
                     }}]}
                ]
            }
        ]

        # ============ Tab 3：盘搜 ============
        pansou_tab: List[Dict[str, Any]] = [
            UIConfig._alert('PanSou搜索服务：网盘资源聚合搜索，用于搜索115网盘分享链接。'),
            {
                'component': 'VRow',
                'content': [
                    {'component': 'VCol', 'props': {'cols': 6, 'md': 3},
                     'content': [{'component': 'VSwitch', 'props': {'model': 'pansou_enabled', 'label': '启用 PanSou'}}]},
                    {'component': 'VCol', 'props': {'cols': 12, 'md': 3},
                     'content': [{'component': 'VTextField', 'props': {'model': 'pansou_url', 'label': 'PanSou API 地址', 'placeholder': 'https://your-pansou-api.com'}}]},
                    {'component': 'VCol', 'props': {'cols': 12, 'md': 6},
                     'content': [{'component': 'VTextField', 'props': {'model': 'pansou_channels', 'label': 'TG 搜索频道', 'placeholder': '频道,用逗号分隔'}}]}
                ]
            },
            {
                'component': 'VRow',
                'content': [
                    {'component': 'VCol', 'props': {'cols': 6, 'md': 3},
                     'content': [{'component': 'VSwitch', 'props': {'model': 'pansou_auth_enabled', 'label': '启用认证'}}]},
                    {'component': 'VCol', 'props': {'cols': 12, 'md': 3},
                     'content': [{'component': 'VTextField', 'props': {'model': 'pansou_username', 'label': 'PanSou 用户名', 'placeholder': '启用认证时填写'}}]},
                    {'component': 'VCol', 'props': {'cols': 12, 'md': 6},
                     'content': [{'component': 'VTextField', 'props': {'model': 'pansou_password', 'label': 'PanSou 密码', 'type': 'password', 'placeholder': '启用认证时填写', 'clearable': True}}]}
                ]
            },
            {
                'component': 'VRow',
                'content': [
                    {'component': 'VCol', 'props': {'cols': 12, 'md': 6},
                     'content': [{'component': 'VSwitch', 'props': {'model': 'pansou_check_enabled', 'label': '启用 PanSou 链接有效性检测(全渠道)',
                         'hint': '转存前先用 PanSou 校验链接是否有效，可减少无效转存', 'persistent-hint': True}}]}
                ]
            }
        ]

        # ============ Tab 4：癫影 ============
        dian115_tab: List[Dict[str, Any]] = [
            UIConfig._alert('癫影（Dian115）：按 TMDB ID 精准查询 115 网盘分享链接，准确度高于关键词搜索。'
                            '实测站点资源绝大多数需要积分才能拿到链接，下方可开启自动解锁并设置预算。'),
            {
                'component': 'VRow',
                'content': [
                    {'component': 'VCol', 'props': {'cols': 12, 'md': 4},
                     'content': [{'component': 'VSwitch', 'props': {'model': 'dian115_enabled', 'label': '启用癫影搜索'}}]},
                    {'component': 'VCol', 'props': {'cols': 12, 'md': 4},
                     'content': [{'component': 'VTextField', 'props': {'model': 'dian115_email', 'label': '癫影账号（邮箱）', 'placeholder': '注册邮箱'}}]},
                    {'component': 'VCol', 'props': {'cols': 12, 'md': 4},
                     'content': [{'component': 'VTextField', 'props': {'model': 'dian115_password', 'label': '癫影密码', 'type': 'password', 'placeholder': '登录密码', 'clearable': True}}]}
                ]
            },
            UIConfig._alert('自动登录（推荐）：填写账号密码后保持下方「自动登录」开启即可，插件会调用内置 '
                            'cloakbrowser 反检测浏览器在本地完成 Cloudflare 人机验证，全程无需手工操作，'
                            '登录态自动持久化并续期。首次登录约需 10 秒（需启动浏览器内核），之后复用会话仅 1~2 秒。',
                            'success'),
            {
                'component': 'VRow',
                'content': [{
                    'component': 'VCol',
                    'props': {'cols': 12},
                    'content': [{'component': 'VSwitch', 'props': {
                        'model': 'dian115_auto_login',
                        'label': '自动登录（Cloudflare 验证）',
                        'hint': '默认开启。账号密码 + 本地浏览器自动过 Cloudflare 人机验证，无需再手工维护 Token。'
                                '关闭后只能依赖下方手工 Token（约 24 小时过期）'
                    }}]
                }]
            },
            {
                'component': 'VRow',
                'content': [{
                    'component': 'VCol',
                    'props': {'cols': 12},
                    'content': [{'component': 'VTextField', 'props': {
                        'model': 'dian115_browser_proxy',
                        'label': '浏览器专用代理（可选）',
                        'placeholder': 'http://127.0.0.1:7890',
                        'clearable': True,
                        'hint': '留空则复用全局代理。Cloudflare 验证需要海外出口、而门户请求想走直连时可单独配置',
                        'persistent-hint': True
                    }}]
                }]
            },
            {
                'component': 'VRow',
                'content': [{
                    'component': 'VCol',
                    'props': {'cols': 12},
                    'content': [{'component': 'VTextarea', 'props': {
                        'model': 'dian115_token',
                        'label': '手工 Token（可选兜底，__Host-portal_token）',
                        'rows': 2,
                        'placeholder': '浏览器登录 m.dian115.com 后，F12 → 应用 → Cookie → 复制 __Host-portal_token 的值',
                        'clearable': True,
                        'hint': '仅当自动登录不可用（非 MoviePilot 环境 / 缺少浏览器内核）时才需要。'
                                '优先级高于账号密码；有效期约 24 小时',
                        'persistent-hint': True
                    }}]
                }]
            },
            {
                'component': 'VRow',
                'content': [{
                    'component': 'VCol',
                    'props': {'cols': 12},
                    'content': [{'component': 'VCronField', 'props': {
                        'model': 'dian115_login_cron',
                        'label': '癫影会话保活周期（Cron）',
                        'placeholder': '0 */12 * * *',
                        'hint': '可选。留空则仅在搜索/签到任务时按需登录。填报后插件会在该周期内主动续期登录态，'
                                '好处是任务执行时无需等待浏览器冷启动（首次约 10 秒）',
                        'persistent-hint': True,
                        'clearable': True
                    }}]
                }]
            },
            {
                'component': 'VRow',
                'content': [{
                    'component': 'VCol',
                    'props': {'cols': 12},
                    'content': [{'component': 'VSwitch', 'props': {
                        'model': 'dian115_auto_unlock',
                        'label': '自动消耗积分解锁资源',
                        'hint': '默认关闭。开启后遇到收费条目会自动调用解锁接口（同样由自动登录解 Cloudflare 验证）；'
                                '预算不足或验证失败时自动跳过并记日志，不会误扣积分、不会中断任务。'
                    }}]
                }]
            },
            {
                'component': 'VRow',
                'content': [
                    {'component': 'VCol', 'props': {'cols': 12, 'md': 6},
                     'content': [{'component': 'VTextField', 'props': {'model': 'dian115_max_unlock_points', 'label': '单次任务解锁总预算', 'type': 'number', 'placeholder': '50', 'hint': '一轮同步任务最多消耗的积分总和'}}]},
                    {'component': 'VCol', 'props': {'cols': 12, 'md': 6},
                     'content': [{'component': 'VTextField', 'props': {'model': 'dian115_max_points_per_sub', 'label': '单个订阅解锁预算', 'type': 'number', 'placeholder': '20', 'hint': '处理单个订阅时允许消耗的最大积分'}}]}
                ]
            }
        ]

        # ============ Tab 5：金山文档 ============
        kdocs_tab: List[Dict[str, Any]] = [
            UIConfig._alert('KDocs在线文档库：读取金山文档在线表格中的资源分享信息并匹配网盘链接。'
                            '需配置 Skill Token；留空文档链接时使用默认文档库。'),
            {
                'component': 'VRow',
                'content': [
                    {'component': 'VCol', 'props': {'cols': 6, 'md': 3},
                     'content': [{'component': 'VSwitch', 'props': {'model': 'kdocs_enabled', 'label': '启用在线文档库'}}]},
                    {'component': 'VCol', 'props': {'cols': 12, 'md': 4},
                     'content': [{'component': 'VTextField', 'props': {'model': 'kdocs_token', 'label': 'Skill Token', 'type': 'password', 'placeholder': 'KDocs Skill Token', 'clearable': True,
                         'hint': '获取：kdocs.cn 登录后右上角头像菜单「金山文档Skill」复制Token', 'persistent-hint': True}}]},
                    {'component': 'VCol', 'props': {'cols': 6, 'md': 2},
                     'content': [{'component': 'VTextField', 'props': {'model': 'kdocs_cache_ttl_hours', 'label': '缓存(小时)', 'type': 'number', 'placeholder': '6'}}]},
                    {'component': 'VCol', 'props': {'cols': 6, 'md': 3},
                     'content': [{'component': 'VTextField', 'props': {'model': 'kdocs_batch_rows', 'label': '分批行数', 'type': 'number', 'placeholder': '1000', 'hint': '单批拉取行数上限1000'}}]}
                ]
            },
            {
                'component': 'VRow',
                'content': [{
                    'component': 'VCol',
                    'props': {'cols': 12},
                    'content': [{'component': 'VTextarea', 'props': {'model': 'kdocs_doc_urls', 'label': '文档库分享链接（每行一个）', 'rows': 3, 'placeholder': 'https://www.kdocs.cn/l/xxxx', 'clearable': True,
                        'hint': '留空使用默认文档库；可维护多个文档库同时匹配', 'persistent-hint': True}}]
                }]
            },
            {
                'component': 'VRow',
                'content': [
                    {'component': 'VCol', 'props': {'cols': 12, 'md': 6},
                     'content': [{'component': 'VTextField', 'props': {'model': 'kdocs_cookie', 'label': '金山文档 Cookie', 'type': 'password', 'placeholder': '浏览器登录 kdocs.cn 后 F12 复制 Cookie', 'clearable': True}}]},
                    {'component': 'VCol', 'props': {'cols': 12, 'md': 6},
                     'content': [{'component': 'VAlert', 'props': {'type': 'info', 'variant': 'tonal', 'text': 'Cookie获取教程：浏览器登录 kdocs.cn → F12 打开开发者工具 → 网络/应用面板复制 Cookie 请求头'}}]}
                ]
            }
        ]

        # ============ Tab 6：榜单（v1.9.0） ============
        leaderboard_tab: List[Dict[str, Any]] = [
            UIConfig._alert('榜单订阅：直接把 MoviePilot 自带榜单（TMDB / 豆瓣）作为订阅入口，'
                            '默认已启用并内置来源（TMDB 流行趋势），可直接在插件数据页浏览并一键订阅，'
                            '也可按需增减来源，无需第三方服务。'),
            {
                'component': 'VRow',
                'content': [
                    {'component': 'VCol', 'props': {'cols': 12, 'md': 3},
                     'content': [{'component': 'VSwitch', 'props': {'model': 'leaderboard_enabled', 'label': '启用榜单订阅'}}]},
                    {'component': 'VCol', 'props': {'cols': 12, 'md': 3},
                     'content': [{'component': 'VTextField', 'props': {'model': 'leaderboard_page_size', 'label': '单页条数', 'type': 'number', 'placeholder': '20',
                         'hint': '单次抓取最多展示的条目数（1-100）', 'persistent-hint': True, 'clearable': True}}]},
                    {'component': 'VCol', 'props': {'cols': 12, 'md': 3},
                     'content': [{'component': 'VTextField', 'props': {'model': 'leaderboard_cache_minutes', 'label': '缓存（分钟）', 'type': 'number', 'placeholder': '30',
                         'hint': '榜单为慢源，页内读取本地缓存，过期才回源（1-1440）', 'persistent-hint': True, 'clearable': True}}]},
                    {'component': 'VCol', 'props': {'cols': 12, 'md': 3},
                     'content': [{'component': 'VTextField', 'props': {'model': 'leaderboard_refresh_minutes', 'label': '后台刷新间隔（分钟）', 'type': 'number', 'placeholder': '0',
                         'hint': '0 表示不后台刷新；大于 0 时定时预热榜单缓存（最多每天一次）', 'persistent-hint': True, 'clearable': True}}]}
                ]
            },
            {
                'component': 'VRow',
                'content': [{
                    'component': 'VCol',
                    'props': {'cols': 12},
                    'content': [{
                        'component': 'VSelect',
                        'props': {
                            'model': 'leaderboard_sources',
                            'label': '榜单来源（多选，按需勾选）',
                            'items': leaderboard_options,
                            'multiple': True,
                            'chips': True,
                            'clearable': True,
                            'closable-chips': True,
                            'hint': '留空时自动使用默认来源（TMDB 流行趋势），保证启用后即有可用榜单；'
                                    '来源不可用时插件会优雅降级并给出中文提示，不影响订阅搜索主流程',
                            'persistent-hint': True
                        }
                    }]
                }]
            },
            {
                'component': 'VRow',
                'content': [{
                    'component': 'VCol',
                    'props': {'cols': 12},
                    'content': [{'component': 'VAlert', 'props': {'type': 'info', 'variant': 'tonal',
                        'text': '榜单页面只读本地快照，零网络开销；抓取与订阅均复用 MoviePilot 原生推荐链与订阅链。'}}]
                }]
            }
        ]

        # ============ 组装：主界面 + Tab 区 ============
        form_schema = [
            {
                'component': 'VForm',
                'content': [
                    # 插件总说明
                    UIConfig._alert('自动搜索115网盘资源并转存缺失的电影和剧集，需配置115 Cookie和搜索服务。'
                                    '执行周期支持任意 Cron 表达式，最小间隔限制 4 小时（更频繁的配置将回退为每 4 小时执行）。'),

                    # 一、插件基本功能配置
                    UIConfig._section_title('插件基本功能配置'),
                    *basic_rows,

                    # 二、115网盘信息配置
                    UIConfig._section_title('115网盘信息配置'),
                    *p115_rows,

                    # 三、功能分页
                    {
                        'component': 'VTabs',
                        'props': {
                            'model': '_tabs',
                            'style': {'margin-top': '8px', 'margin-bottom': '16px'},
                            'stacked': True,
                            'fixed-tabs': True
                        },
                        'content': [
                            {'component': 'VTab', 'props': {'value': 'subscribe_tab'}, 'text': '订阅'},
                            {'component': 'VTab', 'props': {'value': 'checkin_tab'}, 'text': '签到'},
                            {'component': 'VTab', 'props': {'value': 'pansou_tab'}, 'text': '盘搜'},
                            {'component': 'VTab', 'props': {'value': 'dian115_tab'}, 'text': '癫影'},
                            {'component': 'VTab', 'props': {'value': 'kdocs_tab'}, 'text': '金山文档'},
                            {'component': 'VTab', 'props': {'value': 'leaderboard_tab'}, 'text': '榜单'}
                        ]
                    },
                    {
                        'component': 'VWindow',
                        'props': {'model': '_tabs'},
                        'content': [
                            UIConfig._tab_item('subscribe_tab', subscribe_tab),
                            UIConfig._tab_item('checkin_tab', checkin_tab),
                            UIConfig._tab_item('pansou_tab', pansou_tab),
                            UIConfig._tab_item('dian115_tab', dian115_tab),
                            UIConfig._tab_item('kdocs_tab', kdocs_tab),
                            UIConfig._tab_item('leaderboard_tab', leaderboard_tab)
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
            "cron": "30 2,10,18 * * *",
            "account_refresh_minutes": 30,

            "unblock_site_ids": [],
            "unblock_site_names": [],
            "unblock_window_hours": 1,
            "system_subscribe_window_hours": 1,
            "unblock_delay_minutes": 5,

            "save_path": "/我的接收/MoviePilot/TV",
            "movie_save_path": "/我的接收/MoviePilot/Movie",
            "cookies": "",

            # 订阅
            "search_source_order": [],
            "subscribe_filter_mode": "exclude",
            "exclude_subscribes": [],
            "include_subscribes": [],
            "block_system_subscribe": False,

            # 转存风控
            "max_transfer_per_sync": 50,
            "batch_size": 20,
            "max_transfer_links": 5,
            "skip_other_season_dirs": True,

            # 签到（v1.8.0）
            "checkin_enabled": False,
            "checkin_notify": True,
            "checkin_onlyonce": False,
            "checkin_cron": "",
            "p115_checkin_enabled": True,
            "dian115_checkin_enabled": False,
            "dian115_checkin_mode": "normal",
            "dian115_lottery_enabled": False,
            "dian115_lottery_count": 0,

            # 盘搜 PanSou
            "pansou_enabled": True,
            "pansou_url": "https://so.252035.xyz/",
            "pansou_username": "",
            "pansou_password": "",
            "pansou_auth_enabled": False,
            "pansou_channels": "QukanMovie",
            "pansou_check_enabled": True,
            "pinglian_enabled": False,
            "pinglian_url": "",

            # 癫影 Dian115（v1.8.0 / v1.8.1 增自动登录）
            "dian115_enabled": False,
            "dian115_email": "",
            "dian115_password": "",
            "dian115_auto_login": True,
            "dian115_browser_proxy": "",
            "dian115_login_cron": "",
            "dian115_token": "",
            "dian115_auto_unlock": False,
            "dian115_max_unlock_points": 50,
            "dian115_max_points_per_sub": 20,

            "nullbr_enabled": False,
            "nullbr_appid": "",
            "nullbr_api_key": "",

            # 金山文档 KDocs
            "kdocs_enabled": False,
            "kdocs_token": "",
            "kdocs_doc_urls": "",
            "kdocs_cache_ttl_hours": 6,
            "kdocs_batch_rows": 1000,
            "kdocs_cookie": "",

            # 榜单订阅（v1.9.0；v1.9.1 改为「首次使用即可用」：默认启用 + 安全默认来源）
            "leaderboard_enabled": True,
            "leaderboard_sources": list(DEFAULT_LEADERBOARD_SOURCES),
            "leaderboard_page_size": 20,
            "leaderboard_cache_minutes": 30,
            "leaderboard_refresh_minutes": 0
        }

        return form_schema, default_config

    @staticmethod
    def _leaderboard_source_label(source_id: Any) -> str:
        """来源 id -> 中文名（未知 id 原样返回），供数据页展示已配置来源。"""
        sid = str(source_id or '').strip()
        if not sid:
            return ''
        for item in LEADERBOARD_SOURCES or []:
            if isinstance(item, dict) and str(item.get('id') or '').strip() == sid:
                return str(item.get('name') or sid)
        return sid

    @staticmethod
    def _leaderboard_state_summary(state: Optional[dict] = None) -> Dict[str, Any]:
        """把插件传入的榜单状态归一化为展示用结构（v1.9.1）。

        只读取 ``enabled`` / ``source_names`` / ``sources`` 三个非敏感字段：
        即使调用方误传了 Cookie、Token、访问码等字段，也不会进入渲染结果。
        """
        state = state if isinstance(state, dict) else {}
        enabled = bool(state.get('enabled'))

        raw_names = state.get('source_names')
        names = [str(name).strip() for name in raw_names
                 if str(name).strip()] if isinstance(raw_names, (list, tuple)) else []
        if not names:
            raw_ids = state.get('sources')
            if isinstance(raw_ids, (list, tuple)):
                names = [UIConfig._leaderboard_source_label(sid) for sid in raw_ids]
                names = [name for name in names if name]
        return {'enabled': enabled, 'source_names': names}

    @staticmethod
    def _leaderboard_card(items: Optional[List[dict]] = None,
                          state: Optional[dict] = None) -> Dict[str, Any]:
        """数据页「榜单订阅」卡片（v1.9.0，v1.9.1 增加状态与初始化入口）。

        只渲染插件本地快照与**非敏感**配置状态，**零网络请求**：
            * 顶部展示当前启用状态（已启用 / 未启用）；
            * 展示已配置来源的中文名，未配置时给出可操作提示；
            * 空快照时按钮变为「初始化榜单」，非空时为「刷新榜单」，
              两者都指向后台刷新路由（请求线程不同步抓取）。
        """
        summary = UIConfig._leaderboard_state_summary(state)
        enabled = summary['enabled']
        source_names = summary['source_names']
        has_items = bool(items)

        source_text = '、'.join(source_names) if source_names else '未配置来源'
        rows: List[Dict[str, Any]] = []
        rendered_count = 0

        if not has_items:
            if enabled:
                empty_text = (f'暂无榜单数据（已启用榜单订阅，来源：{source_text}）：'
                              '点击下方「初始化榜单」由后台抓取，稍后重新打开本页查看；'
                              '开启后台刷新后本卡片会自动更新。')
            else:
                empty_text = ('暂无榜单数据：当前未启用榜单订阅，请在插件设置页「榜单」页签'
                              '打开「启用榜单订阅」并保存，再点击下方按钮初始化。')
            rows.append({
                'component': 'div',
                'props': {'class': 'text-caption text-medium-emphasis'},
                'text': empty_text
            })
        else:
            for item in items[:20]:
                if not isinstance(item, dict):
                    continue
                title = str(item.get('title') or '').strip()
                if not title:
                    continue
                rendered_count += 1
                year = str(item.get('year') or '').strip()
                type_text = '电影' if str(item.get('media_type') or '') == 'movie' else '剧集'
                source_name = str(item.get('source_name') or item.get('source') or '榜单').strip()
                try:
                    score = float(item.get('vote_average') or 0)
                except (TypeError, ValueError):
                    score = 0.0

                meta = [type_text]
                if year:
                    meta.append(year)
                if score > 0:
                    meta.append(f"{score:.1f} 分")

                rows.append({
                    'component': 'div',
                    'props': {'class': 'd-flex justify-space-between align-center py-1'},
                    'content': [
                        {
                            'component': 'div',
                            'props': {'class': 'd-flex align-center flex-wrap ga-2'},
                            'content': [
                                {'component': 'VIcon', 'props': {'size': 'small', 'class': 'text-medium-emphasis'},
                                 'text': 'mdi-movie-open' if type_text == '电影' else 'mdi-television-classic'},
                                {'component': 'span', 'props': {'class': 'font-weight-medium'}, 'text': title},
                                {'component': 'span', 'props': {'class': 'text-caption text-medium-emphasis'},
                                 'text': ' · '.join(meta)}
                            ]
                        },
                        {'component': 'span', 'props': {'class': 'text-caption text-medium-emphasis'},
                         'text': source_name}
                    ]
                })

        # 启用状态 + 已配置来源（v1.9.1）：让用户在数据页一眼看清当前配置
        status_row = {
            'component': 'div',
            'props': {'class': 'd-flex flex-wrap align-center ga-2 mb-2'},
            'content': [
                {'component': 'VChip',
                 'props': {'size': 'x-small', 'variant': 'flat',
                           'color': 'success' if enabled else 'warning'},
                 'text': '已启用' if enabled else '未启用'},
                {'component': 'span',
                 'props': {'class': 'text-caption text-medium-emphasis'},
                 'text': f'来源：{source_text}'}
            ]
        }

        body: List[Dict[str, Any]] = [
            {
                'component': 'div',
                'props': {'class': 'd-flex justify-space-between align-center mb-2'},
                'content': [
                    {
                        'component': 'div',
                        'props': {'class': 'text-subtitle-2 font-weight-bold'},
                        'text': f'榜单订阅（{rendered_count} 条）'
                    },
                    {
                        'component': 'VBtn',
                        'props': {
                            'size': 'small',
                            'variant': 'text',
                            'color': 'primary',
                            'prepend-icon': 'mdi-refresh',
                            'title': '后台初始化并抓取榜单快照' if not has_items else '后台刷新榜单快照'
                        },
                        'text': '刷新榜单' if has_items else '初始化榜单',
                        # 官方约定：api 为相对路径（不带斜杠、不带 apikey），
                        # 榜单抓取在后台调度器执行，请求线程不阻塞。
                        'events': {
                            'click': {
                                'api': 'plugin/P115SubSearch/leaderboard/refresh',
                                'method': 'post'
                            }
                        }
                    }
                ]
            },
            status_row,
            *rows,
            {
                'component': 'div',
                'props': {'class': 'text-caption text-medium-emphasis mt-2'},
                'text': '榜单数据来自 MoviePilot 原生推荐链（TMDB / 豆瓣），'
                        '本页只读本地快照，不会触发任何网络请求。'
            }
        ]

        return {
            'component': 'VCard',
            'props': {'class': 'mt-4', 'variant': 'tonal', 'color': 'primary', 'rounded': 'lg'},
            'content': [{
                'component': 'VCardText',
                'props': {'class': 'py-3'},
                'content': body
            }]
        }

    @staticmethod
    def _share_link_card() -> Dict[str, Any]:
        """数据页 115 盘链解析入口：只提交用户输入，不回显访问码。"""
        return {
            'component': 'VCard',
            'props': {'class': 'mt-4', 'variant': 'tonal', 'color': 'secondary', 'rounded': 'lg'},
            'content': [{
                'component': 'VCardText',
                'props': {'class': 'py-3'},
                'content': [
                    {'component': 'div', 'props': {'class': 'text-subtitle-2 font-weight-bold mb-2'},
                     'text': '115盘链解析'},
                    {'component': 'VTextarea', 'props': {
                        'model': 'share_link_text', 'label': '115 分享链接或文本', 'rows': 3,
                        'clearable': True, 'placeholder': '粘贴 115 分享链接或包含链接的文本',
                        'hint': '如链接需要访问码，请一并粘贴；访问码只用于本次解析，不会在结果中显示。',
                        'persistent-hint': True}},
                    {'component': 'VBtn', 'props': {
                        'class': 'mt-3', 'size': 'small', 'color': 'primary',
                        'prepend-icon': 'mdi-link-variant', 'title': '解析 115 分享链接'},
                     'text': '解析链接',
                     'events': {'click': {'api': 'plugin/P115SubSearch/resolve_share_link',
                                          'method': 'post', 'params': {'text': 'share_link_text'}}}},
                    {'component': 'div', 'props': {'class': 'text-caption text-medium-emphasis mt-2'},
                     'text': '解析结果会显示链接状态、文件数量和是否需要补充访问码；不会回显访问码。'}
                ]
            }]
        }

    @staticmethod
    def get_page(history: List[dict],
                 leaderboard_snapshot: Optional[List[dict]] = None,
                 leaderboard_state: Optional[dict] = None) -> List[dict]:
        """
        详情页内容与 1.2.4 无强耦合，保持原样即可

        v1.9.0：新增 ``leaderboard_snapshot`` 入参，渲染只读榜单卡片
        （默认 None 时展示空状态，兼容旧调用方）。
        v1.9.1：新增可选 ``leaderboard_state``（启用状态 / 已配置来源），
        缺省时卡片退化为「未启用」提示，旧的两参调用方式保持兼容。
        """
        # 你原有的 get_page 很长，这里不做任何改动，继续沿用你现有版本即可。
        # 如果你希望我也按 1.2.4 统一“文案/按钮标题”，你告诉我我再一起改。
        from datetime import datetime

        history = history or []
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
                    # 第一行：统计卡片（总转存数、今日转存、成功数、失败数）
                    {
                        'component': 'VRow',
                        'content': [
                            # 总转存数
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
                    # 第二行：媒体类型统计（电影数、剧集数）和最近同步时间
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
                            # 剧集数
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
                            # 最近同步时间
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
                    # 操作按钮：立即搜索 + 清空历史记录
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
                                            'api': 'plugin/P115SubSearch/sync_subscribes',
                                            'method': 'get'
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
                                            'api': 'plugin/P115SubSearch/clear_history',
                                            'method': 'post'
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
            # v1.9.0：无转存记录时榜单卡片照常展示（与有记录路径返回结构一致）
            # v1.9.1：同步传入启用状态 / 已配置来源
            return [stats_header, empty_state,
                    UIConfig._share_link_card(),
                    UIConfig._leaderboard_card(leaderboard_snapshot, leaderboard_state)]

        movie_history = [h for h in sorted_history if h.get("type") == "电影"][:50]
        tv_history = [h for h in sorted_history if h.get("type") != "电影"][:50]

        def build_history_item(h: dict) -> dict:
            status = h.get("status", "")
            media_type = h.get("type", "")
            status_color = "success" if status == "成功" else "error" if status == "失败" else "warning"
            status_icon = "mdi-check-circle" if status == "成功" else "mdi-close-circle" if status == "失败" else "mdi-help-circle"
            type_icon = "mdi-movie" if media_type == "电影" else "mdi-television-classic"
            type_color = "amber" if media_type == "电影" else "purple"
            file_name = h.get("file_name", "")

            if media_type == "电影":
                title_text = f'{h.get("title", "")} ({h.get("year", "")})'
            else:
                season = h.get("season", 0) or 0
                episode = h.get("episode", 0) or 0
                title_text = f'{h.get("title", "")} S{season:02d}E{episode:02d}'

            content_items = [
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
                {
                    'component': 'div',
                    'props': {'class': 'd-flex align-center mt-1'},
                    'content': [
                        {'component': 'VIcon', 'props': {'size': 'x-small', 'color': 'grey', 'class': 'mr-1'}, 'text': 'mdi-clock-outline'},
                        {'component': 'span', 'props': {'class': 'text-caption text-grey'}, 'text': h.get("time", "")}
                    ]
                }
            ]

            if file_name:
                content_items.append({
                    'component': 'div',
                    'props': {'class': 'd-flex align-center mt-1'},
                    'content': [
                        {'component': 'VIcon', 'props': {'size': 'x-small', 'color': 'grey', 'class': 'mr-1'}, 'text': 'mdi-file-video'},
                        {'component': 'span', 'props': {'class': 'text-caption text-grey text-truncate'}, 'text': file_name}
                    ]
                })

            border_style = f'border-left: 3px solid var(--v-theme-{status_color}) !important;'
            return {
                'component': 'VCard',
                'props': {'class': 'mb-2', 'variant': 'outlined', 'style': border_style},
                'content': [{'component': 'VCardText', 'props': {'class': 'py-2 px-3'}, 'content': content_items}]
            }

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

        expansion_panels = {
            'component': 'VExpansionPanels',
            'props': {'variant': 'accordion', 'class': 'mt-4'},
            'content': [
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

        # v1.9.0：数据页追加只读榜单卡片（零网络，仅渲染本地快照）
        # v1.9.1：同时展示启用状态 / 已配置来源 / 初始化入口
        return [stats_header, expansion_panels,
                UIConfig._share_link_card(),
                UIConfig._leaderboard_card(leaderboard_snapshot, leaderboard_state)]
