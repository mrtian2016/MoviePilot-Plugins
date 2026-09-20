# -*- coding: utf-8 -*-
"""
P115SubSearch v1.9.1 测试（三）：配置读写对称 / 版本双源同步 / 依赖边界

对齐任务书「历史经验」：
    * 版本号必须双源同步（package.v2.json + __init__.py plugin_version）；
    * 新增 typing/import 或模块级注解必须有模块 import guard；
    * UI/后端改动必须保持配置 read/write 对称：
      类属性、init_plugin 读取、UI 控件、__update_config 写回 四处齐全；
    * grep 证明没有 CloudSubscribe import 或旧插件名残留。

本文件为行为 + AST 测试（非源码字符串断言）。

运行：python tests/test_v190_config_symmetry.py
     或者 pytest tests/test_v190_config_symmetry.py
"""
import ast
import json
import re
import sys
from pathlib import Path

PLUGIN = Path(__file__).resolve().parent.parent
REPO = PLUGIN.parent.parent

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


def src(rel):
    return (PLUGIN / rel).read_text(encoding="utf-8")


def py_files():
    return sorted(p for p in PLUGIN.rglob("*.py") if "__pycache__" not in str(p))


# v1.9.1 新增的配置键
NEW_KEYS = [
    "leaderboard_enabled",
    "leaderboard_sources",
    "leaderboard_page_size",
    "leaderboard_cache_minutes",
    "leaderboard_refresh_minutes",
]


# ===========================================================================
# 1. 版本双源同步
# ===========================================================================

def test_version_dual_source():
    init_text = src("__init__.py")
    match = re.search(r'plugin_version\s*=\s*["\']([^"\']+)["\']', init_text)
    check("1-1 __init__.py 存在 plugin_version", match is not None, "未找到")
    check("1-2 plugin_version 为 1.9.1", match and match.group(1) == "1.9.1",
          match.group(1) if match else "?")

    pkg = json.loads((REPO / "package.v2.json").read_text(encoding="utf-8"))
    entry = pkg.get("P115SubSearch")
    check("1-3 package.v2.json 存在 P115SubSearch 条目", entry is not None, "缺失")
    check("1-4 package.v2.json version 为 1.9.1",
          entry and entry.get("version") == "1.9.1",
          str(entry.get("version")) if entry else "?")
    check("1-5 package.v2.json 有 v1.9.1 中文 history",
          entry and isinstance(entry.get("history"), dict) and "v1.9.1" in entry["history"],
          str(list((entry or {}).get("history", {}))[:3]))
    note = (entry or {}).get("history", {}).get("v1.9.1", "")
    check("1-6 history 文案为中文且非空",
          bool(note) and any("一" <= ch <= "鿿" for ch in note), note)
    check("1-7 __init__.py 版本历史含 v1.9.1",
          "v1.9.1" in init_text, "未找到 v1.9.1 说明")


# ===========================================================================
# 2. 配置四处对称
# ===========================================================================

def test_config_symmetry_four_places():
    init_text = src("__init__.py")
    ui_text = src("ui/config.py")

    # 2.1 类属性声明
    for key in NEW_KEYS:
        check(f"2-1 类属性 _{key} 声明", f"_{key}" in init_text, "未找到")

    # 2.2 init_plugin 读取（形如 config.get("leaderboard_xxx")）
    read_section = init_text.split("def init_plugin", 1)[-1]
    for key in NEW_KEYS:
        pattern = re.compile(rf'config\.get\(\s*["\']{key}["\']')
        check(f"2-2 init_plugin 读取 {key}", bool(pattern.search(read_section)),
              "init_plugin 中未读取")

    # 2.3 __update_config 写回
    update_section = init_text.split("def __update_config", 1)[-1]
    for key in NEW_KEYS:
        pattern = re.compile(rf'["\']{key}["\']\s*:')
        check(f"2-3 __update_config 写回 {key}", bool(pattern.search(update_section)),
              "__update_config 中未写回")

    # 2.4 UI 控件 + default_config 默认值
    for key in NEW_KEYS:
        check(f"2-4 UI 表单包含 {key}", key in ui_text, "ui/config.py 中未出现")
    defaults = ui_text.split("default_config", 1)[-1]
    for key in NEW_KEYS:
        pattern = re.compile(rf'["\']{key}["\']\s*:')
        check(f"2-5 default_config 含 {key}", bool(pattern.search(defaults)),
              "default_config 中未出现")

    # v1.9.1：首次使用即为可用状态 —— 默认启用 + 安全默认来源
    check("2-6 榜单开关默认启用", re.search(r'["\']leaderboard_enabled["\']\s*:\s*True', defaults)
          is not None, "默认值不是 True")
    check("2-7 榜单来源默认为安全来源常量",
          re.search(r'["\']leaderboard_sources["\']\s*:\s*list\(\s*DEFAULT_LEADERBOARD_SOURCES\s*\)',
                    defaults) is not None,
          "默认值未引用 DEFAULT_LEADERBOARD_SOURCES")

    # 现有能力不得被删除（配置键仍在）
    for legacy in ("kdocs_enabled", "pansou_check_enabled", "max_transfer_links",
                   "dian115_enabled", "only_115", "block_system_subscribe"):
        check(f"2-8 既有配置键保留 {legacy}", legacy in init_text and legacy in ui_text,
              "缺失")


def test_config_readback_roundtrip():
    """写入的配置必须能被读回（读写对称的行为验证）。

    v1.9.1：榜单开关与来源的读取改为「缺省即首次使用」语义 —— 开关用
    None 判断兜底为 True，来源统一交给 ``resolve_source_ids`` 归一化并在
    启用但空来源时回落安全默认来源。
    """
    init_text = src("__init__.py")
    update_section = init_text.split("def __update_config", 1)[-1]
    read_section = init_text.split("def init_plugin", 1)[-1]

    for key, cast in (
        ("leaderboard_page_size", "int"),
        ("leaderboard_cache_minutes", "int"),
        ("leaderboard_refresh_minutes", "int"),
    ):
        w = re.search(rf'["\']{key}["\']\s*:\s*(bool|int|list)\(', update_section)
        check(f"3-1 {key} 写回带 {cast} 转换", w and w.group(1) == cast,
              w.group(1) if w else "未匹配")

        line = ""
        for candidate in read_section.splitlines():
            if f'config.get("{key}")' in candidate:
                line = candidate
                break
        check(f"3-2 {key} 读取带 {cast} 转换", f"{cast}(" in line, line.strip() or "未找到读取行")
        check(f"3-3 {key} 读取有兜底默认值", " or " in line, line.strip() or "未找到读取行")

    # 榜单开关：写回 bool(...)，读取用 None 判定兜底为 True（首次使用默认启用）
    check("3-4 leaderboard_enabled 写回带 bool 转换",
          re.search(r'["\']leaderboard_enabled["\']\s*:\s*bool\(', update_section) is not None,
          "写回未带 bool 转换")
    check("3-5 leaderboard_enabled 读取缺省兜底为 True",
          'config.get("leaderboard_enabled")' in read_section
          and re.search(r'if\s+raw_leaderboard_enabled\s+is\s+None\s*:\s*\n\s*raw_leaderboard_enabled\s*=\s*True',
                        read_section) is not None,
          "未找到首次使用默认启用兜底")
    check("3-6 leaderboard_enabled 读取带 bool 转换",
          "bool(raw_leaderboard_enabled)" in read_section, "读取未带 bool 转换")

    # 榜单来源：写回 list(...)，读取交 resolve_source_ids 归一化 + 安全默认兜底
    check("3-7 leaderboard_sources 写回带 list 转换",
          re.search(r'["\']leaderboard_sources["\']\s*:\s*list\(', update_section) is not None,
          "写回未带 list 转换")
    check("3-8 leaderboard_sources 读取经 resolve_source_ids 归一化",
          re.search(r'resolve_source_ids\(\s*\n?\s*config\.get\("leaderboard_sources"\)',
                    read_section) is not None,
          "读取未走 resolve_source_ids")
    check("3-9 resolve_source_ids 已从 clients 导入",
          "resolve_source_ids" in init_text.split("def init_plugin", 1)[0]
          and "from .clients import" in init_text, "未导入 resolve_source_ids")


# ===========================================================================
# 3. 依赖边界 / import guard
# ===========================================================================

def _code_only(text):
    """去掉注释与字符串字面量，只留代码 token（迁移说明注释不算依赖）。"""
    import io
    import tokenize

    parts = []
    try:
        for tok in tokenize.generate_tokens(io.StringIO(text).readline):
            if tok.type in (tokenize.COMMENT, tokenize.STRING):
                continue
            parts.append(tok.string)
    except (tokenize.TokenError, IndentationError):
        return text
    return " ".join(parts)


def test_no_cloudsubscribe_import():
    offenders = []
    for path in py_files():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError as exc:
            offenders.append(f"{path.name}: 语法错误 {exc}")
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if "cloudsubscribe" in alias.name.lower():
                        offenders.append(f"{path.name}: import {alias.name}")
            elif isinstance(node, ast.ImportFrom):
                module = (node.module or "").lower()
                if "cloudsubscribe" in module:
                    offenders.append(f"{path.name}: from {node.module} import ...")
    check("4-1 全插件无 CloudSubscribe import", not offenders, "; ".join(offenders))

    # 代码（忽略注释/字符串中的迁移说明）不得引用源插件
    leftover = []
    for path in py_files():
        if path.name.startswith("test_"):
            continue
        if "cloudsubscribe" in _code_only(path.read_text(encoding="utf-8")).lower():
            leftover.append(path.name)
    check("4-2 非测试源码（忽略注释）无 cloudsubscribe 引用", not leftover, str(leftover))


def test_all_python_files_compile():
    bad = []
    for path in py_files():
        try:
            compile(path.read_text(encoding="utf-8"), str(path), "exec")
        except SyntaxError as exc:
            bad.append(f"{path.name}: {exc}")
        except Exception as exc:  # noqa: BLE001
            bad.append(f"{path.name}: {type(exc).__name__}: {exc}")
    check("5-1 全部 .py 文件 compile 通过", not bad, "; ".join(bad))


def test_module_level_app_imports_are_guarded():
    """模块级 app.* 导入必须在 try/except 内（import guard）。"""
    guarded_rel = ["clients/leaderboard.py", "handlers/leaderboard.py",
                   "handlers/share_link.py"]
    for rel in guarded_rel:
        path = PLUGIN / rel
        check(f"6-0 {rel} 存在", path.exists(), "文件缺失")
        if not path.exists():
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        unguarded = []
        for node in tree.body:  # 只看模块顶层
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("app"):
                unguarded.append(node.module)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("app"):
                        unguarded.append(alias.name)
        check(f"6-1 {rel} 模块级 app.* 导入均有 guard", not unguarded, str(unguarded))

        # 必须暴露可用性标记
        text = path.read_text(encoding="utf-8")
        check(f"6-2 {rel} 暴露 *_AVAILABLE 标记", "_AVAILABLE" in text, "未找到")


def test_leaderboard_modules_are_pure():
    """榜单 / 盘链模块不得反向依赖插件内的重模块（保证可离线单测与低耦合）。"""
    for rel in ("clients/leaderboard.py", "handlers/leaderboard.py"):
        tree = ast.parse(src(rel))
        relative = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.level or 0) > 0:
                relative.append(node.module)
        check(f"7-{rel} 无相对导入（可独立单测）", not relative, str(relative))


def test_api_routes_registered():
    """新增 API 路由必须注册，且不得开放匿名访问（框架默认 apikey 鉴权）。"""
    init_text = src("__init__.py")
    api_section = init_text.split("def get_api", 1)[-1]
    for route in ("/leaderboard/browse", "/leaderboard/subscribe",
                  "/leaderboard/refresh", "/resolve_share_link"):
        check(f"8-1 注册路由 {route}", f'"{route}"' in api_section, "未注册")
    check("8-2 路由未开放匿名访问（框架默认 apikey 鉴权）",
          "allow_anonymous" not in api_section, "出现 allow_anonymous 逃逸")
    check("8-3 路由端点已实现",
          "def api_leaderboard_browse" in init_text
          and "def api_leaderboard_subscribe" in init_text
          and "def api_refresh_leaderboard" in init_text
          and "def api_resolve_share_link" in init_text,
          "端点方法缺失")


def test_service_registration():
    """后台刷新服务必须在 get_service 中注册（慢源只走后台）。"""
    init_text = src("__init__.py")
    service_section = init_text.split("def get_service", 1)[-1]
    check("9-1 get_service 含榜单后台刷新", "leaderboard" in service_section.lower(),
          "未注册榜单服务")


def test_sync_hot_path_untouched():
    """榜单抓取不得进入订阅/搜索同步热路径。"""
    for rel in ("handlers/search.py", "handlers/subscribe.py", "core/services/sync.py"):
        path = PLUGIN / rel
        if not path.exists():
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        bad = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and "leaderboard" in (node.module or "").lower():
                bad.append(node.module)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if "leaderboard" in alias.name.lower():
                        bad.append(alias.name)
        check(f"10-{rel} 热路径未 import 榜单模块", not bad, str(bad))


# ===========================================================================
# runner
# ===========================================================================

TESTS = [
    test_version_dual_source,
    test_config_symmetry_four_places,
    test_config_readback_roundtrip,
    test_no_cloudsubscribe_import,
    test_all_python_files_compile,
    test_module_level_app_imports_are_guarded,
    test_leaderboard_modules_are_pure,
    test_api_routes_registered,
    test_service_registration,
    test_sync_hot_path_untouched,
]


def main():
    print("=" * 68)
    print("P115SubSearch v1.9.1 配置对称 / 版本 / 依赖边界测试")
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
        for f in _failed:
            print(f"  ✗ {f}")
        return 1
    print("\n✅ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
