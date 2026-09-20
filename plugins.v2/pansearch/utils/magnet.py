"""Magnet URI 元数据解析。"""

import base64
import hashlib
import io
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

import httpx
from app.core.config import settings
from app.core.metainfo import MetaInfo
from torf import Magnet, Torrent, TorfError

from .cache import create_platform_ttl_cache
from .file_parser import MediaFileParser

_TORRENT_CACHE_TTL = 30 * 60
DEFAULT_METADATA_URL_TEMPLATE = "https://itorrents.org/torrent/{info_hash}.torrent"
_metadata_url_template = DEFAULT_METADATA_URL_TEMPLATE
_TORRENT_REQUEST_INTERVAL = 1.0
_TORRENT_REQUEST_LOCK = threading.Lock()
_TORRENT_LAST_REQUEST_AT = 0.0
_TORRENT_METADATA_CACHE = create_platform_ttl_cache(
    "magnet:metadata",
    maxsize=256,
    ttl=_TORRENT_CACHE_TTL,
)


def configure_magnet_metadata_url(url_template: str) -> str:
    """配置统一的 torrent 元数据地址模板。"""
    global _metadata_url_template
    value = str(url_template or "").strip()
    if (
            "{info_hash}" not in value
            or not value.lower().startswith(("http://", "https://"))
    ):
        value = DEFAULT_METADATA_URL_TEMPLATE
    _metadata_url_template = value
    return value


def clear_magnet_metadata_cache() -> int:
    """清理 Magnet 元数据缓存并返回移除数量。"""
    count = len(_TORRENT_METADATA_CACHE)
    _TORRENT_METADATA_CACHE.clear()
    return count


def _extract_preview_episodes(
        display_name: str,
        provider_text: str,
        torrent_files: list,
        default_season: Optional[int] = None,
) -> Dict[str, list]:
    """使用元数据识别汇总资源包含的季集。"""
    episodes: Dict[str, set] = {}
    resource_seasons = (
        [max(1, int(default_season))]
        if default_season is not None else []
    )
    resource_texts = list(dict.fromkeys(
        text for text in (
            str(display_name or "").strip(),
            str(provider_text or "").strip(),
        ) if text
    ))
    for resource_title in resource_texts:
        resource_meta = MetaInfo(
            title=resource_title,
        )
        parsed_seasons = (
            resource_meta.season_list
            if resource_meta.begin_season is not None
            else resource_seasons
        )
        if resource_meta.begin_season is not None:
            resource_seasons = resource_meta.season_list
        if resource_meta.episode_list:
            for season in parsed_seasons:
                episodes.setdefault(str(season), set()).update(resource_meta.episode_list)

    for file in torrent_files:
        file_path = Path(str(file))
        if file_path.suffix.lower() not in settings.RMT_MEDIAEXT:
            continue
        file_meta = MetaInfo(file_path.name)
        if not file_meta.episode_list:
            continue
        file_seasons = (
            file_meta.season_list
            if file_meta.begin_season is not None
            else resource_seasons
        )
        for season in file_seasons:
            episodes.setdefault(str(season), set()).update(file_meta.episode_list)

    return {season: sorted(values) for season, values in episodes.items()}


def _fetch_torrent_metadata(
        info_hash: str, timeout: float, url_template: str
) -> Dict[str, Any]:
    """从固定 torrent 缓存读取并校验元数据，不请求任意提供者 URL。"""
    cache_key = hashlib.sha1(
        f"{url_template}|{info_hash}".encode("utf-8")
    ).hexdigest()
    cached = _TORRENT_METADATA_CACHE.get(cache_key)
    if isinstance(cached, dict):
        return dict(cached)
    global _TORRENT_LAST_REQUEST_AT
    with _TORRENT_REQUEST_LOCK:
        wait_seconds = _TORRENT_REQUEST_INTERVAL - (
                time.monotonic() - _TORRENT_LAST_REQUEST_AT
        )
        if wait_seconds > 0:
            time.sleep(wait_seconds)
        _TORRENT_LAST_REQUEST_AT = time.monotonic()
    metadata: Dict[str, Any] = {}
    try:
        with httpx.stream(
                "GET",
                url_template.format(info_hash=info_hash),
                headers={"User-Agent": "MoviePilot-PanSearch/2.0"},
                timeout=max(1, float(timeout)),
                follow_redirects=True,
        ) as response:
            response.raise_for_status()
            chunks = []
            total_size = 0
            for chunk in response.iter_bytes():
                total_size += len(chunk)
                if total_size > 10 * 1024 * 1024:
                    raise ValueError("torrent 元数据响应过大")
                chunks.append(chunk)
            payload = b"".join(chunks)
        if not payload or len(payload) > 10 * 1024 * 1024:
            raise ValueError("torrent 元数据响应为空或过大")
        torrent = Torrent.read_stream(io.BytesIO(payload), validate=True)
        if str(torrent.infohash or "").upper() != info_hash:
            raise ValueError("torrent 元数据 Info Hash 不匹配")
        file_entries = []
        seen_paths = set()
        for file in torrent.files:
            path = str(file).replace("\\", "/").strip("/")
            path_key = path.casefold()
            if not path or path_key in seen_paths:
                continue
            seen_paths.add(path_key)
            season_episode = MediaFileParser.extract_season_episode(Path(path).name)
            file_entries.append({
                "path": path,
                "size": int(getattr(file, "size", 0) or 0),
                "season": season_episode[0] if season_episode else 0,
                "episode": season_episode[1] if season_episode else 0,
            })
        filepaths = [entry["path"] for entry in file_entries]
        if not filepaths and torrent.name:
            filepaths = [str(torrent.name)]
        metadata = {
            "display_name": str(torrent.name or "").strip(),
            "size": int(torrent.size or 0),
            "torrent_files": filepaths,
            "torrent_file_entries": file_entries,
            "metadata_source": "itorrents",
        }
    except (OSError, httpx.HTTPError, TorfError, ValueError):
        metadata = {}
    _TORRENT_METADATA_CACHE[cache_key] = dict(metadata)
    return metadata


def parse_magnet_metadata(
        uri: str,
        provider_text: str = "",
        fetch_info: bool = False,
        timeout: float = 8,
        default_season: Optional[int] = None,
) -> Dict[str, Any]:
    """解析 URI 和提供者文本；可按需短时获取完整 torrent 元数据。"""
    try:
        magnet = Magnet.from_string(str(uri or "").strip())
    except (TorfError, TypeError, ValueError):
        return {}

    info_hash = str(magnet.infohash or "").upper()
    if len(info_hash) == 32:
        try:
            info_hash = base64.b16encode(base64.b32decode(info_hash)).decode("ascii")
        except (ValueError, TypeError):
            return {}

    torrent_files = []
    torrent_file_entries = []
    metadata_source = "uri" if magnet.dn else ""
    if fetch_info:
        fetched = _fetch_torrent_metadata(
            info_hash,
            timeout,
            _metadata_url_template,
        )
        if fetched:
            if fetched["display_name"]:
                magnet.dn = fetched["display_name"]
            if fetched["size"]:
                # 已校验 InfoHash 的 torrent 元数据优先于 URI 中可能缺失
                # 或不可信的 xl 参数。
                magnet.xl = fetched["size"]
            torrent_files = fetched["torrent_files"]
            torrent_file_entries = fetched.get("torrent_file_entries") or []
            metadata_source = fetched["metadata_source"]

    provider_text = str(provider_text or "").strip()
    display_name = str(magnet.dn or "").strip()
    preview_episodes = _extract_preview_episodes(
        display_name=display_name,
        provider_text=provider_text,
        torrent_files=torrent_files,
        default_season=default_season,
    )

    return {
        "info_hash": info_hash,
        "display_name": display_name,
        "size": int(magnet.xl or 0),
        "trackers": [str(value) for value in magnet.tr],
        "webseeds": [str(value) for value in magnet.ws],
        "keywords": list(magnet.kt or []),
        "torrent_files": torrent_files,
        "torrent_file_entries": torrent_file_entries,
        "metadata_available": bool(magnet.dn or torrent_files),
        "provider_metadata_available": bool(provider_text),
        "metadata_source": metadata_source,
        "preview_episodes": preview_episodes,
    }
