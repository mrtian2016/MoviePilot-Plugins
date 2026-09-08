"""自动订阅渠道集合。"""

from .anilist import create_anilist_provider
from .bangumi import create_bangumi_provider
from .douban import create_douban_provider
from .maoyan import create_maoyan_provider
from .mikan import create_mikan_provider
from .netflix import create_netflix_provider
from .tmdb import create_tmdb_provider

__all__ = [
    "create_anilist_provider",
    "create_bangumi_provider",
    "create_douban_provider",
    "create_maoyan_provider",
    "create_mikan_provider",
    "create_netflix_provider",
    "create_tmdb_provider",
]
