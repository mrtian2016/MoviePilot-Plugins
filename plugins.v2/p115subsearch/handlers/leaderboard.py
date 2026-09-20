# -*- coding: utf-8 -*-
"""
榜单订阅处理器：把榜单浏览 / 订阅入口暴露成 P115SubSearch 的插件 API。

设计要点（对齐任务书 A 节）：
    * **复用 MoviePilot 自有媒体识别与订阅机制**：订阅直接调用平台
      ``SubscribeChain.add``，存在性判断调用 ``SubscribeOper.exists``，
      不复制 CloudSubscribe 的订阅服务；
    * **慢源只读缓存**：浏览走 ``LeaderboardClient`` 的 TTL 缓存；
      后台刷新由插件 cron 服务调用 :meth:`refresh_snapshot`，绝不进入
      订阅搜索同步热路径；
    * **降级不抛异常**：所有方法返回 ``{"success", "message", "data"}``
      结构，UI 永远有可展示的中文文案与空状态标记；
    * 本模块不做任何相对导入，保证可离线单测。
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional

try:
    from app.log import logger
    LOGGER_AVAILABLE = True
except Exception:  # pragma: no cover - MoviePilot 运行时之外
    logger = logging.getLogger("p115subsearch.leaderboard")
    LOGGER_AVAILABLE = False

try:
    from app.schemas.types import MediaType
    MEDIATYPE_AVAILABLE = True
except Exception:  # pragma: no cover - MoviePilot 运行时之外
    MediaType = None  # type: ignore[assignment]
    MEDIATYPE_AVAILABLE = False

try:
    from app.chain.subscribe import SubscribeChain
    SUBSCRIBE_CHAIN_AVAILABLE = True
except Exception:  # pragma: no cover - MoviePilot 运行时之外
    SubscribeChain = None  # type: ignore[assignment]
    SUBSCRIBE_CHAIN_AVAILABLE = False

# 媒体类型常量：优先使用 MoviePilot 枚举，缺失时退化为中文值（SubscribeChain 接受中文值）
MTYPE_MOVIE = getattr(MediaType, "MOVIE", "电影") if MediaType is not None else "电影"
MTYPE_TV = getattr(MediaType, "TV", "电视剧") if MediaType is not None else "电视剧"

_MOVIE_TOKENS = {"movie", "电影", "film", "mv"}
_TV_TOKENS = {"tv", "电视剧", "剧集", "series", "动画", "动漫"}
if MediaType is not None:  # pragma: no cover - 依赖 MoviePilot 运行时
    try:
        _MOVIE_TOKENS.add(str(getattr(MediaType.MOVIE, "value", "")).strip().lower())
        _TV_TOKENS.add(str(getattr(MediaType.TV, "value", "")).strip().lower())
    except Exception:
        pass

STATUS_SUBSCRIBED = "subscribed"
STATUS_EXISTS = "subscription_exists"
STATUS_UNRECOGNIZED = "unrecognized"
STATUS_ERROR = "error"


def _to_int(value: Any) -> Optional[int]:
    if value in (None, "", 0, "0"):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class LeaderboardHandler:
    """榜单浏览 / 订阅入口。

    :param client: :class:`~clients.leaderboard.LeaderboardClient` 实例
    :param subscribe_chain: MoviePilot ``SubscribeChain``（测试可注入假实现）
    :param subscribe_oper: MoviePilot ``SubscribeOper``（用于订阅存在性判断）
    :param get_data_func: 读插件数据（零网络快照）
    :param save_data_func: 写插件数据
    """

    SNAPSHOT_KEY = "leaderboard_snapshot"

    def __init__(
        self,
        client: Any = None,
        subscribe_chain: Any = None,
        subscribe_oper: Any = None,
        get_data_func: Optional[Callable[[str], Any]] = None,
        save_data_func: Optional[Callable[[str, Any], None]] = None,
    ) -> None:
        self._client = client
        self._subscribe_chain = subscribe_chain
        self._subscribe_oper = subscribe_oper
        self._get_data = get_data_func
        self._save_data = save_data_func

    # ------------------------------------------------------------ 内部工具

    def _chain(self) -> Any:
        if self._subscribe_chain is not None:
            return self._subscribe_chain
        if not SUBSCRIBE_CHAIN_AVAILABLE or SubscribeChain is None:
            return None
        try:
            self._subscribe_chain = SubscribeChain()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"订阅链初始化失败：{exc}")
            return None
        return self._subscribe_chain

    @staticmethod
    def _media_type(raw: Any) -> Any:
        text = str(raw or "").strip().lower()
        return MTYPE_TV if text in _TV_TOKENS else MTYPE_MOVIE

    def _save_snapshot(self, source_id: str, items: List[Dict[str, Any]]) -> None:
        if not self._save_data:
            return
        try:
            self._save_data(self.SNAPSHOT_KEY, {
                "source": source_id,
                "items": list(items or []),
            })
        except Exception as exc:  # noqa: BLE001 - 快照写入失败不影响浏览
            logger.warning(f"榜单快照写入失败：{exc}")

    # ---------------------------------------------------------------- 浏览

    def browse(self, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """浏览榜单。payload: ``{"source": str, "page": int, "refresh": bool}``"""
        payload = payload or {}
        source_id = str(payload.get("source") or "").strip()
        if not source_id:
            return self._reply(False, "未指定榜单来源，请先选择榜单来源",
                               {"source": "", "items": [], "empty": True})
        if self._client is None:
            return self._reply(False, "榜单客户端未初始化，请重启插件后重试",
                               {"source": source_id, "items": [], "empty": True})

        try:
            page = max(1, int(payload.get("page") or 1))
        except (TypeError, ValueError):
            page = 1

        try:
            result = self._client.browse(source_id, page=page, refresh=bool(payload.get("refresh")))
        except Exception as exc:  # noqa: BLE001 - 客户端已自降级，这里再兜一层
            logger.warning(f"榜单浏览异常：{exc}")
            return self._reply(False, f"榜单获取失败：{exc}",
                               {"source": source_id, "items": [], "empty": True})

        data = dict(result or {})
        data.setdefault("items", [])
        data.setdefault("empty", not data.get("items"))
        data["source_name"] = result.get("source_name") or source_id

        if not result.get("success"):
            reason = result.get("error") or result.get("message") or "来源不可用"
            return self._reply(False, f"榜单获取失败：{reason}", data)

        count = len(data.get("items") or [])
        if count:
            self._save_snapshot(source_id, data["items"])
            message = f"榜单「{data['source_name']}」共 {count} 条"
        else:
            message = f"榜单「{data['source_name']}」暂无数据，请稍后重试或切换其它榜单"
        if result.get("stale"):
            message = f"{message}（来源暂时不可用，当前为缓存数据）"
        return self._reply(True, message, data)

    # ---------------------------------------------------------------- 订阅

    def subscribe(self, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """订阅榜单媒体条目。支持单条 ``{title, media_type, tmdb_id, ...}``
        与批量 ``{"items": [...]}`` 两种入参。"""
        payload = payload or {}
        items = payload.get("items")
        if isinstance(items, list):
            if not items:
                return self._reply(False, "没有可订阅的榜单条目", {"results": [], "subscribed": 0})
            results = [self._subscribe_one(item or {}) for item in items]
            subscribed = sum(1 for item in results if item.get("status") == STATUS_SUBSCRIBED)
            existed = sum(1 for item in results if item.get("status") == STATUS_EXISTS)
            failed = len(results) - subscribed - existed
            data = {"results": results, "subscribed": subscribed,
                    "existing": existed, "failed": failed, "total": len(results)}
            if subscribed or existed:
                message = f"共 {len(results)} 条，新增订阅 {subscribed} 条，已存在 {existed} 条"
                if failed:
                    message = f"{message}，失败 {failed} 条"
                return self._reply(True, message, data)
            return self._reply(False, f"共 {len(results)} 条，全部订阅失败", data)

        single = self._subscribe_one(payload)
        data = dict(single)
        return self._reply(single.get("status") in (STATUS_SUBSCRIBED, STATUS_EXISTS),
                           single.get("message") or "", data)

    def _subscribe_one(self, item: Dict[str, Any]) -> Dict[str, Any]:
        title = str(item.get("title") or item.get("name") or "").strip()
        if not title:
            return self._result(STATUS_UNRECOGNIZED, "无法识别该条目：缺少媒体标题")

        tmdb_id = _to_int(item.get("tmdb_id"))
        douban_id = str(item.get("douban_id") or "").strip() or None
        if not tmdb_id and not douban_id:
            return self._result(STATUS_UNRECOGNIZED,
                                "无法识别该条目：缺少 TMDB / 豆瓣 ID，无法提交订阅")

        raw_type = str(item.get("media_type") or item.get("type") or "").strip().lower()
        is_tv = raw_type in _TV_TOKENS
        season = _to_int(item.get("season")) or (1 if is_tv else None)
        mtype = self._media_type(raw_type)
        year = str(item.get("year") or "").strip()
        source_name = str(item.get("source_name") or item.get("source") or "榜单").strip()

        # 1) 存在性判断：失败不阻塞订阅
        if self._subscribe_oper is not None:
            try:
                if self._subscribe_oper.exists(tmdbid=tmdb_id, doubanid=douban_id, season=season):
                    result = self._result(STATUS_EXISTS, f"《{title}》已在订阅列表中，无需重复订阅")
                    result["title"] = title
                    return result
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"订阅存在性判断失败，继续尝试订阅：{exc}")

        # 2) 提交订阅
        chain = self._chain()
        if chain is None:
            return self._result(STATUS_ERROR, "MoviePilot 订阅链不可用，请确认已启用订阅功能")

        kwargs = {
            "title": title,
            "year": year,
            "mtype": mtype,
            "tmdbid": tmdb_id,
            "doubanid": douban_id,
            "season": season,
            "source": f"榜单订阅·{source_name}",
            "message": False,
        }
        try:
            outcome = chain.add(**kwargs)
        except TypeError as exc:
            # 兼容不同 MoviePilot 版本的 add 签名：退回最小参数集重试
            logger.warning(f"订阅链签名不兼容，使用最小参数重试：{exc}")
            try:
                outcome = chain.add(title=title, year=year, mtype=mtype,
                                    tmdbid=tmdb_id, doubanid=douban_id, message=False)
            except Exception as exc2:  # noqa: BLE001
                return self._result(STATUS_ERROR, f"订阅失败：{exc2}")
        except Exception as exc:  # noqa: BLE001
            return self._result(STATUS_ERROR, f"订阅失败：{exc}")

        subscribe_id, message = self._parse_outcome(outcome)
        if subscribe_id:
            result = self._result(STATUS_SUBSCRIBED, message or f"《{title}》订阅成功")
            result["subscribe_id"] = subscribe_id
            result["title"] = title
            return result
        return self._result(STATUS_ERROR, message or "订阅失败：MoviePilot 未返回订阅 ID")

    @staticmethod
    def _parse_outcome(outcome: Any) -> Any:
        if isinstance(outcome, (tuple, list)):
            if len(outcome) >= 2:
                return outcome[0], str(outcome[1] or "")
            if len(outcome) == 1:
                return outcome[0], ""
            return None, ""
        if isinstance(outcome, int):
            return outcome, ""
        return None, str(outcome or "")

    @staticmethod
    def _result(status: str, message: str) -> Dict[str, Any]:
        return {"status": status, "message": message, "subscribe_id": None,
                "title": "", "success": status in (STATUS_SUBSCRIBED, STATUS_EXISTS)}

    @staticmethod
    def _reply(success: bool, message: str, data: Dict[str, Any]) -> Dict[str, Any]:
        return {"success": bool(success), "message": message, "data": data}

    # ---------------------------------------------------------------- 快照

    def snapshot(self) -> List[Dict[str, Any]]:
        """读取本地榜单快照（**零网络**，供 `get_form` / `get_page` 渲染）。"""
        if not self._get_data:
            return []
        try:
            data = self._get_data(self.SNAPSHOT_KEY)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"榜单快照读取失败：{exc}")
            return []
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        if isinstance(data, dict):
            items = data.get("items")
            if isinstance(items, list):
                return [item for item in items if isinstance(item, dict)]
        return []

    def refresh_snapshot(self, sources: Optional[List[str]] = None) -> Dict[str, Any]:
        """后台预热榜单缓存。**永不抛异常**，供 cron 服务调用。"""
        summary: Dict[str, Any] = {"sources": [], "total": 0, "ok": 0}
        if self._client is None:
            return summary
        for source_id in list(sources or []):
            entry = {"source": source_id, "success": False, "count": 0,
                     "stale": False, "error": ""}
            try:
                result = self._client.browse(source_id, page=1, refresh=True) or {}
                entry["count"] = len(result.get("items") or [])
                entry["stale"] = bool(result.get("stale"))
                entry["success"] = bool(result.get("success")) and not entry["stale"]
                entry["error"] = result.get("error") or result.get("message") or ""
            except Exception as exc:  # noqa: BLE001
                entry["error"] = str(exc)
            summary["sources"].append(entry)
        summary["total"] = len(summary["sources"])
        summary["ok"] = sum(1 for item in summary["sources"] if item["success"])
        if summary["total"] and summary["ok"] != summary["total"]:
            logger.warning(
                f"榜单后台刷新：{summary['ok']}/{summary['total']} 个来源成功，"
                f"失败来源={[i['source'] for i in summary['sources'] if not i['success']]}"
            )
        return summary
