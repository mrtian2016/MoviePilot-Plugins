# -*- coding: utf-8 -*-
"""PanSearch v1.5.3 T1 慢速离线下载不再误判失败。

被测模块依赖 app.*，无法直接导入；沿用 test_wk1_logic.py /
test_v151_offline_logic.py 的 ast 函数抽取方式，把目标方法源码 exec
到桩宿主上验证纯逻辑。覆盖三个分支：仍在下载（进度增长）=> 继续
等待不拉黑；任务已消失且文件不存在 => 真实失败并拉黑；连续 3 轮
进度零增长 => 判定卡死失败。同时断言超时默认值 120 分钟。
"""

import ast
import time
import types
import typing
import unittest
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent


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


def _bind(stub, namespace, name):
    stub.__dict__[name] = types.MethodType(namespace[name], stub)


class _StubLogger:
    def debug(self, *args, **kwargs):
        pass

    info = warning = error = debug


class TestOfflineTimeoutConfig(unittest.TestCase):
    """T1.1: 超时默认 120 分钟且配置链路完整。"""

    def _read(self, rel_path):
        return (PLUGIN_ROOT / rel_path).read_text(encoding="utf-8")

    def test_default_timeout_is_120_minutes(self):
        config_source = self._read("core/config.py")
        self.assertIn('"offline_download_timeout_minutes": 120', config_source)
        service_source = self._read("handlers/sync/service.py")
        self.assertIn("_OFFLINE_TIMEOUT = 120 * 60", service_source)

    def test_sync_handler_accepts_config_override(self):
        service_source = self._read("handlers/sync/service.py")
        # 旧调用方/测试未传参时按 120 分钟兜底，实例配置覆盖类常量。
        self.assertIn(
            "offline_download_timeout_minutes: int = 120", service_source
        )
        self.assertIn(
            "self._OFFLINE_TIMEOUT = timeout_minutes * 60", service_source
        )

    def test_plugin_reads_config_with_fallback(self):
        init_source = self._read("__init__.py")
        self.assertIn(
            'config.get("offline_download_timeout_minutes", 120)',
            init_source,
        )
        self.assertIn(
            "offline_download_timeout_minutes=(\n                self._offline_download_timeout_minutes\n            ),",
            init_source,
        )

    def test_form_field_label_registered(self):
        form_source = self._read("core/api/form_content.py")
        self.assertIn(
            '"offline_download_timeout_minutes": "离线下载超时（分钟）"',
            form_source,
        )

    def test_timeout_fail_reason_uses_configured_minutes(self):
        found = extract_methods(
            "handlers/sync/postprocess.py",
            {"_offline_timeout_fail_reason"},
        )

        class Stub:
            pass

        stub = Stub()
        namespace = {}
        exec(found["_offline_timeout_fail_reason"], namespace)
        _bind(stub, namespace, "_offline_timeout_fail_reason")
        stub._OFFLINE_TIMEOUT = 120 * 60
        self.assertEqual(
            stub._offline_timeout_fail_reason("115 "),
            "115 离线下载超过 120 分钟未完成，已退出",
        )
        stub._OFFLINE_TIMEOUT = 30 * 60
        self.assertEqual(
            stub._offline_timeout_fail_reason("Magnet "),
            "Magnet 离线下载超过 30 分钟未完成，已退出",
        )


class TestOfflineSlowDownloadVerdict(unittest.TestCase):
    """T1.2: 超时终审 missing 后的慢下载判定纯逻辑。"""

    @classmethod
    def setUpClass(cls):
        cls._source = extract_methods(
            "handlers/sync/postprocess.py",
            {"_offline_slow_download_verdict", "_offline_timeout_fail_reason"},
        )

    def _make_handler(self, timeout_seconds=120 * 60):
        class Stub:
            pass

        stub = Stub()
        namespace = {
            "logger": _StubLogger(),
            "time": time,
            "Dict": typing.Dict,
            "Any": typing.Any,
            "Optional": typing.Optional,
            "Tuple": typing.Tuple,
        }
        for name in self._source:
            exec(self._source[name], namespace)
            _bind(stub, namespace, name)
        stub._OFFLINE_TIMEOUT = timeout_seconds
        stub._OFFLINE_ZERO_GROWTH_ROUNDS = 3
        # v1.5.10：ED2K 绝对兜底常量。
        stub._OFFLINE_ED2K_HARD_LIMIT = 24 * 60 * 60
        return stub

    def test_first_timeout_round_records_baseline_and_waits(self):
        handler = self._make_handler()
        item = {}
        verdict, reason = handler._offline_slow_download_verdict(
            item, {"percent": 40.0}, "115 ", file_name="a.mkv",
        )
        self.assertEqual(verdict, "retry_pending")
        self.assertEqual(reason, "")
        self.assertEqual(item["timeout_progress_percent"], 40.0)
        self.assertEqual(item["offline_zero_growth_rounds"], 0)

    def test_progress_growth_resets_counter_and_waits(self):
        handler = self._make_handler()
        item = {"timeout_progress_percent": 40.0, "offline_zero_growth_rounds": 2}
        verdict, reason = handler._offline_slow_download_verdict(
            item, {"percent": 55.0}, "Magnet ", file_name="a.mkv",
        )
        self.assertEqual(verdict, "retry_pending")
        self.assertEqual(reason, "")
        self.assertEqual(item["timeout_progress_percent"], 55.0)
        self.assertEqual(item["offline_zero_growth_rounds"], 0)

    def test_zero_growth_fails_on_third_consecutive_round(self):
        handler = self._make_handler()
        item = {"timeout_progress_percent": 50.0}
        verdicts = []
        for _ in range(3):
            verdict, reason = handler._offline_slow_download_verdict(
                item, {"percent": 50.0}, "115 ", file_name="a.mkv",
            )
            verdicts.append((verdict, reason))
        self.assertEqual(
            [verdict for verdict, _ in verdicts],
            ["retry_pending", "retry_pending", "fail"],
        )
        self.assertIn("零增长", verdicts[2][1])
        self.assertIn("卡死", verdicts[2][1])

    def test_vanished_task_fails_with_configured_timeout_reason(self):
        handler = self._make_handler()
        item = {"timeout_progress_percent": 50.0}
        verdict, reason = handler._offline_slow_download_verdict(
            item, None, "115 ", file_name="a.mkv",
        )
        self.assertEqual(verdict, "fail")
        self.assertIn("120 分钟", reason)

    def test_legacy_row_without_counter_keys_treated_as_zero(self):
        # 旧 pending 载荷缺 offline_zero_growth_rounds 键：按 0 轮处理。
        handler = self._make_handler()
        item = {"timeout_progress_percent": 30.0}
        verdict, _ = handler._offline_slow_download_verdict(
            item, {"percent": 30.0}, "115 ", file_name="a.mkv",
        )
        self.assertEqual(verdict, "retry_pending")
        self.assertEqual(item["offline_zero_growth_rounds"], 1)


class _ShouldNotFail(Exception):
    """不应出现的失败路径哨兵。"""


class TestMonitorSlowDownloadBranches(unittest.TestCase):
    """T1.2: 超时分支接入慢下载判定后的整链行为。"""

    @classmethod
    def setUpClass(cls):
        found = extract_methods(
            "handlers/sync/postprocess.py",
            {"monitor_offline_strm_tasks"},
        )
        cls._monitor_source = found["monitor_offline_strm_tasks"]
        cls._helpers = extract_methods(
            "handlers/sync/postprocess.py",
            {
                "_offline_slow_download_verdict",
                "_offline_timeout_fail_reason",
            },
        )

    def _make_handler(self, item, tasks):
        class Stub:
            pass

        stub = Stub()
        state = {"offline_pending_tasks": {"pending:1": item}}
        calls = {
            "retry": [],
            "blacklist": [],
            "failed": [],
            "progress": [],
        }

        def get_data(key):
            return state.get(key)

        def save_data(key, value):
            state[key] = value

        def update_progress(*args, **kwargs):
            pass

        def schedule_retry(item, now):
            calls["retry"].append(str(item.get("file_name")))

        def add_blacklist(key, reason):
            calls["blacklist"].append((str(key), str(reason)))

        def mark_failed(pending_key, status, reason=""):
            calls["failed"].append((str(pending_key), str(status), str(reason)))

        def persist_progress(item, task):
            calls["progress"].append(float(task.get("percent") or 0))

        class _CloudDirectories:
            def resolve_directory(self, cloud_dir):
                class _DirLookup:
                    checked = False
                    directory_id = None
                return _DirLookup()

            def list_directory(self, directory_id):
                raise _ShouldNotFail("不应列举目录")

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
        exec(self._monitor_source, namespace)
        _bind(stub, namespace, "monitor_offline_strm_tasks")
        for name, source in self._helpers.items():
            helper_namespace = {
                "logger": _StubLogger(),
                "time": time,
                "Dict": typing.Dict,
                "Any": typing.Any,
                "Optional": typing.Optional,
                "Tuple": typing.Tuple,
            }
            exec(source, helper_namespace)
            _bind(stub, helper_namespace, name)
        stub._get_data = get_data
        stub._save_data = save_data
        stub._offline_pending_lock = __import__("threading").Lock()
        stub._OFFLINE_PENDING_KEY = "offline_pending_tasks"
        stub._OFFLINE_MONITOR_LEASE_SECONDS = 300
        stub._OFFLINE_TIMEOUT = 120 * 60
        stub._OFFLINE_ZERO_GROWTH_ROUNDS = 3
        # v1.5.10：ED2K 绝对兜底常量，供 _offline_slow_download_verdict 使用。
        stub._OFFLINE_ED2K_HARD_LIMIT = 24 * 60 * 60
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
        # 超时终审固定 missing：文件尚未落到中转/最终目录。
        stub._offline_timeout_file_verdict = (
            lambda item, now, snapshot, subscribe_cache=None: "missing"
        )
        stub._offline_timeout_should_defer = lambda tasks_valid, verdict: False
        stub._schedule_finalize_retry = schedule_retry
        stub._persist_offline_progress = persist_progress
        stub._add_offline_blacklist = add_blacklist
        stub._cleanup_failed_offline_task = lambda item, reason: None
        stub._mark_offline_history_status = mark_failed
        stub._notify_finalize_dead = lambda *args, **kwargs: None
        stub._postprocess_task_id = lambda item: ""
        stub._task_update = None
        stub._notify_offline_pending_changed = lambda count: None
        stub._FINALIZE_DEAD_REASON = "后处理连续失败 {} 次"
        stub._organize_after_transfer = False
        stub.calls = calls
        stub.state = state
        return stub

    def _base_item(self, task_type, task_id, **extra):
        item = {
            "task_type": task_type,
            "task_id": task_id,
            "file_name": "Show.2020.S01E01.mkv",
            "created_at": time.time() - 9000,
            "subscribe_id": 0,
            "staging_dir": "/pansearch/staging",
            "staging_name": "Show.2020.S01E01.mkv",
            "cloud_dir": "/media",
            "share_url": "magnet:?xt=urn:btih:" + task_id,
        }
        item.update(extra)
        return item

    def _run(self, handler, tasks):
        return handler.monitor_offline_strm_tasks(
            offline_tasks=tasks, offline_tasks_valid=True,
        )

    def test_magnet_still_downloading_defers_without_blacklist(self):
        item = self._base_item(
            "magnet", "ABC123",
            timeout_progress_percent=40.0,
            offline_zero_growth_rounds=0,
        )
        handler = self._make_handler(item, tasks=None)
        result = self._run(handler, tasks=[{"id": "ABC123", "percent": 55.0}])
        # 进度 40% -> 55%：继续等待，不拉黑、不判失败、记录保留。
        self.assertEqual(result["failed"], 0)
        self.assertEqual(handler.calls["blacklist"], [])
        self.assertEqual(handler.calls["failed"], [])
        self.assertEqual(handler.calls["retry"], ["Show.2020.S01E01.mkv"])
        self.assertIn("pending:1", handler.state["offline_pending_tasks"])
        self.assertEqual(
            handler.state["offline_pending_tasks"]["pending:1"][
                "timeout_progress_percent"
            ],
            55.0,
        )

    def test_magnet_vanished_with_no_file_fails_and_blacklists(self):
        item = self._base_item("magnet", "ABC123")
        handler = self._make_handler(item, tasks=None)
        result = self._run(handler, tasks=[])
        # 任务已从 115 离线列表消失且文件终审 missing：真实失败并拉黑。
        self.assertEqual(result["failed"], 1)
        self.assertEqual(len(handler.calls["blacklist"]), 1)
        key, reason = handler.calls["blacklist"][0]
        self.assertEqual(key, "magnet:?xt=urn:btih:ABC123")
        self.assertIn("120 分钟", reason)
        self.assertEqual(handler.calls["failed"][0][1], "失败")
        self.assertNotIn("pending:1", handler.state["offline_pending_tasks"])

    def test_ed2k_zero_growth_three_rounds_fails(self):
        item = self._base_item(
            "ed2k", "XYZ789",
            timeout_progress_percent=50.0,
            offline_zero_growth_rounds=2,
        )
        handler = self._make_handler(item, tasks=None)
        result = self._run(
            handler, tasks=[{"id": "XYZ789", "percent": 50.0}],
        )
        # 第 3 轮零增长：判定卡死，真实失败并拉黑。
        self.assertEqual(result["failed"], 1)
        key, reason = handler.calls["blacklist"][0]
        self.assertEqual(key, "magnet:?xt=urn:btih:XYZ789")
        self.assertIn("零增长", reason)
        self.assertNotIn("pending:1", handler.state["offline_pending_tasks"])

    def test_ed2k_progress_growth_keeps_pending_and_persists_snapshot(self):
        item = self._base_item(
            "ed2k", "XYZ789",
            timeout_progress_percent=20.0,
            offline_zero_growth_rounds=1,
        )
        handler = self._make_handler(item, tasks=None)
        result = self._run(
            handler, tasks=[{"id": "XYZ789", "percent": 35.0}],
        )
        self.assertEqual(result["failed"], 0)
        self.assertEqual(handler.calls["blacklist"], [])
        self.assertEqual(handler.calls["failed"], [])
        # 超时分支的 retry_pending 路径必须保留进度快照并安排复查。
        self.assertIn(35.0, handler.calls["progress"])
        self.assertEqual(handler.calls["retry"], ["Show.2020.S01E01.mkv"])
        self.assertIn("pending:1", handler.state["offline_pending_tasks"])


if __name__ == "__main__":
    unittest.main()
