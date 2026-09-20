"""自动订阅公共能力。"""

from .models import (
    MediaCandidate,
    MediaIdentity,
    SubscribeOutcome,
    SubscribeStatus,
    media_identity,
)
from .provider import SubscribeContext, SubscribeProvider
from .registry import SubscribeProviderRegistry, registry
from .service import AutoSubscribeService

__all__ = [
    "AutoSubscribeService",
    "MediaCandidate",
    "MediaIdentity",
    "SubscribeContext",
    "SubscribeOutcome",
    "SubscribeProvider",
    "SubscribeProviderRegistry",
    "SubscribeStatus",
    "media_identity",
    "registry",
]
