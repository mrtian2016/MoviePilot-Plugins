"""自动订阅的规范候选、媒体身份和结果模型。"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


def _text(value: Any) -> str:
    return " ".join(str(value or "").strip().split())


def _type_value(value: Any) -> str:
    raw = getattr(value, "value", value)
    value = str(raw or "").strip().lower()
    if value in {"movie", "电影", "film", "films"}:
        return "movie"
    if value in {"tv", "电视剧", "show", "series"}:
        return "tv"
    return value


@dataclass(frozen=True)
class MediaIdentity:
    """跨榜单稳定媒体身份；电视剧身份始终包含目标季。"""

    media_type: str
    source: str
    media_id: str
    season: Optional[int] = None

    def key(self) -> tuple[str, str, str, Optional[int]]:
        return self.media_type, self.source, self.media_id, self.season


def media_identity(
        *,
        media_type: Any = "",
        source: Any = "",
        media_id: Any = "",
        season: Any = None,
        title: Any = "",
        year: Any = "",
) -> MediaIdentity:
    """构造身份。无强 ID 时仅生成候选归并键，不能单独用于跳过媒体。"""
    normalized_type = _type_value(media_type) or "unknown"
    normalized_source = _text(source).lower()
    normalized_id = _text(media_id)
    normalized_season = None
    if normalized_type == "tv":
        try:
            normalized_season = max(0, int(1 if season is None else season))
        except (TypeError, ValueError):
            normalized_season = 1
    if not normalized_source or not normalized_id:
        normalized_source = "title"
        normalized_id = f"{_text(title).casefold()}:{_text(year)}"
    return MediaIdentity(normalized_type, normalized_source, normalized_id, normalized_season)


class SubscribeStatus(str, Enum):
    SUBSCRIBED = "subscribed"
    MEDIA_EXISTS = "media_exists"
    SUBSCRIPTION_EXISTS = "subscription_exists"
    FILTERED = "filtered"
    UNRECOGNIZED = "unrecognized"
    ERROR = "error"


@dataclass
class MediaCandidate:
    title: str
    year: Optional[str] = None
    media_type: Optional[str] = None
    season: Optional[int] = None
    tmdb_id: Optional[int] = None
    douban_id: Optional[str] = None
    bangumi_id: Optional[int] = None
    imdb_id: Optional[str] = None
    source: str = ""
    source_meta: dict[str, Any] = field(default_factory=dict)
    unique_seed: str = ""
    vote_average: float = 0.0

    def __post_init__(self) -> None:
        self.title = _text(self.title)
        self.year = _text(self.year) or None
        self.media_type = _type_value(self.media_type) or None
        try:
            self.vote_average = float(self.vote_average or 0.0)
        except (TypeError, ValueError):
            self.vote_average = 0.0
        if self.media_type == "tv":
            try:
                self.season = max(0, int(1 if self.season is None else self.season))
            except (TypeError, ValueError):
                self.season = 1

    @property
    def identity(self) -> MediaIdentity:
        source, media_id = "", ""
        if self.tmdb_id:
            source, media_id = "tmdb", str(self.tmdb_id)
        elif self.douban_id:
            source, media_id = "douban", str(self.douban_id)
        elif self.bangumi_id:
            source, media_id = "bangumi", str(self.bangumi_id)
        elif self.imdb_id:
            source, media_id = "imdb", str(self.imdb_id)
        return media_identity(
            media_type=self.media_type,
            source=source,
            media_id=media_id,
            season=self.season,
            title=self.title,
            year=self.year,
        )

    def key(self) -> str:
        return f"{self.source}:{self.unique_seed or self.title.casefold()}"


@dataclass
class SubscribeOutcome:
    status: SubscribeStatus
    candidate: MediaCandidate
    reason: str = ""
    subscribe_id: Optional[int] = None
    identity: Optional[MediaIdentity] = None
    mediainfo: Any = None
