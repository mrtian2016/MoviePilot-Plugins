# -*- coding: utf-8 -*-
"""Stub app.* / optional SDK modules so pansearch.drive imports on host."""

import importlib
import importlib.util
import sys
import types


class _AnyAttr:
    """Attribute-anywhere callable placeholder."""

    def __init__(self, name=""):
        self._name = name

    def __getattr__(self, item):
        if item.startswith("__"):
            raise AttributeError(item)
        return _AnyAttr(self._name + "." + item)

    def __call__(self, *args, **kwargs):
        return _AnyAttr(self._name + "()")


def _mod(name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


class _StubTTLCache:
    def __init__(self, maxsize=0, ttl=0, **kwargs):
        pass

    def __getitem__(self, key):
        raise KeyError(key)

    def get(self, key, default=None):
        return default

    def set(self, key, value, **kwargs):
        pass


class _StubMetaInfo:
    def __init__(self, title=""):
        self.title = title
        self.name = title
        self.type = None
        self.year = None
        self.resource_type = "unknown"
        self.unmatched = False


def install_stubs():
    """Register stub modules for app.* and optional SDKs (host only)."""
    existing = sys.modules.get("app")
    if existing is not None and not getattr(existing, "_pansearch_stub", False):
        return
    if existing is None:
        try:
            if importlib.util.find_spec("app") is not None:
                # 真实 MoviePilot 宿主（容器内）可用：直接用真 app 包。
                return
        except (ImportError, ValueError):
            pass

    class _SilentLogger:
        def debug(self, *a, **k):
            pass

        info = warning = error = exception = critical = log = debug

    app = _mod("app")
    app._pansearch_stub = True
    _mod("app.log", logger=_SilentLogger())
    _mod("app.core")
    _mod("app.core.cache", TTLCache=_StubTTLCache)
    _mod("app.core.config", settings=_AnyAttr("settings"))
    _mod("app.core.metainfo", MetaInfo=_StubMetaInfo, MetaInfoPath=_StubMetaInfo)
    _mod("app.utils")
    _mod("app.utils.string", StringUtils=_AnyAttr("StringUtils"))
    _mod("app.schemas", MediaInfo=_StubMetaInfo)
    _mod("app.schemas.types", MediaType=_AnyAttr("MediaType"))
    _mod("app.modules")
    _mod("app.modules.themoviedb")
    _mod("app.modules.themoviedb.scraper", TmdbScraper=_AnyAttr("TmdbScraper"))
    _mod("app.utils.http", RequestUtils=_AnyAttr("RequestUtils"))

    # Optional third-party SDKs: leave attributes undefined so the plugin's
    # own "try: from p115client import ... except ImportError" degrade
    # paths trigger exactly like a host without the packages installed.
    for name in (
        "p115client", "p115client.const", "p115client.tool",
        "p115client.tool.clouddownload", "p123client", "qrcode",
    ):
        if name not in sys.modules:
            try:
                importlib.import_module(name)
                continue
            except ImportError:
                pass
            _mod(name)
    sys.modules["p115client"].const = sys.modules["p115client.const"]
    sys.modules["p115client"].tool = sys.modules["p115client.tool"]
    sys.modules["p115client.tool"].clouddownload = sys.modules[
        "p115client.tool.clouddownload"
    ]
    if "torf" not in sys.modules:
        try:
            importlib.import_module("torf")
        except ImportError:
            _mod(
                "torf",
                Magnet=_AnyAttr("Magnet"),
                Torrent=_AnyAttr("Torrent"),
                TorfError=_AnyAttr("TorfError"),
            )


def prepare_plugin_packages(plugin_dir):
    """Register pansearch (and sub-packages) as path-backed shells so
    ``pansearch.drive.*`` imports resolve from disk WITHOUT executing the
    plugin entry ``pansearch/__init__.py`` (which needs the full MP host).
    """
    from pathlib import Path

    root = Path(plugin_dir).resolve()
    if "pansearch" not in sys.modules:
        module = types.ModuleType("pansearch")
        module.__path__ = [str(root)]
        module.__package__ = "pansearch"
        sys.modules["pansearch"] = module
