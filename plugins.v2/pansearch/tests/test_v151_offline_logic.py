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


class _FinalizeEntrySentinel(Exception):
    """同轮落入成功收尾入口时抛出的哨兵。"""


class _LivelockSentinel(Exception):
    """不应出现的重试/失败路径（活锁回归）哨兵。"""


class TestTimeoutReadySameRoundFinalize(unittest.TestCase):
    """T2 修复回归：超时终审 "ready" 必须同轮落入成功收尾。

    历史缺陷：ready 分支只置 task_done=True，随后仍命中外层 continue，
    每轮重新超时、重新终审 ready，永远到不了整理/收尾流程（活锁）。
    """

    @classmethod
    def setUpClass(cls):
        found = extract_methods(
            "handlers/sync/postprocess.py",
            {"monitor_offline_strm_tasks"},
        )
        cls._source = found["monitor_offline_strm_tasks"]

    def _make_handler(self, item, finalize_step, finalize_detail=None):
        class Stub:
            pass

        stub = Stub()
        # v1.5.10：入口早筛会读取该常量，桩上必须存在。
        stub._OFFLINE_ED2K_HARD_LIMIT = 24 * 60 * 60
        state = {"offline_pending_tasks": {"pending:1": item}}
        calls = {"steps": []}

        def get_data(key):
            return state.get(key)

        def save_data(key, value):
            state[key] = value

        def update_progress(
                item, step, position, total, detail="",
        ):
            calls["steps"].append((step, str(detail)))
            if (
                    step == finalize_step
                    and (finalize_detail is None or finalize_detail in str(detail))
            ):
                raise _FinalizeEntrySentinel()

        def schedule_retry(item, now):
            raise _LivelockSentinel("不应再安排重试（活锁）")

        def mark_failed(*args, **kwargs):
            raise _LivelockSentinel("不应判定失败")

        class _DirLookup:
            checked = False
            directory_id = None

        class _CloudDirectories:
            def resolve_directory(self, cloud_dir):
                return _DirLookup()

            def list_directory(self, directory_id):
                raise _LivelockSentinel("不应列举目录")

        namespace = {
            "logger": _StubLogger(),
            "time": time,
            "uuid": __import__("uuid"),
            "copy": __import__("copy"),
            "Dict": typing.Dict,
            "Any": typing.Any,
            "List": typing.List,
            "Optional": typing.Optional,
            "Set": typing.Set,
            "Tuple": typing.Tuple,
            "SessionFactory": None,
            "Subscribe": None,
        }
        exec(self._source, namespace)
        _bind(stub, namespace, "monitor_offline_strm_tasks")
        stub._get_data = get_data
        stub._save_data = save_data
        stub._offline_pending_lock = __import__("threading").Lock()
        stub._OFFLINE_PENDING_KEY = "offline_pending_tasks"
        stub._OFFLINE_MONITOR_LEASE_SECONDS = 300
        stub._OFFLINE_TIMEOUT = 1800
        stub._cloud_directories = _CloudDirectories()
        stub._cloud_query = object()
        stub._cloud_mutations = object()
        stub._cloud_batch_mutations = None
        stub._offline_tasks = None
        stub._due_pending_keys = (
            lambda pending, now, force=False, pending_keys=None: ["pending:1"]
        )
        stub._save_offline_pending = lambda pending: None
        stub._update_postprocess_progress = update_progress
        stub._offline_timeout_file_verdict = (
            lambda item, now, snapshot, subscribe_cache=None: "ready"
        )
        stub._offline_timeout_should_defer = lambda tasks_valid, verdict: False
        def finalize_magnet_package(item, pending_key, subscribe_cache=None):
            raise _FinalizeEntrySentinel()

        stub._schedule_finalize_retry = schedule_retry
        stub._finalize_magnet_package = finalize_magnet_package
        stub._mark_offline_history_status = mark_failed
        stub._add_offline_blacklist = mark_failed
        stub._cleanup_failed_offline_task = lambda item, reason: None
        stub._notify_finalize_dead = lambda *args, **kwargs: None
        stub._FINALIZE_DEAD_REASON = "后处理连续失败 {} 次"
        stub._organize_after_transfer = False
        stub.calls = calls
        return stub

    def _base_item(self, task_type, task_id):
        return {
            "task_type": task_type,
            "task_id": task_id,
            "file_name": "Show.2020.S01E01.mkv",
            "created_at": time.time() - 4000,
            "subscribe_id": 0,
            "staging_dir": "/pansearch/staging",
            "staging_name": "Show.2020.S01E01.mkv",
            "cloud_dir": "/media",
            "share_url": "",
        }

    def _run(self, handler, tasks):
        return handler.monitor_offline_strm_tasks(
            offline_tasks=tasks, offline_tasks_valid=True,
        )

    def test_magnet_ready_falls_through_to_organize(self):
        item = self._base_item("magnet", "ABC123")
        handler = self._make_handler(item, finalize_step="organize")
        # ready 终审后必须同轮进入 "整理 Magnet 下载文件" 收尾入口。
        with self.assertRaises(_FinalizeEntrySentinel):
            self._run(handler, tasks=[])
        self.assertIn(
            ("organize", "整理 Magnet 下载文件"), handler.calls["steps"]
        )

    def test_ed2k_task_ready_skips_retry_and_finalizes(self):
        item = self._base_item("ed2k", "XYZ789")
        handler = self._make_handler(item, finalize_step="locate",
                                     finalize_detail="定位")
        # 任务存在但未完成、已超时、终审 ready：不得走重试，须落入共享收尾。
        with self.assertRaises(_FinalizeEntrySentinel):
            self._run(handler, tasks=[{"id": "XYZ789"}])

    def test_ed2k_fallback_ready_falls_through_to_finalize(self):
        item = self._base_item("ed2k", "XYZ789")
        handler = self._make_handler(item, finalize_step="locate",
                                     finalize_detail="定位")
        # 任务句柄缺失（tasks_valid=True 且目录列举失败不判失败）、超时、
        # 终审 ready：同样必须同轮落入共享收尾。
        with self.assertRaises(_FinalizeEntrySentinel):
            self._run(handler, tasks=[])


class _DirLookup:
    def __init__(self, checked=True, directory_id=1):
        self.checked = checked
        self.directory_id = directory_id


class _DirListing:
    def __init__(self, checked=True, files=()):
        self.checked = checked
        self.files = list(files)


class _CloudDirectoriesStub:
    """resolve_directory + list_directory 的可编程替身。"""

    def __init__(self, directories):
        # directories: {dir: (checked, [files])}
        self._directories = directories
        self.list_calls = []

    def resolve_directory(self, cloud_dir):
        if cloud_dir not in self._directories:
            return _DirLookup(checked=True, directory_id=None)
        return _DirLookup(checked=True, directory_id=cloud_dir)

    def list_directory(self, directory_id):
        self.list_calls.append(directory_id)
        checked, files = self._directories.get(directory_id, (True, []))
        return _DirListing(checked=checked, files=files)


class TestOfflineHistoryBackfill(unittest.TestCase):
    """T4-A: 存量核对自动回填（失败/空/处理中记录反查回填）。"""

    @classmethod
    def setUpClass(cls):
        found = extract_methods(
            "handlers/sync/history.py",
            {
                "reconcile_offline_history_backfill",
                "_offline_backfill_verdict",
                "_offline_backfill_record_key",
                "_offline_backfill_record_label",
                "_offline_backfill_notification_detail",
            },
        )
        cls._source = found

    def _make_handler(self, history, pending=None):
        class Stub:
            pass

        stub = Stub()
        # v1.5.10：入口早筛会读取该常量，桩上必须存在。
        stub._OFFLINE_ED2K_HARD_LIMIT = 24 * 60 * 60
        state = {
            "history": history,
            "offline_pending_tasks": pending or {},
        }
        calls = {"platform": [], "notified": [], "history_changed": 0}

        def get_data(key):
            return state.get(key)

        def save_data(key, value):
            state[key] = value

        namespace = {
            "logger": _StubLogger(),
            "time": time,
            "copy": __import__("copy"),
            "Any": typing.Any,
            "Dict": typing.Dict,
            "List": typing.List,
            "Optional": typing.Optional,
            "Set": typing.Set,
            "Tuple": typing.Tuple,
        }
        for name, source in self._source.items():
            exec(source, namespace)
            # get_source_segment 不含装饰器：静态方法（首参非 self）直接放
            # 实例字典避免被绑定为普通方法。
            value = namespace[name]
            params = list(
                __import__("inspect").signature(value).parameters
            )
            if params and params[0] == "self":
                _bind(stub, namespace, name)
            else:
                stub.__dict__[name] = value
        stub._get_data = get_data
        stub._save_data = save_data
        stub._offline_pending_lock = __import__("threading").Lock()
        stub._OFFLINE_PENDING_KEY = "offline_pending_tasks"
        stub._OFFLINE_BACKFILL_STATUSES = {"失败", "处理中"}
        stub._OFFLINE_BACKFILL_RECHECK_SECONDS = 3600
        stub._OFFLINE_BACKFILL_CACHE_MAXSIZE = 500
        stub._record_platform_transfer_histories = (
            lambda records: calls["platform"].extend(records)
        )
        stub._send_finalized_batch = (
            lambda details: calls["notified"].extend(details)
        )
        stub._history_changed = lambda: calls.__setitem__(
            "history_changed", calls["history_changed"] + 1
        )
        stub._is_upgrade_history = (
            lambda record: bool(record.get("upgrade"))
        )
        stub.calls = calls
        stub.state = state
        return stub

    def _failed_record(self, **overrides):
        record = {
            "title": "测试电影",
            "year": "2020",
            "type": "电影",
            "status": "失败",
            "share_url": "https://115.com/s/abc",
            "file_name": "movie.mkv",
            "source_file_name": "source.mkv",
            "cloud_dir": "/media/movies",
            "staging_dir": "/staging",
            "source_sha1": "",
            "failure_reason": "离线任务超时",
        }
        record.update(overrides)
        return record

    def test_ready_record_backfilled_and_notified(self):
        record = self._failed_record()
        handler = self._make_handler([record])
        handler._cloud_directories = _CloudDirectoriesStub({
            "/staging": (True, [_FakeFile("movie.mkv")]),
            "/media/movies": (True, []),
        })
        repaired = handler.reconcile_offline_history_backfill()
        self.assertEqual(repaired, 1)
        self.assertEqual(record["status"], "成功")
        self.assertNotIn("failure_reason", record)
        # 复用既有通知链路补发。
        self.assertEqual(len(handler.calls["notified"]), 1)
        detail = handler.calls["notified"][0]
        self.assertEqual(detail["type"], "电影")
        self.assertEqual(detail["title"], "测试电影")
        self.assertEqual(detail["file_name"], "movie.mkv")
        self.assertEqual(detail["notification_kind"], "transfer")
        self.assertEqual(len(handler.calls["platform"]), 1)
        self.assertEqual(handler.calls["history_changed"], 1)

    def test_tv_ready_record_detail_carries_episodes(self):
        record = self._failed_record(
            type="电视剧", season=2, episode=5, upgrade=True,
        )
        handler = self._make_handler([record])
        handler._cloud_directories = _CloudDirectoriesStub({
            "/media/movies": (True, [_FakeFile("movie.mkv", "CC" * 20)]),
            "/staging": (True, []),
        })
        record["source_sha1"] = "CC" * 20
        self.assertEqual(handler.reconcile_offline_history_backfill(), 1)
        detail = handler.calls["notified"][0]
        self.assertEqual(detail["type"], "电视剧")
        self.assertEqual(detail["season"], 2)
        self.assertEqual(detail["episodes"], [5])
        self.assertEqual(detail["notification_kind"], "upgrade")

    def test_missing_verdict_keeps_record_unchanged(self):
        record = self._failed_record()
        handler = self._make_handler([record])
        handler._cloud_directories = _CloudDirectoriesStub({
            "/staging": (True, []),
            "/media/movies": (True, [_FakeFile("other.mkv")]),
        })
        self.assertEqual(handler.reconcile_offline_history_backfill(), 0)
        self.assertEqual(record["status"], "失败")
        self.assertEqual(record["failure_reason"], "离线任务超时")
        self.assertEqual(handler.calls["notified"], [])
        self.assertEqual(handler.calls["history_changed"], 0)

    def test_transient_directory_error_skips_without_caching(self):
        record = self._failed_record()
        handler = self._make_handler([record])
        handler._cloud_directories = _CloudDirectoriesStub({
            "/staging": (False, []),
            "/media/movies": (True, []),
        })
        self.assertEqual(handler.reconcile_offline_history_backfill(), 0)
        self.assertEqual(record["status"], "失败")
        # 瞬态错误不写入缓存，下一轮可立即重试。
        self.assertEqual(
            getattr(handler, "_offline_backfill_checked_at", {}), {}
        )
        # 接口恢复后同一轮次之外的下一次调用能正常回填。
        handler._cloud_directories = _CloudDirectoriesStub({
            "/staging": (True, [_FakeFile("movie.mkv")]),
        })
        self.assertEqual(handler.reconcile_offline_history_backfill(), 1)
        self.assertEqual(record["status"], "成功")

    def test_active_pending_record_skipped(self):
        record = self._failed_record(finalize_key="magnet:ABC")
        handler = self._make_handler(
            [record], pending={"magnet:ABC": {"task_id": "ABC"}}
        )
        handler._cloud_directories = _CloudDirectoriesStub({
            "/staging": (True, [_FakeFile("movie.mkv")]),
        })
        self.assertEqual(handler.reconcile_offline_history_backfill(), 0)
        self.assertEqual(record["status"], "失败")
        self.assertEqual(handler._cloud_directories.list_calls, [])

    def test_per_round_limit_and_recheck_cache(self):
        records = [
            self._failed_record(
                share_url=f"https://115.com/s/{index}",
                file_name=f"movie{index}.mkv",
            )
            for index in range(12)
        ]
        handler = self._make_handler(records)
        handler._cloud_directories = _CloudDirectoriesStub({
            "/staging": (True, [_FakeFile(record["file_name"]) for record in records]),
            "/media/movies": (True, []),
        })
        # 每轮最多核对 10 条。
        self.assertEqual(handler.reconcile_offline_history_backfill(), 10)
        self.assertEqual(
            sum(1 for record in records if record["status"] == "成功"), 10
        )
        # 近期核对过的键命中内存缓存：不重复列目录，只补齐剩余 2 条。
        calls_before = len(handler._cloud_directories.list_calls)
        self.assertEqual(handler.reconcile_offline_history_backfill(), 2)
        self.assertEqual(
            sum(1 for record in records if record["status"] == "成功"), 12
        )
        # 缓存命中时第二轮只新列了未核对记录涉及的目录。
        self.assertLess(
            len(handler._cloud_directories.list_calls), calls_before + 4
        )

    def test_success_and_downloading_records_ignored(self):
        records = [
            self._failed_record(status="成功"),
            self._failed_record(status="下载中"),
        ]
        handler = self._make_handler(records)
        handler._cloud_directories = _CloudDirectoriesStub({
            "/staging": (True, [_FakeFile("movie.mkv")]),
        })
        self.assertEqual(handler.reconcile_offline_history_backfill(), 0)
        self.assertEqual(handler._cloud_directories.list_calls, [])

    def test_sync_round_wires_backfill(self):
        source = (
            PLUGIN_ROOT / "core" / "services" / "sync.py"
        ).read_text(encoding="utf-8")
        self.assertIn("reconcile_offline_history_backfill()", source)


class TestDeterministicDeadLink(unittest.TestCase):
    """T4-B: 确定性死链（过期/4100018）单资源防重复转存。"""

    @classmethod
    def setUpClass(cls):
        found = extract_methods(
            "drive/p115/share.py",
            {
                "is_deterministic_dead_link_error",
                "is_exists_error",
                "_note_dead_link_failure",
                "consume_dead_link_failure",
                "_do_transfer",
            },
        )
        cls._share_source = found
        found = extract_methods(
            "handlers/sync/service.py",
            {
                "_blacklist_dead_link_share",
                "_add_offline_blacklist",
                "_offline_hash",
                "_is_offline_blacklisted",
            },
        )
        cls._service_source = found

    def _make_share_service(self, resp=None):
        class _FakeShareService:
            DEAD_LINK_ERRNO = 4100018
            EXISTS_ERRNO = 4200045

        class Stub:
            pass

        stub = Stub()
        stub.client = types.SimpleNamespace(
            share_receive=lambda payload: None
        )

        def rate_limited_call(func, payload, **kwargs):
            if resp is None:
                raise AssertionError("不应发起请求")
            return dict(resp)

        stub._rate_limited_call = rate_limited_call
        stub._ios_request_kwargs = lambda app=False: {}
        namespace = {
            "logger": _StubLogger(),
            "time": time,
            "Any": typing.Any,
            "Dict": typing.Dict,
            "Optional": typing.Optional,
            "DRIVE_RETRY_EXCEPTIONS": (ConnectionError,),
            "ShareService": _FakeShareService,
        }
        for name, source in self._share_source.items():
            exec(source, namespace)
            value = namespace[name]
            params = list(
                __import__("inspect").signature(value).parameters
            )
            if params and params[0] == "self":
                _bind(stub, namespace, name)
            else:
                stub.__dict__[name] = value
        return stub

    def _make_sync_handler(self):
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
        for name, source in self._service_source.items():
            exec(source, namespace)
            _bind(stub, namespace, name)
        stub._offline_blacklist = _TTLStub()
        stub._resource_log_reference = lambda url: str(url or "")
        return stub

    def test_dead_link_error_classification(self):
        classify = self._make_share_service().is_deterministic_dead_link_error
        self.assertTrue(classify("分享链接已过期", 4100018))
        self.assertTrue(classify("分享链接已过期", 0))
        self.assertTrue(classify("link expired", 0))
        self.assertFalse(classify("操作频繁", 990001))
        self.assertFalse(classify("未知错误", "abc"))

    def test_transfer_failure_with_dead_link_errno_recorded(self):
        service = self._make_share_service(resp={
            "state": False, "error": "链接已过期", "errno": 4100018,
        })
        share_url = "https://115.com/s/abc"
        result = service._do_transfer(
            share_code="code", receive_code="rcv", file_id="0",
            parent_id=123, save_path="/save",
            max_retries=0, share_url=share_url,
        )
        self.assertFalse(result)
        failure = service.consume_dead_link_failure(share_url)
        self.assertIsNotNone(failure)
        self.assertEqual(failure["error_code"], 4100018)
        # 取走后标记清空，不会重复消费。
        self.assertIsNone(service.consume_dead_link_failure(share_url))

    def test_non_dead_link_failure_not_recorded(self):
        service = self._make_share_service(resp={
            "state": False, "error": "操作过于频繁", "errno": 990001,
        })
        share_url = "https://115.com/s/abc"
        self.assertFalse(service._do_transfer(
            share_code="code", receive_code="rcv", file_id="0",
            parent_id=123, save_path="/save",
            max_retries=0, share_url=share_url,
        ))
        self.assertIsNone(service.consume_dead_link_failure(share_url))

    def test_dead_link_share_blacklisted_and_skipped(self):
        handler = self._make_sync_handler()
        share_url = "https://115.com/s/abc"
        service = self._make_share_service()
        service._note_dead_link_failure(share_url, "链接已过期", 4100018)
        handler._blacklist_dead_link_share(service, share_url)
        # 死链以单资源粒度入黑名单。
        self.assertIn(share_url, handler._offline_blacklist)
        reason = handler._offline_blacklist[share_url]["reason"]
        self.assertIn("4100018", reason)
        # 处理器选源循环用同一检查跳过黑名单资源。
        self.assertTrue(handler._is_offline_blacklisted(None, share_url))
        # 订阅级兜底键依然被拒绝。
        handler._blacklist_dead_link_share(
            _MarkerOnlyService(dead=True), "subscribe:910"
        )
        self.assertNotIn("subscribe:910", handler._offline_blacklist)

    def test_no_marker_leaves_blacklist_untouched(self):
        handler = self._make_sync_handler()
        handler._blacklist_dead_link_share(
            _MarkerOnlyService(dead=False), "https://115.com/s/abc"
        )
        self.assertEqual(handler._offline_blacklist, {})

    def test_processors_check_share_blacklist(self):
        # 三个处理器的选源循环必须无条件检查黑名单（含普通分享链接）。
        for rel_path, marker in (
                ("handlers/sync/movie.py", "分享链接命中死链黑名单"),
                ("handlers/sync/television.py", "分享链接命中死链黑名单"),
                ("handlers/sync/upgrade.py", "洗版分享链接命中死链黑名单"),
        ):
            source = (PLUGIN_ROOT / rel_path).read_text(encoding="utf-8")
            self.assertIn(
                "if self._is_offline_blacklisted(resource, share_url):",
                source,
            )
            self.assertIn(marker, source)
            # 旧的条件包裹（仅离线/磁力才检查）必须已被移除。
            self.assertNotIn(
                "if self._is_offline_url(share_url) or self._is_magnet_url(share_url):\n"
                "                        if self._is_offline_blacklisted",
                source,
            )

    def test_transfer_entry_points_note_dead_links(self):
        source = (
            PLUGIN_ROOT / "drive" / "p115" / "share.py"
        ).read_text(encoding="utf-8")
        # 三个转存入口都把 share_url 传入 _do_transfer 以便定位死链。
        self.assertEqual(source.count("share_url=share_url,"), 3)
        self.assertIn("self._note_dead_link_failure(share_url, error_msg, error_code)", source)
        # 同步层在转存失败后核对死链标记。
        service_source = (
            PLUGIN_ROOT / "handlers" / "sync" / "service.py"
        ).read_text(encoding="utf-8")
        self.assertIn("self._blacklist_dead_link_share(", service_source)


class _MarkerOnlyService:
    """只带 consume_dead_link_failure 标记的分享服务替身。"""

    def __init__(self, dead=False):
        self._failure = (
            {"error_msg": "链接已过期", "error_code": 4100018}
            if dead else None
        )

    def consume_dead_link_failure(self, share_url):
        return self._failure


class _RecordingLogger(_StubLogger):
    """记录 warning 调用，用于断言告警日志已打出。"""

    def __init__(self):
        self.warnings = []

    def warning(self, message, *args, **kwargs):
        self.warnings.append(str(message))


class TestPendingRecordNoHandle(unittest.TestCase):
    """T1 收尾：哈希型挂起记录解析不到真实哈希时落无句柄标记并告警。"""

    @classmethod
    def setUpClass(cls):
        found = extract_methods(
            "handlers/sync/service.py",
            {"_build_pending_record", "_serialize_mediainfo"},
        )
        cls._source = found

    def _make_handler(self, logger=None):
        class Stub:
            pass

        stub = Stub()
        namespace = {
            "logger": logger or _StubLogger(),
            "re": __import__("re"),
            "copy": __import__("copy"),
            "Any": typing.Any,
            "Dict": typing.Dict,
            "Optional": typing.Optional,
            "List": typing.List,
            "MediaInfo": dict,
            "MediaType": types.SimpleNamespace(TV="tv"),
        }
        for name, source in self._source.items():
            exec(source, namespace)
            _bind(stub, namespace, name)
        stub._upgrade_mode = "size"
        stub._OFFLINE_CHECK_DELAYS = [60, 120]
        return stub

    def _build(self, handler, *, task_type, info_hash="", subscribe_id=910):
        return handler._build_pending_record(
            current={},
            pending_key="pending-key",
            task_type=task_type,
            info_hash=info_hash,
            source_hash="",
            share_url="magnet:?xt=urn:btih:demo",
            cloud_dir="/cloud",
            file_name="demo.mkv",
            staging_dir="/cloud",
            staging_name="demo.mkv",
            file_size=1,
            now=1000.0,
            mediainfo=None,
            media_data={},
            subscribe_id=subscribe_id,
        )

    def test_magnet_with_real_hash_not_flagged(self):
        handler = self._make_handler()
        record = self._build(
            handler,
            task_type="magnet",
            info_hash="0123456789ABCDEF0123456789ABCDEF01234567",
        )
        self.assertFalse(record["no_handle"])
        self.assertEqual(
            record["task_id"], "0123456789ABCDEF0123456789ABCDEF01234567"
        )

    def test_magnet_without_hash_flags_no_handle_and_warns(self):
        logger = _RecordingLogger()
        handler = self._make_handler(logger)
        record = self._build(handler, task_type="magnet", info_hash="")
        # task_id 落到订阅级兜底，必须显式携带无句柄标记并打警告。
        self.assertEqual(record["task_id"], "subscribe:910")
        self.assertTrue(record["no_handle"])
        self.assertTrue(any("真实句柄" in line for line in logger.warnings))

    def test_ed2k_with_32bit_hash_not_flagged(self):
        handler = self._make_handler()
        record = self._build(
            handler,
            task_type="ed2k",
            info_hash="0123456789ABCDEF0123456789ABCDEF",
        )
        self.assertFalse(record["no_handle"])

    def test_share_fallback_not_flagged(self):
        # 非哈希型记录不落无句柄标记，保持原有语义。
        handler = self._make_handler()
        record = self._build(handler, task_type="share", info_hash="")
        self.assertEqual(record["task_id"], "subscribe:910")
        self.assertFalse(record["no_handle"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
