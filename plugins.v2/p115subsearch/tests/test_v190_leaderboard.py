# -*- coding: utf-8 -*-
"""
P115SubSearch v1.9.0 测试（一）：原生榜单订阅（移植 CloudSubscribe 榜单订阅能力）

设计原则（对齐任务书 A 节）：
    * 榜单来源复用 MoviePilot 自带 RecommendChain（tmdb_* / douban_*），
      不引入任何第三方依赖，也**不 import CloudSubscribe**；
    * 慢源只走「缓存读取 + 后台刷新」，绝不进入订阅/搜索同步热路径；
    * 空榜单、异常、无网络都必须优雅降级，方法绝不向上抛异常。

本文件是**行为测试**：注入假 chain 实跑 LeaderboardClient / LeaderboardHandler，
覆盖输入输出与降级路径，不做纯源码字符串断言。

运行：python tests/test_v190_leaderboard.py
     或者 pytest tests/test_v190_leaderboard.py
"""
import importlib.machinery
import importlib.util
import json
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


# ===========================================================================
# 0. 最小 app.* 桩 + 真实包语义加载
# ===========================================================================

def _install_stubs(recommend_available=True):
    app = types.ModuleType("app")
    log = types.ModuleType("app.log")

    class _L:
        def info(self, *a, **k):
            pass

        warning = error = debug = info

    log.logger = _L()
    app.log = log
    sys.modules["app"] = app
    sys.modules["app.log"] = log

    chain = types.ModuleType("app.chain")
    sys.modules["app.chain"] = chain
    if recommend_available:
        recommend = types.ModuleType("app.chain.recommend")

        class RecommendChain:  # 占位，实际测试都注入假 chain
            pass

        recommend.RecommendChain = RecommendChain
        sys.modules["app.chain.recommend"] = recommend
        chain.recommend = recommend
    else:
        sys.modules.pop("app.chain.recommend", None)


def _ensure_pkg(name, path):
    if name in sys.modules:
        return sys.modules[name]
    mod = types.ModuleType(name)
    mod.__path__ = [str(path)]
    mod.__package__ = name
    spec = importlib.machinery.ModuleSpec(name, loader=None, is_package=True)
    spec.submodule_search_locations = mod.__path__
    mod.__spec__ = spec
    sys.modules[name] = mod
    return mod


def load_sub(qualname, relpath):
    """按真实包语义加载插件内的子模块（支持相对导入）。"""
    parts = qualname.split(".")
    for index in range(1, len(parts)):
        _ensure_pkg(".".join(parts[:index]), PLUGIN / Path(*parts[1:index]))
    spec = importlib.util.spec_from_file_location(qualname, str(PLUGIN / relpath))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[qualname] = mod
    spec.loader.exec_module(mod)
    return mod


# ===========================================================================
# 1. 假 RecommendChain
# ===========================================================================

class FakeRecommendChain:
    """可编排的假榜单链：记录调用，按需返回数据或抛异常。"""

    def __init__(self, data=None, error=None):
        self.data = data if data is not None else {}
        self.error = error
        self.calls = []

    def _serve(self, name, page=1, **kwargs):
        self.calls.append((name, page))
        if self.error is not None:
            raise self.error
        value = self.data.get(name, [])
        if callable(value):
            value = value(page)
        if isinstance(value, dict):
            # 支持 {"1": [...], "2": [...]} 分页
            value = value.get(str(page), [])
        return list(value or [])

    def tmdb_trending(self, page=1, **kwargs):
        return self._serve("tmdb_trending", page)

    def tmdb_movies(self, page=1, **kwargs):
        return self._serve("tmdb_movies", page)

    def tmdb_tvs(self, page=1, **kwargs):
        return self._serve("tmdb_tvs", page)

    def douban_movie_showing(self, page=1, **kwargs):
        return self._serve("douban_movie_showing", page)

    def douban_movie_hot(self, page=1, **kwargs):
        return self._serve("douban_movie_hot", page)

    def douban_tv_hot(self, page=1, **kwargs):
        return self._serve("douban_tv_hot", page)

    def douban_tv_weekly_chinese(self, page=1, **kwargs):
        return self._serve("douban_tv_weekly_chinese", page)

    def douban_tv_weekly_global(self, page=1, **kwargs):
        return self._serve("douban_tv_weekly_global", page)

    def douban_tv_animation(self, page=1, **kwargs):
        return self._serve("douban_tv_animation", page)


def tmdb_item(title="示例电影", year="2025", mtype="电影", tmdb_id=101, vote=8.1):
    return {
        "title": title, "year": year, "type": mtype, "tmdb_id": tmdb_id,
        "douban_id": None, "vote_average": vote, "overview": "简介",
        "poster_path": "https://image.tmdb.org/t/p/original/x.jpg",
        "release_date": f"{year}-01-01",
    }


def douban_item(title="示例剧集", year="2024", mtype="电视剧", douban_id="12345", vote=7.7):
    return {
        "title": title, "year": year, "type": mtype, "tmdb_id": None,
        "douban_id": douban_id, "vote_average": vote, "overview": "简介",
        "poster_path": "https://img9.doubanio.com/x.webp",
        "first_air_date": f"{year}-03-01",
    }


# ===========================================================================
# 2. LeaderboardClient：来源目录 / 抓取 / 缓存 / 降级
# ===========================================================================

def _client_module(chain):
    _install_stubs(recommend_available=True)
    mod = load_sub("p115subsearch_v190.clients.leaderboard", "clients/leaderboard.py")
    return mod


def test_sources_catalog():
    mod = _client_module(FakeRecommendChain())
    client = mod.LeaderboardClient(chain=FakeRecommendChain())
    sources = client.sources()
    ids = [s["id"] for s in sources]
    check("1-1 榜单来源为列表且非空", isinstance(sources, list) and len(sources) >= 6, f"ids={ids}")
    check("1-2 覆盖 TMDB 与豆瓣两类来源",
          any(i.startswith("tmdb_") for i in ids) and any(i.startswith("douban_") for i in ids),
          f"ids={ids}")
    check("1-3 每个来源都带中文名与媒体类型",
          all(s.get("name") and s.get("media_type") for s in sources),
          f"sources={sources}")
    check("1-4 来源条目暴露可展示的键",
          all({"id", "name", "media_type"} <= set(s) for s in sources),
          f"sources={sources}")


def test_fetch_normalizes_items():
    chain = FakeRecommendChain({"tmdb_trending": [tmdb_item(), douban_item()]})
    mod = _client_module(chain)
    client = mod.LeaderboardClient(chain=chain)
    items = client.fetch("tmdb_trending")
    check("2-1 返回归一化条目", len(items) == 2, f"items={items}")
    first = items[0]
    check("2-2 条目字段齐备",
          {"title", "year", "media_type", "tmdb_id", "douban_id",
           "vote_average", "poster", "source"} <= set(first),
          f"keys={sorted(first)}")
    check("2-3 中文「电影」归一化为 movie", first["media_type"] == "movie", first["media_type"])
    check("2-4 中文「电视剧」归一化为 tv", items[1]["media_type"] == "tv", items[1]["media_type"])
    check("2-5 海报取 poster_path", first["poster"].startswith("https://"), first["poster"])
    check("2-6 榜单来源打标", first["source"] == "tmdb_trending", first["source"])
    check("2-7 tmdb_id 转 int", first["tmdb_id"] == 101, first["tmdb_id"])


def test_fetch_rejects_unknown_source():
    mod = _client_module(FakeRecommendChain())
    client = mod.LeaderboardClient(chain=FakeRecommendChain())
    try:
        client.fetch("not_a_source")
        check("3-1 未知来源抛 LeaderboardError", False, "未抛异常")
    except mod.LeaderboardError as exc:
        check("3-1 未知来源抛 LeaderboardError", True)
        check("3-2 未知来源提示可操作", "not_a_source" in str(exc), str(exc))
    except Exception as exc:  # noqa: BLE001
        check("3-1 未知来源抛 LeaderboardError", False, f"抛了 {type(exc).__name__}")


def test_fetch_chain_error_raises_leaderboard_error():
    chain = FakeRecommendChain(error=RuntimeError("TMDB 502"))
    mod = _client_module(chain)
    client = mod.LeaderboardClient(chain=chain)
    try:
        client.fetch("tmdb_movies")
        check("4-1 抓取异常包装为 LeaderboardError", False, "未抛异常")
    except mod.LeaderboardError as exc:
        check("4-1 抓取异常包装为 LeaderboardError", True)
        check("4-2 异常信息保留原因", "502" in str(exc), str(exc))
    except Exception as exc:  # noqa: BLE001
        check("4-1 抓取异常包装为 LeaderboardError", False, f"抛了 {type(exc).__name__}")


def test_browse_empty_and_error_paths():
    mod = _client_module(FakeRecommendChain())
    # 空榜单：success=True 但 empty=True，不算错误
    empty_client = mod.LeaderboardClient(chain=FakeRecommendChain({"tmdb_movies": []}))
    result = empty_client.browse("tmdb_movies")
    check("5-1 空榜单不报错", result["success"] is True, json.dumps(result, ensure_ascii=False))
    check("5-2 空榜单标记 empty", result.get("empty") is True, json.dumps(result, ensure_ascii=False))
    check("5-3 空榜单条目为空列表", result["items"] == [], f"items={result['items']}")

    # 异常且无缓存：success=False，降级不抛异常
    bad = mod.LeaderboardClient(chain=FakeRecommendChain(error=RuntimeError("network down")))
    result2 = bad.browse("tmdb_movies")
    check("5-4 异常时 success=False", result2["success"] is False, json.dumps(result2, ensure_ascii=False))
    check("5-5 异常时返回空列表而非抛异常", result2["items"] == [], f"items={result2['items']}")
    check("5-6 异常信息可展示", "network down" in result2.get("error", ""), result2.get("error"))

    # 缺少来源
    result3 = bad.browse("")
    check("5-7 缺少来源时 success=False", result3["success"] is False, json.dumps(result3, ensure_ascii=False))
    check("5-8 缺少来源给出中文提示", "来源" in result3.get("message", ""), result3.get("message"))


def test_browse_cache_hits_and_refresh():
    chain = FakeRecommendChain({"tmdb_movies": [tmdb_item(tmdb_id=1), tmdb_item(tmdb_id=2)]})
    mod = _client_module(chain)
    client = mod.LeaderboardClient(chain=chain, cache_ttl_seconds=600)

    first = client.browse("tmdb_movies")
    check("6-1 首次抓取成功", first["success"] and len(first["items"]) == 2, json.dumps(first, ensure_ascii=False))
    check("6-2 首次为未命中缓存", first["cached"] is False, f"cached={first['cached']}")
    calls_after_first = len(chain.calls)

    second = client.browse("tmdb_movies")
    check("6-3 二次命中缓存", second["cached"] is True, f"cached={second['cached']}")
    check("6-4 命中缓存不再请求来源", len(chain.calls) == calls_after_first,
          f"calls={chain.calls}")

    third = client.browse("tmdb_movies", refresh=True)
    check("6-5 refresh=True 强制重取", len(chain.calls) == calls_after_first + 1,
          f"calls={chain.calls}")
    check("6-6 强制重取后 cached=False", third["cached"] is False, f"cached={third['cached']}")


def test_browse_cache_ttl_expiry():
    chain = FakeRecommendChain({"tmdb_movies": [tmdb_item()]})
    mod = _client_module(chain)
    now = {"t": 1000.0}
    client = mod.LeaderboardClient(
        chain=chain, cache_ttl_seconds=60, clock=lambda: now["t"]
    )
    client.browse("tmdb_movies")
    calls = len(chain.calls)
    now["t"] += 30  # 未过期
    client.browse("tmdb_movies")
    check("7-1 TTL 内仍命中缓存", len(chain.calls) == calls, f"calls={chain.calls}")
    now["t"] += 61  # 过期
    client.browse("tmdb_movies")
    check("7-2 TTL 过期后重新抓取", len(chain.calls) == calls + 1, f"calls={chain.calls}")


def test_browse_stale_cache_degrades_safely():
    """慢源/网络异常：有旧缓存时必须返回旧数据并标记 stale，而不是空手而归。"""
    chain = FakeRecommendChain({"tmdb_movies": [tmdb_item(tmdb_id=9)]})
    mod = _client_module(chain)
    now = {"t": 1000.0}
    client = mod.LeaderboardClient(
        chain=chain, cache_ttl_seconds=60, clock=lambda: now["t"]
    )
    client.browse("tmdb_movies")
    # 缓存过期 + 来源开始报错
    now["t"] += 3600
    chain.error = RuntimeError("HTTP 502")
    result = client.browse("tmdb_movies")
    check("8-1 过期且来源异常时仍返回旧数据", len(result["items"]) == 1,
          json.dumps(result, ensure_ascii=False))
    check("8-2 标记为 stale", result.get("stale") is True, json.dumps(result, ensure_ascii=False))
    check("8-3 success 仍为 True（有可用数据）", result["success"] is True,
          json.dumps(result, ensure_ascii=False))
    check("8-4 同时带出错误原因供提示", "502" in result.get("error", ""), result.get("error"))


def test_page_size_cap():
    many = [tmdb_item(tmdb_id=i, title=f"影片{i}") for i in range(50)]
    chain = FakeRecommendChain({"tmdb_movies": many})
    mod = _client_module(chain)
    client = mod.LeaderboardClient(chain=chain, page_size=8)
    result = client.browse("tmdb_movies")
    check("9-1 单页条目数受 page_size 限制", len(result["items"]) == 8,
          f"n={len(result['items'])}")


def test_missing_chain_method_is_actionable():
    class HalfChain:
        pass

    mod = _client_module(HalfChain())
    client = mod.LeaderboardClient(chain=HalfChain())
    try:
        client.fetch("tmdb_movies")
        check("10-1 缺少链方法时抛 LeaderboardError", False, "未抛异常")
    except mod.LeaderboardError as exc:
        check("10-1 缺少链方法时抛 LeaderboardError", True)
        check("10-2 提示 MoviePilot 版本不兼容", "MoviePilot" in str(exc), str(exc))


def test_import_guard_without_recommend_chain():
    """模块级 import 必须有 guard：无 RecommendChain 时模块仍可导入。"""
    _install_stubs(recommend_available=False)
    mod = load_sub("p115subsearch_v190noguard.clients.leaderboard", "clients/leaderboard.py")
    check("11-1 无 app.chain.recommend 时模块可导入", mod is not None)
    check("11-2 暴露 RECOMMEND_AVAILABLE 标记",
          hasattr(mod, "RECOMMEND_AVAILABLE") and mod.RECOMMEND_AVAILABLE is False,
          str(getattr(mod, "RECOMMEND_AVAILABLE", "<missing>")))
    client = mod.LeaderboardClient()
    result = client.browse("tmdb_movies")
    check("11-3 缺依赖时 browse 仍返回结构化结果而不抛异常",
          isinstance(result, dict) and result.get("success") is False,
          json.dumps(result, ensure_ascii=False))
    check("11-4 缺依赖提示可操作", "RecommendChain" in result.get("message", "")
          or "推荐" in result.get("message", ""), result.get("message"))


# ===========================================================================
# 3. LeaderboardHandler：订阅入口 / 快照
# ===========================================================================

class FakeSubscribeChain:
    def __init__(self, result=(7, "订阅成功"), error=None):
        self.result = result
        self.error = error
        self.added = []

    def add(self, **kwargs):
        self.added.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.result


class FakeSubscribeOper:
    def __init__(self, existing=None):
        self.existing = existing or []
        self.checked = []

    def exists(self, **kwargs):
        self.checked.append(kwargs)
        return bool(self.existing)


def _handler_module(chain, subscribe_chain=None, subscribe_oper=None, store=None):
    _install_stubs(recommend_available=True)
    client_mod = load_sub("p115subsearch_v190.clients.leaderboard", "clients/leaderboard.py")
    handler_mod = load_sub("p115subsearch_v190.handlers.leaderboard", "handlers/leaderboard.py")
    store = store if store is not None else {}

    def get_data(key):
        return store.get(key)

    def save_data(key, value):
        store[key] = value

    handler = handler_mod.LeaderboardHandler(
        client=client_mod.LeaderboardClient(chain=chain),
        subscribe_chain=subscribe_chain or FakeSubscribeChain(),
        subscribe_oper=subscribe_oper or FakeSubscribeOper(),
        get_data_func=get_data,
        save_data_func=save_data,
    )
    return client_mod, handler_mod, handler, store


def test_handler_browse_contract():
    chain = FakeRecommendChain({"tmdb_trending": [tmdb_item(), douban_item()]})
    _, _, handler, _ = _handler_module(chain)
    result = handler.browse({"source": "tmdb_trending", "page": 1})
    check("12-1 browse 返回 success/message/data",
          {"success", "message", "data"} <= set(result), sorted(result))
    check("12-2 data.items 为榜单条目", len(result["data"]["items"]) == 2,
          json.dumps(result, ensure_ascii=False))
    check("12-3 message 为中文可读文案", bool(result.get("message")), result.get("message"))

    missing = handler.browse({})
    check("12-4 缺少来源时 success=False", missing["success"] is False, json.dumps(missing, ensure_ascii=False))
    check("12-5 缺少来源提示中文", "榜单" in missing["message"] or "来源" in missing["message"],
          missing["message"])


def test_handler_browse_empty_message():
    _, _, handler, _ = _handler_module(FakeRecommendChain({"tmdb_movies": []}))
    result = handler.browse({"source": "tmdb_movies"})
    check("13-1 空榜单 success=True", result["success"] is True, json.dumps(result, ensure_ascii=False))
    check("13-2 空榜单给出空状态文案", "暂无" in result["message"], result["message"])
    check("13-3 data.empty=True 便于前端渲染空状态", result["data"].get("empty") is True,
          json.dumps(result, ensure_ascii=False))


def test_handler_browse_error_message():
    _, _, handler, _ = _handler_module(FakeRecommendChain(error=RuntimeError("timeout")))
    result = handler.browse({"source": "tmdb_movies"})
    check("14-1 榜单异常 success=False", result["success"] is False, json.dumps(result, ensure_ascii=False))
    check("14-2 异常文案含原因", "timeout" in result["message"], result["message"])
    check("14-3 异常不抛出", True)


def test_handler_subscribe_success():
    chain = FakeRecommendChain()
    sub = FakeSubscribeChain(result=(321, "订阅成功"))
    oper = FakeSubscribeOper()
    _, _, handler, store = _handler_module(chain, sub, oper)
    result = handler.subscribe({
        "title": "示例电影", "year": "2025", "media_type": "movie",
        "tmdb_id": 101, "source": "tmdb_trending",
    })
    check("15-1 订阅返回 success", result["success"] is True, json.dumps(result, ensure_ascii=False))
    check("15-2 状态为 subscribed", result["data"]["status"] == "subscribed",
          result["data"]["status"])
    check("15-3 回传订阅 ID", result["data"].get("subscribe_id") == 321,
          str(result["data"]))
    check("15-4 真正调用了 MoviePilot 订阅链", len(sub.added) == 1, f"added={sub.added}")
    call = sub.added[0]
    check("15-5 带 tmdbid 提交", call.get("tmdbid") == 101, str(call))
    check("15-6 带中文标题", call.get("title") == "示例电影", str(call))
    check("15-7 关闭即时通知噪音（message=False）", call.get("message") is False, str(call))


def test_handler_subscribe_tv_season():
    chain = FakeRecommendChain()
    sub = FakeSubscribeChain(result=(322, "ok"))
    _, _, handler, _ = _handler_module(chain, sub, FakeSubscribeOper())
    result = handler.subscribe({
        "title": "示例剧集", "year": "2024", "media_type": "tv",
        "season": 2, "douban_id": "12345",
    })
    check("16-1 剧集订阅成功", result["data"]["status"] == "subscribed",
          json.dumps(result, ensure_ascii=False))
    call = sub.added[0]
    check("16-2 剧集带 season", call.get("season") == 2, str(call))
    check("16-3 无 tmdb 时用 doubanid", call.get("doubanid") == "12345", str(call))


def test_handler_subscribe_existing():
    chain = FakeRecommendChain()
    sub = FakeSubscribeChain()
    oper = FakeSubscribeOper(existing=[{"id": 1}])
    _, _, handler, _ = _handler_module(chain, sub, oper)
    result = handler.subscribe({
        "title": "已订阅电影", "year": "2025", "media_type": "movie", "tmdb_id": 555,
    })
    check("17-1 已存在时不重复订阅", len(sub.added) == 0, f"added={sub.added}")
    check("17-2 状态为 subscription_exists",
          result["data"]["status"] == "subscription_exists", result["data"]["status"])
    check("17-3 success 仍为 True（幂等成功）", result["success"] is True,
          json.dumps(result, ensure_ascii=False))


def test_handler_subscribe_unrecognized_and_error():
    chain = FakeRecommendChain()
    sub = FakeSubscribeChain()
    _, _, handler, _ = _handler_module(chain, sub, FakeSubscribeOper())
    # 缺少任何强 ID -> 未识别
    bad = handler.subscribe({"title": "无身份条目", "year": "2025", "media_type": "movie"})
    check("18-1 缺强 ID 不调用订阅链", len(sub.added) == 0, f"added={sub.added}")
    check("18-2 状态为 unrecognized", bad["data"]["status"] == "unrecognized",
          json.dumps(bad, ensure_ascii=False))
    check("18-3 给出可操作提示", "TMDB" in bad["message"] or "豆瓣" in bad["message"],
          bad["message"])

    # 缺标题
    no_title = handler.subscribe({"media_type": "movie", "tmdb_id": 1})
    check("18-4 缺标题为 unrecognized", no_title["data"]["status"] == "unrecognized",
          json.dumps(no_title, ensure_ascii=False))

    # 订阅链抛异常 -> error，不崩
    boom = FakeSubscribeChain(error=RuntimeError("链异常"))
    _, _, handler2, _ = _handler_module(chain, boom, FakeSubscribeOper())
    err = handler2.subscribe({"title": "异常电影", "media_type": "movie", "tmdb_id": 2})
    check("18-5 订阅链异常降级为 error", err["data"]["status"] == "error",
          json.dumps(err, ensure_ascii=False))
    check("18-6 success=False", err["success"] is False, json.dumps(err, ensure_ascii=False))

    # 订阅链返回 (None, msg) -> error
    none_chain = FakeSubscribeChain(result=(None, "识别失败"))
    _, _, handler3, _ = _handler_module(chain, none_chain, FakeSubscribeOper())
    err2 = handler3.subscribe({"title": "识别失败电影", "media_type": "movie", "tmdb_id": 3})
    check("18-7 链返回 None 视为失败", err2["data"]["status"] == "error",
          json.dumps(err2, ensure_ascii=False))


def test_handler_subscribe_batch():
    chain = FakeRecommendChain()
    sub = FakeSubscribeChain(result=(1, "ok"))
    _, _, handler, _ = _handler_module(chain, sub, FakeSubscribeOper())
    result = handler.subscribe({"items": [
        {"title": "A", "media_type": "movie", "tmdb_id": 11},
        {"title": "B", "media_type": "movie", "tmdb_id": 12},
    ]})
    check("19-1 批量订阅返回 results", len(result["data"]["results"]) == 2,
          json.dumps(result, ensure_ascii=False))
    check("19-2 批量调用订阅链两次", len(sub.added) == 2, f"added={len(sub.added)}")
    check("19-3 统计订阅成功数", result["data"].get("subscribed") == 2,
          json.dumps(result["data"], ensure_ascii=False))

    empty = handler.subscribe({"items": []})
    check("19-4 空批次给出提示", empty["success"] is False, json.dumps(empty, ensure_ascii=False))


def test_snapshot_is_local_and_persisted():
    chain = FakeRecommendChain({"tmdb_trending": [tmdb_item(tmdb_id=77)]})
    _, _, handler, store = _handler_module(chain)
    check("20-1 初始快照为空", handler.snapshot() == [], str(handler.snapshot()))
    handler.browse({"source": "tmdb_trending"})
    snap = handler.snapshot()
    check("20-2 浏览后写入本地快照", len(snap) == 1, json.dumps(snap, ensure_ascii=False))
    check("20-3 快照条目带来源", snap[0].get("source") == "tmdb_trending", str(snap[0]))
    calls = len(chain.calls)
    handler.snapshot()
    check("20-4 读快照零网络请求", len(chain.calls) == calls, f"calls={chain.calls}")


def test_refresh_snapshot_background_degrade():
    chain = FakeRecommendChain({"tmdb_trending": [tmdb_item(tmdb_id=5)]})
    _, _, handler, store = _handler_module(chain)
    summary = handler.refresh_snapshot(["tmdb_trending", "tmdb_movies"])
    check("21-1 后台刷新返回结构化摘要", isinstance(summary, dict) and "sources" in summary,
          json.dumps(summary, ensure_ascii=False))
    ok = [s for s in summary["sources"] if s["source"] == "tmdb_trending"][0]
    bad = [s for s in summary["sources"] if s["source"] == "tmdb_movies"][0]
    check("21-2 成功来源标记 ok", ok["success"] is True, str(ok))
    check("21-3 空来源不算失败", bad["success"] is True, str(bad))

    # 全异常时后台刷新不抛异常
    chain.error = RuntimeError("down")
    summary2 = handler.refresh_snapshot(["tmdb_trending"])
    check("21-4 后台刷新异常不抛出", isinstance(summary2, dict), str(summary2))
    check("21-5 异常来源标记失败", summary2["sources"][0]["success"] is False, str(summary2))


def test_no_cloudsubscribe_dependency():
    import ast

    for rel in ("clients/leaderboard.py", "handlers/leaderboard.py"):
        tree = ast.parse((PLUGIN / rel).read_text(encoding="utf-8"))
        bad = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                bad.extend(a.name for a in node.names if "cloudsubscribe" in a.name.lower())
            elif isinstance(node, ast.ImportFrom) and "cloudsubscribe" in (node.module or "").lower():
                bad.append(node.module)
        check(f"22-{rel} 不 import CloudSubscribe", not bad, str(bad))
        # 模块级（非注释）不得出现源插件模块路径依赖
        check(f"22-{rel} 无 cloudsubscribe 模块路径依赖",
              "cloudsubscribe." not in (PLUGIN / rel).read_text(encoding="utf-8").lower(),
              "出现模块路径")


# ===========================================================================
# runner
# ===========================================================================

TESTS = [
    test_sources_catalog,
    test_fetch_normalizes_items,
    test_fetch_rejects_unknown_source,
    test_fetch_chain_error_raises_leaderboard_error,
    test_browse_empty_and_error_paths,
    test_browse_cache_hits_and_refresh,
    test_browse_cache_ttl_expiry,
    test_browse_stale_cache_degrades_safely,
    test_page_size_cap,
    test_missing_chain_method_is_actionable,
    test_import_guard_without_recommend_chain,
    test_handler_browse_contract,
    test_handler_browse_empty_message,
    test_handler_browse_error_message,
    test_handler_subscribe_success,
    test_handler_subscribe_tv_season,
    test_handler_subscribe_existing,
    test_handler_subscribe_unrecognized_and_error,
    test_handler_subscribe_batch,
    test_snapshot_is_local_and_persisted,
    test_refresh_snapshot_background_degrade,
    test_no_cloudsubscribe_dependency,
]


def main():
    print("=" * 68)
    print("P115SubSearch v1.9.0 榜单订阅测试")
    print("=" * 68)
    for fn in TESTS:
        print()
        print(f"-- {fn.__name__}")
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            global _failed
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
