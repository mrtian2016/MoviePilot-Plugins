# -*- coding: utf-8 -*-
"""PanSearch v1.5.13 回归测试：插件必须能被 MoviePilot 真实导入（加载级守护）。

背景（2026-09-12 实测事故）：
    v1.5.12 引入 ``SyncRuntimeService._collect_stopping_keys`` 时使用了
    ``-> Set[str]`` 注解，但 ``runtime.py`` 的 ``typing`` 导入里没有 ``Set``。
    Python 3.12 下**类体内的函数注解会立即求值**，于是 import 阶段直接抛
    ``NameError: name 'Set' is not defined`` —— 插件加载失败，前端表现为
    「网盘搜索助手没装上」（插件列表里查无此件）。

    老测试全部通过、却完全没拦住，因为它们的宿主是
    ``ast.get_source_segment`` 抽出来的**方法片段**，用一个假类 exec 起来，
    **绕过了真实模块的 import**。本文件补上这个缺口。

三条约束：
    T1 全仓库不得存在「用了 typing 名字但没导入」的情况（静态扫描）。
    T2 每个模块的 ``typing`` 导入必须与注解中实际使用的名字一致
       （防止 ``Set`` / ``FrozenSet`` 这类冷门名字再次漏网）。
    T3 ``NameError`` 只在类体/模块级注解上致命，因此必须覆盖**全部**
       在类体中出现的下标注解，不做「函数内部才用」的豁免。
"""

import ast
import re
import unittest
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent

# typing 中常见的、需要在注解里出现的下标泛型名
TYPING_GENERIC_NAMES = (
    "Set", "FrozenSet", "List", "Dict", "Tuple", "Optional", "Any",
    "Iterable", "Iterator", "Callable", "Sequence", "Mapping",
    "MutableMapping", "Union", "Type", "Coroutine", "Awaitable",
)

_USAGE_RE = re.compile(
    r"(?<![\w.])(" + "|".join(TYPING_GENERIC_NAMES) + r")\s*\["
)


def _iter_sources():
    for path in sorted(PLUGIN_ROOT.rglob("*.py")):
        if "__pycache__" in path.parts or "tests" in path.parts:
            continue
        yield path


def _collect_typing_imports(tree):
    """收集 ``from typing import X`` / ``import typing as t`` 提供的裸名字。"""
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if (node.module or "") == "typing" or (node.module or "").endswith(".typing"):
                for alias in node.names:
                    imported.add(alias.asname or alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "typing":
                    # ``import typing`` 只能用 typing.X，不提供裸名字
                    pass
    return imported


def _collect_generic_usages(source):
    """返回源码中实际使用的 typing 泛型名（含函数内局部注解）。"""
    return {m.group(1) for m in _USAGE_RE.finditer(source)}


class TestNoMissingTypingImports(unittest.TestCase):
    """T1/T2：静态扫描，任何模块都不得使用未导入的 typing 名字。"""

    def test_no_module_uses_undeclared_typing_name(self):
        offenders = []
        for path in _iter_sources():
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(path))
            imported = _collect_typing_imports(tree)
            used = _collect_generic_usages(source)
            missing = used - imported
            if missing:
                offenders.append(
                    (str(path.relative_to(PLUGIN_ROOT)), sorted(missing))
                )
        self.assertEqual(
            offenders,
            [],
            "以下模块使用了未导入的 typing 名字，将在插件加载时抛 NameError："
            + repr(offenders),
        )

    def test_runtime_declares_set(self):
        """T2：本次事故的直接回归点。"""
        path = PLUGIN_ROOT / "core" / "services" / "runtime.py"
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        imported = _collect_typing_imports(tree)
        self.assertIn("Set", imported, "runtime.py 必须导入 Set")
        self.assertIn("Set[", source, "runtime.py 应仍在使用 Set[str] 注解")


class TestAnnotationsEvaluateInClassBody(unittest.TestCase):
    """T3：证明「类体里的注解会立即求值」这一前提，防止将来误加豁免。"""

    def test_class_body_annotation_is_evaluated_at_import(self):
        code = (
            "class Boom:\n"
            "    def f(self) -> DefinitelyNotDefined:\n"
            "        return None\n"
        )
        with self.assertRaises(NameError):
            exec(compile(code, "<boom>", "exec"), {})

    def test_module_level_alias_annotation_is_evaluated(self):
        code = "Alias = MissingName[str]\n"
        with self.assertRaises(NameError):
            exec(compile(code, "<boom>", "exec"), {})


class TestPluginEntrypointCompiles(unittest.TestCase):
    """附加保护：``__init__.py`` 与各子模块必须语法可编译。"""

    def test_all_modules_compile(self):
        failed = []
        for path in _iter_sources():
            source = path.read_text(encoding="utf-8")
            try:
                compile(source, str(path), "exec")
            except SyntaxError as exc:  # noqa: PERF203
                failed.append((str(path.relative_to(PLUGIN_ROOT)), str(exc)))
        self.assertEqual(failed, [], "以下模块无法编译：" + repr(failed))


if __name__ == "__main__":
    unittest.main()
