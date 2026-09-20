# -*- coding: utf-8 -*-
"""
P115SubSearch v1.8.2 测试：积分解锁开关修复 + 账户登录状态展示

覆盖：
1. 【关键回归】dian115_auto_unlock / max_unlock_points / max_points_per_sub
   必须从 config 读回 —— v1.8.1 漏读导致开关保存后被类属性默认值覆盖、永远打不开。
2. 配置写入 __update_config 与读取字段必须一一对应（读写对称性）。
3. _collect_account_status() 只读本地状态，不触发网络/浏览器。
4. 115 get_account_info() 契约与 check_login() 布尔兼容。
5. UI _status_card / get_form(account_status) 结构正确。
6. 加载级守护：全模块可 compile。

运行：python tests/test_v182_unlock_and_status.py
"""
import ast
import json
import re
import sys
import traceback
from pathlib import Path

PLUGIN = Path(__file__).resolve().parent.parent
PLUGIN_NAME = PLUGIN.name

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


def src_of(rel):
    return (PLUGIN / rel).read_text(encoding="utf-8")


def method_source(source, class_name, method_name):
    """用 AST 精确抽取某个方法源码片段。"""
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name == method_name:
                    return ast.get_source_segment(source, item) or ""
    return ""


print("=" * 68)
print("1. 积分解锁配置读写对称性（v1.8.1 关键缺陷回归）")
print("=" * 68)
init_src = src_of("__init__.py")

UNLOCK_KEYS = [
    "dian115_auto_unlock",
    "dian115_max_unlock_points",
    "dian115_max_points_per_sub",
]

# 1-1 类属性声明存在
for key in UNLOCK_KEYS:
    check(f"1-{UNLOCK_KEYS.index(key)+1} 类属性 _{key} 已声明",
          f"_{key}:" in init_src or f"_{key} =" in init_src,
          f"未找到 _{key} 声明")

# 1-2 【核心】config.get 读取必须存在（v1.8.1 漏读处）
for idx, key in enumerate(UNLOCK_KEYS, start=4):
    check(f"1-{idx} config.get('{key}') 存在（修复点）",
          f'config.get("{key}"' in init_src,
          f"❌ 未从 config 读回 {key} —— 开关会被默认值覆盖，永远打不开")

# 1-3 读回赋值目标正确
check("1-7 _dian115_auto_unlock 由 config 赋值",
      re.search(r"self\._dian115_auto_unlock\s*=\s*config\.get\(\"dian115_auto_unlock\"", init_src)
      is not None,
      "赋值语句缺失或形参名不匹配")
check("1-8 _dian115_max_unlock_points 由 config 赋值",
      re.search(r"self\._dian115_max_unlock_points\s*=\s*int\(\s*config\.get\(\"dian115_max_unlock_points\"", init_src)
      is not None,
      "赋值语句缺失")
check("1-9 _dian115_max_points_per_sub 由 config 赋值",
      re.search(r"self\._dian115_max_points_per_sub\s*=\s*int\(\s*config\.get\(\"dian115_max_points_per_sub\"", init_src)
      is not None,
      "赋值语句缺失")

print()
print("=" * 68)
print("2. 全部 dian115 配置项读写对称（防止再漏）")
print("=" * 68)
# 从 __update_config 中提取所有 dian115_* 键
update_cfg = method_source(init_src, "P115SubSearch", "__update_config")
written = sorted(set(re.findall(r'"(dian115_[a-z_]+)"\s*:', update_cfg)))

# 从 __init__ 配置加载段提取所有 config.get("dian115_*")
# 定位加载段（含 config.get 的 dian115 字段）
read_keys = set(re.findall(r'config\.get\("(dian115_[a-z_]+)"', init_src))

missing = [k for k in written if k not in read_keys]
check(f"2-1 __update_config 写出的 {len(written)} 个 dian115 键全部有对应读取",
      not missing,
      f"❌ 写了但没读（会永远回退默认值）：{missing}")

# 反向：读了但没写的（会导致保存丢失）
extra = [k for k in sorted(read_keys) if k not in written and k not in {"dian115_token"}]
check("2-2 读取的 dian115 键都已纳入 update_config",
      not extra,
      f"⚠️ 读了但没写回（保存会丢失）：{extra}")
print(f"  写出的键：{written}")
print(f"  读取的键：{sorted(read_keys)}")

print()
print("=" * 68)
print("3. _collect_account_status 只读本地状态（禁止网络/浏览器）")
print("=" * 68)
status_src = method_source(init_src, "P115SubSearch", "_collect_account_status")
check("3-1 方法存在", bool(status_src), "未找到 _collect_account_status")

if status_src:
    # 剥掉注释与文档字符串后再检查，避免注释里提到方法名造成假阳性
    code_only = []
    for line in status_src.splitlines():
        stripped = line.split("#")[0]
        code_only.append(stripped)
    code_body = "\n".join(code_only)
    # 去掉所有字符串字面量内容
    code_body = re.sub(r'""".*?"""', "", code_body, flags=re.S)
    code_body = re.sub(r"'''.*?'''", "", code_body, flags=re.S)
    code_body = re.sub(r'"[^"]*"', '""', code_body)
    code_body = re.sub(r"'[^']*'", "''", code_body)

    # 禁止在该方法内出现真实网络调用
    forbidden = [
        ("get_account_info", "会发 HTTP 请求"),
        ("requests.", "会发 HTTP 请求"),
        ("session.get", "会发 HTTP 请求"),
        ("turnstile", "会启动浏览器"),
        ("launch_context", "会启动浏览器"),
    ]
    for idx, (token, why) in enumerate(forbidden, start=2):
        check(f"3-{idx} 未实际调用 {token}（{why}）",
              token not in code_body,
              f"❌ 配置页状态采集不得{why}，会拖慢表单渲染")

    check("3-7 走本地快照读取器（纯本地，零请求）",
          "_cached_account_status()" in status_src or "_load_account_snapshot" in status_src,
          "账户状态应通过本地快照读取器获取，不得直连第三方")
    check("3-8 有异常兜底",
          "except Exception" in status_src,
          "状态采集必须兜底，不能影响表单渲染")
    check("3-9 读取本地缓存而非实时请求",
          "_load_account_snapshot" in init_src,
          "账户状态应读独立本地快照（v1.8.3 起改为快照层）")

    # 3-10 快照读写方法成对存在（v1.8.3：由 __read/__cache_p115_account 升级为通用快照层）
    cache_read = method_source(init_src, "P115SubSearch", "_load_account_snapshot")
    cache_write = method_source(init_src, "P115SubSearch", "_save_account_snapshot")
    check("3-10 _load_account_snapshot 已定义", bool(cache_read), "缺失")
    check("3-11 _save_account_snapshot 已定义", bool(cache_write), "缺失")
    check("3-12 快照写入复用 get_data/save_data",
          "save_data" in cache_write and "get_data" in cache_read,
          "快照读写应走插件数据接口")

print()
print("=" * 68)
print("3b. import 级守护：模块内引用的名字必须已导入")
print("=" * 68)
# 扫描各模块顶层导入的名字，检查函数体内用到但未导入的标准库模块
STDLIB_MODULES = ["time", "json", "re", "os", "sys", "base64", "hashlib", "random", "math"]
import_fail = []
for rel in ["__init__.py", "clients/p115.py", "clients/dian115.py", "ui/config.py"]:
    text = src_of(rel)
    tree = ast.parse(text)
    # 收集顶层导入名
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                imported.add((a.asname or a.name).split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for a in node.names:
                imported.add(a.asname or a.name)
    # 收集属性访问的模块名（xxx.yyy）
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            base = node.value.id
            if base in STDLIB_MODULES and base not in imported:
                import_fail.append(f"{rel}: 使用了 {base} 但未导入")
check("3b-1 无「用了未导入」的标准库模块",
      not import_fail,
      "; ".join(sorted(set(import_fail))[:5]))

print()
print("=" * 68)
print("4. get_form 状态注入与 UI 结构")
print("=" * 68)
init_getform = method_source(init_src, "P115SubSearch", "get_form")
check("4-1 get_form 向 UIConfig 传入状态",
      "_collect_account_status()" in init_getform,
      "get_form 未注入登录状态")

cfg_src = src_of("ui/config.py")
form_src = method_source(cfg_src, "UIConfig", "get_form")
check("4-2 get_form 接受 account_status 参数",
      re.search(r"def get_form\(\s*account_status", form_src) is not None,
      "签名缺少 account_status")
check("4-3 账户卡片在 basic_rows 中插入",
      "_account_cards" in form_src and "basic_rows.append" in form_src,
      "未插入账户卡片")
check("4-4 _account_card 方法已定义",
      "def _account_card" in cfg_src,
      "缺少 _account_card")

card_src = method_source(cfg_src, "UIConfig", "_account_card")
check("4-5 _account_card 渲染 label/value 明细",
      "label" in card_src and "value" in card_src,
      "缺少 label/value 明细渲染")
check("4-6 _account_card 使用 VCard",
      "VCard" in card_src,
      "缺少 VCard 容器")
check("4-7 卡片带刷新按钮（对齐网盘搜索助手）",
      "refresh_account" in card_src,
      "缺少刷新按钮，无法手动拉取账户信息")

print()
print("=" * 68)
print("5. get_form 函数体结构完整性（括号闭合回归）")
print("=" * 68)
try:
    tree = ast.parse(cfg_src)
    class_names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "UIConfig":
            for f in node.body:
                if isinstance(f, ast.FunctionDef) and f.name == "get_form":
                    for stmt in f.body:
                        if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                            class_names.append(stmt.target.id)
                        elif isinstance(stmt, ast.Assign):
                            for t in stmt.targets:
                                if isinstance(t, ast.Name):
                                    class_names.append(t.id)
    expected = ["basic_rows", "p115_rows", "subscribe_tab", "checkin_tab",
                "pansou_tab", "dian115_tab", "kdocs_tab",
                "form_schema", "default_config"]
    missing_vars = [v for v in expected if v not in class_names]
    check("5-1 get_form 内所有 Tab 变量都在函数体顶层声明",
          not missing_vars,
          f"❌ 变量被错误嵌套进 basic_rows.extend()：{missing_vars}")
    print(f"  检测到顶层变量：{class_names}")
except SyntaxError as e:
    check("5-1 ui/config.py 语法正确", False, f"SyntaxError line {e.lineno}: {e.msg}")

print()
print("=" * 68)
print("6. 115 客户端 get_account_info 契约")
print("=" * 68)
p115_src = src_of("clients/p115.py")
p115_cls = None
for node in ast.walk(ast.parse(p115_src)):
    if isinstance(node, ast.ClassDef) and node.name == "P115ClientManager":
        p115_cls = node
        break

check("6-1 P115ClientManager 类存在", p115_cls is not None, "未找到类")
if p115_cls:
    methods = {i.name for i in p115_cls.body if isinstance(i, ast.FunctionDef)}
    check("6-2 get_account_info 方法存在", "get_account_info" in methods,
          f"缺失；现有方法：{sorted(methods)[:8]}")
    check("6-3 check_login 方法存在", "check_login" in methods, "缺失")
    check("6-4 get_pid_by_path 未被破坏", "get_pid_by_path" in methods,
          "❌ 编辑时误删了 get_pid_by_path")

    check_fn = method_source(p115_src, "P115ClientManager", "check_login")
    check("6-5 check_login 返回布尔（兼容旧调用）",
          "bool(" in check_fn and "get_account_info" in check_fn,
          "check_login 应委托 get_account_info 并返回布尔")

    info_fn = method_source(p115_src, "P115ClientManager", "get_account_info")
    check("6-6 get_account_info 返回 connected 字段",
          '"connected"' in info_fn,
          "缺少 connected 键")
    check("6-7 get_account_info 有异常兜底",
          "except Exception" in info_fn,
          "缺少异常处理")
    check("6-8 get_account_info 复用 user_my_info（不额外请求）",
          "user_my_info" in info_fn,
          "应复用 user_my_info 避免额外 API 调用")

print()
print("=" * 68)
print("7. 加载级守护：全部模块可编译")
print("=" * 68)
py_files = sorted(PLUGIN.rglob("*.py"))
py_files = [p for p in py_files if "__pycache__" not in str(p)]
compile_fail = []
for p in py_files:
    try:
        compile(p.read_text(encoding="utf-8"), str(p), "exec")
    except SyntaxError as e:
        compile_fail.append(f"{p.relative_to(PLUGIN)}:{e.lineno} {e.msg}")
check(f"7-1 全部 {len(py_files)} 个模块编译通过",
      not compile_fail,
      "; ".join(compile_fail[:5]))

print()
print("=" * 68)
print(f"结果：{_passed} 项通过，{len(_failed)} 项失败")
print("=" * 68)
if _failed:
    print("\n失败明细：")
    for f in _failed:
        print(f"  ✗ {f}")
    sys.exit(1)
print("\n✅ 全部通过")
