"""自动订阅统一编排：全量抓取、识别、归并、查重和创建订阅。"""
from __future__ import annotations

import copy
import datetime
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Optional

from app.chain.download import DownloadChain
from app.chain.media import MediaChain
from app.chain.subscribe import SubscribeChain
from app.core.metainfo import MetaInfo
from app.db.subscribe_oper import SubscribeOper
from app.log import logger
from app.schemas.types import MediaType, NotificationType

from .models import (
    MediaCandidate,
    MediaIdentity,
    SubscribeOutcome,
    SubscribeStatus,
    media_identity,
)
from .provider import SubscribeContext
from .registry import SubscribeProviderRegistry, registry
from ..config import DEFAULT_AUTO_SUBSCRIBE_USERNAME
from ..media import (
    apply_media_identity,
    call_with_supported_kwargs,
    legacy_media_ids,
    media_identity as platform_media_identity,
    recognize_media,
    tmdb_id_of,
)
from ...utils.http_client import (
    build_proxy_url,
    normalize_proxies,
    request_error_summary,
    requests,
    validate_proxy_address,
)


@dataclass
class _ResolvedMedia:
    candidate: MediaCandidate
    mediainfo: Any
    meta: Any
    identity: MediaIdentity
    aliases: set[tuple[str, str, str, Optional[int]]] = field(default_factory=set)


@dataclass
class _LibraryProgress:
    """榜单目标在媒体库中的当前进度。"""

    complete: bool = False
    existing_episodes: list[int] = field(default_factory=list)
    missing_episodes: list[int] = field(default_factory=list)
    total_episode: int = 0
    start_episode: int = 1


class AutoSubscribeService:
    """四类榜单共享的唯一订阅落地入口。"""

    def __init__(
            self,
            owner: Any,
            provider_registry: SubscribeProviderRegistry = registry,
            media_chain: Any = None,
            download_chain: Any = None,
            subscribe_chain: Any = None,
            subscribe_oper: Any = None,
    ) -> None:
        self.owner = owner
        self.registry = provider_registry
        self.media_chain = media_chain or MediaChain()
        self.download_chain = download_chain or DownloadChain()
        self.subscribe_chain = subscribe_chain or SubscribeChain()
        self.subscribe_oper = subscribe_oper or SubscribeOper()
        self._existing_subscriptions: Optional[list[Any]] = None

    def run(self, notify: Optional[bool] = None) -> dict[str, Any]:
        """先收齐并识别所有渠道，跨渠道归并完成后再创建订阅。"""
        # 手动平台订阅可能在两次榜单运行之间发生变化，兜底查重必须读新快照。
        self._existing_subscriptions = None
        config = dict(getattr(self.owner, "_applied_config", None) or {})
        providers = self.registry.create_all()
        provider_names = {provider.provider_id for provider in providers}
        selected = {
            provider_id
            for provider_id in provider_names
            if bool(config.get(f"auto_subscribe_{provider_id}_enabled", False))
        }
        context = SubscribeContext(
            owner=self.owner,
            event=getattr(self.owner, "_stop_event", None),
            logger=logger,
            config=config,
            proxy=self._proxy_from_config(config),
        )
        candidates: list[MediaCandidate] = []
        provider_jobs = []
        for provider in providers:
            if provider.provider_id not in selected:
                continue
            options = self._provider_options(config, provider.provider_id)
            if not provider.has_listening(options):
                continue
            logger.debug(
                f"榜单渠道开始抓取：provider={provider.provider_id}, "
                f"options={self._debug_provider_options(options)}"
            )
            provider_jobs.append((provider, options))

        candidate_batches: list[list[MediaCandidate]] = [
            [] for _ in provider_jobs
        ]
        provider_errors: list[Optional[str]] = [None for _ in provider_jobs]
        if provider_jobs:
            logger.info(f"已启用 {len(provider_jobs)} 个榜单渠道，开始并发抓取")
            with ThreadPoolExecutor(
                    max_workers=len(provider_jobs),
                    thread_name_prefix="pansearch-auto-subscribe",
            ) as executor:
                futures = {
                    executor.submit(
                        self._fetch_provider_candidates,
                        provider,
                        options,
                        context,
                    ): (index, provider)
                    for index, (provider, options) in enumerate(provider_jobs)
                }
                for future in as_completed(futures):
                    index, provider = futures[future]
                    try:
                        candidate_batches[index] = future.result()
                    except Exception as error:
                        message = f"{provider.provider_name}抓取失败：{error}"
                        logger.error(message)
                        provider_errors[index] = message

        errors = [message for message in provider_errors if message]
        for (provider, options), batch in zip(provider_jobs, candidate_batches):
            for candidate in batch:
                if self._pre_filter(candidate, options, config):
                    candidates.append(candidate)

        resolved: list[_ResolvedMedia] = []
        outcomes: list[SubscribeOutcome] = []
        seen_candidates: set[str] = set()
        for candidate in candidates:
            if candidate.key() in seen_candidates:
                continue
            seen_candidates.add(candidate.key())
            result = self._recognize(candidate)
            if result is None:
                outcomes.append(SubscribeOutcome(
                    SubscribeStatus.UNRECOGNIZED,
                    candidate,
                    reason="未识别到媒体信息",
                ))
                continue
            options = self._provider_options(config, candidate.source)
            if not self._post_filter(result, options, config):
                outcomes.append(SubscribeOutcome(
                    SubscribeStatus.FILTERED,
                    candidate,
                    reason="未通过评分、类型或季过滤",
                    identity=result.identity,
                    mediainfo=result.mediainfo,
                ))
                continue
            resolved.append(result)

        groups = self._merge_resolved(resolved)
        subscribed_by_rank: dict[str, int] = {}
        for group in groups:
            open_ranks = self._open_rank_keys(group, config, subscribed_by_rank)
            if not open_ranks:
                outcomes.append(SubscribeOutcome(
                    SubscribeStatus.FILTERED,
                    group[0].candidate,
                    reason="已达到每榜新增数量",
                    identity=group[0].identity,
                    mediainfo=group[0].mediainfo,
                ))
                continue
            outcome = self._subscribe_group(group, config)
            outcomes.append(outcome)
            if outcome.status == SubscribeStatus.SUBSCRIBED:
                for key in open_ranks:
                    subscribed_by_rank[key] = subscribed_by_rank.get(key, 0) + 1

        stats: dict[str, int] = {}
        for outcome in outcomes:
            stats[outcome.status.value] = stats.get(outcome.status.value, 0) + 1
        subscribed = [
            self._subscribed_item(outcome)
            for outcome in outcomes
            if outcome.status == SubscribeStatus.SUBSCRIBED
        ]
        failed_subscriptions = stats.get(SubscribeStatus.ERROR.value, 0)
        summary = (
            f"自动订阅完成：抓取 {len(candidates)} 条，归并 {len(groups)} 个媒体，"
            f"新增订阅 {len(subscribed)} 个"
        )
        if errors:
            summary += f"，失败渠道 {len(errors)} 个"
        if failed_subscriptions:
            summary += f"，创建失败 {failed_subscriptions} 个"
        message_parts = [summary]
        if subscribed:
            message_parts.append(
                "已订阅：\n" + "\n".join(
                    f"- {item['display_name']}" for item in subscribed
                )
            )
        if errors:
            message_parts.append("抓取失败：\n" + "\n".join(f"- {error}" for error in errors))
        result = {
            "success": not errors and not failed_subscriptions,
            "message": "\n".join(message_parts),
            "data": {
                "candidates": len(candidates),
                "media": len(groups),
                "stats": stats,
                "subscribed": subscribed,
                "errors": errors,
            },
        }
        logger.info(result["message"])
        should_notify = (
            bool(config.get("auto_subscribe_notify"))
            if notify is None else bool(notify)
        )
        if should_notify:
            try:
                self.owner.post_message(
                    mtype=NotificationType.Plugin,
                    title="榜单自动订阅",
                    text=result["message"],
                )
            except Exception as error:
                logger.warning(f"榜单自动订阅通知发送失败：{error}")
        return result

    @staticmethod
    def _fetch_provider_candidates(
            provider: Any,
            options: dict[str, Any],
            context: SubscribeContext,
    ) -> list[MediaCandidate]:
        candidates = []
        for candidate in provider.fetch(options, context):
            if context.stopped():
                break
            if not isinstance(candidate, MediaCandidate):
                continue
            candidate.source = provider.provider_id
            candidates.append(candidate)
        return candidates

    def _subscribed_item(self, outcome: SubscribeOutcome) -> dict[str, Any]:
        candidate = outcome.candidate
        identity = outcome.identity
        title = self._localized_title(outcome.mediainfo) or candidate.title
        year = str(
            getattr(outcome.mediainfo, "year", None) or candidate.year or ""
        ).strip()
        season = identity.season if identity else candidate.season
        display_name = title
        if year:
            display_name += f" ({year})"
        if season is not None:
            display_name += f" 第 {season} 季"
        return {
            "title": title,
            "year": year or None,
            "media_type": identity.media_type if identity else candidate.media_type,
            "season": season,
            "subscribe_id": outcome.subscribe_id,
            "source": candidate.source,
            "display_name": display_name,
        }

    def run_auto_subscribe(self, notify: Optional[bool] = None) -> dict[str, Any]:
        return self.run(notify=notify)

    def api_run_auto_subscribe(self) -> dict[str, Any]:
        return self.run()

    def api_test_auto_subscribe(self, payload: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        """抓取指定榜单的少量示例，仅验证来源连通性，不使用任何过滤规则。"""
        config = dict(getattr(self.owner, "_applied_config", None) or {})
        submitted_config = (payload or {}).get("config")
        if isinstance(submitted_config, dict):
            config.update(submitted_config)
        provider_id = str((payload or {}).get("provider_id") or "").strip().lower()
        if provider_id not in self.registry.ids():
            return {"success": False, "message": "缺少或不支持的榜单来源"}
        provider = self.registry.get(provider_id)
        # 测试榜单不需要使用过滤规则简化代码：直接移除评分、年份和类型过滤
        options = dict(self._provider_options(config, provider_id))
        options.pop("min_year", None)
        options.pop("min_month", None)
        options.pop("min_vote", None)
        options.pop("media_type", None)
        options["_candidate_scan_limit"] = 3
        try:
            proxy = self._proxy_from_config(config, strict=True)
        except ValueError as error:
            return {"success": False, "message": f"榜单代理配置无效：{error}"}
        context = SubscribeContext(
            owner=self.owner,
            event=getattr(self.owner, "_stop_event", None),
            logger=logger,
            config=config,
            proxy=proxy,
        )
        samples = []
        try:
            for candidate in provider.fetch(options, context):
                samples.append({
                    "title": candidate.title,
                    "year": candidate.year,
                    "media_type": candidate.media_type,
                    "season": candidate.season,
                })
                if len(samples) >= 3:
                    break
        except Exception as error:
            logger.warning(f"{provider.provider_name} 测试失败：{error}")
            return {"success": False, "message": f"{provider.provider_name} 测试失败：{error}"}

        if samples:
            sample_titles = "、".join(str(item.get("title") or "") for item in samples)
            return {
                "success": True,
                "message": f"{provider.provider_name} 测试成功：{sample_titles}",
                "data": {"provider_id": provider_id, "items": samples},
            }

        return {
            "success": False,
            "message": f"{provider.provider_name} 已连通但未返回任何条目（请检查是否已选择监听榜单）",
            "data": {"provider_id": provider_id, "items": []},
        }

    def api_test_auto_subscribe_proxy(self, payload: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        """测试榜单通用代理，不依赖搜索渠道配置。"""
        data = dict(payload or {})
        response = None
        try:
            proxy = build_proxy_url(
                validate_proxy_address(data.get("proxy")),
                data.get("username"),
                data.get("password"),
            )
            started = time.perf_counter()
            response = requests.get(
                "https://www.cloudflare.com/cdn-cgi/trace",
                proxies=normalize_proxies(proxy),
                timeout=15,
                allow_redirects=True,
                impersonate="chrome",
            )
            if response.status_code != 200:
                return {"success": False, "message": f"代理测试失败：HTTP {response.status_code}"}
            latency_ms = max(0, round((time.perf_counter() - started) * 1000))
            return {"success": True, "message": f"代理连接成功（{latency_ms} ms）", "data": {"latency_ms": latency_ms}}
        except requests.exceptions.RequestException as error:
            return {"success": False, "message": f"代理测试失败：{request_error_summary(error)}"}
        except ValueError as error:
            return {"success": False, "message": f"代理测试失败：{error}"}
        finally:
            if response is not None:
                response.close()

    @staticmethod
    def _provider_options(config: dict[str, Any], provider_id: str) -> dict[str, Any]:
        prefix = f"auto_subscribe_{provider_id}_"
        options = {
            key[len(prefix):]: value
            for key, value in config.items()
            if key.startswith(prefix)
        }
        options.setdefault("min_month", datetime.datetime.now().month)
        return options

    @staticmethod
    def _debug_provider_options(options: dict[str, Any]) -> dict[str, Any]:
        """日志中保留连接诊断字段，避免输出代理密码等敏感配置。"""
        return {
            key: value
            for key, value in options.items()
            if key in {"rsshub_base", "rss_urls", "ranks", "base_url", "enabled"}
        }

    @classmethod
    def _provider_limit(cls, config: dict[str, Any], provider_id: str) -> int:
        defaults = {
            "douban": 30,
            "tmdb": 20,
            "bangumi": 50,
            "anilist": 30,
            "maoyan": 10,
            "netflix": 10,
            "mikan": 100,
        }
        value = cls._provider_options(config, provider_id).get("limit")
        return max(1, min(cls._as_int(value, defaults.get(provider_id, 20)), 100))

    def _proxy_from_config(
            self, config: dict[str, Any], strict: bool = False
    ) -> str:
        """复用搜索渠道的代理校验和鉴权拼接，供榜单测试及运行共用。"""
        raw_proxy = config.get("auto_subscribe_proxy")
        username = config.get("auto_subscribe_proxy_username", "")
        password = config.get("auto_subscribe_proxy_password", "")
        if not str(raw_proxy or "").strip():
            return ""
        try:
            address = validate_proxy_address(raw_proxy)
            return build_proxy_url(address, username, password)
        except ValueError as error:
            if strict:
                raise
            logger.warning(f"榜单自动订阅代理配置无效，本次使用直连：{error}")
            return ""

    @staticmethod
    def _as_int(value: Any, default: int = 0) -> int:
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return default

    def _pre_filter(
            self, candidate: MediaCandidate, options: dict[str, Any], config: dict[str, Any]
    ) -> bool:
        minimum_year = self._as_int(options.get("min_year"))
        candidate_year = self._as_int(candidate.year)
        if minimum_year and candidate_year and candidate_year < minimum_year:
            return False
        media_type = str(options.get("media_type") or "").strip().lower()
        legacy_media_types = options.get("media_types") or []
        media_types = (
            {media_type}
            if media_type and media_type != "all"
            else {
                str(value or "").strip().lower()
                for value in legacy_media_types
                if str(value or "").strip().lower() != "all"
            }
        )
        if media_types and candidate.media_type and candidate.media_type not in media_types:
            return False
        if candidate.media_type == "tv" and config.get("auto_subscribe_skip_season_zero", True):
            if int(1 if candidate.season is None else candidate.season) == 0:
                return False
        return True

    def _post_filter(
            self, resolved: _ResolvedMedia, options: dict[str, Any], config: dict[str, Any]
    ) -> bool:
        minimum_vote = float(options.get("min_vote") or 0)
        vote = float(getattr(resolved.mediainfo, "vote_average", 0) or 0)
        if minimum_vote and vote < minimum_vote:
            return False
        minimum_year = self._as_int(options.get("min_year"))
        media_year = self._as_int(
            getattr(resolved.mediainfo, "year", None) or resolved.candidate.year
        )
        if minimum_year and media_year and media_year < minimum_year:
            return False
        minimum_month = self._as_int(options.get("min_month"))
        media_month = self._media_month(resolved)
        if (
                minimum_year
                and minimum_month
                and media_year == minimum_year
                and media_month
                and media_month < minimum_month
        ):
            return False
        if resolved.identity.media_type == "tv" and config.get("auto_subscribe_skip_season_zero", True):
            resolved_season = getattr(resolved.mediainfo, "season", None)
            if resolved_season is None:
                resolved_season = resolved.identity.season
            if int(1 if resolved_season is None else resolved_season) == 0:
                return False
        return True

    @staticmethod
    def _media_month(resolved: _ResolvedMedia) -> int:
        """读取电影上映月或剧集首播月；来源未提供日期时不做月份误杀。"""
        values = [
            getattr(resolved.mediainfo, "release_date", None),
            getattr(resolved.mediainfo, "first_air_date", None),
            resolved.candidate.source_meta.get("release_date"),
            resolved.candidate.source_meta.get("first_air_date"),
            resolved.candidate.source_meta.get("air_date"),
        ]
        for value in values:
            if isinstance(value, (datetime.date, datetime.datetime)):
                return value.month
            matched = re.match(r"^\s*\d{4}[-/.](\d{1,2})", str(value or ""))
            if matched:
                month = int(matched.group(1))
                if 1 <= month <= 12:
                    return month
        return 0

    def _recognize(self, candidate: MediaCandidate) -> Optional[_ResolvedMedia]:
        meta = MetaInfo(candidate.title)
        meta.year = candidate.year
        if candidate.media_type == "movie":
            meta.type = MediaType.MOVIE
        elif candidate.media_type == "tv":
            meta.type = MediaType.TV
            meta.begin_season = int(1 if candidate.season is None else candidate.season)
        mediainfo = recognize_media(
            self.media_chain,
            meta=meta,
            mtype=getattr(meta, "type", None),
            tmdb_id=candidate.tmdb_id,
            douban_id=candidate.douban_id,
            bangumi_id=candidate.bangumi_id,
            cache=True,
        )
        if not mediainfo:
            return None
        candidate_tmdb_id = tmdb_id_of({"tmdb_id": candidate.tmdb_id})
        tmdb_id = tmdb_id_of(mediainfo) or candidate_tmdb_id
        if not tmdb_id:
            tmdb_media = recognize_media(
                self.media_chain,
                meta=meta,
                mtype=getattr(mediainfo, "type", getattr(meta, "type", None)),
                cache=True,
            )
            tmdb_id = tmdb_id_of(tmdb_media)
            if tmdb_id:
                mediainfo = tmdb_media
                logger.debug(
                    f"{candidate.source} 榜单已补充 TMDB 身份："
                    f"{candidate.title} -> {tmdb_id}"
                )
        if not tmdb_id:
            logger.warning(
                f"{candidate.source} 榜单未匹配到 TMDB，跳过：{candidate.title}"
            )
            return None
        apply_media_identity(mediainfo, "themoviedb", tmdb_id)
        if hasattr(mediainfo, "tmdb_id"):
            mediainfo.tmdb_id = int(tmdb_id)
        media_type = "movie" if mediainfo.type == MediaType.MOVIE else "tv"
        season = None if media_type == "movie" else int(
            1 if candidate.season is None else candidate.season
        )
        if media_type == "tv":
            meta.type = MediaType.TV
            meta.begin_season = season
        else:
            meta.type = MediaType.MOVIE
            meta.begin_season = None
        identity = media_identity(
            media_type=media_type,
            source="themoviedb",
            media_id=str(tmdb_id),
            season=season,
            title=getattr(mediainfo, "title", candidate.title),
            year=getattr(mediainfo, "year", candidate.year),
        )
        aliases = self._aliases(candidate, mediainfo, media_type, season)
        aliases.add(identity.key())
        return _ResolvedMedia(candidate, mediainfo, meta, identity, aliases)

    @staticmethod
    def _aliases(
            candidate: MediaCandidate, mediainfo: Any, media_type: str, season: Optional[int]
    ) -> set[tuple[str, str, str, Optional[int]]]:
        aliases = set()
        values = {
            "tmdb": (
                    tmdb_id_of(mediainfo)
                    or tmdb_id_of({"tmdb_id": candidate.tmdb_id})
            ),
            "douban": getattr(mediainfo, "douban_id", None) or candidate.douban_id,
            "bangumi": getattr(mediainfo, "bangumi_id", None) or candidate.bangumi_id,
            "imdb": getattr(mediainfo, "imdb_id", None) or candidate.imdb_id,
        }
        for source, value in values.items():
            if value not in (None, ""):
                aliases.add((media_type, source, str(value), season))
        return aliases

    @staticmethod
    def _merge_resolved(items: list[_ResolvedMedia]) -> list[list[_ResolvedMedia]]:
        """按任一强 ID 相交归并；不同季的电视剧永不合并。"""
        groups: list[list[_ResolvedMedia]] = []
        group_aliases: list[set[tuple[str, str, str, Optional[int]]]] = []
        for item in items:
            matched = [index for index, aliases in enumerate(group_aliases) if aliases & item.aliases]
            if not matched:
                groups.append([item])
                group_aliases.append(set(item.aliases))
                continue
            target = matched[0]
            groups[target].append(item)
            group_aliases[target].update(item.aliases)
            for index in reversed(matched[1:]):
                groups[target].extend(groups.pop(index))
                group_aliases[target].update(group_aliases.pop(index))
        return groups

    @staticmethod
    def _group_rank_keys(group: list[_ResolvedMedia]) -> set[str]:
        """返回归并媒体涉及的榜单桶，用于按榜单限制实际新增数量。"""
        keys = set()
        for item in group:
            candidate = item.candidate
            meta = candidate.source_meta or {}
            rank = meta.get("rank_key") or meta.get("category") or meta.get("rank") or "default"
            scope = meta.get("scope") or meta.get("platform") or ""
            keys.add(f"{candidate.source}:{rank}:{scope}")
        return keys

    @classmethod
    def _open_rank_keys(
            cls,
            group: list[_ResolvedMedia],
            config: dict[str, Any],
            subscribed_by_rank: dict[str, int],
    ) -> set[str]:
        return {
            key
            for key in cls._group_rank_keys(group)
            if subscribed_by_rank.get(key, 0)
               < cls._provider_limit(config, key.split(":", 1)[0])
        }

    def _subscribe_group(
            self, group: list[_ResolvedMedia], config: dict[str, Any]
    ) -> SubscribeOutcome:
        item = group[0]
        identity = item.identity
        season = identity.season
        skip_subscribe = bool(config.get("auto_subscribe_skip_subscribed", True))
        skip_history = bool(config.get("auto_subscribe_skip_history", True))
        skip_library = bool(config.get("auto_subscribe_skip_library", True))
        manual_match = self._manual_subscription_match(item, season) if skip_subscribe else None
        primary_exists = skip_subscribe and self._exists_primary(group, season)
        if skip_subscribe and (
                primary_exists
                or self._exists_any(self.subscribe_oper.exists, group, season)
                or manual_match is not None
        ):
            if not primary_exists and manual_match is not None:
                self._supplement_tmdb_identity(manual_match, item.mediainfo)
            return SubscribeOutcome(
                SubscribeStatus.SUBSCRIPTION_EXISTS,
                item.candidate,
                reason="活动订阅已存在",
                identity=identity,
                mediainfo=item.mediainfo,
            )
        if skip_history and self._exists_any(self.subscribe_oper.exist_history, group, season):
            return SubscribeOutcome(
                SubscribeStatus.SUBSCRIPTION_EXISTS,
                item.candidate,
                reason="历史订阅已存在",
                identity=identity,
                mediainfo=item.mediainfo,
            )
        library_progress = self._media_progress(item, season)
        logger.debug(
            f"榜单订阅媒体库检查：title={item.candidate.title}, season={season}, "
            f"complete={library_progress.complete}, "
            f"existing={library_progress.existing_episodes}, "
            f"missing={library_progress.missing_episodes}"
        )
        if skip_library and library_progress.complete:
            return SubscribeOutcome(
                SubscribeStatus.MEDIA_EXISTS,
                item.candidate,
                reason=f"媒体库已存在{f'第 {season} 季' if season else ''}",
                identity=identity,
                mediainfo=item.mediainfo,
            )

        subscribe_title = self._localized_title(item.mediainfo)
        if not subscribe_title:
            return SubscribeOutcome(
                SubscribeStatus.UNRECOGNIZED,
                item.candidate,
                reason="TMDB 未返回中文标题",
                identity=identity,
                mediainfo=item.mediainfo,
            )

        media_source, media_id = self._preferred_identity(item.mediainfo)
        sid, message = self.subscribe_chain.add(
            title=subscribe_title,
            year=getattr(item.mediainfo, "year", item.candidate.year),
            mtype=getattr(item.mediainfo, "type", None),
            tmdbid=tmdb_id_of(item.mediainfo),
            doubanid=(
                    getattr(item.mediainfo, "douban_id", None)
                    or item.candidate.douban_id
            ),
            bangumiid=(
                    getattr(item.mediainfo, "bangumi_id", None)
                    or item.candidate.bangumi_id
            ),
            media_source=media_source,
            media_id=media_id,
            season=season,
            note=library_progress.existing_episodes,
            total_episode=library_progress.total_episode or None,
            start_episode=(
                min(library_progress.missing_episodes)
                if library_progress.missing_episodes
                else library_progress.start_episode
            ),
            lack_episode=(
                len(library_progress.missing_episodes)
                if library_progress.missing_episodes
                else None
            ),
            username=str(
                config.get("auto_subscribe_username") or DEFAULT_AUTO_SUBSCRIBE_USERNAME
            ).strip(),
            exist_ok=False,
            message=False,
        )
        return SubscribeOutcome(
            SubscribeStatus.SUBSCRIBED if sid else SubscribeStatus.ERROR,
            item.candidate,
            reason=str(message or ""),
            subscribe_id=sid or None,
            identity=identity,
            mediainfo=item.mediainfo,
        )

    @staticmethod
    def _localized_title(mediainfo: Any) -> str:
        """只允许中文标题落订阅；优先使用 MoviePilot/TMDB 本地化后的主标题。"""
        values: list[Any] = [
            getattr(mediainfo, "title", None),
            getattr(mediainfo, "names", None),
            getattr(mediainfo, "hk_title", None),
            getattr(mediainfo, "tw_title", None),
            getattr(mediainfo, "sg_title", None),
        ]
        while values:
            value = values.pop(0)
            if isinstance(value, dict):
                values.extend(value.values())
                continue
            if isinstance(value, (list, tuple, set)):
                values.extend(value)
                continue
            title = str(value or "").strip()
            if title and re.search(r"[\u3400-\u9fff]", title):
                return title
        return ""

    @staticmethod
    def _identity_kwargs(group: list[_ResolvedMedia], season: Optional[int]) -> dict[str, Any]:
        item = group[0]
        media = item.mediainfo
        source, media_id = AutoSubscribeService._preferred_identity(media)
        return {
            "tmdbid": tmdb_id_of(media),
            "doubanid": getattr(media, "douban_id", None) or item.candidate.douban_id,
            "bangumiid": getattr(media, "bangumi_id", None) or item.candidate.bangumi_id,
            "media_source": source,
            "media_id": media_id,
            "season": season,
        }

    @classmethod
    def _identity_kwargs_list(
            cls, group: list[_ResolvedMedia], season: Optional[int]
    ) -> list[dict[str, Any]]:
        """为归并组生成所有稳定身份，兼容历史记录使用的非主来源。"""
        identities: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for item in group:
            media = item.mediainfo
            tmdb_id = (
                    tmdb_id_of(media)
                    or tmdb_id_of({"tmdb_id": item.candidate.tmdb_id})
            )
            source, media_id = AutoSubscribeService._preferred_identity(media)
            values = (
                ("themoviedb", tmdb_id),
                (source, media_id),
                ("douban", getattr(media, "douban_id", None) or item.candidate.douban_id),
                ("bangumi", getattr(media, "bangumi_id", None) or item.candidate.bangumi_id),
                ("imdb", getattr(media, "imdb_id", None) or item.candidate.imdb_id),
            )
            for identity_source, identity_id in values:
                if identity_source and identity_id not in (None, ""):
                    key = (str(identity_source).lower(), str(identity_id).strip())
                    if key in seen:
                        continue
                    seen.add(key)
                    identities.append({
                        "tmdbid": key[1] if key[0] == "themoviedb" else None,
                        "doubanid": key[1] if key[0] == "douban" else None,
                        "bangumiid": key[1] if key[0] == "bangumi" else None,
                        "media_source": key[0],
                        "media_id": key[1],
                        "season": season,
                    })
        return identities or [cls._identity_kwargs(group, season)]

    @staticmethod
    def _preferred_identity(media: Any) -> tuple[Optional[str], Optional[str]]:
        """TMDB 是影视订阅主身份，其他来源仅在 TMDB 缺失时使用。"""
        tmdb_id = tmdb_id_of(media)
        if tmdb_id:
            return "themoviedb", str(tmdb_id).strip()
        return platform_media_identity(media)

    @staticmethod
    def _exists(func: Any, kwargs: dict[str, Any]) -> bool:
        """兼容新旧 MoviePilot 的身份参数集合。"""
        return bool(call_with_supported_kwargs(func, kwargs))

    @classmethod
    def _exists_any(
            cls, func: Any, group: list[_ResolvedMedia], season: Optional[int]
    ) -> bool:
        """按归并组内全部身份查重，避免来源字段不同导致重复订阅。"""
        identity_kwargs = cls._identity_kwargs_list(group, season)
        return any(cls._exists(func, item) for item in identity_kwargs)

    def _exists_primary(
            self, group: list[_ResolvedMedia], season: Optional[int]
    ) -> bool:
        """优先判断 TMDB 主身份，避免更新豆瓣卡片时撞上已有 TMDB 卡片。"""
        identities = [
            item for item in self._identity_kwargs_list(group, season)
            if item.get("media_source") == "themoviedb"
        ]
        return any(self._exists(self.subscribe_oper.exists, item) for item in identities)

    def _manual_subscription_match(
            self, item: _ResolvedMedia, season: Optional[int]
    ) -> Optional[Any]:
        """按 TMDB 主身份查找，并兼容只有豆瓣身份的手动订阅。"""
        if self._existing_subscriptions is None:
            try:
                self._existing_subscriptions = list(self.subscribe_oper.list() or [])
            except Exception as error:
                logger.debug(f"读取平台订阅列表用于榜单查重失败：{error}")
                self._existing_subscriptions = []
        media = item.mediainfo
        media_type = getattr(media, "type", None)
        media_type = getattr(media_type, "value", media_type)
        media_type = str(media_type or "").strip().lower()
        year = str(getattr(media, "year", None) or item.candidate.year or "").strip()
        titles = {
            str(value or "").strip().casefold()
            for value in (
                item.candidate.title,
                self._localized_title(media),
                getattr(media, "title", None),
            )
            if str(value or "").strip()
        }
        if not titles:
            return None
        wanted_ids = {
            (str(identity.get("media_source") or "").lower(),
             str(identity.get("media_id") or "").strip())
            for identity in self._identity_kwargs_list([item], season)
            if identity.get("media_source") and identity.get("media_id")
        }
        for subscribe in self._existing_subscriptions:
            row_ids = set()
            source, media_id = platform_media_identity(subscribe)
            if source and media_id:
                row_ids.add((str(source).lower(), str(media_id).strip()))
            legacy = legacy_media_ids(subscribe)
            for source_name, field in (
                    ("themoviedb", "tmdbid"),
                    ("douban", "doubanid"),
                    ("bangumi", "bangumiid"),
            ):
                value = legacy.get(field)
                if value not in (None, ""):
                    row_ids.add((source_name, str(value).strip()))
            if wanted_ids.intersection(row_ids):
                row_type = getattr(subscribe, "type", None)
                if isinstance(subscribe, dict):
                    row_type = subscribe.get("type")
                row_type = getattr(row_type, "value", row_type)
                row_season = (
                    subscribe.get("season") if isinstance(subscribe, dict)
                    else getattr(subscribe, "season", None)
                )
                if str(row_type or "").strip() in {"电视剧", "tv"} and int(
                        1 if row_season is None else row_season
                ) != int(1 if season is None else season):
                    continue
                return subscribe
            name = str(
                subscribe.get("name") if isinstance(subscribe, dict)
                else getattr(subscribe, "name", "")
            ).strip().casefold()
            if not name or name not in titles:
                continue
            subscribe_year = str(
                subscribe.get("year") if isinstance(subscribe, dict)
                else getattr(subscribe, "year", "")
            ).strip()
            if year and subscribe_year and year != subscribe_year:
                continue
            subscribe_type = getattr(subscribe, "type", None)
            if isinstance(subscribe, dict):
                subscribe_type = subscribe.get("type")
            subscribe_type = getattr(subscribe_type, "value", subscribe_type)
            if media_type and str(subscribe_type or "").strip().lower() not in {
                media_type, "", "unknown"
            }:
                continue
            subscribe_season = (
                subscribe.get("season") if isinstance(subscribe, dict)
                else getattr(subscribe, "season", None)
            )
            if media_type in {"tv", "电视剧"} and int(
                    1 if subscribe_season is None else subscribe_season
            ) != int(1 if season is None else season):
                continue
            return subscribe
        return None

    def _supplement_tmdb_identity(self, subscribe: Any, mediainfo: Any) -> None:
        """把榜单识别到的 TMDB ID 补回手动订阅，避免再创建第二条记录。"""
        tmdb_id = tmdb_id_of(mediainfo)
        subscribe_id = getattr(subscribe, "id", None)
        if isinstance(subscribe, dict):
            tmdb_id = tmdb_id or (
                mediainfo.get("tmdb_id") if isinstance(mediainfo, dict) else None
            )
            subscribe_id = subscribe.get("id")
        if not tmdb_id or not subscribe_id:
            return

        payload: dict[str, Any] = {}
        fields = set(subscribe.keys()) if isinstance(subscribe, dict) else set(
            getattr(type(subscribe), "__table__", {}).columns.keys()
            if getattr(type(subscribe), "__table__", None) is not None else ()
        )
        if "tmdbid" in fields or hasattr(subscribe, "tmdbid"):
            current_tmdb = (
                subscribe.get("tmdbid") if isinstance(subscribe, dict)
                else getattr(subscribe, "tmdbid", None)
            )
            if str(current_tmdb or "") != str(tmdb_id):
                payload["tmdbid"] = int(tmdb_id)
        if (
                "media_source" in fields
                or hasattr(subscribe, "media_source")
        ) and (
                "media_id" in fields
                or hasattr(subscribe, "media_id")
        ):
            current_source, current_id = platform_media_identity(subscribe)
            if current_source != "themoviedb" or str(current_id or "") != str(tmdb_id):
                payload.update({"media_source": "themoviedb", "media_id": str(tmdb_id)})
        if not payload:
            return
        try:
            updated = self.subscribe_oper.update(int(subscribe_id), payload)
            if updated:
                for field, value in payload.items():
                    if isinstance(subscribe, dict):
                        subscribe[field] = value
                    else:
                        setattr(subscribe, field, value)
                logger.info(
                    f"手动订阅已合并 TMDB 身份：{getattr(subscribe, 'name', '')} -> {tmdb_id}"
                )
        except Exception as error:
            logger.warning(f"手动订阅补充 TMDB 身份失败：{subscribe_id} - {error}")

    def _media_progress(self, item: _ResolvedMedia, season: Optional[int]) -> _LibraryProgress:
        """只检查目标季，并保留媒体库已有的精确集数。"""
        media = copy.deepcopy(item.mediainfo)
        meta = MetaInfo(getattr(media, "title", item.candidate.title))
        meta.year = getattr(media, "year", item.candidate.year)
        meta.type = getattr(media, "type", None)
        if meta.type == MediaType.TV:
            target_season = int(1 if season is None else season)
            meta.begin_season = target_season
            source_seasons = dict(getattr(media, "seasons", None) or {})
            target_episodes = (
                    source_seasons.get(target_season)
                    or source_seasons.get(str(target_season))
                    or []
            )
            if not target_episodes:
                return _LibraryProgress(start_episode=target_season)
            target_episodes = sorted({int(episode) for episode in target_episodes})
            media.season = target_season
            media.seasons = {target_season: target_episodes}
            exists, no_exists = self.download_chain.get_no_exists_info(meta=meta, mediainfo=media)
            missing: set[int] = set()
            for seasons in (no_exists or {}).values():
                for info in (seasons or {}).values():
                    if getattr(info, "season", None) == target_season:
                        episodes = getattr(info, "episodes", None) or []
                        missing.update(
                            target_episodes
                            if not episodes
                            else (int(episode) for episode in episodes)
                        )
            if not exists and not missing:
                missing.update(target_episodes)
            existing = sorted(set(target_episodes) - missing)
            return _LibraryProgress(
                complete=bool(exists),
                existing_episodes=existing,
                missing_episodes=sorted(missing),
                total_episode=max(target_episodes),
                start_episode=min(target_episodes),
            )
        exists, _ = self.download_chain.get_no_exists_info(meta=meta, mediainfo=media)
        return _LibraryProgress(complete=bool(exists))

    def _media_exists(self, item: _ResolvedMedia, season: Optional[int]) -> bool:
        """兼容内部调用方，仅返回媒体库是否完整。"""
        return self._media_progress(item, season).complete
