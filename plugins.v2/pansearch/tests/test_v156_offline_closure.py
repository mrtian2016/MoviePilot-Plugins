# -*- coding: utf-8 -*-
"""PanSearch v1.5.6 F1/F2/F3 回归测试。

F1（整理开关闭环）：转存后整理关闭时，文件一旦被 MoviePilot 等外部流程
    从转存目录接管移走，必须立即判终成功——不再等待文件出现在插件自己
    算出的媒体目录、不再做全盘兜底检索、不再生成 STRM。
F3（dict 条目兼容）：云条目存在 CloudFile 对象与提供方原始 dict 两种形态，
    终态链路取文件名必须统一走 _cloud_entry_name，不得直接用 .name。
F2（异常隔离）：整组执行失败时降级为逐条重试，健康条目不得被坏数据拖累。
"""

import ast
import threading
import time
import types
import typing
import unittest
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent

POSTPROCESS_PATH = PLUGIN_ROOT / "handlers" / "sync" / "postprocess.py"
SUBTITLES_PATH = PLUGIN_ROOT / "handlers" / "sync" / "subtitles.py"
SERVICE_PATH = PLUGIN_ROOT / "handlers" / "sync" / "service.py"
HISTORY_PATH = PLUGIN_ROOT / "handlers" / "sync" / "history.py"
RUNTIME_PATH = PLUGIN_ROOT / "core" / "services" / "runtime.py"
INIT_PATH = PLUGIN_ROOT / "__init__.py"

POSTPROCESS_SOURCE = POSTPROCESS_PATH.read_text(encoding="utf-8")
SUBTITLES_SOURCE = SUBTITLES_PATH.read_text(encoding="utf-8")
HISTORY_SOURCE = HISTORY_PATH.read_text(encoding="utf-8")
RUNTIME_SOURCE = RUNTIME_PATH.read_text(encoding="utf-8")
INIT_SOURCE = INIT_PATH.read_text(encoding="utf-8")


class _RetrySentinel(Exception):
    """走到"继续等待"路径时抛出。"""


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


class TestCloudEntryNameCompat(unittest.TestCase):
    """F3 根因：云条目取名字必须同时兼容 CloudFile 与原始 dict。"""

    def setUp(self):
        ns, statics = extract_methods(
            "handlers/sync/service.py", ["_cloud_entry_name"]
        )
        ns["Any"] = typing.Any
        ns["Mapping"] = typing.Mapping
        self.stub = types.SimpleNamespace()
        _bind(self.stub, ns, "_cloud_entry_name", statics=statics)

    def test_object_entry(self):
        entry = types.SimpleNamespace(name="奥德赛.2024.mkv")
        self.assertEqual(self.stub._cloud_entry_name(entry), "奥德赛.2024.mkv")

    def test_dict_entry_uses_name(self):
        self.assertEqual(self.stub._cloud_entry_name({"name": "a.mkv"}), "a.mkv")

    def test_dict_entry_uses_n_fallback(self):
        self.assertEqual(self.stub._cloud_entry_name({"n": "b.mkv"}), "b.mkv")

    def test_dict_entry_uses_file_name_fallback(self):
        self.assertEqual(
            self.stub._cloud_entry_name({"file_name": "c.mkv"}), "c.mkv"
        )

    def test_missing_name_returns_empty(self):
        self.assertEqual(self.stub._cloud_entry_name({"id": "1"}), "")
        self.assertEqual(self.stub._cloud_entry_name(None), "")


class TestNoBareNameAccess(unittest.TestCase):
    """F3 回归：终态链路不得再对云条目直接用 .name。"""

    def test_postprocess_has_no_bare_target_file_name(self):
        self.assertNotIn("if target_file.name !=", POSTPROCESS_SOURCE)
        self.assertNotIn("if moved.name !=", POSTPROCESS_SOURCE)
        self.assertIn("self._cloud_entry_name(target_file)", POSTPROCESS_SOURCE)
        self.assertIn("self._cloud_entry_name(moved)", POSTPROCESS_SOURCE)

    def test_subtitles_has_no_bare_target_file_name(self):
        self.assertNotIn("if target_file.name !=", SUBTITLES_SOURCE)
        self.assertIn("self._cloud_entry_name(target_file)", SUBTITLES_SOURCE)

    def test_full_pan_verdict_keeps_compat_name(self):
        # 全盘兜底检索的返回值可能是 dict，取名字早已走兼容工具；
        # 修复后下游消费点同样必须走兼容工具。
        self.assertIn(
            "self._cloud_entry_name(target_file) or str(file_name or \"\")",
            POSTPROCESS_SOURCE,
        )

    def test_no_bare_name_in_directory_index(self):
        """F3 补充：目录快照建索引时不得对云条目直接用 .name。

        线上实测残留：magnet 整包条目（目录级 pending_key 形如
        ``magnet:<hash>:-1``）走目录索引时触发
        ``AttributeError: 'dict' object has no attribute 'name'``，
        每约 6 分钟一轮、永不推进。此处锁定 postprocess 两处目录快照
        建索引的写法。

        注：history.py 的同形写法经评估不在本次故障链路上（报错堆栈只
        经过 postprocess 链路），且其测试桩未挂载 ``_cloud_entry_name``，
        强行改动会引入 9 项回归，故此处不再对其施加断言。
        """
        self.assertNotIn("if file_item.name:", POSTPROCESS_SOURCE)
        self.assertNotIn("file_item.name: file_item", POSTPROCESS_SOURCE)
        self.assertNotIn(
            "{value.name: value for value in listing.files if value.name}",
            POSTPROCESS_SOURCE,
        )
        self.assertIn("self._cloud_entry_name(file_item)", POSTPROCESS_SOURCE)
        self.assertIn("self._cloud_entry_name(value)", POSTPROCESS_SOURCE)


class TestDriverNormalizesEntry(unittest.TestCase):
    """F3 收尾：驱动层变更接口必须容忍 dict 形态的云条目。

    真实 traceback（v1.5.7 热更后仍在报错，靠容器内注入 traceback 才抓到）：

        postprocess.py:1309  monitor_offline_strm_tasks
          → postprocess.py:2093  _finalize_magnet_package
            → core/cloud.py:454  guarded
              → drive/p115/files.py:169  rename_file
                    path, native_dict(item), item.name, target_name
                                             ^^^^^^^^^
        AttributeError: 'dict' object has no attribute 'name'

    ``_finalize_magnet_package`` 里 ``source_file`` 来自 ``_match_movie_file`` /
    ``_match_episode_files``，是**目录快照里的提供方原始 dict**（整包 magnet
    场景下 115 返回 dict）。上游用 ``_cloud_entry_name`` / ``.get()`` 都不会崩，
    唯独驱动层按 CloudFile 契约读 ``item.name`` 才炸——这也解释了为什么
    「关 organize 走 rename_file 报错、开 organize 走 move_file 不报错」
    （``move_file`` 用的是 ``native_dict(item)``，本身兼容 Mapping）。
    """

    P115_FILES = (
        PLUGIN_ROOT / "drive" / "p115" / "files.py"
    ).read_text(encoding="utf-8")

    def test_p115_rename_file_normalizes_entry(self):
        self.assertNotIn("path, native_dict(item), item.name, target_name",
                         self.P115_FILES)
        self.assertIn("normalized = item if isinstance(item, CloudFile) "
                      "else cloud_file(item)", self.P115_FILES)
        self.assertIn("normalized.name", self.P115_FILES)

    def test_p115_rename_file_guards_none(self):
        # cloud_file() 可能返回 None（缺 file_id/name），必须显式判空，
        # 否则归一化失败会变成属性错误，比原 bug 更难定位。
        self.assertIn("if normalized is None:", self.P115_FILES)

    def test_move_file_stays_mapping_tolerant(self):
        # move_file 用 native_dict(item) 已兼容 Mapping，不得被改回裸属性读取。
        self.assertIn("native_dict(item), save_path, target_name",
                      self.P115_FILES)


class TestStopServiceNoSelfJoin(unittest.TestCase):
    """v1.5.9：stop_service 关闭调度器必须非阻塞，防止自杀式死锁。

    真实故障（py-spy 抓到的冻结栈，v1.5.8 热更后 15:12:45 起再也登不进）：

        MainThread
          → shutdown (concurrent/futures/thread.py:239)   ← join 执行器 worker
          → shutdown (apscheduler/executors/pool.py:33)
          → shutdown (apscheduler/schedulers/background.py:40)
          → stop_service (__init__.py:2019)
          → PluginManager.init_config → sync_plugins

    MoviePilot 检测到第三方插件更新后调用 ``init_config()``，它会先
    ``stop()`` 所有插件。此时 PanSearch 的 ``_offline_scheduler`` 有 job
    正在执行 ``monitor_offline_strm_tasks``（全盘递归扫描 115 目录，见
    ``list_files_recursive``，耗时不可控）。APScheduler 的 ``shutdown``
    默认 ``wait=True``，会在执行器线程池上 join，等待这个跑不完的 job，
    而 ``stop_service`` 又跑在 uvicorn 主线程上——事件循环被永久冻结：
    页面能打开、所有 ``/api/v1`` 请求一直挂起、无法登录。

    约束：两处调度器关闭都必须显式 ``wait=False``。
    """

    def test_no_bare_scheduler_shutdown(self):
        # 不允许出现无参 shutdown()，默认 wait=True 就是死锁根源。
        self.assertNotIn(".shutdown()", INIT_SOURCE)

    def test_both_schedulers_shutdown_nonblocking(self):
        # v1.5.10：改为「先摘引用、再关调度器」（局部变量 shutdown），
        # 消除 .running 竞态窗口；非阻塞语义不变。
        self.assertIn("scheduler.shutdown(wait=False)", INIT_SOURCE)
        self.assertIn("offline_scheduler.shutdown(wait=False)", INIT_SOURCE)

    def test_runtime_teardown_stays_nonblocking(self):
        # runtime.py 清空队列处同样必须非阻塞，两处语义保持一致。
        self.assertIn("scheduler.shutdown(wait=False)", RUNTIME_SOURCE)


class TestRuntimeIsolation(unittest.TestCase):
    """F2：整组失败降级逐条重试，且失败日志带 traceback。"""

    def test_retry_group_isolated_exists(self):
        self.assertIn("def _retry_group_isolated(", RUNTIME_SOURCE)
        self.assertIn("_retry_group_isolated(", RUNTIME_SOURCE)

    def test_queue_failure_logs_traceback(self):
        self.assertIn("exc_info=True", RUNTIME_SOURCE)

    def test_isolated_retry_skips_healthy_items(self):
        runtime_source = RUNTIME_SOURCE
        start = runtime_source.index("    def _retry_group_isolated(")
        end = runtime_source.index("    def _run_offline_monitor(")
        snippet = runtime_source[start:end]
        namespace = {
            "Any": typing.Any,
            "Dict": typing.Dict,
            "logger": _StubLogger(),
        }
        exec("class _Host:\n" + snippet, namespace)
        host = namespace["_Host"]()

        calls = []

        class _Handler:
            def monitor_offline_strm_tasks(self, **kwargs):
                keys = list(kwargs.get("pending_keys") or [])
                key = keys[0] if keys else ""
                calls.append(key)
                if key == "bad":
                    raise RuntimeError("boom")
                return {"checked": 1, "completed": 1, "failed": 0}

        totals = host._retry_group_isolated(
            _Handler(), {"pending_keys": ["good", "bad"]}, {}
        )
        self.assertEqual(calls, ["good", "bad"])
        self.assertEqual(totals["completed"], 1)
        self.assertEqual(totals["failed"], 1)


class _F1Harness(unittest.TestCase):
    """构造最小可运行的 monitor_offline_strm_tasks 桩环境。"""

    def _make_handler(self, item, entries=None, organize=False):
        state = {"offline_pending_tasks": {"pending:1": item}}
        history = []
        strm_calls = {"count": 0}

        def get_data(key):
            return state.get(key)

        def save_data(key, value):
            state[key] = value

        def mark_history(pending_key, status, reason=""):
            history.append((pending_key, status, reason))

        def schedule_retry(pending_item, now):
            raise _RetrySentinel()

        class _DirLookup:
            def __init__(self, checked=True, directory_id=1):
                self.checked = checked
                self.directory_id = directory_id

        class _Listing:
            def __init__(self, checked=True, files=None):
                self.checked = checked
                self.files = files or []

        class _CloudDirectories:
            def resolve_directory(self, cloud_dir):
                return _DirLookup()

            def list_directory(self, directory_id):
                return _Listing(files=list(entries or []))

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
        exec(self._method_source(), namespace)
        # 真实纯函数实现，避免与生产行为漂移。
        task_id_ns, task_id_statics = extract_methods(
            "handlers/sync/postprocess.py", ["_postprocess_task_id"]
        )
        task_id_ns["Dict"] = typing.Dict
        task_id_ns["Any"] = typing.Any
        exec(task_id_ns["_postprocess_task_id"], task_id_ns)
        stub = types.SimpleNamespace()
        stub.__dict__["monitor_offline_strm_tasks"] = types.MethodType(
            namespace["monitor_offline_strm_tasks"], stub
        )
        stub._get_data = get_data
        stub._save_data = save_data
        stub._offline_pending_lock = threading.Lock()
        stub._OFFLINE_PENDING_KEY = "offline_pending_tasks"
        stub._OFFLINE_MONITOR_LEASE_SECONDS = 300
        stub._OFFLINE_TIMEOUT = 1800
        stub._FILE_FINALIZE_TIMEOUT = 1800
        stub._OFFLINE_ED2K_HARD_LIMIT = 24 * 60 * 60
        stub._STAGING_HANDOFF_GRACE_SECONDS = 300
        stub._cloud_directories = _CloudDirectories()
        stub._cloud_query = object()
        stub._cloud_mutations = object()
        stub._cloud_batch_mutations = None
        stub._offline_tasks = None
        stub._due_pending_keys = (
            lambda pending, now, force=False, pending_keys=None: ["pending:1"]
        )
        stub._save_offline_pending = lambda pending: None
        stub._update_postprocess_progress = (
            lambda item, step, position, total, detail="": None
        )
        stub._schedule_finalize_retry = schedule_retry
        stub._mark_offline_history_status = mark_history
        stub._add_offline_blacklist = lambda *args, **kwargs: None
        stub._cleanup_failed_offline_task = lambda item, reason: None
        stub._notify_finalize_dead = lambda *args, **kwargs: None
        stub._finalize_failure = lambda item, pending_key: False
        stub._FINALIZE_DEAD_REASON = "后处理连续失败 {} 次"
        stub._restore_pending_media_context = lambda item, key: (None, {})
        stub._media_context_key = lambda item: ""
        stub._notify_pending_file_finalized = lambda *args, **kwargs: None
        stub._generate_strm = lambda *args, **kwargs: (
            strm_calls.__setitem__("count", strm_calls["count"] + 1) or None
        )
        stub._strm_generate_enabled = True
        stub._strm_generator = object()
        stub._local_resource_path = "/media"
        stub._postprocess_task_id = task_id_ns["_postprocess_task_id"]
        stub._recompute_expected_cloud_dir = lambda item, file_name: None
        stub._task_update = None
        stub._notify_offline_pending_changed = lambda count: None
        stub._finalize_full_pan_verdict = (
            lambda item, file_name, now: ("retry", "")
        )
        stub._organize_after_transfer = organize
        stub.state = state
        stub.history = history
        stub.strm_calls = strm_calls
        return stub

    @staticmethod
    def _method_source():
        tree = ast.parse(POSTPROCESS_SOURCE)
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and (
                    node.name == "monitor_offline_strm_tasks"
            ):
                return ast.get_source_segment(POSTPROCESS_SOURCE, node)
        raise AssertionError("monitor_offline_strm_tasks not found")

    def _base_item(self, **overrides):
        item = {
            "task_type": "share",
            "task_id": "media:1",
            "file_name": "斯图尔特未能拯救宇宙.2025.mkv",
            "created_at": time.time() - 4000,
            "subscribe_id": 0,
            "staging_dir": "/未整理/待整理",
            "staging_name": "斯图尔特未能拯救宇宙.2025.mkv",
            "cloud_dir": "/影视库/电影/斯图尔特未能拯救宇宙 (2025)",
            "share_url": "",
        }
        item.update(overrides)
        return item


class TestOrganizeOffExternalTakeover(_F1Harness):
    """F1 行为回归：文件被外部流程接管后必须立即判终，不再等待。"""

    def test_seen_then_taken_over_finalizes_success(self):
        item = self._base_item(staging_seen_at=time.time() - 600)
        handler = self._make_handler(item, entries=[], organize=False)
        result = handler.monitor_offline_strm_tasks(
            offline_tasks=[], offline_tasks_valid=True
        )
        self.assertEqual(result["completed"], 1)
        self.assertEqual(result["failed"], 0)
        self.assertEqual(handler.state["offline_pending_tasks"], {})
        self.assertEqual(handler.history, [("pending:1", "成功", "")])
        self.assertEqual(handler.strm_calls["count"], 0)

    def test_never_seen_still_waits_within_grace(self):
        """观察期内（v1.5.12 F2）不可误判成功，必须继续等待（走重试）。"""
        item = self._base_item(created_at=time.time() - 10)
        handler = self._make_handler(item, entries=[], organize=False)
        with self.assertRaises(_RetrySentinel):
            handler.monitor_offline_strm_tasks(
                offline_tasks=[], offline_tasks_valid=True
            )
        self.assertIn("pending:1", handler.state["offline_pending_tasks"])

    def test_never_seen_after_grace_converges(self):
        """v1.5.12 F2：越过观察期且文件仍未出现在转存目录，判定已被
        外部流程接管并收敛出队，不再死等到终审窗口后全盘递归。"""
        item = self._base_item(created_at=time.time() - 4000)
        handler = self._make_handler(item, entries=[], organize=False)
        result = handler.monitor_offline_strm_tasks(
            offline_tasks=[], offline_tasks_valid=True
        )
        self.assertEqual(result["completed"], 1)
        self.assertEqual(result["failed"], 0)
        self.assertEqual(handler.state["offline_pending_tasks"], {})

    def test_organize_on_still_waits_within_grace(self):
        """整理开启时：观察期内文件未就位仍需等待，不得直接判终。"""
        item = self._base_item(
            staging_seen_at=time.time() - 600, created_at=time.time() - 10
        )
        handler = self._make_handler(item, entries=[], organize=True)
        with self.assertRaises(_RetrySentinel):
            handler.monitor_offline_strm_tasks(
                offline_tasks=[], offline_tasks_valid=True
            )
        self.assertIn("pending:1", handler.state["offline_pending_tasks"])

    def test_organize_on_after_grace_converges(self):
        """v1.5.12 F2：整理开启但文件已被外部接走时同样收敛，避免死循环。"""
        item = self._base_item(
            staging_seen_at=time.time() - 600, created_at=time.time() - 4000
        )
        handler = self._make_handler(item, entries=[], organize=True)
        result = handler.monitor_offline_strm_tasks(
            offline_tasks=[], offline_tasks_valid=True
        )
        self.assertEqual(result["completed"], 1)


class TestF1SourceWiring(_F1Harness):
    """F1 接线断言：新增的判终分支与 seen 标记均已就位。"""

    def test_staging_seen_marker_written(self):
        self.assertIn('item["staging_seen_at"] = now', POSTPROCESS_SOURCE)

    def test_finalize_branch_uses_seen_marker(self):
        self.assertIn("not self._organize_after_transfer and item.get(", POSTPROCESS_SOURCE)
        self.assertIn('"staging_seen_at"', POSTPROCESS_SOURCE)


if __name__ == "__main__":
    unittest.main()
