# -*- coding: utf-8 -*-
"""v1.8.5 回归测试（脚本式，直接 `python 本文件` 运行）。

覆盖本轮四项改动：
    1. 癫影签到/抽奖：`_game_item` 兼容 items / games / 根三种容器；
    2. 癫影离线型资源（magnet / ed2k）：识别 + 115 云下载提交 + 链接优先取 115；
    3. 后台刷新静默（silent）+ 打开设置页刷新账户；
    4. HDHive 彻底删除：源码零残留 + 全模块可编译（加载级守护）。
"""
import ast
import re
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# 注意：包内存在多个 __init__.py，key 必须用相对路径，不能只用文件名
SOURCES = {}
for path in ROOT.rglob("*.py"):
    if "__pycache__" not in str(path):
        SOURCES[str(path.relative_to(ROOT)).replace("\\", "/")] = \
            path.read_text(encoding="utf-8")

INIT_SRC = SOURCES["__init__.py"]
P115_SRC = SOURCES["clients/p115.py"]
SYNC_SRC = SOURCES["handlers/sync.py"]
SEARCH_SRC = SOURCES["handlers/search.py"]
CONFIG_SRC = SOURCES["ui/config.py"]

_RESULTS = []


def check(name, fn):
    try:
        fn()
        print("  [PASS] %s" % name)
        _RESULTS.append(True)
    except Exception as error:  # noqa: BLE001
        print("  [FAIL] %s :: %s" % (name, error))
        _RESULTS.append(False)


def section(title):
    print("\n" + "=" * 68)
    print(title)
    print("=" * 68)


def _extract_function(rel_path, func_name, class_name=None):
    """从源码里抽出函数/方法源码文本（AST + dedent）。"""
    source = SOURCES[rel_path]
    tree = ast.parse(source)

    def walk_body(body):
        for node in body:
            if isinstance(node, ast.FunctionDef) and node.name == func_name:
                return textwrap.dedent(ast.get_source_segment(source, node))
        return None

    found = None
    if class_name is None:
        found = walk_body(tree.body)
    else:
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == class_name:
                found = walk_body(node.body)
                if found:
                    break
    if not found:
        raise AssertionError("未找到 %s::%s" % (rel_path, func_name))
    return found


def _exec_snippet(source, namespace):
    exec(compile(source, "<extracted>", "exec"), namespace)  # noqa: S102
    return namespace


def _unbind(fn):
    return fn.__func__ if hasattr(fn, "__func__") else fn


# ---------------------------------------------------------------- 1. 签到解析
section("1. 癫影签到/抽奖：_game_item 容器兼容")


class FakeDian115Error(Exception):
    def __init__(self, message="", code="", status_code=0):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


_ns_game = {"Dict": dict, "Any": object, "Dian115Error": FakeDian115Error}
_exec_snippet(
    _extract_function("clients/dian115.py", "_game_item", "Dian115Client"),
    _ns_game,
)
game_item = _unbind(_ns_game["_game_item"])
_WHEEL = {"used_today": 20, "max_plays": 20}


def t_items():
    got = game_item({"code": "ok", "items": {"daily_wheel": _WHEEL}}, "daily_wheel")
    assert got == _WHEEL, got


def t_games():
    got = game_item({"games": {"daily_wheel": _WHEEL}}, "daily_wheel")
    assert got == _WHEEL, got


def t_root():
    got = game_item({"daily_wheel": _WHEEL}, "daily_wheel")
    assert got == _WHEEL, got


def t_missing():
    try:
        game_item({"code": "ok", "items": {}}, "daily_wheel")
    except FakeDian115Error:
        return
    raise AssertionError("缺字段时应抛 Dian115Error")


check("1-1 站点实测结构 items.daily_wheel", t_items)
check("1-2 兼容 games 容器", t_games)
check("1-3 兼容根级容器", t_root)
check("1-4 缺少字段仍抛 schema_changed", t_missing)

# ------------------------------------------------------- 2. 离线链接与云下载
section("2. 癫影离线型资源（magnet / ed2k）")

_ns_offline = {"OFFLINE_LINK_PREFIXES": ("magnet:", "ed2k://", "thunder://", "ftp://")}
_exec_snippet(_extract_function("handlers/sync.py", "_is_offline_link"), _ns_offline)
is_offline_link = _ns_offline["_is_offline_link"]


def t_offline_yes():
    assert is_offline_link("magnet:?xt=urn:btih:24eb124f")
    assert is_offline_link("ed2k://|file|xxx|")
    assert is_offline_link("  MAGNET:?xt=urn:btih:abc ")


def t_offline_no():
    assert not is_offline_link("https://115.com/s/abc?password=def")
    assert not is_offline_link("")
    assert not is_offline_link(None)


check("2-1 magnet/ed2k 判定为离线链接", t_offline_yes)
check("2-2 115 分享链接不是离线链接", t_offline_no)


class FakeLogger:
    def __init__(self):
        self.records = []

    def _add(self, level, message):
        self.records.append((level, str(message)))

    def info(self, message):
        self._add("info", message)

    def debug(self, message):
        self._add("debug", message)

    def warning(self, message):
        self._add("warning", message)

    def error(self, message):
        self._add("error", message)


class FakeCloudClient:
    def __init__(self, response=None):
        self.calls = []
        self.response = response or {"state": True}

    def clouddownload_task_add_url(self, payload):
        self.calls.append(payload)
        return self.response


class FakeRateLimiter:
    def wait(self):
        return None


class FakeP115Manager:
    def __init__(self, client, cid=123):
        self.client = client
        self._api_call_count = 0
        self.rate_limiter = FakeRateLimiter()
        self._cid = cid

    def get_pid_by_path(self, path, mkdir=True):  # noqa: ARG002
        return self._cid


_ns_add = {"Any": object, "Dict": dict, "Optional": object, "logger": FakeLogger()}
_exec_snippet(
    _extract_function("clients/p115.py", "add_offline_task", "P115ClientManager"),
    _ns_add,
)
add_offline_task = _unbind(_ns_add["add_offline_task"])


def t_add_ok():
    client = FakeCloudClient()
    manager = FakeP115Manager(client, cid=456)
    assert add_offline_task(manager, "magnet:?xt=urn:btih:abc", "/电影/测试") is True
    assert client.calls, "未调用云下载接口"
    payload = client.calls[0]
    assert payload.get("url") == "magnet:?xt=urn:btih:abc", payload
    assert payload.get("wp_path_id") == 456, payload


def t_add_fail():
    client = FakeCloudClient({"state": False, "error_msg": "quota exceeded"})
    manager = FakeP115Manager(client)
    assert add_offline_task(manager, "magnet:?xt=urn:btih:abc", "/x") is False


def t_add_no_client():
    manager = FakeP115Manager(None)
    manager.client = None
    assert add_offline_task(manager, "magnet:?xt=urn:btih:abc", "/x") is False


check("2-3 云下载提交成功并带上 wp_path_id", t_add_ok)
check("2-4 接口返回 state=False 判失败", t_add_fail)
check("2-5 客户端未初始化判失败", t_add_no_client)

# ------------------------------------------------- 3. 解锁响应优先取 115 链接
section("3. 解锁响应链接取值优先级")

_ns115 = {"re": re}
_exec_snippet(_extract_function("clients/dian115.py", "is_115_share_url"), _ns115)
_ns_extract = {
    "Dict": dict, "Any": object,
    "is_115_share_url": _ns115["is_115_share_url"],
}
_exec_snippet(
    _extract_function(
        "handlers/search.py", "_dian115_extract_unlock_url", "SearchHandler"),
    _ns_extract,
)
extract_unlock_url = _unbind(_ns_extract["_dian115_extract_unlock_url"])


def t_prefer_115():
    payload = {"payload": {"url": "magnet:?xt=urn:btih:abc",
                           "share_url": "https://115.com/s/swmxyz?password=abcd"}}
    got = extract_unlock_url(payload)
    assert got.startswith("https://115.com/s/"), got


def t_fallback_offline():
    payload = {"payload": {"url": "magnet:?xt=urn:btih:abc"}}
    assert extract_unlock_url(payload) == "magnet:?xt=urn:btih:abc"


def t_share_code():
    payload = {"share_code": "swmabc", "receive_code": "abcd"}
    assert extract_unlock_url(payload).startswith("https://115.com/s/swmabc")


def t_empty():
    assert extract_unlock_url({}) == ""
    assert extract_unlock_url(None) == ""


check("3-1 同时存在时优先 115 分享链接", t_prefer_115)
check("3-2 仅有 magnet 时回退为离线链接", t_fallback_offline)
check("3-3 share_code 仍可拼出 115 链接", t_share_code)
check("3-4 空响应返回空串", t_empty)

# --------------------------------------------------- 4. 刷新时机与日志静默
section("4. 后台刷新静默 + 打开设置页刷新")


def t_silent_param():
    assert "def get_account_info(self, silent: bool = False)" in P115_SRC
    assert 'logger.debug(f"115 登录成功' in P115_SRC
    assert "manager.get_account_info(silent=silent)" in INIT_SRC


def t_bg_silent():
    assert "self._load_account(key, allow_browser_login=True, silent=True)" in INIT_SRC


def t_page_refresh():
    tree = ast.parse(INIT_SRC)
    found = False
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for sub in node.body:
                if isinstance(sub, ast.FunctionDef) and sub.name == "get_page":
                    text = ast.get_source_segment(INIT_SRC, sub) or ""
                    found = "_refresh_accounts_on_page_open" in text
    assert found, "get_page 未触发账户刷新"


def t_page_refresh_no_browser():
    text = _extract_function(
        "__init__.py", "_refresh_accounts_on_page_open", "P115SubSearch")
    assert "allow_browser_login=False" in text, "设置页刷新必须禁用浏览器登录"
    assert "silent=True" in text


def t_offline_branch_wired():
    assert SYNC_SRC.count("if _is_offline_link(share_url):") >= 2, \
        "电影/电视剧两处都应接入离线分支"
    assert "_submit_offline_link" in SYNC_SRC
    assert "add_offline_task" in P115_SRC


check("4-1 115 账户读取支持 silent", t_silent_param)
check("4-2 后台定时刷新走 silent", t_bg_silent)
check("4-3 打开设置页触发账户刷新", t_page_refresh)
check("4-4 设置页刷新禁用浏览器登录且静默", t_page_refresh_no_browser)
check("4-5 电影/电视剧均已接入离线分支", t_offline_branch_wired)

# ------------------------------------------------------------ 5. HDHive 删除
section("5. HDHive 彻底删除 + 加载级守护")


def t_no_hdhive():
    # 本测试文件自身含 "hdhive" 字样，排除
    self_name = "tests/test_v185_offline_and_refresh.py"
    hit = [name for name, text in SOURCES.items()
           if "hdhive" in text.lower() and name != self_name]
    assert not hit, "仍有残留：%s" % hit


def t_no_hdhive_client():
    assert not (ROOT / "clients" / "hdhive.py").exists(), "hdhive 客户端文件未删除"


def t_no_hdhive_ui():
    assert "HDHive" not in CONFIG_SRC, "配置页仍有 HDHive 控件"


def t_compile_all():
    for name, text in SOURCES.items():
        compile(text, name, "exec")


def t_search_sources_intact():
    """删 hdhive 后其余搜索源必须还在。"""
    for token in ("pansou", "dian115", "nullbr", "kdocs"):
        assert token in SEARCH_SRC.lower(), "缺失搜索源 %s" % token


check("5-1 全插件源码无 hdhive 残留", t_no_hdhive)
check("5-2 hdhive 客户端文件已删除", t_no_hdhive_client)
check("5-3 配置页无 HDHive 控件", t_no_hdhive_ui)
check("5-4 全部模块可编译（加载级守护）", t_compile_all)
check("5-5 其余搜索源未受影响", t_search_sources_intact)

print("\n" + "=" * 68)
passed = sum(1 for item in _RESULTS if item)
print("结果：%d/%d 通过" % (passed, len(_RESULTS)))
print("ALL PASS" if passed == len(_RESULTS) else "存在失败项")
print("=" * 68)
sys.exit(0 if passed == len(_RESULTS) else 1)
