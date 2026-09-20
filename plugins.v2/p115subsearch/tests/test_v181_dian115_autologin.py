# -*- coding: utf-8 -*-
"""
P115SubSearch v1.8.1：癫影（Dian115）自动登录专项测试。

覆盖目标
--------
1. **加载级守护**：新模块可导入、相对导入链路完好（v1.5.12 血泪教训：
   「AST 抽方法 + 假类 exec」会绕过 import 期错误）。
2. **Turnstile 解算器契约**：超时、环境缺失、失败码、交互点击四条路径。
3. **回退语义**：cloakbrowser 缺失时必须抛 ``browser_unavailable`` 且
   **不吞异常、不静默成功**，由上层回退手工 Token。
4. **客户端集成**：
   * 手工 Token 的 JWT ``exp`` 解析；
   * 已过期的手工 Token 必须被忽略（否则会带着死 Cookie 打接口）；
   * ``auto_login=False`` 时必须抛 ``turnstile_required`` 而非硬闯；
   * ``allow_browser=False``（签到链路）必须抛 ``browser_login_forbidden``。
5. **配置链路**：新增配置项在 UI 表单、默认值、插件读写三处一致。

不使用 pytest（与项目既有脚本式测试一致），直接 python 执行。
"""
import ast
import base64
import importlib.util
import json
import pathlib
import sys
import types
import time

PLUGIN = pathlib.Path(__file__).resolve().parent.parent

FAILURES = []


def check(name, cond, extra=""):
    if cond:
        print(f"[PASS] {name}")
    else:
        print(f"[FAIL] {name} {extra}")
        FAILURES.append(name)


# ==================== 桩：app.log / app.core.config ====================

def _install_stubs():
    if "app" not in sys.modules:
        app = types.ModuleType("app")
        log = types.ModuleType("app.log")

        class _L:
            def info(self, *a, **k):
                pass

            warning = error = debug = info

        log.logger = _L()
        app.log = log
        sys.modules.update({"app": app, "app.log": log})

    if "app.core" not in sys.modules:
        core = types.ModuleType("app.core")
        core_config = types.ModuleType("app.core.config")

        class _Settings:
            CLOAKBROWSER_HUMANIZE = True
            CLOAKBROWSER_HUMAN_PRESET = "default"

        core_config.settings = _Settings()
        core.config = core_config
        sys.modules.update({"app.core": core, "app.core.config": core_config})


_install_stubs()


def _ensure_package_chain(package):
    import importlib.machinery
    parts = package.split(".")
    for index in range(1, len(parts) + 1):
        name = ".".join(parts[:index])
        if name in sys.modules:
            continue
        mod = types.ModuleType(name)
        mod.__path__ = [str(PLUGIN / pathlib.Path(*parts[:index]))]
        spec = importlib.machinery.ModuleSpec(name, loader=None, is_package=True)
        spec.submodule_search_locations = mod.__path__
        mod.__spec__ = spec
        sys.modules[name] = mod


def load(rel, qualified):
    """按真实包语义加载插件内模块（相对导入可解析）。"""
    _ensure_package_chain(qualified.rsplit(".", 1)[0])
    path = PLUGIN / rel
    spec = importlib.util.spec_from_file_location(qualified, str(path))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[qualified] = mod
    spec.loader.exec_module(mod)
    return mod


PKG = "_v181_probe.clients"

# ==================== 1. 模块可加载（含相对导入链路） ====================

try:
    turnstile_mod = load("clients/dian115_turnstile.py", f"{PKG}.dian115_turnstile")
    check("1-1 dian115_turnstile 可加载", hasattr(turnstile_mod, "Dian115Turnstile"))
except Exception as e:  # noqa: BLE001
    check("1-1 dian115_turnstile 可加载", False, f"{e.__class__.__name__}: {e}")
    turnstile_mod = None

try:
    dian_mod = load("clients/dian115.py", f"{PKG}.dian115")
    check("1-2 dian115 可加载（相对导入成功）", hasattr(dian_mod, "Dian115Client"),
          "相对导入 from .dian115_turnstile 必须能解析")
except Exception as e:  # noqa: BLE001
    check("1-2 dian115 可加载（相对导入成功）", False, f"{e.__class__.__name__}: {e}")
    dian_mod = None

if dian_mod is None:  # 后续断言依赖该模块，提前退出
    print("=" * 60)
    print(f"结果：{len(FAILURES)} 项失败（模块不可加载，跳过后续）")
    sys.exit(1)


Dian115Client = dian_mod.Dian115Client
Dian115Error = dian_mod.Dian115Error

# ==================== 2. JWT exp 解析 ====================


def make_jwt(exp=0, uid=1):
    def b64(obj):
        raw = json.dumps(obj, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    return ".".join([
        b64({"alg": "HS256", "typ": "JWT"}),
        b64({"uid": uid, "sub": f"u_{uid}", "role": "vip", "exp": exp}),
        "sig",
    ])


future = int(time.time()) + 3600
past = int(time.time()) - 3600

check("2-1 解析未来 exp", abs(dian_mod._jwt_expires_at(make_jwt(future)) - future) < 1)
check("2-2 解析过期 exp", abs(dian_mod._jwt_expires_at(make_jwt(past)) - past) < 1)
check("2-3 非法输入返回 0", dian_mod._jwt_expires_at("not-a-jwt") == 0.0)
check("2-4 空输入返回 0", dian_mod._jwt_expires_at("") == 0.0)
check("2-5 exp 缺失返回 0", dian_mod._jwt_expires_at(make_jwt(exp=0)) == 0.0)

# ==================== 3. Turnstile 解算器：环境缺失时必须显式失败 ====================

if turnstile_mod is not None:
    TS = turnstile_mod.Dian115Turnstile
    Unavailable = turnstile_mod.Dian115TurnstileUnavailable

    # 3-1 参数校验在提交线程前完成，不依赖浏览器
    solver = TS("https://m.dian115.com", None)
    try:
        solver.token("", "portal_login")
        check("3-1 空 site_key 抛 ValueError", False)
    except ValueError:
        check("3-1 空 site_key 抛 ValueError", True)
    except Exception as e:  # noqa: BLE001
        check("3-1 空 site_key 抛 ValueError", False, f"实际 {e.__class__.__name__}")

    try:
        solver.token("0xKEY", "portal_evil")
        check("3-2 非法 action 抛 ValueError", False)
    except ValueError:
        check("3-2 非法 action 抛 ValueError", True)
    except Exception as e:  # noqa: BLE001
        check("3-2 非法 action 抛 ValueError", False, f"实际 {e.__class__.__name__}")

    # 3-3 _load_launch_context 在无 cloakbrowser 时抛专用异常
    import importlib.util as _ilu
    real_find = _ilu.find_spec

    def _block_cloak(name, *a, **k):
        if name.split(".")[0] == "cloakbrowser":
            raise ModuleNotFoundError("No module named 'cloakbrowser'")
        return real_find(name, *a, **k)

    _ilu.find_spec = _block_cloak
    sys.modules.pop("cloakbrowser", None)
    try:
        TS._load_launch_context()
        check("3-3 缺 cloakbrowser 抛专用异常", False)
    except Unavailable:
        check("3-3 缺 cloakbrowser 抛专用异常", True)
    except Exception as e:  # noqa: BLE001
        check("3-3 缺 cloakbrowser 抛专用异常", False, f"实际 {e.__class__.__name__}")
    finally:
        _ilu.find_spec = real_find

    # 3-4 close() 幂等（未启动浏览器时也不应抛）
    try:
        s2 = TS("https://m.dian115.com", None)
        s2.close()
        s2.close()
        check("3-4 close 幂等且不抛", True)
    except Exception as e:  # noqa: BLE001
        check("3-4 close 幂等且不抛", False, f"{e.__class__.__name__}: {e}")

    # 3-5 解算器源码必须含关键 Turnstile 参数（防止被误删导致静默降级）
    ts_src = (PLUGIN / "clients" / "dian115_turnstile.py").read_text(encoding="utf-8")
    for key, desc in [
        ("interaction-only", "隐藏式 widget（免点击主路径）"),
        ("execution: 'execute'", "显式执行解算"),
        ("'response-field': false", "不注入隐藏表单域"),
        ("before-interactive-callback", "交互回调（点击分支触发点）"),
        ("challenges.cloudflare.com", "交互框 host 匹配"),
        ("context.close()", "异常后销毁上下文"),
    ]:
        check(f"3-5 Turnstile 关键参数保留：{desc}", key in ts_src)
else:
    check("3-x 解算器模块可用", False)

# ==================== 4. 客户端集成：回退与拒绝语义 ====================


def make_client(**kw):
    """构造不触发网络的客户端；get_data/save_data 用内存字典桩。"""
    store = {}
    kw.setdefault("email", "wukangxxx@126.com")
    kw.setdefault("password", "dummy-pass")
    kw.setdefault("auto_login", True)
    return Dian115Client(
        get_data_func=lambda k: store.get(k),
        save_data_func=lambda k, v: store.__setitem__(k, v),
        **kw
    ), store


# 4-1 手工 Token 优先，且必须能挂到会话 Cookie 上
c, _ = make_client(token=make_jwt(future))
check("4-1 手工有效 Token 被采纳", c._authenticated and c._cookie("__Host-portal_token") != "")

# 4-2 手工 Token 已过期 → 必须被忽略（否则会带着死 Cookie 打接口）
c2, _ = make_client(token=make_jwt(past))
check("4-2 过期手工 Token 被忽略", not c2._authenticated and c2._explicit_token != "")

# 4-3 上次会话持久化的 Token 续用
store = {}
c3 = Dian115Client(
    email="wukangxxx@126.com", password="dummy-pass", auto_login=True,
    get_data_func=lambda k: store.get(k),
    save_data_func=lambda k, v: store.__setitem__(k, v),
)
store[dian_mod.SESSION_DATA_KEY] = {
    "email": "wukangxxx@126.com",
    "base_url": Dian115Client.BASE_URL,
    "token": make_jwt(future),
    "updated_at": int(time.time()),
}
c3._restore_auth_cookie()
check("4-3 持久化 Token 被续用", c3._authenticated)

# 4-4 持久化 Token 过期 → 忽略
store[dian_mod.SESSION_DATA_KEY]["token"] = make_jwt(past)
c4 = Dian115Client(
    email="wukangxxx@126.com", password="dummy-pass", auto_login=True,
    get_data_func=lambda k: store.get(k),
    save_data_func=lambda k, v: store.__setitem__(k, v),
)
c4._restore_auth_cookie()
check("4-4 过期持久化 Token 被忽略", not c4._authenticated)

# 4-5 auto_login=False → turnstile_required（不是硬闯、不是 browser_unavailable）
c5, _ = make_client(auto_login=False)
c5._turnstile_policy = ({"turnstile_enabled": True,
                         "turnstile_site_key": "0x4AAA"}, time.monotonic() + 300)
try:
    c5._turnstile_token("portal_login", True)
    check("4-5 auto_login=False 抛 turnstile_required", False)
except Dian115Error as e:
    check("4-5 auto_login=False 抛 turnstile_required", e.code == "turnstile_required",
          f"实际 code={e.code}")
except Exception as e:  # noqa: BLE001
    check("4-5 auto_login=False 抛 turnstile_required", False, f"实际 {e.__class__.__name__}")

# 4-6 allow_browser=False（签到链路）→ browser_login_forbidden
c6, _ = make_client()
c6._turnstile_policy = ({"turnstile_enabled": True,
                         "turnstile_site_key": "0x4AAA"}, time.monotonic() + 300)
try:
    c6._turnstile_token("portal_login", False)
    check("4-6 allow_browser=False 抛 browser_login_forbidden", False)
except Dian115Error as e:
    check("4-6 allow_browser=False 抛 browser_login_forbidden",
          e.code == "browser_login_forbidden", f"实际 code={e.code}")
except Exception as e:  # noqa: BLE001
    check("4-6 allow_browser=False 抛 browser_login_forbidden", False,
          f"实际 {e.__class__.__name__}")

# 4-7 站点关闭验证 → 返回 None（走纯净登录，不启动浏览器）
c7, _ = make_client()
c7._turnstile_policy = ({"turnstile_enabled": False}, time.monotonic() + 300)
try:
    result = c7._turnstile_token("portal_login", True)
    check("4-7 站点关闭验证时返回 None", result is None)
except Exception as e:  # noqa: BLE001
    check("4-7 站点关闭验证时返回 None", False, f"{e.__class__.__name__}: {e}")

# 4-8 策略缺 site_key → schema_changed（不能拿空 key 去解算）
c8, _ = make_client()
c8._turnstile_policy = ({"turnstile_enabled": True}, time.monotonic() + 300)
try:
    c8._turnstile_token("portal_login", True)
    check("4-8 缺 site_key 抛 schema_changed", False)
except Dian115Error as e:
    check("4-8 缺 site_key 抛 schema_changed", e.code == "schema_changed",
          f"实际 code={e.code}")
except Exception as e:  # noqa: BLE001
    check("4-8 缺 site_key 抛 schema_changed", False, f"实际 {e.__class__.__name__}")

# 4-9 环境缺 cloakbrowser 且账号密码自动登录 → browser_unavailable（供上层回退）
c9, _ = make_client()
c9._turnstile_policy = ({"turnstile_enabled": True,
                         "turnstile_site_key": "0x4AAA"}, time.monotonic() + 300)
c9._turnstile_unavailable = "simulated-missing"
try:
    c9._turnstile_token("portal_login", True)
    check("4-9 环境缺失抛 browser_unavailable", False)
except Dian115Error as e:
    check("4-9 环境缺失抛 browser_unavailable", e.code == "browser_unavailable",
          f"实际 code={e.code}")
except Exception as e:  # noqa: BLE001
    check("4-9 环境缺失抛 browser_unavailable", False, f"实际 {e.__class__.__name__}")

# 4-10 login_status 不发网络请求且字段齐备
c10, _ = make_client(token=make_jwt(future))
try:
    st = c10.login_status()
    need = {"email", "has_password", "auto_login", "browser_available",
            "has_token", "token_remaining_hours"}
    check("4-10 login_status 字段齐备", need <= set(st), f"missing={need - set(st)}")
except Exception as e:  # noqa: BLE001
    check("4-10 login_status 字段齐备", False, f"{e.__class__.__name__}: {e}")

# 4-11 matches_config 必须把 auto_login 纳入比对（否则改开关后复用旧实例）
a, _ = make_client(auto_login=True)
check("4-11 matches_config 感知 auto_login",
      a.matches_config("wukangxxx@126.com", "dummy-pass", None, 1.0, True)
      and not a.matches_config("wukangxxx@126.com", "dummy-pass", None, 1.0, False))

# 4-12 _browser_proxy：字典代理必须抽出单串（playwright 不吃字典）
b, _ = make_client(proxy={"http": "http://h:1", "https": "http://s:2"})
check("4-12 _browser_proxy 从字典抽出 https", b._browser_proxy() == "http://s:2")
b2, _ = make_client(browser_proxy="http://browser:3")
check("4-13 独立 browser_proxy 优先", b2._browser_proxy() == "http://browser:3")
b3, _ = make_client()
check("4-14 无代理时返回 None", b3._browser_proxy() is None)

# ==================== 4b. Cookie 容器兼容性（真机踩坑回归） ====================
# 真机（MoviePilot 容器，curl_cffi 存在）实测报过：
#   Cookies.clear() got an unexpected keyword argument 'name'
# 本机没有 curl_cffi，因此注入一个「只支持 curl_cffi 那套 API」的假 Cookies
# 来钉死契约，避免将来又把 requests 的签名硬套上去。


class _CurlCffiLikeCookies:
    """模拟 curl_cffi.Cookies：clear 只收 domain/path，支持 _cookies 字典。"""

    def __init__(self):
        self._cookies = {}

    # --- 与真实实现一致：签名里没有 name ---
    def clear(self, domain=None, path=None):
        if domain is None and path is None:
            self._cookies.clear()
            return
        for key in list(self._cookies):
            c = self._cookies[key]
            if (domain is None or c.get("domain") == domain) and \
               (path is None or c.get("path") == path):
                del self._cookies[key]

    def set(self, name, value, domain="", path="/", secure=False, **kw):
        if value == "" or kw.get("expire") == 0:
            self._cookies.pop(name, None)
            return
        self._cookies[name] = {"value": value, "domain": domain,
                               "path": path, "secure": secure}

    def get(self, name, default=None):
        item = self._cookies.get(name)
        return item["value"] if item else default

    def get_dict(self):
        return {k: v["value"] for k, v in self._cookies.items()}

    def __contains__(self, name):
        return name in self._cookies

    def __delitem__(self, name):
        del self._cookies[name]


def _with_jar(jar):
    """把一个自定义 Cookie 容器塞进客户端会话。"""
    c, _ = make_client(token=make_jwt(future))
    c._session.cookies = jar
    c._authenticated = False
    c._saved_token = ""
    return c


check("4-15 _drop_cookie 不炸等价删除（requests 路径）", (lambda: (
    (lambda c: (c._drop_cookie("__Host-portal_token"), True)[1])(make_client()[0])
))())

# curl_cffi 风格容器：clear(name=...) 会 TypeError，_drop_cookie 必须内部吞掉
jar = _CurlCffiLikeCookies()
jar.set("__Host-portal_token", "stale-value")
c15 = _with_jar(jar)
try:
    c15._apply_token(make_jwt(future))
    ok15 = (c15._authenticated is True
            and c15._cookie("__Host-portal_token") != "stale-value"
            and c15._cookie("__Host-portal_token") != "")
except Exception as e:  # noqa: BLE001
    ok15 = False
    print(f"       异常：{e.__class__.__name__}: {e}")
check("4-16 curl_cffi 风格 Cookie 容器可写入 Token", ok15,
      "Cookies.clear(name=...) 抛 TypeError 时不得导致写入失败")

# 空容器上删除也必须安全（首次登录场景）
jar2 = _CurlCffiLikeCookies()
c16 = _with_jar(jar2)
try:
    c16._drop_cookie("__Host-portal_token")
    c16._apply_token(make_jwt(future))
    ok16 = c16._cookie("__Host-portal_token") != ""
except Exception as e:  # noqa: BLE001
    ok16 = False
    print(f"       异常：{e.__class__.__name__}: {e}")
check("4-17 空容器删除不抛且随后可写入", ok16)

# requests 路径同样要能写入（确保两边都覆盖）
c17, _ = make_client()
c17._apply_token(make_jwt(future))
check("4-18 requests 风格 Cookie 容器可写入 Token",
      c17._authenticated and c17._cookie("__Host-portal_token") != "")

# ==================== 5. 配置链路一致性 ====================

ui_src = (PLUGIN / "ui" / "config.py").read_text(encoding="utf-8")
init_src = (PLUGIN / "__init__.py").read_text(encoding="utf-8")

for key in ("dian115_auto_login", "dian115_browser_proxy", "dian115_login_cron"):
    check(f"5-x UI 表单引用 {key}", f"'{key}'" in ui_src)
    check(f"5-x 默认值含 {key}", f'"{key}"' in ui_src)
    check(f"5-x 插件读写 {key}", key in init_src)

# 5-y 配置项必须同时出现在 UI 默认值 与 插件 __update_config 落盘段
ui_defaults = ui_src.split("default_config", 1)[-1] if "default_config" in ui_src else ""
check("5-1 UI 默认值块可定位", "dian115_auto_login" in ui_defaults)
check("5-2 auto_login 默认开启（True）", '"dian115_auto_login": True' in ui_src,
      "自动登录应默认开启，手工 Token 才是降级路径")

# 5-3 保活服务必须已注册
check("5-3 保活服务已接入 get_service", "_build_dian115_login_service" in init_src)
check("5-4 保活不抛异常污染调度", "不影响后续任务" in init_src)

# 5-5 旧的手工 Token 必填门槛必须已移除
check("5-5 不再强制要求手工 Token",
      "if not self._dian115_token:\n            logger.warning(" not in init_src,
      "v1.8.0 的『未配置 Token 直接 return』必须删除，否则自动登录无从生效")

# ==================== 6. 加载级守护：静态名 import 检查 ====================


def scan_static_names():
    """扫描新增模块：注解位置使用的 typing 名必须已导入。"""
    import typing
    typing_names = {n for n in dir(typing) if n[:1].isupper()}
    problems = []
    for rel in ("clients/dian115.py", "clients/dian115_turnstile.py"):
        p = PLUGIN / rel
        tree = ast.parse(p.read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "typing":
                imported |= {a.name for a in node.names}
            elif isinstance(node, ast.Import):
                if any(a.name == "typing" for a in node.names):
                    imported |= typing_names
        used = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id in typing_names:
                used.add(node.id)
            elif isinstance(node, ast.Attribute) and node.attr in typing_names:
                if isinstance(node.value, ast.Name) and node.value.id == "typing":
                    continue
                used.add(node.attr)
        missing = used - imported
        if missing:
            problems.append(f"{rel}: {sorted(missing)}")
    return problems


check("6-1 新增模块无未导入 typing 名", not scan_static_names(),
      f"{scan_static_names()}")

# ==================== 结果 ====================

print("=" * 60)
if FAILURES:
    print(f"结果：{len(FAILURES)} 项失败 -> {FAILURES}")
    sys.exit(1)
print("结果：全部断言通过")
