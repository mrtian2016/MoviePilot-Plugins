"""自动订阅渠道注册表。"""
from __future__ import annotations

from typing import Type

from .provider import SubscribeProvider


class SubscribeProviderRegistry:
    def __init__(self) -> None:
        self._providers: dict[str, Type[SubscribeProvider]] = {}

    def register(self, provider: Type[SubscribeProvider]) -> Type[SubscribeProvider]:
        key = str(provider.provider_id or "").strip().lower()
        if not key:
            raise ValueError("自动订阅 provider_id 不能为空")
        if key in self._providers and self._providers[key] is not provider:
            raise ValueError(f"自动订阅提供方重复注册：{key}")
        self._providers[key] = provider
        return provider

    def create_all(self) -> list[SubscribeProvider]:
        return [provider() for provider in self._providers.values()]

    def get(self, provider_id: str) -> SubscribeProvider | None:
        provider = self._providers.get(str(provider_id or "").strip().lower())
        return provider() if provider else None

    def ids(self) -> list[str]:
        return list(self._providers)


registry = SubscribeProviderRegistry()


def register(provider: Type[SubscribeProvider]) -> Type[SubscribeProvider]:
    return registry.register(provider)
