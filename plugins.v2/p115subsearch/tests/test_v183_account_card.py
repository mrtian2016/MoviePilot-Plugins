# -*- coding: utf-8 -*-
"""
P115SubSearch v1.8.3 测试：账户信息卡片（移植自网盘搜索助手）+ 抽奖字段修复

覆盖：
1. 账户卡片契约 {connected, user, points, details} 与网盘搜索助手一致。
2. 缓存三层：内存 TTL、刷新冷却、独立落盘快照 —— 语义正确。
3. _cached_account_status() 只读本地，**绝不发网络请求**。
4. _account_info(refresh) 的冷却拦截与命中缓存。
5. update_account_points 只改 points/details，不请求第三方。
6. _run_dian115 的 lottery 扁平化字段与 used_after 语义。
7. 加载级守护：全模块 compile + 无未导入 typing 名字。

运行：python tests/test_v183_account_card.py
"""
import ast
import re
import sys
import time
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


init_src = src_of("__init__.py")
cfg_src = src_of("ui/config.py")
checkin_src = src_of("handlers/checkin.py")

print("=" * 68)
print("1. 账户卡片契约（对齐网盘搜索助手 _search_account_card）")
print("=" * 68)

# 1-1 模块级缓存与冷却常量必须存在
check("1-1 定义了内存 TTL 常量",
      "_ACCOUNT_INFO_TTL_SECONDS" in init_src,
      "缺少 TTL 常量")
check("1-2 定义了刷新冷却常量",
      "_ACCOUNT_REFRESH_COOLDOWN_SECONDS" in init_src,
      "缺少刷新冷却常量")
check("1-3 定义了缓存锁",
      "_ACCOUNT_INFO_LOCK" in init_src,
      "缺少缓存锁")

# 1-2 卡片契约的关键字段
card_fn = method_source(init_src, "P115SubSearch", "_p115_account_card")
check("1-4 _p115_account_card 已定义", bool(card_fn), "缺失")
for field in ("connected", "user", "points", "details"):
    check(f"1-4-{field} 卡片含 {field} 字段",
          f'"{field}"' in card_fn or f"'{field}'" in card_fn,
          f"缺少 {field}")

dian_card_fn = method_source(init_src, "P115SubSearch", "_dian115_account_card")
check("1-5 _dian115_account_card 已定义", bool(dian_card_fn), "缺失")
check("1-6 癫影卡片含 points 契约",
      '"points"' in dian_card_fn or "'points'" in dian_card_fn,
      "癫影卡片缺少 points")
check("1-7 癫影卡片含 details 契约",
      '"details"' in dian_card_fn or "'details'" in dian_card_fn,
      "癫影卡片缺少 details")

# 1-3 账户键与网盘搜索助手同构（category:source）
check("1-8 账户键使用 category:source 形式",
      'ACCOUNT_KEY_P115 = "drive:p115"' in init_src
      and 'ACCOUNT_KEY_DIAN115 = "search:dian115"' in init_src,
      "账户键未对齐网盘搜索助手（应为 drive:p115 / search:dian115）")

normalize_fn = method_source(init_src, "P115SubSearch", "_normalize_account_key")
check("1-9 账户键校验 category ∈ {drive, search}",
      '{"drive", "search"}' in normalize_fn,
      "未校验 category，非法键会污染缓存")

print()
print("=" * 68)
print("2. 缓存三层：内存 TTL / 刷新冷却 / 独立落盘快照")
print("=" * 68)

check("2-1 定义了 _account_cache_get", "def _account_cache_get" in init_src, "缺失")
check("2-2 定义了 _account_cache_set", "def _account_cache_set" in init_src, "缺失")
check("2-3 定义了 _account_guard_active", "def _account_guard_active" in init_src, "缺失")
check("2-4 定义了 _account_guard_arm", "def _account_guard_arm" in init_src, "缺失")
check("2-5 定义了 clear_account_cache", "def clear_account_cache" in init_src, "缺失")

cache_get = method_source(init_src, "P115SubSearch", "_load_account_snapshot")
check("2-6 快照读取先查内存缓存",
      "_account_cache_get" in cache_get,
      "应优先命中内存 TTL 缓存")
check("2-7 快照读取回退到落盘数据",
      "get_data" in cache_get,
      "内存未命中时应读落盘快照")

cache_set = method_source(init_src, "P115SubSearch", "_save_account_snapshot")
check("2-8 快照写入同时更新内存与落盘",
      "_account_cache_set" in cache_set and "save_data" in cache_set,
      "快照写入应双写内存与落盘")
check("2-9 快照键带前缀避免污染其他配置",
      "ACCOUNT_SNAPSHOT_PREFIX" in cache_set,
      "快照应使用独立命名空间")

account_info = method_source(init_src, "P115SubSearch", "_account_info")
check("2-10 刷新前检查冷却守卫",
      "_account_guard_active" in account_info,
      "缺少冷却检查，连点刷新会打爆第三方")
check("2-11 冷却期内返回 limited=True",
      "True" in account_info and "guard_active" in account_info,
      "冷却拦截应标记 limited")
check("2-12 真正加载前先上锁冷却",
      "_account_guard_arm" in account_info,
      "应先置冷却再发请求，避免并发穿透")

# 2-13 真实行为验证：冷却与 TTL
ns = {}
ttl_code = f'''
import time, copy
{init_src.split("def _human_size")[0].split("lock = Lock()")[1]}
'''
# 直接抽出模块级缓存工具做行为测试（避免引入 app.* 依赖）
cache_block = re.search(
    r"_ACCOUNT_INFO_TTL_SECONDS.*?def clear_account_cache.*?(?=\n\n\ndef |\n\n\nclass )",
    init_src, re.S)
if cache_block:
    import copy as _copy
    import threading as _threading
    import typing as _typing
    ns = {
        "time": time,
        "copy": _copy,
        "RLock": _threading.RLock,
        "Dict": _typing.Dict,
        "Tuple": _typing.Tuple,
        "Any": _typing.Any,
        "Optional": _typing.Optional,
        "List": _typing.List,
        "logger": type("L", (), {
            "debug": staticmethod(lambda *a, **k: None),
            "warning": staticmethod(lambda *a, **k: None),
            "error": staticmethod(lambda *a, **k: None),
        })(),
    }
    exec(compile(cache_block.group(0), "<cache>", "exec"), ns)
    get_c = ns.get("_account_cache_get")
    set_c = ns.get("_account_cache_set")
    guard_active = ns.get("_account_guard_active")
    guard_arm = ns.get("_account_guard_arm")
    clear_c = ns.get("clear_account_cache")

    if all(callable(x) for x in (get_c, set_c, guard_active, guard_arm, clear_c)):
        clear_c()
        set_c("drive:p115", {"connected": True, "user": {"name": "tester"}})
        hit = get_c("drive:p115")
        check("2-13 缓存命中返回深拷贝",
              hit and hit["user"]["name"] == "tester",
              f"actual={hit}")
        hit["user"]["name"] = "mutated"
        check("2-14 深拷贝隔离（外部改动不污染缓存）",
              get_c("drive:p115")["user"]["name"] == "tester",
              "缓存应返回深拷贝")

        check("2-15 初始无冷却", guard_active("drive:p115") is False, "初始不应冷却")
        guard_arm("drive:p115")
        check("2-16 上锁后进入冷却", guard_active("drive:p115") is True, "冷却未生效")

        # TTL 过期验证：直接把缓存条目的过期时间改到过去
        # （_account_cache_get 读的是模块级全局 CACHE，不能改常量绕过）
        raw_cache = ns["_ACCOUNT_INFO_CACHE"]
        expire_at, payload = raw_cache["drive:p115"]
        raw_cache["drive:p115"] = (expire_at - ns["_ACCOUNT_INFO_TTL_SECONDS"] - 1, payload)
        check("2-17 TTL 过期后视为未命中",
              get_c("drive:p115") is None,
              "过期缓存应返回 None")
        clear_c()
        check("2-18 clear 后缓存与冷却清空",
              get_c("drive:p115") is None and guard_active("drive:p115") is False,
              "clear 未清干净")
    else:
        check("2-13~2-18 缓存工具可执行", False, "未能抽取缓存工具函数")
else:
    check("2-13~2-18 缓存工具可执行", False, "未能定位缓存工具代码块")

print()
print("=" * 68)
print("3. 配置页只读路径：绝不发网络请求")
print("=" * 68)

cached_fn = method_source(init_src, "P115SubSearch", "_cached_account_status")
check("3-1 _cached_account_status 已定义", bool(cached_fn), "缺失")

# 剥注释与字符串后检查
code_only = re.sub(r'""".*?"""', "", cached_fn, flags=re.S)
code_only = re.sub(r"'''.*?'''", "", code_only, flags=re.S)
code_only = "\n".join(line.split("#")[0] for line in code_only.splitlines())
code_only = re.sub(r'"[^"]*"', '""', code_only)
code_only = re.sub(r"'[^']*'", "''", code_only)

for idx, (token, why) in enumerate([
    ("get_account_info", "会发 HTTP 请求"),
    ("_account_info(", "会发 HTTP 请求"),
    ("_load_account(", "会发 HTTP 请求"),
    ("requests.", "会发 HTTP 请求"),
    ("turnstile", "会启动浏览器"),
], start=2):
    check(f"3-{idx} 未实际调用 {token}（{why}）",
          token not in code_only,
          f"❌ 配置页路径不得{why}")

check("3-7 只走本地快照读取",
      "_load_account_snapshot" in cached_fn,
      "配置页应只读本地快照")

collect_fn = method_source(init_src, "P115SubSearch", "_collect_account_status")
check("3-8 _collect_account_status 有异常兜底",
      "except Exception" in collect_fn,
      "状态采集必须兜底")
check("3-9 _collect_account_status 委托给本地快照路径",
      "_cached_account_status" in collect_fn,
      "应委托 _cached_account_status")

print()
print("=" * 68)
print("4. 签到结果回写快照")
print("=" * 68)

update_fn = method_source(init_src, "P115SubSearch", "update_account_points")
check("4-1 update_account_points 已定义", bool(update_fn), "缺失")
check("4-2 只改 points.available",
      'point_info["available"]' in update_fn,
      "应只更新积分数值")
check("4-3 兼容「累计签到 / 连续签到」明细",
      '"累计签到", "连续签到"' in update_fn,
      "应更新签到天数明细")
check("4-4 不触发网络请求",
      "get_account_info" not in update_fn and "requests" not in update_fn,
      "回写快照不得发请求")

sync_fn = method_source(init_src, "P115SubSearch", "_sync_account_snapshot_after_checkin")
check("4-5 签到后回写钩子已定义", bool(sync_fn), "缺失")
check("4-6 只回写成功的记录",
      'record.get("success")' in sync_fn,
      "失败记录不应回写")
check("4-7 已挂到 run_checkin",
      "_sync_account_snapshot_after_checkin" in
      method_source(init_src, "P115SubSearch", "run_checkin"),
      "签到完成未回写快照")

print()
print("=" * 68)
print("5. /refresh_account 路由")
print("=" * 68)

api_fn = method_source(init_src, "P115SubSearch", "get_api")
check("5-1 get_api 暴露 /refresh_account",
      '"/refresh_account"' in api_fn,
      "缺少刷新路由")
check("5-2 使用 POST 方法",
      '"POST"' in api_fn and "/refresh_account" in api_fn,
      "刷新应走 POST")
check("5-3 endpoint 指向 api_refresh_account",
      "api_refresh_account" in api_fn,
      "endpoint 未绑定")

refresh_api = method_source(init_src, "P115SubSearch", "api_refresh_account")
check("5-4 api_refresh_account 已定义", bool(refresh_api), "缺失")
# v1.8.4：鉴权移交 MoviePilot 统一 verify_apikey 依赖，handler 不再有 apikey 参数
# （AST 检查参数列表，避免 docstring 里的字样造成假阳性）
import ast as _ast
_params = None
for _node in _ast.walk(_ast.parse(init_src)):
    if isinstance(_node, _ast.ClassDef) and _node.name == "P115SubSearch":
        for _item in _node.body:
            if isinstance(_item, _ast.FunctionDef) and _item.name == "api_refresh_account":
                _params = {a.arg for a in _item.args.args} | {a.arg for a in _item.args.kwonlyargs}
check("5-5 鉴权移交框架（无 apikey 必填参数）",
      _params is not None and "apikey" not in _params,
      "handler 不应再有 apikey 参数")
check("5-5b 缺 key 时明确报错",
      "缺少账户卡片标识" in refresh_api,
      "key 参数缺失时应返回明确错误")
check("5-6 返回 limited 标记",
      '"limited"' in refresh_api,
      "应返回是否被冷却限制")
check("5-7 返回 key/account/data 结构",
      '"data"' in refresh_api and '"account"' in refresh_api,
      "返回结构与网盘搜索助手不一致")

print()
print("=" * 68)
print("6. 抽奖记录字段修复（对齐网盘搜索助手 _build_record）")
print("=" * 68)

run_dian = method_source(checkin_src, "CheckinHandler", "_run_dian115")
check("6-1 _run_dian115 已定义", bool(run_dian), "缺失")
for field in (
        "lottery_target_count", "lottery_executed", "lottery_used_after",
        "lottery_cost_points", "lottery_award_points", "lottery_vip_days"):
    check(f"6-2 记录含 {field}",
          f'"{field}"' in run_dian,
          f"缺少扁平字段 {field}")

check("6-3 进度取 used_after（当日累计）而非 executed",
      'lottery.get("used_after")' in run_dian,
      "❌ executed 是本次增量，作进度显示会误导")
check("6-4 进度文案标注「当日」",
      "当日" in run_dian,
      "文案应说明是当日累计口径")
check("6-5 不再使用旧的嵌套 lottery 结构",
      '"lottery": {' not in run_dian,
      "应改为扁平字段，与网盘搜索助手一致")

print()
print("=" * 68)
print("7. 加载级守护")
print("=" * 68)

# 7-1 全模块 compile
py_files = [
    p for p in PLUGIN.rglob("*.py")
    if "__pycache__" not in p.parts
]
compile_fail = []
for p in py_files:
    try:
        compile(p.read_text(encoding="utf-8"), str(p), "exec")
    except SyntaxError as e:
        compile_fail.append(f"{p.relative_to(PLUGIN)}:{e.lineno} {e.msg}")
check(f"7-1 全部 {len(py_files)} 个模块编译通过",
      not compile_fail,
      "; ".join(compile_fail[:5]))

# 7-2 用了未导入的 typing 名字（v1.5.12 曾因此插件加载失败）
TYPING_NAMES = {
    "Any", "Dict", "List", "Optional", "Tuple", "Set", "Callable",
    "Iterable", "Sequence", "Mapping", "Union", "Type", "Iterator",
}
typing_fail = []
for rel in ["__init__.py", "ui/config.py", "handlers/checkin.py"]:
    text = src_of(rel)
    tree = ast.parse(text)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for a in node.names:
                imported.add(a.asname or a.name)
        elif isinstance(node, ast.Import):
            for a in node.names:
                imported.add((a.asname or a.name).split(".")[0])
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in TYPING_NAMES:
            if node.id not in imported:
                typing_fail.append(f"{rel}: 使用了 {node.id} 但未导入")
check("7-2 无未导入的 typing 名字",
      not typing_fail,
      "; ".join(sorted(set(typing_fail))[:5]))

# 7-3 缓存使用的新导入可用
check("7-3 已导入 copy（深拷贝隔离）",
      re.search(r"^import copy$", init_src, re.M) is not None,
      "缺少 copy 导入")
check("7-4 已导入 RLock",
      "RLock" in init_src.split("\n\n")[0] or "from threading import" in init_src,
      "缺少 RLock 导入")

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
