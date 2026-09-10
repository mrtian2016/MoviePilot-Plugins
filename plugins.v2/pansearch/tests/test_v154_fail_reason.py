# -*- coding: utf-8 -*-
"""PanSearch v1.5.4 T3 失败写入收口测试。

事故：交锋 E10（id150）同一轮里 4 个文件转存成功、订阅进度写满，
但历史仍落了一条 status="失败" 且 payload 无 reason、无 WARNING 的记录。

根因：append_history_records 的终态合并分支在写入非终态更新（下载中/处理中）
时，把仍保持"失败"的记录里已有的 failure_reason 又 pop 掉了，且失败写入
没有任何原因兜底与告警。

本测试沿用 test_wk1_logic.py 的 ast 函数抽取方式（被测模块依赖 app.*），
在桩宿主上做行为级验证，并对无法执行的结构做源码级断言。
"""

import ast
import textwrap
import threading
import types
import typing
import unittest
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent

FAILURE_MARK_PREFIX = "_mark_offline_history_status"


class _StubLogger:
    def __init__(self):
        self.warnings = []
        self.errors = []
        self.infos = []

    def debug(self, *args, **kwargs):
        pass

    def info(self, *args, **kwargs):
        self.infos.append(args[0] if args else "")

    def warning(self, *args, **kwargs):
        self.warnings.append(args[0] if args else "")

    def error(self, *args, **kwargs):
        self.errors.append(args[0] if args else "")


def read_source(rel_path):
    return (PLUGIN_ROOT / rel_path).read_text(encoding="utf-8")


def _method_source(source, node):
    """含装饰器的函数源码（ast.get_source_segment 会丢掉 @classmethod）。"""
    start = node.lineno
    for decorator in node.decorator_list:
        start = min(start, decorator.lineno)
    lines = source.splitlines()
    return "\n".join(lines[start - 1:node.end_lineno])


def extract_methods(rel_path, names):
    source = read_source(rel_path)
    tree = ast.parse(source)
    found = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for child in node.body:
            if isinstance(child, ast.FunctionDef) and child.name in names:
                found[child.name] = _method_source(source, child)
    missing = set(names) - set(found)
    assert not missing, "missing methods: %s" % missing
    return found


def build_service_class(rel_path, names, extra_globals=None):
    """把抽出的方法按原装饰器拼装成一个可实例化的桩类。"""
    methods = extract_methods(rel_path, names)
    segments = [textwrap.indent(src, "    ") for src in methods.values()]
    namespace = {
        "copy": __import__("copy"),
        "hashlib": __import__("hashlib"),
        "re": __import__("re"),
        "json": __import__("json"),
        "os": __import__("os"),
        "time": __import__("time"),
        "Set": typing.Set,
        "List": typing.List,
        "Dict": typing.Dict,
        "Any": typing.Any,
        "Optional": typing.Optional,
        "Tuple": typing.Tuple,
        "MediaInfo": type("MediaInfo", (), {}),
    }
    namespace.update(extra_globals or {})
    exec("class _ExtractedService:\n" + "\n".join(segments), namespace)
    return namespace["_ExtractedService"]


class _MediaTypeStub:
    MOVIE = types.SimpleNamespace(value="电影")
    TV = types.SimpleNamespace(value="电视剧")


# history 模块桩环境所需的全局符号。
_HISTORY_GLOBALS = {"MediaType": _MediaTypeStub, "logger": _StubLogger()}


def _history_failure_class(logger):
    methods = {
        "append_history_records",
        "_history_record_identity",
        "_ensure_history_record_id",
        "_upgrade_scope_identity",
        "_positive_int",
        "_is_upgrade_history",
        "_history_task_types",
        "_is_workflow_history",
        "_ensure_history_failure_reason",
        "_history_failure_label",
        "_warn_history_failure",
    }
    return build_service_class(
        "handlers/sync/history.py", methods,
        {"MediaType": _MediaTypeStub, "logger": logger},
    )


class TestHistoryFailureMerge(unittest.TestCase):
    """失败终态合并：原因不被吞掉，且必有告警。"""

    def setUp(self):
        self.logger = _StubLogger()
        self.Service = _history_failure_class(self.logger)

    def _make_service(self, history):
        service = self.Service()
        service._data = {"history": history}
        service._offline_pending_lock = threading.Lock()
        service._OFFLINE_PENDING_KEY = "offline_pending"
        service._record_platform_transfer_histories = lambda records: None
        service._history_changed = None
        service._get_data = lambda key: service._data.get(key)
        service._save_data = lambda key, value: service._data.__setitem__(key, value)
        service._save_offline_pending = lambda pending: service._data.__setitem__(
            "offline_pending", pending
        )
        service._notify_offline_pending_changed = lambda count: None
        return service

    @staticmethod
    def _record(**fields):
        record = {
            "share_url": "https://115.com/s/AAA",
            "file_name": "E10.mkv",
            "source_file_name": "E10.mkv",
            "tmdb_id": "111",
            "season": 1,
            "episode": 10,
            "type": "电视剧",
            "title": "交锋",
            "subscribe_id": 42,
        }
        record.update(fields)
        return record

    def test_terminal_failure_keeps_reason_on_non_terminal_merge(self):
        # 事故回归：失败记录被"下载中"更新合并后，reason 不得被 pop。
        history = [self._record(status="失败", failure_reason="分享批量转存失败：链接可能已失效")]
        service = self._make_service(history)
        service.append_history_records([
            self._record(status="下载中", finalize_key="k1")
        ])
        record = service._data["history"][0]
        self.assertEqual(record["status"], "失败")
        self.assertEqual(record["failure_reason"], "分享批量转存失败：链接可能已失效")
        self.assertTrue(self.logger.warnings, "失败写入必须产生 WARNING")
        self.assertIn("E10", self.logger.warnings[0])
        self.assertIn("链接可能已失效", self.logger.warnings[0])

    def test_old_record_without_reason_gets_placeholder(self):
        # 旧记录缺 failure_reason 键：不崩溃，失败后补占位原因并告警。
        history = [self._record(status="失败")]
        service = self._make_service(history)
        service.append_history_records([self._record(status="处理中")])
        record = service._data["history"][0]
        self.assertEqual(record["status"], "失败")
        self.assertEqual(record["failure_reason"], "未提供失败原因")
        self.assertTrue(self.logger.warnings)

    def test_fresh_failure_without_reason_is_enforced(self):
        service = self._make_service([])
        service.append_history_records([self._record(status="失败")])
        record = service._data["history"][0]
        self.assertEqual(record["failure_reason"], "未提供失败原因")
        self.assertEqual(len(self.logger.warnings), 1)
        self.assertIn("原因：未提供失败原因", self.logger.warnings[0])

    def test_success_wins_over_later_failure(self):
        # 已成功记录不得被新的失败降级，也不得留下矛盾失败记录。
        history = [self._record(status="成功")]
        service = self._make_service(history)
        service.append_history_records([
            self._record(status="失败", failure_reason="另一来源秒失败")
        ])
        self.assertEqual(len(service._data["history"]), 1)
        record = service._data["history"][0]
        self.assertEqual(record["status"], "成功")
        self.assertNotIn("failure_reason", record)
        self.assertTrue(self.logger.warnings)

    def test_success_wins_over_failure_in_same_round(self):
        # 同一轮 A 源失败、B 源成功：只留成功，不得留矛盾失败记录。
        service = self._make_service([])
        service.append_history_records([
            self._record(status="失败", failure_reason="A源秒失败"),
            self._record(
                share_url="https://115.com/s/BBB",
                file_name="E10b.mkv",
                status="成功",
            ),
        ])
        records = service._data["history"]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["status"], "成功")
        self.assertNotIn("failure_reason", records[0])

    def test_failure_reason_never_lost_across_rounds(self):
        service = self._make_service([])
        service.append_history_records([
            self._record(status="失败", failure_reason="首次失败")
        ])
        service.append_history_records([self._record(status="下载中")])
        service.append_history_records([self._record(status="处理中")])
        record = service._data["history"][0]
        self.assertEqual(record["status"], "失败")
        self.assertEqual(record["failure_reason"], "首次失败")

    def test_success_clears_stale_reason(self):
        history = [self._record(status="失败", failure_reason="旧原因")]
        service = self._make_service(history)
        service.append_history_records([self._record(status="成功")])
        record = service._data["history"][0]
        self.assertEqual(record["status"], "成功")
        self.assertNotIn("failure_reason", record)


class _StubShareTransfer:
    """分享转存通道：秒失败（返回 failed_ids）桩。"""

    transfer_risk_blocked = False

    def __init__(self, failed_ids, success_ids=None, risk_blocked=False):
        self.failed_ids = failed_ids
        self.success_ids = success_ids or []
        self.transfer_risk_blocked = risk_blocked

    def transfer_files_batch(self, **kwargs):
        return list(self.success_ids), list(self.failed_ids)


class TestShareTransferFastFailReason(unittest.TestCase):
    """分享转存通道秒失败分支必须透出失败原因（v1.5.4 T3）。"""

    @classmethod
    def setUpClass(cls):
        cls.Service = build_service_class(
            "handlers/sync/service.py", {"_transfer_episode_batch"}
        )

    def _make_service(self, share_transfer):
        service = self.Service()
        service._stop_requested = lambda: False
        service._is_cloud_resource_url = lambda url: False
        service._is_direct_cloud_resource_url = lambda url: False
        service._is_offline_url = lambda url: False
        service._resource_provider_for_url = lambda url: None
        service._cloud_drive = types.SimpleNamespace(key="115", name="115")
        service._ensure_share_transfer_available = lambda key: None
        service._cross_transfer_enabled = False
        service._timed_sync_call = lambda name, func, **kwargs: func(**kwargs)
        service._share_transfer = share_transfer
        service._blacklist_dead_link_share = lambda svc, url: None
        service._activate_share_transfer_cooldown = lambda key: None
        service._transfer_risk_cooldown = 0
        service._batch_size = 10
        service._batch_interval = 0
        service._cloud_transfer_path = "/transfer"
        service._transfer_task_batch_size = 10
        service._generate_or_queue_strm_batch = lambda items, *a, **k: {}
        service._transfer_companion_subtitles = lambda **k: []
        service._media_server_notifier = types.SimpleNamespace(notify=lambda **k: None)
        service._resource_staging_dir = lambda url, item: ""
        service._serialize_pending_target_subscribe = lambda subscribe: {}
        service._upgrade_mode = None
        return service

    @staticmethod
    def _item():
        return {
            "file": {
                "id": "f1",
                "name": "E10.mkv",
                "url": "https://115.com/s/AAA/file",
                "sha1": "SHA1",
                "size": 1024,
            },
            "target_name": "E10.mkv",
            "target_dir": "/tv/交锋",
            "episode": 10,
            "score": 1,
            "is_upgrade": False,
            "resource": {},
        }

    def test_share_fast_fail_result_carries_reason(self):
        service = self._make_service(_StubShareTransfer(failed_ids=["f1"]))
        results = service._transfer_episode_batch(
            [self._item()],
            "https://115.com/s/AAA",
            types.SimpleNamespace(type=_MediaTypeStub.TV),
            types.SimpleNamespace(id=42),
            1,
            "sub",
        )
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0]["success"])
        reason = str(results[0]["reason"] or "").strip()
        self.assertTrue(reason, "分享秒失败必须透出失败原因")
        self.assertTrue("失效" in reason or "风控" in reason)

    def test_share_risk_blocked_reason(self):
        service = self._make_service(
            _StubShareTransfer(failed_ids=["f1"], risk_blocked=True)
        )
        results = service._transfer_episode_batch(
            [self._item()],
            "https://115.com/s/AAA",
            types.SimpleNamespace(type=_MediaTypeStub.TV),
            types.SimpleNamespace(id=42),
            1,
            "sub",
        )
        reason = str(results[0]["reason"] or "")
        self.assertIn("风控", reason)


class TestShareFastFailWriteEnd2End(unittest.TestCase):
    """分享秒失败原因落库必须带 reason 且触发 WARNING。"""

    def setUp(self):
        self.logger = _StubLogger()
        self.Service = _history_failure_class(self.logger)

    def _make_service(self, history):
        service = self.Service()
        service._data = {"history": history}
        service._offline_pending_lock = threading.Lock()
        service._OFFLINE_PENDING_KEY = "offline_pending"
        service._record_platform_transfer_histories = lambda records: None
        service._history_changed = None
        service._get_data = lambda key: service._data.get(key)
        service._save_data = lambda key, value: service._data.__setitem__(key, value)
        service._save_offline_pending = lambda pending: service._data.__setitem__(
            "offline_pending", pending
        )
        service._notify_offline_pending_changed = lambda count: None
        return service

    def test_share_fast_fail_record_has_reason_and_warning(self):
        service = self._make_service([])
        record = {
            "share_url": "https://115.com/s/AAA",
            "file_name": "E10.mkv",
            "source_file_name": "E10.mkv",
            "tmdb_id": "111",
            "season": 1,
            "episode": 10,
            "type": "电视剧",
            "title": "交锋",
            "subscribe_id": 42,
            "status": "失败",
            "failure_reason": "分享批量转存失败：链接可能已失效",
        }
        service.append_history_records([record])
        stored = service._data["history"][0]
        self.assertEqual(stored["status"], "失败")
        self.assertEqual(stored["failure_reason"], "分享批量转存失败：链接可能已失效")
        self.assertTrue(self.logger.warnings)
        self.assertIn("订阅#42", self.logger.warnings[0])
        self.assertIn("E10", self.logger.warnings[0])
        self.assertIn("链接可能已失效", self.logger.warnings[0])


class TestNoReasonlessFailureWrites(unittest.TestCase):
    """源码级收口断言：不存在无 reason 的失败写入。"""

    HANDLER_FILES = [
        "handlers/sync/history.py",
        "handlers/sync/postprocess.py",
        "handlers/sync/movie.py",
        "handlers/sync/television.py",
        "handlers/sync/upgrade.py",
        "handlers/sync/resources.py",
        "handlers/sync/service.py",
    ]

    def _iter_handler_sources(self):
        for rel_path in self.HANDLER_FILES:
            yield rel_path, read_source(rel_path)

    def test_mark_failure_calls_always_pass_reason(self):
        # 任何 _mark_offline_history_status*(..., "失败") 都必须带原因实参。
        for rel_path, source in self._iter_handler_sources():
            tree = ast.parse(source)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                name = getattr(func, "attr", None) or getattr(func, "id", "")
                if not name.startswith(FAILURE_MARK_PREFIX):
                    continue
                args = list(node.args)
                if len(args) < 2:
                    continue
                status = args[1]
                if not (isinstance(status, ast.Constant) and status.value == "失败"):
                    continue
                self.assertTrue(
                    len(args) >= 3,
                    "%s:%d 失败标记缺少原因实参" % (rel_path, node.lineno),
                )
                reason = args[2]
                if isinstance(reason, ast.Constant):
                    self.assertTrue(
                        str(reason.value or "").strip(),
                        "%s:%d 失败标记原因不能为空" % (rel_path, node.lineno),
                    )

    def test_direct_failure_status_writes_carry_reason(self):
        # 直接写 ["status"] = "失败" 处，邻近必须有 failure_reason 赋值。
        for rel_path, source in self._iter_handler_sources():
            lines = source.splitlines()
            tree = ast.parse(source)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Assign):
                    continue
                target = node.targets[0] if node.targets else None
                if not isinstance(target, ast.Subscript):
                    continue
                key = target.slice
                if not (isinstance(key, ast.Constant) and key.value == "status"):
                    continue
                if not (
                        isinstance(node.value, ast.Constant)
                        and node.value.value == "失败"
                ):
                    continue
                context = "\n".join(
                    lines[max(0, node.lineno - 3):node.lineno + 6]
                )
                self.assertIn(
                    "failure_reason", context,
                    "%s:%d 直接写失败状态却缺少 failure_reason"
                    % (rel_path, node.lineno),
                )

    def test_append_history_records_enforces_reason_and_warning(self):
        source = read_source("handlers/sync/history.py")
        self.assertIn("_ensure_history_failure_reason", source)
        self.assertIn("_warn_history_failure", source)
        tree = ast.parse(source)
        fn = next(
            node for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef)
            and node.name == "append_history_records"
        )
        segment = ast.get_source_segment(source, fn)
        self.assertIn("success_scope_index", segment)
        self.assertIn("_ensure_history_failure_reason", segment)
        self.assertIn("_warn_history_failure", segment)
        self.assertIn('merged["status"] = "成功"', segment)

    def test_movie_registration_failure_uses_warning(self):
        source = read_source("handlers/sync/movie.py")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if getattr(node.func, "attr", "") != "warning":
                continue
            segment = ast.get_source_segment(source, node) or ""
            if "后处理任务登记失败" in segment:
                self.assertIn("failure_reason", segment)
                return
        self.fail("movie 后处理登记失败分支缺少 WARNING")


if __name__ == "__main__":
    unittest.main()
