"""Dian115 资源搜索、客户端生命周期与积分解锁。"""

import re
from typing import Any, Dict, List, Mapping, Optional
from urllib.parse import urlencode

from app.log import logger
from app.schemas.types import MediaType

from .client import Dian115Client, Dian115Error
from .resource import Dian115ResourceService
from ..budget import PointBudgetLedger
from ..matching import unique_texts
from ...core import OwnerDelegator, SearchQuery, format_search_label
from ...core.media import tmdb_id_of
from ...utils.cache import create_platform_ttl_cache
from ...utils.file_parser import MediaFileParser


class Dian115SearchService(OwnerDelegator):
    """提供 Dian115 curl_cffi 搜索与按需解锁能力。"""

    _HISTORY_KEY = "dian115_sub_points_history"

    def __init__(self, owner):
        super().__init__(owner)
        unlocked_cache = create_platform_ttl_cache(
            "search:dian115_unlocked_urls",
            str(owner._dian115_email or "").casefold(),
            maxsize=512,
            ttl=30 * 60,
        )
        object.__setattr__(self, "_budget", PointBudgetLedger(
            self._HISTORY_KEY,
            owner._dian115_max_unlock_points,
            owner._dian115_max_points_per_sub,
            unlocked_cache=unlocked_cache,
        ))

    @property
    def _dian115_budget(self):
        return self._budget

    def _get_dian115_resources(self) -> Dian115ResourceService:
        """复用唯一认证客户端，返回独立的资源服务。"""
        proxy = self._search_proxy
        with self._dian115_client_lock:
            client = self._dian115_client
            if client is None or not client.matches_config(
                    self._dian115_email,
                    self._dian115_password,
                    proxy,
                    self._dian115_request_interval,
                    self._dian115_unlocks_per_minute,
            ):
                if client:
                    client.close()
                client = Dian115Client(
                    email=self._dian115_email,
                    password=self._dian115_password,
                    proxy=proxy,
                    request_interval=self._dian115_request_interval,
                    unlocks_per_minute=self._dian115_unlocks_per_minute,
                    get_data_func=self._dian115_budget.get_data_func,
                    save_data_func=self._dian115_budget.save_data_func,
                )
                self._dian115_client = client
                self._dian115_resources = None
            resources = self._dian115_resources
            if resources is None or not resources.matches_client(client):
                resources = Dian115ResourceService(client)
                self._dian115_resources = resources
            return resources

    def get_client(self) -> Dian115Client:
        """返回搜索、账户信息与签到共同复用的唯一接口客户端。"""
        self._get_dian115_resources()
        return self._dian115_client

    @property
    def budget(self):
        return self._budget

    @property
    def available(self) -> bool:
        return bool(
            self._dian115_enabled
            and self._dian115_email
            and self._dian115_password
        )

    @property
    def resource_types(self):
        return frozenset(self._resource_type_order_config)

    @property
    def cache_context(self) -> Dict[str, Any]:
        return {
            "version": 1,
            "auto_unlock": self._dian115_auto_unlock,
            "candidate_limit": self._dian115_candidate_limit,
        }

    def close(self) -> None:
        with self._dian115_client_lock:
            client = self._dian115_client
            self._dian115_client = None
            self._dian115_resources = None
        if client:
            client.close()

    def clear_cache(self) -> int:
        with self._dian115_client_lock:
            return (
                self._dian115_resources.clear_cache()
                if self._dian115_resources else 0
            )

    @staticmethod
    def _episode_values(value: Any) -> List[int]:
        numbers = []
        for part in re.split(r"[,，\s]+", str(value or "").strip()):
            if not part:
                continue
            match = re.fullmatch(r"(\d+)(?:\s*[-~至]\s*(\d+))?", part)
            if not match:
                continue
            start = int(match.group(1))
            end = int(match.group(2) or start)
            if 0 < start <= end <= 10000:
                numbers.extend(range(start, end + 1))
        return sorted(set(numbers))

    @staticmethod
    def _seasons(share: Dict[str, Any]) -> List[int]:
        values = Dian115SearchService._episode_values(
            share.get("seasons_csv") or share.get("seasons")
        )
        if values:
            return values
        try:
            season = int(share.get("season") or 0)
        except (TypeError, ValueError):
            season = 0
        return [season] if season >= 0 else []

    @staticmethod
    def _resource_type(share: Dict[str, Any]) -> str:
        if str(share.get("share_kind") or "").strip().lower() != "offline":
            return "115"
        resource_type = str(share.get("offline_type") or "").strip().lower()
        return resource_type if resource_type in {"ed2k", "magnet"} else ""

    @staticmethod
    def _share_url(share: Dict[str, Any]) -> str:
        resource_type = Dian115SearchService._resource_type(share)
        if resource_type in {"ed2k", "magnet"}:
            return str(share.get("url") or "").strip()
        direct = str(share.get("url_115") or share.get("url") or "").strip()
        if direct:
            return direct
        share_code = str(share.get("share_code") or "").strip()
        receive_code = str(share.get("receive_code") or "").strip()
        if share_code and receive_code:
            return (
                f"https://115.com/s/{share_code}?"
                f"{urlencode({'password': receive_code})}"
            )
        return ""

    @staticmethod
    def _unlock_payload_url(payload: Dict[str, Any]) -> str:
        data = payload.get("payload") or {}
        if not isinstance(data, dict):
            return ""
        return Dian115SearchService._share_url(data)

    def _normalize_share(
            self,
            share: Dict[str, Any],
            resource: Dict[str, Any],
            resource_key: str,
            resource_path: str,
            media_type: str,
            tmdb_id: int,
            target_season: Optional[int],
            resource_list_mode: bool = False,
    ) -> Optional[Dict[str, Any]]:
        resource_type = self._resource_type(share)
        if not resource_type or (
                not resource_list_mode and resource_type not in self._resource_type_order_config
        ):
            return None
        if str(share.get("status") or "active").strip().lower() != "active":
            return None
        seasons = self._seasons(share)
        if (not resource_list_mode and media_type == "tv" and target_season is not None
                and int(target_season) not in seasons):
            return None

        share_id = int(share.get("id") or 0)
        if share_id <= 0:
            return None
        resource_id = int(share.get("resource_id") or 0)
        url = self._share_url(share)
        is_unlocked = bool(share.get("is_unlocked")) or bool(url)
        file_list = [
            str(value).strip()
            for value in (share.get("file_list") or [])
            if str(value or "").strip()
        ]
        try:
            unlock_points = max(0, int(share.get("unlock_cost") or 0))
        except (TypeError, ValueError):
            unlock_points = 0

        episodes = self._episode_values(
            share.get("episodes") or share.get("episodes_csv")
        )
        file_episodes: Dict[int, List[int]] = {}
        for file_name in file_list:
            parsed = MediaFileParser.extract_season_episode(file_name)
            if not parsed:
                continue
            file_season, file_episode = parsed
            file_episodes.setdefault(int(file_season), []).append(int(file_episode))
        if not episodes and file_episodes:
            episodes = sorted({
                episode for values in file_episodes.values() for episode in values
            })
        preview_episodes = {}
        for season in seasons or ([int(target_season)] if target_season is not None else []):
            preview_episodes[str(season)] = sorted(set(
                file_episodes.get(int(season), episodes)
            ))

        tag = share.get("tag_decoded") or {}
        if not isinstance(tag, dict):
            tag = {}
        tag_values = [
            tag.get("resolution"), tag.get("source"), tag.get("video_codec"),
            tag.get("audio_codec"), tag.get("hdr"), tag.get("frame_rate"),
            "中字" if tag.get("chn_sub") else "",
            str(share.get("subtitle_label") or "").strip(),
            str(share.get("file_extension") or "").strip().upper(),
        ]
        tags = unique_texts(tag_values)
        title = str(
            share.get("offline_title")
            or share.get("title_override")
            or share.get("file_name")
            or share.get("resource_title")
            or resource.get("title")
            or f"Dian115 分享 {share_id}"
        ).strip()
        page_url = f"{Dian115Client.BASE_URL}{resource_path}"
        return {
            "url": url,
            "title": title,
            "description": str(share.get("file_name") or "").strip(),
            "size": int(share.get("total_size_bytes") or 0),
            "size_human": str(share.get("total_size_human") or "").strip(),
            "file_list": file_list,
            "file_count": len(file_list),
            "episode_count": max(0, int(share.get("episode_count") or 0)),
            "tags": tags,
            "tag_decoded": dict(tag),
            "resource_type": resource_type,
            "source": "dian115",
            "source_url": page_url,
            "media_page_url": page_url,
            "resource_ref": str(share_id),
            "unlock_group": f"dian115:share:{share_id}",
            "need_unlock": not is_unlocked and unlock_points > 0,
            "need_access": not is_unlocked and unlock_points <= 0,
            "unlock_points": unlock_points,
            "is_unlocked": is_unlocked,
            "is_free": unlock_points <= 0,
            "preview_episodes": preview_episodes,
            "identity_verified": True,
            "target_season": (
                int(target_season) if target_season is not None else None
            ),
            "resolution": str(tag.get("resolution") or ""),
            "codec": str(tag.get("video_codec") or ""),
            "audio_codec": str(tag.get("audio_codec") or ""),
            "source_type": str(tag.get("source") or ""),
            "hdr_type": str(tag.get("hdr") or ""),
            "subtitle": str(share.get("subtitle_label") or ""),
            "update_time": share.get("created_at"),
            "provider_data": {
                "detail_path": resource_path,
                "resource_id": resource_id,
                "resource_key": resource_key,
                "tmdb_id": tmdb_id,
                "media_type": media_type,
                "season": int(target_season or 0),
            },
        }

    def search(self, query: SearchQuery) -> Optional[List[Dict[str, Any]]]:
        mediainfo = query.mediainfo
        media_type = query.media_type
        season = query.season
        target_episodes = list(query.target_episodes)
        subscribe = query.subscribe
        resource_list_mode = query.resource_list_mode
        result_limit = query.result_limit
        tmdb_id = mediainfo.tmdb_id or tmdb_id_of(subscribe)
        search_label = format_search_label(mediainfo, media_type, season)
        prefix = f"[{search_label}][DIAN115]"
        if not tmdb_id:
            logger.debug(f"{prefix} 缺少 TMDB ID，跳过查询")
            return []
        if not self._dian115_email or not self._dian115_password:
            logger.warning(f"{prefix} 已启用但未配置邮箱或密码")
            return []
        normalized_type = "movie" if media_type == MediaType.MOVIE else "tv"
        target_season = int(season or 0) if normalized_type == "tv" else 0
        try:
            resources = self._get_dian115_resources()
            detail = resources.resource_detail(
                int(tmdb_id), normalized_type, target_season
            )
            resource = detail.get("resource") or {}
            candidates = []
            shares = detail.get("shares") or []
            normalized_count = 0
            auto_unlock_skipped = 0
            inaccessible_skipped = 0
            for share in shares:
                if not isinstance(share, dict):
                    continue
                candidate = self._normalize_share(
                    share,
                    resource,
                    str(detail.get("resource_key") or ""),
                    str(detail.get("resource_path") or ""),
                    normalized_type,
                    int(tmdb_id),
                    season if normalized_type == "tv" else None,
                    resource_list_mode=resource_list_mode,
                )
                if not candidate:
                    continue
                normalized_count += 1
                if not resource_list_mode and candidate["need_unlock"] and not self._dian115_auto_unlock:
                    auto_unlock_skipped += 1
                    continue
                if not resource_list_mode and not candidate["url"] and not candidate["need_unlock"]:
                    inaccessible_skipped += 1
                    continue
                candidates.append(candidate)
                if resource_list_mode and len(candidates) >= max(
                        1, int(result_limit or self._dian115_candidate_limit)
                ):
                    break

            before_limit_count = len(candidates)
            if resource_list_mode:
                candidates = candidates[
                    :max(1, int(result_limit or self._dian115_candidate_limit))
                ]
            else:
                candidates = self._prefilter_resource_order(
                    candidates,
                    season=season,
                    target_episodes=target_episodes,
                )[:self._dian115_candidate_limit]
            logger.debug(
                f"{prefix} WebAPI 渠道统计：站点分享={len(shares)}，"
                f"规范化={normalized_count}，"
                f"待积分解锁 {sum(bool(item.get('need_unlock')) for item in candidates)} 个，"
                f"跳过（自动解锁关闭={auto_unlock_skipped}，"
                f"无可用链接={inaccessible_skipped}，"
                f"预筛/上限={max(0, before_limit_count - len(candidates))}）"
            )
            return candidates
        except Dian115Error as error:
            logger.error(
                f"{prefix} 查询失败："
                f"[{error.code or error.status_code or 'request'}] {error}"
            )
            return None
        except Exception as error:
            logger.error(f"{prefix} 查询异常：{error}")
            return None

    def unlock(
            self,
            candidate: Mapping[str, Any],
            search_label: str = "",
    ) -> Optional[str]:
        provider_data = candidate.get("provider_data") or {}
        share_id = int(candidate.get("resource_ref") or 0)
        resource_id = int(provider_data.get("resource_id") or 0)
        unlock_points = int(candidate.get("unlock_points") or 0)
        tmdb_id = int(provider_data.get("tmdb_id") or 0)
        media_type = str(provider_data.get("media_type") or "")
        season = int(provider_data.get("season") or 0)
        prefix = f"[{search_label}][DIAN115]" if search_label else "[DIAN115]"
        with self._dian115_budget.lock:
            cache_key = str(int(share_id or 0))
            cached_url = self._dian115_budget.cached_url(cache_key)
            if cached_url:
                logger.debug(
                    f"{prefix} 复用已取得的 Dian115 分享链接："
                    f"share_id={share_id}，订阅={self._dian115_budget.subscribe_key or '<none>'}，"
                    "跳过重复解锁和积分记账"
                )
                return cached_url
            budget_status = self._dian115_budget.status(unlock_points)
            if not budget_status:
                logger.warning(
                    f"{prefix} 解锁积分无效：share_id={share_id}，points={unlock_points}"
                )
                return None
            logger.debug(
                f"{prefix} Dian115 解锁预算快照：share_id={share_id}，"
                f"resource_id={resource_id}，media={media_type or '<unknown>'}，"
                f"tmdb_id={tmdb_id}，season={season}，"
                f"{self._dian115_budget.format_snapshot(unlock_points)}"
            )
            if not budget_status.allowed:
                logger.warning(
                    f"{prefix} 积分预算不足：share_id={share_id}，"
                    f"需要={budget_status.requested}，"
                    f"任务={budget_status.task_spent}/{budget_status.task_limit}，"
                    f"订阅={budget_status.subscribe_spent}/{budget_status.subscribe_limit}"
                )
                return None
            unlock_points = budget_status.requested
            try:
                result = self._get_dian115_resources().unlock_share(
                    share_id,
                    resource_id,
                    max_unlock_points=unlock_points,
                    tmdb_id=tmdb_id,
                    media_type=media_type,
                    season=season,
                )
                actual_points = self._dian115_budget.normalize_points(
                    result.get("actual_points")
                ) or 0
                url = self._unlock_payload_url(result)
                actual_points, before_task, before_subscribe = (
                    self._dian115_budget.record_result(
                        cache_key, url, actual_points
                    )
                )
                if not url:
                    logger.error(
                        f"{prefix} 解锁响应未返回可用链接：share_id={share_id}，"
                        f"已按服务端结果记录 {actual_points} 积分"
                    )
                    return None
                if actual_points > unlock_points:
                    logger.error(
                        f"{prefix} 实际扣费高于搜索时价格："
                        f"预计={unlock_points}，实际={actual_points}"
                    )
                remaining_task, remaining_subscribe = (
                    self._dian115_budget.remaining()
                )
                logger.debug(
                    f"{prefix} Dian115 积分记账：share_id={share_id}，"
                    f"服务端实际扣分={actual_points}，"
                    f"任务={before_task}->{self._dian115_budget.task_spent}，"
                    f"订阅={before_subscribe}->{self._dian115_budget.subscribe_spent}，"
                    f"缓存已取得链接=True"
                )
                logger.info(
                    f"{prefix} 已取得分享链接：share_id={share_id}，"
                    f"消耗 {actual_points} 积分；"
                    f"任务剩余 {remaining_task}，"
                    f"当前订阅剩余 {remaining_subscribe}"
                )
                return url
            except Dian115Error as error:
                logger.error(
                    f"{prefix} 解锁失败："
                    f"[{error.code or error.status_code or 'request'}] {error}"
                )
                return None
