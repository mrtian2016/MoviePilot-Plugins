"""HDHive 页面、资源字段、链接和剧集信息解析。"""

import html
import json
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import (
    parse_qsl,
    unquote,
    urlencode,
    urljoin,
    urlparse,
    urlsplit,
    urlunsplit,
)

from ...matching import unique_texts
from ...types import (
    SUPPORTED_CLOUD_TYPES,
    SUPPORTED_RESOURCE_TYPES,
    normalize_resource_type,
    resource_type_from_url,
)
from ....utils import MediaFileParser

NEXT_REDIRECT_RE = re.compile(
    r"NEXT_REDIRECT;(?:replace|push);"
    r"((?:\\/|/)(?:resource(?:\\/|/)[A-Za-z0-9_-]+(?:\\/|/)[A-Za-z0-9_-]+|"
    r"resource(?:\\/|/)[A-Za-z0-9_-]+|movie(?:\\/|/)[A-Za-z0-9_-]+|"
    r"tv(?:\\/|/)[A-Za-z0-9_-]+));[0-9]{3};",
    re.I,
)
NEXT_SCRIPT_RE = re.compile(
    r"self\.__next_f\.push\((\[.*?\])\)</script>", re.S
)
EMBEDDED_ESCAPE_RE = re.compile(
    r"\\+(u003a|u002f|u0026|u003d|u003f|u002b|/|[\"'])", re.I
)
EMBEDDED_ESCAPE_VALUES = {
    "u003a": ":", "u002f": "/", "u0026": "&", "u003d": "=",
    "u003f": "?", "u002b": "+", "/": "/", '"': '"', "'": "'",
}
FILE_PREVIEW_CAPABILITY_RE = re.compile(
    r'["\']fileListPreviewEnabled["\']\s*:\s*(true|false)', re.I
)
NO_RESOURCE_MARKERS = ("暂无资源", "暂时没有资源", "尚无资源")
CHALLENGE_MARKERS = (
    "cf-chl-", "challenge-platform", "captcha", "访问频繁",
    "页面过期", "请刷新页面", "安全验证",
)
ED2K_URL_RE = re.compile(
    r"ed2k://\|file\|[^|\r\n]+\|\d+\|[0-9A-Fa-f]{32}"
    r"(?:\|(?:h|p)=[^|\r\n]+)*\|/",
    re.I,
)
HDHIVE_RESOURCE_TYPES = frozenset(SUPPORTED_RESOURCE_TYPES)
HDHIVE_DETAIL_RESOURCE_TYPES = HDHIVE_RESOURCE_TYPES - {"magnet"}


def response_body(response: Any) -> bytes:
    content = getattr(response, "content", b"") or b""
    if isinstance(content, (bytes, bytearray, memoryview)):
        body = bytes(content)
    else:
        body = str(content).encode("utf-8", errors="replace")
    return body or str(getattr(response, "text", "") or "").encode(
        "utf-8", errors="replace"
    )


def response_text(response: Any) -> str:
    body = response_body(response)
    return body.decode("utf-8", errors="replace") if body else ""


def decode_embedded_text(value: Any) -> str:
    text = str(value or "")
    for _ in range(4):
        decoded = html.unescape(unquote(text))
        decoded = EMBEDDED_ESCAPE_RE.sub(
            lambda match: EMBEDDED_ESCAPE_VALUES[match.group(1).lower()],
            decoded,
        )
        if decoded == text:
            break
        text = decoded
    return text


def page_account_snapshot(page_text: str) -> Dict[str, Any]:
    """从首页/Action 的 Flight 内容读取用户可见账户快照。 """

    text = decode_embedded_text(page_text)
    decoder = json.JSONDecoder()
    for match in re.finditer(r'["\']user_meta["\']\s*:', text):
        start = text.find("{", match.end())
        if start < 0:
            continue
        try:
            value, _ = decoder.raw_decode(text, start)
        except (TypeError, ValueError):
            continue
        if not isinstance(value, dict):
            continue
        snapshot: Dict[str, Any] = {}
        for source, target in (
                ("points", "points"),
                ("signin_days_total", "signin_days"),
                ("signin_days", "signin_days"),
        ):
            if source in value:
                snapshot[target] = value[source]
        if snapshot:
            return snapshot
    return {}


def _nested_dict(value: Any, key: str) -> Optional[Dict[str, Any]]:
    pending = [value]
    while pending:
        current = pending.pop()
        if isinstance(current, dict):
            nested = current.get(key)
            if isinstance(nested, dict):
                return nested
            pending.extend(current.values())
        elif isinstance(current, list):
            pending.extend(current)
    return None


def resource_detail_path(response: Any) -> str:
    headers = getattr(response, "headers", {}) or {}
    current_path = str(
        headers.get("x-current-path")
        or headers.get("x-current-url")
        or ""
    ).strip().split("?", 1)[0]
    if re.fullmatch(
            r"/(?:resource/(?:[A-Za-z0-9_-]+/)?[A-Za-z0-9_-]+|"
            r"tv/[A-Za-z0-9_-]+|movie/[A-Za-z0-9_-]+)",
            current_path,
            re.I,
    ):
        return current_path
    match = NEXT_REDIRECT_RE.search(response_text(response))
    return decode_embedded_text(match.group(1)) if match else ""


def resource_group_data(page_text: str) -> Optional[Dict[str, Any]]:
    decoder = json.JSONDecoder()
    for script_match in NEXT_SCRIPT_RE.finditer(page_text or ""):
        try:
            payload = json.loads(script_match.group(1))
        except (TypeError, ValueError):
            continue
        fragments = payload if isinstance(payload, list) else [payload]
        for fragment in fragments:
            if not isinstance(fragment, str) or "groupData" not in fragment:
                continue
            for start, char in enumerate(fragment):
                if char not in "[{":
                    continue
                try:
                    parsed, _ = decoder.raw_decode(fragment, start)
                except ValueError:
                    continue
                group_data = _nested_dict(parsed, "groupData")
                if group_data is not None:
                    return group_data
    if any(marker in (page_text or "") for marker in NO_RESOURCE_MARKERS):
        return {}
    return None


def is_challenge_page(page_text: str) -> bool:
    normalized = str(page_text or "").lower()
    return any(marker.lower() in normalized for marker in CHALLENGE_MARKERS)


def file_preview_capability(response: Any) -> Optional[bool]:
    match = FILE_PREVIEW_CAPABILITY_RE.search(
        decode_embedded_text(response_text(response))
    )
    return match.group(1).lower() == "true" if match else None

def preview_episodes_from_files(
        files: List[Dict[str, Any]], target_season: Optional[int]
) -> Dict[str, List[int]]:
    episodes: Dict[str, set] = {}
    fallback_season = max(1, int(target_season or 1))
    for item in files or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("path") or item.get("name") or "").strip()
        parsed = MediaFileParser.extract_season_episode(name)
        if parsed:
            episodes.setdefault(str(parsed[0]), set()).add(parsed[1])
            continue
        match = re.search(r"(?i)(?:^|[^A-Za-z0-9])EP?0*(\d{1,4})(?!\d)", name)
        if not match:
            match = re.search(r"第\s*(\d{1,4})\s*集", name)
        if match:
            episodes.setdefault(str(fallback_season), set()).add(
                int(match.group(1))
            )
    return {
        season: sorted(values)
        for season, values in episodes.items()
        if values
    }


def valid_share_url(value: str, resource_type_value: str) -> bool:
    candidate = str(value or "").strip()
    if not candidate or "\\" in candidate or any(
            ord(character) < 32 for character in candidate
    ):
        return False
    if resource_type_value == "ed2k":
        return bool(ED2K_URL_RE.fullmatch(candidate))
    try:
        parsed = urlparse(candidate)
        hostname = parsed.hostname
    except ValueError:
        return False
    valid = (
            parsed.scheme.lower() in {"http", "https"}
            and bool(hostname)
            and resource_type_from_url(candidate) == resource_type_value
    )
    if not valid or resource_type_value != "115":
        return valid
    share_code = parsed.path.rstrip("/").rsplit("/", 1)[-1]
    receive_code = dict(parse_qsl(parsed.query)).get("password", "")
    return bool(
        re.fullmatch(r"[A-Za-z0-9]+", share_code)
        and re.fullmatch(r"[A-Za-z0-9]{4}", receive_code)
    )


def normalize_share_url(
        value: Any, resource_type_value: str, access_code: Any = ""
) -> str:
    candidate = decode_embedded_text(value).strip().rstrip("),.;]}")
    if resource_type_value == "115" and candidate:
        parsed = urlsplit(candidate)
        query = parse_qsl(parsed.query, keep_blank_values=True)
        embedded_code = next((
            item for key, item in query
            if key.lower() in {"password", "pwd", "receive_code"}
        ), "")
        code = decode_embedded_text(access_code or embedded_code).strip()
        if re.fullmatch(r"[A-Za-z0-9]{4}", code):
            query = [
                (key, item) for key, item in query
                if key.lower() not in {"password", "pwd", "receive_code"}
            ]
            query.append(("password", code))
            candidate = urlunsplit(parsed._replace(query=urlencode(query)))
    return candidate if valid_share_url(candidate, resource_type_value) else ""


def share_url_from_values(values: Any, resource_type_value: str) -> str:
    pending = [values]
    while pending:
        value = pending.pop()
        if isinstance(value, dict):
            access_code = next((
                item for key, item in value.items()
                if str(key).replace("_", "").lower()
                   in {"password", "pwd", "receivecode"}
            ), "")
            for item in value.values():
                if isinstance(item, (dict, list, tuple, set)):
                    continue
                normalized = normalize_share_url(
                    item, resource_type_value, access_code
                )
                if normalized:
                    return normalized
            pending.extend(value.values())
            continue
        if isinstance(value, (list, tuple, set)):
            pending.extend(value)
            continue
        text = decode_embedded_text(value)
        if resource_type_value == "ed2k":
            match = ED2K_URL_RE.search(text)
            if match:
                normalized = normalize_share_url(
                    match.group(0), resource_type_value
                )
                if normalized:
                    return normalized
        for match in re.finditer(r"https?://[^\s\\\"'<>]+", text, re.I):
            if match.end() < len(text) and text[match.end()] == "\\":
                continue
            candidate = match.group(0).rstrip("),.;]}")
            access_code = ""
            if resource_type_value == "115":
                nearby = text[max(0, match.start() - 256):match.end() + 256]
                code_match = re.search(
                    r'["\'](?:password|pwd|receive_?code)["\']\s*[:=]\s*'
                    r'["\']([A-Za-z0-9]{4})["\']',
                    nearby,
                    re.I,
                )
                access_code = code_match.group(1) if code_match else ""
            normalized = normalize_share_url(
                candidate, resource_type_value, access_code
            )
            if normalized:
                return normalized
    return ""


def resource_slug(row: Dict[str, Any]) -> str:
    return str(row.get("slug") or "").strip()


def deduplicate(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    result = []
    seen = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        slug = resource_slug(row)
        if slug and slug not in seen:
            seen.add(slug)
            result.append(row)
    return result


def flatten_group_data(group_data: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for values in (group_data or {}).values():
        if isinstance(values, list):
            rows.extend(dict(value) for value in values if isinstance(value, dict))
    return deduplicate(rows)


def normalize_languages(values: Any) -> List[str]:
    if isinstance(values, str):
        values = re.split(r"[,，\s]+", values)
    return unique_texts(values, lambda value: value.lower().replace("_", "-"))


def torrentclaw_rows(
        value: Any, base_url: str = "https://re0.me"
) -> List[Dict[str, Any]]:
    result = value.get("result") if isinstance(value, dict) else None
    if not isinstance(result, dict):
        return []
    content_url = str(result.get("contentUrl") or "").strip()
    source_url = urljoin(f"{base_url}/", content_url) if content_url else ""
    rows = []
    for torrent in result.get("torrents") or []:
        if not isinstance(torrent, dict):
            continue
        magnet_url = str(torrent.get("magnetUrl") or "").strip()
        if not magnet_url.lower().startswith("magnet:?"):
            continue
        rows.append({
            "website": "magnet",
            "url": magnet_url,
            "slug": str(torrent.get("infoHash") or "").strip(),
            "title": str(
                torrent.get("rawTitle") or result.get("title")
                or "HDHive Magnet资源"
            ),
            "quality": torrent.get("quality") or "",
            "codec": torrent.get("codec") or "",
            "source_type": torrent.get("sourceType") or "",
            "audio_codec": torrent.get("audioCodec") or "",
            "audio_channels": torrent.get("audioChannels"),
            "hdr_type": torrent.get("hdrType") or "",
            "release_group": torrent.get("releaseGroup") or "",
            "video_info": torrent.get("videoInfo") or {},
            "size": torrent.get("sizeBytes") or 0,
            "size_bytes": torrent.get("sizeBytes") or 0,
            "seeders": torrent.get("seeders") or 0,
            "leechers": torrent.get("leechers") or 0,
            "source": torrent.get("source") or "torrentclaw",
            "quality_score": torrent.get("qualityScore"),
            "created_at": torrent.get("uploadedAt") or "",
            "languages": torrent.get("languages") or [],
            "subtitle_languages": torrent.get("subtitleLanguages") or [],
            "subtitle": ", ".join(
                str(language).strip()
                for language in (torrent.get("subtitleLanguages") or [])
                if str(language).strip()
            ),
            "season": torrent.get("season"),
            "episode": torrent.get("episode"),
            "is_free": True,
            "is_unlocked": True,
            "unlock_points": 0,
            "source_url": source_url,
        })
    return rows


def resource_timestamp(value: Any) -> float:
    text = str(value or "").strip()
    if not text:
        return 0
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except ValueError:
        return 0


def resource_update_time(row: Dict[str, Any]) -> str:
    return str(
        row.get("updated_at")
        or row.get("posted_at")
        or row.get("created_at")
        or ""
    )


def _episode_range(start: int, end: int) -> List[int]:
    if start <= 0 or end < start or end - start > 500:
        return []
    return list(range(start, end + 1))


def preview_episodes_from_row(
        row: Dict[str, Any], target_season: Optional[int]
) -> Dict[str, List[int]]:
    """从 HDHive 卡片 remark 提取季集范围。"""
    text = str(row.get("remark") or "").strip()
    if not text:
        return {}
    episodes: Dict[str, set] = {}
    for match in re.finditer(
            r"S(?P<season>\d{1,3})\s*E(?P<start>\d{1,4})"
            r"(?:\s*(?:-|~|—|至)\s*(?:S(?P<end_season>\d{1,3})\s*)?"
            r"E?(?P<end>\d{1,4}))?",
            text,
            re.I,
    ):
        season = int(match.group("season"))
        end_season = int(match.group("end_season") or season)
        if season <= 0 or season != end_season:
            continue
        start = int(match.group("start"))
        values = _episode_range(start, int(match.group("end") or start))
        if values:
            episodes.setdefault(str(season), set()).update(values)
    fallback_season = max(1, int(target_season or 1))
    for match in re.finditer(
            r"(?<![A-Za-z0-9])(?P<start>\d{1,4})\s*"
            r"(?:-|~|—|至)\s*(?P<end>\d{1,4})\s*集",
            text,
    ):
        values = _episode_range(
            int(match.group("start")), int(match.group("end"))
        )
        if values:
            episodes.setdefault(str(fallback_season), set()).update(values)
    for match in re.finditer(r"(?:更新至|更至)\s*(\d{1,4})\s*集", text):
        values = _episode_range(1, int(match.group(1)))
        if values:
            episodes.setdefault(str(fallback_season), set()).update(values)
    for match in re.finditer(r"第\s*(\d{1,4})\s*集", text):
        episode = int(match.group(1))
        if episode > 0:
            episodes.setdefault(str(fallback_season), set()).add(episode)
    return {
        season: sorted(values)
        for season, values in episodes.items()
        if values
    }


def earliest_target_air_time(
        target_episodes: set, episode_air_dates: Dict[int, str]
) -> float:
    values = [
        resource_timestamp(episode_air_dates.get(episode))
        for episode in target_episodes
        if episode_air_dates.get(episode)
    ]
    return min((value for value in values if value > 0), default=0)


def unlock_points(row: Dict[str, Any]) -> int:
    try:
        return max(0, int(row.get("unlock_points") or 0))
    except (TypeError, ValueError):
        return 0


def is_free_resource(row: Dict[str, Any]) -> bool:
    return bool(row.get("is_unlocked")) or (
            row.get("unlock_points") is not None and unlock_points(row) == 0
    )


def resource_type(row: Dict[str, Any]) -> str:
    website = normalize_resource_type(row.get("website") or "")
    return website if website in HDHIVE_RESOURCE_TYPES else ""


def file_preview_capability_from_row(
        row: Optional[Dict[str, Any]] = None
) -> Optional[bool]:
    """读取站点显式能力字段；缺失时返回未知，不按网盘类型猜测。"""
    if not isinstance(row, dict):
        return None
    for key in (
            "file_list_preview_enabled",
            "fileListPreviewEnabled",
            "supports_file_list",
            "supportsFileList",
    ):
        if key not in row or row.get(key) is None:
            continue
        value = row.get(key)
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)
    return None


def build_resource_detail_path(
        resource_type_value: str, slug: str, route_type: Any = ""
) -> str:
    normalized_type = normalize_resource_type(resource_type_value)
    normalized_slug = str(slug or "").strip()
    if (
            normalized_type not in HDHIVE_DETAIL_RESOURCE_TYPES
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", normalized_slug)
    ):
        raise ValueError("HDHive 资源类型或详情标识无效")
    segments = ["resource"]
    if normalized_type in SUPPORTED_CLOUD_TYPES:
        raw_route_type = str(route_type or normalized_type).strip().lower()
        if normalize_resource_type(raw_route_type) == normalized_type:
            segments.append(raw_route_type)
    segments.append(normalized_slug)
    return "/" + "/".join(segments)


def resolve_resource_detail_path(
        resource_type_value: str,
        slug: str,
        detail_path: str = "",
        base_url: str = "https://re0.me",
) -> str:
    """校验并复用卡片详情路径，缺失时按资源类别推导。"""
    normalized_type = normalize_resource_type(resource_type_value)
    normalized_slug = str(slug or "").strip()
    candidate = str(detail_path or "").strip()
    if not candidate:
        return build_resource_detail_path(normalized_type, normalized_slug)
    parsed = urlparse(urljoin(f"{base_url}/", candidate))
    expected_host = urlparse(base_url).hostname
    if (
            parsed.scheme not in {"http", "https"}
            or parsed.hostname != expected_host
    ):
        raise ValueError("HDHive 资源详情地址无效")
    parts = [part for part in parsed.path.split("/") if part]
    valid = (
            normalized_type in HDHIVE_DETAIL_RESOURCE_TYPES
            and len(parts) in {2, 3}
            and parts[0] == "resource"
            and parts[-1] == normalized_slug
    )
    if len(parts) == 3:
        valid = (
                valid
                and normalized_type in SUPPORTED_CLOUD_TYPES
                and normalize_resource_type(parts[1]) == normalized_type
        )
    if not valid:
        raise ValueError("HDHive 资源详情地址与资源类型不匹配")
    return parsed.path
