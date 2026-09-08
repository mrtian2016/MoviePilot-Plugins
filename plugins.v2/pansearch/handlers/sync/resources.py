"""候选资源解析、校验与网盘 Provider 路由。"""

import copy
import re
from pathlib import PurePosixPath
from typing import Any, Dict, List, Mapping, Optional, Set, Tuple
from urllib.parse import unquote, urlsplit

from app.log import logger

from ...core import (
    CloudDriveCapability,
    CloudDriveProvider,
    CloudFile,
    OwnerDelegator,
    SearchCapability,
)
from ...search.types import normalize_resource_type, resource_type_from_url
from ...utils import MediaFileParser, parse_magnet_metadata


class ResourceTransferService(OwnerDelegator):
    """统一搜索候选到网盘转存能力之间的适配。"""

    _TITLE_SEASON_PATTERN = re.compile(
        r"(?<![A-Za-z0-9])[Ss]\s*0*(\d{1,3})(?!\d)|"
        r"第\s*0*(\d{1,3})\s*季"
    )

    @staticmethod
    def _is_cloud_resource_url(value: str) -> bool:
        return str(value or "").strip().lower().startswith("cloud://")

    def _resource_input_label(self, value: str) -> str:
        if self._is_cloud_resource_url(value):
            return "网盘路径"
        if self._is_offline_url(value):
            return "离线资源"
        return "分享"

    @classmethod
    def _cloud_resource_provider_key(cls, value: str) -> str:
        if not cls._is_cloud_resource_url(value):
            return ""
        return str(urlsplit(str(value).strip()).netloc or "").strip().lower()

    def _is_direct_cloud_resource_url(self, value: str) -> bool:
        source_key = self._cloud_resource_provider_key(value)
        target_key = str(getattr(self._cloud_drive, "key", "") or "").strip().lower()
        return not source_key or not target_key or source_key == target_key

    @classmethod
    def _cloud_resource_path(cls, value: str) -> str:
        if not cls._is_cloud_resource_url(value):
            return ""
        path = unquote(urlsplit(str(value or "").strip()).path or "/")
        return str(PurePosixPath("/" + path.replace("\\", "/").lstrip("/")))

    def _resource_staging_dir(
            self, share_url: str, file_item: Optional[Dict[str, Any]] = None
    ) -> str:
        if (
                self._is_cloud_resource_url(share_url)
                and self._is_direct_cloud_resource_url(share_url)
        ):
            source_path = str((file_item or {}).get("cloud_path") or "").strip()
            return source_path or self._cloud_resource_path(share_url)
        return self._cloud_transfer_path

    def _list_cloud_resource_files(
            self, path: str, provider_key: str = ""
    ) -> List[Dict[str, Any]]:
        """递归列出所选网盘目录，并保留每个文件的真实源目录。"""
        source_drive = self._cloud_drive
        if provider_key and self._cloud_drive_registry:
            try:
                source_drive = self._cloud_drive_registry.get(provider_key)
            except KeyError:
                return []
        if not source_drive or not source_drive.supports(CloudDriveCapability.DIRECTORY_READ):
            return []
        directory_service = source_drive.require(CloudDriveCapability.DIRECTORY_READ)
        lookup = directory_service.resolve_directory(path)
        if not lookup.checked:
            raise RuntimeError(f"读取网盘资源路径失败：{path}")
        if lookup.directory_id is None:
            return []
        files = []
        root_path = PurePosixPath(path)
        stack = [(path, str(lookup.directory_id))]
        visited = set()
        while stack:
            current_path, directory_id = stack.pop()
            if directory_id in visited:
                continue
            visited.add(directory_id)
            listing = directory_service.list_directory(directory_id)
            if not listing.checked:
                raise RuntimeError(f"读取网盘资源目录失败：{current_path}")
            for item in listing.files:
                if item.is_directory:
                    child_path = str(PurePosixPath(current_path) / item.name)
                    stack.append((child_path, str(item.id)))
                    continue
                current_posix_path = PurePosixPath(current_path)
                try:
                    relative_parent = current_posix_path.relative_to(root_path)
                except ValueError:
                    relative_parent = current_posix_path
                relative_path = str(relative_parent / item.name).lstrip("/")
                files.append({
                    **dict(item),
                    "cloud_path": current_path,
                    "cloud_provider": provider_key,
                    "_relative_path": relative_path,
                    "_cloud_file": item,
                })
        return files

    @staticmethod
    def _resource_preview_episodes(
            resource: Dict[str, Any], season: int
    ) -> Set[int]:
        """读取搜索阶段已取得的文件预览，不额外请求网盘接口。"""
        preview_episodes = resource.get("preview_episodes") or {}
        values = preview_episodes.get(
            str(season), preview_episodes.get(season, [])
        )
        episodes = set()
        for value in values or []:
            try:
                episodes.add(int(value))
            except (TypeError, ValueError):
                continue
        return episodes

    @staticmethod
    def _resource_title_episodes(title: str, season: int) -> Set[int]:
        """从资源标题读取明确的单集信息，不为汇总标题猜测集数。"""
        text = str(title or "").strip()
        if not text:
            return set()
        episodes: Set[int] = set()
        season_number = int(season or 1)
        season_pattern = re.compile(
            r"[Ss](\d{1,2})[Ee]\s*0*(\d{1,4})"
            r"(?:\s*[-~～–—至到]\s*[Ee]?\s*0*(\d{1,4}))?"
        )
        for match in season_pattern.finditer(text):
            if int(match.group(1)) != season_number:
                continue
            start = int(match.group(2))
            end = int(match.group(3) or start)
            if start <= end and end - start <= 999:
                episodes.update(range(start, end + 1))

        # 防止 S02E08-E09 的尾部 E09 被当成无季号表达式再次命中。
        unscoped_text = season_pattern.sub(
            lambda match: " " * len(match.group(0)), text
        )
        for pattern in (
                re.compile(
                    r"(?<!\d)第\s*0*(\d{1,4})"
                    r"(?:\s*[-~～–—至到]\s*0*(\d{1,4}))?\s*集"
                ),
                re.compile(
                    r"(?<![A-Za-z0-9])[Ee][Pp]?\s*0*(\d{1,4})"
                    r"(?:\s*[-~～–—至到]\s*[EePp]?\s*0*(\d{1,4}))?(?!\d)"
                ),
        ):
            for match in pattern.finditer(unscoped_text):
                start = int(match.group(1))
                end = int(match.group(2) or start)
                if end < start or end - start > 999:
                    continue
                episodes.update(range(start, end + 1))
        return {episode for episode in episodes if episode > 0}

    @classmethod
    def _resource_title_seasons(cls, title: str) -> Set[int]:
        """提取标题中明确声明的季号。"""
        seasons = set()
        for match in cls._TITLE_SEASON_PATTERN.finditer(str(title or "")):
            value = match.group(1) or match.group(2)
            if value and int(value) > 0:
                seasons.add(int(value))
        return seasons

    @staticmethod
    def _magnet_resource_title(resource: Dict[str, Any]) -> str:
        """合并所有来源可能携带的 Magnet 发布标题。"""
        metadata = resource.get("magnet_metadata") or {}
        values = (
            resource.get("title"),
            resource.get("description"),
            resource.get("name"),
            resource.get("raw_title"),
            resource.get("rawTitle"),
            resource.get("release_name"),
            resource.get("magnet_name"),
            resource.get("magnet_uri_name"),
            resource.get("torrent_name"),
            metadata.get("display_name"),
        )
        return " ".join(dict.fromkeys(
            str(value or "").strip() for value in values if str(value or "").strip()
        ))

    @classmethod
    def _prepare_magnet_resource(
            cls, resource: Dict[str, Any], share_url: str
    ) -> str:
        """统一补全 Magnet URI 展示名和轻量集数预览，不请求远端 torrent 元数据。"""
        metadata = dict(resource.get("magnet_metadata") or {})
        provider_text = cls._magnet_resource_title(resource)
        parsed = parse_magnet_metadata(share_url, provider_text=provider_text)
        if parsed:
            for key, value in parsed.items():
                if value and not metadata.get(key):
                    metadata[key] = value
            if metadata:
                resource["magnet_metadata"] = metadata
            if parsed.get("display_name"):
                resource["magnet_uri_name"] = parsed["display_name"]
                if not resource.get("magnet_name"):
                    resource["magnet_name"] = parsed["display_name"]
            if parsed.get("preview_episodes") and not resource.get("preview_episodes"):
                resource["preview_episodes"] = parsed["preview_episodes"]
        return cls._magnet_resource_title(resource)

    @classmethod
    def _magnet_title_episodes(
            cls, resource: Dict[str, Any], season: int
    ) -> Set[int]:
        """从统一标题和来源结构化集数字段提取明确集数。"""
        episodes = cls._resource_title_episodes(
            cls._magnet_resource_title(resource), season
        )
        season_value = resource.get("season")
        try:
            season_matches = season_value in (None, "") or int(season_value) == int(season)
        except (TypeError, ValueError):
            season_matches = False
        if season_matches:
            episode_values = resource.get("episodes")
            if not isinstance(episode_values, (list, tuple, set)):
                episode_values = [episode_values or resource.get("episode")]
            for value in episode_values:
                try:
                    episode = int(value)
                except (TypeError, ValueError):
                    episodes.update(
                        cls._resource_title_episodes(f"E{value}", season)
                    )
                else:
                    if episode > 0:
                        episodes.add(episode)
            try:
                start = int(resource.get("episode_start") or 0)
                end = int(resource.get("episode_end") or start)
            except (TypeError, ValueError):
                start = end = 0
            if 0 < start <= end and end - start <= 999:
                episodes.update(range(start, end + 1))
        return episodes

    @classmethod
    def _magnet_title_seasons(cls, resource: Dict[str, Any]) -> Set[int]:
        """从统一标题和来源结构化字段提取明确季号。"""
        seasons = cls._resource_title_seasons(
            cls._magnet_resource_title(resource)
        )
        try:
            structured = int(resource.get("season") or 0)
        except (TypeError, ValueError):
            structured = 0
        if structured > 0:
            seasons.add(structured)
        return seasons

    @staticmethod
    def _magnet_history_entries(
            resource: Dict[str, Any],
            season: Optional[int] = None,
            target_episodes: Optional[List[int]] = None,
    ) -> List[Dict[str, Any]]:
        """从已校验 torrent 元数据生成历史占位文件，禁止使用发布标题猜测。"""
        metadata = resource.get("magnet_metadata") or {}
        target_set = {
            int(value) for value in (target_episodes or []) if int(value) > 0
        }
        entries = []
        seen = set()
        for source in metadata.get("torrent_file_entries") or []:
            path = str(source.get("path") or "").replace("\\", "/").strip("/")
            file_name = path.rsplit("/", 1)[-1]
            if not MediaFileParser.is_video(file_name):
                continue
            try:
                entry_season = int(source.get("season") or 0)
                episode = int(source.get("episode") or 0)
            except (TypeError, ValueError):
                continue
            if season is None and not entry_season and not episode:
                entries.append({
                    "file_name": file_name,
                    "file_size": max(0, int(source.get("size") or 0)),
                    "season": 0,
                    "episode": 0,
                })
                continue
            if season is not None and entry_season != int(season):
                continue
            if target_set and episode not in target_set:
                continue
            identity = (entry_season, episode, file_name.casefold())
            if identity in seen:
                continue
            seen.add(identity)
            entries.append({
                "file_name": file_name,
                "file_size": max(0, int(source.get("size") or 0)),
                "season": entry_season,
                "episode": episode,
            })
        return entries

    def _append_magnet_pending_history(
            self,
            history: List[Dict[str, Any]],
            mediainfo: Any,
            subscribe: Any,
            share_url: str,
            cloud_dir: str,
            resource: Dict[str, Any],
            finalize_key: str,
            season: Optional[int] = None,
            target_episodes: Optional[List[int]] = None,
            **fields: Any,
    ) -> int:
        """按 torrent 真实媒体文件追加待完成历史，并避免同任务重复记录。"""
        entries = self._magnet_history_entries(resource, season, target_episodes)
        if not entries:
            return 0
        existing = {
            (
                str(item.get("finalize_key") or ""),
                int(item.get("season") or 0),
                int(item.get("episode") or 0),
            )
            for item in history
            if isinstance(item, dict)
        }
        appended = 0
        for entry in entries:
            identity = (
                finalize_key,
                int(entry.get("season") or 0),
                int(entry.get("episode") or 0),
            )
            if identity in existing:
                continue
            record_fields = dict(fields)
            record_fields.update({
                "file_size": entry["file_size"],
                "season": entry["season"],
                "episode": entry["episode"],
                "target_episodes": [entry["episode"]],
                "finalize_key": finalize_key,
            })
            history.append(self._build_transfer_history_item(
                mediainfo=mediainfo,
                subscribe=subscribe,
                status="下载中",
                share_url=share_url,
                file_name=entry["file_name"],
                source_file_name=entry["file_name"],
                cloud_dir=cloud_dir,
                resource=resource,
                **record_fields,
            ))
            existing.add(identity)
            appended += 1
        return appended

    def _resource_history_meta(
            self, resource: Dict[str, Any], share_url: str
    ) -> Dict[str, Any]:
        source = str(resource.get("source") or "unknown").strip().lower()
        resource_type = normalize_resource_type(
            resource.get("resource_type") or resource.get("pan_type") or ""
        )
        if not resource_type:
            resource_type = resource_type_from_url(share_url) or "unknown"
        points = resource.get("unlock_points")
        try:
            points = int(points) if points is not None else None
        except (TypeError, ValueError):
            points = None
        source_url = ""
        if source != "manual":
            for value in (
                    resource.get("source_url"), resource.get("media_page_url")
            ):
                candidate = str(value or "").strip()
                if candidate.lower().startswith(("http://", "https://")):
                    if source == "hdhive":
                        parsed = urlsplit(candidate)
                        candidate = f"{parsed.path or '/'}{f'?{parsed.query}' if parsed.query else ''}"
                    source_url = candidate
                    break
        result = {
            "resource_type": resource_type,
            "source": source,
            "points": points,
        }
        if resource.get("skip_history"):
            result["skip_history"] = True
        if source_url:
            result["source_url"] = source_url
        return result

    def _expand_resource_urls(
            self,
            resources: List[Dict[str, Any]],
            resource_index: int,
            resource: Dict[str, Any],
            value: Any,
    ) -> str:
        """展开列表或字符串中的多条离线链接，后续条目不重复计算积分。"""
        raw_values = value if isinstance(value, (list, tuple)) else [value]
        urls = []
        seen_urls = set()
        for raw_value in raw_values:
            text = str(raw_value or "").replace("｜", "|").strip()
            if not text:
                continue
            matches = list(self._OFFLINE_RESOURCE_URL_RE.finditer(text))
            extracted = [match.group(0).strip() for match in matches]
            remainder = self._OFFLINE_RESOURCE_URL_RE.sub("", text).strip()
            candidates = extracted if extracted and not remainder else [text]
            for url in candidates:
                if url in seen_urls:
                    continue
                seen_urls.add(url)
                urls.append(url)
        if not urls:
            return ""

        resource["url"] = urls[0]
        resource["need_unlock"] = False
        resource["need_access"] = False
        if len(urls) > 1:
            expanded = []
            for url in urls[1:]:
                item = copy.deepcopy(resource)
                item["url"] = url
                item["unlock_points"] = 0
                expanded.append(item)
            resources[resource_index + 1:resource_index + 1] = expanded
            source_name = str(resource.get("source") or "资源源").upper()
            logger.debug(
                f"{source_name} 同一资源包含 {len(urls)} 条链接，"
                "已展开处理且积分仅计一次"
            )
        return urls[0]

    def _resolve_candidate_resource_url(
            self,
            resources: List[Dict[str, Any]],
            resource_index: int,
            resource: Dict[str, Any],
            search_label: str,
            log_prefix: str = "",
    ) -> str:
        """统一处理积分搜索源的延迟解锁，并展开一次返回的多条链接。"""
        share_url = str(resource.get("url") or "").strip()
        if share_url:
            return self._expand_resource_urls(
                resources, resource_index, resource, share_url
            )
        if not (
                resource.get("need_unlock") or resource.get("need_access")
        ):
            return ""
        resource_ref = str(resource.get("resource_ref") or "").strip()
        if not resource_ref:
            return ""
        source = str(resource.get("source") or "").strip().lower()
        try:
            target_season = max(1, int(resource.get("target_season") or 1))
        except (TypeError, ValueError):
            target_season = 1
        target_episodes = set()
        for value in resource.get("target_episodes") or []:
            try:
                episode = int(value)
            except (TypeError, ValueError):
                continue
            if episode > 0:
                target_episodes.add(episode)
        remark_episodes = self._resource_preview_episodes(
            resource, target_season
        )
        if (
                resource.get("preview_episodes_authoritative")
                and target_episodes
                and remark_episodes
                and not (target_episodes & remark_episodes)
        ):
            prefix = f"{log_prefix} " if log_prefix else ""
            logger.debug(
                f"{prefix}{source.upper()} 预览已明确不覆盖当前缺集，"
                f"跳过详情预览与解锁：resource_ref={resource_ref}"
            )
            return ""
        try:
            unlock_points = int(resource.get("unlock_points") or 0)
        except (TypeError, ValueError):
            unlock_points = 0
        prefix = f"{log_prefix} " if log_prefix else ""
        resource_title = str(resource.get("title") or "").strip()
        if not (
                self._search_handler.supports(
                    source, SearchCapability.RESOURCE_UNLOCK
                )
                and self._search_handler.supports(
            source, SearchCapability.POINT_BUDGET
        )
        ):
            logger.debug(
                f"{prefix}跳过 {source.upper()} 资源 {resource_title}："
                "渠道未声明资源解锁和积分预算能力"
            )
            return ""
        has_budget = self._search_handler.has_unlock_budget(
            source, unlock_points
        )
        source_label = self._search_handler.source_name(source)
        if not has_budget:
            logger.debug(
                f"{prefix}跳过 {source_label} 资源 {resource_title}："
                f"需要 {unlock_points} 积分，当前预算不足"
            )
            return ""
        action_label = "获取免费资源链接" if unlock_points <= 0 else "消耗积分解锁"
        media_page_url = str(resource.get("media_page_url") or "").strip()
        media_page_suffix = f"，媒体页：{media_page_url}" if media_page_url else ""
        logger.info(
            f"{prefix}遇到尚未取得链接的 {source_label} 资源 {resource_title} "
            f"(resource_ref: {resource_ref})，尝试{action_label}{media_page_suffix}"
        )
        unlocked = self._search_handler.unlock_resource(
            source, resource, search_label=search_label
        )
        if self._stop_requested() or not unlocked:
            if not self._stop_requested():
                logger.error(
                    f"{prefix}未能取得 {source_label} 资源链接：{resource_title}"
                )
            return ""
        return self._expand_resource_urls(
            resources, resource_index, resource, unlocked
        )

    def _validate_resource_url(
            self,
            share_url: str,
            resource_label: str = "分享链接",
            log_prefix: str = "",
    ) -> bool:
        """使用对应 Provider 的统一能力校验资源链接。"""
        provider = self._resource_provider_for_url(share_url)
        share_service = (
            provider.require(CloudDriveCapability.SHARE_TRANSFER)
            if provider else self._share_transfer
        )
        if not share_service:
            return False
        status = self._timed_sync_call(
            "share_validation", share_service.check_share_status, share_url
        )
        if status.is_valid:
            return True
        prefix = f"{log_prefix} " if log_prefix else ""
        logger.debug(
            f"{prefix}{resource_label}无效："
            f"{self._resource_log_reference(share_url)}，原因：{status.status_text}"
        )
        return False

    def _validated_resource_files(
            self,
            share_url: str,
            resource_title: str = "",
            target_season: Optional[int] = None,
            log_prefix: str = "",
    ) -> List[Dict[str, Any]]:
        """校验分享并读取文件列表，供电影、剧集和洗版共同使用。"""
        if self._is_cloud_resource_url(share_url):
            files = self._list_cloud_resource_files(
                self._cloud_resource_path(share_url),
                self._cloud_resource_provider_key(share_url),
            )
            files = list(MediaFileParser.iter_files(files))
            if not files:
                logger.debug(f"所选网盘路径没有可处理文件：{self._cloud_resource_path(share_url)}")
            return files
        if not self._validate_resource_url(
                share_url, resource_label="分享链接", log_prefix=log_prefix
        ):
            return []
        kwargs = {}
        if target_season is not None:
            kwargs["target_season"] = target_season
        if log_prefix:
            kwargs["log_prefix"] = log_prefix
        provider = self._resource_provider_for_url(share_url)
        share_service = (
            provider.require(CloudDriveCapability.SHARE_TRANSFER)
            if provider else self._share_transfer
        )
        if not share_service:
            return []
        files = self._timed_sync_call(
            "share_listing",
            share_service.list_share_files,
            share_url,
            **kwargs,
        ) or []
        files = list(MediaFileParser.iter_files(files))
        if not files:
            label = resource_title or self._resource_log_reference(share_url)
            logger.debug(
                f"{log_prefix + ' ' if log_prefix else ''}分享链接无内容：{label}"
            )
        return files

    def preview_resource_files(self, share_url: str) -> List[Dict[str, Any]]:
        """只读校验分享链接并列举媒体文件，供无订阅入口识别内容。"""
        return self._validated_resource_files(
            share_url,
            resource_title="Telegram 分享链接",
            log_prefix="快速识别",
        )

    def _transfer_history_status(self, success: bool, share_url: str) -> str:
        if not success:
            return "失败"
        return "下载中" if self._is_offline_url(share_url) else "成功"

    @staticmethod
    def _supported_resource_type(
            resource: Dict[str, Any], share_url: str
    ) -> str:
        resource_type = str(
            resource.get("resource_type") or resource.get("pan_type") or ""
        ).strip().lower()
        if resource_type:
            return resource_type
        normalized_url = str(share_url).lstrip().lower()
        if normalized_url.startswith("cloud://"):
            return "cloud"
        if normalized_url.startswith("ed2k://"):
            return "ed2k"
        if normalized_url.startswith("magnet:?"):
            return "magnet"
        for marker, value in (
                ("quark", "quark"), ("189.cn", "tianyi"),
                ("cloud.189", "tianyi"), ("guangya", "guangya"),
                ("123pan", "123"), ("123.cn", "123"),
                ("123684.com", "123"), ("123865.com", "123"),
                ("115cdn.com", "115"),
                ("alipan.com", "alipan"), ("aliyundrive.com", "alipan"),
        ):
            if marker in normalized_url:
                return value
        return "115"

    def _is_cross_drive_resource(
            self, resource: Dict[str, Any], share_url: str = ""
    ) -> bool:
        """判断候选是否必须经过跨盘下载上传。"""
        if not self._cloud_drive:
            return False
        actual_url = str(share_url or resource.get("url") or "").strip()
        if self._supported_resource_type(resource, share_url) == "cloud":
            return not self._is_direct_cloud_resource_url(actual_url)
        if actual_url and not self._is_offline_url(actual_url):
            source = self._resource_provider_for_url(actual_url)
            if source:
                return source.key != self._cloud_drive.key
        resource_type = self._supported_resource_type(resource, actual_url)
        resource_type = {
            "189": "tianyi", "aliyun": "alipan",
        }.get(resource_type, resource_type)
        return not self._cloud_drive.supports_resource_type(resource_type)

    def _build_transfer_resource_batches(
            self,
            sources: List[str],
            source_results: Mapping[str, List[Dict[str, Any]]],
    ) -> List[Tuple[str, List[Dict[str, Any]], bool]]:
        """按目标盘直存优先、跨盘最后生成来源批次。"""
        direct_batches = []
        cross_batches = []
        direct_count = 0
        cross_count = 0
        for source in sources or []:
            direct_resources = []
            cross_resources = []
            for resource in source_results.get(source) or []:
                if self._is_cross_drive_resource(resource):
                    cross_resources.append(resource)
                else:
                    direct_resources.append(resource)
            if direct_resources:
                direct_count += len(direct_resources)
                direct_batches.append((source, direct_resources, False))
            if cross_resources:
                cross_count += len(cross_resources)
                cross_batches.append((source, cross_resources, True))
        if direct_count or cross_count:
            logger.debug(
                f"候选转存顺序：目标网盘直存 {direct_count} 个，"
                f"跨盘 {cross_count} 个（跨盘最后处理）"
            )
        return direct_batches + cross_batches

    def _resource_provider_for_url(
            self, share_url: str
    ) -> Optional[CloudDriveProvider]:
        if not self._cloud_drive_registry:
            return self._cloud_drive
        key = self._supported_resource_type({}, share_url)
        if key == "cloud":
            source_key = self._cloud_resource_provider_key(share_url)
            if source_key and self._cloud_drive_registry:
                try:
                    return self._cloud_drive_registry.get(source_key)
                except KeyError:
                    return None
            return self._cloud_drive
        aliases = {"189": "tianyi", "aliyun": "alipan"}
        try:
            return self._cloud_drive_registry.get(aliases.get(key, key))
        except KeyError:
            return self._cloud_drive if key == "115" else None

    @staticmethod
    def _normalize_cross_transfer_media_type(value: Any) -> str:
        normalized = str(getattr(value, "name", value) or "").strip().lower()
        return {
            "电影": "movie",
            "电视剧": "tv",
            "mediatype.movie": "movie",
            "mediatype.tv": "tv",
        }.get(normalized, normalized)

    @staticmethod
    def _cloud_file_from_dict(item: Dict[str, Any]) -> CloudFile:
        existing = item.get("_cloud_file")
        if isinstance(existing, CloudFile):
            return existing
        return CloudFile(
            id=str(item.get("id") or ""),
            name=str(item.get("name") or ""),
            is_directory=False,
            size=int(item.get("size") or 0),
            sha1=str(item.get("sha1") or ""),
            md5=str(item.get("md5") or ""),
            native=item,
        )

    def _is_supported_resource(
            self, resource: Dict[str, Any], share_url: str
    ) -> bool:
        if not self._cloud_drive:
            return False
        resource_type = self._supported_resource_type(resource, share_url)
        if resource_type == "cloud":
            source = self._resource_provider_for_url(share_url)
            if not source or not source.supports(CloudDriveCapability.DIRECTORY_READ):
                return False
            if self._is_direct_cloud_resource_url(share_url):
                return all(
                    self._cloud_drive.supports(capability)
                    for capability in (
                        CloudDriveCapability.FILE_QUERY,
                        CloudDriveCapability.FILE_MUTATION,
                    )
                )
            return bool(
                self._cross_transfer_enabled
                and source.supports(CloudDriveCapability.FILE_QUERY)
                and source.supports(CloudDriveCapability.FILE_DOWNLOAD)
                and self._cloud_drive.supports(CloudDriveCapability.LOCAL_UPLOAD)
                and self._cloud_drive.supports(CloudDriveCapability.FILE_QUERY)
            )
        is_manual_override = bool(
            resource.get("source") == "manual"
            or resource.get("is_cross")
            or resource.get("_manual")
        )
        if not self._cloud_drive.supports_resource_type(resource_type):
            if not self._cross_transfer_enabled and not is_manual_override:
                return False
            source = self._resource_provider_for_url(share_url)
            if not source or not source.supports(
                    CloudDriveCapability.SHARE_TRANSFER
            ):
                return False
            if not source.supports(CloudDriveCapability.FILE_QUERY):
                return False
            if not source.supports(CloudDriveCapability.FILE_DOWNLOAD):
                return False
            if not self._cloud_drive.supports(CloudDriveCapability.LOCAL_UPLOAD):
                return False
            if not self._cloud_drive.supports(CloudDriveCapability.FILE_QUERY):
                return False
        if resource_type in {"ed2k", "magnet"}:
            return self._offline_download is not None
        source = self._resource_provider_for_url(share_url)
        return bool(
            source and source.supports(CloudDriveCapability.SHARE_TRANSFER)
        )

    @classmethod
    def _format_resource_summary(
            cls, resources: List[Dict[str, Any]]
    ) -> str:
        labels = {
            "share": "网盘分享", "cloud": "网盘路径",
            "ed2k": "ED2K", "magnet": "Magnet",
        }
        summary_counts: Dict[str, Dict[str, int]] = {}
        seen = set()
        for resource in resources or []:
            resource_type = cls._supported_resource_type(
                resource, str(resource.get("url") or "")
            )
            label = labels.get(resource_type, resource_type.upper() or "未知")
            identity = str(
                resource.get("unlock_group") or resource.get("source_url")
                or resource.get("url") or resource.get("title") or ""
            )
            key = (resource_type, identity)
            if key in seen:
                continue
            seen.add(key)
            counts = summary_counts.setdefault(
                label, {"total": 0, "available": 0, "paid": 0, "official": 0}
            )
            counts["total"] += 1
            if resource.get("is_official"):
                counts["official"] += 1
            if resource.get("need_unlock"):
                counts["paid"] += 1
            else:
                counts["available"] += 1
        summaries = []
        for label, counts in summary_counts.items():
            statuses = [f"可用 {counts['available']}"]
            if counts["paid"]:
                statuses.append(f"待解锁 {counts['paid']}")
            if counts["official"]:
                statuses.append(f"官组 {counts['official']}")
            summaries.append(
                f"{label} {counts['total']}（{'，'.join(statuses)}）"
            )
        return f"共 {len(seen)} 个候选资源：" + "；".join(summaries)
