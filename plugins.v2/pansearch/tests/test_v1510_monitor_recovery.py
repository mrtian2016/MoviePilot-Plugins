# -*- coding: utf-8 -*-
"""PanSearch v1.5.10 回归测试。

修复两个叠加故障，二者共同造成「后处理任务永久卡住且点停止无反应」：

T1（监控器自愈）：v1.5.9 的停机改动引入竞态——``stop_service`` 对旧调度器
    执行 ``remove_all_jobs()`` + ``shutdown(wait=False)`` 后，``shutdown``
    返回时 ``.running`` 仍可能短暂为 True，而 job 已被摘除。此时
    ``_update_offline_monitor`` 读到该实例并调用
    ``modify_job("PanSearch_OfflineMonitor")``，抛出 ``JobLookupError`` 并
    向上冒泡，导致「网盘文件终态后处理监控」再也无法重建——待处理任务从此
    无人复查，永久停留「后处理中」，停止按钮也无法收尾。
    约束：``modify_job`` 必须被兜底，任何异常都要降级为重建调度器；
    ``stop_service`` 必须「先摘引用、再关调度器」。

T2（ED2K 无限空转）：``_schedule_finalize_retry`` 原先把复查时间钳位到
    ``min(now+延迟, created_at+超时)``，一旦当前时间越过超时点，
    ``next_check_at`` 会被永久钉死在过去的固定时刻，该任务此后每轮都被判为
    「已到期」而反复空转（实测 4 个 ED2K 任务的 ``next_check_at`` 冻结在
    26 小时前、``check_index`` 恒为 5）。
    约束：仅在超时点尚未到达时才允许提前复查；复查时间不得早于
    ``now + _OFFLINE_MIN_RETRY_SECONDS``。

T3（绝对兜底）：``_offline_slow_download_verdict`` 必须对创建超过
    ``_OFFLINE_ED2K_HARD_LIMIT`` 的任务强制判失败，不依赖可能缺失的
    零增长基线键。

T4（停止不挂死）：``_finish_postprocessing_stop`` 对 ``_offline_monitor_lock``
    的等待必须带超时，且未获取锁时不得 release。
"""

import ast
import time
import types
import typing
import unittest
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent

POSTPROCESS_PATH = PLUGIN_ROOT / "handlers" / "sync" / "postprocess.py"
RUNTIME_PATH = PLUGIN_ROOT / "core" / "services" / "runtime.py"
INIT_PATH = PLUGIN_ROOT / "__init__.py"

POSTPROCESS_SOURCE = POSTPROCESS_PATH.read_text(encoding="utf-8")
RUNTIME_SOURCE = RUNTIME_PATH.read_text(encoding="utf-8")
INIT_SOURCE = INIT_PATH.read_text(encoding="utf-8")


class _StubLogger:
    def debug(self, *args, **kwargs):
        pass

    info = warning = error = debug


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


def _cls_attr(rel_path, name, cast=float):
    """读取类级别常量的值（含算术表达式，如 24 * 60 * 60）。"""
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


class TestOfflineMonitorSelfHealing(unittest.TestCase):
    """T1：离线监控器必须能自愈，modify_job 异常不得逃逸。"""

    def test_modify_job_is_guarded(self):
        start = RUNTIME_SOURCE.index("    def _update_offline_monitor(")
        end = RUNTIME_SOURCE.index("    def monitor_offline_tasks(")
        snippet = RUNTIME_SOURCE[start:end]
        # modify_job 必须被 try/except 包裹
        self.assertIn("scheduler.modify_job(", snippet)
        self.assertIn("except Exception as error:", snippet)
        self.assertIn("复用网盘文件终态监控失败，重建调度器", snippet)
        self.assertIn("self._offline_scheduler = None", snippet)

    def test_rebuild_helper_exists_and_used(self):
        self.assertIn("def _start_offline_monitor_scheduler(", RUNTIME_SOURCE)
        start = RUNTIME_SOURCE.index("    def _update_offline_monitor(")
        end = RUNTIME_SOURCE.index("    def monitor_offline_tasks(")
        snippet = RUNTIME_SOURCE[start:end]
        self.assertIn("self._start_offline_monitor_scheduler(pending_count)", snippet)

    def test_stop_service_detaches_before_shutdown(self):
        """先摘引用、再关调度器，消除 .running 竞态窗口。"""
        start = INIT_SOURCE.index("    def stop_service(")
        end = INIT_SOURCE.index("    @eventmanager.register(EventType.PluginReload)")
        snippet = INIT_SOURCE[start:end]
        off_start = snippet.index("offline_scheduler = self._offline_scheduler")
        off_none = snippet.index("self._offline_scheduler = None")
        off_shutdown = snippet.index("offline_scheduler.shutdown(wait=False)")
        self.assertLess(off_none, off_shutdown)
        self.assertLess(off_start, off_none)

    def test_modify_job_behavior_rebuild_on_lookup_error(self):
        """行为验证：modify_job 抛异常时必须重建调度器，异常不得逃逸。"""
        start = RUNTIME_SOURCE.index("    def _update_offline_monitor(")
        end = RUNTIME_SOURCE.index("    def monitor_offline_tasks(")
        snippet = RUNTIME_SOURCE[start:end]
        namespace = {
            "logger": _StubLogger(),
            "datetime": __import__("datetime"),
            "pytz": types.SimpleNamespace(
                timezone=lambda name: __import__("datetime").timezone.utc
            ),
            "settings": types.SimpleNamespace(TZ="Asia/Shanghai"),
            "Optional": typing.Optional,
        }
        exec("class _Host:\n" + snippet, namespace)
        host = namespace["_Host"]()

        calls = {"modify": 0, "rebuild": 0}

        class _BrokenScheduler:
            running = True

            def modify_job(self, *args, **kwargs):
                calls["modify"] += 1
                raise LookupError("No job by the id of PanSearch_OfflineMonitor")

            def remove_all_jobs(self):
                pass

            def shutdown(self, wait=False):
                pass

        host._offline_scheduler_lock = __import__("threading").RLock()
        host._offline_scheduler = _BrokenScheduler()
        host._refresh_postprocessing_sync_tasks = lambda: None

        def _rebuild(pending_count):
            calls["rebuild"] += 1
            return object()

        host._start_offline_monitor_scheduler = _rebuild
        # 若异常逃逸，此调用会抛 LookupError 导致测试失败。
        host._update_offline_monitor(3)
        self.assertEqual(calls["modify"], 1)
        self.assertEqual(calls["rebuild"], 1)


class TestFinalizeRetryNoPastClamp(unittest.TestCase):
    """T2：复查时间不得被钉死在过去。"""

    def _make_host(self):
        ns, statics = extract_methods(
            "handlers/sync/postprocess.py", ["_schedule_finalize_retry"]
        )
        ns["logger"] = _StubLogger()
        ns["Dict"] = typing.Dict
        ns["Any"] = typing.Any
        exec(ns["_schedule_finalize_retry"], ns)
        stub = types.SimpleNamespace()
        stub.__dict__["_schedule_finalize_retry"] = types.MethodType(
            ns["_schedule_finalize_retry"], stub
        )
        stub._OFFLINE_CHECK_DELAYS = (10, 20, 40, 60, 120, 300)
        stub._OFFLINE_TIMEOUT = 120 * 60
        stub._OFFLINE_MIN_RETRY_SECONDS = _cls_attr(
            "handlers/sync/postprocess.py", "_OFFLINE_MIN_RETRY_SECONDS", int
        )
        return stub

    def test_past_deadline_does_not_pin_next_check(self):
        """核心回归：now 已越过超时点，next_check_at 必须仍在未来。"""
        host = self._make_host()
        now = time.time()
        item = {
            "task_type": "ed2k",
            "created_at": now - 26 * 3600,  # 26 小时前，远超 120 分钟超时
            "check_index": 5,
        }
        host._schedule_finalize_retry(item, now)
        self.assertGreater(
            item["next_check_at"], now,
            "next_check_at 被钉死在过去，会导致每轮空转",
        )
        self.assertGreaterEqual(
            item["next_check_at"], now + host._OFFLINE_MIN_RETRY_SECONDS
        )

    def test_deadline_not_reached_still_advances(self):
        host = self._make_host()
        now = time.time()
        item = {"task_type": "ed2k", "created_at": now, "check_index": 0}
        host._schedule_finalize_retry(item, now)
        self.assertGreater(item["next_check_at"], now)

    def test_share_task_also_gets_floor(self):
        host = self._make_host()
        now = time.time()
        item = {"task_type": "share", "created_at": now, "check_index": 0}
        host._schedule_finalize_retry(item, now)
        self.assertGreaterEqual(
            item["next_check_at"], now + host._OFFLINE_MIN_RETRY_SECONDS
        )

    def test_check_index_clamped_at_max(self):
        host = self._make_host()
        item = {"task_type": "ed2k", "created_at": time.time(), "check_index": 99}
        host._schedule_finalize_retry(item, time.time())
        # min(99 + 1, len((10,20,40,60,120,300)) - 1) == 5
        self.assertEqual(item["check_index"], 5)


class TestEd2kHardLimit(unittest.TestCase):
    """T3：ED2K 绝对兜底必须存在并生效。"""

    def test_constants_declared(self):
        limit = _cls_attr(
            "handlers/sync/postprocess.py", "_OFFLINE_ED2K_HARD_LIMIT", int
        )
        self.assertEqual(limit, 24 * 60 * 60)

    def test_verdict_enforces_hard_limit(self):
        limit = _cls_attr(
            "handlers/sync/postprocess.py", "_OFFLINE_ED2K_HARD_LIMIT", int
        )
        ns, statics = extract_methods(
            "handlers/sync/postprocess.py", ["_offline_slow_download_verdict"]
        )
        ns["logger"] = _StubLogger()
        ns["time"] = time
        ns["Dict"] = typing.Dict
        ns["Any"] = typing.Any
        ns["Optional"] = typing.Optional
        ns["Tuple"] = typing.Tuple
        exec(ns["_offline_slow_download_verdict"], ns)
        stub = types.SimpleNamespace()
        stub.__dict__["_offline_slow_download_verdict"] = types.MethodType(
            ns["_offline_slow_download_verdict"], stub
        )
        stub._OFFLINE_ZERO_GROWTH_ROUNDS = 3
        stub._OFFLINE_ED2K_HARD_LIMIT = limit
        stub._offline_timeout_fail_reason = lambda prefix: "超时"

        old_item = {
            "task_type": "ed2k",
            "created_at": time.time() - limit - 60,
        }
        verdict, reason = stub._offline_slow_download_verdict(
            old_item, {"percent": 42.0}, "115 ", file_name="x.mkv"
        )
        self.assertEqual(verdict, "fail")
        self.assertIn("绝对兜底", reason)

        fresh_item = {"task_type": "ed2k", "created_at": time.time()}
        verdict2, _ = stub._offline_slow_download_verdict(
            fresh_item, {"percent": 42.0}, "115 ", file_name="x.mkv"
        )
        self.assertEqual(verdict2, "retry_pending")


class TestExpiredTaskEarlySweep(unittest.TestCase):
    """T5：超期 ED2K/磁力任务必须在任何网盘重活之前被收割。

    历史故障：ED2K 任务越过超时点后 next_check_at 被钉在过去，每轮都被判
    「已到期」，于是每轮都要先做一次 115 全盘递归定位（耗时数分钟到数十分钟）
    才可能走到判定逻辑，监控器被长期占满、队列看似卡死。
    约束：入口处必有绝对时限早筛，且位置必须早于全盘检索调用。
    """

    def _snippet(self):
        start = POSTPROCESS_SOURCE.index("    def monitor_offline_strm_tasks(")
        end = POSTPROCESS_SOURCE.index("        def queue_subscription_completion(")
        return POSTPROCESS_SOURCE[start:end]

    def test_early_sweep_exists(self):
        snippet = self._snippet()
        self.assertIn("expired_keys", snippet)
        self.assertIn("_OFFLINE_ED2K_HARD_LIMIT", snippet)
        self.assertIn("判定卡死退出（绝对兜底）", snippet)

    def test_sweep_covers_all_task_types(self):
        """v1.5.12 F3：早筛不得再按 task_type 过滤。

        v1.5.11 只对 ed2k/magnet 生效，分享转存（share）在「文件被外部
        流程接走」的场景下同样会无限复查（2026-09-12 实测：冬城猎凶
        S01E08 卡 3 小时、check_index 恒为 0）。
        """
        snippet = self._snippet()
        self.assertNotIn(
            'if str(item.get("task_type") or "share") not in {"ed2k", "magnet"}:',
            snippet,
        )

    def test_sweep_before_any_pan_heavy_work(self):
        """早筛必须出现在 due_keys 之后、订阅/网盘处理之前。"""
        snippet = self._snippet()
        i_sweep = snippet.index("expired_keys")
        i_due = snippet.index("due_keys = self._due_pending_keys(")
        self.assertLess(i_due, i_sweep)
        # 早筛须早于首个网盘目录/全盘检索相关调用
        for marker in ("_full_pan_locate_file", "list_files_recursive",
                       "get_offline_task_list_snapshot"):
            if marker in snippet:
                self.assertLess(i_sweep, snippet.index(marker),
                                "%s 必须晚于早筛" % marker)

    def test_sweep_removes_and_persists(self):
        snippet = self._snippet()
        self.assertIn("pending.pop(key, None)", snippet)
        self.assertIn("self._save_offline_pending(pending)", snippet)
        # v1.5.14：超期任务的历史状态写入改为批量接口（定向 UPDATE），
        # 不再逐条调用 _mark_offline_history_status。
        self.assertIn("self._mark_offline_history_status_batch(", snippet)

    def test_sweep_history_write_outside_lock(self):
        """v1.5.14：超期任务的历史落库必须在 _offline_pending_lock 之外。

        历史故障（2026-09-12 实测）：``_mark_offline_history_status`` 在
        ``_offline_pending_lock`` 锁内被调用，而它会全量读历史表并用
        ``replace_all`` 整体重写。历史行一多（165+ 条）就把监控器、
        前端刷新 API、后续调度 tick 全部堵在这把锁上，表现为「后处理
        长期卡住 + 页面很慢」——而 py-spy 抓到的栈顶正是
        ``get_pending_finalize_tasks`` 在等这把锁。
        """
        snippet = self._snippet()
        marker = "# ---- 以下全部在 _offline_pending_lock 之外 ----"
        self.assertIn(marker, snippet)
        i_unlock = snippet.index(marker)
        i_mark = snippet.index("self._mark_offline_history_status_batch(")
        self.assertLess(i_unlock, i_mark, "历史状态写入必须位于锁外")

        locked = snippet[:i_unlock]
        self.assertNotIn("self._mark_offline_history_status(", locked)
        self.assertNotIn("self._mark_offline_history_status_batch(", locked)
        for heavy in (
            '_get_data("history")',
            "_get_data('history')",
            '_save_data("history")',
            "_save_data('history')",
            "_record_platform_transfer_histories(",
        ):
            self.assertNotIn(heavy, locked, "%s 不得出现在锁内" % heavy)

    def test_expired_count_carried_into_result(self):
        """提前返回与主流程返回都必须带上超期计数。"""
        snippet = self._snippet()
        self.assertIn("expired_count = len(expired_keys)", snippet)
        self.assertIn('"failed": expired_count', snippet)
        # 主流程中 failed 初始值必须继承超期数
        self.assertIn("failed = expired_count", POSTPROCESS_SOURCE)

    def test_hard_limit_is_24h(self):
        self.assertEqual(
            _cls_attr("handlers/sync/postprocess.py", "_OFFLINE_ED2K_HARD_LIMIT", float),
            24 * 60 * 60,
        )


class TestFinishStopDoesNotHang(unittest.TestCase):
    """T4：停止流程不得因锁等待而永久挂死。"""

    def test_acquire_has_timeout(self):
        self.assertIn(
            "self._offline_monitor_lock.acquire(\n"
            "            timeout=self._POSTPROCESS_STOP_WAIT_SECONDS\n"
            "        )",
            RUNTIME_SOURCE,
        )

    def test_release_only_when_acquired(self):
        self.assertIn("if acquired:\n                self._offline_monitor_lock.release()",
                      RUNTIME_SOURCE)

    def test_timeout_constant_declared(self):
        self.assertIn("_POSTPROCESS_STOP_WAIT_SECONDS = 30", RUNTIME_SOURCE)


if __name__ == "__main__":
    unittest.main()
