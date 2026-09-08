"""聚影资源搜索、缓存和短时票据兑换。"""

import re
import threading
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import urlparse

from app.log import logger

from .client import JuyingClient, JuyingError
from ..matching import (
    extract_season,
    extract_year,
    media_identifier_queries,
    normalize_title,
    title_matches,
)
from ..types import (
    RESOURCE_TYPE_ORDER,
    SUPPORTED_RESOURCE_TYPES,
    TYPE_HOSTS,
    resource_type_from_text,
    resource_type_from_url,
)
from ...utils.cache import (
    create_platform_ttl_cache,
    normalize_platform_cache_key,
)


class JuyingResourceService:
    """使用认证客户端查询资源，不管理登录会话。"""

    _MAX_RESOURCE_PAGES = 10

    def __init__(self, client: JuyingClient):
        self._client = client
        self._lock = threading.RLock()
        cache_namespace = str(
            getattr(client, "cache_namespace", "") or ""
        ).strip()
        cache_identity = (
            f"{cache_namespace or f'instance:{id(client)}'}|"
            f"{getattr(client, 'base_url', '')}|"
            f"{str(getattr(client, 'username', '') or '').strip().casefold()}"
        )
        self._search_cache = create_platform_ttl_cache(
            "juying:search", cache_identity, maxsize=128, ttl=15 * 60
        )
        self._resource_cache = create_platform_ttl_cache(
            "juying:resources", cache_identity, maxsize=1024, ttl=5 * 60
        )
        self._resource_context = create_platform_ttl_cache(
            "juying:resource_context", cache_identity,
            maxsize=1024, ttl=15 * 60,
        )

    @staticmethod
    def _safe_int(value: Any) -> int:
        try:
            return int(str(value or "0").strip())
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _type_matches(value: object, media_type: str) -> bool:
        actual = str(value or "").strip().casefold()
        return actual == "tv" if media_type == "tv" else actual in {"movie", "anime", "doc"}

    def _select_movie(
            self, rows: Iterable[Dict[str, Any]], expected_titles: List[str],
            expected_year: str, media_type: str, tmdb_id: Optional[int],
    ) -> Optional[Dict[str, Any]]:
        best = None
        for index, row in enumerate(rows):
            exact_tmdb = bool(tmdb_id and self._safe_int(row.get("tmdb_id")) == int(tmdb_id))
            if not exact_tmdb and not self._type_matches(row.get("movie_type"), media_type):
                continue
            if not exact_tmdb and not title_matches(row.get("title"), expected_titles):
                continue
            row_year = extract_year(row.get("release_year") or row.get("year"))
            if media_type == "movie" and expected_year and row_year != expected_year:
                continue
            if media_type == "tv" and expected_year and row_year and row_year != expected_year:
                continue
            score = (1000 if exact_tmdb else 200) + (60 if row_year == expected_year else 0)
            candidate = (score, -index, row)
            if best is None or (score, -index) > (best[0], best[1]):
                best = candidate
        return best[2] if best is not None else None

    def _find_movie(self, title: str, alternative_titles: List[str], year: str,
                    media_type: str, tmdb_id: Optional[object],
                    douban_id: Optional[object], imdb_id: Optional[object]):
        titles = list(dict.fromkeys(str(value).strip() for value in [title, *alternative_titles]
                                    if str(value or "").strip()))
        identifier_queries = media_identifier_queries(
            tmdb_id=tmdb_id,
            douban_id=douban_id,
            imdb_id=imdb_id,
        )
        for label, query, response_field in identifier_queries:
            normalized_query = query.casefold()
            payload = self._client.request_json(
                "GET",
                "/api/app/movies/",
                params={
                    "q": query,
                    "page": 1,
                    "page_size": 24,
                    "exact": 1,
                    "count": 1,
                },
            )
            rows = [
                row for row in (payload.get("results") or [])
                if isinstance(row, dict)
            ]
            if not rows:
                continue
            selected = next(
                (
                    row for row in rows
                    if str(row.get(response_field) or "").strip().casefold()
                       == normalized_query
                ),
                None,
            )
            if not selected:
                logger.debug(f"[JUYING] {label} ID 查询未返回精确匹配：{query}")
                continue
            logger.debug(
                f"[JUYING] {label} ID 精确命中媒体："
                f"{selected.get('title') or selected.get('id') or query}"
            )
            return {**selected, "_identity_verified": True}

        attempted = set()
        for query in titles:
            for query_year in ([year, ""] if year else [""]):
                key = (normalize_title(query), query_year)
                if key in attempted:
                    continue
                attempted.add(key)
                params: Dict[str, Any] = {"q": query, "page": 1, "page_size": 30}
                if query_year:
                    params["year"] = query_year
                payload = self._client.request_json("GET", "/api/app/movies/", params=params)
                rows = [
                    row for row in (payload.get("results") or [])
                    if isinstance(row, dict)
                ]
                selected = self._select_movie(
                    rows,
                    titles, year, media_type, tmdb_id,
                )
                if selected:
                    return selected
        return None

    def _load_resources(self, movie_id: int) -> List[Dict[str, Any]]:
        resources, seen = [], set()
        for page in range(1, self._MAX_RESOURCE_PAGES + 1):
            payload = self._client.request_json(
                "GET", f"/api/app/movie/{movie_id}/resources/",
                params={"page": page, "page_size": 120 if page == 1 else 200},
            )
            rows = payload.get("resources") or []
            if not isinstance(rows, list) or not rows:
                break
            for row in rows:
                if not isinstance(row, dict):
                    continue
                resource_id = str(row.get("id") or "").strip()
                if resource_id and resource_id not in seen:
                    seen.add(resource_id)
                    resources.append(dict(row))
            if not payload.get("has_more"):
                break
        return resources

    @staticmethod
    def _resource_type(row: Dict[str, Any]) -> str:
        actual = resource_type_from_text(row.get("resource_type"))
        if actual:
            return actual
        marker = " ".join(
            str(row.get(key) or "").strip().casefold()
            for key in (
                "resource_type_display", "title", "description",
                "resource_description", "share_link", "raw_share_link",
                "share_link_with_code",
            )
            if str(row.get(key) or "").strip()
        )
        return resource_type_from_text(marker)

    @staticmethod
    def _resource_size(value: Any) -> Any:
        text = str(value or "").strip()
        if not text:
            return 0
        return f"{text}GB" if re.fullmatch(r"\d+(?:\.\d+)?", text) else text

    def _load_search_context(self, title: str, alternative_titles: List[str], year: str,
                             media_type: str, tmdb_id: Optional[object],
                             douban_id: Optional[object], imdb_id: Optional[object],
                             season: Optional[int],
                             force: bool = False,
                             filter_season: bool = True) -> List[Dict[str, Any]]:
        cache_key = (media_type, tmdb_id or 0, str(douban_id or ""),
                     str(imdb_id or "").casefold(), normalize_title(title),
                     tuple(normalize_title(item) for item in alternative_titles), year,
                     season or 0, bool(filter_season))
        cache_key = normalize_platform_cache_key(cache_key)
        if not force:
            cached = self._search_cache.get(cache_key)
            if cached is not None:
                logger.debug(f"[JUYING] 资源搜索缓存命中，候选 {len(cached)} 个")
                return [dict(item) for item in cached]
        movie = self._find_movie(
            title, alternative_titles, year, media_type,
            tmdb_id, douban_id, imdb_id,
        )
        if not movie:
            self._search_cache.set(cache_key, [])
            return []
        movie_id = self._safe_int(movie.get("id"))
        if not movie_id:
            self._search_cache.set(cache_key, [])
            return []
        source_url = f"{self._client.base_url}/movie/{movie_id}"
        identity_verified = bool(movie.get("_identity_verified"))
        context = {"title": title, "alternative_titles": alternative_titles, "year": year,
                   "media_type": media_type, "tmdb_id": tmdb_id,
                   "douban_id": douban_id, "imdb_id": imdb_id, "season": season,
                   "filter_season": filter_season}
        public_rows = []
        for row in self._load_resources(movie_id):
            resource_type = self._resource_type(row)
            resource_id = str(row.get("id") or "").strip()
            if not resource_type or not resource_id:
                continue
            resource_title = str(row.get("title") or row.get("description") or
                                 row.get("resource_description") or f"聚影资源 #{resource_id}").strip()
            resource_season = extract_season(resource_title)
            if (filter_season and media_type == "tv" and season
                    and resource_season not in (None, season)):
                continue
            self._resource_cache.set(resource_id, dict(row))
            self._resource_context.set(resource_id, dict(context))
            public_rows.append({"resource_id": resource_id,
                                "title": resource_title,
                                "description": str(
                                    row.get("resource_description") or row.get("description") or "").strip(),
                                "size": self._resource_size(row.get("file_size")),
                                "resource_type": resource_type,
                                "link_exposed": bool(row.get("link_exposed")),
                                "link_hidden_reason": str(row.get("link_hidden_reason") or ""),
                                "update_time": str(row.get("created_at") or ""),
                                "uploader": str(row.get("uploader") or ""),
                                "source_url": source_url,
                                "identity_verified": identity_verified,
                                "target_season": (
                                    int(season)
                                    if identity_verified and season else None
                                )})
        self._search_cache.set(
            cache_key, [dict(item) for item in public_rows]
        )
        return public_rows

    def _reload_resource(self, resource_id: str) -> Optional[Dict[str, Any]]:
        context = self._resource_context.get(resource_id)
        if not context:
            return None
        self._load_search_context(**context, force=True)
        cached = self._resource_cache.get(resource_id)
        return dict(cached) if isinstance(cached, dict) else None

    @staticmethod
    def _validate_target(resource_type: str, target: str) -> None:
        scheme = str(urlparse(target).scheme or "").casefold()
        if (
                resource_type not in SUPPORTED_RESOURCE_TYPES
                or resource_type_from_url(target) != resource_type
                or (resource_type in TYPE_HOSTS and scheme != "https")
        ):
            raise JuyingError("聚影返回的资源链接格式异常", "juying_invalid_link")

    @staticmethod
    def _access_path(raw: Dict[str, Any], resource_id: str) -> str:
        endpoint = str(raw.get("access_endpoint") or "").strip()
        expected = f"/api/app/resource/{resource_id}/access/"
        return endpoint if endpoint == expected else expected

    @staticmethod
    def _append_access_code(resource_type: str, target: str, access_code: str) -> str:
        access_code = str(access_code or "").strip()
        if not access_code:
            return target
        if (
                resource_type == "115"
                and re.fullmatch(r"[A-Za-z0-9]{4}", access_code)
                and not re.search(r"[?&]password=", target, re.I)
        ):
            return f"{target}{'&' if '?' in target else '?'}password={access_code}"
        if resource_type == "123" and not re.search(r"[?&](?:pwd|code)=", target, re.I):
            return f"{target}{'&' if '?' in target else '?'}pwd={access_code}"
        if resource_type == "guangya" and not re.search(r"[?&](?:pwd|code)=", target, re.I):
            return f"{target}{'&' if '?' in target else '?'}code={access_code}"
        if resource_type == "quark" and not re.search(r"(?:提取码|密码|code)", target, re.I):
            return f"{target} 提取码: {access_code}"
        return target

    def _resolve_resource(self, resource_id: str) -> Dict[str, Any]:
        raw = self._resource_cache.get(resource_id)
        if not isinstance(raw, dict):
            raw = self._reload_resource(resource_id)
        if not isinstance(raw, dict):
            raise JuyingError("聚影资源票据已失效，请重新搜索", "juying_ticket_expired")
        if not raw.get("link_exposed"):
            raise JuyingError(str(raw.get("link_hidden_reason") or "当前账号不可访问该资源"),
                              "juying_link_hidden")
        ticket = str(raw.get("access_ticket") or "").strip()
        if not ticket:
            raise JuyingError("聚影资源缺少访问票据", "juying_ticket_expired")
        try:
            payload = self._client.request_json(
                "POST",
                self._access_path(raw, resource_id),
                protected_access=True,
                json={"access_ticket": ticket},
            )
        except JuyingError as error:
            if error.code not in {"juying_request_failed", "juying_access_forbidden"}:
                raise
            logger.debug(
                f"[JUYING] 资源 #{resource_id} access={error.code}，"
                "强制刷新资源列表后重试一次"
            )
            refreshed = self._reload_resource(resource_id)
            refreshed_ticket = str((refreshed or {}).get("access_ticket") or "").strip()
            if not refreshed_ticket or refreshed_ticket == ticket:
                raise
            payload = self._client.request_json(
                "POST",
                self._access_path(refreshed or {}, resource_id),
                protected_access=True,
                json={"access_ticket": refreshed_ticket},
            )
        target = str(payload.get("target") or "").strip()
        if not target:
            raise JuyingError("聚影资源链接为空", "juying_empty_link")
        resource_type = resource_type_from_url(target)
        self._validate_target(resource_type, target)
        access_code = str(payload.get("access_code") or "").strip()
        target = self._append_access_code(resource_type, target, access_code)
        return {"url": target, "resource_type": resource_type,
                "access_mode": str(payload.get("access_mode") or ""),
                "expires_in": self._safe_int(payload.get("expires_in"))}

    def resolve_resource(self, resource_id: str) -> Dict[str, Any]:
        """按测试搜索返回的资源 ID 兑换一次实际链接。"""
        with self._lock:
            return self._resolve_resource(str(resource_id or "").strip())

    def search(self, title: str, alternative_titles: Iterable[str], year: object,
               media_type: str, tmdb_id: Optional[object],
               douban_id: Optional[object], imdb_id: Optional[object],
               season: Optional[int],
               resource_type_order: Iterable[str], limit: int = 5,
               resource_list_mode: bool = False) -> List[Dict[str, Any]]:
        normalized_limit = max(1, min(int(limit or 5), 80))
        allowed_order = (
            list(RESOURCE_TYPE_ORDER)
            if resource_list_mode
            else list(dict.fromkeys(
                str(value).strip().casefold()
                for value in resource_type_order
                if str(value).strip().casefold() in SUPPORTED_RESOURCE_TYPES
            ))
        )
        if not allowed_order:
            return []
        alternatives = [str(value).strip() for value in alternative_titles if str(value or "").strip()]
        with self._lock:
            rows = self._load_search_context(str(title or "").strip(), alternatives,
                                             extract_year(year), "tv" if media_type == "tv" else "movie",
                                             tmdb_id, douban_id, imdb_id, season,
                                             filter_season=not resource_list_mode)
            order_map = {value: index for index, value in enumerate(allowed_order)}
            fallback_order = len(allowed_order)
            rows.sort(
                key=lambda item: order_map.get(
                    item["resource_type"], fallback_order
                )
            )
            rows = [
                row for row in rows
                if row["resource_type"] in allowed_order
                   and (resource_list_mode or row["link_exposed"])
            ][:normalized_limit]
            results = []
            for row in rows:
                if resource_list_mode:
                    results.append({
                        **{
                            key: value for key, value in row.items()
                            if key != "resource_id"
                        },
                        "url": "",
                        "source_url": row["source_url"],
                        "provider_data": {
                            "resource_id": row["resource_id"]
                        },
                    })
                    continue
                try:
                    resolved = self._resolve_resource(row["resource_id"])
                except JuyingError as error:
                    if error.code == "juying_rate_limited":
                        raise
                    logger.debug(
                        f"[JUYING] 资源 #{row['resource_id']} 获取失败：{error.code}"
                    )
                    continue
                if resolved["resource_type"] not in allowed_order:
                    continue
                results.append({**resolved, "title": row["title"],
                                "description": row["description"], "size": row["size"],
                                "update_time": row["update_time"], "uploader": row["uploader"],
                                "source_url": row["source_url"],
                                "identity_verified": bool(
                                    row.get("identity_verified")
                                ),
                                "target_season": row.get("target_season"),
                                "provider_data": {
                                    "resource_id": row["resource_id"]
                                }})
            return results

    def clear_cache(self) -> Dict[str, int]:
        with self._lock:
            counts = {
                "search": len(list(self._search_cache.items())),
                "resource": len(list(self._resource_cache.items())),
                "context": len(list(self._resource_context.items())),
            }
            self._search_cache.clear()
            self._resource_cache.clear()
            self._resource_context.clear()
            return counts
