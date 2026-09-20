"""PanSou 资源搜索。"""

from typing import Any, Dict, List, Optional

from app.log import logger
from app.schemas import MediaInfo
from app.schemas.types import MediaType

from ...core import OwnerDelegator, SearchQuery
from ...search.magnet import clear_cache, normalize_magnets


class PanSouSearchService(OwnerDelegator):
    """提供 PanSou 搜索实现。"""

    def search(self, query: SearchQuery) -> List[Dict]:
        """将统一查询参数适配为 PanSou 的电影或剧集搜索。"""
        if query.media_type == MediaType.MOVIE:
            return self._search_pansou_movie(
                query.mediainfo,
                resource_list_mode=query.resource_list_mode,
                result_limit=query.result_limit,
            )
        return self._search_pansou_tv(
            query.mediainfo,
            query.season,
            resource_list_mode=query.resource_list_mode,
            result_limit=query.result_limit,
        )

    def clear_cache(self) -> int:
        return clear_cache(self._pansou_client)

    @staticmethod
    def _pansou_resource_type(resource: Dict[str, Any]) -> str:
        value = str(
            resource.get("resource_type") or resource.get("pan_type") or ""
        ).strip().lower()
        return "alipan" if value == "aliyun" else value

    def _pansou_search(
            self,
            keyword: str,
            mediainfo: MediaInfo,
            media_type: MediaType,
            season: Optional[int] = None,
            resource_list_mode: bool = False,
            result_limit: Optional[int] = None,
    ) -> List[Dict]:
        """
        PanSou 搜索的通用逻辑

        :param keyword: 搜索关键词
        :return: 已规范化的多网盘分享及离线资源列表
        """
        titles = [str(getattr(mediainfo, "title", "") or "").strip()]
        title_en = str(
            getattr(mediainfo, "original_title", "")
            or getattr(mediainfo, "original_name", "")
            or ""
        ).strip()
        if title_en:
            titles.append(title_en)
        effective_limit = (
            max(1, int(result_limit or self._pansou_result_limit))
            if resource_list_mode else self._pansou_result_limit
        )
        search_results = self._pansou_client.request_search(
            keyword=keyword,
            cloud_types=[
                "aliyun" if value == "alipan" else value
                for value in self._resource_type_order_config
            ],
            channels=[] if resource_list_mode else self._pansou_channels,
            plugins=[] if resource_list_mode else self._pansou_plugins,
            limit=effective_limit,
            expected_titles=titles,
            expected_year=getattr(mediainfo, "year", None),
            filter_config={} if resource_list_mode else self._pansou_filter,
            refresh=self._pansou_refresh,
            concurrency=self._pansou_concurrency,
        )

        search_prefix = (
            f"[{self._search_label(mediainfo, media_type, season)}][PANSOU]"
        )
        if not search_results:
            logger.warning(
                f"{search_prefix} 搜索失败：关键词 '{keyword}'，"
                "接口未返回结果"
            )
            return []
        if search_results.get("error"):
            logger.warning(
                f"{search_prefix} 搜索失败：关键词 '{keyword}'，"
                f"原因：{search_results['error']}"
            )
            return []

        results = search_results.get("results", {})
        groups = [group for group in results.values() if isinstance(group, list)]
        if resource_list_mode:
            candidates = []
            offsets = [0] * len(groups)
            while groups and len(candidates) < effective_limit:
                for index in range(len(groups) - 1, -1, -1):
                    group = groups[index]
                    offset = offsets[index]
                    if offset >= len(group):
                        groups.pop(index)
                        offsets.pop(index)
                        continue
                    candidates.append(group[offset])
                    offsets[index] += 1
                    if len(candidates) >= effective_limit:
                        break
        else:
            candidates = [
                item
                for group in groups
                for item in group
                if self._pansou_resource_type(item)
                   in self._resource_type_order_config
            ]
        candidates = normalize_magnets(candidates, "pansou")
        usable = [
            resource
            for resource in candidates
            if resource_list_mode
               or resource.get("resource_type") != "magnet"
               or resource.get("magnet_metadata")
            if self._pansou_media_type_matches(resource, media_type)
        ]
        logger.debug(
            f"{search_prefix} 查询完成：原始条目={int(search_results.get('raw_count') or 0)}，"
            f"匹配链接={int(search_results.get('count') or 0)}，"
            f"可用候选={len(usable)}，已选类型={'/'.join(self._resource_type_order_config) or '无'}，"
            f"耗时={int(search_results.get('elapsed_ms') or 0)}ms"
        )
        return usable

    @staticmethod
    def _pansou_media_type_matches(
            resource: Dict[str, Any], media_type: MediaType
    ) -> bool:
        """仅按 PanSou 返回的明确分类标签排除冲突类型，未知分类不误杀。"""
        tags = " ".join(str(tag) for tag in (resource.get("tags") or [])).lower()
        if not tags:
            return True
        movie_markers = ("电影", "影片", "movie")
        tv_markers = ("电视剧", "剧集", "连续剧", "tv series", "tv")
        has_movie = any(marker in tags for marker in movie_markers)
        has_tv = any(marker in tags for marker in tv_markers)
        if media_type == MediaType.MOVIE:
            return not (has_tv and not has_movie)
        return not (has_movie and not has_tv)

    def _search_pansou_movie(
            self,
            mediainfo: MediaInfo,
            resource_list_mode: bool = False,
            result_limit: Optional[int] = None,
    ) -> List[Dict]:
        """
        仅使用 PanSou 搜索电影资源

        :param mediainfo: 媒体信息
        :return: PanSou 电影候选资源列表
        """
        if not self._pansou_client:
            logger.warning(f"PanSou 客户端未初始化，跳过 PanSou 查询")
            return []

        keyword = f"{mediainfo.title} {mediainfo.year or ''}".strip()
        results = self._pansou_search(
            keyword, mediainfo, MediaType.MOVIE, resource_list_mode=resource_list_mode,
            result_limit=result_limit,
        )
        return results

    def _search_pansou_tv(
            self,
            mediainfo: MediaInfo,
            season: int,
            resource_list_mode: bool = False,
            result_limit: Optional[int] = None,
    ) -> List[Dict]:
        """
        仅使用 PanSou 搜索电视剧资源

        :param mediainfo: 媒体信息
        :param season: 季号
        :return: PanSou 电视剧候选资源列表
        """
        if not self._pansou_client:
            logger.warning(f"PanSou 客户端未初始化，跳过 PanSou 查询")
            return []

        season_number = max(1, int(season or 1))
        keyword = str(mediainfo.title or "").strip()
        results = self._pansou_search(
            keyword, mediainfo, MediaType.TV, season_number,
            resource_list_mode=resource_list_mode, result_limit=result_limit,
        )
        return results
