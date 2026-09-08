"""MoviePilot 平台规则适配与资源候选排名。"""

import hashlib
import json
from typing import Any, Dict, List, Optional

from app.log import logger
from app.schemas import MediaInfo, TorrentInfo
from app.utils.string import StringUtils

from ...core import OwnerDelegator
from ...search.matching import positive_ints


class PlatformRuleService(OwnerDelegator):
    """集中处理平台过滤规则与 PanSearch 资源结构之间的适配。"""

    @staticmethod
    def _resource_filter_title(resource: Dict[str, Any]) -> str:
        """将搜索源的结构化发布信息还原为可识别的规则标题。"""
        video_info = resource.get("video_info") or {}
        language_values = []
        for value in (
                resource.get("languages"), resource.get("subtitle_languages")
        ):
            if isinstance(value, (list, tuple, set)):
                language_values.extend(value)
            elif value:
                language_values.append(value)
        fields = (
            resource.get("title"),
            resource.get("resolution"),
            resource.get("quality"),
            resource.get("source_type"),
            resource.get("codec"),
            resource.get("audio_codec"),
            resource.get("audio_channels"),
            resource.get("hdr_type"),
            video_info.get("hdr") if isinstance(video_info, dict) else "",
            resource.get("release_group"),
            " ".join(str(value) for value in language_values if str(value).strip()),
            resource.get("subtitle"),
            resource.get("description"),
        )
        return " ".join(dict.fromkeys(
            str(value).strip() for value in fields if str(value or "").strip()
        ))

    @staticmethod
    def _file_filter_title(item: Any, index: int) -> str:
        """保留父目录中的版本信息，供平台规则识别清晰度、来源和编码。"""
        value = (
                item.get("_relative_path")
                or item.get("cloud_path")
                or item.get("name")
                or f"file-{index}"
        )
        title = str(value).replace("\\", " ").replace("/", " ").strip()
        file_name = str(item.get("name") or "").strip()
        if file_name and file_name not in title:
            title = f"{title} {file_name}".strip()
        return title

    def _filter_by_platform_rules(
            self,
            resources: List[Dict],
            mediainfo: MediaInfo,
            subscribe: Any = None,
            season: Optional[int] = None,
            target_episodes: Optional[List[int]] = None,
            prefiltered: bool = False,
    ) -> List[Dict]:
        """使用平台规则组筛选并按平台优先级排序。"""
        if not resources:
            return []
        if not prefiltered:
            resources = self._prefilter_resource_order(
                resources, season=season, target_episodes=target_episodes
            )
        try:
            rule_groups = self._platform_rule_groups(subscribe)
            if not rule_groups:
                return list(resources)

            torrents = []
            resource_by_url = {}
            for index, resource in enumerate(resources):
                title = self._resource_filter_title(resource)
                page_url = f"https://cloudsubscribe.invalid/resource/{index}"
                torrents.append(TorrentInfo(
                    title=title or f"resource-{index}",
                    description=str(resource.get("description") or ""),
                    page_url=page_url,
                    size=max(
                        0,
                        int(StringUtils.num_filesize(resource.get("size")) or 0),
                    ),
                    labels=[],
                ))
                resource_by_url[page_url] = resource

            matched = self.filter_torrents_by_rules(
                rule_groups=rule_groups,
                torrent_list=torrents,
                mediainfo=mediainfo,
            ) or []
            filtered = []
            for item in matched:
                resource = resource_by_url.get(getattr(item, "page_url", None))
                if resource is None:
                    continue
                resource = dict(resource)
                resource["platform_priority"] = int(
                    getattr(item, "pri_order", 0) or 0
                )
                filtered.append(resource)
            targets = positive_ints(target_episodes)
            filtered.sort(
                key=lambda resource: self._resource_sort_key(
                    resource, season, targets
                )
            )
            logger.debug(
                f"平台优先级规则组筛选资源：{len(resources)} -> {len(filtered)}，"
                f"规则组：{rule_groups}"
            )
            return filtered
        except Exception as error:
            logger.error(f"平台优先级规则组筛选失败，已拒绝本批资源：{error}")
            return []

    def _platform_rule_groups(self, subscribe: Any = None) -> List[str]:
        """读取订阅指定规则组，否则使用对应的全局规则组。"""
        from app.db.systemconfig_oper import SystemConfigOper
        from app.schemas.types import SystemConfigKey

        rule_groups = list(getattr(subscribe, "filter_groups", None) or [])
        if rule_groups:
            return rule_groups
        config_key = (
            SystemConfigKey.BestVersionFilterRuleGroups
            if self._is_cloud_upgrade_subscribe(subscribe)
            else SystemConfigKey.SubscribeFilterRuleGroups
        )
        return list(SystemConfigOper().get(config_key) or [])

    def rank_file_candidates(
            self,
            files: List[Any],
            mediainfo: MediaInfo,
            subscribe: Any = None,
    ) -> List[tuple]:
        """使用规则组筛选实际文件并返回 ``(文件, pri_order)``。"""
        candidates = [item for item in files or [] if item]
        if not candidates:
            return []
        rule_groups = self._platform_rule_groups(subscribe)
        if not rule_groups:
            return sorted(
                ((item, 0) for item in candidates),
                key=lambda pair: max(
                    0, int(StringUtils.num_filesize(pair[0].get("size")) or 0)
                ),
                reverse=True,
            )

        torrents = []
        by_url = {}
        size_by_url = {}
        for index, item in enumerate(candidates):
            page_url = f"https://cloudsubscribe.invalid/file/{index}"
            size_bytes = max(
                0, int(StringUtils.num_filesize(item.get("size")) or 0)
            )
            torrents.append(TorrentInfo(
                title=self._file_filter_title(item, index),
                description="",
                page_url=page_url,
                size=size_bytes,
                labels=[],
            ))
            by_url[page_url] = item
            size_by_url[page_url] = size_bytes
        try:
            matched = self.filter_torrents_by_rules(
                rule_groups=rule_groups,
                torrent_list=torrents,
                mediainfo=mediainfo,
            )
        except Exception as error:
            logger.error(f"平台优先级规则匹配文件失败：{error}")
            return []
        ranked = [
            (
                by_url[item.page_url],
                int(item.pri_order or 0),
                size_by_url[item.page_url],
            )
            for item in matched or []
            if getattr(item, "page_url", None) in by_url
        ]
        ranked.sort(key=lambda item: (item[1], item[2]), reverse=True)
        return [(item, priority) for item, priority, _ in ranked]

    def select_file_candidate(
            self,
            files: List[Any],
            mediainfo: MediaInfo,
            subscribe: Any = None,
    ) -> tuple:
        ranked = self.rank_file_candidates(files, mediainfo, subscribe)
        return ranked[0] if ranked else (None, 0)

    def filter_torrents_by_rules(
            self,
            rule_groups: List[str],
            torrent_list: List[Any],
            mediainfo: MediaInfo,
    ) -> List[Any]:
        """复用 MoviePilot 过滤模块，避免每次搜索或评分重复加载规则集。"""
        with self._platform_filter_lock:
            signature = self._platform_rules_signature()
            if (
                    self._platform_filter_module is None
                    or signature != self._platform_filter_signature
            ):
                from app.modules.filter import FilterModule

                self._platform_filter_module = FilterModule()
                self._platform_filter_module.init_module()
                self._platform_filter_signature = signature
                logger.debug("MoviePilot平台过滤规则已同步到 PanSearch")
            return self._platform_filter_module.filter_torrents(
                rule_groups=rule_groups,
                torrent_list=torrent_list,
                mediainfo=mediainfo,
            ) or []

    def _platform_rules_signature(self) -> str:
        """检测平台规则配置变化，避免长期复用已经过期的 FilterModule。"""
        cached = self._platform_filter_signature_cache.get("signature")
        if cached:
            return str(cached)
        from app.db.systemconfig_oper import SystemConfigOper
        from app.schemas.types import SystemConfigKey

        oper = SystemConfigOper()
        payload = {
            "groups": oper.get(SystemConfigKey.UserFilterRuleGroups) or [],
            "rules": oper.get(SystemConfigKey.CustomFilterRules) or [],
        }
        serialized = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        signature = hashlib.sha1(serialized.encode("utf-8")).hexdigest()
        self._platform_filter_signature_cache.set("signature", signature)
        return signature
