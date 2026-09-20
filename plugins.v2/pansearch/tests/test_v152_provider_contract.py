# -*- coding: utf-8 -*-
"""PanSearch v1.5.2 六大网盘 provider 契约回归测试。

背景：v1.5.1 T1 给 OfflineDownloadOperations 契约新增抽象成员
get_offline_tasks，但 123 网盘与光鸭的离线服务未实现该方法，
CloudDriveProvider.__post_init__ 运行时校验直接 TypeError，
插件初始化整体崩溃。本测试用真实 import 路径构造全部六个
provider（等价 __post_init__ 全能力校验），防止同类回归再犯。

宿主无 MoviePilot app 包时通过 _drive_import_env 打桩；容器内
存在真 app 包则直接使用，两条路径校验逻辑一致。
"""

import sys
import unittest
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
PLUGIN_ROOT = TESTS_DIR.parent
for entry in (str(TESTS_DIR), str(PLUGIN_ROOT.parent)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from _drive_import_env import install_stubs, prepare_plugin_packages

install_stubs()
prepare_plugin_packages(PLUGIN_ROOT)

from pansearch.core.cloud import (  # noqa: E402
    CAPABILITY_CONTRACTS,
    CloudDriveCapability,
    OfflineDownloadOperations,
    _implements_contract,
)
from pansearch.drive.alipan import (  # noqa: E402
    AliPanClient,
    AliPanDrive,
    create_alipan_provider,
)
from pansearch.drive.guangya import (  # noqa: E402
    GuangyaClient,
    GuangyaDrive,
    create_guangya_provider,
)
from pansearch.drive.p115 import (  # noqa: E402
    P115ClientManager,
    create_p115_provider,
)
from pansearch.drive.p123 import (  # noqa: E402
    P123ClientManager,
    P123Drive,
    create_p123_provider,
)
from pansearch.drive.quark import (  # noqa: E402
    QuarkClient,
    QuarkDrive,
    create_quark_provider,
)
from pansearch.drive.tianyi import (  # noqa: E402
    TianyiClient,
    TianyiDrive,
    create_tianyi_provider,
)


def build_all_providers():
    """按 __init__.py 注册顺序构造六大 provider（空凭证，不发网络请求）。"""
    return {
        "p115": create_p115_provider(P115ClientManager(cookies="")),
        "p123": create_p123_provider(
            P123Drive(client=P123ClientManager(token=""))
        ),
        "quark": create_quark_provider(QuarkDrive(client=QuarkClient(cookie=""))),
        "guangya": create_guangya_provider(GuangyaDrive(client=GuangyaClient())),
        "tianyi": create_tianyi_provider(TianyiDrive(client=TianyiClient(cookie=""))),
        "alipan": create_alipan_provider(AliPanDrive(client=AliPanClient())),
    }


class TestProviderContract(unittest.TestCase):
    """六大 provider 完整注册必须通过全部能力契约校验。"""

    @classmethod
    def setUpClass(cls):
        cls.providers = build_all_providers()

    def test_all_six_providers_construct(self):
        self.assertEqual(len(self.providers), 6)

    def test_every_registered_capability_satisfies_contract(self):
        for key, provider in self.providers.items():
            for capability, service in provider.services.items():
                contract = CAPABILITY_CONTRACTS.get(capability)
                self.assertIsNotNone(
                    contract, f"{key} 注册了未定义能力 {capability}"
                )
                self.assertTrue(
                    _implements_contract(service, contract),
                    f"{key} 的 {capability.value} 服务不符合契约 "
                    f"{contract.__name__}",
                )

    def test_offline_download_contract_keeps_get_offline_tasks(self):
        """契约防稀释：get_offline_tasks 仍须是 OfflineDownloadOperations 成员。"""
        self.assertIn("get_offline_tasks", OfflineDownloadOperations.__dict__)

    def test_offline_download_services_implement_get_offline_tasks(self):
        for key, provider in self.providers.items():
            service = provider.services.get(
                CloudDriveCapability.OFFLINE_DOWNLOAD
            )
            if service is None:
                continue
            self.assertTrue(
                callable(getattr(service, "get_offline_tasks", None)),
                f"{key} 的 offline_download 缺少 get_offline_tasks",
            )


class TestP123OfflineTasksAccessor(unittest.TestCase):
    """123 get_offline_tasks 复用 _load_tasks 缓存，空客户端不触发网络。"""

    def test_returns_task_list_snapshot(self):
        service = create_p123_provider(
            P123Drive(client=P123ClientManager(token=""))
        ).services[CloudDriveCapability.OFFLINE_DOWNLOAD]

        sentinel = [{"id": "A1B2", "name": "x", "state": "running"}]

        def fake_load(force=False):
            fake_load.calls.append(force)
            return [dict(task) for task in sentinel]

        fake_load.calls = []
        service._load_tasks = fake_load
        tasks = service.get_offline_tasks(force=True)
        self.assertEqual(tasks, sentinel)
        self.assertEqual(fake_load.calls, [True])
        # 返回副本，外部修改不得污染缓存
        tasks[0]["name"] = "tampered"
        self.assertEqual(sentinel[0]["name"], "x")


class TestGuangyaOfflineTasksAccessor(unittest.TestCase):
    """光鸭 get_offline_tasks 归一化 cloud_task_list 返回。"""

    class FakeClient:
        API_BASE_URL = "https://guangya.fake"
        access_token = "fake-token"

        def request(self, method, url, **kwargs):
            return {
                "code": 0,
                "data": {
                    "list": [
                        {
                            "taskId": "t1",
                            "fileName": "demo.mkv",
                            "size": 1024,
                            "status": 1,
                            "percent": 55,
                            "createTime": 1750000000,
                        }
                    ]
                },
            }

        @staticmethod
        def is_success(response):
            return isinstance(response, dict) and response.get("code") == 0

        @staticmethod
        def data(response):
            return response.get("data") or {}

    class BrokenClient(FakeClient):
        def request(self, method, url, **kwargs):
            raise RuntimeError("network down")

    def test_maps_task_rows(self):
        from pansearch.drive.guangya.offline import GuangyaOfflineService

        service = GuangyaOfflineService(client=self.FakeClient(), files=None)
        tasks = service.get_offline_tasks(force=True)
        self.assertEqual(len(tasks), 1)
        task = tasks[0]
        self.assertEqual(task["id"], "t1")
        self.assertEqual(task["name"], "demo.mkv")
        self.assertEqual(task["size"], 1024)
        self.assertEqual(task["state"], "running")
        self.assertFalse(task["completed"])
        self.assertFalse(task["failed"])
        self.assertEqual(task["percent"], 55.0)
        self.assertEqual(task["add_time"], 1750000000)

    def test_failure_degrades_to_empty_list(self):
        from pansearch.drive.guangya.offline import GuangyaOfflineService

        service = GuangyaOfflineService(client=self.BrokenClient(), files=None)
        self.assertEqual(service.get_offline_tasks(force=True), [])


if __name__ == "__main__":
    unittest.main()
