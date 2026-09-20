"""Telegram 资源链接的消息路由接管。"""

import inspect
from functools import wraps
from threading import Lock, Thread
from typing import Any, Callable, Dict, Optional

from app.chain.message import MessageChain
from app.log import logger
from app.schemas.types import MessageChannel

from ..delegation import OwnerDelegator
from ...search.types import resource_type_from_url


class MessageRoutingHook(OwnerDelegator):
    """在全局智能体路由前接管 Telegram 资源链接。"""

    _patch_lock = Lock()
    _original: Optional[Callable] = None
    _wrapped: Optional[Callable] = None
    _signature: Optional[inspect.Signature] = None
    _active: Optional["MessageRoutingHook"] = None

    @staticmethod
    def _channel_value(channel: Any) -> str:
        return str(getattr(channel, "value", channel) or "").strip().lower()

    def _message_payload(self, arguments: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if not self._enabled or not self._direct_transfer_enabled:
            return None
        channel = arguments.get("channel")
        if self._channel_value(channel) != MessageChannel.Telegram.value.lower():
            return None
        text = str(arguments.get("text") or "").strip()
        if not text or text.startswith(("/", "CALLBACK:")):
            return None
        if (
                arguments.get("images")
                or arguments.get("files")
                or arguments.get("audio_refs")
                or arguments.get("has_audio_input")
        ):
            return None
        links = [
            link for link in self.extract_resource_links(text)
            if resource_type_from_url(link)
        ]
        if not links:
            return None
        return {
            "channel": channel,
            "source": arguments.get("source"),
            "user": arguments.get("userid"),
            "userid": arguments.get("userid"),
            "username": arguments.get("username"),
            "message_id": arguments.get("original_message_id"),
            "chat_id": arguments.get("original_chat_id"),
            "reply_to_message_id": arguments.get("reply_to_message_id"),
            "text": text,
            "links": links,
        }

    def _try_handle(self, arguments: Dict[str, Any]) -> bool:
        payload = self._message_payload(arguments)
        if not payload:
            return False
        try:
            Thread(
                target=self.handle_telegram_links,
                args=(payload,),
                daemon=True,
                name="pansearch-telegram-links",
            ).start()
        except Exception as error:
            logger.error(f"Telegram 资源链接处理线程启动失败：{error}")
            self._post_command_message(
                payload,
                "【网盘订阅】资源处理失败",
                "无法启动资源识别任务，请稍后重试。",
            )
        return True

    def install(self) -> None:
        """安装消息路由包装，并在插件重载时切换到最新实例。"""
        if not self._enabled or not self._direct_transfer_enabled:
            self.close()
            return
        cls = type(self)
        with cls._patch_lock:
            cls._active = self
            if cls._original is not None:
                return
            original = MessageChain._handle_message_core
            signature = inspect.signature(original)
            cls._original = original
            cls._signature = signature

            @wraps(original)
            def wrapped(chain_self, *args, **kwargs):
                active = cls._active
                if active:
                    try:
                        bound = signature.bind_partial(chain_self, *args, **kwargs)
                        if active._try_handle(dict(bound.arguments)):
                            return False
                    except Exception as error:
                        logger.error(f"Telegram 资源链接路由失败：{error}")
                return original(chain_self, *args, **kwargs)

            MessageChain._handle_message_core = wrapped
            cls._wrapped = wrapped

    def close(self) -> None:
        """卸载当前实例，并在没有接管方时恢复平台原方法。"""
        cls = type(self)
        with cls._patch_lock:
            if cls._active is self:
                cls._active = None
            original = cls._original
            if original and MessageChain._handle_message_core is cls._wrapped:
                MessageChain._handle_message_core = original
            cls._original = None
            cls._wrapped = None
            cls._signature = None
