# -*- coding: utf-8 -*-
"""PanSearch v1.5.3 T2 115"文件已存在"(4200045)视同转存成功。

被测模块依赖 app.*，无法直接导入；沿用 test_wk1_logic.py 的 ast 函数
抽取方式，把 drive/p115/share.py 的目标方法源码 exec 到桩宿主上验证
纯逻辑。覆盖四类分支：_do_transfer 按错误码 4200045 判定已存在（错误
文案不含"已存在"字样也必须命中）；transfer_file 的 None 分支无论
target_name/source_sha1 是否可用一律返回成功；transfer_files_batch
整页已存在与预检已存在均计入 success_ids 不进 failed_ids；整包
transfer_share 的 None 返回不再被当成失败。另含 grep 式源码断言，
确保失败分支不再覆盖"已存在"场景且全部走 INFO 日志。
"""

import ast
import time
import types
import typing
import unittest
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
SHARE_REL_PATH = "drive/p115/share.py"


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


def extract_static_methods(rel_path, names):
    """抽取 @staticmethod：exec 后不绑定 self，直接作为普通函数使用。"""
    source = (PLUGIN_ROOT / rel_path).read_text(encoding="utf-8")
    tree = ast.parse(source)
    found = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for child in node.body:
                if isinstance(child, ast.FunctionDef) and child.name in names:
                    for decorator in child.decorator_list:
                        if getattr(decorator, "id", "") == "staticmethod":
                            found[child.name] = ast.get_source_segment(
                                source, child
                            )
    missing = set(names) - set(found)
    assert not missing, "missing staticmethods: %s" % missing
    return found


def _bind(stub, namespace, name):
    stub.__dict__[name] = types.MethodType(namespace[name], stub)


class _RecordingLogger:
    """按级别记录日志消息，供断言 INFO/禁止 WARNING 使用。"""

    def __init__(self):
        self.records = []

    def _record(self, level, msg):
        self.records.append((level, str(msg)))

    def debug(self, msg, *args, **kwargs):
        self._record("debug", msg)

    def info(self, msg, *args, **kwargs):
        self._record("info", msg)

    def warning(self, msg, *args, **kwargs):
        self._record("warning", msg)

    def error(self, msg, *args, **kwargs):
        self._record("error", msg)

    def infos(self):
        return [msg for level, msg in self.records if level == "info"]

    def warnings(self):
        return [msg for level, msg in self.records if level == "warning"]

    def errors(self):
        return [msg for level, msg in self.records if level == "error"]


_TYPING_NAMESPACE = {
    "Dict": typing.Dict,
    "List": typing.List,
    "Tuple": typing.Tuple,
    "Any": typing.Any,
    "Optional": typing.Optional,
}


class _ShareStub:
    """承载抽取出的 ShareService 方法的桩宿主。"""

    def __init__(self, logger):
        self.logger = logger
        self.client = object()

    @staticmethod
    def build(rel_path, method_names, static_names=(), logger=None):
        stub = _ShareStub(logger or _RecordingLogger())
        namespace = dict(_TYPING_NAMESPACE)
        namespace["logger"] = stub.logger
        namespace["time"] = time
        namespace["DRIVE_RETRY_EXCEPTIONS"] = ()
        # 静态方法内引用类常量时以类名解析，这里注入同值桩类。
        namespace["ShareService"] = type("ShareService", (), {
            "EXISTS_ERRNO": 4200045,
            "DEAD_LINK_ERRNO": 4100018,
        })
        for name, source in extract_methods(rel_path, method_names).items():
            exec(source, namespace)
            _bind(stub, namespace, name)
        for name, source in extract_static_methods(rel_path, static_names).items():
            exec(source, namespace)
            stub.__dict__[name] = namespace[name]
        return stub


class TestExistsErrorCodeDetection(unittest.TestCase):
    """T2.1: is_exists_error 纯函数 + 4200045 常量登记。"""

    def test_source_registers_exists_errno(self):
        source = (PLUGIN_ROOT / SHARE_REL_PATH).read_text(encoding="utf-8")
        self.assertIn("EXISTS_ERRNO = 4200045", source)

    def test_is_exists_error(self):
        stub = _ShareStub.build(
            SHARE_REL_PATH, [], static_names={"is_exists_error"}
        )
        self.assertTrue(stub.is_exists_error(4200045))
        self.assertTrue(stub.is_exists_error("4200045"))
        self.assertFalse(stub.is_exists_error(4100018))
        self.assertFalse(stub.is_exists_error(None))
        self.assertFalse(stub.is_exists_error("abc"))
        self.assertFalse(stub.is_exists_error(0))


class TestDoTransferExistsBranch(unittest.TestCase):
    """T2.2: _do_transfer 对 4200045 返回 None（存在哨兵）而非 False。"""

    def _make_handler(self, resp):
        stub = _ShareStub.build(
            SHARE_REL_PATH,
            {"_do_transfer"},
            static_names={"is_exists_error", "is_deterministic_dead_link_error"},
        )
        stub.DEFAULT_MAX_RETRIES = 1

        def share_receive(payload, **kwargs):
            return resp

        stub.client = types.SimpleNamespace(share_receive=share_receive)
        stub._rate_limited_call = lambda func, *args, **kwargs: func(
            *args, **kwargs
        )
        stub._ios_request_kwargs = lambda **kwargs: {}
        stub._note_dead_link_failure = lambda *args, **kwargs: None
        return stub

    def _run(self, stub):
        return stub._do_transfer(
            share_code="sc", receive_code="rc", file_id="123",
            parent_id=5, save_path="/media", share_url="https://115.com/s/sc",
        )

    def test_errno_4200045_without_keyword_is_exists(self):
        # 生产事故：错误码 4200045 但文案不含"已存在/重复"。
        stub = self._make_handler(
            {"state": False, "error": "系统繁忙", "errno": 4200045}
        )
        result = self._run(stub)
        self.assertIsNone(result)  # None = 已存在哨兵，绝不是 False
        self.assertTrue(any(
            "视同转存成功" in msg for msg in stub.logger.infos()
        ))
        self.assertEqual(stub.logger.warnings(), [])
        self.assertEqual(stub.logger.errors(), [])

    def test_message_contains_exists_keyword(self):
        stub = self._make_handler({"state": False, "error": "文件已存在", "errno": 0})
        self.assertIsNone(self._run(stub))
        self.assertTrue(any(
            "视同转存成功" in msg for msg in stub.logger.infos()
        ))

    def test_message_contains_duplicate_keyword(self):
        stub = self._make_handler({"state": False, "error": "重复转存", "errno": 0})
        self.assertIsNone(self._run(stub))

    def test_other_error_still_fails(self):
        stub = self._make_handler(
            {"state": False, "error": "链接已过期", "errno": 4100018}
        )
        self.assertIs(self._run(stub), False)
        # 真实失败保持 ERROR 日志，且绝不误报"视同转存成功"。
        self.assertTrue(stub.logger.errors())
        self.assertFalse(any(
            "视同转存成功" in msg for msg in stub.logger.infos()
        ))


class TestTransferFileExistsBranch(unittest.TestCase):
    """T2.3: transfer_file 的"已存在"结果一律成功且打 INFO。"""

    def _make_handler(self, do_transfer=None, precheck_existing=None,
                      rename_ok=False):
        stub = _ShareStub.build(SHARE_REL_PATH, {"transfer_file"})
        stub.is_offline_url = lambda url: False
        stub.rename_files_by_sha1_batch = (
            lambda *args, **kwargs: (precheck_existing or {}, [])
        )
        stub.extract_share_info = lambda url: {
            "share_code": "sc", "receive_code": "rc",
        }
        stub.get_pid_by_path = lambda path, mkdir=False: 5
        stub._do_transfer = do_transfer or (lambda **kwargs: None)
        stub.rename_file_by_sha1 = lambda *args, **kwargs: rename_ok
        return stub

    def test_precheck_existing_is_silent_success_no_more(self):
        # 预检发现目标已在暂存目录：成功且必须有 INFO，不允许静默。
        stub = self._make_handler(precheck_existing={"123"})
        result = stub.transfer_file(
            "https://115.com/s/sc", "123", "/media", target_name="A.mkv",
            source_sha1="a" * 40,
        )
        self.assertTrue(result)
        self.assertIn("文件已存在，视同转存成功：A.mkv", stub.logger.infos())

    def test_exists_without_sha1_is_success(self):
        # 事故分支：单文件渠道缺 sha1 时旧代码 return False。
        stub = self._make_handler()
        result = stub.transfer_file(
            "https://115.com/s/sc", "123", "/media", target_name="A.mkv",
            source_sha1="",
        )
        self.assertTrue(result)
        self.assertTrue(any(
            "文件已存在，视同转存成功：A.mkv" in msg
            for msg in stub.logger.infos()
        ))
        self.assertEqual(stub.logger.warnings(), [])
        self.assertEqual(stub.logger.errors(), [])

    def test_exists_without_target_name_is_success(self):
        # 历史重试渠道不传 target_name：同样必须成功。
        stub = self._make_handler()
        result = stub.transfer_file(
            "https://115.com/s/sc", "123", "/media",
            target_name=None, source_sha1="",
        )
        self.assertTrue(result)
        self.assertTrue(any(
            "文件已存在，视同转存成功：123" in msg
            for msg in stub.logger.infos()
        ))

    def test_exists_and_rename_succeeds_is_success(self):
        stub = self._make_handler(rename_ok=True)
        self.assertTrue(stub.transfer_file(
            "https://115.com/s/sc", "123", "/media", target_name="A.mkv",
            source_sha1="a" * 40,
        ))
        self.assertTrue(any(
            "文件已存在，视同转存成功：A.mkv" in msg
            for msg in stub.logger.infos()
        ))

    def test_real_failure_still_fails(self):
        stub = self._make_handler(do_transfer=lambda **kwargs: False)
        result = stub.transfer_file(
            "https://115.com/s/sc", "123", "/media", target_name="A.mkv",
            source_sha1="a" * 40,
        )
        self.assertFalse(result)
        self.assertFalse(any(
            "视同转存成功" in msg for msg in stub.logger.infos()
        ))


class TestTransferFilesBatchExistsBranch(unittest.TestCase):
    """T2.4: 批量转存整页 4200045 / 预检已存在均计入成功侧。"""

    def _make_handler(self, do_transfer=None, existing=None):
        stub = _ShareStub.build(SHARE_REL_PATH, {"transfer_files_batch"})
        stub.is_offline_url = lambda url: False
        stub.SHARE_TRANSFER_PAGE_SIZE = 115

        # 语义与真实实现一致：existing = 已在目标目录的文件 ID；
        # unresolved = 预检过但尚未可见、仍需转存的文件 ID。
        def rename_files_by_sha1_batch(save_path, items, ids, **kwargs):
            existing_set = set(existing or [])
            return (
                existing_set,
                [str(fid) for fid in ids if str(fid) not in existing_set],
            )

        stub.rename_files_by_sha1_batch = rename_files_by_sha1_batch
        stub.extract_share_info = lambda url: {
            "share_code": "sc", "receive_code": "rc",
        }
        stub.get_pid_by_path = lambda path, mkdir=False: 5
        transfer_calls = []
        if do_transfer is None:
            def do_transfer(**kwargs):
                transfer_calls.append(str(kwargs.get("file_id") or ""))
                return None
        else:
            original = do_transfer

            def do_transfer(**kwargs):  # noqa: F811 记录调用后转发原桩
                transfer_calls.append(str(kwargs.get("file_id") or ""))
                return original(**kwargs)

        stub._do_transfer = do_transfer
        stub.transfer_calls = transfer_calls
        return stub

    def _rename_items(self):
        return {
            "1": {"sha1": "a" * 40, "target_name": "E01.mkv", "url": "u1"},
            "2": {"sha1": "b" * 40, "target_name": "E02.mkv", "url": "u2"},
        }

    def test_whole_page_exists_counts_as_success(self):
        # 整页 4200045：全部计入 success_ids，failed_ids 必须为空。
        stub = self._make_handler()
        success_ids, failed_ids = stub.transfer_files_batch(
            "https://115.com/s/sc", ["1", "2"], "/media",
            rename_items=self._rename_items(),
        )
        self.assertEqual(success_ids, ["1", "2"])
        self.assertEqual(failed_ids, [])
        page_info = [msg for msg in stub.logger.infos() if "视同转存成功" in msg]
        self.assertTrue(page_info)
        self.assertTrue(any("E01.mkv" in msg and "E02.mkv" in msg
                            for msg in page_info))
        self.assertEqual(stub.logger.warnings(), [])

    def test_precheck_existing_counts_into_success(self):
        # 多源重复命中：目标已在暂存目录的文件直接复用，不重复转存也不失败。
        stub = self._make_handler(existing={"1"})
        success_ids, failed_ids = stub.transfer_files_batch(
            "https://115.com/s/sc", ["1", "2"], "/media",
            rename_items=self._rename_items(),
        )
        self.assertIn("1", success_ids)
        self.assertIn("2", success_ids)
        self.assertEqual(failed_ids, [])
        # "1" 已存在必须复用，不得再次进入转存页；"2" 正常转存。
        self.assertEqual(stub.transfer_calls, ["2"])
        self.assertTrue(any(
            "文件已存在，视同转存成功：E01.mkv" in msg
            for msg in stub.logger.infos()
        ))

    def test_real_page_failure_still_records_failed(self):
        stub = self._make_handler(do_transfer=lambda **kwargs: False)
        success_ids, failed_ids = stub.transfer_files_batch(
            "https://115.com/s/sc", ["1", "2"], "/media",
            rename_items=self._rename_items(),
        )
        self.assertEqual(success_ids, [])
        self.assertEqual(failed_ids, ["1", "2"])


class TestTransferShareExistsBranch(unittest.TestCase):
    """T2.5: 整包转存的 None（已存在）不再被当成失败。"""

    def test_none_result_is_success(self):
        source = (PLUGIN_ROOT / SHARE_REL_PATH).read_text(encoding="utf-8")
        # transfer_share 依赖较多，直接做 grep 式断言：
        self.assertIn(") is not False", source)
        # None 分支必须显式 return True，禁止再出现静默 return False。
        transfer_file_seg = extract_methods(
            SHARE_REL_PATH, {"transfer_file"}
        )["transfer_file"]
        none_branch = transfer_file_seg.split("if success is None:", 1)[1]
        none_branch = none_branch.split("if success and target_name:", 1)[0]
        self.assertIn("return True", none_branch)
        self.assertNotIn("return False", none_branch)
        self.assertIn("视同转存成功", none_branch)


class TestExistsSuccessSourceGuards(unittest.TestCase):
    """T2.6: grep 式源码断言——失败路径不再覆盖"已存在"。"""

    def _source(self):
        return (PLUGIN_ROOT / SHARE_REL_PATH).read_text(encoding="utf-8")

    def test_no_warning_logs_for_exists_case(self):
        source = self._source()
        self.assertNotIn("等待目标目录复核", source)
        self.assertNotIn(
            "第 {page_num} 页返回文件已存在，目录复核后确认", source
        )
        self.assertNotIn("暂存目录已存在目标文件，跳过重复转存", source)

    def test_exists_detection_covers_error_code(self):
        source = self._source()
        self.assertIn("is_exists_error(error_code)", source)
        self.assertIn("EXISTS_ERRNO = 4200045", source)

    def test_consumer_failure_paths_untouched_for_real_failures(self):
        # 真实失败路径（页面失败告警、_transfer_file 拉黑）必须保留。
        share_source = self._source()
        self.assertIn(
            "第 {page_num} 页批量转存失败，停止逐文件重试以避免放大风控",
            share_source,
        )
        service_source = (
            PLUGIN_ROOT / "handlers/sync/service.py"
        ).read_text(encoding="utf-8")
        self.assertIn("if not transferred:", service_source)

    def test_exists_success_counts_into_transfer_statistics(self):
        # 成功侧统计沿用既有 accounting：television/movie 由 success 标志
        # 驱动 transferred_count 与 success_episodes，不存在独立 skipped 状态。
        television_source = (
            PLUGIN_ROOT / "handlers/sync/television.py"
        ).read_text(encoding="utf-8")
        self.assertIn(
            "success_episodes.append(episode)", television_source
        )
        movie_source = (
            PLUGIN_ROOT / "handlers/sync/movie.py"
        ).read_text(encoding="utf-8")
        self.assertIn("movie_transferred = True", movie_source)


if __name__ == "__main__":
    unittest.main()
