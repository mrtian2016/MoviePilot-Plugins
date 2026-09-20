# -*- coding: utf-8 -*-
"""PanSearch v1.5.14 回归测试：离线监控锁粒度重构。

背景（2026-09-12 实测事故，v1.5.13 之后仍复现）：
    前端始终显示「2 个任务后处理中」，点停止无反应；页面访问也明显变慢。
    py-spy 连续三轮抓栈，栈顶一致指向

        ThreadPoolExecutor-245_0
          -> postprocess.py:829  with self._offline_pending_lock
        MainThread / 其它 worker
          -> runtime.py:1124     len(sync_handler.get_pending_finalize_tasks())

    根因不是死锁（``_offline_pending_lock`` 是 ``RLock``，可重入），而是
    **锁持有时间过长**：

    1. 早筛段在锁内调用 ``_mark_offline_history_status``，而该方法走
       ``_get_data("history")`` 全量读（实测 165+ 条）后，再由
       ``_save_data("history")`` -> ``replace_all`` **整体重写整张表**
       （逐字段深比较 + ``copy.deepcopy``）。
    2. 于是「待处理计数」「前端刷新 API」「下一个调度 tick」全部排队等这
       一把锁；``BackgroundScheduler`` 的线程池（max_instances=2）被耗尽，
       监控器一轮也领不走任务（``check_index`` 恒 0、``_monitor_token``
       全空），队列表现为彻底停摆。

    约束（本文件固化）：
    T1 数据层必须提供按 ``finalize_key`` 的**定向 UPDATE**，不再整体重写。
    T2 历史状态批量落库必须优先走定向 UPDATE 快路径，失败才回退。
    T3 早筛的落库动作全部在 ``_offline_pending_lock`` 之外。
    T4 待处理计数必须**非阻塞**：拿不到锁立刻返回 fallback，绝不排队。
"""

import ast
import types
import typing
import unittest
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent

HISTORY_PATH = PLUGIN_ROOT / "handlers" / "sync" / "history.py"
RUNTIME_PATH = PLUGIN_ROOT / "core" / "services" / "runtime.py"
POSTPROCESS_PATH = PLUGIN_ROOT / "handlers" / "sync" / "postprocess.py"
STORAGE_PATH = PLUGIN_ROOT / "core" / "storage.py"
REPO_PATH = PLUGIN_ROOT / "core" / "database" / "repositories.py"

HISTORY_SOURCE = HISTORY_PATH.read_text(encoding="utf-8")
RUNTIME_SOURCE = RUNTIME_PATH.read_text(encoding="utf-8")
POSTPROCESS_SOURCE = POSTPROCESS_PATH.read_text(encoding="utf-8")
STORAGE_SOURCE = STORAGE_PATH.read_text(encoding="utf-8")
REPO_SOURCE = REPO_PATH.read_text(encoding="utf-8")


class _StubLogger:
    def debug(self, *args, **kwargs):
        pass

    info = warning = error = debug


def extract_methods(rel_path, names):
    """按名字从源码里抽出方法（含 ``@staticmethod`` 装饰器）。"""
    source = (PLUGIN_ROOT / rel_path).read_text(encoding="utf-8")
    tree = ast.parse(source)
    found = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for child in node.body:
                if isinstance(child, ast.FunctionDef) and child.name in names:
                    segment = ast.get_source_segment(source, child)
                    if any(
                        isinstance(d, ast.Name) and d.id == "staticmethod"
                        for d in child.decorator_list
                    ):
                        segment = "@staticmethod\n" + segment
                    found[child.name] = segment
    missing = set(names) - set(found)
    assert not missing, "missing methods: %s" % missing
    return found


def _indent(source: str, width: int = 4) -> str:
    pad = " " * width
    return "\n".join(
        pad + line if line.strip() else line for line in source.splitlines()
    )


def _build_host(methods_source, extra_ns=None):
    import threading

    ns = {
        "logger": _StubLogger(),
        "time": __import__("time"),
        "copy": __import__("copy"),
        "typing": typing,
        "Any": typing.Any,
        "Optional": typing.Optional,
        "Set": typing.Set,
        "List": typing.List,
        "Dict": typing.Dict,
        "Tuple": typing.Tuple,
        "threading": threading,
    }
    if extra_ns:
        ns.update(extra_ns)
    exec("class _Host:\n" + _indent(methods_source), ns)
    return ns["_Host"]()


# --------------------------------------------------------------------------
# T1：数据层定向 UPDATE
# --------------------------------------------------------------------------
class TestTargetedUpdateExists(unittest.TestCase):
    """仓库层与 storage 层都必须提供按 finalize_key 的定向更新入口。"""

    def test_repository_has_targeted_update(self):
        self.assertIn("def update_status_by_finalize_keys(", REPO_SOURCE)

    def test_storage_has_targeted_update(self):
        self.assertIn("def update_history_status_by_finalize_keys(", STORAGE_SOURCE)

    def test_storage_delegates_to_repository(self):
        seg = STORAGE_SOURCE[
            STORAGE_SOURCE.index("def update_history_status_by_finalize_keys("):
        ]
        seg = seg[: seg.index("\n    def ")] if "\n    def " in seg else seg
        self.assertIn("update_status_by_finalize_keys", seg)
        self.assertIn("repositories.history", seg)

    def test_targeted_update_signature(self):
        """签名必须是 (finalize_keys, status, reason="")，且不整表重写。"""
        tree = ast.parse(REPO_SOURCE)
        hit = None
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                for child in node.body:
                    if (
                        isinstance(child, ast.FunctionDef)
                        and child.name == "update_status_by_finalize_keys"
                    ):
                        hit = child
        self.assertIsNotNone(hit, "未找到 update_status_by_finalize_keys")
        names = [a.arg for a in hit.args.args]
        self.assertEqual(names[1:4], ["finalize_keys", "status", "reason"])

    def test_targeted_update_does_not_replace_all(self):
        start = REPO_SOURCE.index("def update_status_by_finalize_keys(")
        body = REPO_SOURCE[start: start + 4000]
        # 定向更新只 UPDATE 命中行，绝不能调用整表 replace_all
        self.assertNotIn("replace_all(", body)
        self.assertIn("row.update(db,", body)

    def test_targeted_update_drops_finalize_key(self):
        """出终态后必须摘掉 finalize_key，防止同一条被反复扫到。"""
        start = REPO_SOURCE.index("def update_status_by_finalize_keys(")
        body = REPO_SOURCE[start: start + 4000]
        self.assertIn('new_payload.pop("finalize_key", None)', body)


# --------------------------------------------------------------------------
# T2：批量落库优先走快路径
# --------------------------------------------------------------------------
class TestBatchMarkUsesFastPath(unittest.TestCase):
    """``_mark_offline_history_status_batch`` 必须先试定向 UPDATE。"""

    def _body(self):
        start = HISTORY_SOURCE.index("def _mark_offline_history_status_batch(")
        tail = HISTORY_SOURCE[start:]
        nxt = tail.find("\n    def ")
        return tail[:nxt] if nxt > 0 else tail

    def test_fast_path_attempted_before_full_rewrite(self):
        body = self._body()
        i_updater = body.index("update_history_status_by_finalize_keys")
        i_full = body.index("with self._offline_pending_lock")
        self.assertLess(i_updater, i_full, "快路径必须先于全量读改写尝试")

    def test_fast_path_falls_back_on_error(self):
        body = self._body()
        self.assertIn("except Exception as error", body)
        self.assertIn("回退全量重写", body)
        self.assertIn("platform_records is not None", body)

    def test_no_legacy_helper_extracted(self):
        """不得拆出 _mark_offline_history_status_legacy（老测试桩抽不到）。"""
        self.assertNotIn(
            "_mark_offline_history_status_legacy", HISTORY_SOURCE
        )

    def test_accepts_both_store_accessors(self):
        """store 既可能是 _get_data_store() 方法，也可能是 _data_store 属性。"""
        body = self._body()
        self.assertIn("_get_data_store", body)
        self.assertIn("_data_store", body)

    def test_empty_keys_short_circuit(self):
        body = self._body()
        self.assertIn("if not normalized_keys", body)


# --------------------------------------------------------------------------
# T3：早筛落库在锁外（与 test_v1510 互补，这里从整函数视角再固化一次）
# --------------------------------------------------------------------------
class TestEarlySweepOutsideLock(unittest.TestCase):
    """早筛段（``monitor_offline_strm_tasks``）锁内区间必须短且无重活。"""

    def _snippet(self):
        start = POSTPROCESS_SOURCE.index("    def monitor_offline_strm_tasks(")
        end = POSTPROCESS_SOURCE.index("        def queue_subscription_completion(")
        return POSTPROCESS_SOURCE[start:end]

    def _locked_region(self):
        snippet = self._snippet()
        start = snippet.index("        with self._offline_pending_lock:")
        end = snippet.index("# ---- 以下全部在 _offline_pending_lock 之外 ----")
        return snippet[start:end]

    def test_unlock_marker_exists(self):
        self.assertIn(
            "# ---- 以下全部在 _offline_pending_lock 之外 ----",
            POSTPROCESS_SOURCE,
        )

    def test_lock_block_is_short(self):
        """锁内区间不得无限膨胀（v1.5.14 基线约 40 行有效代码）。"""
        lines = [
            line
            for line in self._locked_region().splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        self.assertLess(len(lines), 60, "锁内代码行数异常膨胀：%d" % len(lines))

    def test_no_heavy_io_inside_lock(self):
        locked = self._locked_region()
        for heavy in (
            "get_offline_task_list_snapshot",
            "list_files_recursive",
            "_full_pan_locate_file",
            "_get_data(\"history\")",
            "_get_data('history')",
            "_save_data(\"history\")",
            "_save_data('history')",
            "_mark_offline_history_status(",
            "_record_platform_transfer_histories(",
        ):
            self.assertNotIn(heavy, locked, "%s 不得出现在锁内" % heavy)

    def test_locked_region_only_mutates_memory(self):
        """锁内允许的唯一落库动作是写 pending 自身。"""
        locked = self._locked_region()
        self.assertIn("self._save_offline_pending(pending)", locked)
        writes = [
            line.strip()
            for line in locked.splitlines()
            if line.strip().startswith("self._save_")
        ]
        self.assertTrue(
            all("_save_offline_pending(" in line for line in writes),
            "锁内出现了非 pending 的落库调用：%s" % writes,
        )


# --------------------------------------------------------------------------
# T4：计数非阻塞
# --------------------------------------------------------------------------
class TestPendingCountNonBlocking(unittest.TestCase):
    """计数绝不能因为拿不到锁而把整个监控线程拖住。"""

    def test_runtime_uses_non_blocking_counter(self):
        self.assertIn("_count_pending_non_blocking", RUNTIME_SOURCE)
        self.assertNotIn(
            "len(sync_handler.get_pending_finalize_tasks())", RUNTIME_SOURCE
        )

    def test_runtime_counter_defined(self):
        self.assertIn("def _count_pending_non_blocking(", RUNTIME_SOURCE)


class TestCountPendingNonBlockingBehaviour(unittest.TestCase):
    """``_count_pending_non_blocking`` 的取值/降级行为。"""

    @classmethod
    def setUpClass(cls):
        src = extract_methods(
            "core/services/runtime.py", ["_count_pending_non_blocking"]
        )["_count_pending_non_blocking"]
        cls.host = _build_host(src)

    def test_returns_counter_value(self):
        class _H:
            def count_pending_finalize_tasks(self, fallback=0):
                return 7

        self.assertEqual(self.host._count_pending_non_blocking(_H()), 7)

    def test_fallback_when_no_counter(self):
        self.assertEqual(self.host._count_pending_non_blocking(object(), 3), 3)

    def test_zero_when_counter_returns_none(self):
        class _H:
            def count_pending_finalize_tasks(self, fallback=0):
                return None

        self.assertEqual(self.host._count_pending_non_blocking(_H(), 5), 0)

    def test_fallback_when_counter_raises(self):
        class _H:
            def count_pending_finalize_tasks(self, fallback=0):
                raise RuntimeError("boom")

        self.assertEqual(self.host._count_pending_non_blocking(_H(), 4), 4)


class TestCountPendingFinalizeTasksBehaviour(unittest.TestCase):
    """``count_pending_finalize_tasks`` 拿不到锁必须立刻返回 fallback。"""

    @classmethod
    def setUpClass(cls):
        src = extract_methods(
            "handlers/sync/history.py", ["count_pending_finalize_tasks"]
        )["count_pending_finalize_tasks"]
        cls.src = src

    def _host(self, data, lock=None):
        import threading

        host = _build_host(self.src)
        host._get_data = lambda key: dict(data) if data is not None else None
        host._offline_pending_lock = lock or threading.RLock()
        host._OFFLINE_PENDING_KEY = "offline_pending_tasks"
        return host

    def test_counts_pending(self):
        host = self._host({"a": {}, "b": {}})
        self.assertEqual(host.count_pending_finalize_tasks(0), 2)

    def test_zero_when_no_data_accessor(self):
        host = _build_host(self.src)
        host._get_data = None
        import threading

        host._offline_pending_lock = threading.RLock()
        self.assertEqual(host.count_pending_finalize_tasks(9), 0)

    def test_returns_fallback_when_lock_busy(self):
        import threading

        lock = threading.Lock()
        host = self._host({"a": {}, "b": {}, "c": {}}, lock)
        acquired = lock.acquire(blocking=False)
        self.assertTrue(acquired)
        try:
            # 锁被别的线程持有，必须立刻降级，不能阻塞
            self.assertEqual(host.count_pending_finalize_tasks(6), 6)
        finally:
            lock.release()
        self.assertEqual(host.count_pending_finalize_tasks(0), 3)

    def test_uses_non_blocking_acquire(self):
        self.assertIn("acquire(blocking=False)", self.src)


if __name__ == "__main__":
    unittest.main()
