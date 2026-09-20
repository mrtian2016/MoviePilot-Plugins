# -*- coding: utf-8 -*-
"""PanSearch v1.5.4 T2 ED2K/Magnet 提交成功≠下载成功。

事故（完美世界 E286，DB id149）：dian115 消耗积分解锁 ED2K 后 39 秒，
history 直接写"成功"、订阅进度写 286、转送完成通知发送，但 115 网盘
实际没有文件。云下载类资源（ed2k/magnet）提交成功只允许写"下载中"，
并且必须登记 offline_pending（含 info_hash/ed2k 指纹、cloud_dir、
file_name、source_sha1、subscribe_id 等终审字段），"成功"只能由后处理
实证文件落盘后回写。

被测模块依赖 app.*，无法直接导入；沿用 tests/ 的 ast 函数抽取方式，
把目标方法源码 exec 到桩宿主上验证纯逻辑。
"""

import ast
import re
import threading
import types
import unittest
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent

ED2K = (
    "ed2k://|file|Perfect.World.286.1080p.mp4|1267015352|"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA|/"
)
MAGNET = "magnet:?xt=urn:btih:BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB&dn=x"
SHARE_URL = "https://115.com/s/abcdefg?password=1234"
DIAN115_PAGE = "https://m.dian115.com/resource/tv/123"


def extract_methods(rel_path, names, class_name=None):
    """抽取方法源码并记录 staticmethod / classmethod 装饰器。"""
    source = (PLUGIN_ROOT / rel_path).read_text(encoding="utf-8")
    tree = ast.parse(source)
    found = {}
    decorators = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        if class_name is not None and node.name != class_name:
            continue
        for child in node.body:
            if isinstance(child, ast.FunctionDef) and child.name in names:
                found[child.name] = ast.get_source_segment(source, child)
                kind = ""
                for dec in child.decorator_list:
                    if isinstance(dec, ast.Name) and dec.id in (
                            "staticmethod", "classmethod"):
                        kind = dec.id
                    elif isinstance(dec, ast.Attribute) and dec.attr in (
                            "staticmethod", "classmethod"):
                        kind = dec.attr
                decorators[child.name] = kind
    missing = set(names) - set(found)
    assert not missing, "missing methods: %s" % missing
    return found, decorators


def extract_class_attrs(rel_path, class_name, names):
    """抽取类级字面量/表达式赋值（如正则与字段名元组）。"""
    source = (PLUGIN_ROOT / rel_path).read_text(encoding="utf-8")
    tree = ast.parse(source)
    attrs = {}
    for node in tree.body:
        if not isinstance(node, ast.ClassDef) or node.name != class_name:
            continue
        for child in node.body:
            if not isinstance(child, ast.Assign):
                continue
            for target in child.targets:
                if isinstance(target, ast.Name) and target.id in names:
                    attrs[target.id] = eval(
                        compile(ast.Expression(child.value), "<attr>", "eval"),
                        {"re": re},
                    )
    missing = set(names) - set(attrs)
    assert not missing, "missing class attrs: %s" % missing
    return attrs


def bind_method(stub, namespace, name, kind=""):
    exec(namespace[name], namespace)
    if kind == "staticmethod":
        stub.__dict__[name] = namespace[name]
    else:
        stub.__dict__[name] = types.MethodType(namespace[name], stub)


def make_class(namespace, methods, kinds, attrs=None):
    """构造只承载 classmethod 的桩类（含类级字面量属性）。"""
    holder = type("Stub", (object,), dict(attrs or {}))
    for name in methods:
        exec(namespace[name], namespace)
        setattr(holder, name, classmethod(namespace[name]))
    return holder


class _StubLogger:
    def debug(self, *args, **kwargs):
        pass

    info = warning = error = debug


class _StubOffline:
    """离线链接判定桩，等价 drive/p115/offline.py 的 URL 语义。"""

    def is_offline_url(self, url):
        return self.is_ed2k_url(url) or self.is_magnet_url(url)

    def is_ed2k_url(self, url):
        return isinstance(url, str) and url.lstrip().lower().startswith("ed2k://")

    def is_magnet_url(self, url):
        return isinstance(url, str) and url.lstrip().lower().startswith("magnet:?")

    def parse_magnet_link(self, url, fetch_metadata=False):
        return None


# --------------------------------------------------------------------------
# 1) history 状态：云下载类资源提交成功不得写"成功"
# --------------------------------------------------------------------------

RESOURCE_METHODS = [
    "_is_offline_resource_meta",
    "_is_cloud_download_resource",
    "_transfer_history_status",
]


class TestEd2kHistoryStatus(unittest.TestCase):
    """T2.1 只凭单一 share_url 判定会漏判，进而在提交阶段落"成功"。"""

    def setUp(self):
        ns, kinds = extract_methods(
            "handlers/sync/resources.py", RESOURCE_METHODS
        )
        exec("from typing import Any, Mapping", ns)
        ns["logger"] = _StubLogger()
        self.stub = types.SimpleNamespace()
        self.stub._offline_download = _StubOffline()
        self.stub._is_offline_url = types.MethodType(
            lambda self_, url: self_._offline_download.is_offline_url(url),
            self.stub,
        )
        for name in RESOURCE_METHODS:
            bind_method(self.stub, ns, name, kinds.get(name, ""))

    def test_ed2k_and_magnet_submit_are_downloading(self):
        for url in (ED2K, MAGNET):
            status = self.stub._transfer_history_status(True, url)
            self.assertEqual(status, "下载中")
            self.assertNotEqual(status, "成功")

    def test_dian115_page_url_with_ed2k_metadata_is_not_success(self):
        """事故复现：链接字段不是 ed2k，但资源元数据声明 ed2k。"""
        status = self.stub._transfer_history_status(
            True, DIAN115_PAGE, resource={"resource_type": "ed2k"}
        )
        self.assertEqual(status, "下载中")

    def test_per_file_offline_url_is_not_success(self):
        status = self.stub._transfer_history_status(
            True, DIAN115_PAGE, ED2K, resource={}
        )
        self.assertEqual(status, "下载中")

    def test_offline_share_kind_meta_is_not_success(self):
        self.assertTrue(
            self.stub._is_cloud_download_resource(
                DIAN115_PAGE, resource={"share_kind": "offline"}
            )
        )

    def test_plain_share_still_success(self):
        self.assertEqual(
            self.stub._transfer_history_status(
                True, SHARE_URL, resource={"resource_type": "115"}
            ),
            "成功",
        )

    def test_failure_is_still_failure(self):
        self.assertEqual(self.stub._transfer_history_status(False, ED2K), "失败")


# --------------------------------------------------------------------------
# 2) pending 登记：ED2K 提交成功必须落 offline_pending（与 Magnet 同套 schema）
# --------------------------------------------------------------------------

SERVICE_METHODS = [
    "_offline_hash",
    "_pending_identity",
    "_finalize_source_identity",
    "_build_pending_record",
    "_generate_or_queue_strm",
    "_queue_file_finalize",
]

BIND_STATICS = {"_finalize_source_identity"}


class TestEd2kPendingRegistration(unittest.TestCase):
    """T2.2 mock ED2K 提交成功：pending 必须登记且字段齐全。"""

    def setUp(self):
        ns, kinds = extract_methods("handlers/sync/service.py", SERVICE_METHODS)
        exec(
            "import re\n"
            "import hashlib\n"
            "import copy\n"
            "import time\n"
            "from typing import Any, Dict, List, Optional, Tuple\n"
            "from pathlib import Path\n"
            "class MediaInfo: pass\n"
            "class CloudFile: pass\n",
            ns,
        )
        ns["logger"] = _StubLogger()
        ns["MediaType"] = types.SimpleNamespace(TV="tv", MOVIE="movie")

        self.store = {}
        stub = types.SimpleNamespace()
        stub._OFFLINE_PENDING_KEY = "pending_offline_strm"
        stub._OFFLINE_CHECK_DELAYS = (10, 20, 40, 60, 120, 300)
        stub._upgrade_mode = "largest"
        stub._offline_pending_lock = threading.RLock()
        stub._offline_download = _StubOffline()
        stub._cloud_drive = types.SimpleNamespace(key="115")
        stub._get_data = lambda key: self.store.get(key)
        stub._save_data = lambda key, value: self.store.__setitem__(key, value)
        stub._save_offline_pending = lambda pending: self.store.__setitem__(
            stub._OFFLINE_PENDING_KEY, pending
        )
        stub._notify_offline_pending_changed = lambda count: None
        stub._serialize_mediainfo = lambda mediainfo: {
            "title": getattr(mediainfo, "title", ""),
        }
        stub._is_offline_url = types.MethodType(
            lambda self_, url: self_._offline_download.is_offline_url(url),
            stub,
        )
        stub._is_magnet_url = types.MethodType(
            lambda self_, url: self_._offline_download.is_magnet_url(url),
            stub,
        )
        for name in SERVICE_METHODS:
            kind = "staticmethod" if name in BIND_STATICS else kinds.get(name, "")
            bind_method(stub, ns, name, kind)
        self.stub = stub

    def _mediainfo(self):
        media = ns_media = types.SimpleNamespace()
        ns_media.title = "完美世界"
        ns_media.type = "tv"
        return ns_media

    def test_identity_uses_ed2k_hash_fingerprint(self):
        pending_key, task_type, info_hash = self.stub._pending_identity(
            ED2K, "/staging", "完美世界 - S01E286.mp4", "IDENT"
        )
        self.assertEqual(task_type, "ed2k")
        self.assertEqual(info_hash, "A" * 32)
        self.assertEqual(pending_key, "A" * 32)

    def test_mock_submit_registers_schema_complete_pending(self):
        pend_key = self.stub._queue_file_finalize(
            share_url=ED2K,
            cloud_dir="/media/完美世界/Season 1",
            file_name="完美世界 - S01E286.mp4",
            mediainfo=self._mediainfo(),
            source_sha1="",
            file_size=1267015352,
            subscribe_id=286,
            success_episodes=[286],
            season=1,
            staging_dir="/staging",
            staging_name="Perfect.World.286.1080p.mp4",
        )
        self.assertTrue(pend_key)
        pending = self.store["pending_offline_strm"]
        record = pending[pend_key]
        self.assertEqual(record["task_type"], "ed2k")
        self.assertEqual(record["task_id"], "A" * 32)
        self.assertEqual(record["info_hash"], "A" * 32)
        for field in (
                "pending_key", "share_url", "cloud_dir", "file_name",
                "source_sha1", "subscribe_id", "staging_dir", "staging_name",
                "file_size", "created_at", "next_check_at", "mediainfo",
        ):
            self.assertIn(field, record, field)
        self.assertEqual(record["cloud_dir"], "/media/完美世界/Season 1")
        self.assertEqual(record["file_name"], "完美世界 - S01E286.mp4")
        self.assertEqual(record["subscribe_id"], 286)
        self.assertEqual(record["share_url"], ED2K)

    def test_old_pending_record_without_new_fields_is_tolerated(self):
        """旧 pending 载荷缺 info_hash 等新键时必须自动兼容。"""
        old_key = "A" * 32
        self.store["pending_offline_strm"] = {
            old_key: {
                "pending_key": old_key,
                "task_type": "ed2k",
                "task_id": old_key,
                "share_url": ED2K,
                "cloud_dir": "/staging",
                "file_name": "完美世界 - S01E286.mp4",
            }
        }
        pend_key = self.stub._queue_file_finalize(
            share_url=ED2K,
            cloud_dir="/staging",
            file_name="完美世界 - S01E286.mp4",
            mediainfo=self._mediainfo(),
            subscribe_id=286,
            staging_dir="/staging",
        )
        self.assertEqual(pend_key, old_key)
        record = self.store["pending_offline_strm"][old_key]
        self.assertIn("info_hash", record)
        self.assertEqual(record["info_hash"], old_key)
        self.assertEqual(record["staging_name"], "完美世界 - S01E286.mp4")

    def test_offline_strm_shortcut_cannot_skip_pending(self):
        """ED2K/Magnet 不得走 STRM 快速通道直接落终态成功。"""
        strm_calls = {"count": 0}
        queue_calls = {"count": 0}

        def fake_generate_strm(*args, **kwargs):
            strm_calls["count"] += 1
            return Path("/tmp/should-not-be-used.strm")

        def fake_queue(**kwargs):
            queue_calls["count"] += 1
            return "PENDING-KEY"

        self.stub._generate_strm = fake_generate_strm
        self.stub._scrape_metadata = lambda *a, **k: None
        self.stub._queue_file_finalize = fake_queue

        strm_path, pending_key = self.stub._generate_or_queue_strm(
            ED2K,
            "/media",
            "完美世界 - S01E286.mp4",
            self._mediainfo(),
            staging_dir="",
        )
        self.assertIsNone(strm_path)
        self.assertEqual(pending_key, "PENDING-KEY")
        self.assertEqual(strm_calls["count"], 0)
        self.assertEqual(queue_calls["count"], 1)

        # 普通分享保持原行为：仍可直接生成 STRM。
        strm_path, pending_key = self.stub._generate_or_queue_strm(
            SHARE_URL,
            "/media",
            "普通分享.mp4",
            self._mediainfo(),
            staging_dir="",
        )
        # 路径分隔符在 Windows 上为反斜杠，统一归一后再比较
        self.assertEqual(
            str(strm_path).replace("\\", "/"), "/tmp/should-not-be-used.strm"
        )
        self.assertEqual(pending_key, "")
        self.assertEqual(strm_calls["count"], 1)


# --------------------------------------------------------------------------
# 3) dian115 离线资源类型/链接必须识别为离线，不能走普通分享"提交即成功"
# --------------------------------------------------------------------------

DIAN115_METHODS = [
    "_offline_url_type",
    "_offline_link_candidates",
    "_resource_type",
    "_share_url",
]


class TestDian115OfflineTyping(unittest.TestCase):
    """T2.3 链接本身是权威信号：ED2K 不能被当成普通115分享。"""

    def setUp(self):
        ns, kinds = extract_methods(
            "search/dian115/service.py", DIAN115_METHODS,
            class_name="Dian115SearchService",
        )
        exec("from typing import Any, Dict\nfrom urllib.parse import urlencode\n", ns)
        attrs = extract_class_attrs(
            "search/dian115/service.py", "Dian115SearchService",
            ["_OFFLINE_LINK_FIELDS", "_OFFLINE_LINK_RE"],
        )
        ns.update(attrs)
        self.cls = make_class(ns, DIAN115_METHODS, kinds, attrs)

    def test_offline_ed2k_share_returns_offline_link(self):
        share = {"share_kind": "offline", "offline_type": "ed2k", "url": ED2K}
        self.assertEqual(self.cls._resource_type(share), "ed2k")
        self.assertEqual(self.cls._share_url(share), ED2K)

    def test_missing_share_kind_but_ed2k_link_is_offline(self):
        """事故复现：offline_type/share_kind 缺失，链接是 ed2k。"""
        share = {"url": ED2K, "title": "完美世界 286"}
        self.assertEqual(self.cls._resource_type(share), "ed2k")
        self.assertEqual(self.cls._share_url(share), ED2K)

    def test_never_falls_back_to_share_page_for_offline(self):
        """离线字段存在时必须取离线链接，绝不回退到115分享页。"""
        share = {
            "share_kind": "offline",
            "offline_type": "ed2k",
            "url": SHARE_URL,
            "offline_url": ED2K,
        }
        self.assertEqual(self.cls._resource_type(share), "ed2k")
        self.assertEqual(self.cls._share_url(share), ED2K)

    def test_offline_without_link_is_not_routed_as_share(self):
        share = {"share_kind": "offline", "offline_type": "ed2k", "url": ""}
        self.assertEqual(self.cls._resource_type(share), "ed2k")
        self.assertEqual(self.cls._share_url(share), "")

    def test_plain_115_share_unchanged(self):
        share = {"url_115": SHARE_URL}
        self.assertEqual(self.cls._resource_type(share), "115")
        self.assertEqual(self.cls._share_url(share), SHARE_URL)

    def test_magnet_link_detected(self):
        share = {"url": MAGNET}
        self.assertEqual(self.cls._resource_type(share), "magnet")
        self.assertEqual(self.cls._share_url(share), MAGNET)


if __name__ == "__main__":
    unittest.main()
