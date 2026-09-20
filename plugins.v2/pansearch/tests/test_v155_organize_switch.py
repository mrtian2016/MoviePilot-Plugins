# -*- coding: utf-8 -*-
"""PanSearch v1.5.5 F3: organize_after_transfer closed loop + path source.

Behavior: when organize is OFF, files must stay in the staging dir
(cloud_transfer_path) and no media category dir is applied; when ON, the
computed media dir is kept. Wired through subtitles.py _finalize_subtitle_files
which shares the same item["cloud_dir"] contract as postprocess.

Path-source: every postprocess / scan path resolution must flow through the
F1 computed logic (_platform_rename_path -> _select_media_root), never the old
cloud_dir override.
"""

import ast
import types
import unittest
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent

SUBTITLES_PATH = PLUGIN_ROOT / "handlers" / "sync" / "subtitles.py"
POSTPROCESS_PATH = PLUGIN_ROOT / "handlers" / "sync" / "postprocess.py"
SERVICE_PATH = PLUGIN_ROOT / "handlers" / "sync" / "service.py"

SUBTITLES_SOURCE = SUBTITLES_PATH.read_text(encoding="utf-8")
POSTPROCESS_SOURCE = POSTPROCESS_PATH.read_text(encoding="utf-8")
SERVICE_SOURCE = SERVICE_PATH.read_text(encoding="utf-8")


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


class _CloudFile:
    def __init__(self, name="a.ass", file_id="1", sha1="", size=100):
        self.name = name
        self.id = file_id
        self.sha1 = sha1
        self.size = size


class TestSubtitleFinalizeOrganizeOff(unittest.TestCase):
    """Behavior: organize off rewrites item cloud_dir to staging; on keeps it."""

    def setUp(self):
        ns, statics = extract_methods(
            "handlers/sync/subtitles.py", ["_finalize_subtitle_files"]
        )
        ns["Path"] = Path
        ns["Any"] = __import__("typing").Any
        ns["Dict"] = __import__("typing").Dict
        ns["List"] = __import__("typing").List
        ns["Callable"] = __import__("typing").Callable
        ns["Optional"] = __import__("typing").Optional
        ns["CloudFile"] = _CloudFile
        ns["CloudDriveCapability"] = types.SimpleNamespace(FILE_DOWNLOAD="FILE_DOWNLOAD")
        ns["logger"] = _StubLogger()
        self.stub = types.SimpleNamespace(_organize_after_transfer=False)
        _bind(self.stub, ns, "_finalize_subtitle_files", statics=statics)

    def _item(self):
        return {
            "subtitles": [{"file_name": "a.ass", "source_name": "a.ass"}],
            "cloud_dir": "/影视库/电视剧/国产剧/交锋 (2026)/Season 1",
            "staging_dir": "/未整理/待整理",
            "file_name": "交锋.S01E01.mkv",
        }

    def test_organize_off_keeps_file_in_staging_no_category_dir(self):
        self.stub._organize_after_transfer = False
        item = self._item()
        result = self.stub._finalize_subtitle_files(
            item, lambda path: (False, {})
        )
        # snapshot invalid -> method bails; but the organize-off rewrite
        # already happened: cloud_dir pinned to staging, category dir unused.
        self.assertFalse(result)
        self.assertEqual(item["cloud_dir"], "/未整理/待整理")

    def test_organize_on_keeps_computed_media_dir(self):
        self.stub._organize_after_transfer = True
        item = self._item()
        result = self.stub._finalize_subtitle_files(
            item, lambda path: (False, {})
        )
        self.assertFalse(result)
        self.assertEqual(
            item["cloud_dir"], "/影视库/电视剧/国产剧/交锋 (2026)/Season 1"
        )

    def test_empty_subtitles_returns_true_untouched(self):
        item = {"subtitles": [], "cloud_dir": "/影视库/电影/奥德赛 (2024)"}
        result = self.stub._finalize_subtitle_files(item, lambda path: (False, {}))
        self.assertTrue(result)
        self.assertEqual(item["cloud_dir"], "/影视库/电影/奥德赛 (2024)")


class TestPostprocessOrganizeOffWiring(unittest.TestCase):
    """Wiring: postprocess keeps files in staging when organize is off."""

    def test_postprocess_stays_in_staging_branch(self):
        self.assertIn(
            "if not already_moved and not self._organize_after_transfer:",
            POSTPROCESS_SOURCE,
        )
        self.assertIn('item["cloud_dir"] = staging_dir', POSTPROCESS_SOURCE)

    def test_postprocess_moves_only_inside_organize_block(self):
        # rename/move happen inside "if not already_moved:" which is skipped
        # when the organize-off branch set already_moved = True.
        self.assertIn("if not already_moved:", POSTPROCESS_SOURCE)
        self.assertIn("self._cloud_mutations.move_file(", POSTPROCESS_SOURCE)

    def test_strm_and_subtitle_organize_gates(self):
        self.assertIn("and self._organize_after_transfer", POSTPROCESS_SOURCE)
        self.assertIn("or not self._organize_after_transfer", POSTPROCESS_SOURCE)


class TestPathSourceUnification(unittest.TestCase):
    """F3 acceptance: postprocess/scan path resolution shares F1 path source."""

    def test_postprocess_target_uses_platform_root(self):
        self.assertIn(
            "_platform_target(\n                self._CLOUD_MEDIA_ROOT,",
            POSTPROCESS_SOURCE,
        )

    def test_movie_scan_uses_platform_root(self):
        self.assertIn(
            "cloud_dir, expected_name = self._platform_target(\n"
            "                self._CLOUD_MEDIA_ROOT,",
            SERVICE_SOURCE,
        )

    def test_season_dir_resolution_goes_through_rename_path(self):
        self.assertIn("self._platform_rename_path(", SERVICE_SOURCE)

    def test_rename_path_routes_through_select_media_root(self):
        self.assertIn("media_root = self._select_media_root(", SERVICE_SOURCE)
        self.assertIn("_platform_classified_root(\n            media_root,", SERVICE_SOURCE)

    def test_subtitles_organize_off_branch(self):
        self.assertIn("if not self._organize_after_transfer:", SUBTITLES_SOURCE)
        self.assertIn('item["cloud_dir"] = final_dir', SUBTITLES_SOURCE)


if __name__ == "__main__":
    unittest.main(verbosity=2)
