# -*- coding: utf-8 -*-
"""PanSearch v1.5.4 T4：pending 推进修复 + 存量核对升级。

事故（奥德赛 id153）：手动 /cloud_link 提交 magnet 后登记了
offline_pending_tasks，但 next_check_at 过期后再未被后续轮次拾取；根因是
手动提交通道（_queue_magnet_package）与订阅后处理通道
（_build_pending_record）登记的 pending schema 不一致（缺
info_hash/source_sha1/staging_dir/staging_name/file_size 等定位字段）。

被测模块依赖 app.* 无法直接导入，沿用 tests/ 的 ast 函数抽取方式把目标
方法 exec 到桩宿主上验证纯逻辑。覆盖：
1) 两条通道 pending schema 统一（手动通道补齐定位字段）；
2) next_check_at 过期的 pending 下一轮必检必处理，缺字段有兜底 + WARNING
   一次（奥德赛场景最小复现）；
3) 存量核对反向回查：离线假成功（成功但网盘无文件）降级为失败并修正
   订阅进度 note，检索能力不足时绝不误降级。
"""

import ast
import copy
import time
import types
import typing
import unittest
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent

ODYSSEY_HASH = "5109" + "AB" * 18  # 40 位 info_hash
ODYSSEY_PENDING_KEY = f"magnet:{ODYSSEY_HASH}:153"
NOW = 1789041000.0  # 晚于 next_check_at=1789040294


def extract_methods(rel_path, names):
    """抽取方法源码（返回 name -> source，以及 classmethod 集合）。"""
    source = (PLUGIN_ROOT / rel_path).read_text(encoding="utf-8")
    tree = ast.parse(source)
    found = {}
    classmethods = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for child in node.body:
            if isinstance(child, ast.FunctionDef) and child.name in names:
                found[child.name] = ast.get_source_segment(source, child)
                for dec in child.decorator_list:
                    if isinstance(dec, ast.Name) and dec.id == "classmethod":
                        classmethods.add(child.name)
    missing = set(names) - set(found)
    assert not missing, "missing methods: %s" % missing
    return found, classmethods


def _bind(stub, namespace, name, source, statics=()):
    exec(source, namespace)
    value = namespace[name]
    params = list(__import__("inspect").signature(value).parameters)
    if params and params[0] == "self":
        stub.__dict__[name] = types.MethodType(value, stub)
    else:
        stub.__dict__[name] = value


class _RecordingLogger:
    """记录 (级别, 消息) 的 logger 替身，用于断言 WARNING/INFO 输出。"""

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

    def messages(self, level):
        return [message for item_level, message in self.records
                if item_level == level]


# ---------------------------------------------------------------------------
# 1) 手动提交与订阅通道 pending schema 统一
# ---------------------------------------------------------------------------

SERVICE_METHODS = {"_queue_magnet_package", "_build_pending_record"}

UNIFIED_PENDING_KEYS = {
    "pending_key", "task_type", "task_id", "info_hash", "source_sha1",
    "share_url", "cloud_dir", "file_name", "staging_dir", "staging_name",
    "file_size", "status", "created_at", "next_check_at", "history_ready",
    "subscribe_id", "success_episodes", "notification_episodes", "season",
}


class _StubMediaInfo:
    def __init__(self, media_type="电影", title="奥德赛"):
        self.type = media_type
        self.title = title

    def to_dict(self):
        return {"type": self.type, "title": self.title}


class _EnumValue:
    def __init__(self, value):
        self.value = value

    def __eq__(self, other):
        return self.value == other

    def __hash__(self):
        return hash(self.value)

    def __str__(self):
        return self.value


class _StubMediaType:
    TV = _EnumValue("电视剧")
    MOVIE = _EnumValue("电影")


class _StubOfflineDownload:
    def __init__(self, submit_hash=""):
        self.submit_hash = submit_hash
        self.submitted = []

    def add_offline_download(self, url, save_path, target_name=None):
        self.submitted.append((url, save_path))
        return self.submit_hash

    def get_offline_tasks(self, force=False):
        return []

    def parse_magnet_link(self, url, fetch_metadata=False):
        return None


class TestManualChannelPendingSchema(unittest.TestCase):
    """T4.1：手动 magnet 通道经统一 builder 登记完整定位字段。"""

    @classmethod
    def setUpClass(cls):
        cls._source, _ = extract_methods(
            "handlers/sync/service.py", SERVICE_METHODS
        )

    def _make_handler(self, resource, subscribe_id=153):
        class Stub:
            pass

        stub = Stub()
        self.logger = _RecordingLogger()
        namespace = {
            "logger": self.logger,
            "time": time,
            "copy": copy,
            "re": __import__("re"),
            "Any": typing.Any,
            "Dict": typing.Dict,
            "List": typing.List,
            "Optional": typing.Optional,
            "Set": typing.Set,
            "Tuple": typing.Tuple,
            "MediaType": _StubMediaType,
            "MediaInfo": _StubMediaInfo,
            "tmdb_id_of": lambda subscribe: 0,
            "legacy_media_ids": lambda subscribe: {},
        }
        for name, source in self._source.items():
            _bind(stub, namespace, name, source)
        stub._OFFLINE_PENDING_KEY = "offline_pending_tasks"
        stub._OFFLINE_CHECK_DELAYS = (10, 20, 40, 60, 120, 300)
        stub._cloud_transfer_path = "/pansearch/staging"
        stub._upgrade_mode = "largest"
        stub._offline_pending_lock = __import__("threading").RLock()
        stub._offline_download = _StubOfflineDownload(submit_hash=ODYSSEY_HASH)
        stub._serialize_mediainfo = lambda mediainfo: (
            mediainfo.to_dict() if mediainfo else {}
        )
        stub._serialize_pending_target_subscribe = (
            lambda subscribe: {"name": getattr(subscribe, "name", "")}
        )
        stub._resource_size_bytes = lambda value: 4671000000
        stub._add_offline_blacklist = lambda url, reason: None
        stub._notify_offline_pending_changed = lambda count: None
        self.pending = {}
        stub._get_data = lambda key: self.pending
        stub._save_offline_pending = (
            lambda value: self.pending.clear() or self.pending.update(value)
        )
        stub._offline_hash = lambda url: ODYSSEY_HASH
        stub._prepare_magnet_resource = lambda res, url: "奥德赛"
        stub._magnet_title_seasons = lambda res: set()
        stub._magnet_title_episodes = lambda res, season: set()
        stub._resource_preview_episodes = lambda res, season: {1}
        stub._format_episode_ranges = lambda values: "E01"
        stub._is_magnet_url = lambda url: True
        return stub

    def _resource(self):
        return {
            "title": "奥德赛",
            "size": "4.35GB",
            "source_sha1": "CC" * 20,
            "magnet_metadata": {
                "metadata_available": True,
                "display_name": "Odyssey.2024.mkv",
            },
        }

    def test_manual_channel_registers_unified_schema(self):
        resource = self._resource()
        handler = self._make_handler(resource)
        pending_key = handler._queue_magnet_package(
            resource, f"magnet:?xt=urn:btih:{ODYSSEY_HASH}",
            types.SimpleNamespace(id=153, name="奥德赛"), _StubMediaInfo(),
        )
        self.assertEqual(pending_key, ODYSSEY_PENDING_KEY)
        record = self.pending[pending_key]
        missing = UNIFIED_PENDING_KEYS - set(record)
        self.assertFalse(missing, f"manual channel missing keys: {missing}")
        # 手动通道此前缺失的定位字段已补齐并落值。
        self.assertEqual(record["info_hash"], ODYSSEY_HASH)
        self.assertEqual(record["source_sha1"], "CC" * 20)
        self.assertEqual(record["staging_dir"], "/pansearch/staging")
        self.assertEqual(record["staging_name"], "Odyssey.2024.mkv")
        self.assertEqual(record["file_size"], 4671000000)
        self.assertEqual(record["subscribe_id"], 153)
        self.assertEqual(record["status"], "下载中")
        self.assertTrue(record["history_ready"])
        # 磁力专属键经 current 原样保留。
        self.assertEqual(record["resource"], resource)
        self.assertIn("target_episodes", record)
        self.assertIn("upgrade_baseline", record)

    def test_manual_and_subscribe_channels_share_builder(self):
        source = (PLUGIN_ROOT / "handlers/sync/service.py").read_text(
            encoding="utf-8")
        self.assertIn("record = self._build_pending_record(", source)


# ---------------------------------------------------------------------------
# 2) 过期 pending 下一轮必检必处理（奥德赛最小复现）
# ---------------------------------------------------------------------------

POSTPROCESS_METHODS = {"_normalize_pending_item", "_due_pending_keys"}


def _manual_pending_record(**overrides):
    record = {
        "pending_key": ODYSSEY_PENDING_KEY,
        "task_type": "magnet",
        "task_id": ODYSSEY_HASH,
        "share_url": f"magnet:?xt=urn:btih:{ODYSSEY_HASH}",
        "cloud_dir": "/pansearch/staging",
        "file_name": "Odyssey.2024.mkv",
        "created_at": 1789039653.0,
        "next_check_at": 1789040294.0,
        "check_index": 0,
        "history_ready": True,
        "subscribe_id": 153,
    }
    record.update(overrides)
    return record


class TestExpiredPendingPickup(unittest.TestCase):
    """T4.2：next_check_at 过期后下一轮必被拾取，缺字段兜底一次告警。"""

    @classmethod
    def setUpClass(cls):
        cls._source, _ = extract_methods(
            "handlers/sync/postprocess.py", POSTPROCESS_METHODS
        )

    def _make_handler(self):
        class Stub:
            pass

        stub = Stub()
        self.logger = _RecordingLogger()
        namespace = {
            "logger": self.logger,
            "Any": typing.Any,
            "Dict": typing.Dict,
            "List": typing.List,
            "Optional": typing.Optional,
            "Set": typing.Set,
            "Tuple": typing.Tuple,
        }
        for name, source in self._source.items():
            _bind(stub, namespace, name, source)
        stub._pending_schema_warned = set()
        return stub

    def test_expired_pending_selected_and_fields_backfilled(self):
        record = _manual_pending_record()
        # 模拟上一轮异常退出残留租约：过期 next_check_at 必须能覆盖它。
        record["_monitor_until"] = NOW + 900
        pending = {ODYSSEY_PENDING_KEY: record}
        handler = self._make_handler()

        due = handler._due_pending_keys(pending, NOW)
        self.assertEqual(due, [ODYSSEY_PENDING_KEY])
        # 缺字段兜底补齐：key 存在 + 有可处理值。
        for field in ("info_hash", "source_sha1", "staging_dir",
                      "staging_name", "subscribe_id", "next_check_at"):
            self.assertIn(field, record)
        self.assertEqual(record["staging_dir"], record["cloud_dir"])
        self.assertEqual(record["staging_name"], record["file_name"])
        self.assertEqual(record["source_sha1"], "")
        self.assertEqual(record["info_hash"], "")
        # 每个键只告警一次。
        warnings = [m for m in self.logger.messages("warning")
                    if "字段缺失" in m]
        self.assertEqual(len(warnings), 1)
        self.assertIn(ODYSSEY_PENDING_KEY, warnings[0])
        handler._due_pending_keys(pending, NOW + 5)
        warnings = [m for m in self.logger.messages("warning")
                    if "字段缺失" in m]
        self.assertEqual(len(warnings), 1)

    def test_future_next_check_not_due_without_force(self):
        record = _manual_pending_record(next_check_at=NOW + 600)
        handler = self._make_handler()
        self.assertEqual(
            handler._due_pending_keys({ODYSSEY_PENDING_KEY: record}, NOW), []
        )
        self.assertEqual(
            handler._due_pending_keys(
                {ODYSSEY_PENDING_KEY: record}, NOW, force=True
            ),
            [ODYSSEY_PENDING_KEY],
        )

    def test_old_record_missing_history_ready_still_due(self):
        record = _manual_pending_record()
        record.pop("history_ready")
        record.pop("next_check_at")
        record.pop("staging_dir", None)
        pending = {ODYSSEY_PENDING_KEY: record}
        handler = self._make_handler()
        self.assertEqual(
            handler._due_pending_keys(pending, NOW), [ODYSSEY_PENDING_KEY]
        )
        self.assertTrue(record["history_ready"])
        self.assertEqual(record["next_check_at"], 0.0)


# ---------------------------------------------------------------------------
# 3) 存量核对：假成功反向回查降级 + 订阅进度修正
# ---------------------------------------------------------------------------

HISTORY_METHODS = {
    "reconcile_offline_history_backfill",
    "_offline_backfill_verdict",
    "_offline_backfill_record_key",
    "_offline_backfill_record_label",
    "_offline_backfill_notification_detail",
    "_offline_record_locate_fields",
    "_full_pan_search_available",
    "_offline_success_audit_candidate",
    "_offline_success_reverse_verdict",
    "_offline_fake_success_reason",
    "_downgrade_fake_success_record",
    "_refresh_deleted_subscribe_notes",
    "_history_episodes",
}


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
        self._directories = directories

    def resolve_directory(self, cloud_dir):
        if cloud_dir not in self._directories:
            return _DirLookup(checked=True, directory_id=None)
        return _DirLookup(checked=True, directory_id=cloud_dir)

    def list_directory(self, directory_id):
        checked, files = self._directories.get(directory_id, (True, []))
        return _DirListing(checked=checked, files=files)


class _CloudQueryStub:
    def __init__(self, recursive=True, hits=()):
        self._hits = list(hits)
        if recursive:
            self.list_files_recursive = self._list_files_recursive

    def _list_files_recursive(self, root, max_depth=None):
        return list(self._hits)


class _FakeSubscribe:
    def __init__(self, subscribe_id, name, note, total, start=1, media_type="电视剧"):
        self.id = subscribe_id
        self.name = name
        self.note = list(note)
        self.total_episode = total
        self.start_episode = start
        self.type = media_type


class _SubscribeOperStub:
    def __init__(self, updates):
        self._updates = updates

    def update(self, subscribe_id, values):
        self._updates.append((subscribe_id, dict(values)))
        return True


class _EnumValue:
    def __init__(self, value):
        self.value = value

    def __eq__(self, other):
        return self.value == other

    def __hash__(self):
        return hash(self.value)

    def __str__(self):
        return self.value


class _StubMediaType:
    TV = _EnumValue("电视剧")
    MOVIE = _EnumValue("电影")


class TestFakeSuccessReverseAudit(unittest.TestCase):
    """T4.3：离线假成功反查确认无文件后降级并修正订阅 note。"""

    @classmethod
    def setUpClass(cls):
        cls._source, cls._classmethods = extract_methods(
            "handlers/sync/history.py", HISTORY_METHODS
        )

    def _make_handler(self, history, pending=None, recursive=True, hits=()):
        class Stub:
            _OFFLINE_SUCCESS_AUDIT_RESOURCE_TYPES = {"magnet", "ed2k", "offline"}

        stub = Stub()
        state = {"history": history, "offline_pending_tasks": pending or {}}
        calls = {"platform": [], "notified": [], "history_changed": 0}
        self.logger = _RecordingLogger()
        self.subscribe_updates = []
        self.subscribes = []
        namespace = {
            "logger": self.logger,
            "time": time,
            "copy": copy,
            "Any": typing.Any,
            "Dict": typing.Dict,
            "List": typing.List,
            "Optional": typing.Optional,
            "Set": typing.Set,
            "Tuple": typing.Tuple,
            "MediaType": _StubMediaType,
            "SubscribeOper": lambda *a, **k: _SubscribeOperStub(
                self.subscribe_updates
            ),
            "list_subscribes_by_tmdb_id": (
                lambda oper, tmdb_id, season: list(self.subscribes)
            ),
        }
        for name, source in self._source.items():
            if name in self._classmethods:
                exec(source, namespace)
                fn = namespace[name]
                stub.__dict__[name] = (
                    lambda record, _fn=fn, _cls=Stub: _fn(_cls, record)
                )
            else:
                _bind(stub, namespace, name, source)
        stub._get_data = lambda key: state.get(key)
        stub._save_data = lambda key, value: state.__setitem__(key, value)
        stub._offline_pending_lock = __import__("threading").RLock()
        stub._OFFLINE_PENDING_KEY = "offline_pending_tasks"
        stub._OFFLINE_BACKFILL_STATUSES = {"失败", "处理中"}
        stub._OFFLINE_BACKFILL_RECHECK_SECONDS = 3600
        stub._OFFLINE_BACKFILL_CACHE_MAXSIZE = 500
        stub._cloud_directories = _CloudDirectoriesStub({})
        stub._cloud_query = _CloudQueryStub(recursive=recursive, hits=hits)
        stub._full_pan_locate_file = (
            (lambda record, file_name="", source_sha1="": None)
            if not hits else None
        )
        if hits:
            # 命中时返回 (条目, 目录)，模拟终审全盘检索能力。
            def _locate(record, file_name="", source_sha1=""):
                return (_FakeFile(file_name or "hit.mkv"), "/found")
            stub._full_pan_locate_file = _locate
        stub._record_platform_transfer_histories = (
            lambda records: calls["platform"].extend(records)
        )
        stub._send_finalized_batch = (
            lambda details: calls["notified"].extend(details)
        )
        stub._history_changed = lambda: calls.__setitem__(
            "history_changed", calls["history_changed"] + 1
        )
        stub._is_upgrade_history = lambda record: bool(record.get("upgrade"))
        stub.calls = calls
        stub.state = state
        return stub

    def _fake_success_record(self, **overrides):
        record = {
            "title": "完美世界",
            "year": "2024",
            "type": "电视剧",
            "status": "成功",
            "tmdb_id": 12345,
            "season": 2,
            "episode": 286,
            "share_url": "ed2k://|file|Perfect.World.E286.mkv|1267015352|"
                         + "AA" * 16 + "|/",
            "resource_type": "ed2k",
            "file_name": "Perfect.World.E286.mkv",
            "source_file_name": "Perfect.World.E286.mkv",
            "cloud_dir": "/media/tv",
            "staging_dir": "/pansearch/staging",
            "source_sha1": "CC" * 20,
        }
        record.update(overrides)
        return record

    def _empty_dirs(self):
        return _CloudDirectoriesStub({
            "/pansearch/staging": (True, []),
            "/media/tv": (True, []),
        })

    def test_fake_success_downgraded_and_note_fixed(self):
        record = self._fake_success_record()
        handler = self._make_handler([record])
        handler._cloud_directories = self._empty_dirs()
        self.subscribes = [
            _FakeSubscribe(153, "完美世界", [284, 285, 286], 286)
        ]
        result = handler.reconcile_offline_history_backfill()
        # 降级不计入回填成功数。
        self.assertEqual(result, 0)
        self.assertEqual(record["status"], "失败")
        self.assertIn("假成功", record["failure_reason"])
        self.assertIn("offline_downgraded_at", record)
        # INFO 降级日志含记录标识与原因（v1.5.3 T4 风格）。
        downgrade_logs = [
            message for message in self.logger.messages("info")
            if "反向回查降级" in message
        ]
        self.assertTrue(downgrade_logs)
        self.assertIn("完美世界 S02E286", downgrade_logs[0])
        # 订阅进度 note 修正：E286 被移除，缺集数重算。
        self.assertTrue(self.subscribe_updates)
        subscribe_id, values = self.subscribe_updates[-1]
        self.assertEqual(subscribe_id, 153)
        self.assertEqual(values["note"], [284, 285])
        self.assertEqual(values["lack_episode"], 284)

    def test_confirmed_success_marked_verified(self):
        record = self._fake_success_record()
        handler = self._make_handler([record])
        handler._cloud_directories = _CloudDirectoriesStub({
            "/pansearch/staging": (
                True, [_FakeFile("Perfect.World.E286.mkv")]),
            "/media/tv": (True, []),
        })
        handler.reconcile_offline_history_backfill()
        self.assertEqual(record["status"], "成功")
        self.assertIn("offline_verified_at", record)
        self.assertFalse(self.subscribe_updates)

    def test_defer_when_full_pan_search_unavailable(self):
        record = self._fake_success_record()
        handler = self._make_handler([record], recursive=False)
        handler._cloud_directories = self._empty_dirs()
        # 无递归能力：证据不足，绝不误降级。
        handler._full_pan_locate_file = None
        handler.reconcile_offline_history_backfill()
        self.assertEqual(record["status"], "成功")
        self.assertNotIn("offline_verified_at", record)
        self.assertFalse(self.subscribe_updates)

    def test_plain_share_success_not_audited(self):
        record = self._fake_success_record(
            share_url="https://115.com/s/abcdefg", resource_type="share",
        )
        handler = self._make_handler([record])
        handler._cloud_directories = self._empty_dirs()
        handler.reconcile_offline_history_backfill()
        self.assertEqual(record["status"], "成功")

    def test_old_success_record_without_locate_fields_defers(self):
        record = {
            "title": "旧记录", "type": "电影", "status": "成功",
            "share_url": "magnet:?xt=urn:btih:" + "AB" * 20,
        }
        record["resource_type"] = "magnet"
        handler = self._make_handler([record])
        handler._cloud_directories = _CloudDirectoriesStub({})
        handler.reconcile_offline_history_backfill()
        # 缺 field_name/cloud_dir/source_sha1：无法定位 -> defer，不降级。
        self.assertEqual(record["status"], "成功")
        self.assertNotIn("offline_downgraded_at", record)

    def test_audit_candidate_predicate(self):
        handler = self._make_handler([])
        predicate = handler._offline_success_audit_candidate
        self.assertTrue(predicate(self._fake_success_record()))
        self.assertTrue(predicate(self._fake_success_record(
            status="成功", resource_type="share",
            share_url=f"magnet:?xt=urn:btih:{ODYSSEY_HASH}",
        )))
        self.assertTrue(predicate(self._fake_success_record(
            resource_type="share",
            share_url="https://115.com/s/x", finalize_key="magnet:x:1",
        )))
        self.assertFalse(predicate(self._fake_success_record(
            status="失败", resource_type="ed2k",
        )))
        self.assertFalse(predicate(self._fake_success_record(
            status="成功", resource_type="share",
            share_url="https://115.com/s/abcdefg", finalize_key=None,
        )))


if __name__ == "__main__":
    unittest.main()
