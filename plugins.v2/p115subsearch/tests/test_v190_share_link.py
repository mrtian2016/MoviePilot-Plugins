# -*- coding: utf-8 -*-
"""
P115SubSearch v1.9.0 测试（二）：盘链能力（115 分享链接识别 / 解析 / 状态提示）

设计原则（对齐任务书 B 节）：
    * 至少覆盖 115 分享链接；不得破坏已有 115 转存、访问码处理、
      dian115 离线资源转存与订阅搜索链路；
    * 解析给出明确状态；非法 / 失效 / 需要访问码给出可操作提示；
    * **绝不泄露配置中的敏感字段**（响应里不得出现访问码明文）。

本文件为**行为测试**：注入假 P115ClientManager 实跑 ShareLinkHandler，
覆盖输入输出与降级路径；纯解析用例完全离线，不依赖网络。

运行：python tests/test_v190_share_link.py
     或者 pytest tests/test_v190_share_link.py
"""
import importlib.machinery
import importlib.util
import json
import sys
import types
from pathlib import Path

PLUGIN = Path(__file__).resolve().parent.parent

_passed = 0
_failed = []


def check(name, condition, detail=""):
    global _passed
    if condition:
        _passed += 1
        print(f"  [PASS] {name}")
    else:
        _failed.append(f"{name} :: {detail}")
        print(f"  [FAIL] {name} :: {detail}")


# ===========================================================================
# 0. 最小 app.* 桩 + 真实包语义加载
# ===========================================================================

def _install_stubs():
    app = types.ModuleType("app")
    log = types.ModuleType("app.log")

    class _L:
        def info(self, *a, **k):
            pass

        warning = error = debug = info

    log.logger = _L()
    app.log = log
    sys.modules["app"] = app
    sys.modules["app.log"] = log


def _ensure_pkg(name, path):
    if name in sys.modules:
        return sys.modules[name]
    mod = types.ModuleType(name)
    mod.__path__ = [str(path)]
    mod.__package__ = name
    spec = importlib.machinery.ModuleSpec(name, loader=None, is_package=True)
    spec.submodule_search_locations = mod.__path__
    mod.__spec__ = spec
    sys.modules[name] = mod
    return mod


def load_sub(qualname, relpath):
    parts = qualname.split(".")
    for index in range(1, len(parts)):
        _ensure_pkg(".".join(parts[:index]), PLUGIN / Path(*parts[1:index]))
    spec = importlib.util.spec_from_file_location(qualname, str(PLUGIN / relpath))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[qualname] = mod
    spec.loader.exec_module(mod)
    return mod


# ===========================================================================
# 1. 假 P115ClientManager
# ===========================================================================

class FakeStatus:
    """模拟 clients.p115.ShareLinkStatus"""

    def __init__(self, is_valid=True, is_expired=False, is_cancelled=False,
                 is_deleted=False, error_code=None, error_message=None,
                 file_count=3, share_info=None):
        self.is_valid = is_valid
        self.is_expired = is_expired
        self.is_cancelled = is_cancelled
        self.is_deleted = is_deleted
        self.error_code = error_code
        self.error_message = error_message
        self.file_count = file_count
        self.share_info = share_info or {"share_title": "示例分享", "file_count": file_count}

    @property
    def status_text(self):
        if self.is_valid:
            return "有效"
        if self.is_expired:
            return "已过期"
        if self.is_cancelled:
            return "已取消"
        if self.is_deleted:
            return "文件已删除"
        return self.error_message or "未知状态"


class FakeManager:
    def __init__(self, extracted=None, status=None, extract_error=None, status_error=None):
        self.extracted = extracted or {"share_code": "abcd1234", "receive_code": "xyz1"}
        self.status = status or FakeStatus()
        self.extract_error = extract_error
        self.status_error = status_error
        self.extract_calls = []
        self.status_calls = []

    def extract_share_info(self, url):
        self.extract_calls.append(url)
        if self.extract_error is not None:
            raise self.extract_error
        return self.extracted

    def check_share_status(self, url):
        self.status_calls.append(url)
        if self.status_error is not None:
            raise self.status_error
        return self.status


def _handler_module(manager=None, **kwargs):
    _install_stubs()
    mod = load_sub("p115subsearch_v190sl.handlers.share_link", "handlers/share_link.py")
    handler = mod.ShareLinkHandler(p115_manager=manager, **kwargs)
    return mod, handler


class LogRecorder:
    """替换模块级 logger，捕获 warning 文本用于断言不泄露。"""

    def __init__(self):
        self.records = []

    def warning(self, msg, *args, **kwargs):
        self.records.append(str(msg))

    info = error = debug = warning

    @property
    def blob(self):
        return "\n".join(self.records)


# ===========================================================================
# 2. 纯解析：链接形态
# ===========================================================================

def test_parse_standard_115_links():
    mod, handler = _handler_module(FakeManager())
    cases = [
        ("https://115.com/s/abcd1234?password=xyz1", "abcd1234", "xyz1"),
        ("https://115cdn.com/s/abcd1234?password=xyz1", "abcd1234", "xyz1"),
        ("https://share.115.com/abcd1234?password=xyz1", "abcd1234", "xyz1"),
        ("https://115.com/s/abcd1234#xyz1", "abcd1234", "xyz1"),
        ("https://115.com/s/abcd1234", "abcd1234", ""),
        ("abcd1234-xyz1", "abcd1234", "xyz1"),
        ("/abcd1234-xyz1/", "abcd1234", "xyz1"),
        ("#abcd1234-xyz1#", "abcd1234", "xyz1"),
    ]
    for text, code, pwd in cases:
        parsed = handler.parse(text)
        check(f"1-{text} 识别为 115 分享链接", parsed.get("is_share_link") is True,
              json.dumps(parsed, ensure_ascii=False))
        check(f"1-{text} share_code 正确", parsed.get("share_code") == code, str(parsed))
        check(f"1-{text} receive_code 正确", parsed.get("receive_code") == pwd, str(parsed))


def test_parse_free_text_with_password():
    """从一段自由文本（如聊天消息）中提取链接与访问码。"""
    _, handler = _handler_module(FakeManager())
    text = "【分享】示例资源 https://115.com/s/abcd1234 访问码：xyz1 尽快转存"
    parsed = handler.parse(text)
    check("2-1 自由文本识别为分享链接", parsed.get("is_share_link") is True,
          json.dumps(parsed, ensure_ascii=False))
    check("2-2 提取到 share_code", parsed.get("share_code") == "abcd1234", str(parsed))
    check("2-3 提取到访问码", parsed.get("receive_code") == "xyz1", str(parsed))

    text2 = "链接: https://115cdn.com/s/zzzz9999?password=aaaa 提取码 aaaa"
    parsed2 = handler.parse(text2)
    check("2-4 自由文本 + 查询串识别", parsed2.get("share_code") == "zzzz9999", str(parsed2))
    check("2-5 自由文本访问码正确", parsed2.get("receive_code") == "aaaa", str(parsed2))


def test_parse_invalid_inputs():
    _, handler = _handler_module(FakeManager())
    for text in ["", "   ", "这不是一个链接", "https://example.com/s/abcd1234",
                 "https://115.com/", "ftp://115.com/s/abcd"]:
        parsed = handler.parse(text)
        check(f"3-{text!r} 判定为非法", parsed.get("is_share_link") is False,
              json.dumps(parsed, ensure_ascii=False))
        check(f"3-{text!r} 状态为 invalid", parsed.get("status") == "invalid", str(parsed))
        check(f"3-{text!r} 给出可操作提示", bool(parsed.get("message")), str(parsed))


def test_parse_never_leaks_password():
    """对外响应（resolve）里绝不能出现访问码明文，只用布尔量表达。"""
    _, handler = _handler_module(FakeManager(status=FakeStatus(is_valid=True, file_count=1)))
    result = handler.resolve("https://115.com/s/abcd1234?password=SECRET99")
    blob = json.dumps(result, ensure_ascii=False)
    check("4-1 对外响应不含访问码明文", "SECRET99" not in blob, blob)
    check("4-2 用 has_password 布尔量表达", result["data"].get("has_password") is True, blob)
    check("4-3 对外响应仍含 share_code 供转存", result["data"].get("share_code") == "abcd1234", blob)

    # parse 是内部结构（转存直接用），保留 receive_code；这是与对外响应的边界
    parsed = handler.parse("https://115.com/s/abcd1234?password=SECRET99")
    check("4-4 parse 保留 receive_code 供内部转存", parsed.get("receive_code") == "SECRET99",
          json.dumps(parsed, ensure_ascii=False))


# ===========================================================================
# 3. 状态解析（本地校验）
# ===========================================================================

def test_resolve_without_manager_degrades():
    mod, handler = _handler_module(None)
    result = handler.resolve("https://115.com/s/abcd1234?password=xyz1")
    check("5-1 无 115 客户端时 success=False", result["success"] is False,
          json.dumps(result, ensure_ascii=False))
    check("5-2 状态为 unverified（仍需凭据）", result["data"]["status"] == "unverified",
          json.dumps(result, ensure_ascii=False))
    check("5-3 提示可操作", bool(result["message"]), result["message"])
    check("5-4 不抛异常", True)


def test_resolve_invalid_link():
    _, handler = _handler_module(FakeManager())
    result = handler.resolve("随便一段文本")
    check("6-1 非法链接 success=False", result["success"] is False,
          json.dumps(result, ensure_ascii=False))
    check("6-2 状态为 invalid", result["data"]["status"] == "invalid", str(result))
    check("6-3 中文可操作提示", "链接" in result["message"], result["message"])


def test_resolve_valid_link():
    manager = FakeManager(status=FakeStatus(is_valid=True, file_count=7))
    _, handler = _handler_module(manager)
    result = handler.resolve("https://115.com/s/abcd1234?password=xyz1")
    check("7-1 有效链接 success=True", result["success"] is True,
          json.dumps(result, ensure_ascii=False))
    check("7-2 状态为 valid", result["data"]["status"] == "valid", str(result["data"]))
    check("7-3 带出文件数", result["data"].get("file_count") == 7, str(result["data"]))
    check("7-4 中文状态文案", result["data"].get("status_text") == "有效", str(result["data"]))
    check("7-5 调用了客户端校验", len(manager.status_calls) == 1, f"{manager.status_calls}")
    check("7-6 响应不含访问码明文", "xyz1" not in json.dumps(result, ensure_ascii=False),
          json.dumps(result, ensure_ascii=False))


def test_resolve_expired_and_deleted():
    cases = [
        ("expired", FakeStatus(is_valid=False, is_expired=True), "已过期", "过期"),
        ("deleted", FakeStatus(is_valid=False, is_deleted=True), "文件已删除", "删除"),
        ("cancelled", FakeStatus(is_valid=False, is_cancelled=True), "已取消", "取消"),
    ]
    for name, status, text, keyword in cases:
        _, handler = _handler_module(FakeManager(status=status))
        result = handler.resolve("https://115.com/s/abcd1234?password=xyz1")
        check(f"8-{name} success=False", result["success"] is False,
              json.dumps(result, ensure_ascii=False))
        check(f"8-{name} 状态为 {name}", result["data"]["status"] == name, str(result["data"]))
        check(f"8-{name} 状态文案 {text}", result["data"].get("status_text") == text,
              str(result["data"]))
        check(f"8-{name} 提示含原因", keyword in result["message"], result["message"])


def test_resolve_password_required():
    """无访问码且来源要求访问码 -> password_required 并给出填写引导。"""
    status = FakeStatus(is_valid=False, error_code=990001,
                        error_message="需要访问码", file_count=0)
    manager = FakeManager(status=status)
    _, handler = _handler_module(manager)
    result = handler.resolve("https://115.com/s/abcd1234")
    check("9-1 无码链接 success=False", result["success"] is False,
          json.dumps(result, ensure_ascii=False))
    check("9-2 状态为 password_required",
          result["data"]["status"] == "password_required", str(result["data"]))
    check("9-3 提示引导补充访问码", "访问码" in result["message"], result["message"])
    check("9-4 标记 needs_password", result["data"].get("needs_password") is True,
          str(result["data"]))

    # 带码但码错误同样归类为需要访问码
    status2 = FakeStatus(is_valid=False, error_code=990002,
                         error_message="访问码错误", file_count=0)
    _, handler2 = _handler_module(FakeManager(status=status2))
    result2 = handler2.resolve("https://115.com/s/abcd1234?password=bad1")
    check("9-5 码错误也归为 password_required",
          result2["data"]["status"] == "password_required", str(result2["data"]))
    check("9-6 响应不含错误访问码", "bad1" not in json.dumps(result2, ensure_ascii=False),
          json.dumps(result2, ensure_ascii=False))


def test_resolve_manager_exception_degrades():
    manager = FakeManager(status_error=RuntimeError("115 接口 502"))
    _, handler = _handler_module(manager)
    result = handler.resolve("https://115.com/s/abcd1234?password=xyz1")
    check("10-1 客户端异常不抛出", isinstance(result, dict), str(result))
    check("10-2 success=False", result["success"] is False, json.dumps(result, ensure_ascii=False))
    check("10-3 状态为 error", result["data"]["status"] == "error", str(result["data"]))
    check("10-4 提示为通用可操作中文（不回显异常详情）",
          "重试" in result["message"] and "502" not in result["message"],
          result["message"])
    check("10-5 异常响应不含访问码明文",
          "xyz1" not in json.dumps(result, ensure_ascii=False),
          json.dumps(result, ensure_ascii=False))

    manager2 = FakeManager(extract_error=ValueError("非法链接"))
    _, handler2 = _handler_module(manager2)
    result2 = handler2.resolve("https://115.com/s/abcd1234?password=xyz1")
    check("10-6 解析异常降级为 invalid 或 error",
          result2["data"]["status"] in ("invalid", "error"), str(result2["data"]))
    check("10-7 解析异常也带提示", bool(result2["message"]), result2["message"])


def test_exception_text_never_leaks_share_url_or_code():
    """安全回归：异常文本可能内嵌完整分享链接 / 访问码，日志与响应都不得回显。

    share_code 是解析出的非敏感字段（对外响应按设计保留），
    这里断言的是完整分享 URL 与访问码明文均不出现。
    """
    leak_url = "https://115.com/s/leak9999?password=LEAKPWD7"
    leak_pwd = "LEAKPWD7"

    # 场景一：extract_share_info 抛出的异常内嵌分享链接与访问码
    mod1, handler1 = _handler_module(FakeManager(
        extract_error=RuntimeError(f"transfer failed for {leak_url} pwd={leak_pwd}")))
    rec1 = LogRecorder()
    mod1.logger = rec1
    result1 = handler1.resolve(leak_url)
    blob1 = json.dumps(result1, ensure_ascii=False)

    check("14-1 解析异常不抛出", isinstance(result1, dict), str(result1))
    check("14-2 解析异常 success=False", result1["success"] is False, blob1)
    check("14-3 解析异常仍为 error 状态", result1["data"]["status"] == "error", str(result1["data"]))
    check("14-4 解析异常响应不含分享 URL", leak_url not in blob1, blob1)
    check("14-5 解析异常响应不含访问码明文", leak_pwd not in blob1, blob1)
    check("14-6 解析异常响应不含异常原文", "transfer failed" not in blob1, blob1)
    check("14-7 解析异常告警日志不含分享 URL", leak_url not in rec1.blob, rec1.blob)
    check("14-8 解析异常告警日志不含访问码明文", leak_pwd not in rec1.blob, rec1.blob)
    check("14-9 解析异常告警日志不含异常原文", "transfer failed" not in rec1.blob, rec1.blob)
    check("14-10 解析异常仍有 warning 记录", len(rec1.records) >= 1, str(rec1.records))
    check("14-11 解析异常提示通用且可操作",
          "解析失败" in result1["message"] and "重试" in result1["message"],
          result1["message"])
    check("14-12 解析异常保留非敏感 share_code",
          result1["data"].get("share_code") == "leak9999", blob1)

    # 场景二：check_share_status 抛出的异常内嵌分享链接与访问码
    mod2, handler2 = _handler_module(FakeManager(
        status_error=ValueError(f"bad status for {leak_url} code {leak_pwd}")))
    rec2 = LogRecorder()
    mod2.logger = rec2
    result2 = handler2.resolve(leak_url)
    blob2 = json.dumps(result2, ensure_ascii=False)

    check("15-1 查询异常不抛出", isinstance(result2, dict), str(result2))
    check("15-2 查询异常 success=False", result2["success"] is False, blob2)
    check("15-3 查询异常仍为 error 状态", result2["data"]["status"] == "error", str(result2["data"]))
    check("15-4 查询异常响应不含分享 URL", leak_url not in blob2, blob2)
    check("15-5 查询异常响应不含访问码明文", leak_pwd not in blob2, blob2)
    check("15-6 查询异常响应不含异常原文", "bad status" not in blob2, blob2)
    check("15-7 查询异常告警日志不含分享 URL", leak_url not in rec2.blob, rec2.blob)
    check("15-8 查询异常告警日志不含访问码明文", leak_pwd not in rec2.blob, rec2.blob)
    check("15-9 查询异常告警日志不含异常原文", "bad status" not in rec2.blob, rec2.blob)
    check("15-10 查询异常仍有 warning 记录", len(rec2.records) >= 1, str(rec2.records))
    check("15-11 查询异常提示通用且可操作",
          "状态查询失败" in result2["message"] and "重试" in result2["message"],
          result2["message"])
    check("15-12 查询异常保留非敏感 share_code",
          result2["data"].get("share_code") == "leak9999", blob2)


def test_resolve_batch():
    manager = FakeManager(status=FakeStatus(is_valid=True, file_count=2))
    _, handler = _handler_module(manager)
    result = handler.resolve_batch([
        "https://115.com/s/abcd1234?password=xyz1",
        "不是链接",
    ])
    check("11-1 批量返回 results", len(result["data"]["results"]) == 2,
          json.dumps(result["data"], ensure_ascii=False))
    check("11-2 批量统计有效数", result["data"].get("valid") == 1,
          json.dumps(result["data"], ensure_ascii=False))
    check("11-3 批量统计无效数", result["data"].get("invalid") == 1,
          json.dumps(result["data"], ensure_ascii=False))

    empty = handler.resolve_batch([])
    check("11-4 空批次给出提示", empty["success"] is False,
          json.dumps(empty, ensure_ascii=False))


def test_extract_links_from_text():
    _, handler = _handler_module(FakeManager())
    text = ("第一个 https://115.com/s/aaaa1111?password=p1 ，"
            "第二个 https://115cdn.com/s/bbbb2222 访问码 q2 结束")
    links = handler.extract_links(text)
    check("12-1 提取出两条链接", len(links) == 2, json.dumps(links, ensure_ascii=False))
    check("12-2 第一条链接正确", links[0]["share_code"] == "aaaa1111", str(links[0]))
    check("12-3 第二条链接访问码正确", links[1]["receive_code"] == "q2", str(links[1]))
    check("12-4 去重", len(handler.extract_links(text)) == 2, "重复")
    check("12-5 无链接返回空列表", handler.extract_links("没有链接") == [], "非空")


def test_no_cloudsubscribe_dependency():
    import ast

    path = PLUGIN / "handlers/share_link.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    bad = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            bad.extend(a.name for a in node.names if "cloudsubscribe" in a.name.lower())
        elif isinstance(node, ast.ImportFrom) and "cloudsubscribe" in (node.module or "").lower():
            bad.append(node.module)
    check("13-1 不 import CloudSubscribe", not bad, str(bad))
    check("13-2 无 cloudsubscribe 模块路径依赖",
          "cloudsubscribe." not in path.read_text(encoding="utf-8").lower(), "出现模块路径")


# ===========================================================================
# runner
# ===========================================================================

TESTS = [
    test_parse_standard_115_links,
    test_parse_free_text_with_password,
    test_parse_invalid_inputs,
    test_parse_never_leaks_password,
    test_resolve_without_manager_degrades,
    test_resolve_invalid_link,
    test_resolve_valid_link,
    test_resolve_expired_and_deleted,
    test_resolve_password_required,
    test_resolve_manager_exception_degrades,
    test_exception_text_never_leaks_share_url_or_code,
    test_resolve_batch,
    test_extract_links_from_text,
    test_no_cloudsubscribe_dependency,
]


def main():
    print("=" * 68)
    print("P115SubSearch v1.9.0 盘链能力测试")
    print("=" * 68)
    for fn in TESTS:
        print()
        print(f"-- {fn.__name__}")
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            _failed.append(f"{fn.__name__} 执行异常 :: {type(exc).__name__}: {exc}")
            print(f"  [FAIL] {fn.__name__} 执行异常 :: {type(exc).__name__}: {exc}")
    print()
    print("=" * 68)
    print(f"结果：{_passed} 项通过，{len(_failed)} 项失败")
    print("=" * 68)
    if _failed:
        for f in _failed:
            print(f"  ✗ {f}")
        return 1
    print("\n✅ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
