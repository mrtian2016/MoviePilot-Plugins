# -*- coding: utf-8 -*-
"""
P115SubSearch v1.8.0 自测（standalone，无 MoviePilot 运行时依赖）

覆盖：
A. 搜索源调度
   A1 dian115 在启用且有客户端时进入可用源
   A2 Nullbr 已失效，即使配置勾选也不进入可用源（硬屏蔽）
   A3 自定义优先级生效，未列出的源追加在末尾
   A4 search_single_source("dian115") 走 _search_dian115
B. Dian115 客户端资源归一化
   B1 只保留 115 分享链接（ed2k/magnet 丢弃）
   B2 需积分解锁且未解锁的条目被跳过
   B3 status != active 的条目被跳过
   B4 统一格式为 {"url","title","update_time"} 且 limit 生效
C. 签到处理器
   C1 enabled_providers 按开关与凭据判定
   C2 单一 provider 失败不影响其他 provider
   C3 历史写入走 save_data 且截断到上限
   C4 dian115 签到合并签到+转盘为一条记录
D. 配置页 Tab 结构
   D1 六个 Tab 齐备且顺序正确
   D2 每个 model 都出现在 default_config 中
   D3 主界面含「插件基本功能配置」「115网盘信息配置」两个区块标题
   D4 已失效源（nullbr）不再出现在表单中

实现：直接加载源码模块（import 前桩掉 app.* 依赖），用假对象驱动。
"""
import re
import sys
import types
from pathlib import Path

PLUGIN = Path(__file__).resolve().parent.parent

FAILURES = []


def check(name, cond, extra=""):
    if cond:
        print(f"[PASS] {name}")
    else:
        print(f"[FAIL] {name} {extra}")
        FAILURES.append(name)


# ---------------- 桩掉 app.* 运行时依赖 ----------------


class _Logger:
    def info(self, *a, **k):
        pass

    def warning(self, *a, **k):
        pass

    def error(self, *a, **k):
        pass

    def debug(self, *a, **k):
        pass


def _install_stubs():
    if "app" in sys.modules:
        return
    app = types.ModuleType("app")
    app.log = types.ModuleType("app.log")
    app.log.logger = _Logger()

    core = types.ModuleType("app.core")
    cfg = types.ModuleType("app.core.config")
    cfg.settings = types.SimpleNamespace(PROXY=None, TZ="Asia/Shanghai")
    core.config = cfg
    app.core = core

    schemas = types.ModuleType("app.schemas")
    schemas.MediaInfo = object
    stypes = types.ModuleType("app.schemas.types")

    class MediaType:
        MOVIE = "MOVIE"
        TV = "TV"

    stypes.MediaType = MediaType
    schemas.types = stypes

    sys.modules.update({
        "app": app,
        "app.log": app.log,
        "app.core": core,
        "app.core.config": cfg,
        "app.schemas": schemas,
        "app.schemas.types": stypes,
    })


_install_stubs()

# ---------------- 加载被测模块 ----------------

SRC_SEARCH = (PLUGIN / "handlers" / "search.py").read_text(encoding="utf-8")
SRC_CHECKIN = (PLUGIN / "handlers" / "checkin.py").read_text(encoding="utf-8")
SRC_DIAN = (PLUGIN / "clients" / "dian115.py").read_text(encoding="utf-8")
SRC_UI = (PLUGIN / "ui" / "config.py").read_text(encoding="utf-8")


def load_module(name, source, inject=None):
    """用桩环境加载一个源码文件为模块。

    被测模块存在 ``from ..utils import xxx`` 形式的包内相对导入，
    独立 exec 时会触发 ImportError。这里把相对导入替换为同名桩，
    既保留模块内对该名字的引用，又不依赖完整包结构。
    """
    stubs = []
    def _replace(m):
        # group(1)=相对层级, group(2)=模块, group(3)=导入名
        names = [x.strip() for x in m.group(3).split(",") if x.strip()]
        for n in names:
            # SimpleTTLCache 在构造期就会被实例化，必须给可调用对象而非 None
            if n == "SimpleTTLCache":
                stubs.append(
                    "class SimpleTTLCache:\n"
                    "    def __init__(self, *a, **k):\n"
                    "        self._d = {}\n"
                    "    def get(self, k, default=None):\n"
                    "        return self._d.get(str(k), default)\n"
                    "    def set(self, k, v):\n"
                    "        self._d[str(k)] = v\n"
                    "    def pop(self, k, default=None):\n"
                    "        return self._d.pop(str(k), default)\n"
                    "    def clear(self):\n"
                    "        self._d.clear()\n"
                    "    def __len__(self):\n"
                    "        return len(self._d)"
                )
            else:
                stubs.append(f"{n} = None")
        return "# [stubbed] " + m.group(0)

    source = re.sub(
        r"^from (\.+)([\w\.]+) import (.+)$", _replace, source, flags=re.MULTILINE
    )

    mod = types.ModuleType(name)
    mod.__dict__.update(inject or {})
    sys.modules[name] = mod
    exec(compile("\n".join(stubs) + "\n" + source,
                 str(PLUGIN / f"{name}.py"), "exec"), mod.__dict__)
    return mod


# ================= A. 搜索源调度 =================

search_mod = load_module("_p115_search_probe", SRC_SEARCH)


class _FakeDianClient:
    is_configured = True

    def __init__(self):
        self.calls = []

    def search_resources(self, tmdb_id, media_type, season, limit=20):
        self.calls.append((tmdb_id, media_type, season, limit))
        return [{"url": "https://115.com/s/aaa", "title": "t", "update_time": ""}
                for _ in range(limit)]


class _FakeMediaInfo:
    def __init__(self, title="测试片", tmdb_id=123, year=2024):
        self.title = title
        self.tmdb_id = tmdb_id
        self.year = year


def make_search_handler(**over):
    params = dict(
        pansou_client=object(),
        nullbr_client=object(),
        pansou_enabled=True,
        nullbr_enabled=True,      # 即使勾选也应被硬屏蔽
        kdocs_client=types.SimpleNamespace(is_ready=True),
        kdocs_enabled=True,
        dian115_client=over.pop("dian115_client", _FakeDianClient()),
        dian115_enabled=True,
    )
    params.update(over)
    return search_mod.SearchHandler(**params)


SearchHandler = search_mod.SearchHandler
MediaType = sys.modules["app.schemas.types"].MediaType

h = make_search_handler()
sources = h.get_enabled_sources()
check("A1 dian115 进入可用源", "dian115" in sources, f"actual={sources}")
check("A2-1 nullbr 被硬屏蔽", "nullbr" not in sources, f"actual={sources}")
check("A3 默认优先级 dian115 在前", sources[0] == "dian115", f"actual={sources}")

h2 = make_search_handler(search_source_order=["kdocs", "pansou"])
sources2 = h2.get_enabled_sources()
check("A3-1 自定义优先级在前", sources2[:2] == ["kdocs", "pansou"], f"actual={sources2}")
check("A3-2 未列出源追加末尾", "dian115" in sources2[2:], f"actual={sources2}")

client = _FakeDianClient()
h3 = make_search_handler(dian115_client=client)
results = h3.search_single_source(
    "dian115", _FakeMediaInfo(), MediaType.TV, 2, tag_source=True
)
check("A4-1 dian115 分支有结果", len(results) == 20, f"actual={len(results)}")
check("A4-2 调用带 season 与 media_type",
      client.calls and client.calls[0][1] == "tv" and client.calls[0][2] == 2,
      f"actual={client.calls}")
check("A4-3 结果带 _source 标签",
      all(x.get("_source") == "dian115" for x in results))

h4 = make_search_handler(dian115_client=None)
check("A4-4 客户端缺失时安全返回", h4.search_single_source(
    "dian115", _FakeMediaInfo(), MediaType.MOVIE) == [])

# ================= B. Dian115 资源归一化 =================

dian_mod = load_module("_p115_dian_probe", SRC_DIAN)
Dian115Client = dian_mod.Dian115Client
normalize = Dian115Client._normalize_share
share_url = dian_mod.share_url_from_codes
is_115 = dian_mod.is_115_share_url

check("B0-1 115 分享链接可识别",
      is_115(share_url("abcd1234", "xy")))
check("B0-2 非 115 链接被拒绝",
      not is_115("magnet:?xt=urn:btih:abc") and not is_115("ed2k://|file|x|1|h|/"))

res = {"title": "测试片 4K"}
b1 = normalize({"share_code": "s1", "receive_code": "r1", "status": "active"}, res)
check("B1-1 115 链接保留", b1 is not None and "115.com" in b1["url"], f"actual={b1}")
check("B1-2 统一格式三字段",
      b1 is not None and set(b1.keys()) == {"url", "title", "update_time"},
      f"actual={b1 and list(b1.keys())}")

b2 = normalize({"share_code": "s1", "receive_code": "r1",
                "unlock_cost": 5, "is_unlocked": False}, res)
check("B2-1 需解锁未解锁 -> 跳过", b2 is None, f"actual={b2}")
b2b = normalize({"share_code": "s1", "receive_code": "r1",
                 "unlock_cost": 5, "is_unlocked": True}, res)
check("B2-2 已解锁 -> 保留", b2b is not None, f"actual={b2b}")

b3 = normalize({"share_code": "s1", "receive_code": "r1", "status": "deleted"}, res)
check("B3 非 active -> 跳过", b3 is None, f"actual={b3}")


class _SearchStub(Dian115Client):
    """只驱动 search_resources 的假客户端（绕过网络握手）。"""

    def __init__(self, shares):
        self._shares = shares

    def resource_detail(self, tmdb_id, media_type, season=0):
        return {"resource": {"title": "X"}, "shares": self._shares}


many = [{"share_code": f"s{i}", "receive_code": "r", "status": "active"} for i in range(50)]
got = _SearchStub(many).search_resources(1, "movie", 0, limit=3)
check("B4-1 limit 生效", len(got) == 3, f"actual={len(got)}")

mixed = [
    {"share_code": "s1", "receive_code": "r", "status": "active"},
    {"share_code": "", "receive_code": "", "status": "active"},      # 无链接
    {"share_code": "s2", "receive_code": "r", "status": "active",
     "unlock_cost": 3},                                              # 需解锁
    {"share_code": "s3", "receive_code": "r", "status": "active"},
]
got2 = _SearchStub(mixed).search_resources(1, "movie", 0, limit=20)
check("B4-2 混合列表只留 2 条有效 115", len(got2) == 2, f"actual={len(got2)}")

# ================= C. 签到处理器 =================

checkin_mod = load_module("_p115_checkin_probe", SRC_CHECKIN)
CheckinHandler = checkin_mod.CheckinHandler


class _MemStore:
    def __init__(self):
        self.data = {}

    def get(self, k):
        return self.data.get(k)

    def save(self, k, v):
        self.data[k] = v


class _FakeP115:
    def __init__(self, ok=True, already=False, points=10, days=7, client=True):
        self._ok = ok
        self._already = already
        self._points = points
        self._days = days
        self.client = object() if client else None

    def points_sign(self):
        if not self._ok:
            return {"success": False, "message": "接口失败"}
        return {
            "success": True,
            "already": self._already,
            "points": self._points,
            "days": self._days,
            "message": "今日已签到" if self._already else f"签到成功，{self._points} 枫叶"
        }


class _FakeDian:
    def __init__(self, fail=False):
        self._fail = fail
        self._n = 0

    def get_account_info(self):
        if self._fail:
            raise RuntimeError("握手失败")
        self._n += 1
        return {"points": 100 + self._n, "consecutive_signin": 3}

    def signin(self, mode="normal"):
        return {"success": True, "already_checked_in": False,
                "award_points": 5, "mode": mode}

    def run_lottery(self, count):
        return {"success": True, "target_count": count, "executed": count,
                "used_after": count, "cost_points": 1, "award_points": 2,
                "vip_days": 0, "points_change": 1}


store = _MemStore()
ch = CheckinHandler(
    p115_manager=_FakeP115(),
    dian115_client=_FakeDian(),
    p115_checkin_enabled=True,
    dian115_checkin_enabled=True,
    dian115_lottery_enabled=True,
    dian115_lottery_count=2,
    get_data_func=store.get,
    save_data_func=store.save,
)
providers = ch.enabled_providers()
check("C1-1 两个源都可用", providers == ["p115", "dian115"], f"actual={providers}")

ch2 = CheckinHandler(p115_manager=_FakeP115(client=False),
                     dian115_client=None,
                     p115_checkin_enabled=True, dian115_checkin_enabled=True)
check("C1-2 凭据缺失时都不启用", ch2.enabled_providers() == [],
      f"actual={ch2.enabled_providers()}")

result = ch.run_checkin()
check("C2-1 两个源都执行", result["total"] == 2 and result["success_count"] == 2,
      f"actual={result}")
hist = store.get("checkin_history")
check("C3-1 历史已写入", isinstance(hist, list) and len(hist) == 2, f"actual={hist}")
check("C3-2 历史字段完整",
      all({"time", "provider", "status", "message"} <= set(r) for r in hist or []))

bad = CheckinHandler(p115_manager=_FakeP115(ok=False), dian115_client=_FakeDian(fail=True),
                     p115_checkin_enabled=True, dian115_checkin_enabled=True,
                     get_data_func=store.get, save_data_func=store.save)
r2 = bad.run_checkin()
check("C2-2 全失败时 fail_count=2", r2["fail_count"] == 2, f"actual={r2}")
check("C2-3 失败也有历史记录", len(store.get("checkin_history")) == 4,
      f"actual={len(store.get('checkin_history'))}")

mixed_h = CheckinHandler(p115_manager=_FakeP115(ok=False), dian115_client=_FakeDian(),
                         p115_checkin_enabled=True, dian115_checkin_enabled=True,
                         dian115_lottery_enabled=True, dian115_lottery_count=1,
                         get_data_func=store.get, save_data_func=store.save)
r3 = mixed_h.run_checkin()
check("C2-4 一成一败", r3["success_count"] == 1 and r3["fail_count"] == 1,
      f"actual={r3}")

dian_rec = [r for r in r3["records"] if r["provider"] == "dian115"][0]
# v1.8.3：转盘字段由嵌套 lottery 改为扁平 lottery_* 字段（对齐网盘搜索助手 _build_record）
check("C4-1 dian115 记录含转盘明细（扁平字段）",
      dian_rec.get("lottery_target_count") == 1
      and "lottery_executed" in dian_rec
      and "lottery_used_after" in dian_rec,
      f"actual={dian_rec}")
check("C4-1b 转盘进度取当日累计（used_after）而非本次增量",
      dian_rec.get("lottery_used_after") is not None
      and "当日" in str(dian_rec.get("message") or ""),
      f"actual={dian_rec}")
check("C4-2 dian115 记录成功", dian_rec["success"] is True, f"actual={dian_rec}")

# 历史截断
big_store = _MemStore()
big_store.save("checkin_history", [{"time": f"2026-01-01 00:00:{i:02d}"} for i in range(60)])
ch3 = CheckinHandler(p115_manager=_FakeP115(), p115_checkin_enabled=True,
                     get_data_func=big_store.get, save_data_func=big_store.save)
ch3.run_checkin()
check("C3-3 历史不超过上限 100",
      len(big_store.get("checkin_history")) <= 100,
      f"actual={len(big_store.get('checkin_history'))}")

# ================= D. 配置页 Tab 结构 =================

ui_src = SRC_UI
tab_values = re.findall(r"'value': '(\w+_tab)'", ui_src)
expected_tabs = ["subscribe_tab", "checkin_tab", "pansou_tab",
                 "dian115_tab", "kdocs_tab", "leaderboard_tab"]
# VTabs 区域只应出现一次（VWindow 中复用同样字符串，去重后比对顺序）
seen = []
for v in tab_values:
    if v not in seen:
        seen.append(v)
check("D1 六个 Tab 齐备且顺序正确", seen == expected_tabs, f"actual={seen}")

models = sorted(set(re.findall(r"'model': '([a-z0-9_]+)'", ui_src)))
# 下划线开头的是 Tab 内部状态键（如 _tabs），不参与配置持久化
models = [m for m in models if not m.startswith("_")]
default_block = ui_src[ui_src.index("default_config = {"):]
missing = [m for m in models if m not in default_block]
check("D2 所有 model 都有默认值", not missing, f"missing={missing}")

check("D3-1 主界面含基本功能配置区块", "插件基本功能配置" in ui_src)
check("D3-2 主界面含115网盘信息配置区块", "115网盘信息配置" in ui_src)
check("D4 已失效 nullbr 表单项已移除",
      "'model': 'nullbr_enabled'" not in ui_src)

# ================= 结果 =================

print("=" * 60)
if FAILURES:
    print(f"结果：{len(FAILURES)} 项失败 -> {FAILURES}")
    sys.exit(1)
print("结果：全部断言通过")
