"""供智能体调用的精简业务工具。"""

import json
from typing import Optional, Type

from app.agent.tools.base import MoviePilotTool
from app.agent.tools.tags import ToolTag
from app.core.plugin import PluginManager
from app.utils.string import StringUtils
from pydantic import BaseModel

from .schemas import (
    PanSearchCacheClearInput,
    PanSearchCheckinInput,
    PanSearchCheckinHistoryInput,
    PanSearchConfigUpdateInput,
    PanSearchLinksInput,
    PanSearchPerformanceInput,
    PanSearchResourceSearchInput,
    PanSearchResourceSelectInput,
    PanSearchStatusInput,
    PanSearchSyncInput,
)


def _plugin():
    plugin = PluginManager().running_plugins.get("PanSearch")
    return plugin if plugin and getattr(plugin, "_agent_enabled", True) else None


class PanSearchStatusTool(MoviePilotTool):
    name: str = "pansearch_status"
    tags: list[str] = [ToolTag.Read, ToolTag.Plugin, ToolTag.Subscription]
    description: str = (
        "查询网盘订阅助手的运行状态、任务进度、转存汇总、缓存占用、网盘能力和最近记录。"
        "用户询问插件是否运行、处理到哪里或统计数据时使用；所有结果均使用中文说明。"
    )
    args_schema: Type[BaseModel] = PanSearchStatusInput

    def get_tool_message(self, **kwargs) -> Optional[str]:
        return "正在查询网盘订阅助手运行状态"

    async def run(self, include_recent: bool = True, **kwargs) -> str:
        plugin = _plugin()
        if not plugin:
            return "网盘订阅助手未运行"
        data = await self.run_blocking(
            "default",
            plugin.get_platform_overview,
            5 if include_recent else 0,
        )
        return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


class PanSearchSyncTool(MoviePilotTool):
    name: str = "pansearch_start_sync"
    tags: list[str] = [ToolTag.Write, ToolTag.Subscription, ToolTag.Plugin]
    description: str = (
        "启动一次网盘订阅同步搜索。仅在用户明确要求立即搜索、追更或同步订阅时使用；"
        "不要用它代替候选资源搜索，也不要在用户只查询状态时调用。"
    )
    args_schema: Type[BaseModel] = PanSearchSyncInput

    def get_tool_message(self, **kwargs) -> Optional[str]:
        return "正在启动网盘订阅搜索"

    async def run(self, explanation: str = "", **kwargs) -> str:
        plugin = _plugin()
        if not plugin:
            return "网盘订阅助手未运行"
        result = plugin.start_platform_sync()
        return str(result.get("message") or ("启动成功" if result.get("success") else "启动失败"))


class PanSearchLinksTool(MoviePilotTool):
    name: str = "pansearch_process_links"
    tags: list[str] = [
        ToolTag.Write,
        ToolTag.Resource,
        ToolTag.Subscription,
        ToolTag.Transfer,
    ]
    description: str = (
        "校验并提交用户直接提供的115分享、ED2K或Magnet链接。订阅不存在或未指定时，"
        "优先定位唯一匹配订阅；没有订阅时使用媒体名称快速识别 TMDB；只有一个候选会直接进入完整转存流程，多个候选会"
        "返回 selection_id，此时应让用户选择媒体类型和 TMDB ID 后再次调用本工具。"
        "仅处理用户明确提供的链接；"
        "搜索工具返回的候选必须改用 pansearch_select_resources，禁止复制或改写候选链接。"
    )
    args_schema: Type[BaseModel] = PanSearchLinksInput

    def get_tool_message(self, **kwargs) -> Optional[str]:
        links = kwargs.get("resource_links") or []
        return f"正在校验并提交 {len(links)} 个网盘资源"

    async def run(
            self,
            subscribe_id: Optional[int] = None,
            resource_links: Optional[list[str]] = None,
            title: Optional[str] = None,
            media_type: Optional[str] = None,
            season: Optional[int] = None,
            seasons: Optional[list[int]] = None,
            selection_id: Optional[str] = None,
            tmdb_id: Optional[int] = None,
            **kwargs,
    ) -> str:
        plugin = _plugin()
        if not plugin:
            return "网盘订阅助手未运行"
        if not resource_links and not selection_id:
            return "请提供资源链接，或提供上次返回的 selection_id 与已选 TMDB ID"
        if media_type and media_type not in {"movie", "tv"}:
            return "媒体类型仅支持 movie（电影）或 tv（电视剧）"
        if selection_id and (not tmdb_id or media_type not in {"movie", "tv"}):
            return "选择 TMDB 候选时请同时提供 selection_id、media_type 和 tmdb_id"
        if season is not None and seasons:
            return "season 与 seasons 只能提供一个"
        result = await self.run_blocking(
            "storage",
            plugin.submit_platform_links,
            subscribe_id=subscribe_id,
            resource_links=resource_links,
            title=title or "",
            media_type=media_type or "",
            season=season,
            seasons=seasons,
            selection_id=selection_id or "",
            tmdb_id=tmdb_id,
            selection_scope=f"agent:{self._session_id}",
        )
        return json.dumps(result, ensure_ascii=False, indent=2, default=str)


class PanSearchCheckinTool(MoviePilotTool):
    name: str = "pansearch_checkin"
    tags: list[str] = [ToolTag.Write, ToolTag.Plugin]
    description: str = (
        "立即执行网盘订阅助手签到。可指定渠道，省略时签到全部已启用渠道。"
        "normal 为普通签到；gambler/lucky 为渠道配置的其他签到模式。"
    )
    args_schema: Type[BaseModel] = PanSearchCheckinInput

    def get_tool_message(self, **kwargs) -> Optional[str]:
        return "正在执行网盘渠道签到"

    async def run(
            self,
            provider: Optional[str] = None,
            mode: Optional[str] = None,
            **kwargs,
    ) -> str:
        plugin = _plugin()
        if not plugin:
            return "网盘订阅助手未运行"
        result = await self.run_blocking(
            "web",
            plugin.run_quick_checkin,
            provider=provider or "",
            mode=mode or "",
        )
        return json.dumps(result, ensure_ascii=False, indent=2, default=str)


class PanSearchCheckinHistoryTool(MoviePilotTool):
    name: str = "pansearch_checkin_history"
    tags: list[str] = [ToolTag.Read, ToolTag.Plugin]
    description: str = (
        "按渠道列举网盘订阅助手的签到详情，包括执行时间、状态、模式、积分变化、"
        "当前积分和累计签到天数。不会返回 HTTP、错误码或验证码等内部信息。"
    )
    args_schema: Type[BaseModel] = PanSearchCheckinHistoryInput

    def get_tool_message(self, **kwargs) -> Optional[str]:
        return "正在查询网盘渠道签到详情"

    async def run(
            self,
            provider: Optional[str] = None,
            limit: int = 10,
            **kwargs,
    ) -> str:
        plugin = _plugin()
        if not plugin:
            return "网盘订阅助手未运行"
        result = await self.run_blocking(
            "default",
            plugin.list_checkin_details,
            provider=provider or "",
            limit=limit,
        )
        return json.dumps(result, ensure_ascii=False, indent=2, default=str)


class PanSearchResourceSearchTool(MoviePilotTool):
    name: str = "pansearch_search_resources"
    sends_message: bool = True
    return_direct: bool = True
    tags: list[str] = [
        ToolTag.Read,
        ToolTag.Resource,
        ToolTag.Subscription,
        ToolTag.Recommendation,
    ]
    description: str = (
        "按媒体名称或订阅 ID 直接搜索网盘候选资源，支持指定电视剧季号或最新季，"
        "返回候选列表、来源与类型汇总、"
        "MoviePilot规则优先级、清晰度、大小、更新时间、解锁成本和推荐候选ID。"
        "用户只提供媒体名称时直接调用本工具，不要先查询全部订阅。必须先调用本工具"
        "再做AI筛选或推荐，并使用中文展示候选和解释推荐理由。"
        "用户要手动选择时，支持按钮的渠道应调用 ask_user_choice，按钮值保留候选ID，"
        "收到选择后使用本工具返回的 search_id 调用 pansearch_select_resources。"
        "未关联现有订阅的搜索只能展示和推荐，不能直接转存。"
    )
    args_schema: Type[BaseModel] = PanSearchResourceSearchInput

    def get_tool_message(self, **kwargs) -> Optional[str]:
        target = str(kwargs.get("title") or "").strip()
        if not target:
            target = f"订阅 {kwargs.get('subscribe_id')}"
        season = "最新季" if kwargs.get("latest_season") else ""
        if kwargs.get("season"):
            season = f"第 {kwargs.get('season')} 季"
        return f"正在搜索 {target}{season} 的网盘候选资源"

    @staticmethod
    def _format_size(value) -> str:
        try:
            size = int(value or 0)
        except (TypeError, ValueError):
            return str(value or "未知")
        if size <= 0:
            return "未知"
        return StringUtils.format_size(size)

    @classmethod
    def _format_search_message(cls, result: dict) -> str:
        if not result.get("success"):
            return str(result.get("message") or "网盘资源搜索失败")

        media = result.get("media") or {}
        summary = result.get("summary") or {}
        candidates = result.get("candidates") or []
        season = media.get("season")
        heading = str(media.get("title") or "未知媒体")
        if season:
            heading += f" S{int(season):02d}"
        lines = [
            f"{heading}：找到 {summary.get('total', len(candidates))} 个网盘候选。"
        ]
        for item in candidates[:20]:
            details = [
                str(item.get("resource_type") or "unknown").upper(),
                str(item.get("source") or "unknown"),
            ]
            definition = str(item.get("resolution") or item.get("quality") or "").strip()
            if definition:
                details.append(definition)
            size = cls._format_size(item.get("size"))
            if size != "未知":
                details.append(size)
            if item.get("update_time"):
                details.append(str(item.get("update_time")))
            points = int(item.get("unlock_points") or 0)
            details.append(f"{points}积分" if points else "免费")
            lines.append(
                f"{item.get('candidate_id')}｜{item.get('title') or '未知资源'}\n"
                f"  {' · '.join(details)}"
            )

        recommended = result.get("recommended_candidate_ids") or []
        if recommended:
            lines.append(f"推荐候选：{', '.join(recommended)}")
        if media.get("subscribe_id"):
            lines.append("回复要处理的候选 ID；也可以要求我比较后再选择。")
        else:
            lines.append("当前媒体未绑定订阅，只能查看候选，不能直接转存。")
        return "\n\n".join(lines)

    async def run(
            self,
            subscribe_id: Optional[int] = None,
            title: Optional[str] = None,
            media_type: Optional[str] = None,
            season: Optional[int] = None,
            latest_season: bool = False,
            limit: int = 20,
            **kwargs,
    ) -> str:
        plugin = _plugin()
        if not plugin:
            return "网盘订阅助手未运行或智能体功能已关闭"
        title = str(title or "").strip()
        media_type = str(media_type or "").strip().lower()
        if not subscribe_id and not title:
            return "请提供媒体名称或订阅 ID"
        if media_type and media_type not in {"movie", "tv"}:
            return "媒体类型仅支持 movie（电影）或 tv（电视剧）"
        if season and latest_season:
            return "指定季号与最新季不能同时使用"
        result = await self.run_blocking(
            "web",
            plugin.search_platform_resources,
            self._session_id,
            subscribe_id,
            title,
            media_type,
            season,
            latest_season,
            limit,
        )
        await self.send_tool_message(
            self._format_search_message(result),
            title="网盘资源搜索",
        )
        self._agent_context["user_reply_sent"] = True
        self._agent_context["reply_mode"] = "pansearch_search"
        return json.dumps(result, ensure_ascii=False, indent=2, default=str)


class PanSearchResourceSelectTool(MoviePilotTool):
    name: str = "pansearch_select_resources"
    tags: list[str] = [
        ToolTag.Write,
        ToolTag.Resource,
        ToolTag.Subscription,
        ToolTag.Transfer,
    ]
    description: str = (
        "提交用户已确认的网盘候选资源。只能使用 pansearch_search_resources 最近一次"
        "搜索返回的 search_id 和候选ID，不能接收或构造原始链接。AI推荐后必须等待用户明确确认；"
        "搜索未关联现有订阅时不能提交，需先创建或选择订阅后重新搜索。"
        "若用户要求手动选择，先使用 ask_user_choice，收到选择结果后再调用本工具。"
    )
    args_schema: Type[BaseModel] = PanSearchResourceSelectInput

    def get_tool_message(self, **kwargs) -> Optional[str]:
        candidates = kwargs.get("candidate_ids") or []
        return f"正在提交用户选择的 {len(candidates)} 个候选资源"

    async def run(
            self,
            search_id: str,
            candidate_ids: list[str],
            **kwargs,
    ) -> str:
        plugin = _plugin()
        if not plugin:
            return "网盘订阅助手未运行或智能体功能已关闭"
        result = await self.run_blocking(
            "storage",
            plugin.select_platform_resources,
            self._session_id,
            search_id,
            candidate_ids,
        )
        return json.dumps(result, ensure_ascii=False, indent=2, default=str)


class PanSearchCacheClearTool(MoviePilotTool):
    name: str = "pansearch_clear_cache"
    tags: list[str] = [ToolTag.Write, ToolTag.Admin, ToolTag.Plugin, ToolTag.System]
    description: str = (
        "清理网盘订阅助手的搜索、候选资源、网盘分享和路径等运行缓存。"
        "仅在用户明确要求清理缓存并确认后调用；不会删除订阅、历史记录或网盘文件。"
    )
    require_admin: bool = True
    args_schema: Type[BaseModel] = PanSearchCacheClearInput

    def get_tool_message(self, **kwargs) -> Optional[str]:
        return "正在清理网盘订阅助手缓存"

    async def run(self, confirm: bool = False, **kwargs) -> str:
        if not confirm:
            return "请先向用户确认是否清理缓存，确认后将 confirm 设为 true"
        plugin = _plugin()
        if not plugin:
            return "网盘订阅助手未运行或智能体功能已关闭"
        result = await self.run_blocking("storage", plugin.api_vue_clear_cache)
        return json.dumps(result, ensure_ascii=False, indent=2, default=str)


class PanSearchPerformanceTool(MoviePilotTool):
    name: str = "pansearch_performance"
    tags: list[str] = [ToolTag.Read, ToolTag.Plugin, ToolTag.System]
    description: str = (
        "查询网盘订阅助手当前任务的排队时间、运行耗时、进度、转存吞吐、"
        "搜索源请求与缓存命中情况，以及同步阶段耗时。用户询问运行效率、"
        "任务是否卡住、搜索耗时或缓存效果时使用，并用中文汇总结论。"
    )
    args_schema: Type[BaseModel] = PanSearchPerformanceInput

    def get_tool_message(self, **kwargs) -> Optional[str]:
        return "正在汇总网盘订阅助手运行性能"

    async def run(self, include_tasks: bool = True, **kwargs) -> str:
        plugin = _plugin()
        if not plugin:
            return "网盘订阅助手未运行或智能体功能已关闭"
        result = plugin.get_runtime_performance(include_tasks=include_tasks)
        return json.dumps(result, ensure_ascii=False, indent=2, default=str)


class PanSearchConfigUpdateTool(MoviePilotTool):
    name: str = "pansearch_update_config"
    tags: list[str] = [ToolTag.Write, ToolTag.Admin, ToolTag.Plugin, ToolTag.Settings]
    description: str = (
        "修改网盘订阅助手允许智能体调整的非敏感配置，包括侧栏、智能体开关、通知、"
        "搜索缓存和并发性能参数。只传需要修改的字段；不支持 Cookie、账号凭据、"
        "站点接管、路径、Webhook 或解锁配置。修改前应向用户说明字段和值。"
    )
    require_admin: bool = True
    args_schema: Type[BaseModel] = PanSearchConfigUpdateInput

    def get_tool_message(self, **kwargs) -> Optional[str]:
        return "正在修改网盘订阅助手白名单配置"

    async def run(self, **kwargs) -> str:
        plugin = _plugin()
        if not plugin:
            return "网盘订阅助手未运行或智能体功能已关闭"
        updates = {
            key: value
            for key, value in kwargs.items()
            if key in plugin.AGENT_CONFIG_FIELDS and value is not None
        }
        result = await self.run_blocking(
            "default",
            plugin.update_agent_config,
            updates,
        )
        return json.dumps(result, ensure_ascii=False, indent=2, default=str)
