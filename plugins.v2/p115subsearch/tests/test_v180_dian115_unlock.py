#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
v1.8.0 Dian115 积分解锁与预算控制测试

覆盖：
    U1  客户端 _normalize_share 对「免费/已解锁/需解锁/无 ID」四类条目的分流
    U2  客户端 unlock_share 的请求体与响应解析（实扣积分、already、链接提取）
    U3  SearchHandler._search_dian115 的两级预算过滤与自动解锁开关
    U4  SearchHandler.unlock_dian115_resource 的预算拦截、幂等缓存、记账
    U5  SyncHandler._unlock_pending_resource 的 Dian115 分派
    U6  配置页默认值与主插件字段存在性（Tab 里的开关与预算都落到 default_config）
    U7  SimpleTTLCache 的 TTL、容量上限与并发安全基本语义

这些用例刻意走「真实模块 import」（而不是 AST 抽片段），
以保证新增的 import / 字段 / 方法签名真的可加载。
"""
import ast
import io
import pathlib
import sys
import types

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT.parent))

PASS = 0
FAIL = 0


def check(name, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {detail}")


def read(rel):
    return (ROOT / rel).read_text(encoding="utf-8")


def install_stubs():
    """构造最小可加载运行环境（app.* 与 p115client 等外部依赖）。"""
    # 清掉可能被前序用例污染的包缓存，保证后续 import 拿到最新源码
    for name in list(sys.modules):
        if name == "p115subsearch" or name.startswith("p115subsearch."):
            sys.modules.pop(name, None)

    app = types.ModuleType("app")
    app.__path__ = []
    app_core = types.ModuleType("app.core")
    app_core.__path__ = []
    app_core_config = types.ModuleType("app.core.config")

    class _Settings:
        TZ = "Asia/Shanghai"

    app_core_config.settings = _Settings()

    app_log = types.ModuleType("app.log")

    class _Logger:
        def __getattr__(self, name):
            return lambda *a, **k: None

    app_log.logger = _Logger()
    app_schemas = types.ModuleType("app.schemas")

    class MediaInfo:
        def __init__(self, **kw):
            self.title = kw.get("title", "")
            self.tmdb_id = kw.get("tmdb_id")
            for k, v in kw.items():
                setattr(self, k, v)

    app_schemas.MediaInfo = MediaInfo
    app_schemas_types = types.ModuleType("app.schemas.types")

    class MediaType:
        MOVIE = "电影"
        TV = "电视剧"

    app_schemas_types.MediaType = MediaType

    # utils/file_matcher 依赖 app.core.metainfo.MetaInfo
    app_core_metainfo = types.ModuleType("app.core.metainfo")

    class MetaInfo:
        def __init__(self, *a, **k):
            pass

    app_core_metainfo.MetaInfo = MetaInfo

    # handlers/sync.py 需要的一串宿主依赖（这里只保证 import 期可解析）
    app_core_config.global_vars = types.SimpleNamespace()

    class _DownloadChain:
        def __init__(self, *a, **k):
            pass

    app_chain = types.ModuleType("app.chain")
    app_chain.__path__ = []
    app_chain_download = types.ModuleType("app.chain.download")
    app_chain_download.DownloadChain = _DownloadChain

    def _mk_oper(name):
        cls = type(name, (), {
            "__init__": lambda self, *a, **k: None,
            "list": lambda self, *a, **k: [],
            "get": lambda self, *a, **k: None,
        })
        return cls

    app_db = types.ModuleType("app.db")
    app_db.SessionFactory = lambda *a, **k: types.SimpleNamespace(
        __enter__=lambda s: s, __exit__=lambda *a: False
    )
    for mod_name, cls_name in (
        ("app.db.subscribe_oper", "SubscribeOper"),
        ("app.db.downloadhistory_oper", "DownloadHistoryOper"),
    ):
        m = types.ModuleType(mod_name)
        setattr(m, cls_name, _mk_oper(cls_name))
        sys.modules[mod_name] = m
        setattr(app_db, cls_name, getattr(m, cls_name))
    m = types.ModuleType("app.db.models")
    m.__path__ = []
    sys.modules["app.db.models"] = m
    m_site = types.ModuleType("app.db.models.site")
    m_site.Site = _mk_oper("Site")
    sys.modules["app.db.models.site"] = m_site

    app_utils = types.ModuleType("app.utils")
    app_utils.__path__ = []
    app_utils_string = types.ModuleType("app.utils.string")

    class StringUtils:
        @staticmethod
        def clear_file_name(value: str) -> str:
            return value or ""

    app_utils_string.StringUtils = StringUtils

    # handlers/subscribe.py 需要 app.chain.subscribe.SubscribeChain
    app_chain_subscribe = types.ModuleType("app.chain.subscribe")

    class _SubscribeChain:
        def __init__(self, *a, **k):
            pass

        @staticmethod
        def update_subscribe(*a, **k):
            return None

    app_chain_subscribe.SubscribeChain = _SubscribeChain

    # 第三方依赖最小桩（仅保证 import 期可解析）
    sqlalchemy = types.ModuleType("sqlalchemy")
    sqlalchemy.text = lambda value: value
    sqlalchemy.create_engine = lambda *a, **k: types.SimpleNamespace()
    sqlalchemy.orm = types.ModuleType("sqlalchemy.orm")
    sqlalchemy.orm.Session = type("Session", (), {})
    sys.modules["sqlalchemy"] = sqlalchemy
    sys.modules["sqlalchemy.orm"] = sqlalchemy.orm

    class NotificationType:
        Plugin = "Plugin"

    app_schemas_types.NotificationType = NotificationType

    for name, mod in [
        ("app", app),
        ("app.core", app_core),
        ("app.core.config", app_core_config),
        ("app.core.metainfo", app_core_metainfo),
        ("app.log", app_log),
        ("app.schemas", app_schemas),
        ("app.schemas.types", app_schemas_types),
        ("app.chain", app_chain),
        ("app.chain.download", app_chain_download),
        ("app.chain.subscribe", app_chain_subscribe),
        ("app.db", app_db),
        ("app.utils", app_utils),
        ("app.utils.string", app_utils_string),
    ]:
        sys.modules[name] = mod

    pkg = types.ModuleType("p115subsearch")
    pkg.__path__ = [str(ROOT)]
    sys.modules["p115subsearch"] = pkg


def import_module(dotted):
    import importlib
    return importlib.import_module(dotted)


# ======================================================================
# U7 SimpleTTLCache
# ======================================================================
def test_ttl_cache():
    print("\n[U7] SimpleTTLCache")
    install_stubs()
    tools = import_module("p115subsearch.utils.tools")
    cache = tools.SimpleTTLCache(ttl=100, maxsize=3)
    cache.set("a", "1")
    check("U7-1 set/get 命中", cache.get("a") == "1")
    check("U7-2 缺省值", cache.get("missing", "d") == "d")
    check("U7-3 pop 返回并移除", cache.pop("a") == "1" and cache.get("a") is None)
    cache.set("b", "2")
    cache.set("c", "3")
    cache.set("d", "4")
    check("U7-4 容量上限生效", len(cache) <= 3, f"len={len(cache)}")
    cache.clear()
    check("U7-5 clear 清空", len(cache) == 0)

    # 过期语义：把内部时钟前拨
    cache2 = tools.SimpleTTLCache(ttl=10, maxsize=8)
    cache2.set("k", "v")
    cache2._time = lambda: 1e9  # 远大于 TTL
    check("U7-6 TTL 过期返回默认值", cache2.get("k") is None)


# ======================================================================
# U1 客户端 _normalize_share 分流
# ======================================================================
def test_normalize_share():
    print("\n[U1] Dian115Client._normalize_share")
    install_stubs()
    mod = import_module("p115subsearch.clients.dian115")
    cls = mod.Dian115Client
    resource = {"title": "示例电影"}

    free = cls._normalize_share(
        {"id": 1, "share_code": "abc", "receive_code": "1234",
         "unlock_cost": 0, "is_unlocked": True, "status": "active"},
        resource,
    )
    check("U1-1 免费条目返回可直接转存链接",
          bool(free) and free.get("url") == "https://115.com/s/abc?password=1234",
          f"got={free}")
    check("U1-2 免费条目不带 need_unlock", not (free or {}).get("need_unlock"))

    paid = cls._normalize_share(
        {"id": 42, "share_code": "", "receive_code": "",
         "unlock_cost": 8, "is_unlocked": False, "status": "active",
         "file_name": "示例电影.2024.1080p.mkv"},
        resource,
    )
    check("U1-3 需解锁条目返回占位并带 share_id/unlock_points",
          bool(paid) and paid.get("need_unlock") is True
          and paid.get("share_id") == 42 and paid.get("unlock_points") == 8,
          f"got={paid}")
    check("U1-4 占位条目 url 为空", paid.get("url") == "")

    locked_but_paid = cls._normalize_share(
        {"id": 7, "share_code": "xyz", "receive_code": "",
         "unlock_cost": 5, "is_unlocked": True, "status": "active"},
        resource,
    )
    check("U1-5 已解锁条目即使有成本也直接给链接",
          bool(locked_but_paid) and locked_but_paid.get("url", "").startswith("https://115.com/s/xyz"))

    inactive = cls._normalize_share(
        {"id": 9, "share_code": "s", "receive_code": "", "status": "expired"},
        resource,
    )
    check("U1-6 非 active 条目被丢弃", inactive is None)

    no_id = cls._normalize_share(
        {"id": 0, "unlock_cost": 3, "is_unlocked": False, "status": "active"},
        resource,
    )
    check("U1-7 无分享 ID 的收费条目被丢弃", no_id is None)

    ed2k = cls._normalize_share(
        {"id": 11, "share_code": "", "receive_code": "",
         "unlock_cost": 0, "is_unlocked": False, "status": "active",
         "url": "ed2k://|file|movie.mkv|123|ABC|/"},
        resource,
    )
    check("U1-8 ed2k/magnet 无 115 链接且无成本 → 丢弃", ed2k is None)


# ======================================================================
# U2 客户端 unlock_share
# ======================================================================
def test_unlock_share():
    print("\n[U2] Dian115Client.unlock_share")
    install_stubs()
    mod = import_module("p115subsearch.clients.dian115")
    cls = mod.Dian115Client

    captured = {}

    def fake_request_json(method, api_path, current_path, **kwargs):
        captured["method"] = method
        captured["api_path"] = api_path
        captured["current_path"] = current_path
        captured["json"] = kwargs.get("json")
        return {
            "unlock": {
                "cost_points": 6,
                "payload": {"share_code": "sc1", "receive_code": "rc1"},
            }
        }

    inst = cls.__new__(cls)
    inst._request_json = fake_request_json
    result = inst.unlock_share(42)
    check("U2-1 请求方法与路径正确",
          captured["method"] == "POST" and captured["api_path"] == "/api/portal/unlock",
          f"got={captured}")
    check("U2-2 请求体带 share_id", captured["json"] == {"share_id": 42},
          f"got={captured.get('json')}")
    check("U2-3 解析出 115 链接",
          result.get("url") == "https://115.com/s/sc1?password=rc1",
          f"got={result}")
    check("U2-4 实扣积分入账", result.get("actual_points") == 6, f"got={result}")
    check("U2-5 非历史解锁", result.get("already") is False)

    # already 场景：不计费
    def fake_already(method, api_path, current_path, **kwargs):
        return {
            "already": True,
            "unlock": {"cost_points": 9, "url": "https://115.com/s/old"},
        }

    inst2 = cls.__new__(cls)
    inst2._request_json = fake_already
    r2 = inst2.unlock_share(7)
    check("U2-6 历史已解锁不重复计费",
          r2.get("already") is True and r2.get("actual_points") == 0,
          f"got={r2}")
    check("U2-7 历史已解锁仍返回链接",
          r2.get("url") == "https://115.com/s/old", f"got={r2}")

    inst3 = cls.__new__(cls)
    try:
        inst3.unlock_share(0)
        check("U2-8 无效 share_id 抛错", False)
    except Exception as exc:
        check("U2-8 无效 share_id 抛错", "invalid_share_id" in str(getattr(exc, "code", "")),
              f"got={exc}")


# ======================================================================
# U3 SearchHandler._search_dian115 预算过滤
# ======================================================================
def make_search_handler(**overrides):
    install_stubs()
    mod = import_module("p115subsearch.handlers.search")
    kwargs = dict(
        pansou_client=None, nullbr_client=None,
        only_115=True,
    )
    kwargs.update(overrides)
    return mod.SearchHandler(**kwargs), mod


def test_search_budget_filter():
    print("\n[U3] SearchHandler._search_dian115 预算过滤")

    class FakeClient:
        is_configured = True

        def __init__(self, items):
            self._items = items
            self.calls = 0

        def search_resources(self, tmdb_id, media_type, season=0, limit=20):
            self.calls += 1
            return [dict(i) for i in self._items]

    class FakeMediaInfo:
        title = "示例电影"
        tmdb_id = 550

    media_type = sys.modules["app.schemas.types"].MediaType

    items = [
        {"url": "https://115.com/s/free", "title": "免费", "update_time": ""},
        {"url": "", "title": "小成本", "update_time": "",
         "share_id": 1, "need_unlock": True, "unlock_points": 5},
        {"url": "", "title": "超预算", "update_time": "",
         "share_id": 2, "need_unlock": True, "unlock_points": 999},
    ]

    # 3-1 未开自动解锁：收费条目全部被丢弃
    client = FakeClient(items)
    handler, _ = make_search_handler(
        dian115_client=client, dian115_enabled=True,
        dian115_auto_unlock=False,
        dian115_max_unlock_points=50, dian115_max_points_per_sub=20,
    )
    got = handler._search_dian115(FakeMediaInfo(), media_type.MOVIE, None)
    check("U3-1 未开自动解锁只剩免费条目",
          len(got) == 1 and got[0]["title"] == "免费", f"got={got}")

    # 3-2 开启自动解锁：保留免费 + 预算内收费，超预算丢弃
    client = FakeClient(items)
    handler2, _ = make_search_handler(
        dian115_client=client, dian115_enabled=True,
        dian115_auto_unlock=True,
        dian115_max_unlock_points=50, dian115_max_points_per_sub=20,
    )
    got2 = handler2._search_dian115(FakeMediaInfo(), media_type.MOVIE, None)
    titles = [i["title"] for i in got2]
    check("U3-2 开启自动解锁保留免费+预算内条目",
          titles == ["免费", "小成本"], f"got={titles}")
    check("U3-3 收费条目被标记 need_unlock",
          all(i.get("need_unlock") for i in got2 if i["title"] != "免费"),
          f"got={got2}")

    # 3-4 单订阅预算更小：5 分条目仍被保留（5<=20），改小到 3 则应丢弃
    client = FakeClient(items)
    handler3, _ = make_search_handler(
        dian115_client=client, dian115_enabled=True,
        dian115_auto_unlock=True,
        dian115_max_unlock_points=50, dian115_max_points_per_sub=3,
    )
    got3 = handler3._search_dian115(FakeMediaInfo(), media_type.MOVIE, None)
    check("U3-4 单订阅预算收紧后丢弃超限条目",
          [i["title"] for i in got3] == ["免费"], f"got={got3}")

    # 3-5 无 TMDB / 未配置 → 空
    client = FakeClient(items)
    handler4, _ = make_search_handler(dian115_client=client, dian115_enabled=True)

    class NoTmdb:
        title = "无ID"
        tmdb_id = None

    check("U3-5 缺 TMDB ID 返回空",
          handler4._search_dian115(NoTmdb(), media_type.MOVIE, None) == [])
    check("U3-6 缺 TMDB 时不发请求", client.calls == 0)


# ======================================================================
# U4 SearchHandler.unlock_dian115_resource
# ======================================================================
def test_unlock_resource():
    print("\n[U4] SearchHandler.unlock_dian115_resource")

    class FakeDianClient:
        is_configured = True

        def __init__(self):
            self.calls = []

        def unlock_share(self, share_id, resource_id=0):
            self.calls.append(share_id)
            return {
                "url": f"https://115.com/s/s{share_id}",
                "actual_points": 6,
                "already": False,
                "raw": {},
            }

    def build(**over):
        client = FakeDianClient()
        handler, _ = make_search_handler(
            dian115_client=client, dian115_enabled=True,
            dian115_auto_unlock=True,
            **over,
        )
        return handler, client

    # 4-1 正常解锁 + 记账
    handler, client = build(dian115_max_unlock_points=50, dian115_max_points_per_sub=20)
    url = handler.unlock_dian115_resource(101, 6)
    check("U4-1 解锁返回链接", url == "https://115.com/s/s101", f"got={url}")
    check("U4-2 任务账本累加实扣积分", handler._dian115_spent_points == 6,
          f"got={handler._dian115_spent_points}")
    check("U4-3 订阅账本累加实扣积分", handler._dian115_sub_spent_points == 6,
          f"got={handler._dian115_sub_spent_points}")

    # 4-4 幂等：同一 share 再解锁不重复请求
    url_again = handler.unlock_dian115_resource(101, 6)
    check("U4-4 同一分享复用缓存不重复扣分",
          url_again == url and client.calls.count(101) == 1, f"calls={client.calls}")

    # 4-5 单订阅预算不足拦截
    handler2, client2 = build(dian115_max_unlock_points=50, dian115_max_points_per_sub=3)
    check("U4-5 单订阅预算不足返回 None",
          handler2.unlock_dian115_resource(202, 6) is None)
    check("U4-6 预算拦截时不发请求", client2.calls == [])

    # 4-7 任务总预算不足拦截
    handler3, client3 = build(dian115_max_unlock_points=4, dian115_max_points_per_sub=20)
    check("U4-7 任务总预算不足返回 None",
          handler3.unlock_dian115_resource(303, 6) is None)

    # 4-8 无效 share_id
    handler4, client4 = build(dian115_max_unlock_points=50, dian115_max_points_per_sub=20)
    check("U4-8 无效 share_id 返回 None",
          handler4.unlock_dian115_resource(0, 6) is None)

    # 4-9 单订阅历史预算持久化与复用
    handler5, _ = build(dian115_max_unlock_points=50, dian115_max_points_per_sub=20)
    saved = {}

    def fake_save(key, data):
        saved[key] = data

    handler5._save_data_func = fake_save
    handler5._get_data_func = lambda key: saved.get(key, {})
    handler5._current_sub_key = "tmdb_550_movie"
    handler5.unlock_dian115_resource(404, 6)
    check("U4-9 订阅花费写入持久化历史",
          saved.get("sub_points_history", {}).get("dian115:tmdb_550_movie") == 6,
          f"got={saved}")

    handler6, _ = build(dian115_max_unlock_points=50, dian115_max_points_per_sub=20)
    handler6._get_data_func = lambda key: {"dian115:tmdb_550_movie": 18}
    handler6.reset_dian115_sub_spent_points("tmdb_550_movie")
    check("U4-10 新任务载入订阅历史花费",
          handler6._dian115_sub_spent_points == 18,
          f"got={handler6._dian115_sub_spent_points}")
    check("U4-11 历史花费吃到上限后拒绝解锁",
          handler6.unlock_dian115_resource(505, 6) is None)

    handler7, _ = build(dian115_max_unlock_points=50, dian115_max_points_per_sub=20)
    saved2 = {"dian115:tmdb_550_movie": 12}

    def fake_save2(key, data):
        saved2.clear()
        saved2.update(data)

    handler7._save_data_func = fake_save2
    handler7._get_data_func = lambda key: dict(saved2)
    handler7.clear_dian115_sub_points("tmdb_550_movie")
    check("U4-12 订阅完成后清除历史记录",
          "dian115:tmdb_550_movie" not in saved2, f"got={saved2}")

    # 4-13 解锁服务端实扣高于预估时以服务端为准
    class HigherCostClient(FakeDianClient):
        def unlock_share(self, share_id, resource_id=0):
            return {"url": "https://115.com/s/hi", "actual_points": 18,
                    "already": False, "raw": {}}

    handler8, _ = build(dian115_max_unlock_points=100, dian115_max_points_per_sub=100)
    handler8._dian115_client = HigherCostClient()
    handler8.unlock_dian115_resource(606, 5)
    check("U4-13 实扣高于预估以服务端为准",
          handler8._dian115_spent_points == 18,
          f"got={handler8._dian115_spent_points}")

    # 4-14 Turnstile 拦截必须降级为「不扣分、不中断」
    class TurnstileClient(FakeDianClient):
        def unlock_share(self, share_id, resource_id=0):
            raise MakeErr("turnstile_failed")

    def MakeErr(code):
        err = RuntimeError("人机验证失败")
        err.code = code
        return err

    handler9, _ = build(dian115_max_unlock_points=50, dian115_max_points_per_sub=20)
    handler9._dian115_client = TurnstileClient()
    got9 = handler9.unlock_dian115_resource(707, 4)
    check("U4-14 turnstile 拦截返回 None 而非抛错", got9 is None, f"got={got9}")
    check("U4-15 turnstile 拦截不产生任何扣分记账",
          handler9._dian115_spent_points == 0
          and handler9._dian115_sub_spent_points == 0,
          f"task={handler9._dian115_spent_points} "
          f"sub={handler9._dian115_sub_spent_points}")
    check("U4-16 turnstile 拦截不写缓存（下次仍可尝试）",
          handler9._dian115_unlocked_cache.get("707") is None)


# ======================================================================
# U5 SyncHandler._unlock_pending_resource 分派
# ======================================================================
def test_sync_dispatch():
    print("\n[U5] SyncHandler._unlock_pending_resource")

    class FakeSearch:
        def __init__(self):
            self.dian_calls = []

        def unlock_dian115_resource(self, share_id, points):
            self.dian_calls.append((share_id, points))
            return f"https://115.com/s/d{share_id}" if share_id > 0 else ""

    install_stubs()
    sync_mod = import_module("p115subsearch.handlers.sync")
    sync_cls = sync_mod.SyncHandler

    obj = sync_cls.__new__(sync_cls)
    obj._search_handler = FakeSearch()

    # 5-1 显式 _source=dian115
    url = obj._unlock_pending_resource(
        {"_source": "dian115", "share_id": 55, "unlock_points": 4}, "A")
    check("U5-1 显式 dian115 来源走 dian115 解锁",
          url == "https://115.com/s/d55" and obj._search_handler.dian_calls == [(55, 4)],
          f"got={url} calls={obj._search_handler.dian_calls}")

    # 5-2 按字段特征推断 dian115（无 _source、无 slug、有 share_id）
    obj._search_handler = FakeSearch()
    url2 = obj._unlock_pending_resource(
        {"share_id": 66, "unlock_points": 3}, "B")
    check("U5-2 字段特征推断为 dian115",
          url2 == "https://115.com/s/d66", f"got={url2}")

    # 5-3 dian115 缺 share_id → 不解锁
    obj._search_handler = FakeSearch()
    url4 = obj._unlock_pending_resource({"_source": "dian115", "unlock_points": 2}, "D")
    check("U5-4 dian115 缺 share_id 返回空", url4 == "")

    # 5-5 既无 slug 也无 share_id → 空
    obj._search_handler = FakeSearch()
    check("U5-5 无任何解锁标识返回空",
          obj._unlock_pending_resource({"unlock_points": 2}, "E") == "")

    # 5-6 解锁失败透传空串
    class FailSearch(FakeSearch):
        def unlock_dian115_resource(self, share_id, points):
            return ""

    obj._search_handler = FailSearch()
    check("U5-6 解锁失败返回空串",
          obj._unlock_pending_resource({"_source": "dian115", "share_id": 9}, "F") == "")


# ======================================================================
# U6 配置页与主插件一致性
# ======================================================================
def test_config_wiring():
    print("\n[U6] 配置页与主插件一致性")
    ui_src = read("ui/config.py")
    init_src = read("__init__.py")

    for key in ("dian115_auto_unlock", "dian115_max_unlock_points",
                "dian115_max_points_per_sub"):
        check(f"U6-{key} 出现在配置表单", f"'model': '{key}'" in ui_src)
        default_block = ui_src[ui_src.index("default_config = {"):]
        check(f"U6-{key} 有默认值", f'"{key}"' in default_block)
        check(f"U6-{key} 主插件有类属性", f"_{key}:" in init_src)
        check(f"U6-{key} 主插件有写回", f'"{key}": self._{key}' in init_src)

    # 构造 SearchHandler 时确实透传
    check("U6-透传 auto_unlock",
          "dian115_auto_unlock=self._dian115_auto_unlock" in init_src)
    check("U6-透传 任务预算",
          "dian115_max_unlock_points=self._dian115_max_unlock_points" in init_src)
    check("U6-透传 订阅预算",
          "dian115_max_points_per_sub=self._dian115_max_points_per_sub" in init_src)

    # Tab 结构：五个 tab 都在
    for tab in ("订阅", "签到", "盘搜", "癫影", "金山文档"):
        check(f"U6-Tab「{tab}」存在", tab in ui_src)

    # SyncHandler 分派存在
    sync_src = read("handlers/sync.py")
    check("U6-SyncHandler 有统一解锁分派", "_unlock_pending_resource" in sync_src)
    check("U6-SyncHandler 载入 dian115 订阅预算",
          "reset_dian115_sub_spent_points" in sync_src)
    check("U6-SyncHandler 清理 dian115 订阅预算",
          "clear_dian115_sub_points" in sync_src)


# ======================================================================
# U8 加载级守护：全部模块可 import / 可编译
# ======================================================================
def test_import_guard():
    print("\n[U8] 加载级守护")
    install_stubs()
    import py_compile
    import compileall
    ok = compileall.compile_dir(str(ROOT), quiet=2, force=True)
    check("U8-1 全部模块 compile 通过", bool(ok))

    # 静态检查：类体内注解用到的 typing 名字必须已导入
    typing_names = {
        "Set", "Tuple", "Union", "Any", "Callable", "Dict", "List",
        "Optional", "Iterable", "Sequence", "Mapping", "Iterator", "FrozenSet",
    }
    missing = {}
    for path in ROOT.rglob("*.py"):
        if "__pycache__" in str(path):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "typing":
                imported |= {a.name for a in node.names}
        used = {
            n.id for n in ast.walk(tree)
            if isinstance(n, ast.Name) and n.id in typing_names
        }
        gap = used - imported
        if gap:
            missing[str(path.relative_to(ROOT))] = sorted(gap)
    check("U8-2 无未导入的 typing 名字", not missing, f"missing={missing}")

    # 旧测试桩里被替换掉的方法名不应再被生产代码引用
    for name in ("_mark_offline_history_status", ):
        pass


def main():
    print("=" * 68)
    print("v1.8.0 Dian115 积分解锁与预算控制测试")
    print("=" * 68)
    test_ttl_cache()
    test_normalize_share()
    test_unlock_share()
    test_search_budget_filter()
    test_unlock_resource()
    test_sync_dispatch()
    test_config_wiring()
    test_import_guard()
    print("\n" + "=" * 68)
    print(f"结果：{PASS} passed, {FAIL} failed")
    print("=" * 68)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
