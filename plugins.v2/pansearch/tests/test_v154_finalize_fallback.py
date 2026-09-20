# -*- coding: utf-8 -*-
"""PanSearch v1.5.4 T1 终审定位失败不得直接判死：全盘兜底 + 窗口同源。

被测模块依赖 app.*，无法直接导入；沿用 tests/ 的 ast 函数抽取方式，
把目标方法源码 exec 到桩宿主上验证纯逻辑。覆盖：
1) _FILE_FINALIZE_TIMEOUT 不再是硬编码 30*60，且与离线超时配置同源；
2) 终审 reason 文案动态输出实际分钟数，不再有"30分钟"硬编码；
3) 全盘兜底判定三分支：全盘命中->located（原位/移动/actual_path）、
   未命中且零进展轮数未达上限->retry、连续 3 轮零进展->fail；
4) _full_pan_locate_file：sha1 优先、文件名次之、无递归能力降级 None、
   同名但 sha1 不同不误认。
"""

import ast
import types
import unittest
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent


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
                            or (isinstance(d, ast.Attribute)
                                and d.attr == "staticmethod")
                            for d in child.decorator_list):
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


POSTPROCESS_METHODS = [
    "_finalize_timeout_minutes",
    "_finalize_locate_fail_reason",
    "_finalize_strm_fail_reason",
    "_finalize_full_pan_verdict",
]

SERVICE_METHODS = [
    "_cloud_entry_name",
    "_cloud_entry_sha1",
    "_cloud_entry_is_directory",
    "_cloud_entry_dir",
    "_full_pan_search_roots",
    "_full_pan_locate_file",
]


class TestFinalizeWindowConfig(unittest.TestCase):
    """T1.1 终审窗口与离线超时同源，消灭独立 30 分钟硬编码。"""

    def _service(self):
        return (PLUGIN_ROOT / "handlers/sync/service.py").read_text(
            encoding="utf-8")

    def test_no_hardcoded_30min_finalize_timeout(self):
        self.assertNotIn("_FILE_FINALIZE_TIMEOUT = 30 * 60", self._service())
        self.assertIn("_FILE_FINALIZE_TIMEOUT = 120 * 60", self._service())

    def test_finalize_window_follows_config(self):
        src = self._service()
        self.assertIn(
            "self._FILE_FINALIZE_TIMEOUT = timeout_minutes * 60", src
        )

    def test_no_hardcoded_30min_reason_strings(self):
        src = (PLUGIN_ROOT / "handlers/sync/postprocess.py").read_text(
            encoding="utf-8")
        self.assertNotIn("网盘文件已保存但30分钟", src)
        self.assertNotIn("文件已下载但30分钟", src)


class TestFinalizeReasonText(unittest.TestCase):
    """T1.2 reason 文案动态分钟数。"""

    def setUp(self):
        ns, statics = extract_methods("handlers/sync/postprocess.py",
                                      POSTPROCESS_METHODS)
        exec("from typing import Any, Dict, Optional, Tuple\n"
             "from collections.abc import Mapping", ns)
        ns["logger"] = _StubLogger()

        class Stub:
            _OFFLINE_ZERO_GROWTH_ROUNDS = 3

        self.stub = Stub()
        self.stub._FILE_FINALIZE_TIMEOUT = 120 * 60
        _bind(self.stub, ns, *POSTPROCESS_METHODS, statics=statics)

    def test_minutes_dynamic(self):
        self.assertEqual(self.stub._finalize_timeout_minutes(), 120)
        self.stub._FILE_FINALIZE_TIMEOUT = 45 * 60
        self.assertIn("45 分钟", self.stub._finalize_locate_fail_reason())
        self.assertIn("45 分钟", self.stub._finalize_strm_fail_reason())

    def test_rounds_suffix(self):
        reason = self.stub._finalize_locate_fail_reason(rounds=3)
        self.assertIn("连续 3 轮全盘检索均未找到", reason)

    def test_bad_window_falls_back(self):
        self.stub._FILE_FINALIZE_TIMEOUT = None
        self.assertEqual(self.stub._finalize_timeout_minutes(), 120)


class _FakeEntry:
    def __init__(self, name, sha1="", is_directory=False, cloud_dir=None):
        self.name = name
        self.sha1 = sha1
        self.is_directory = is_directory
        self.native = {"_cloud_dir": cloud_dir} if cloud_dir else {}


class TestFullPanLocate(unittest.TestCase):
    """T1.3 全盘检索：sha1 优先、文件名次之、无能力降级。"""

    def setUp(self):
        ns, statics = extract_methods("handlers/sync/service.py",
                                      SERVICE_METHODS)
        exec("from typing import Any, Dict, List, Optional, Tuple\n"
             "from collections.abc import Mapping", ns)
        ns["logger"] = _StubLogger()

        class Stub:
            _FULL_PAN_SEARCH_DEPTH = 6

        self.stub = Stub()
        self.stub._cloud_query = None
        _bind(self.stub, ns, *SERVICE_METHODS, statics=statics)

    def test_no_recursive_capability(self):
        self.assertIsNone(
            self.stub._full_pan_locate_file({"source_sha1": "AB"}, "x.mkv"))

    def test_sha1_hit_wins_over_name(self):
        calls = []

        class Query:
            def list_files_recursive(self, path, max_depth=None):
                calls.append(path)
                return [
                    _FakeEntry("wrong.mkv", "FF" * 20, cloud_dir=path),
                    _FakeEntry("right.mkv", "AB" * 20, cloud_dir=path),
                ]

        self.stub._cloud_query = Query()
        hit = self.stub._full_pan_locate_file(
            {"source_sha1": "AB" * 20, "staging_dir": "/影视库/剧",
             "cloud_dir": "/影视库/剧"},
            file_name="right.mkv")
        self.assertIsNotNone(hit)
        self.assertEqual(hit[0].name, "right.mkv")
        self.assertEqual(hit[1], "/影视库/剧")

    def test_same_name_different_sha1_not_mistaken(self):
        class Query:
            def list_files_recursive(self, path, max_depth=None):
                return [_FakeEntry("movie.mkv", "CD" * 20, cloud_dir=path)]

        self.stub._cloud_query = Query()
        hit = self.stub._full_pan_locate_file(
            {"source_sha1": "AB" * 20, "file_name": "movie.mkv"})
        self.assertIsNone(hit)

    def test_name_fallback_when_no_sha1(self):
        class Query:
            def list_files_recursive(self, path, max_depth=None):
                return [_FakeEntry("movie.mkv", "", cloud_dir=path)]

        self.stub._cloud_query = Query()
        hit = self.stub._full_pan_locate_file(
            {"file_name": "movie.mkv", "staging_dir": "/x", "cloud_dir": "/x"},
            "movie.mkv")
        self.assertIsNotNone(hit)
        self.assertEqual(hit[1], "/x")

    def test_roots_dedup_nested(self):
        roots = self.stub._full_pan_search_roots({
            "staging_dir": "/转存",
            "cloud_dir": "/转存/子目录",
        })
        self.assertIn("/转存", roots)
        self.assertNotIn("/转存/子目录", roots)


class TestFinalizeFullPanVerdict(unittest.TestCase):
    """T1.4 判死前兜底三分支。"""

    def _make(self, locate_result, mutations=False):
        pp = extract_methods("handlers/sync/postprocess.py",
                             POSTPROCESS_METHODS)
        pp, statics4 = pp
        exec("from typing import Any, Dict, Optional, Tuple\n"
             "from collections.abc import Mapping", pp)
        pp["logger"] = _StubLogger()

        class Stub:
            _OFFLINE_ZERO_GROWTH_ROUNDS = 3

        stub = Stub()
        stub._FILE_FINALIZE_TIMEOUT = 120 * 60
        stub._cloud_mutations = object() if mutations else None

        stub._full_pan_locate_file = lambda item, file_name="": locate_result
        stub._cloud_entry_name = lambda e: str(getattr(e, "name", "") or "")
        _bind(stub, pp, *POSTPROCESS_METHODS, statics=statics4)
        return stub, pp["logger"]

    def test_located_in_place(self):
        entry = _FakeEntry("ep20.mkv")
        stub, log = self._make((entry, "/影视库/剧/Season 1"))
        item = {"cloud_dir": "/影视库/剧/Season 1"}
        verdict, payload = stub._finalize_full_pan_verdict(
            item, "ep20.mkv", 1000.0)
        self.assertEqual(verdict, "located")
        self.assertTrue(payload[2])  # already_moved
        self.assertEqual(item.get("moved_at"), 1000.0)

    def test_located_elsewhere_moves(self):
        entry = _FakeEntry("ep20.mkv")
        stub, _ = self._make((entry, "/待整理"), mutations=True)
        item = {"cloud_dir": "/影视库/剧"}
        verdict, payload = stub._finalize_full_pan_verdict(
            item, "ep20.mkv", 1000.0)
        self.assertEqual(verdict, "located")
        self.assertFalse(payload[2])
        self.assertEqual(item["staging_dir"], "/待整理")

    def test_located_no_move_capability_records_actual_path(self):
        entry = _FakeEntry("ep20.mkv")
        stub, _ = self._make((entry, "/离线默认目录"), mutations=False)
        item = {"cloud_dir": "/影视库/剧"}
        verdict, _ = stub._finalize_full_pan_verdict(
            item, "ep20.mkv", 1000.0)
        self.assertEqual(verdict, "located")
        self.assertEqual(item.get("actual_path"), "/离线默认目录/ep20.mkv")

    def test_missing_defers_then_fails_after_rounds(self):
        stub, _ = self._make(None)
        item = {"cloud_dir": "/影视库/剧", "download_completed_at": 1.0}
        verdict, reason = stub._finalize_full_pan_verdict(item, "f.mkv", 1e9)
        self.assertEqual(verdict, "retry")
        self.assertEqual(item["finalize_zero_progress_rounds"], 1)
        for _ in range(2):
            verdict, reason = stub._finalize_full_pan_verdict(
                item, "f.mkv", 1e9)
        self.assertEqual(verdict, "fail")
        self.assertIn("120 分钟", str(reason))
        self.assertIn("连续 3 轮", str(reason))

    def test_old_payload_missing_keys(self):
        stub, _ = self._make(None)
        item = {}
        verdict, _ = stub._finalize_full_pan_verdict(item, "f.mkv", 5.0)
        self.assertEqual(verdict, "retry")  # 无 download_completed_at 不崩


class TestCallSiteWiring(unittest.TestCase):
    """T1.5 接线断言：到期分支必须先兜底再判死，STRM 失败文案动态。"""

    def test_finalize_branch_uses_verdict(self):
        src = (PLUGIN_ROOT / "handlers/sync/postprocess.py").read_text(
            encoding="utf-8")
        self.assertIn("_finalize_full_pan_verdict(item, file_name, now)", src)
        self.assertIn('locate_verdict == "retry"', src)
        self.assertIn("target_file, staging_dir, already_moved = "
                      "locate_payload", src)

    def test_strm_branch_dynamic_reason(self):
        src = (PLUGIN_ROOT / "handlers/sync/postprocess.py").read_text(
            encoding="utf-8")
        self.assertIn("reason = self._finalize_strm_fail_reason()", src)


if __name__ == "__main__":
    unittest.main()
