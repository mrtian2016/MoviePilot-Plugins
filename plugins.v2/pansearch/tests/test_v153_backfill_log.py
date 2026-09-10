# -*- coding: utf-8 -*-
"""PanSearch v1.5.3 T4: 存量核对回填成功路径的 INFO 日志。

沿用 test_v151_offline_logic.py 的 ast 函数抽取方式（被测模块依赖
app.* 无法直接导入），注入记录型 logger 断言回填成功时发出含记录
标识与网盘实证（目录/文件名）的 INFO 日志。
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


class _RecordingLogger:
    """记录 (级别, 消息) 的 logger 替身，用于断言 INFO 输出。"""

    def __init__(self):
        self.records = []

    def _log(self, level, message):
        self.records.append((level, str(message)))

    def debug(self, message, *args, **kwargs):
        self._log("debug", message)

    def info(self, message, *args, **kwargs):
        self._log("info", message)

    def warning(self, message, *args, **kwargs):
        self._log("warning", message)

    def error(self, message, *args, **kwargs):
        self._log("error", message)


class _FakeFile:
    def __init__(self, name, sha1=""):
        self.name = name
        self.sha1 = sha1


class _DirLookup:
    def __init__(self, checked=True, directory_id=1):
        self.checked = checked
        self.directory_id = directory_id


class _DirListing:
    def __init__(self, checked=True, files=()):
        self.checked = checked
        self.files = list(files)


class _CloudDirectoriesStub:
    def __init__(self, directories):
        # directories: {dir: (checked, [files])}
        self._directories = directories

    def resolve_directory(self, cloud_dir):
        if cloud_dir not in self._directories:
            return _DirLookup(checked=True, directory_id=None)
        return _DirLookup(checked=True, directory_id=cloud_dir)

    def list_directory(self, directory_id):
        checked, files = self._directories.get(directory_id, (True, []))
        return _DirListing(checked=checked, files=files)


class TestBackfillSuccessInfoLog(unittest.TestCase):
    """T4: 回填成功（文件已在网盘就绪→失败记录改成功）必须打 INFO。"""

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
        state = {
            "history": history,
            "offline_pending_tasks": pending or {},
        }
        calls = {"platform": [], "notified": [], "history_changed": 0}

        def get_data(key):
            return state.get(key)

        def save_data(key, value):
            state[key] = value

        self.logger = _RecordingLogger()
        namespace = {
            "logger": self.logger,
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

    def _info_messages(self):
        return [message for level, message in self.logger.records
                if level == "info"]

    def test_ready_backfill_emits_info_with_label_and_evidence(self):
        record = self._failed_record()
        handler = self._make_handler([record])
        handler._cloud_directories = _CloudDirectoriesStub({
            "/staging": (True, [_FakeFile("movie.mkv")]),
            "/media/movies": (True, []),
        })
        repaired = handler.reconcile_offline_history_backfill()
        # 行为断言：失败记录被回填为成功并补发通知。
        self.assertEqual(repaired, 1)
        self.assertEqual(record["status"], "成功")
        self.assertEqual(len(handler.calls["notified"]), 1)
        # 单条回填日志：INFO 且同时含记录标识与实证（目录+文件名）。
        ready_logs = [
            message for message in self._info_messages()
            if "自动回填为成功" in message
        ]
        self.assertEqual(len(ready_logs), 1)
        self.assertIn("测试电影", ready_logs[0])
        self.assertIn("/staging/movie.mkv", ready_logs[0])

    def test_batch_summary_info_lists_record_labels(self):
        records = [
            self._failed_record(
                share_url=f"https://115.com/s/{index}",
                file_name=f"movie{index}.mkv",
            )
            for index in range(2)
        ]
        handler = self._make_handler(records)
        handler._cloud_directories = _CloudDirectoriesStub({
            "/staging": (True, [_FakeFile(record["file_name"])
                                for record in records]),
            "/media/movies": (True, []),
        })
        self.assertEqual(handler.reconcile_offline_history_backfill(), 2)
        summary_logs = [
            message for message in self._info_messages()
            if "存量核对自动回填" in message
        ]
        self.assertEqual(len(summary_logs), 1)
        self.assertIn("2 条", summary_logs[0])
        self.assertIn("测试电影、测试电影", summary_logs[0])

    def test_tv_record_label_carries_season_episode(self):
        record = self._failed_record(
            type="电视剧", season=2, episode=5,
        )
        handler = self._make_handler([record])
        handler._cloud_directories = _CloudDirectoriesStub({
            "/staging": (True, [_FakeFile("movie.mkv")]),
        })
        self.assertEqual(handler.reconcile_offline_history_backfill(), 1)
        ready_logs = [
            message for message in self._info_messages()
            if "自动回填为成功" in message
        ]
        self.assertEqual(len(ready_logs), 1)
        self.assertIn("测试电影 S02E05", ready_logs[0])

    def test_missing_record_stays_debug_quiet(self):
        record = self._failed_record()
        handler = self._make_handler([record])
        handler._cloud_directories = _CloudDirectoriesStub({
            "/staging": (True, []),
            "/media/movies": (True, [_FakeFile("other.mkv")]),
        })
        self.assertEqual(handler.reconcile_offline_history_backfill(), 0)
        self.assertEqual(record["status"], "失败")
        # 未回填（文件确实不存在）不打 INFO，保持日志量克制。
        self.assertEqual(self._info_messages(), [])

    def test_verdict_source_uses_info_not_debug(self):
        source = self._source["_offline_backfill_verdict"]
        self.assertIn("logger.info(", source)
        self.assertNotIn("logger.debug(", source)


if __name__ == "__main__":
    unittest.main()
