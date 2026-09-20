"""Webhook 通知处理器。"""
from datetime import datetime
from typing import Any, Dict, List
from urllib.parse import urlsplit

from app.log import logger
from app.utils.http import RequestUtils


class WebhookHandler:
    """在转存成功后向第三方接口发送一次汇总通知。"""

    EVENT_TYPE = "PanSearch.TransferComplete"

    def __init__(self, enabled: bool, url: str, method: str = "POST", timeout: int = 10):
        self._enabled = bool(enabled)
        self._url = str(url or "").strip()
        self._method = str(method or "POST").upper()
        self._timeout = max(1, min(int(timeout or 10), 120))

    def send_transfer_complete(self, transfer_details: List[Dict[str, Any]], total_count: int) -> bool:
        if not self._enabled or not self._url or total_count <= 0:
            return False

        payload = {
            "type": self.EVENT_TYPE,
            "data": {
                "source": "PanSearch",
                "event": "transfer_complete",
                "total_count": total_count,
                "time": datetime.now().astimezone().isoformat(timespec="seconds"),
                "items": transfer_details or [],
            },
        }
        endpoint = self._safe_endpoint(self._url)

        try:
            request = RequestUtils(timeout=self._timeout)
            if self._method == "GET":
                response = request.get_res(self._url, params=payload)
            else:
                response = RequestUtils(
                    timeout=self._timeout,
                    content_type="application/json",
                ).post_res(self._url, json=payload)

            if response and response.ok:
                logger.info(f"Webhook 转存完成通知发送成功：{endpoint}")
                return True
            if response is not None:
                logger.warning(
                    f"Webhook 转存完成通知发送失败：{endpoint}，状态码={response.status_code}"
                )
            else:
                logger.warning(f"Webhook 转存完成通知发送失败：{endpoint}，未收到响应")
        except Exception as error:
            logger.warning(f"Webhook 转存完成通知发送异常：{endpoint}，{error}")
        return False

    @staticmethod
    def _safe_endpoint(url: str) -> str:
        """日志仅保留协议和主机，避免泄露 URL 中的令牌和查询参数。"""
        try:
            parsed = urlsplit(url)
            return f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else "已配置地址"
        except Exception:
            return "已配置地址"
