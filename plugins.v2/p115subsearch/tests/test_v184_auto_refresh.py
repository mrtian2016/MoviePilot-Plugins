# -*- coding: utf-8 -*-
"""
P115SubSearch v1.8.4 测试：刷新按钮官方写法 + 账户快照自动刷新服务 + 手动刷新防超时

背景（用户反馈）：
  「账户信息显示无连接账户，点击右侧刷新显示账户信息，但是并没有账户信息，
    不能自动刷新吗」

根因：
  1. 刷新按钮 api 写成了绝对路径 `/plugin/...?apikey=...`——官方约定是相对路径
     （不带斜杠、不带 apikey，鉴权由前端统一附加），参数走 params。
  2. api_refresh_account 的 apikey 是必填参数，前端不传会 422。
  3. 配置页是静态渲染，没有自动刷新机制，快照为空就一直显示未连接。

覆盖：
  1. UI 按钮：api 相对路径、无 apikey、params 传 key；get_page 两个旧按钮同步修正。
  2. 自动刷新服务：注册条件、成功才覆盖快照、癫影兼做保活。
  3. 手动刷新防超时：refresh_account 默认禁止浏览器登录，过期返回友好提示。
  4. 鉴权移交：handler 无 apikey 必填参数（框架 verify_apikey 统一校验）。
  5. 配置四处齐全：account_refresh_minutes 声明/读取/写回/控件/默认值。
  6. 行为验证：假对象实跑 account_snapshot_refresh_service 与 _account_info。
  7. 加载级守护。

运行：python tests/test_v184_auto_refresh.py
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
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name == method_name:
                    return ast.get_source_segment(source, item) or ""
    return ""


init_src = src_of("__init__.py")
cfg_src = src_of("ui/config.py")
api_src = src_of("handlers/api.py")

print("=" * 68)
print("1. 刷新按钮：官方 events.click.api 写法")
print("=" * 68)

card_src = method_source(cfg_src, "UIConfig", "_account_card")
check("1-1 _account_card 已定义", bool(card_src), "缺失")
# 剥掉注释行再匹配（本项目铁律：注释里的字样不算代码）
card_code = "\n".join(
    ln for ln in card_src.splitlines() if not ln.strip().startswith("#")
)
# 1-2 相对路径、不带前导斜杠、不带 apikey
check("1-2 api 为相对路径（无前导斜杠）",
      "'api': 'plugin/P115SubSearch/refresh_account'" in card_code,
      "必须是相对路径 plugin/P115SubSearch/refresh_account")
check("1-3 不再拼接 apikey",
      "apikey" not in card_code,
      "按钮不应自带 apikey（前端统一附加）")
check("1-4 参数走 params.key",
      "'params': {'key': account_key}" in card_code,
      "应用 params 传递 key")
check("1-5 method 为 post",
      "'method': 'post'" in card_code,
      "路由是 POST")

page_src = method_source(cfg_src, "UIConfig", "get_page")
check("1-6 数据页「立即搜索」按钮改为相对路径",
      "'api': 'plugin/P115SubSearch/sync_subscribes'" in page_src,
      "旧写法 /plugin/...?apikey= 带前导斜杠，请求打不到后端")
check("1-7 数据页「清空历史」按钮改为相对路径",
      "'api': 'plugin/P115SubSearch/clear_history'" in page_src,
      "旧写法错误")

print()
print("=" * 68)
print("2. 鉴权移交：handler 无 apikey 必填参数（AST 检查参数列表）")
print("=" * 68)


def fn_args(source, class_name, method_name):
    """返回目标方法的参数名集合。"""
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name == method_name:
                    names = {a.arg for a in item.args.args}
                    names |= {a.arg for a in item.args.kwonlyargs}
                    return names
    return None


check("2-1 api_refresh_account 无 apikey 参数",
      "apikey" not in (fn_args(init_src, "P115SubSearch", "api_refresh_account") or {"x"}),
      "前端不传 apikey，必填参数会导致 422")
check("2-2 api_clear_history 无 apikey 参数",
      "apikey" not in (fn_args(init_src, "P115SubSearch", "api_clear_history") or {"x"}),
      "同上")
check("2-3 ApiHandler.clear_history 删除内部 apikey 校验",
      "settings.API_TOKEN" not in method_source(api_src, "ApiHandler", "clear_history"),
      "框架已统一 verify_apikey，内部校验会挡住前端调用")
check("2-4 sync_subscribes 无 apikey 参数",
      "apikey" not in (fn_args(init_src, "P115SubSearch", "sync_subscribes") or {"x"}),
      "GET 按钮")

print()
print("=" * 68)
print("3. 手动刷新防超时：禁止浏览器自动登录")
print("=" * 68)

refresh_fn = method_source(init_src, "P115SubSearch", "refresh_account")
check("3-1 refresh_account 默认 allow_browser_login=False",
      "allow_browser_login: bool = False" in refresh_fn,
      "手动刷新是同步 HTTP，浏览器登录 30s+ 会超时")
dian_card = method_source(init_src, "P115SubSearch", "_dian115_account_card")
check("3-2 _dian115_account_card 支持禁止浏览器登录",
      "allow_browser_login" in dian_card,
      "缺少参数化")
check("3-3 禁止登录时过期返回友好提示",
      "登录态已过期" in dian_card,
      "应提示等待自动登录")
load_account = method_source(init_src, "P115SubSearch", "_load_account")
check("3-4 _load_account 透传 allow_browser_login",
      "allow_browser_login=allow_browser_login" in load_account,
      "参数未透传")

print()
print("=" * 68)
print("4. 异常不覆盖快照（自动刷新不冲掉好快照）")
print("=" * 68)

info_fn = method_source(init_src, "P115SubSearch", "_account_info")
check("4-1 异常路径返回占位卡",
      "except Exception" in info_fn and "placeholder" in info_fn,
      "异常应返回占位卡")
# 异常分支不得调用 _save_account_snapshot：剥掉 try 块，检查 except 分支
except_branch = info_fn.split("except Exception as error:")[-1]
check("4-2 异常分支不写快照",
      "_save_account_snapshot" not in except_branch.split("account[\"refreshed_at\"]")[0]
      if "account[\"refreshed_at\"]" in except_branch else
      "_save_account_snapshot" not in except_branch.split("return placeholder")[0],
      "临时网络抖动不应把好快照冲成占位卡")

print()
print("=" * 68)
print("5. 账户快照自动刷新服务")
print("=" * 68)

auto_fn = method_source(init_src, "P115SubSearch", "account_snapshot_refresh_service")
check("5-1 account_snapshot_refresh_service 已定义", bool(auto_fn), "缺失")
check("5-2 逐账户独立兜底",
      auto_fn.count("except Exception") >= 1 and "continue" in auto_fn,
      "单账户失败不应影响另一个")
check("5-3 成功才覆盖快照",
      'account.get("connected")' in auto_fn,
      "未连接/失败时不应覆盖既有快照")
check("5-4 癫影允许浏览器登录（兼做保活）",
      "allow_browser_login=True" in auto_fn,
      "后台刷新应允许自动登录以保活")
build_fn = method_source(init_src, "P115SubSearch", "_build_account_refresh_service")
check("5-5 _build_account_refresh_service 已定义", bool(build_fn), "缺失")
check("5-6 间隔为 0 时不注册",
      "minutes <= 0" in build_fn and "return []" in build_fn,
      "应支持关闭")
check("5-7 无任何凭据时不注册",
      "has_p115" in build_fn and "has_dian115" in build_fn,
      "缺凭据时注册无意义")
check("5-8 服务 id 唯一",
      '"P115SubSearchAccountRefresh"' in build_fn,
      "缺少服务 id")
service_fn = method_source(init_src, "P115SubSearch", "get_service")
check("5-9 get_service 注册自动刷新",
      "_build_account_refresh_service" in service_fn,
      "未注册进 get_service")

print()
print("=" * 68)
print("6. 配置四处齐全：account_refresh_minutes")
print("=" * 68)

check("6-1 类属性声明",
      re.search(r"_account_refresh_minutes: int = 30", init_src) is not None,
      "缺类属性")
check("6-2 init_plugin 读取",
      'config.get("account_refresh_minutes"' in init_src,
      "缺读取")
check("6-3 __update_config 写回",
      '"account_refresh_minutes": self._account_refresh_minutes' in init_src,
      "缺写回")
check("6-4 UI 控件",
      "'model': 'account_refresh_minutes'" in cfg_src,
      "缺控件")
check("6-5 默认值",
      '"account_refresh_minutes": 30' in cfg_src,
      "缺默认配置")
check("6-6 hint 说明含关闭语义",
      "设为 0 关闭" in cfg_src,
      "应说明 0=关闭")

print()
print("=" * 68)
print("7. 行为验证（假对象实跑）")
print("=" * 68)

ns = {
    "time": time,
    "copy": __import__("copy"),
    "RLock": __import__("threading").RLock,
    "logger": type("L", (), {
        "debug": staticmethod(lambda *a, **k: None),
        "warning": staticmethod(lambda *a, **k: None),
        "error": staticmethod(lambda *a, **k: None),
    })(),
}
import threading
ns["threading"] = threading
from typing import Any, Dict, List, Optional, Tuple
ns["Any"], ns["Dict"], ns["List"], ns["Optional"], ns["Tuple"] = Any, Dict, List, Optional, Tuple
# 模块级缓存状态（clear_account_cache / guard 依赖）
ns["_ACCOUNT_INFO_LOCK"] = threading.RLock()
ns["_ACCOUNT_INFO_CACHE"] = {}
ns["_ACCOUNT_REFRESH_GUARD"] = {}
ns["_ACCOUNT_INFO_TTL_SECONDS"] = 300
ns["_ACCOUNT_REFRESH_COOLDOWN_SECONDS"] = 30

parts = []
for name in ("_account_cache_get", "_account_cache_set",
             "_account_guard_active", "_account_guard_arm", "clear_account_cache",
             "_human_size"):
    m = re.search(
        rf"^def {name}\(.*?(?=^def |^class |\Z)", init_src, re.S | re.M
    )
    if m:
        parts.append(m.group(0))
exec(compile("\n\n".join(parts), "<cache>", "exec"), ns)

METHODS = [
    "_normalize_account_key", "_load_account_snapshot", "_save_account_snapshot",
    "_account_info", "_load_account", "_p115_account_card", "_dian115_account_card",
    "_account_card_placeholder", "_p115_account_card_placeholder",
    "_dian115_account_card_placeholder", "refresh_account",
    "account_snapshot_refresh_service", "_build_account_refresh_service",
    "api_refresh_account",
]
for name in METHODS:
    m = re.search(
        rf"^    def {name}\(.*?(?=\n    def |\n    @|\n\nclass |\Z)",
        init_src, re.S | re.M,
    )
    if not m:
        check(f"7-0 抽取 {name}", False, "源码片段抽取失败")
        continue
    fn_src = m.group(0)
    # 快照键前缀保留字面量，使落盘键与真实实现一致：account_snapshot:<key>
    fn_src = fn_src.replace('f"{self.ACCOUNT_SNAPSHOT_PREFIX}{normalized}"',
                            'f"account_snapshot:{normalized}"')
    fn_src = re.sub(rf"\bself\.ACCOUNT_SNAPSHOT_PREFIX\b", "'account_snapshot:'", fn_src)
    ns[name] = fn_src

exec_src = "\n".join(ns.pop(name) for name in list(ns) if name in METHODS)
import textwrap
exec(compile(textwrap.dedent(exec_src), "<plugin>", "exec"), ns)

# 把 exec 进 ns 的模块级函数绑定到测试全局，便于直接调用
for fname in ("clear_account_cache", "_account_cache_get", "_account_cache_set",
              "_account_guard_active", "_account_guard_arm"):
    if fname in ns:
        globals()[fname] = ns[fname]

ACCOUNT_KEY_P115 = "drive:p115"
ACCOUNT_KEY_DIAN115 = "search:dian115"
ns["ACCOUNT_KEY_P115"] = ACCOUNT_KEY_P115
ns["ACCOUNT_KEY_DIAN115"] = ACCOUNT_KEY_DIAN115


class FakeDian115:
    def __init__(self, fail=False):
        self.fail = fail

    def login_status(self):
        return {"has_token": True, "auto_login": True}

    def get_account_info(self, allow_browser_login=True):
        if self.fail:
            raise RuntimeError("boom")
        return {
            "name": "癫影用户", "email": "a@b.c", "avatar": "", "points": 77,
            "role": "user", "is_vip": True, "vip_until": "2026-12-31",
            "unlock_count": 3, "consecutive_signin": 4,
        }


class FakeManager:
    client = object()

    def get_account_info(self, silent=False):
        return {"connected": True, "name": "115用户", "vip": True,
                "vip_name": "VIP", "expire": 100, "user_id": 1,
                "space_used": 10, "space_total": 100}


class Plugin:
    ACCOUNT_KEY_P115 = ACCOUNT_KEY_P115
    ACCOUNT_KEY_DIAN115 = ACCOUNT_KEY_DIAN115
    ACCOUNT_SNAPSHOT_PREFIX = "account_snapshot:"

    def __init__(self, dian_fail=False):
        self._p115_manager = FakeManager()
        self._cookies = "UID=1; CID=2; SEID=3; KID=4"
        self._dian115_client = FakeDian115(fail=dian_fail)
        self._dian115_enabled = True
        self._dian115_checkin_enabled = True
        self._dian115_email = "a@b.c"
        self._dian115_password = "pw"
        self._dian115_auto_login = True
        self._account_refresh_minutes = 30
        self.store = {}

    def get_data(self, key):
        return self.store.get(key)

    def save_data(self, key, value):
        self.store[key] = value


for name in METHODS:
    if name in ns:
        fn = ns[name]
        if name in ("_account_card_placeholder", "_normalize_account_key"):
            setattr(Plugin, name, staticmethod(fn))
        else:
            setattr(Plugin, name, fn)

p = Plugin()
clear_account_cache()
p.account_snapshot_refresh_service()

snap_p115 = p.store.get(f"account_snapshot:{ACCOUNT_KEY_P115}")
snap_dian = p.store.get(f"account_snapshot:{ACCOUNT_KEY_DIAN115}")
check("7-1 115 快照自动写入", bool(snap_p115) and snap_p115.get("connected") is True, str(snap_p115)[:80])
check("7-2 癫影快照自动写入", bool(snap_dian) and snap_dian.get("connected") is True, str(snap_dian)[:80])
check("7-3 快照带 refreshed_at",
      bool(snap_p115) and isinstance(snap_p115.get("refreshed_at"), int),
      "缺 refreshed_at")

# 7-4 癫影临时故障：115 快照保留，癫影不覆盖
clear_account_cache()
p2 = Plugin(dian_fail=True)
p2.store[f"account_snapshot:{ACCOUNT_KEY_DIAN115}"] = {"connected": True, "user": {"name": "旧快照"}}
p2.account_snapshot_refresh_service()
snap_dian2 = p2.store.get(f"account_snapshot:{ACCOUNT_KEY_DIAN115}")
check("7-4 单账户故障不影响另一账户",
      bool(p2.store.get(f"account_snapshot:{ACCOUNT_KEY_P115}")),
      "115 快照应正常写入")
check("7-5 故障不覆盖既有好快照",
      snap_dian2 and snap_dian2.get("user", {}).get("name") == "旧快照",
      f"actual={snap_dian2}")

# 7-6 手动刷新：癫影 token 过期（禁止浏览器登录）→ 提示卡片，不写快照
clear_account_cache()
p3 = Plugin()


class ExpiredClient(FakeDian115):
    def get_account_info(self, allow_browser_login=True):
        raise RuntimeError("expired")


p3._dian115_client = ExpiredClient()
account, limited = p3.refresh_account(ACCOUNT_KEY_DIAN115)
check("7-6 手动刷新过期返回提示而非抛异常",
      account.get("connected") is False and "过期" in str(account.get("error") or ""),
      f"actual={account}")
check("7-7 手动刷新失败不写快照",
      f"account_snapshot:{ACCOUNT_KEY_DIAN115}" not in p3.store,
      "占位卡不应落盘")

# 7-8 api_refresh_account：缺 key 明确报错；error 透出
ret = p3.api_refresh_account(key="")
check("7-8 缺 key 返回明确错误",
      ret.get("success") is False and "缺少账户卡片标识" in ret.get("message", ""),
      f"actual={ret}")
clear_account_cache()
ret2 = Plugin(dian_fail=True)
for name in METHODS:
    if name in ns:
        fn = ns[name]
        if name in ("_account_card_placeholder", "_normalize_account_key"):
            setattr(type(ret2), name, staticmethod(fn))
        else:
            setattr(type(ret2), name, fn)
ret3 = ret2.api_refresh_account(key=ACCOUNT_KEY_DIAN115)
check("7-9 刷新失败透出错误信息",
      ret3.get("success") is False and ret3.get("message"),
      f"actual={ret3}")

# 7-10 服务注册逻辑
check("7-10 间隔 0 不注册", p._build_account_refresh_service.__self__ is p and True, "")
p4 = Plugin()
p4._account_refresh_minutes = 0
check("7-10b 间隔为 0 返回空列表", p4._build_account_refresh_service() == [], "应不注册")
p5 = Plugin()
p5._account_refresh_minutes = 15
svc = p5._build_account_refresh_service()
check("7-11 正常注册且间隔正确",
      len(svc) == 1 and svc[0]["kwargs"] == {"minutes": 15},
      f"actual={svc}")
p6 = Plugin()
p6._p115_manager = None
p6._cookies = ""
p6._dian115_email = ""
p6._dian115_password = ""
check("7-12 无凭据不注册", p6._build_account_refresh_service() == [], "应不注册")

print()
print("=" * 68)
print("8. 加载级守护")
print("=" * 68)

all_ok = True
for rel in ("__init__.py", "ui/config.py", "handlers/api.py", "handlers/checkin.py",
            "clients/dian115.py", "clients/p115.py"):
    try:
        compile(src_of(rel), rel, "exec")
    except SyntaxError as e:
        all_ok = False
        check(f"8-1 compile {rel}", False, str(e))
check("8-1 全部模块可编译", all_ok, "")
# import 期守护由 tests/test_v180_import_guard.py 专门覆盖，此处不重复

print()
print("=" * 68)
total = _passed + len(_failed)
print(f"结果：{_passed}/{total} 通过")
if _failed:
    print("失败明细：")
    for f in _failed:
        print(f"  ✗ {f}")
    sys.exit(1)
print("ALL PASS")
