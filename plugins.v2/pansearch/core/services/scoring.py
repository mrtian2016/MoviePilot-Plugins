"""订阅已有资源评分。"""

from app.db import SessionFactory
from app.db.subscribe_oper import SubscribeOper
from app.log import logger
from app.schemas.types import MediaType

from ...core import OwnerDelegator


class SubscriptionScoringService(OwnerDelegator):
    """统一复用同步处理器的整季基线和规则评分。"""

    def _selected_tv_subscribes(self):
        selected_ids = set(self._upgrade_subscribe_ids or [])
        if not selected_ids:
            return []
        with SessionFactory() as db:
            subscribes = SubscribeOper(db=db).list() or []
        return [
            subscribe for subscribe in subscribes
            if subscribe.id in selected_ids and subscribe.type == MediaType.TV.value
        ]

    def _score_subscriptions(self, force: bool) -> dict:
        target_subscribes = self._selected_tv_subscribes()
        if not self._upgrade_subscribe_ids:
            message = "请先选择需要洗版的电视剧订阅"
            return {"success": False, "message": message, "results": []}
        if not target_subscribes:
            message = "所选项目中没有有效的电视剧订阅"
            return {"success": False, "message": message, "results": []}

        results = []
        updated_episodes = 0
        cleaned_episodes = 0
        failed = 0
        for subscribe in target_subscribes:
            season = int(subscribe.season or 1)
            label = f"{subscribe.name} S{season:02d}"
            try:
                mediainfo = self._sync_handler._subscribe_mediainfo(
                    subscribe, MediaType.TV
                )
                if not mediainfo:
                    failed += 1
                    results.append(f"{label}：媒体识别失败")
                    continue

                baseline = self._sync_handler._build_episode_baseline(
                    subscribe,
                    mediainfo,
                    season,
                    include_saved=not force,
                )
                scores = {
                    str(episode): int(item.get("score") or 0)
                    for episode, item in baseline.items()
                    if int(item.get("score") or 0) > 0
                }
                if not scores:
                    failed += 1
                    results.append(f"{label}：没有可评分的现有文件")
                    continue

                old_scores = self._sync_handler._read_ep_priority(subscribe)
                old_episode_keys = {str(key) for key in old_scores}
                cleaned = len(old_episode_keys - set(scores)) if force else 0
                SubscribeOper().update(
                    subscribe.id,
                    {"episode_priority": scores},
                )
                updated_episodes += len(scores)
                cleaned_episodes += cleaned
                result = f"{label}：已更新 {len(scores)} 集"
                if cleaned:
                    result += f"，清理 {cleaned} 条旧评分"
                results.append(result)
            except Exception as error:
                failed += 1
                results.append(f"{label}：{error}")
                logger.error(f"订阅评分失败 {label}：{error}")

        summary = (
            f"评分完成：{len(target_subscribes) - failed}/"
            f"{len(target_subscribes)} 个订阅，更新 {updated_episodes} 集"
        )
        if cleaned_episodes:
            summary += f"，清理 {cleaned_episodes} 条旧评分"
        logger.info(summary)
        return {
            "success": failed < len(target_subscribes),
            "message": summary,
            "results": results,
        }

    def _batch_re_score(self) -> dict:
        return self._score_subscriptions(force=False)

    def api_batch_re_score(self) -> dict:
        return self._batch_re_score()

    def _force_re_score(self) -> dict:
        return self._score_subscriptions(force=True)

    def api_force_re_score(self) -> dict:
        return self._force_re_score()
