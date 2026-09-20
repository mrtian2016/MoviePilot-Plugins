"""MoviePilot v2/v3 媒体身份兼容层。

v2 以 ``tmdbid``、``doubanid`` 等独立字段传递媒体身份，v3 则统一使用
``media_source`` 与 ``media_id``。本模块先把两种对象归一为统一身份，再根据
运行时方法签名和 ORM 模型字段选择当前版本实际支持的参数，业务层无需判断版本号。
"""

from __future__ import annotations

import inspect
from typing import Any, Dict, Iterable, List, Optional, Tuple

TMDB_SOURCE = "themoviedb"

_SOURCE_ALIASES = {
    "tmdb": TMDB_SOURCE,
    "themoviedb": TMDB_SOURCE,
    "douban": "douban",
    "bangumi": "bangumi",
    "anilist": "anilist",
    "imdb": "imdb",
    "tvdb": "tvdb",
}
_SOURCE_ID_FIELDS = (
    (TMDB_SOURCE, ("tmdb_id", "tmdbid")),
    ("douban", ("douban_id", "doubanid")),
    ("bangumi", ("bangumi_id", "bangumiid")),
    ("anilist", ("anilist_id", "anilistid")),
    ("imdb", ("imdb_id", "imdbid")),
    ("tvdb", ("tvdb_id", "tvdbid")),
)
_LEGACY_ARGUMENTS = {
    TMDB_SOURCE: "tmdbid",
    "douban": "doubanid",
    "bangumi": "bangumiid",
    "anilist": "anilistid",
}


def _read(value: Any, field: str) -> Any:
    if isinstance(value, dict):
        return value.get(field)
    return getattr(value, field, None)


def _present(value: Any) -> bool:
    return value not in (None, "", 0, "0")


def normalize_media_source(value: Any) -> Optional[str]:
    """返回 v2/v3 均可识别的稳定媒体来源值。"""
    normalized = str(getattr(value, "value", value) or "").strip().casefold()
    return _SOURCE_ALIASES.get(normalized, normalized or None)


def media_identity(value: Any) -> Tuple[Optional[str], Optional[str]]:
    """读取规范媒体身份；缺失时回退到 v2 的独立 ID 字段。"""
    source = normalize_media_source(
        _read(value, "media_source") or _read(value, "source")
    )
    media_id = _read(value, "media_id") or _read(value, "mediaid")
    if source and _present(media_id):
        return source, str(media_id).strip()

    for candidate_source, fields in _SOURCE_ID_FIELDS:
        for field in fields:
            candidate_id = _read(value, field)
            if _present(candidate_id):
                return candidate_source, str(candidate_id).strip()
    return None, None


def apply_media_identity(
        value: Any, media_source: Any, media_id: Any
) -> Any:
    """按照当前 MediaInfo 暴露的字段名写入规范媒体身份。"""
    source = normalize_media_source(media_source)
    normalized_id = str(media_id).strip() if _present(media_id) else None
    if not source or not normalized_id:
        return value
    if hasattr(value, "media_source"):
        setattr(value, "media_source", source)
    elif hasattr(value, "source"):
        setattr(value, "source", source)
    if hasattr(value, "media_id"):
        setattr(value, "media_id", normalized_id)
    elif hasattr(value, "mediaid"):
        setattr(value, "mediaid", normalized_id)
    return value


def legacy_media_ids(value: Any) -> Dict[str, Any]:
    """读取 v2 独立 ID；必要时从 v3 规范身份反向推导。"""
    result: Dict[str, Any] = {}
    for source, fields in _SOURCE_ID_FIELDS:
        for field in fields:
            candidate_id = _read(value, field)
            if _present(candidate_id):
                argument = _LEGACY_ARGUMENTS.get(source)
                if argument:
                    result[argument] = candidate_id
                break

    source, media_id = media_identity(value)
    argument = _LEGACY_ARGUMENTS.get(source or "")
    if argument and argument not in result and _present(media_id):
        result[argument] = media_id
    return result


def media_id_of(value: Any, media_source: Any) -> Optional[str]:
    """从 v2/v3 对象中读取指定来源的媒体 ID。"""
    expected_source = normalize_media_source(media_source)
    source, media_id = media_identity(value)
    if source == expected_source and media_id:
        return media_id
    for candidate_source, fields in _SOURCE_ID_FIELDS:
        if candidate_source != expected_source:
            continue
        for field in fields:
            candidate_id = _read(value, field)
            if _present(candidate_id):
                return str(candidate_id).strip()
    return None


def tmdb_id_of(value: Any) -> Optional[int]:
    """从 v2 模型或 v3 规范身份中读取 TMDB ID。"""
    candidate = media_id_of(value, TMDB_SOURCE)
    if not _present(candidate):
        return None
    try:
        parsed = int(candidate)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def tmdb_identity_update(model_or_row: Any, tmdb_id: Any) -> Dict[str, Any]:
    """根据当前 Subscribe 模型生成可接受的 TMDB 身份更新字段。"""
    parsed_id = tmdb_id_of({"tmdb_id": tmdb_id})
    if not parsed_id:
        return {}
    payload: Dict[str, Any] = {
        "media_source": TMDB_SOURCE,
        "media_id": str(parsed_id),
    }
    target = model_or_row if isinstance(model_or_row, type) else type(model_or_row)
    if hasattr(target, "tmdbid"):
        payload["tmdbid"] = parsed_id
    return payload


def _parameters(callable_obj: Any) -> Dict[str, inspect.Parameter]:
    try:
        return dict(inspect.signature(callable_obj).parameters)
    except (TypeError, ValueError):
        return {}


def _accepted_kwargs(callable_obj: Any, kwargs: Dict[str, Any]) -> Dict[str, Any]:
    parameters = _parameters(callable_obj)
    if any(item.kind == inspect.Parameter.VAR_KEYWORD for item in parameters.values()):
        return kwargs
    return {key: value for key, value in kwargs.items() if key in parameters}


def call_with_supported_kwargs(callable_obj: Any, kwargs: Dict[str, Any]) -> Any:
    """只保留当前 v2/v3 方法声明的参数后再调用。"""
    return callable_obj(**_accepted_kwargs(callable_obj, kwargs))


def recognize_media(
        chain: Any,
        *,
        meta: Any = None,
        mtype: Any = None,
        media_source: Any = None,
        media_id: Any = None,
        tmdb_id: Any = None,
        douban_id: Any = None,
        bangumi_id: Any = None,
        anilist_id: Any = None,
        episode_group: Any = None,
        cache: bool = True,
        share_meta: Any = None,
        **legacy_kwargs: Any,
) -> Any:
    """按运行时签名使用 v2 或 v3 原生身份契约调用媒体识别。"""
    media_source = media_source or legacy_kwargs.get("source")
    media_id = media_id or legacy_kwargs.get("mediaid")
    tmdb_id = tmdb_id or legacy_kwargs.get("tmdbid")
    douban_id = douban_id or legacy_kwargs.get("doubanid")
    bangumi_id = bangumi_id or legacy_kwargs.get("bangumiid")
    anilist_id = anilist_id or legacy_kwargs.get("anilistid")
    method = chain.recognize_media
    parameters = _parameters(method)
    kwargs: Dict[str, Any] = {
        "meta": meta,
        "mtype": mtype,
        "episode_group": episode_group,
        "cache": cache,
        "share_meta": share_meta,
    }
    explicit_ids = {
        "tmdbid": tmdb_id,
        "doubanid": douban_id,
        "bangumiid": bangumi_id,
        "anilistid": anilist_id,
    }

    if "media_source" in parameters:
        source = normalize_media_source(media_source)
        normalized_id = str(media_id).strip() if _present(media_id) else None
        if not source or not normalized_id:
            for legacy_name, value in explicit_ids.items():
                if not _present(value):
                    continue
                source = next(
                    key for key, argument in _LEGACY_ARGUMENTS.items()
                    if argument == legacy_name
                )
                normalized_id = str(value).strip()
                break
        if source and normalized_id:
            kwargs.update({"media_source": source, "media_id": normalized_id})
    else:
        source = normalize_media_source(media_source)
        normalized_id = str(media_id).strip() if _present(media_id) else None
        legacy_name = _LEGACY_ARGUMENTS.get(source or "")
        if legacy_name and normalized_id and not _present(explicit_ids.get(legacy_name)):
            explicit_ids[legacy_name] = normalized_id
        elif source and normalized_id:
            kwargs.update({"source": source, "mediaid": normalized_id})
        kwargs.update({key: value for key, value in explicit_ids.items() if _present(value)})

    return method(**_accepted_kwargs(method, kwargs))


def list_subscribes_by_tmdb_id(
        oper: Any, tmdb_id: Any, season: Optional[int] = None
) -> List[Any]:
    """使用 v2 或 v3 当前可用的媒体身份接口查询订阅。"""
    parsed_id = tmdb_id_of({"tmdb_id": tmdb_id})
    if not parsed_id:
        return []
    legacy_method = getattr(oper, "list_by_tmdbid", None)
    if callable(legacy_method):
        return list(legacy_method(parsed_id, season) or [])

    identity_method = getattr(oper, "list_by_media_identity", None)
    if not callable(identity_method):
        return []
    rows = list(identity_method(TMDB_SOURCE, str(parsed_id)) or [])
    if season is None:
        return rows
    return [
        item for item in rows
        if int(getattr(item, "season", 0) or 0) == int(season)
    ]


def get_subscribe_by_media(
        oper: Any,
        *,
        media_type: str,
        media: Any,
        season: Optional[int] = None,
) -> Any:
    """按照当前版本的 ``get_by`` 契约查询单个订阅。"""
    method = oper.get_by
    source, media_id = media_identity(media)
    kwargs: Dict[str, Any] = {"type": media_type, "season": season}
    if "media_source" in _parameters(method) and source and media_id:
        kwargs.update({"media_source": source, "media_id": media_id})
    else:
        kwargs.update(legacy_media_ids(media))
        if source and media_id:
            kwargs.update({"media_source": source, "media_id": media_id})
    return method(**_accepted_kwargs(method, kwargs))


def download_history_identity_payload(media: Any, model: Any) -> Dict[str, Any]:
    """按照当前 DownloadHistory ORM 模型生成媒体身份字段。"""
    source, media_id = media_identity(media)
    payload: Dict[str, Any] = {}
    if hasattr(model, "media_source") and source and media_id:
        payload.update({"media_source": source, "media_id": media_id})
    for field, value in legacy_media_ids(media).items():
        if hasattr(model, field):
            payload[field] = value
    for source_name, fields in _SOURCE_ID_FIELDS:
        if source_name in _LEGACY_ARGUMENTS:
            continue
        for field in fields:
            value = _read(media, field)
            if hasattr(model, field.replace("_", "")) and _present(value):
                payload[field.replace("_", "")] = value
                break
    return payload


def get_download_history_last_by(
        oper: Any, *, mtype: str, title: Optional[str], year: Optional[str],
        tmdb_id: Any, season: Any = None, episode: Any = None,
) -> List[Any]:
    method = oper.get_last_by
    kwargs: Dict[str, Any] = {
        "mtype": mtype,
        "title": title,
        "year": year,
        "season": season,
        "episode": episode,
    }
    if "media_source" in _parameters(method) and _present(tmdb_id):
        kwargs.update({"media_source": TMDB_SOURCE, "media_id": str(tmdb_id)})
    elif _present(tmdb_id):
        kwargs["tmdbid"] = tmdb_id
    return list(method(**_accepted_kwargs(method, kwargs)) or [])


def get_transfer_history_by(
        oper: Any, *, tmdb_id: Any = None, media_source: Any = None,
        media_id: Any = None, **kwargs: Any,
) -> List[Any]:
    method = oper.get_by
    parameters = _parameters(method)
    source = normalize_media_source(media_source)
    normalized_id = str(media_id).strip() if _present(media_id) else None
    if (not source or not normalized_id) and _present(tmdb_id):
        source, normalized_id = TMDB_SOURCE, str(tmdb_id)
    if "media_source" in parameters and source and normalized_id:
        kwargs.update({"media_source": source, "media_id": normalized_id})
    elif _present(tmdb_id):
        kwargs["tmdbid"] = tmdb_id
    return list(method(**_accepted_kwargs(method, kwargs)) or [])


def media_server_tmdb_filters(model: Any, tmdb_ids: Iterable[int]) -> List[Any]:
    """按照当前 MediaServerItem 模型生成 TMDB 查询条件。"""
    normalized = {int(item) for item in tmdb_ids if int(item) > 0}
    if hasattr(model, "media_source") and not hasattr(model, "tmdbid"):
        return [
            model.media_source == TMDB_SOURCE,
            model.media_id.in_({str(item) for item in normalized}),
        ]
    return [model.tmdbid.in_(normalized)]
