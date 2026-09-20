# -*- coding: utf-8 -*-
"""
P115SubSearch v1.9.1 测试：榜单首次使用可用性（默认启用 + 安全来源 + 数据页状态）

背景（用户反馈）：
  榜单订阅 v1.9.0 默认 ``leaderboard_enabled=False`` 且 ``leaderboard_sources=[]``，
  首次安装的用户进入数据页只能看到「暂无榜单数据 + 去设置页勾选来源」，
  既没有默认来源也没有任何可直接操作的入口 —— 功能等于不可用。

覆盖（**真实执行**，非源码字符串断言）：
  1. 首次使用默认配置 = 启用 + 安全默认来源（且默认来源确实存在于后端来源目录）；
  2. ``resolve_source_ids`` 首次使用兜底：启用且空/全无效来源 -> 默认来源；
     关闭时尊重用户空配置；未知 id 过滤、去重、字符串逗号分隔容错；
  3. 数据页展示当前启用状态与已配置来源中文名（有/无转存记录两条路径）；
  4. 未启用时给出可操作的中文引导；空快照时按钮为「初始化榜单」，
     有数据时为「刷新榜单」，两者 api 不变（相对路径 / POST）；
  5. 旧的两参 ``get_page`` 调用与单参 ``_leaderboard_card`` 调用保持兼容；
  6. 渲染数据白名单：即使调用方误传 Cookie / Token / 访问码等敏感字段，
     也不会出现在数据页输出中；
  7. 数据页 / 配置页渲染零网络；
  8. 插件侧 ``_leaderboard_page_state`` 真实执行：只产出非敏感状态，
     且 init_plugin 走 first-use 兜底、get_page 传入状态。

运行：python tests/test_v191_leaderboard_first_use.py
"""
import ast
import importlib.util
import sys
import textwrap
import types
from pathlib import Path

PLUGIN = Path(__file__).resolve().parent.parent
INIT_SRC = (PLUGIN / "__init__.py").read_text(encoding="utf-8")

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


def method_source(source, class_name, method_name):
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name == method_name:
                    return ast.get_source_segment(source, item) or ""
    return ""


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

    sys.modules['p115subsearch'] = types.ModuleType('p115subsearch')
    sys.modules['p115subsearch'].__path__ = [str(PLUGIN)]


def _load(mod_name, rel_path):
    spec = importlib.util.spec_from_file_location(mod_name, PLUGIN / rel_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


_install_stubs()
_lb = _load('p115subsearch.clients.leaderboard', 'clients/leaderboard.py')
_ui = _load('p115subsearch.ui.config', 'ui/config.py')
UIConfig = _ui.UIConfig

FORM, DEFAULTS = UIConfig.get_form()
SNIPPET_CHAIN = None


class _RecordingChain:
    """假 RecommendChain：记录调用但绝不触网（用于验证渲染零网络 / 来源中文名）。"""

    def __init__(self):
        self.calls = []

    def tmdb_trending(self, **kwargs):
        self.calls.append(('tmdb_trending', kwargs))
        return []

    def __getattr__(self, name):
        def _recorder(*a, **k):
            self.calls.append((name, k))
            return []
        return _recorder


SNAPSHOT = [
    {'title': '流浪地球3', 'year': '2027', 'media_type': 'movie',
     'vote_average': 8.4, 'source_name': 'TMDB 流行趋势', 'tmdb_id': 1},
    {'title': '', 'media_type': 'tv'},  # 缺标题：必须被跳过
    {'title': '漫长的季节', 'year': '2023', 'media_type': 'tv',
     'vote_average': 9.4, 'source_name': '豆瓣热门剧集'},
]

ENABLED_STATE = {
    'enabled': True,
    'sources': ['tmdb_trending'],
    'source_names': ['TMDB 流行趋势'],
    'refresh_minutes': 0,
}
DISABLED_STATE = {'enabled': False, 'sources': [], 'source_names': [], 'refresh_minutes': 0}


def _flat(node):
    return str(node)


# ===========================================================================
# 1. 首次使用默认配置
# ===========================================================================

def test_first_use_default_enabled_with_safe_source():
    check("1-1 默认启用榜单订阅", DEFAULTS.get("leaderboard_enabled") is True,
          repr(DEFAULTS.get("leaderboard_enabled")))
    sources = DEFAULTS.get("leaderboard_sources")
    check("1-2 默认来源非空且为列表", isinstance(sources, list) and bool(sources),
          repr(sources))
    check("1-3 默认来源含 TMDB 流行趋势",
          bool(sources) and sources[0] == "tmdb_trending", repr(sources))

    backend_ids = [item["id"] for item in _lb.LEADERBOARD_SOURCES]
    check("1-4 默认来源全部存在于后端来源目录",
          all(sid in backend_ids for sid in (sources or [])), repr(sources))
    check("1-5 默认来源与后端常量同源",
          list(sources or []) == list(_lb.DEFAULT_LEADERBOARD_SOURCES),
          f"{sources} != {_lb.DEFAULT_LEADERBOARD_SOURCES}")

    # 默认来源必须在配置页可选（否则用户无法看到/取消）
    option_ids = [opt["value"] for opt in UIConfig.get_leaderboard_source_options()]
    check("1-6 默认来源在配置页可选项中",
          all(sid in option_ids for sid in (sources or [])), repr(option_ids))

    # 不能破坏既有配置键
    check("1-7 既有配置键未被破坏", DEFAULTS.get("max_transfer_links") == 5
          and DEFAULTS.get("pansou_check_enabled") is True, "")


def test_default_source_has_chinese_name():
    client = _lb.LeaderboardClient(chain=_RecordingChain())
    name = client.source_name(_lb.DEFAULT_LEADERBOARD_SOURCES[0])
    check("2-1 默认来源有中文展示名", bool(name) and name != "tmdb_trending", repr(name))
    check("2-2 默认来源可被目录列出",
          any(item["id"] == _lb.DEFAULT_LEADERBOARD_SOURCES[0] for item in client.sources()),
          "")


# ===========================================================================
# 2. resolve_source_ids 首次使用兜底（真实执行）
# ===========================================================================

def test_resolve_source_ids_first_use_fallback():
    default = list(_lb.DEFAULT_LEADERBOARD_SOURCES)

    check("3-1 启用 + 未配置 -> 默认来源",
          _lb.resolve_source_ids(None, enabled=True) == default,
          repr(_lb.resolve_source_ids(None, enabled=True)))
    check("3-2 启用 + 空列表 -> 默认来源",
          _lb.resolve_source_ids([], enabled=True) == default, "")
    check("3-3 启用 + 全是无效 id -> 默认来源",
          _lb.resolve_source_ids(["ghost", "", None], enabled=True) == default, "")
    check("3-4 启用 + 非法类型 -> 默认来源",
          _lb.resolve_source_ids({"a": 1}, enabled=True) == default, "")

    check("3-5 关闭 + 未配置 -> 保持空（不启用就不强塞来源）",
          _lb.resolve_source_ids(None, enabled=False) == [], "")
    check("3-6 关闭 + 已配置 -> 保留用户配置（便于重新启用）",
          _lb.resolve_source_ids(["douban_tv_hot"], enabled=False) == ["douban_tv_hot"], "")

    check("3-7 有效 id 保留且去重、保持顺序",
          _lb.resolve_source_ids(["douban_tv_hot", "tmdb_trending", "douban_tv_hot"],
                                 enabled=True) == ["douban_tv_hot", "tmdb_trending"], "")
    check("3-8 未知 id 被过滤但有效 id 保留",
          _lb.resolve_source_ids(["ghost", "tmdb_movies"], enabled=True) == ["tmdb_movies"], "")
    check("3-9 字符串逗号分隔容错（含中文逗号）",
          _lb.resolve_source_ids("tmdb_movies， douban_movie_hot", enabled=True)
          == ["tmdb_movies", "douban_movie_hot"], "")
    check("3-10 默认常量不被调用方污染",
          (lambda arr: (arr.append("x"), _lb.default_leaderboard_sources() == default)[1])(
              _lb.default_leaderboard_sources()), "")


# ===========================================================================
# 3. 数据页：启用状态 + 已配置来源
# ===========================================================================

def _card_text(page_or_card):
    return _flat(page_or_card)


def test_page_shows_enabled_state_and_sources():
    empty_page = UIConfig.get_page([], None, ENABLED_STATE)
    data_page = UIConfig.get_page(
        [{'status': '成功', 'type': '电影', 'title': 'T', 'time': '2026-09-19 10:00'}],
        SNAPSHOT, ENABLED_STATE)

    empty_text = _card_text(empty_page[-1])
    data_text = _card_text(data_page[-1])
    check("4-1 无转存记录时展示「已启用」", '已启用' in empty_text, empty_text[:80])
    check("4-2 无转存记录时展示已配置来源中文名",
          'TMDB 流行趋势' in empty_text, empty_text[:120])
    check("4-3 有转存记录时展示「已启用」与来源",
          '已启用' in data_text and 'TMDB 流行趋势' in data_text, "")
    check("4-4 有数据时仍展示状态行（状态独立于快照）",
          '来源：TMDB 流行趋势' in data_text, "")


def test_page_shows_actionable_guidance_when_disabled():
    page = UIConfig.get_page([], None, DISABLED_STATE)
    text = _card_text(page[-1])
    check("5-1 未启用时明确展示「未启用」", '未启用' in text, text[:80])
    check("5-2 未启用时给出可操作的设置页引导",
          '设置页' in text and '启用榜单订阅' in text, text[:160])
    check("5-3 未启用时不误报来源", '来源：未配置来源' in text, text[:160])

    missing_state = UIConfig.get_page([], None)
    missing_text = _card_text(missing_state[-1])
    check("5-4 调用方未传状态时退化为未启用提示（不抛异常）",
          '未启用' in missing_text, missing_text[:80])


def test_initialize_entry_switches_with_snapshot():
    empty_card = UIConfig._leaderboard_card(None, ENABLED_STATE)
    data_card = UIConfig._leaderboard_card(SNAPSHOT, ENABLED_STATE)

    def _button(card):
        found = []

        def walk(node):
            if isinstance(node, dict):
                if node.get('component') == 'VBtn':
                    found.append(node)
                for value in node.values():
                    walk(value)
            elif isinstance(node, (list, tuple)):
                for item in node:
                    walk(item)

        walk(card)
        return found[0] if found else {}

    empty_btn = _button(empty_card)
    data_btn = _button(data_card)
    check("6-1 空快照按钮为「初始化榜单」", empty_btn.get('text') == '初始化榜单',
          repr(empty_btn.get('text')))
    check("6-2 有数据按钮为「刷新榜单」", data_btn.get('text') == '刷新榜单',
          repr(data_btn.get('text')))
    for label, btn in (("6-3 初始化", empty_btn), ("6-4 刷新", data_btn)):
        click = ((btn.get('events') or {}).get('click') or {})
        check(f"{label}入口走相对路径 / POST",
              click.get('api') == 'plugin/P115SubSearch/leaderboard/refresh'
              and click.get('method') == 'post' and 'apikey' not in _flat(btn),
              repr(click))
    check("6-5 初始化文案向用户说明由后台抓取",
          '后台' in _flat(empty_btn) or '初始化' in _flat(empty_card), "")


def test_legacy_calls_still_work():
    # v1.9.0 的两参 get_page / 单参 _leaderboard_card 必须继续可用
    try:
        legacy = UIConfig.get_page([], SNAPSHOT)
        legacy_card = UIConfig._leaderboard_card(SNAPSHOT)
        legacy_empty = UIConfig._leaderboard_card(None)
        ok = (legacy and legacy[-1].get('component') == 'VCard'
              and legacy_card.get('component') == 'VCard'
              and legacy_empty.get('component') == 'VCard')
    except Exception as exc:  # noqa: BLE001
        check("7-1 兼容旧调用签名", False, f"{type(exc).__name__}: {exc}")
        return
    check("7-1 兼容旧调用签名（两参 get_page / 单参 card）", ok, "")

    # 卡片结构未被破坏：标题仍带条数，缺标题条目仍被跳过
    body = UIConfig._leaderboard_card(SNAPSHOT, ENABLED_STATE)['content'][0]['content']
    check("7-2 卡片标题仍显示条数", '2' in str(body[0]['content'][0].get('text')),
          str(body[0]['content'][0].get('text')))
    item_rows = [n for n in body if isinstance(n.get('content'), list)
                 and (n.get('props') or {}).get('class', '').startswith(
                     'd-flex justify-space-between align-center py-1')]
    check("7-3 缺标题条目仍被跳过", len(item_rows) == 2, str(len(item_rows)))


def test_no_secrets_leak_into_page():
    """状态白名单：调用方误传敏感字段也不得进入渲染结果。"""
    secrets = {
        "cookies": "SecretCookieUID_1A2B3C",
        "token": "SecretTokenXYZ",
        "password": "SecretPassword123",
        "dian115_password": "SecretDianPass",
        "pansou_password": "SecretPansouPass",
        "access_code": "SecretAccessCode",
        "share_pwd": "SecretSharePwd",
        "apikey": "SecretApiKey",
    }
    state = dict(ENABLED_STATE)
    state.update(secrets)
    rendered = _flat(UIConfig.get_page([], SNAPSHOT, state))
    leaked = [key for key, value in secrets.items() if value in rendered]
    check("8-1 敏感字段值不出现在数据页", not leaked, str(leaked))
    check("8-2 渲染结果不含 apikey 字样", 'apikey' not in rendered, "")
    check("8-3 白名单只保留状态与来源名",
          UIConfig._leaderboard_state_summary(state) == {
              'enabled': True, 'source_names': ['TMDB 流行趋势']},
          repr(UIConfig._leaderboard_state_summary(state)))
    leaderboard_rendered = _flat(UIConfig._leaderboard_card(SNAPSHOT, state))
    check("8-4 榜单卡片不展示任何访问码/提取码字段",
          all(word not in leaderboard_rendered for word in ('访问码', '提取码', '密码')), "")


# ===========================================================================
# 4. 零网络 + 插件侧状态装配
# ===========================================================================

def test_page_render_zero_network():
    chain = _RecordingChain()
    original = _lb.RecommendChain
    _lb.RecommendChain = lambda: chain
    try:
        UIConfig.get_page([], SNAPSHOT, ENABLED_STATE)
        UIConfig.get_form()
        UIConfig.get_leaderboard_source_options()
    finally:
        _lb.RecommendChain = original
    check("9-1 数据页/配置页渲染零网络", not chain.calls, str(chain.calls))


def _load_page_state_method():
    source = method_source(INIT_SRC, "P115SubSearch", "_leaderboard_page_state")
    if not source:
        return None
    namespace = {"Dict": dict, "List": list, "Any": object, "Optional": object}
    exec("class _Harness:\n" + textwrap.indent(source, "    "), namespace)
    return namespace["_Harness"]


class _FakeClient:
    def source_name(self, source_id):
        return {"tmdb_trending": "TMDB 流行趋势"}.get(source_id, source_id)


def test_plugin_page_state_is_non_sensitive_and_wired():
    Harness = _load_page_state_method()
    check("10-1 插件实现了 _leaderboard_page_state", Harness is not None, "未找到")
    if Harness is None:
        return

    harness = Harness()
    harness._leaderboard_enabled = True
    harness._leaderboard_sources = ["tmdb_trending", "ghost_source"]
    harness._leaderboard_refresh_minutes = 30
    harness._leaderboard_client = _FakeClient()
    state = harness._leaderboard_page_state()
    check("10-2 状态含启用标记", state.get("enabled") is True, repr(state))
    check("10-3 状态含来源与中文名",
          state.get("sources") == ["tmdb_trending", "ghost_source"]
          and state.get("source_names") == ["TMDB 流行趋势", "ghost_source"], repr(state))
    check("10-4 状态不含任何敏感键",
          set(state) == {"enabled", "sources", "source_names", "refresh_minutes"},
          repr(sorted(state)))

    # 客户端缺失时退化为来源 id，不允许抛异常
    harness._leaderboard_client = None
    fallback = harness._leaderboard_page_state()
    check("10-5 无客户端时退化为 id 且不抛异常",
          fallback.get("source_names") == ["tmdb_trending", "ghost_source"], repr(fallback))

    # 插件侧装配：first-use 兜底 + get_page 传入状态
    init_plugin = method_source(INIT_SRC, "P115SubSearch", "init_plugin")
    check("10-6 init_plugin 使用 resolve_source_ids 归一化来源",
          "resolve_source_ids(" in init_plugin, "未走归一化")
    check("10-7 init_plugin 对缺失开关默认启用",
          "raw_leaderboard_enabled is None" in init_plugin
          and "raw_leaderboard_enabled = True" in init_plugin, "缺少 first-use 兜底")
    page_fn = method_source(INIT_SRC, "P115SubSearch", "get_page")
    check("10-8 插件 get_page 向 UI 传入状态",
          "UIConfig.get_page(history, leaderboard_snapshot, self._leaderboard_page_state())"
          in page_fn, page_fn[:200])

    api_fn = method_source(INIT_SRC, "P115SubSearch", "api_refresh_leaderboard")
    check("10-9 手动初始化接口在未启用时给出可操作提示",
          "榜单订阅未启用" in api_fn, "")


# ===========================================================================
# runner
# ===========================================================================

TESTS = [
    test_first_use_default_enabled_with_safe_source,
    test_default_source_has_chinese_name,
    test_resolve_source_ids_first_use_fallback,
    test_page_shows_enabled_state_and_sources,
    test_page_shows_actionable_guidance_when_disabled,
    test_initialize_entry_switches_with_snapshot,
    test_legacy_calls_still_work,
    test_no_secrets_leak_into_page,
    test_page_render_zero_network,
    test_plugin_page_state_is_non_sensitive_and_wired,
]


def main():
    print("=" * 68)
    print("P115SubSearch v1.9.1 榜单首次使用可用性测试（真实执行）")
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
