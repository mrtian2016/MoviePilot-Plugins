# -*- coding: utf-8 -*-
"""PanSearch v1.5.5 F1: classified-root directory computation fix (red->green).

Red state (v1.5.4): _platform_classified_root overrides the MoviePilot
type directory library_path with the plugin cloud_media_path, dropping the
电视剧/电影 layer and producing paths like /影视库/国产剧/xxx. The plugin
then cannot locate files -> postprocess status never converges.

Green state (v1.5.5): library_path is never overridden; root is selected by
media type (movie_media_path / tv_media_path, fallback cloud_media_path);
MoviePilot rules still drive the category layer.

Covered:
1) MoviePilot dir hit (TV) -> classified root starts with /影视库/电视剧/
   and contains the category layer; movie -> /影视库/电影/.
2) get_dir miss -> fallback lands on <selected root>/电视剧 or /电影.
3) _select_media_root type-based selection (movie/tv/fallback to root_path).
4) _platform_rename_path final cloud dir prefix (acceptance-level).
5) grep: no more "library_path": root_path override; new config keys wired.
"""

import ast
import copy
import threading
import types
import unittest
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent

SERVICE_PATH = PLUGIN_ROOT / "handlers" / "sync" / "service.py"
INIT_PATH = PLUGIN_ROOT / "__init__.py"
SERVICE_SOURCE = SERVICE_PATH.read_text(encoding="utf-8")
INIT_SOURCE = INIT_PATH.read_text(encoding="utf-8")

HAS_SELECT_MEDIA_ROOT = "def _select_media_root" in SERVICE_SOURCE


def extract_methods(rel_path, names):
    source = (PLUGIN_ROOT / rel_path).read_text(encoding="utf-8")
    tree = ast.parse(source)
    found = {}
    statics = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for child in node.body:
                if isinstance(child, ast.FunctionDef) and child.name in names:
                    found[child.name] = ast.get_source_segment(source, child)
                    if any(
                        (isinstance(d, ast.Name) and d.id == "staticmethod")
                        or (isinstance(d, ast.Attribute) and d.attr == "staticmethod")
                        for d in child.decorator_list
                    ):
                        statics.add(child.name)
    missing = set(names) - set(found)
    assert not missing, "missing methods: %s" % missing
    return found, statics


def _bind(stub, namespace, *names, statics=()):
    for name in names:
        exec(namespace[name], namespace)
        if name in statics:
            stub.__dict__[name] = namespace[name]
        else:
            stub.__dict__[name] = types.MethodType(namespace[name], stub)


class _StubLogger:
    def debug(self, *args, **kwargs):
        pass

    info = warning = error = debug


class _FakeCache(dict):
    """dict with .set() to mimic the platform TTL cache used by service.py."""

    def set(self, key, value, **kwargs):
        self[key] = value


class _MediaType:
    MOVIE = types.SimpleNamespace(value="电影")
    TV = types.SimpleNamespace(value="电视剧")


class _FakeDirectory:
    """Mimic MoviePilot Directory pydantic object."""

    def __init__(self, library_path, library_category_folder=True, category=""):
        self.library_path = library_path
        self.library_category_folder = library_category_folder
        self.category = category

    def model_copy(self, deep=True, update=None):
        clone = _FakeDirectory(
            self.library_path, self.library_category_folder, self.category
        )
        for key, value in (update or {}).items():
            setattr(clone, key, value)
        return clone

    def copy(self, deep=True, update=None):
        return self.model_copy(deep=deep, update=update)


class _FakeTransHandler:
    """Mimic MoviePilot TransHandler.get_dest_dir:

    <library_path> [+ /<category> when category folder enabled] + /<title> (<year>)
    """

    def get_dest_dir(self, mediainfo=None, target_dir=None):
        base = str(target_dir.library_path).rstrip("/") or "/"
        if getattr(target_dir, "library_category_folder", False):
            category = getattr(mediainfo, "category", None) or "其他"
            base = f"{base}/{category}"
        title = getattr(mediainfo, "title", None) or "未知"
        year = getattr(mediainfo, "year", None)
        if year:
            return f"{base}/{title} ({year})"
        return f"{base}/{title}"


def _make_namespace(directory_result, trans_handler):
    ns = {
        "logger": _StubLogger(),
        "Path": Path,
        "MediaType": _MediaType,
        "DirectoryHelper": lambda: types.SimpleNamespace(
            get_dir=lambda media=None, include_unsorted=False: directory_result
        ),
        "TransHandler": lambda: trans_handler,
        "media_identity": lambda media: ("themoviedb", getattr(media, "tmdb_id", None)),
        "normalize_platform_cache_key": lambda key: str(key),
        "Optional": __import__("typing").Optional,
        "Tuple": __import__("typing").Tuple,
        "List": __import__("typing").List,
        "Dict": __import__("typing").Dict,
        "Any": __import__("typing").Any,
        "Callable": __import__("typing").Callable,
        "Mapping": __import__("typing").Mapping,
        "MediaInfo": object,
    }
    return ns


def _make_stub():
    return types.SimpleNamespace(
        _platform_root_lock=threading.RLock(),
        _platform_root_cache=_FakeCache(),
        _CLOUD_MEDIA_ROOT="/影视库",
        _MOVIE_MEDIA_ROOT="/影视库/电影",
        _TV_MEDIA_ROOT="/影视库/电视剧",
    )


def _tv_mediainfo(category="国产剧", title="交锋", year=2024):
    return types.SimpleNamespace(
        type=_MediaType.TV,
        category=category,
        title=title,
        year=year,
        tmdb_id=1001,
        name=title,
    )


def _movie_mediainfo(title="奥德赛", year=2024):
    return types.SimpleNamespace(
        type=_MediaType.MOVIE,
        category="科幻",
        title=title,
        year=year,
        tmdb_id=2002,
        name=title,
    )


class TestClassifiedRootNoOverride(unittest.TestCase):
    """MoviePilot dir hit: type layer must survive, category layer present."""

    def _classified(self, directory_result, root_path, mediainfo):
        trans = _FakeTransHandler()
        ns, statics = extract_methods(
            "handlers/sync/service.py", ["_platform_classified_root"]
        )
        ns.update(_make_namespace(directory_result, trans))
        stub = _make_stub()
        _bind(stub, ns, "_platform_classified_root", statics=statics)
        return stub._platform_classified_root(root_path, None, mediainfo)

    def test_tv_dir_hit_keeps_tv_layer_and_category(self):
        tv_dir = _FakeDirectory(
            library_path="/影视库/电视剧", library_category_folder=True
        )
        resolved = self._classified(tv_dir, "/影视库", _tv_mediainfo())
        self.assertIsNotNone(resolved)
        posix = resolved.as_posix()
        self.assertTrue(
            posix.startswith("/影视库/电视剧/"),
            "TV dir hit must keep /影视库/电视剧/ layer, got: %s" % posix,
        )
        self.assertIn("国产剧", posix)

    def test_movie_dir_hit_keeps_movie_layer(self):
        movie_dir = _FakeDirectory(
            library_path="/影视库/电影", library_category_folder=True
        )
        resolved = self._classified(movie_dir, "/影视库", _movie_mediainfo())
        self.assertIsNotNone(resolved)
        posix = resolved.as_posix()
        self.assertTrue(
            posix.startswith("/影视库/电影/"),
            "Movie dir hit must keep /影视库/电影/ layer, got: %s" % posix,
        )

    def test_get_dir_miss_falls_back_under_selected_root(self):
        resolved = self._classified(None, "/影视库/电视剧", _tv_mediainfo())
        self.assertIsNotNone(resolved)
        self.assertEqual(resolved.as_posix(), "/影视库/电视剧/电视剧")


class TestClassifiedRootGrep(unittest.TestCase):
    """F1 acceptance greps: no library_path override; new config keys wired."""

    def test_no_library_path_override(self):
        self.assertNotIn('"library_path": root_path', SERVICE_SOURCE)

    def test_type_based_root_selection_present(self):
        self.assertIn("MediaType.MOVIE.value", SERVICE_SOURCE)
        self.assertIn("MediaType.TV.value", SERVICE_SOURCE)

    def test_movie_tv_media_path_config_keys(self):
        self.assertIn('config.get("movie_media_path"', INIT_SOURCE)
        self.assertIn('config.get("tv_media_path"', INIT_SOURCE)

    def test_sync_handler_passes_movie_tv_roots(self):
        self.assertIn("movie_media_root=", INIT_SOURCE)
        self.assertIn("tv_media_root=", INIT_SOURCE)

    def test_service_ctor_has_movie_tv_root_params(self):
        self.assertIn("movie_media_root: str =", SERVICE_SOURCE)
        self.assertIn("tv_media_root: str =", SERVICE_SOURCE)


@unittest.skipUnless(HAS_SELECT_MEDIA_ROOT, "F1 _select_media_root not implemented yet")
class TestMediaRootSelection(unittest.TestCase):
    """Type-based root selection used by _platform_rename_path."""

    def setUp(self):
        ns, statics = extract_methods(
            "handlers/sync/service.py", ["_select_media_root"]
        )
        ns.update(_make_namespace(None, _FakeTransHandler()))
        self.stub = _make_stub()
        _bind(self.stub, ns, "_select_media_root", statics=statics)

    def test_tv_uses_tv_media_root(self):
        self.assertEqual(
            self.stub._select_media_root("/影视库", _tv_mediainfo()),
            "/影视库/电视剧",
        )

    def test_movie_uses_movie_media_root(self):
        self.assertEqual(
            self.stub._select_media_root("/影视库", _movie_mediainfo()),
            "/影视库/电影",
        )

    def test_unknown_type_falls_back_to_root_path(self):
        unknown = types.SimpleNamespace(type=types.SimpleNamespace(value="综艺"))
        self.assertEqual(
            self.stub._select_media_root("/影视库", unknown), "/影视库"
        )

    def test_empty_configured_root_falls_back_to_root_path(self):
        self.stub._TV_MEDIA_ROOT = ""
        self.assertEqual(
            self.stub._select_media_root("/影视库", _tv_mediainfo()), "/影视库"
        )


    def test_local_resource_root_not_hijacked_tv(self):
        # Regression lock (F3 Finding A): local root must pass through unchanged.
        self.assertEqual(
            self.stub._select_media_root("/app/media", _tv_mediainfo()),
            "/app/media",
        )

    def test_local_resource_root_not_hijacked_movie(self):
        self.assertEqual(
            self.stub._select_media_root("/app/media", _movie_mediainfo()),
            "/app/media",
        )

@unittest.skipUnless(HAS_SELECT_MEDIA_ROOT, "F1 _select_media_root not implemented yet")
class TestRenamePathLevel(unittest.TestCase):
    """Acceptance-level: final cloud dir prefix via _platform_rename_path."""

    def setUp(self):
        class _FakeMetaInfo:
            def __init__(self, source_name):
                self.source_name = source_name

        class _FakeFileManagerModule:
            @staticmethod
            def recommend_name(meta, media):
                return "交锋.S01E01.mkv"

        self.tv_dir = _FakeDirectory(
            library_path="/影视库/电视剧", library_category_folder=True
        )
        ns, statics = extract_methods(
            "handlers/sync/service.py",
            [
                "_platform_classified_root",
                "_platform_rename_path",
                "_effective_mediainfo",
                "_select_media_root",
            ],
        )
        ns.update(_make_namespace(self.tv_dir, _FakeTransHandler()))
        ns["copy"] = copy
        ns["MetaInfo"] = _FakeMetaInfo
        ns["FileManagerModule"] = _FakeFileManagerModule
        self.stub = _make_stub()
        _bind(
            self.stub,
            ns,
            "_platform_classified_root",
            "_platform_rename_path",
            "_effective_mediainfo",
            "_select_media_root",
            statics=statics,
        )

    def test_final_cloud_dir_starts_with_tv_library_and_has_category(self):
        target = self.stub._platform_rename_path(
            "/影视库", None, _tv_mediainfo(), "交锋.S01E01.mkv", season=1
        )
        self.assertIsNotNone(target)
        posix = target.as_posix()
        self.assertTrue(
            posix.startswith("/影视库/电视剧/"),
            "final cloud dir must start with /影视库/电视剧/, got: %s" % posix,
        )
        self.assertIn("国产剧", posix)


if __name__ == "__main__":
    unittest.main(verbosity=2)

