# -*- coding: utf-8 -*-
"""pytest 收集前置：为宿主环境安装 app.* 桩与 pansearch 包壳。

插件主入口 pansearch/__init__.py 依赖完整 MoviePilot 宿主；tests 目录
同时是包（tests/__init__.py），pytest prepend 模式会先导入父包。这里
提前把桩环境与带 __path__ 的包壳注册进 sys.modules，使
pansearch.drive.* 的真实导入路径可用，六大 provider 契约测试得以在
宿主与容器内以同一份代码运行。容器内存在真 app 包时桩不生效。
"""

import sys
from pathlib import Path

_TESTS_DIR = Path(__file__).resolve().parent / "plugins.v2" / "pansearch" / "tests"
_PLUGIN_ROOT = _TESTS_DIR.parent

if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))

from _drive_import_env import install_stubs, prepare_plugin_packages

install_stubs()
prepare_plugin_packages(_PLUGIN_ROOT)
