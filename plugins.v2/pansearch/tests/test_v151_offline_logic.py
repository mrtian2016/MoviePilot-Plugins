# -*- coding: utf-8 -*-
"""PanSearch v1.5.1 离线任务逻辑测试。

被测模块依赖 app.*，无法直接导入；沿用 test_wk1_logic.py 的 ast
函数抽取方式，把目标方法源码 exec 到桩宿主上验证纯逻辑。
"""

import ast
import importlib.util
import sys
import time
import types
import typing
import unittest
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent


def load_module(name, rel_path):
    spec = importlib.util.spec_from_file_location(name, PLUGIN_ROOT / rel_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def extract_methods(rel_path, names):
    source = (PLUGIN_ROOT / rel_path).read_text(encoding="utf-8")
    tree = ast.parse(source)
    found = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for child in node.body:
                if isinstance(child, ast.FunctionDef) and child.name in names:
                    found[child.name] = ast.get_source_segment(source, child)
    missing = set(names) - set(found)
    assert not missing, "missing methods: %s" % missing
    return found


def extract_functions(rel_path, names):
    source = (PLUGIN_ROOT / rel_path).read_text(encoding="utf-8")
    tree = ast.parse(source)
    found = {
        node.name: ast.get_source_segment(source, node)
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in names
    }
    missing = set(names) - set(found)
    assert not missing, "missing functions: %s" % missing
    return found


class _StubLogger:
    def debug(self, *args, **kwargs):
        pass

    info = warning = error = debug


def _bind(stub, namespace, name):
    stub.__dict__[name] = types.MethodType(namespace[name], stub)


class _TTLStub(dict):
    """create_platform_ttl_cache 的替身。"""


class TestBlacklistGranularity(unittest.TestCase):
    """T2.2: _add_offline_blacklist 拒绝 subscribe:*/media:* 兜底键。"""

    @classmethod
    def setUpClass(cls):
        found = extract_methods(
            "handlers/sync/service.py",
            {"_add_offline_blacklist", "_offline_hash"},
        )
        cls._source = found

    def _make_handler(self):
        class Stub:
            pass

        stub = Stub()

        class OfflineStub:
            @staticmethod
            def parse_magnet_link(url, fetch_metadata=False):
                if "btih:" not in url:
                    return None
                return {
                    "url": url,
                    "name": "demo",
                    "size": 1,
                    "hash": "0123456789ABCDEF0123456789ABCDEF01234567",
                }

        stub._offline_download = OfflineStub()
        namespace = {
            "logger": _StubLogger(),
            "time": time,
            "re": __import__("re"),
            "Any": typing.Any,
            "Dict": typing.Dict,
            "Optional": typing.Optional,
            "Tuple": typing.Tuple,
            "List": typing.List,
        }
        for name, source in self._source.items():
            exec(source, namespace)
            _bind(stub, namespace, name)
        stub._offline_blacklist = _TTLStub()
        return stub

    def test_rejects_subscribe_fallback_key(self):
        handler = self._make_handler()
        handler._add_offline_blacklist("subscribe:910", "超时")
        self.assertEqual(handler._offline_blacklist, {})

    def test_rejects_media_fallback_key(self):
        handler = self._make_handler()
        handler._add_offline_blacklist("media:movie:123", "超时")
        self.assertEqual(handler._offline_blacklist, {})

    def test_keeps_real_share_url_and_hash(self):
        handler = self._make_handler()
        handler._add_offline_blacklist(
            "https://115.com/s/abc123", "提交离线下载失败"
        )
        self.assertIn("https://115.com/s/abc123", handler._offline_blacklist)

    def test_keeps_magnet_info_hash(self):
        handler = self._make_handler()
        magnet = (
            "magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567&dn=x"
        )
        handler._add_offline_blacklist(magnet, "超时")
        self.assertIn(
            "0123456789ABCDEF0123456789ABCDEF01234567",
            handler._offline_blacklist,
        )
        self.assertIn(magnet, handler._offline_blacklist)

    def test_empty_key_is_noop(self):
        handler = self._make_handler()
        handler._add_offline_blacklist("", "超时")
        self.assertEqual(handler._offline_blacklist, {})


class TestAddOfflineDownloadReturnType(unittest.TestCase):
    """T1.1: add_offline_download 返回 info_hash 字符串。"""

    @classmethod
    def setUpClass(cls):
        found = extract_methods(
            "drive/p115/offline.py",
            {"add_offline_download"},
        )
        cls._source = found

    def _make_service(self, batch_result):
        class Stub:
            pass

        stub = Stub()
        stub._batch_result = batch_result

        def batch(items, save_path, batch_size=20, batch_interval=3.0):
            return stub._batch_result

        def parse_magnet_link(url, fetch_metadata=False):
            return {
                "url": url,
                "name": "demo",
                "size": 1,
                "hash": "ABCDEF0123456789ABCDEF0123456789ABCDEF01",
            }

        def is_ed2k_url(url):
            return False

        stub.add_offline_downloads_batch = batch
        stub.parse_magnet_link = parse_magnet_link
        stub.is_ed2k_url = is_ed2k_url
        namespace = {"logger": _StubLogger()}
        exec(self._source["add_offline_download"], namespace)
        _bind(stub, namespace, "add_offline_download")
        return stub

    def test_source_declares_str_return(self):
        source = (
            PLUGIN_ROOT / "drive" / "p115" / "offline.py"
        ).read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if (
                    isinstance(node, ast.FunctionDef)
                    and node.name == "add_offline_download"
            ):
                self.assertIsInstance(node.returns, ast.Name)
                self.assertEqual(node.returns.id, "str")
                return
        self.fail("add_offline_download 未找到")

    def test_returns_hash_string_on_success(self):
        stub = self._make_service(
            (["ABCDEF0123456789ABCDEF0123456789ABCDEF01"], [])
        )
        handle = stub.add_offline_download("magnet:?xt=urn:btih:x", "/save")
        self.assertIsInstance(handle, str)
        self.assertTrue(handle)
        self.assertEqual(handle, "ABCDEF0123456789ABCDEF0123456789ABCDEF01")

    def test_returns_empty_string_on_failure(self):
        stub = self._make_service(([], []))
        handle = stub.add_offline_download("magnet:?xt=urn:btih:x", "/save")
        self.assertIsInstance(handle, str)
        self.assertEqual(handle, "")


class _FakeFile:
    def __init__(self, name, sha1=""):
        self.name = name
        self.sha1 = sha1


def _snapshot_stub(results):
    calls = []

    def snapshot(cloud_dir):
        calls.append(cloud_dir)
        return results.get(cloud_dir, (True, {}))

    snapshot.calls = calls
    return snapshot


class TestOfflineTimeoutFileVerdict(unittest.TestCase):
    """T2.1: 超时路径优先文件已存在终审。"""

    @classmethod
    def setUpClass(cls):
        found = extract_methods(
            "handlers/sync/postprocess.py",
            {"_offline_timeout_file_verdict"},
        )
        cls._source = found

    def _make_handler(self):
        class Stub:
            pass

        stub = Stub()
        namespace = {
            "logger": _StubLogger(),
            "time": time,
            "Any": typing.Any,
            "Dict": typing.Dict,
            "Optional": typing.Optional,
            "Tuple": typing.Tuple,
            "List": typing.List,
        }
        exec(self._source["_offline_timeout_file_verdict"], namespace)
        _bind(stub, namespace, "_offline_timeout_file_verdict")
        return stub

    def test_ready_when_staging_file_matches_name(self):
        handler = self._make_handler()
        snapshot = _snapshot_stub({
            "/staging": (True, {"movie.mkv": _FakeFile("movie.mkv")}),
        })
        item = {
            "staging_dir": "/staging",
            "staging_name": "movie.mkv",
            "file_name": "movie.mkv",
            "cloud_dir": "/final",
            "source_sha1": "",
        }
        self.assertEqual(
            handler._offline_timeout_file_verdict(item, time.time(), snapshot),
            "ready",
        )

    def test_ready_when_sha1_matches(self):
        handler = self._make_handler()
        snapshot = _snapshot_stub({
            "/final": (True, {
                "other.mkv": _FakeFile("other.mkv", "AA" * 20),
            }),
        })
        item = {
            "staging_dir": "/staging",
            "staging_name": "movie.mkv",
            "cloud_dir": "/final",
            "source_sha1": "AA" * 20,
        }
        self.assertEqual(
            handler._offline_timeout_file_verdict(item, time.time(), snapshot),
            "ready",
        )

    def test_defer_when_directory_listing_fails(self):
        handler = self._make_handler()
        snapshot = _snapshot_stub({
            "/staging": (False, {}),
        })
        item = {
            "staging_dir": "/staging",
            "staging_name": "movie.mkv",
            "cloud_dir": "/staging",
            "source_sha1": "",
        }
        self.assertEqual(
            handler._offline_timeout_file_verdict(item, time.time(), snapshot),
            "defer",
        )

    def test_missing_when_file_not_found(self):
        handler = self._make_handler()
        snapshot = _snapshot_stub({
            "/staging": (True, {}),
            "/final": (True, {"unrelated.mkv": _FakeFile("unrelated.mkv")}),
        })
        item = {
            "staging_dir": "/staging",
            "staging_name": "movie.mkv",
            "cloud_dir": "/final",
            "source_sha1": "BB" * 20,
        }
        self.assertEqual(
            handler._offline_timeout_file_verdict(item, time.time(), snapshot),
            "missing",
        )

    def test_magnet_branch_timeout_uses_verdict(self):
        # 抽样校验：超时失败分支必须先走终审且 "ready" 时不失败。
        source = (
            PLUGIN_ROOT / "handlers" / "sync" / "postprocess.py"
        ).read_text(encoding="utf-8")
        self.assertIn("self._offline_timeout_file_verdict(", source)
        self.assertIn('if verdict == "ready":', source)
        # 旧的直接失败文案仍在，但仅在终审 "missing" 时执行。
        self.assertIn('verdict = self._offline_timeout_file_verdict(', source)


class TestPersistOfflineProgress(unittest.TestCase):
    """T1.4: 进度快照写入 pending 项且同步历史状态。"""

    @classmethod
    def setUpClass(cls):
        found = extract_methods(
            "handlers/sync/postprocess.py",
            {"_persist_offline_progress", "_sync_pending_history_status"},
        )
        cls._source = found

    def _make_handler(self, history=None):
        class Stub:
            pass

        stub = Stub()
        state = {"history": history or []}

        def get_data(key):
            return state.get(key)

        def save_data(key, value):
            state[key] = value

        namespace = {
            "logger": _StubLogger(),
            "time": time,
            "Any": typing.Any,
            "Dict": typing.Dict,
            "Optional": typing.Optional,
            "Tuple": typing.Tuple,
            "List": typing.List,
        }
        for name, source in self._source.items():
            exec(source, namespace)
            _bind(stub, namespace, name)
        stub._get_data = get_data
        stub._save_data = save_data
        stub._offline_pending_lock = __import__("threading").Lock()
        return stub

    def test_snapshot_written_and_history_status_filled(self):
        history = [{"finalize_key": "magnet:ABC:1", "status": ""}]
        handler = self._make_handler(history)
        item = {"pending_key": "magnet:ABC:1"}
        task = {
            "status_text": "下载中",
            "percent": 42.0,
            "state": "running",
        }
        handler._persist_offline_progress(item, task)
        snapshot = item.get("offline_progress")
        self.assertEqual(snapshot["status_text"], "下载中")
        self.assertEqual(snapshot["percent"], 42.0)
        self.assertEqual(history[0]["status"], "下载中 42%")

    def test_terminal_history_status_not_overwritten(self):
        history = [{"finalize_key": "magnet:ABC:1", "status": "成功"}]
        handler = self._make_handler(history)
        item = {"pending_key": "magnet:ABC:1"}
        handler._persist_offline_progress(
            item, {"status_text": "下载中", "percent": 50.0, "state": "running"}
        )
        self.assertEqual(history[0]["status"], "成功")

    def test_no_task_leaves_item_untouched(self):
        handler = self._make_handler([])
        item = {"pending_key": "k"}
        handler._persist_offline_progress(item, {})
        # 空任务文本兜底为“处理中”，快照仍写入。
        self.assertEqual(item["offline_progress"]["status_text"], "处理中")


class TestPendingRecordGuardrails(unittest.TestCase):
    """T1.2/T1.3: 句柄登记与 no_handle 标记的存在性检查。"""

    def test_service_captures_submit_handle(self):
        source = (
            PLUGIN_ROOT / "handlers" / "sync" / "service.py"
        ).read_text(encoding="utf-8")
        self.assertIn(
            "submit_handle = self._offline_download.add_offline_download(",
            source,
        )
        self.assertIn('if not submit_handle:', source)
        self.assertIn('"task_id": real_task_id,', source)
        self.assertIn('"no_handle": no_handle,', source)
        self.assertIn(
            "self._offline_download.get_offline_tasks(force=True)", source
        )

    def test_postprocess_persists_progress_snapshot(self):
        source = (
            PLUGIN_ROOT / "handlers" / "sync" / "postprocess.py"
        ).read_text(encoding="utf-8")
        self.assertIn("self._persist_offline_progress(item, task)", source)


class TestRetryTransientCall(unittest.TestCase):
    """T3.1/T3.2: retry_transient_call 有界重试后抛出原始异常。"""

    @classmethod
    def setUpClass(cls):
        found = extract_functions(
            "drive/common.py",
            {"retry_transient_call", "is_transient_drive_error",
             "error_http_status"},
        )
        namespace = {
            "logger": _StubLogger(),
            "time": __import__("time"),
            "Any": typing.Any,
            "Callable": typing.Callable,
            "Optional": typing.Optional,
            "Sequence": typing.Sequence,
        }
        for name, source in found.items():
            exec(source, namespace)
        cls._retry = staticmethod(namespace["retry_transient_call"])
        cls._is_transient = staticmethod(namespace["is_transient_drive_error"])
        cls._http_status = staticmethod(namespace["error_http_status"])

    def test_success_on_first_attempt(self):
        calls = []

        def func():
            calls.append(1)
            return "ok"

        self.assertEqual(
            self._retry(func), "ok"
        )
        self.assertEqual(len(calls), 1)

    def test_gives_up_after_attempts_and_reraises(self):
        calls = []
        error = RuntimeError("server error")

        def func():
            calls.append(1)
            raise error

        with self.assertRaises(RuntimeError) as ctx:
            self._retry(
                func, attempts=3, delays=(0, 0)
            )
        self.assertIs(ctx.exception, error)
        self.assertEqual(len(calls), 3)

    def test_success_on_later_attempt(self):
        state = {"calls": 0}

        def func():
            state["calls"] += 1
            if state["calls"] < 3:
                raise ConnectionError("reset")
            return "ok"

        self.assertEqual(
            self._retry(
                func, attempts=3, delays=(0, 0)
            ),
            "ok",
        )
        self.assertEqual(state["calls"], 3)

    def test_non_transient_error_not_retried(self):
        calls = []

        class FakeHTTPError(Exception):
            status_code = 405

        def func():
            calls.append(1)
            raise FakeHTTPError("method not allowed")

        with self.assertRaises(FakeHTTPError):
            self._retry(func, attempts=3, delays=(0, 0))
        self.assertEqual(len(calls), 1)

    def test_transient_status_classification(self):
        is_transient = self._is_transient
        http_status = self._http_status

        class FakeHTTPError(Exception):
            def __init__(self, status):
                self.status_code = status

        self.assertTrue(is_transient(FakeHTTPError(502)))
        self.assertTrue(is_transient(FakeHTTPError(429)))
        self.assertFalse(is_transient(FakeHTTPError(405)))
        self.assertFalse(is_transient(FakeHTTPError(403)))
        self.assertTrue(is_transient(TimeoutError("network")))
        self.assertEqual(http_status(FakeHTTPError(502)), 502)
        self.assertIsNone(http_status(TimeoutError("network")))

    def test_delays_bounded_under_15s(self):
        # 静态检查：离线任务列表重试的退避延迟总和必须小于 15 秒。
        source = (
            PLUGIN_ROOT / "drive" / "p115" / "offline.py"
        ).read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if (
                    isinstance(node, ast.Call)
                    and getattr(node.func, "id", "") == "retry_transient_call"
            ):
                delay_arg = next(
                    (
                        kw.value for kw in node.keywords
                        if kw.arg == "delays"
                    ),
                    None,
                )
                if delay_arg is not None and isinstance(
                    delay_arg, (ast.List, ast.Tuple)
                ):
                    total = sum(
                        element.value
                        for element in delay_arg.elts
                        if isinstance(element, ast.Constant)
                    )
                    self.assertLess(total, 15)


class TestOfflineTimeoutShouldDefer(unittest.TestCase):
    """T3.3: 快照不可用时超时判定暂缓，而不是失败。"""

    @classmethod
    def setUpClass(cls):
        found = extract_methods(
            "handlers/sync/postprocess.py",
            {"_offline_timeout_should_defer"},
        )
        cls._source = found

    def _make_handler(self):
        class Stub:
            pass

        stub = Stub()
        namespace = {"Optional": typing.Optional}
        exec(self._source["_offline_timeout_should_defer"], namespace)
        Stub._offline_timeout_should_defer = staticmethod(
            namespace["_offline_timeout_should_defer"]
        )
        return stub

    def test_defer_when_snapshot_invalid_and_missing(self):
        handler = self._make_handler()
        self.assertTrue(
            handler._offline_timeout_should_defer(False, "missing")
        )

    def test_defer_when_snapshot_invalid_and_no_verdict(self):
        handler = self._make_handler()
        self.assertTrue(handler._offline_timeout_should_defer(False, None))

    def test_ready_wins_even_when_snapshot_invalid(self):
        handler = self._make_handler()
        self.assertFalse(
            handler._offline_timeout_should_defer(False, "ready")
        )

    def test_no_defer_when_snapshot_valid(self):
        handler = self._make_handler()
        self.assertFalse(
            handler._offline_timeout_should_defer(True, "missing")
        )

    def test_postprocess_wires_defer_in_all_timeout_branches(self):
        source = (
            PLUGIN_ROOT / "handlers" / "sync" / "postprocess.py"
        ).read_text(encoding="utf-8")
        self.assertEqual(
            source.count("self._offline_timeout_should_defer("), 3
        )
        self.assertEqual(source.count("接口异常，暂缓判定"), 3)
        # 暂缓分支必须复用既有重试节奏，避免热循环。
        self.assertIn("_schedule_finalize_retry", source)


class TestIterDirectory405Fallback(unittest.TestCase):
    """T3.2: 持续 405 时切换 ios 渠道重放目录列举。"""

    def test_web_then_ios_channel_ordering(self):
        source = (
            PLUGIN_ROOT / "drive" / "p115" / "files.py"
        ).read_text(encoding="utf-8")
        tree = ast.parse(source)
        method = None
        for node in ast.walk(tree):
            if (
                    isinstance(node, ast.FunctionDef)
                    and node.name == "_iter_directory"
            ):
                method = node
                break
        self.assertIsNotNone(method, "_iter_directory 未找到")
        source_segment = ast.get_source_segment(source, method)
        # web 渠道先重试，持续 405 后才切换 ios 渠道。
        self.assertIn('app="web"', source_segment)
        self.assertIn('app="ios"', source_segment)
        self.assertLess(
            source_segment.index('app="web"'),
            source_segment.index('app="ios"'),
        )
        self.assertIn("retry_transient_call", source_segment)
        self.assertIn("405", source_segment)

    def test_offline_task_fetch_keeps_rate_limiter_discipline(self):
        source = (
            PLUGIN_ROOT / "drive" / "p115" / "offline.py"
        ).read_text(encoding="utf-8")
        # 每次重试都仍经由 rate_limiter，且不叠加其内部重试。
        self.assertIn("retry_transient_call(", source)
        self.assertIn("max_retries=0", source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
