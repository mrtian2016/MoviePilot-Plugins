# -*- coding: utf-8 -*-
"""PanSearch v1.5.12 回归测试：后处理「卡死 + 终止不了」根治。

背景（2026-09-12 实测事故）：
    一条普通分享转存任务（``task_type=share``，冬城猎凶 S01E08）自登记起
    3 小时 ``check_index`` 恒为 0、``_monitor_token`` 从未写入，前端永远
    显示「处理中」，点击停止也毫无反应。

T1（停止租约回收，F1）：``_monitor_offline_task_groups`` 会把 stopping
    任务的 ``postprocess_stop_pending_keys`` 从后处理队列中剔除，而负责
    收尾的停止线程是 daemon 线程、可能卡在网盘请求上永不返回，且原实现
    对 stopping 没有任何超时保护 —— 一旦标记留下，这些 pending 既不会被
    处理、也不会被停止。
    约束：stopping 必须有租约（``_POSTPROCESS_STOP_LEASE_SECONDS``），
    超期由监控器（每轮必跑）回收并强制终结；``_finish_postprocessing_stop``
    提前 return 前必须丢弃失效标记。

T2（外部流程接管即收敛，F2）：转存目录可正常列举、但目标文件从未在该
    目录出现过，说明文件落盘后已被 MoviePilot 目录整理等外部流程接走。
    此时既等不到 staging 命中，也等不到插件自算媒体目录命中（外部命名与
    插件规范名不同），历史实现只能死等到 120 分钟终审窗口再全盘递归。
    约束：观察期后必须按「已由外部流程接管」直接收敛出队。

T3（绝对兜底全类型化，F3）：v1.5.11 的 24 小时早筛只对 ed2k/magnet 生效，
    share 任务同样会无限复查。约束：早筛不得再按 task_type 过滤。

T4（强制清理入口，F4）：必须存在按 key / 全量强制出队并写历史原因的方法，
    用于从历史故障中一次性恢复。
"""

import types
import typing
import unittest
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent

POSTPROCESS_PATH = PLUGIN_ROOT / "handlers" / "sync" / "postprocess.py"
RUNTIME_PATH = PLUGIN_ROOT / "core" / "services" / "runtime.py"

POSTPROCESS_SOURCE = POSTPROCESS_PATH.read_text(encoding="utf-8")
RUNTIME_SOURCE = RUNTIME_PATH.read_text(encoding="utf-8")


class _StubLogger:
    def debug(self, *args, **kwargs):
        pass

    info = warning = error = debug


def extract_methods(rel_path, names):
    import ast
    import time as _time

    source = (PLUGIN_ROOT / rel_path).read_text(encoding="utf-8")
    tree = ast.parse(source)
    found = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for child in node.body:
                if isinstance(child, ast.FunctionDef) and child.name in names:
                    source_segment = ast.get_source_segment(source, child)
                    # get_source_segment 从 def 行起算，装饰器会丢失，需补回
                    if any(
                            isinstance(d, ast.Name) and d.id == "staticmethod"
                            for d in child.decorator_list
                    ):
                        source_segment = "@staticmethod\n" + source_segment
                    found[child.name] = source_segment
    missing = set(names) - set(found)
    assert not missing, "missing methods: %s" % missing
    return found


def _cls_attr(rel_path, name, cast=float):
    import ast

    source = (PLUGIN_ROOT / rel_path).read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for child in node.body:
                if (
                        isinstance(child, ast.Assign)
                        and len(child.targets) == 1
                        and isinstance(child.targets[0], ast.Name)
                        and child.targets[0].id == name
                ):
                    return cast(eval(compile(ast.Expression(child.value), "", "eval")))
    raise AssertionError("class attr not found: %s" % name)


def _indent(source: str, width: int = 4) -> str:
    """把提取出的方法源码重新缩进为类体。"""
    pad = " " * width
    return "\n".join(
        pad + line if line.strip() else line for line in source.splitlines()
    )


def _build_host(methods_source, extra_ns=None):
    import threading

    ns = {
        "logger": _StubLogger(),
        "time": __import__("time"),
        "typing": typing,
        "Optional": typing.Optional,
        "Set": typing.Set,
        "List": typing.List,
        "Dict": typing.Dict,
        "Any": typing.Any,
        "threading": threading,
    }
    if extra_ns:
        ns.update(extra_ns)
    exec("class _Host:\n" + _indent(methods_source), ns)
    return ns["_Host"]()


class TestStopLeaseReclaim(unittest.TestCase):
    """T1：stopping 必须有租约，超期强制回收。"""

    def _make_host(self, tasks):
        import time as _time_module

        found = extract_methods(
            "core/services/runtime.py",
            ["_collect_stopping_keys", "_discard_postprocess_stop"],
        )
        host = _build_host(
            found["_collect_stopping_keys"] + "\n" + found["_discard_postprocess_stop"]
        )
        host._POSTPROCESS_STOP_LEASE_SECONDS = _cls_attr(
            "core/services/runtime.py", "_POSTPROCESS_STOP_LEASE_SECONDS", int
        )
        host._sync_tasks_lock = __import__("threading").RLock()
        host._sync_tasks = tasks
        host._mark_runtime_changed = lambda: None
        return host

    def test_lease_constant_is_positive(self):
        self.assertGreater(
            _cls_attr(
                "core/services/runtime.py", "_POSTPROCESS_STOP_LEASE_SECONDS", int
            ),
            0,
        )

    def test_within_lease_keys_are_excluded(self):
        import time as time_module

        now = time_module.time()
        host = self._make_host({
            "t1": {
                "status": "stopping",
                "postprocess_stop_token": "tok",
                "postprocess_stop_pending_keys": {"key-a"},
                "postprocess_stop_started_at": now - 5,
            }
        })
        self.assertEqual(host._collect_stopping_keys(now), {"key-a"})
        # 未超期不得改动任务状态
        self.assertEqual(host._sync_tasks["t1"]["status"], "stopping")

    def test_expired_lease_is_reclaimed(self):
        import time as time_module

        now = time_module.time()
        host = self._make_host({
            "t1": {
                "status": "stopping",
                "postprocess_stop_token": "tok",
                "postprocess_stop_pending_keys": {"key-a"},
                "postprocess_stop_started_at": now - 10_000,
            }
        })
        self.assertEqual(host._collect_stopping_keys(now), set())
        task = host._sync_tasks["t1"]
        self.assertEqual(task["status"], "stopped")
        self.assertNotIn("postprocess_stop_token", task)
        self.assertNotIn("postprocess_stop_pending_keys", task)

    def test_missing_started_at_is_initialized(self):
        import time as time_module

        now = time_module.time()
        host = self._make_host({
            "t1": {
                "status": "stopping",
                "postprocess_stop_token": "tok",
                "postprocess_stop_pending_keys": {"key-a"},
            }
        })
        self.assertEqual(host._collect_stopping_keys(now), {"key-a"})
        self.assertIn("postprocess_stop_started_at", host._sync_tasks["t1"])

    def test_discard_matching_token_rolls_back(self):
        host = self._make_host({})
        task = {
            "status": "stopping",
            "postprocess_stop_token": "tok",
            "postprocess_stop_pending_keys": {"key-a"},
        }
        host._discard_postprocess_stop(task, "tok")
        self.assertEqual(task["status"], "postprocessing")
        self.assertNotIn("postprocess_stop_token", task)

    def test_discard_ignores_other_token(self):
        host = self._make_host({})
        task = {
            "status": "stopping",
            "postprocess_stop_token": "tok",
            "postprocess_stop_pending_keys": {"key-a"},
        }
        host._discard_postprocess_stop(task, "other")
        self.assertEqual(task["status"], "stopping")
        self.assertEqual(task["postprocess_stop_token"], "tok")

    def test_monitor_uses_collect_helper(self):
        start = RUNTIME_SOURCE.index("    def _monitor_offline_task_groups(")
        end = RUNTIME_SOURCE.index("    def _retry_group_isolated(")
        snippet = RUNTIME_SOURCE[start:end]
        self.assertIn("self._collect_stopping_keys()", snippet)


class TestExternalHandoffConvergence(unittest.TestCase):
    """T2：文件已由外部流程接管时必须收敛，不得死等到全盘递归。"""

    def test_grace_constant_is_positive(self):
        self.assertGreater(
            _cls_attr(
                "handlers/sync/postprocess.py",
                "_STAGING_HANDOFF_GRACE_SECONDS",
                int,
            ),
            0,
        )

    def test_handoff_branch_exists(self):
        self.assertIn("_STAGING_HANDOFF_GRACE_SECONDS", POSTPROCESS_SOURCE)
        self.assertIn("判定由外部流程接管", POSTPROCESS_SOURCE)

    def test_handoff_gated_by_grace_and_seen_marker(self):
        start = POSTPROCESS_SOURCE.index(
            "                    # v1.5.12 F2：文件从未在转存目录出现过"
        )
        end = POSTPROCESS_SOURCE.index(
            "                    if final_dir != staging_dir:"
        )
        snippet = POSTPROCESS_SOURCE[start:end]
        # 三个必要条件缺一不可：仅未定位到文件、转存目录可正常列举、
        # 且已越过观察期（避免刚转存尚未索引可见时误判）
        self.assertIn("not target_file", snippet)
        self.assertIn("directory_valid", snippet)
        self.assertIn("_STAGING_HANDOFF_GRACE_SECONDS", snippet)
        self.assertIn("finalize_after_metadata", snippet)


class TestHardLimitCoversAllTaskTypes(unittest.TestCase):
    """T3：24 小时绝对兜底不得再按 task_type 过滤。"""

    def test_early_sweep_has_no_task_type_filter(self):
        start = POSTPROCESS_SOURCE.index(
            "            # v1.5.10：绝对时限早筛"
        )
        end = POSTPROCESS_SOURCE.index(
            "            monitor_token_early = uuid.uuid4().hex"
        )
        snippet = POSTPROCESS_SOURCE[start:end]
        self.assertNotIn(
            "not in {\"ed2k\", \"magnet\"}",
            snippet,
            "早筛仍在按 task_type 过滤，share 任务会继续无限复查",
        )

    def test_hard_limit_constant_present(self):
        self.assertEqual(
            _cls_attr(
                "handlers/sync/postprocess.py", "_OFFLINE_ED2K_HARD_LIMIT", int
            ),
            24 * 60 * 60,
        )


class TestForceClearPending(unittest.TestCase):
    """T4：必须能强制清理僵尸 pending。"""

    def _make_stub(self, pending):
        import threading

        found = extract_methods(
            "handlers/sync/postprocess.py", ["force_clear_offline_pending"]
        )
        stub = _build_host(found["force_clear_offline_pending"])
        stub._get_data = lambda key: pending
        stub._OFFLINE_PENDING_KEY = "pending_offline_strm"
        stub._offline_pending_lock = threading.RLock()
        stub._save_offline_pending = lambda p: None
        stub._mark_offline_history_status = lambda k, s, r: None
        notified = {}
        stub._notify_offline_pending_changed = lambda c: notified.__setitem__("count", c)
        return stub, notified

    def test_clear_all(self):
        pending = {"k1": {"file_name": "a.mkv"}, "k2": {"file_name": "b.mkv"}}
        stub, notified = self._make_stub(pending)
        self.assertEqual(stub.force_clear_offline_pending(), 2)
        self.assertEqual(pending, {})
        self.assertEqual(notified["count"], 0)

    def test_clear_selected_keys_only(self):
        pending = {"k1": {"file_name": "a.mkv"}, "k2": {"file_name": "b.mkv"}}
        stub, notified = self._make_stub(pending)
        self.assertEqual(stub.force_clear_offline_pending({"k1"}), 1)
        self.assertEqual(set(pending), {"k2"})
        self.assertEqual(notified["count"], 1)

    def test_clear_nonexistent_key_is_noop(self):
        pending = {"k1": {"file_name": "a.mkv"}}
        stub, _ = self._make_stub(pending)
        self.assertEqual(stub.force_clear_offline_pending({"nope"}), 0)
        self.assertEqual(set(pending), {"k1"})

    def test_clear_empty_pending_is_noop(self):
        stub, _ = self._make_stub({})
        self.assertEqual(stub.force_clear_offline_pending(), 0)

    def test_api_entry_exists(self):
        self.assertIn("def api_force_clear_offline_pending(", RUNTIME_SOURCE)


if __name__ == "__main__":
    unittest.main()
