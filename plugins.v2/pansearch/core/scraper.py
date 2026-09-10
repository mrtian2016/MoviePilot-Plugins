"""基于TMDB 刮削器批量生成本地媒体元数据。"""
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Dict, Iterable, Optional

from app.core.config import settings
from app.core.metainfo import MetaInfoPath
from app.modules.themoviedb.scraper import TmdbScraper
from app.schemas import MediaInfo
from app.schemas.types import MediaType
from app.utils.http import RequestUtils


class MediaScraper:
    """在平台分类目录批量生成一致的 NFO 和图片。"""

    def __init__(self, nfo_enabled: bool, image_enabled: bool):
        self._nfo_enabled = bool(nfo_enabled)
        self._image_enabled = bool(image_enabled)
        self._scraper = TmdbScraper()

    @staticmethod
    def _write_missing(path: Path, content: bytes) -> bool:
        if path.exists():
            return False
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_suffix(f"{path.suffix}.tmp")
        temp_path.write_bytes(content)
        temp_path.replace(path)
        return True

    @staticmethod
    def _has_image(directory: Path, stem: str) -> bool:
        return any(directory.glob(f"{stem}.*"))

    def _cached_image_checker(self):
        """在一次刮削批次内缓存目录图片探测结果。"""
        image_cache = {}

        def has_image(directory: Path, stem: str) -> bool:
            key = (directory, stem)
            if key not in image_cache:
                image_cache[key] = self._has_image(directory, stem)
            return image_cache[key]

        return has_image

    @staticmethod
    def _download_missing(path: Path, url: str) -> bool:
        if path.exists() or not url:
            return False
        path.parent.mkdir(parents=True, exist_ok=True)
        request = RequestUtils(proxies=settings.PROXY, ua=settings.NORMAL_USER_AGENT)
        temp_path: Optional[Path] = None
        try:
            with request.get_stream(url=url) as response:
                if not response or response.status_code != 200:
                    raise RuntimeError(f"HTTP {getattr(response, 'status_code', '-')}")
                with NamedTemporaryFile(delete=False, dir=path.parent, suffix=".tmp") as temp:
                    temp_path = Path(temp.name)
                    for chunk in response.iter_content(chunk_size=8192):
                        if chunk:
                            temp.write(chunk)
            temp_path.replace(path)
            return True
        finally:
            if temp_path and temp_path.exists():
                temp_path.unlink(missing_ok=True)

    def filter_missing_items(
            self, items: Iterable[Dict], mediainfo: MediaInfo
    ) -> list[Dict]:
        """在请求 TMDB 前筛出仍缺少目标元数据的媒体文件。"""
        normalized = [dict(item) for item in (items or []) if item.get("media_path")]
        if not normalized or not mediainfo or not getattr(mediainfo, "tmdb_id", None):
            return []
        has_image = self._cached_image_checker()

        if mediainfo.type == MediaType.MOVIE:
            result = []
            for item in normalized:
                media_path = Path(item["media_path"])
                missing_nfo = self._nfo_enabled and not media_path.with_suffix(".nfo").exists()
                missing_image = self._image_enabled and (
                        not has_image(media_path.parent, "poster")
                        or not has_image(media_path.parent, "fanart")
                )
                if missing_nfo or missing_image:
                    result.append(item)
            return result

        selected = []
        selected_ids = set()
        parsed = []
        for item in normalized:
            media_path = Path(item["media_path"])
            meta = MetaInfoPath(media_path)
            season = int(item.get("season") or meta.begin_season or 1)
            episode = int(item.get("episode") or meta.begin_episode or 0)
            parsed.append((item, media_path, season, episode))

        show_dir = parsed[0][1].parent.parent
        shared_missing = self._nfo_enabled and not (show_dir / "tvshow.nfo").exists()
        shared_missing = shared_missing or self._image_enabled and (
                not has_image(show_dir, "poster")
                or not has_image(show_dir, "fanart")
        )
        seasons_checked = set()
        for item, media_path, season, episode in parsed:
            missing = False
            if season not in seasons_checked:
                seasons_checked.add(season)
                poster_stem = (
                    "season-specials-poster" if season == 0
                    else f"season{season:02d}-poster"
                )
                missing = self._nfo_enabled and not (
                        media_path.parent / "season.nfo"
                ).exists()
                missing = missing or self._image_enabled and not has_image(
                    show_dir, poster_stem
                )
            if episode:
                missing = missing or self._nfo_enabled and not media_path.with_suffix(
                    ".nfo"
                ).exists()
                missing = missing or self._image_enabled and not has_image(
                    media_path.parent, f"{media_path.stem}-thumb"
                )
            if shared_missing or missing:
                item_id = id(item)
                if item_id not in selected_ids:
                    selected_ids.add(item_id)
                    selected.append(item)
                shared_missing = False
        return selected

    def _scrape_movie(self, media_path: Path, mediainfo: MediaInfo) -> int:
        media_path.parent.mkdir(parents=True, exist_ok=True)
        meta = MetaInfoPath(media_path)
        has_image = self._cached_image_checker()
        created = 0
        if self._nfo_enabled and not media_path.with_suffix(".nfo").exists():
            nfo = self._scraper.get_metadata_nfo(meta, mediainfo)
            if nfo:
                created += self._write_missing(media_path.with_suffix(".nfo"), nfo)
        if self._image_enabled and (
                not has_image(media_path.parent, "poster")
                or not has_image(media_path.parent, "fanart")
        ):
            for image_name, url in self._scraper.get_metadata_img(mediainfo).items():
                lower_name = image_name.lower()
                stem = "fanart" if "backdrop" in lower_name else "poster" if "poster" in lower_name else None
                if stem and not self._has_image(media_path.parent, stem):
                    created += self._download_missing(
                        media_path.parent / f"{stem}{Path(image_name).suffix}", url
                    )
        return created

    def _scrape_tv_batch(self, items: Iterable[Dict], mediainfo: MediaInfo) -> int:
        normalized = []
        for item in items:
            media_path = Path(item["media_path"])
            meta = MetaInfoPath(media_path)
            normalized.append((
                media_path,
                int(item.get("season") or meta.begin_season or 1),
                int(item.get("episode") or meta.begin_episode or 0),
                meta,
            ))
        if not normalized:
            return 0
        created = 0
        show_dir = normalized[0][0].parent.parent
        has_image = self._cached_image_checker()

        show_dir.mkdir(parents=True, exist_ok=True)
        sample_meta = normalized[0][3]
        if self._nfo_enabled and not (show_dir / "tvshow.nfo").exists():
            nfo = self._scraper.get_metadata_nfo(sample_meta, mediainfo)
            if nfo:
                created += self._write_missing(show_dir / "tvshow.nfo", nfo)
        if self._image_enabled and (
                not has_image(show_dir, "poster")
                or not has_image(show_dir, "fanart")
        ):
            for image_name, url in self._scraper.get_metadata_img(mediainfo).items():
                lower_name = image_name.lower()
                stem = "fanart" if "backdrop" in lower_name else "poster" if "poster" in lower_name else None
                if stem and not self._has_image(show_dir, stem):
                    created += self._download_missing(show_dir / f"{stem}{Path(image_name).suffix}", url)

        seasons = {}
        for media_path, season, _, meta in normalized:
            seasons.setdefault(season, (media_path.parent, meta))
        for season, (season_dir, meta) in seasons.items():
            season_dir.mkdir(parents=True, exist_ok=True)
            if self._nfo_enabled and not (season_dir / "season.nfo").exists():
                nfo = self._scraper.get_metadata_nfo(meta, mediainfo, season=season)
                if nfo:
                    created += self._write_missing(season_dir / "season.nfo", nfo)
            poster_stem = "season-specials-poster" if season == 0 else f"season{season:02d}-poster"
            if self._image_enabled and not has_image(show_dir, poster_stem):
                for image_name, url in self._scraper.get_metadata_img(mediainfo, season=season).items():
                    created += self._download_missing(show_dir / image_name, url)

        for media_path, season, episode, meta in normalized:
            if not episode:
                continue
            if self._nfo_enabled and not media_path.with_suffix(".nfo").exists():
                nfo = self._scraper.get_metadata_nfo(
                    meta, mediainfo, season=season, episode=episode
                )
                if nfo:
                    created += self._write_missing(media_path.with_suffix(".nfo"), nfo)
            thumb_stem = f"{media_path.stem}-thumb"
            if self._image_enabled and not has_image(media_path.parent, thumb_stem):
                for image_name, url in self._scraper.get_metadata_img(
                        mediainfo, season=season, episode=episode
                ).items():
                    created += self._download_missing(
                        media_path.with_name(f"{thumb_stem}{Path(image_name).suffix}"), url
                    )
        return created

    def scrape_batch(self, items: Iterable[Dict], mediainfo: MediaInfo) -> int:
        """批量补齐元数据，并对剧根与季目录去重。"""
        items = list(items or [])
        if not items or not mediainfo or not getattr(mediainfo, "tmdb_id", None):
            return 0
        if mediainfo.type == MediaType.MOVIE:
            return sum(self._scrape_movie(Path(item["media_path"]), mediainfo) for item in items)
        return self._scrape_tv_batch(items, mediainfo)
