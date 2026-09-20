# -*- coding: utf-8 -*-
"""PanSearch v1.5.3 T3 失败原因落库测试。

被测模块依赖 app.*，无法直接导入；沿用 test_wk1_logic.py 的 ast
函数抽取方式，把目标方法源码 exec 到桩宿主上验证纯逻辑，
并对无法执行的调用结构做源码级断言。
"""

import ast
import importlib.util
import sys
import threading
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


def read_source(rel_path):
    return (PLUGIN_ROOT / rel_path).read_text(encoding="utf-8")


class _StubLogger:
    def __init__(self):
        self.warnings = []
        self.errors = []

    def debug(self, *args, **kwargs):
        pass

    def info(self, *args, **kwargs):
        pass

    def warning(self, *args, **kwargs):
        self.warnings.append(args[0] if args else "")

    def error(self, *args, **kwargs):
        self.errors.append(args[0] if args else "")


def _bind(stub, namespace, name):
    stub.__dict__[name] = types.MethodType(namespace[name], stub)


class _DataStore:
    """history 数据桩：_get_data/_save_data 闭包宿主。"""

    def __init__(self, data):
        self.data = data

    def get(self, key):
        return self.data.get(key)

    def save(self, key, value):
        self.data[key] = value


class TestMarkOfflineHistoryStatusBatch(unittest.TestCase):
    """失败终态必须携带原因，旧记录缺键不得崩溃。"""

    @classmethod
    def setUpClass(cls):
        cls._source = extract_methods(
            "handlers/sync/history.py",
            {"_mark_offline_history_status", "_mark_offline_history_status_batch"},
        )

    def _make_service(self, history):
        class Stub:
            pass

        stub = Stub()
        store = _DataStore({"history": history})
        stub._get_data = store.get
        stub._save_data = store.save
        stub._offline_pending_lock = threading.Lock()
        stub._record_platform_transfer_histories = lambda records: None
        stub._history_changed = None
        namespace = {
            "logger": _StubLogger(),
            "copy": __import__("copy"),
            "Set": typing.Set,
            "Dict": typing.Dict,
            "Any": typing.Any,
            "List": typing.List,
            "Optional": typing.Optional,
            "Tuple": typing.Tuple,
        }
        for name, source in self._source.items():
            exec(source, namespace)
            _bind(stub, namespace, name)
        return stub, store

    def test_failure_with_reason_sets_failure_reason(self):
        history = [{"share_url": "https://115.com/s/abc", "status": "下载中"}]
        stub, store = self._make_service(history)
        stub._mark_offline_history_status("https://115.com/s/ABC", "失败", "离线下载超时")
        record = store.data["history"][0]
        self.assertEqual(record["status"], "失败")
        self.assertEqual(record["failure_reason"], "离线下载超时")

    def test_failure_without_reason_keeps_existing_reason(self):
        history = [{
            "share_url": "https://115.com/s/abc",
            "status": "处理中",
            "failure_reason": "历史原因",
        }]
        stub, store = self._make_service(history)
        stub._mark_offline_history_status("https://115.com/s/abc", "失败", "")
        self.assertEqual(store.data["history"][0]["failure_reason"], "历史原因")

    def test_failure_without_reason_and_old_payload_gets_placeholder(self):
        # 旧记录没有 failure_reason 键：不崩溃，且失败后必有原因。
        history = [{"share_url": "https://115.com/s/abc", "status": "处理中"}]
        stub, store = self._make_service(history)
        stub._mark_offline_history_status("https://115.com/s/abc", "失败", "")
        self.assertEqual(
            store.data["history"][0]["failure_reason"], "未提供失败原因"
        )

    def test_success_clears_failure_reason(self):
        history = [{
            "share_url": "https://115.com/s/abc",
            "status": "失败",
            "failure_reason": "历史原因",
        }]
        stub, store = self._make_service(history)
        stub._mark_offline_history_status("https://115.com/s/abc", "成功", "")
        record = store.data["history"][0]
        self.assertEqual(record["status"], "成功")
        self.assertNotIn("failure_reason", record)


class TestTransferFileReasonCapture(unittest.TestCase):
    """_transfer_file 失败路径把原因回写 file_item。"""

    @classmethod
    def setUpClass(cls):
        cls._source = extract_methods(
            "handlers/sync/service.py",
            {"_transfer_file"},
        )

    def _make_service(self, transfer_result):
        class Stub:
            pass

        stub = Stub()
        stub._is_cloud_resource_url = lambda url: False
        stub._resource_provider_for_url = lambda url: None
        stub._blacklist_dead_link_share = lambda service, url: None
        stub._stop_requested = lambda: False

        class ShareStub:
            transfer_risk_blocked = False

            @staticmethod
            def transfer_file(**kwargs):
                if isinstance(transfer_result, Exception):
                    raise transfer_result
                return transfer_result

        stub._share_transfer = ShareStub()
        namespace = {
            "logger": _StubLogger(),
            "time": __import__("time"),
            "Optional": typing.Optional,
            "Callable": typing.Callable,
            "Dict": typing.Dict,
            "Any": typing.Any,
        }
        exec(self._source["_transfer_file"], namespace)
        _bind(stub, namespace, "_transfer_file")
        return stub

    def test_transfer_false_records_reason(self):
        stub = self._make_service(False)
        file_item = {"id": "123", "name": "demo.mkv"}
        success = stub._transfer_file(
            "https://115.com/s/abc", file_item, "/target", "demo.mkv", "SHA1"
        )
        self.assertFalse(success)
        self.assertTrue(str(file_item.get("transfer_failure_reason") or "").strip())

    def test_transfer_exception_records_reason_and_reraises(self):
        stub = self._make_service(RuntimeError("链接已失效"))
        file_item = {"id": "123", "name": "demo.mkv"}
        with self.assertRaises(RuntimeError):
            stub._transfer_file(
                "https://115.com/s/abc", file_item, "/target", "demo.mkv", "SHA1"
            )
        self.assertIn("链接已失效", file_item.get("transfer_failure_reason") or "")

    def test_success_clears_stale_reason(self):
        stub = self._make_service(True)
        file_item = {
            "id": "123",
            "name": "demo.mkv",
            "transfer_failure_reason": "上次的失败",
        }
        success = stub._transfer_file(
            "https://115.com/s/abc", file_item, "/target", "demo.mkv", "SHA1"
        )
        self.assertTrue(success)
        self.assertNotIn("transfer_failure_reason", file_item)


class TestEpisodeBatchReasonPropagation(unittest.TestCase):
    """_transfer_episode_batch 的 results 必须携带 reason 键。"""

    def test_results_include_reason_key(self):
        source = read_source("handlers/sync/service.py")
        self.assertIn(
            '"reason": failure_reasons.get(file_id, "")', source,
            "transfer_results 必须透出 failure_reasons 中的失败原因",
        )
        tree = ast.parse(source)
        batch_fn = next(
            node for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef)
            and node.name == "_transfer_episode_batch"
        )
        assigned = set()
        for node in ast.walk(batch_fn):
            if (
                    isinstance(node, ast.Assign)
                    and isinstance(node.targets[0], ast.Name)
            ):
                assigned.add(node.targets[0].id)
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                assigned.add(node.target.id)
        self.assertIn("failure_reasons", assigned)


class TestHistoryFailureReasonOnWrite(unittest.TestCase):
    """movie/television/upgrade 失败落库必须带原因并输出 WARNING。"""

    def _failure_branch(self, rel_path, marker):
        """返回包含 marker 的 logger.warning 调用源码段所在文件全文。"""
        source = read_source(rel_path)
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or getattr(node.func, "attr", "") != "warning":
                continue
            segment = ast.get_source_segment(source, node)
            if segment and marker in segment:
                return source, segment
        return source, None

    def test_television_failure_branch_has_reason_and_warning(self):
        source, segment = self._failure_branch(
            "handlers/sync/television.py", "转存失败"
        )
        self.assertIsNotNone(segment, "television 失败分支缺少 WARNING")
        self.assertIn("原因：{reason}", segment)
        self.assertIn('history_item["failure_reason"] = reason', source)

    def test_upgrade_failure_branch_has_reason_and_warning(self):
        source, segment = self._failure_branch(
            "handlers/sync/upgrade.py", "转存失败"
        )
        self.assertIsNotNone(segment, "upgrade 失败分支缺少 WARNING")
        self.assertIn("原因：{reason}", segment)
        self.assertIn('history_item["failure_reason"] = reason', source)

    def test_movie_failure_branch_has_reason_and_warning(self):
        source, segment = self._failure_branch(
            "handlers/sync/movie.py", "电影转存失败"
        )
        self.assertIsNotNone(segment, "movie 失败分支缺少 WARNING")
        self.assertIn("原因：", segment)
        self.assertIn('history_item["failure_reason"]', source)

    def test_transfer_results_reason_consumed(self):
        for rel_path in (
            "handlers/sync/television.py",
            "handlers/sync/upgrade.py",
        ):
            source = read_source(rel_path)
            self.assertIn(
                'transfer_result.get("reason")', source,
                "%s 必须消费 transfer_results 的 reason" % rel_path,
            )


class TestPostprocessRoundFailureSummary(unittest.TestCase):
    """批量失败场景输出一条汇总 WARNING，不再逐条刷屏。"""

    def test_summary_warning_exists(self):
        source = read_source("handlers/sync/postprocess.py")
        self.assertIn("record_round_failure", source)
        self.assertIn("本轮离线后处理判定失败", source)

    def test_no_per_item_timeout_error_spam(self):
        # 原先三处 logger.error(f"{reason}：{file_name}") 应已并入汇总。
        source = read_source("handlers/sync/postprocess.py")
        self.assertNotIn('logger.error(f"{reason}：{file_name}")', source)

    def test_every_failure_mark_records_round_entry(self):
        # 主循环内每个 failed += 1 前都必须登记 record_round_failure。
        source = read_source("handlers/sync/postprocess.py")
        tree = ast.parse(source)
        monitor_fn = next(
            node for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef)
            and node.name == "monitor_offline_strm_tasks"
        )
        for node in ast.walk(monitor_fn):
            if (
                    isinstance(node, ast.AugAssign)
                    and isinstance(node.target, ast.Name)
                    and node.target.id == "failed"
                    and isinstance(node.op, ast.Add)
            ):
                segment = ast.get_source_segment(source, node) or ""
                # 向上取本条语句前的最近上下文：直接检查所在块的源码段。
                block = ast.get_source_segment(source, node)
                self.assertIsNotNone(block)
                # failed += 1 所在行前 6 行内必须出现 record_round_failure。
                lines = source.splitlines()
                line_no = node.lineno
                context = "\n".join(lines[max(0, line_no - 7):line_no])
                self.assertIn(
                    "record_round_failure", context,
                    "失败计数前缺少 record_round_failure 登记（行 %d）" % line_no,
                )


class TestFinalizeMagnetPackageReason(unittest.TestCase):
    """Magnet 元数据缺失分支补齐告警与失败标记（源码级）。"""

    def test_metadata_missing_branch_marks_failure(self):
        source = read_source("handlers/sync/postprocess.py")
        tree = ast.parse(source)
        fn = next(
            node for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef)
            and node.name == "_finalize_magnet_package"
        )
        segment = ast.get_source_segment(source, fn)
        self.assertIn("媒体元数据不存在", segment)
        self.assertIn('_mark_offline_history_status(pending_key, "失败", reason)', segment)
        self.assertIn("logger.warning", segment)


if __name__ == "__main__":
    unittest.main()
