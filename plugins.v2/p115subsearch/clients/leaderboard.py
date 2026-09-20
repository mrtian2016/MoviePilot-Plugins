# -*- coding: utf-8 -*-
"""
榜单订阅（List）客户端：把 MoviePilot 自带的推荐链适配成 P115SubSearch 的榜单来源。

设计要点（对齐任务书 A 节）：
    * **零第三方依赖**：榜单数据全部来自 MoviePilot 原生 ``RecommendChain``
      （TMDB / 豆瓣榜单），不拷贝 CloudSubscribe 的多网盘系统，也不 import 它；
    * **慢源不阻塞**：所有抓取结果进本地 TTL 缓存；缓存过期且来源异常时，
      返回旧数据并标记 ``stale``，调用方绝不因外部来源抖动而中断；
    * **import guard**：``app.chain.recommend`` / ``app.schemas.types`` 均为可选依赖，
      缺失时模块仍可导入，方法返回结构化降级结果而不是抛 AttributeError；
    * 本模块**不写任何同步热路径**，只由 UI 浏览 / 后台刷新服务调用。
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Dict, List, Optional

try:
    from app.log import logger
    LOGGER_AVAILABLE = True
except Exception:  # pragma: no cover - MoviePilot 运行时之外
    logger = logging.getLogger("p115subsearch.leaderboard")
    LOGGER_AVAILABLE = False

try:
    from app.chain.recommend import RecommendChain
    RECOMMEND_AVAILABLE = True
except Exception:  # pragma: no cover - MoviePilot 运行时之外
    RecommendChain = None  # type: ignore[assignment]
    RECOMMEND_AVAILABLE = False

try:
    from app.schemas.types import MediaType
    MEDIATYPE_AVAILABLE = True
except Exception:  # pragma: no cover - MoviePilot 运行时之外
    MediaType = None  # type: ignore[assignment]
    MEDIATYPE_AVAILABLE = False


class LeaderboardError(Exception):
    """榜单获取失败（来源未知、依赖缺失、上游异常统一包装为此异常）。"""


# 榜单来源目录：id -> (RecommendChain 方法名, 中文名, 默认媒体类型, 是否支持 count 参数)
LEADERBOARD_SOURCES: List[Dict[str, str]] = [
    {"id": "tmdb_trending", "name": "TMDB 流行趋势", "media_type": "movie", "method": "tmdb_trending", "count": False},
    {"id": "tmdb_movies", "name": "TMDB 热门电影", "media_type": "movie", "method": "tmdb_movies", "count": False},
    {"id": "tmdb_tvs", "name": "TMDB 热门剧集", "media_type": "tv", "method": "tmdb_tvs", "count": False},
    {"id": "douban_movie_showing", "name": "豆瓣正在热映", "media_type": "movie", "method": "douban_movie_showing", "count": True},
    {"id": "douban_movie_hot", "name": "豆瓣热门电影", "media_type": "movie", "method": "douban_movie_hot", "count": True},
    {"id": "douban_tv_hot", "name": "豆瓣热门剧集", "media_type": "tv", "method": "douban_tv_hot", "count": True},
    {"id": "douban_tv_weekly_chinese", "name": "豆瓣国产剧集周榜", "media_type": "tv", "method": "douban_tv_weekly_chinese", "count": True},
    {"id": "douban_tv_weekly_global", "name": "豆瓣全球剧集周榜", "media_type": "tv", "method": "douban_tv_weekly_global", "count": True},
    {"id": "douban_tv_animation", "name": "豆瓣动画剧集", "media_type": "tv", "method": "douban_tv_animation", "count": True},
    {"id": "douban_movies", "name": "豆瓣最新电影", "media_type": "movie", "method": "douban_movies", "count": True},
    {"id": "douban_tvs", "name": "豆瓣最新剧集", "media_type": "tv", "method": "douban_tvs", "count": True},
]

SOURCE_MAP: Dict[str, Dict[str, str]] = {item["id"]: item for item in LEADERBOARD_SOURCES}

# v1.9.1 first-use 默认来源：
#   只依赖 MoviePilot 原生推荐链（TMDB），不引入任何第三方服务；
#   用户未配置来源时至少有一个可浏览的榜单，避免「已启用却空白」的不可用状态。
DEFAULT_LEADERBOARD_SOURCES: List[str] = ["tmdb_trending"]


def default_leaderboard_sources() -> List[str]:
    """安全默认来源 id 的副本（调用方可安全修改，不会污染常量）。"""
    return list(DEFAULT_LEADERBOARD_SOURCES)


def normalize_source_ids(raw: Any) -> List[str]:
    """把配置里的来源归一化为**有效** id 列表：过滤未知来源、去重、保持顺序。

    容忍字符串（逗号分隔）/列表/元组/集合；任何其它类型返回空列表，绝不抛异常。
    """
    if isinstance(raw, str):
        candidates = [part.strip() for part in raw.replace("，", ",").split(",")]
    elif isinstance(raw, (list, tuple, set)):
        candidates = [str(item).strip() for item in raw]
    else:
        return []

    seen = set()
    result: List[str] = []
    for source_id in candidates:
        if source_id and source_id in SOURCE_MAP and source_id not in seen:
            seen.add(source_id)
            result.append(source_id)
    return result


def resolve_source_ids(raw: Any, enabled: bool = True) -> List[str]:
    """归一化用户配置的榜单来源，并在 first-use 场景回落到安全默认来源（v1.9.1）。

    * **启用榜单**但未配置（或配置项全部无效）时，返回
      :data:`DEFAULT_LEADERBOARD_SOURCES` —— 保证启用即有可用来源；
    * 榜单关闭时不强塞默认值，尊重用户的空配置（重新启用时仍可回落默认）。
    """
    source_ids = normalize_source_ids(raw)
    if not source_ids and enabled:
        return default_leaderboard_sources()
    return source_ids

# 媒体类型归一化词表
_MOVIE_TOKENS = {"movie", "电影", "film", "mv", "teleplay_movie"}
_TV_TOKENS = {"tv", "电视剧", "剧集", "teleplay", "series", "动画", "动漫", "综艺"}
if MediaType is not None:  # pragma: no cover - 依赖 MoviePilot 运行时
    try:
        _MOVIE_TOKENS.add(str(getattr(MediaType.MOVIE, "value", "")).strip().lower())
        _TV_TOKENS.add(str(getattr(MediaType.TV, "value", "")).strip().lower())
    except Exception:
        pass


def normalize_media_type(raw: Any, default: str = "movie") -> str:
    """把上游五花八门的类型字段归一化为 ``movie`` / ``tv``。"""
    text = str(raw or "").strip().lower()
    if text in _MOVIE_TOKENS:
        return "movie"
    if text in _TV_TOKENS:
        return "tv"
    return default if default in ("movie", "tv") else "movie"


def _to_int(value: Any) -> Optional[int]:
    if value in (None, "", 0, "0"):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _to_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def normalize_item(item: Any, source_id: str, default_type: str = "movie") -> Optional[Dict[str, Any]]:
    """把上游条目（dict / 对象 / ``to_dict()``）归一化为前端可直接渲染的结构。"""
    if item is None:
        return None
    data: Dict[str, Any]
    if isinstance(item, dict):
        data = item
    elif hasattr(item, "to_dict"):
        try:
            data = dict(item.to_dict() or {})
        except Exception:
            return None
    else:
        try:
            data = dict(item)
        except Exception:
            return None

    title = str(data.get("title") or data.get("name") or "").strip()
    if not title:
        return None

    year = str(data.get("year") or "").strip()
    if not year:
        for key in ("release_date", "first_air_date", "date"):
            raw_date = str(data.get(key) or "").strip()
            if len(raw_date) >= 4 and raw_date[:4] > "1900":
                year = raw_date[:4]
                break

    poster = data.get("poster_path") or data.get("poster") or data.get("cover") or ""

    return {
        "title": title,
        "year": year,
        "media_type": normalize_media_type(data.get("type") or data.get("media_type"), default_type),
        "tmdb_id": _to_int(data.get("tmdb_id")),
        "douban_id": str(data.get("douban_id") or "").strip() or None,
        "vote_average": round(_to_float(data.get("vote_average") or data.get("vote")), 1),
        "poster": str(poster or ""),
        "overview": str(data.get("overview") or data.get("intro") or "").strip(),
        "source": source_id,
        "source_name": SOURCE_MAP.get(source_id, {}).get("name", source_id),
    }


class LeaderboardClient:
    """榜单客户端：来源目录 + 抓取 + TTL 缓存 + 安全降级。

    :param chain: 可注入的 ``RecommendChain`` 实例（测试注入假实现）
    :param cache_ttl_seconds: 缓存有效期，默认 30 分钟
    :param page_size: 单页条目上限
    :param clock: 时间源（测试注入）
    """

    DEFAULT_CACHE_TTL = 1800
    DEFAULT_PAGE_SIZE = 20

    def __init__(
        self,
        chain: Any = None,
        cache_ttl_seconds: int = DEFAULT_CACHE_TTL,
        page_size: int = DEFAULT_PAGE_SIZE,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        self._chain = chain
        try:
            self._cache_ttl = max(0, int(cache_ttl_seconds))
        except (TypeError, ValueError):
            self._cache_ttl = self.DEFAULT_CACHE_TTL
        try:
            self._page_size = max(1, int(page_size))
        except (TypeError, ValueError):
            self._page_size = self.DEFAULT_PAGE_SIZE
        self._clock = clock or time.monotonic
        self._cache: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ 目录

    def sources(self) -> List[Dict[str, str]]:
        """返回可浏览的榜单来源目录（纯静态，零网络）。"""
        return [
            {"id": item["id"], "name": item["name"], "media_type": item["media_type"]}
            for item in LEADERBOARD_SOURCES
        ]

    def source_name(self, source_id: str) -> str:
        return SOURCE_MAP.get(str(source_id or "").strip(), {}).get("name", str(source_id or ""))

    # ------------------------------------------------------------------ 抓取

    def _resolve_chain(self) -> Any:
        if self._chain is not None:
            return self._chain
        if not RECOMMEND_AVAILABLE or RecommendChain is None:
            raise LeaderboardError(
                "MoviePilot 推荐链（RecommendChain）不可用，请确认已启用 MoviePilot 推荐功能"
            )
        try:
            self._chain = RecommendChain()
        except Exception as exc:  # noqa: BLE001
            raise LeaderboardError(f"MoviePilot 推荐链（RecommendChain）初始化失败：{exc}") from exc
        return self._chain

    def _chain_method(self, source_id: str):
        meta = SOURCE_MAP.get(source_id)
        if not meta:
            raise LeaderboardError(f"未知榜单来源：{source_id}，请从榜单来源列表中选择")
        chain = self._resolve_chain()
        method_name = meta["method"]
        method = getattr(chain, method_name, None)
        if not callable(method):
            raise LeaderboardError(
                f"当前 MoviePilot 推荐链不支持榜单「{meta['name']}」"
                f"（缺少 RecommendChain.{method_name}），请升级 MoviePilot"
            )
        return method, meta

    def fetch(self, source_id: str, page: int = 1) -> List[Dict[str, Any]]:
        """抓取指定榜单的一页并归一化；失败统一抛 :class:`LeaderboardError`。"""
        source_id = str(source_id or "").strip()
        method, meta = self._chain_method(source_id)
        try:
            page = max(1, int(page))
        except (TypeError, ValueError):
            page = 1

        try:
            if meta.get("count"):
                raw_items = method(page=page, count=self._page_size) or []
            else:
                raw_items = method(page=page) or []
        except Exception as exc:  # noqa: BLE001
            raise LeaderboardError(f"抓取榜单「{meta['name']}」失败：{exc}") from exc

        items: List[Dict[str, Any]] = []
        for raw in list(raw_items)[: self._page_size]:
            normalized = normalize_item(raw, source_id, meta.get("media_type", "movie"))
            if normalized:
                items.append(normalized)
        return items

    # ------------------------------------------------------------------ 浏览

    def browse(self, source_id: str, page: int = 1, refresh: bool = False) -> Dict[str, Any]:
        """浏览榜单：缓存优先，失败安全降级。**本方法永不抛异常。**"""
        source_id = str(source_id or "").strip()
        if not source_id:
            return self._failure("", "", "未指定榜单来源，请先选择榜单来源")

        try:
            page = max(1, int(page))
        except (TypeError, ValueError):
            page = 1

        key = f"{source_id}:{page}"
        now = self._clock()
        with self._lock:
            entry = self._cache.get(key)

        if entry and not refresh and (now - float(entry.get("fetched_at") or 0)) < self._cache_ttl:
            return self._success(source_id, page, list(entry.get("items") or []),
                                 cached=True, stale=False, fetched_at=entry.get("fetched_at"))

        try:
            items = self.fetch(source_id, page)
        except LeaderboardError as exc:
            return self._degrade(source_id, page, entry, exc)
        except Exception as exc:  # noqa: BLE001 - 任何未预期异常都不允许冒泡到 UI/主线程
            return self._degrade(source_id, page, entry, exc)

        with self._lock:
            self._cache[key] = {"items": list(items), "fetched_at": now}
        return self._success(source_id, page, items, cached=False, stale=False, fetched_at=now)

    # -------------------------------------------------------------- 结果构造

    def _success(self, source_id: str, page: int, items: List[Dict[str, Any]],
                 cached: bool, stale: bool, fetched_at: Any) -> Dict[str, Any]:
        return {
            "success": True,
            "source": source_id,
            "source_name": self.source_name(source_id),
            "page": page,
            "items": items,
            "empty": not items,
            "cached": cached,
            "stale": stale,
            "error": "",
            "message": "",
            "fetched_at": fetched_at,
        }

    def _degrade(self, source_id: str, page: int, entry: Optional[Dict[str, Any]],
                 exc: Exception) -> Dict[str, Any]:
        """抓取失败：有旧缓存就返回旧数据并标记 stale，否则给出可操作失败结果。"""
        reason = str(exc) or exc.__class__.__name__
        logger.warning(f"榜单[{source_id}]获取失败：{reason}")
        if entry and entry.get("items"):
            result = self._success(source_id, page, list(entry.get("items") or []),
                                   cached=True, stale=True, fetched_at=entry.get("fetched_at"))
            result["error"] = reason
            result["message"] = f"榜单来源暂时不可用，已展示缓存数据：{reason}"
            return result
        return self._failure(source_id, page, f"榜单获取失败：{reason}", error=reason)

    def _failure(self, source_id: str, page: int, message: str, error: str = "") -> Dict[str, Any]:
        return {
            "success": False,
            "source": source_id,
            "source_name": self.source_name(source_id),
            "page": page,
            "items": [],
            "empty": True,
            "cached": False,
            "stale": False,
            "error": error,
            "message": message,
            "fetched_at": None,
        }

    # ------------------------------------------------------------------ 其它

    def clear_cache(self) -> None:
        with self._lock:
            self._cache.clear()
