"""转存历史记录API。"""

from threading import Thread
from typing import Any, Dict, List

from app.core.config import settings
from app.log import logger

from .. import OwnerDelegator
from ..services.runtime import sync_lock


class HistoryApi(OwnerDelegator):
    """提供历史记录清理、删除和补发通知接口。"""

    def api_clear_history(
            self,
            apikey: str,
            force: bool = False,
            clear_points_history: bool = False,
    ) -> dict:
        if apikey != settings.API_TOKEN:
            return {"success": False, "message": "API密钥错误"}
        if not self._sync_handler:
            return {"success": False, "message": "同步处理器未初始化"}
        if not sync_lock.acquire(blocking=False):
            return {
                "success": False,
                "message": "订阅任务正在合并转存记录，请稍后再清理",
            }
        try:
            result = self._sync_handler.clear_deletable_history(force=force)
            if clear_points_history and self._search_handler:
                result["points_history"] = (
                    self._search_handler.clear_point_history()
                )
        except Exception as error:
            logger.error(f"清空历史记录异常：{error}")
            return {"success": False, "message": str(error)}
        finally:
            sync_lock.release()
        logger.info(
            f"网盘订阅助手历史记录已清理：删除 {result['deleted']} 条，"
            f"保留 {result['retained']} 条，强制清理={'是' if force else '否'}"
        )
        message = f"已清理 {result['deleted']} 条历史记录"
        if result["retained"]:
            message += f"，保留 {result['retained']} 条处理中记录"
        points = result.get("points_history") or {}
        if points:
            message += (
                f"；已清理积分消费记录 HDHive={int(points.get('hdhive') or 0)}、"
                f"Dian115={int(points.get('dian115') or 0)}"
            )
        return {"success": True, "message": message, "data": result}

    def api_delete_history(self, apikey: str, identity: Dict[str, Any]) -> dict:
        if apikey != settings.API_TOKEN:
            return {"success": False, "message": "API密钥错误"}
        if not self._sync_handler:
            return {"success": False, "message": "同步处理器未初始化"}
        if not sync_lock.acquire(blocking=False):
            return {
                "success": False,
                "message": "订阅任务正在合并转存记录，请稍后再删除",
            }
        try:
            delete_linked_files = identity.get("delete_linked_files") is True
            deleted = self._sync_handler.delete_history_record(
                identity,
                delete_linked_files=delete_linked_files,
            )
        except Exception as error:
            logger.warning(f"删除单条历史记录失败：{error}")
            return {"success": False, "message": str(error)}
        finally:
            sync_lock.release()
        logger.info(
            f"已删除单条转存历史：{deleted.get('title') or '-'} / "
            f"{deleted.get('file_name') or '-'}"
        )
        linked_result = deleted.get("linked_delete") or {}
        if linked_result:
            cloud_status = (
                "网盘文件已移入回收站"
                if linked_result.get("cloud_file_deleted")
                else "网盘文件删除失败"
                if linked_result.get("cloud_file_error")
                else "网盘文件不存在"
            )
            strm_status = (
                "STRM已删除"
                if linked_result.get("strm_deleted")
                else "STRM删除失败"
                if linked_result.get("strm_error")
                else "STRM不存在"
            )
            message = f"历史记录已删除；{cloud_status}，{strm_status}"
        else:
            message = "历史记录已删除，网盘文件和STRM均已保留"
        if int(deleted.get("cache_deleted") or 0) > 0:
            message += "；跨盘缓存已清理"
        return {
            "success": True,
            "message": message,
            "data": linked_result,
        }

    def api_delete_history_batch(
            self,
            apikey: str,
            identities: List[Dict[str, Any]],
            delete_linked_files: bool = False,
    ) -> dict:
        if apikey != settings.API_TOKEN:
            return {"success": False, "message": "API密钥错误"}
        if not self._sync_handler:
            return {"success": False, "message": "同步处理器未初始化"}
        records = [item for item in identities if isinstance(item, dict)]
        if not records:
            return {"success": False, "message": "未选择可删除的历史记录"}
        if len(records) > 2000:
            return {"success": False, "message": "单次最多删除 2000 条历史记录"}
        if not sync_lock.acquire(blocking=False):
            return {
                "success": False,
                "message": "订阅任务正在合并转存记录，请稍后再删除",
            }
        try:
            result = self._sync_handler.delete_history_records(
                records,
                delete_linked_files=delete_linked_files,
            )
        except Exception as error:
            logger.warning(f"批量删除历史记录失败：{error}")
            return {"success": False, "message": str(error)}
        finally:
            sync_lock.release()

        logger.info(
            f"已批量删除转存历史：删除 {result['deleted']} 条，"
            f"保留 {result['skipped']} 条"
        )
        message = f"已删除 {result['deleted']} 条历史记录"
        if result["skipped"]:
            message += f"，保留 {result['skipped']} 条状态变化或处理失败记录"
        if delete_linked_files and result["linked_deleted"]:
            message += f"；同步处理 {result['linked_deleted']} 条关联文件"
        if result.get("cache_deleted"):
            message += f"；清理 {result['cache_deleted']} 个跨盘缓存文件"
        return {"success": True, "message": message, "data": result}

    def api_notify_history(self, apikey: str, identity: Dict[str, Any]) -> dict:
        if apikey != settings.API_TOKEN:
            return {"success": False, "message": "API密钥错误"}
        if not self._sync_handler:
            return {"success": False, "message": "同步处理器未初始化"}
        try:
            result = self._sync_handler.notify_history_record(identity)
            summary_title = str(result.get("summary_title") or "").strip()
            return {
                "success": True,
                "message": (
                    f"{summary_title} 的入库通知和Webhook已补发"
                    if summary_title
                    else "入库通知和Webhook已补发"
                ),
                "data": result,
            }
        except Exception as error:
            logger.warning(f"手动补发历史通知失败：{error}")
            return {"success": False, "message": str(error)}

    def api_upgrade_history(
            self, apikey: str, request: Dict[str, Any]
    ) -> dict:
        """提交历史记录或媒体服务器内容的手动洗版任务。"""
        if apikey != settings.API_TOKEN:
            return {"success": False, "message": "API密钥错误"}
        if not self._sync_handler:
            return {"success": False, "message": "同步处理器未初始化"}
        if not bool(getattr(self, "_enable_cloud_upgrade", False)):
            return {"success": False, "message": "请先在洗版设置中启用网盘洗版"}
        if getattr(self, "_sync_running", False) or sync_lock.locked():
            return {"success": False, "message": "已有订阅或洗版任务正在运行"}

        source = str((request or {}).get("source") or "history").strip().lower()
        if source == "media_server":
            items = [
                value for value in ((request or {}).get("items") or [])
                if isinstance(value, dict)
            ]
            if not items:
                return {"success": False, "message": "请选择至少一个媒体库内容"}
            if len(items) > 200:
                return {"success": False, "message": "单次最多选择 200 个媒体内容"}
            try:
                targets = self._sync_handler.resolve_media_server_upgrade_targets(items)
            except Exception as error:
                logger.warning(f"媒体库洗版目标校验失败：{error}")
                return {"success": False, "message": str(error)}
            upgrade_request = {"source": "resolved", "targets": targets}
            message = f"已提交 {len(items)} 个媒体库内容的洗版任务"
        else:
            records = [
                item for item in ((request or {}).get("records") or [])
                if isinstance(item, dict)
            ]
            if not records:
                return {"success": False, "message": "请选择可洗版的历史记录"}
            if len(records) > 2000:
                return {"success": False, "message": "单次最多选择 2000 条历史记录"}
            upgrade_request = {"source": "history", "records": records}
            message = "已提交历史洗版任务"

        Thread(
            target=self.sync_subscribes,
            kwargs={"upgrade_request": upgrade_request},
            daemon=True,
            name="pansearch-history-upgrade",
        ).start()
        return {"success": True, "message": message}
