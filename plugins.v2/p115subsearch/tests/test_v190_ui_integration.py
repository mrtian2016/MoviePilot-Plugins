# -*- coding: utf-8 -*-
"""
P115SubSearch v1.9.0 测试（四）：声明式 UI 集成（真实执行，非源码字符串断言）

对齐任务书「配置读写对称 / 声明式界面」要求，本文件在桩掉 app.* 依赖后
**真实 import 并执行** ``ui/config.py`` 与 ``clients/leaderboard.py``：

    1. get_form 渲染成功，榜单 5 个配置键全部进入 default_config；
    2. 榜单来源选项与后端 LEADERBOARD_SOURCES 同源（单一事实来源，不漂移）；
    3. 「榜单」页签同时存在于 VTabs 与 VWindow，且顺序一致；
    4. get_page 在「有转存记录」与「无转存记录」两条路径上都渲染榜单卡片
      （v1.9.0 修复：空历史早退分支此前会吞掉榜单卡片）；
    5. 榜单卡片对缺标题条目容错、空快照给出中文空状态；
    6. 数据页「刷新榜单」按钮走相对路径且指向已注册的后台刷新路由。

运行：python tests/test_v190_ui_integration.py
"""
import importlib.util
import sys
import types
from pathlib import Path

PLUGIN = Path(__file__).resolve().parent.parent

_passed = 0
_failed = []


def check(name, condition, detail=""):
    global _passed
    if condition:
        _passed += 1
        print(f"  [PASS] {name}")
    else:
        _failed.append(f"{name} :: {detail}")
        print(f"  [FAIL] {name} :: {detail}")


# ---------------------------------------------------------------- app.* 桩


def _mk(name):
    module = types.ModuleType(name)
    sys.modules[name] = module
    return module


class _Logger:
    def __getattr__(self, _name):
        return lambda *a, **k: None


class _FakeSession:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, *a, **k):
        return types.SimpleNamespace(fetchall=lambda: [])


def _install_stubs():
    _mk('app')
    _mk('app.log')
    sys.modules['app.log'].logger = _Logger()

    _mk('app.db')
    sys.modules['app.db'].SessionFactory = lambda: _FakeSession()
    _mk('app.db.subscribe_oper')
    sys.modules['app.db.subscribe_oper'].SubscribeOper = lambda *a, **k: types.SimpleNamespace(
        list=lambda *a, **k: []
    )

    _mk('app.schemas')
    _mk('app.schemas.types')
    sys.modules['app.schemas.types'].MediaType = type(
        'MediaType', (), {'TV': 'TV', 'MOVIE': 'MOVIE'})

    _mk('sqlalchemy')
    sys.modules['sqlalchemy'].text = lambda s: s

    # 只放行 p115subsearch 包本身，子模块走真实文件加载
    sys.modules['p115subsearch'] = types.ModuleType('p115subsearch')
    sys.modules['p115subsearch'].__path__ = [str(PLUGIN)]


def _load(mod_name, rel_path):
    spec = importlib.util.spec_from_file_location(mod_name, PLUGIN / rel_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


_install_stubs()
_leaderboard_client = _load('p115subsearch.clients.leaderboard', 'clients/leaderboard.py')
_ui = _load('p115subsearch.ui.config', 'ui/config.py')
UIConfig = _ui.UIConfig

FORM, DEFAULTS = UIConfig.get_form()


# ===========================================================================
# 1. 配置表单：默认值 + 来源选项
# ===========================================================================

def test_form_defaults():
    # v1.9.1：首次使用默认为「已启用 + 安全默认来源」，不再是不可用的关闭/空来源状态
    for key, expected in (
        ("leaderboard_enabled", True),
        ("leaderboard_sources", ["tmdb_trending"]),
        ("leaderboard_page_size", 20),
        ("leaderboard_cache_minutes", 30),
        ("leaderboard_refresh_minutes", 0),
    ):
        check(f"1-1 default_config.{key} = {expected!r}",
              DEFAULTS.get(key) == expected, repr(DEFAULTS.get(key)))
    backend_ids = {item["id"] for item in _leaderboard_client.LEADERBOARD_SOURCES}
    check("1-2 默认来源均在后端来源目录内",
          set(DEFAULTS.get("leaderboard_sources") or []).issubset(backend_ids),
          str(DEFAULTS.get("leaderboard_sources")))
    check("1-3 default_config 原有键未被覆盖", DEFAULTS.get("max_transfer_links") == 5,
          repr(DEFAULTS.get("max_transfer_links")))


def test_source_options_single_source_of_truth():
    options = UIConfig.get_leaderboard_source_options()
    check("2-1 来源选项非空", len(options) > 0, str(len(options)))
    ids = [o["value"] for o in options]
    backend_ids = [item["id"] for item in _leaderboard_client.LEADERBOARD_SOURCES]
    check("2-2 来源选项与后端目录完全同源", ids == backend_ids,
          f"{ids} != {backend_ids}")
    check("2-3 每个来源都有中文名与 id",
          all(o.get("title") and o.get("value") for o in options),
          str(options[:2]))


def test_form_contains_leaderboard_controls():
    """表单模型键必须落到 render 出来的 schema 里（行为级，非源码扫描）。"""
    rendered_models = set()

    def walk(node):
        if isinstance(node, dict):
            model = node.get('props', {}).get('model') if isinstance(node.get('props'), dict) else None
            if model:
                rendered_models.add(model)
            for value in node.values():
                walk(value)
        elif isinstance(node, (list, tuple)):
            for item in node:
                walk(item)

    walk(FORM)
    for key in ("leaderboard_enabled", "leaderboard_sources", "leaderboard_page_size",
                "leaderboard_cache_minutes", "leaderboard_refresh_minutes"):
        check(f"3-1 表单渲染出控件 {key}", key in rendered_models, "未渲染")

    # 榜单来源多选必须真的带上 items（否则用户无从勾选）
    source_items = []

    def walk_select(node):
        if isinstance(node, dict):
            if node.get('component') == 'VSelect':
                props = node.get('props') or {}
                if props.get('model') == 'leaderboard_sources':
                    source_items.append(props)
            for value in node.values():
                walk_select(value)
        elif isinstance(node, (list, tuple)):
            for item in node:
                walk_select(item)

    walk_select(FORM)
    check("3-2 榜单来源为多选且带选项",
          len(source_items) == 1 and source_items[0].get('multiple') is True
          and len(source_items[0].get('items') or []) > 0,
          str(source_items[:1]))


def test_tab_registered_in_both_containers():
    tab_labels, window_values = [], []

    def walk(node):
        if isinstance(node, dict):
            component = node.get('component')
            if component == 'VTab':
                tab_labels.append((node.get('props') or {}).get('value'))
            elif component == 'VWindowItem':
                window_values.append((node.get('props') or {}).get('value'))
            for value in node.values():
                walk(value)
        elif isinstance(node, (list, tuple)):
            for item in node:
                walk(item)

    walk(FORM)
    check("4-1 VTabs 含 leaderboard_tab 且位于末尾",
          tab_labels and tab_labels[-1] == 'leaderboard_tab', str(tab_labels))
    check("4-2 VWindow 含 leaderboard_tab 且与 VTabs 顺序一致",
          window_values == tab_labels, f"{window_values} != {tab_labels}")


# ===========================================================================
# 2. 数据页：榜单卡片
# ===========================================================================

SNAPSHOT = [
    {'title': '流浪地球3', 'year': '2027', 'media_type': 'movie',
     'vote_average': 8.4, 'source_name': 'TMDB 热门电影', 'tmdb_id': 1},
    {'title': '', 'media_type': 'tv'},  # 缺标题：必须被跳过而不是渲染空行
    {'title': '漫长的季节', 'year': '2023', 'media_type': 'tv',
     'vote_average': 9.4, 'source_name': '豆瓣热门剧集'},
]


def test_page_renders_card_on_both_paths():
    empty_page = UIConfig.get_page([])
    data_page = UIConfig.get_page(
        [{'status': '成功', 'type': '电影', 'title': 'T', 'time': '2026-09-19 10:00'}],
        SNAPSHOT,
    )
    check("5-1 无转存记录时仍渲染榜单卡片",
          empty_page and empty_page[-1].get('component') == 'VCard',
          str(empty_page[-1].get('component') if empty_page else None))
    check("5-2 有转存记录时渲染榜单卡片",
          data_page and data_page[-1].get('component') == 'VCard',
          str(data_page[-1].get('component') if data_page else None))
    check("5-3 兼容旧调用（不传快照不报错）", len(UIConfig.get_page([])) >= 2, "")


def test_card_content_and_tolerance():
    card = UIConfig._leaderboard_card(SNAPSHOT)
    body = card['content'][0]['content']
    title_node = body[0]['content'][0]
    check("6-1 卡片标题显示条数", '2' in str(title_node.get('text')),
          str(title_node.get('text')))
    # 3 条输入中 1 条缺标题 -> 只剩 2 行条目
    item_rows = [n for n in body if isinstance(n.get('content'), list)
                 and (n.get('props') or {}).get('class', '').startswith('d-flex justify-space-between align-center py-1')]
    check("6-2 缺标题条目被跳过", len(item_rows) == 2, str(len(item_rows)))
    flat = str(card)
    check("6-3 条目展示年份与评分", '2027' in flat and '8.4' in flat, "")
    check("6-4 条目展示来源", 'TMDB 热门电影' in flat, "")

    empty_card = UIConfig._leaderboard_card(None)
    empty_text = str(empty_card)
    check("6-5 空快照给出中文空状态",
          '暂无榜单数据' in empty_text and '榜单' in empty_text, "")
    check("6-6 空快照不抛异常且结构完整",
          isinstance(empty_card, dict) and empty_card.get('component') == 'VCard', "")


def test_share_link_card_routes_and_redacts_access_code():
    """盘链卡片在两条数据页路径均可用，并只发输入字段到既有相对 API。"""
    page_empty = UIConfig.get_page([])
    page_history = UIConfig.get_page([{'status': '成功', 'type': '电影', 'title': 'T',
                                       'time': '2026-09-19 10:00'}])

    def locate_share_card(page):
        for node in page:
            if '115盘链解析' in str(node):
                return node
        return None

    card_empty = locate_share_card(page_empty)
    card_history = locate_share_card(page_history)
    check('7-1 空历史数据页渲染 115盘链解析卡片', card_empty is not None, '')
    check('7-2 有历史数据页渲染 115盘链解析卡片', card_history is not None, '')
    card = card_empty or {}
    flat = str(card)
    check('7-3 卡片说明状态文件数与访问码提示',
          '状态' in flat and '文件数量' in flat and '访问码' in flat, flat)
    check('7-4 卡片不含任何访问码输入回显字段',
          'receive_code' not in flat and 'password' not in flat.lower(), flat)

    clicks = []
    def walk(node):
        if isinstance(node, dict):
            event = node.get('events', {}).get('click') if isinstance(node.get('events'), dict) else None
            if isinstance(event, dict):
                clicks.append(event)
            for value in node.values():
                walk(value)
        elif isinstance(node, (list, tuple)):
            for value in node:
                walk(value)
    walk(card)
    check('7-5 盘链按钮调用既有相对解析路由',
          len(clicks) == 1 and clicks[0].get('api') == 'plugin/P115SubSearch/resolve_share_link', str(clicks))
    check('7-6 盘链按钮只提交文本模型，不携带访问码字段',
          len(clicks) == 1 and clicks[0].get('params') == {'text': 'share_link_text'}, str(clicks))


def test_refresh_button_targets_registered_route():
    card = UIConfig._leaderboard_card(SNAPSHOT)
    clicks = []

    def walk(node):
        if isinstance(node, dict):
            events = node.get('events')
            if isinstance(events, dict) and isinstance(events.get('click'), dict):
                clicks.append(events['click'])
            for value in node.values():
                walk(value)
        elif isinstance(node, (list, tuple)):
            for item in node:
                walk(item)

    walk(card)
    check("7-1 榜单卡片含刷新按钮", len(clicks) == 1, str(clicks))
    if clicks:
        click = clicks[0]
        check("7-2 刷新按钮使用相对路径且不带 apikey",
              click.get('api') == 'plugin/P115SubSearch/leaderboard/refresh'
              and 'apikey' not in str(click), str(click))
        check("7-3 刷新按钮为 POST", click.get('method') == 'post', str(click.get('method')))

    init_text = (PLUGIN / '__init__.py').read_text(encoding="utf-8")
    check("7-4 后端已注册 /leaderboard/refresh 路由",
          '"/leaderboard/refresh"' in init_text.split("def get_api", 1)[-1], "未注册")
    check("7-5 手动刷新走后台调度器（不在请求线程同步抓取）",
          "def api_refresh_leaderboard" in init_text
          and "add_job" in init_text.split("def api_refresh_leaderboard", 1)[-1].split("def ")[0],
          "刷新端点未使用后台调度")


def test_no_network_on_page_render():
    """数据页渲染必须零网络：榜单来源为纯静态目录。"""
    calls = []

    class _Boom:
        def __getattr__(self, name):
            def _recorder(*a, **k):
                calls.append(name)
                raise AssertionError(f"页面渲染触发了网络/外部调用：{name}")
            return _recorder

    original = _leaderboard_client.RecommendChain
    _leaderboard_client.RecommendChain = _Boom()
    try:
        UIConfig.get_page([], SNAPSHOT)
        UIConfig.get_form()
        UIConfig.get_leaderboard_source_options()
    finally:
        _leaderboard_client.RecommendChain = original
    check("8-1 数据页/配置页渲染零网络", not calls, str(calls))


# ===========================================================================
# runner
# ===========================================================================

TESTS = [
    test_form_defaults,
    test_source_options_single_source_of_truth,
    test_form_contains_leaderboard_controls,
    test_tab_registered_in_both_containers,
    test_page_renders_card_on_both_paths,
    test_card_content_and_tolerance,
    test_share_link_card_routes_and_redacts_access_code,
    test_refresh_button_targets_registered_route,
    test_no_network_on_page_render,
]


def main():
    print("=" * 68)
    print("P115SubSearch v1.9.0 声明式 UI 集成测试（真实执行）")
    print("=" * 68)
    for fn in TESTS:
        print()
        print(f"-- {fn.__name__}")
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            _failed.append(f"{fn.__name__} 执行异常 :: {type(exc).__name__}: {exc}")
            print(f"  [FAIL] {fn.__name__} 执行异常 :: {type(exc).__name__}: {exc}")
    print()
    print("=" * 68)
    print(f"结果：{_passed} 项通过，{len(_failed)} 项失败")
    print("=" * 68)
    if _failed:
        for item in _failed:
            print(f"  ✗ {item}")
        return 1
    print("\n✅ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
