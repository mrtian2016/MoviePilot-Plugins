# -*- coding: utf-8 -*-
"""
盘链能力：面向用户输入 / 发送的 115 分享链接做识别、解析与状态提示。

设计要点（对齐任务书 B 节）：
    * **只覆盖 115**：不引入非 115 网盘驱动，不复制 CloudSubscribe 的多网盘系统，
      也不 import 它（``p115client`` 解析能力通过现有 ``P115ClientManager`` 复用）；
    * **不破坏既有链路**：本模块只做只读识别与状态校验，绝不改写
      ``check_share_status`` / 转存 / dian115 离线 / 订阅搜索的行为；
    * **不泄露敏感字段**：对外响应（``resolve`` / ``resolve_batch``）只返回
      ``share_code`` 与 ``has_password`` 布尔量，**绝不回显访问码明文**；
      凭据类字段（Token/Cookie/密码）本模块完全不接触；
    * 状态可读：非法 / 失效 / 取消 / 删除 / 需要访问码都给出中文可操作提示。
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional

try:
    from app.log import logger
    LOGGER_AVAILABLE = True
except Exception:  # pragma: no cover - MoviePilot 运行时之外
    logger = logging.getLogger("p115subsearch.share_link")
    LOGGER_AVAILABLE = False

# ---------------------------------------------------------------- 状态常量

STATUS_INVALID = "invalid"
STATUS_UNVERIFIED = "unverified"
STATUS_VALID = "valid"
STATUS_EXPIRED = "expired"
STATUS_DELETED = "deleted"
STATUS_CANCELLED = "cancelled"
STATUS_PASSWORD_REQUIRED = "password_required"
STATUS_ERROR = "error"

# ---------------------------------------------------------------- 解析正则

# 从自由文本（如聊天消息）里切出候选 URL token：排除空白与常见中文标点
_URL_TOKEN_RE = re.compile(r"https?://[^\s一-鿿，。；、！？（）【】《》“”‘’]+", re.IGNORECASE)
# 115 分享链接宿主与分享码（兼容 115.com / 115cdn.com / share.115.com，带或不带 /s/）
_HOST_RE = re.compile(
    r"^https?://(?:share\.)?115(?:cdn)?\.com/(?:s/)?([A-Za-z0-9]{4,20})", re.IGNORECASE
)
# 纯分享码形态：abcd1234-xyz1 / #abcd1234-xyz1# / /abcd1234-xyz1/
_ID_RE = re.compile(r"(?:^|[^A-Za-z0-9])([A-Za-z0-9]{4,20})-([A-Za-z0-9]{1,20})(?![A-Za-z0-9])")
# 链接尾部携带的访问码：?password=xxxx / #xxxx
_QUERY_PWD_RE = re.compile(r"(?:password|pwd|passcode|receive_code)=([A-Za-z0-9]{1,20})", re.IGNORECASE)
_FRAGMENT_PWD_RE = re.compile(r"#([A-Za-z0-9]{1,20})$")
# 自由文本里的访问码：访问码：xxxx / 提取码 xxxx / 密码=xxxx
_ACCESS_CODE_RE = re.compile(r"(?:访问码|提取码|访问密码|提取密码|密码)\s*[:：=]?\s*([A-Za-z0-9]{1,20})")

# 115 要求访问码时的错误码（历史观测值）
_PASSWORD_ERROR_CODES = {990001, 990002, 990003}


def _password_from_rest(rest: str) -> str:
    """从链接尾部（查询串 / 锚点）解析访问码，解析不到返回空串。"""
    if not rest:
        return ""
    match = _QUERY_PWD_RE.search(rest)
    if match:
        return match.group(1)
    match = _FRAGMENT_PWD_RE.search(rest)
    if match:
        return match.group(1)
    return ""


class ShareLinkHandler:
    """115 分享链接识别 / 状态解析处理器。

    :param p115_manager: ``clients.p115.P115ClientManager`` 实例；为 None 时
        只做离线格式识别，返回 ``unverified`` 状态而不是失败抛异常。
    """

    def __init__(self, p115_manager: Any = None) -> None:
        self._manager = p115_manager

    # ------------------------------------------------------------ 纯解析

    def parse(self, text: Any) -> Dict[str, Any]:
        """识别并解析一段文本中的 115 分享链接（**纯本地、零网络**）。

        返回字典含 ``is_share_link`` / ``share_code`` / ``receive_code`` /
        ``has_password`` / ``status`` / ``message``。``receive_code`` 仅供内部
        转存使用，对外响应请走 :meth:`resolve`。
        """
        raw = str(text or "").strip()
        if not raw:
            return self._parse_failure("请输入 115 分享链接后再试")

        share_code = ""
        receive_code = ""
        for token in _URL_TOKEN_RE.findall(raw):
            match = _HOST_RE.match(token)
            if match:
                share_code = match.group(1)
                receive_code = _password_from_rest(token[match.end():])
                break

        if not share_code:
            match = _ID_RE.search(raw)
            if match:
                share_code = match.group(1)
                receive_code = match.group(2)

        if not share_code:
            return self._parse_failure(
                "无法识别为 115 分享链接，请检查链接格式"
                "（示例：https://115.com/s/xxxx?password=yyyy）"
            )

        if not receive_code:
            match = _ACCESS_CODE_RE.search(raw)
            if match:
                receive_code = match.group(1)

        return {
            "is_share_link": True,
            "share_type": "115",
            "share_code": share_code,
            "receive_code": receive_code,
            "has_password": bool(receive_code),
            "status": STATUS_VALID,
            "message": (
                "已识别 115 分享链接（含访问码）" if receive_code
                else "已识别 115 分享链接（未提供访问码，部分分享需要访问码）"
            ),
        }

    @staticmethod
    def _parse_failure(message: str) -> Dict[str, Any]:
        return {
            "is_share_link": False,
            "share_type": "115",
            "share_code": "",
            "receive_code": "",
            "has_password": False,
            "status": STATUS_INVALID,
            "message": message,
        }

    def extract_links(self, text: Any) -> List[Dict[str, Any]]:
        """从一段文本中提取全部 115 分享链接（去重，按出现顺序）。"""
        raw = str(text or "").strip()
        if not raw:
            return []

        results: List[Dict[str, Any]] = []
        seen = set()

        for token in _URL_TOKEN_RE.findall(raw):
            match = _HOST_RE.match(token)
            if not match:
                continue
            code = match.group(1)
            if code in seen:
                continue
            seen.add(code)
            results.append({
                "share_code": code,
                "receive_code": _password_from_rest(token[match.end():]),
                "has_password": False,
                "is_share_link": True,
                "share_type": "115",
            })

        if not results:
            for match in _ID_RE.finditer(raw):
                code = match.group(1)
                if code in seen:
                    continue
                seen.add(code)
                results.append({
                    "share_code": code,
                    "receive_code": match.group(2),
                    "has_password": False,
                    "is_share_link": True,
                    "share_type": "115",
                })

        # 自由文本里若只出现唯一的访问码，则补给它名下仍缺码的链接
        codes = {m.group(1) for m in _ACCESS_CODE_RE.finditer(raw)}
        if len(codes) == 1:
            only_code = next(iter(codes))
            for item in results:
                if not item["receive_code"]:
                    item["receive_code"] = only_code

        for item in results:
            item["has_password"] = bool(item["receive_code"])
        return results

    # ------------------------------------------------------------ 状态解析

    def resolve(self, text: Any) -> Dict[str, Any]:
        """解析并校验链接状态。**永不抛异常**，返回结构化中文提示。

        响应体只包含 ``share_code`` 与非敏感元信息，**不含访问码明文**。
        """
        parsed = self.parse(text)
        if not parsed.get("is_share_link"):
            return self._reply(False, parsed["message"], self._data(STATUS_INVALID, parsed))

        if self._manager is None:
            return self._reply(
                False,
                "115 网盘客户端尚未就绪，暂时无法校验该链接；链接格式已识别，请稍后在账户正常时重试",
                self._data(STATUS_UNVERIFIED, parsed, status_text="未校验"),
            )

        extractor = getattr(self._manager, "extract_share_info", None)
        if callable(extractor):
            try:
                extractor(text)
            except Exception:  # noqa: BLE001
                # 异常文本可能内嵌完整分享链接 / 访问码，禁止写入日志或响应
                logger.warning("115 分享链接解析失败（异常详情已隐藏）")
                return self._reply(
                    False,
                    "115 分享链接解析失败，请稍后重试；"
                    "若持续失败请检查链接格式是否正确",
                    self._data(STATUS_ERROR, parsed, status_text="解析失败"),
                )

        try:
            status = self._manager.check_share_status(text)
        except Exception:  # noqa: BLE001
            # 异常文本可能内嵌完整分享链接 / 访问码，禁止写入日志或响应
            logger.warning("115 分享状态查询失败（异常详情已隐藏）")
            return self._reply(
                False,
                "115 分享状态查询失败，请稍后重试；"
                "若持续失败请检查 115 账户是否正常",
                self._data(STATUS_ERROR, parsed, status_text="查询失败"),
            )

        if status is None:
            return self._reply(False, "115 未返回分享状态，请稍后重试",
                               self._data(STATUS_ERROR, parsed, status_text="未知状态"))

        status_text = str(getattr(status, "status_text", "") or "未知状态")
        file_count = int(getattr(status, "file_count", 0) or 0)
        share_title = self._share_title(status)

        if getattr(status, "is_valid", False):
            return self._reply(
                True,
                f"链接有效，共 {file_count} 个文件" if file_count else "链接有效",
                self._data(STATUS_VALID, parsed, status_text=status_text,
                           file_count=file_count, share_title=share_title),
            )

        if getattr(status, "is_expired", False):
            return self._reply(False, "分享链接已过期，请向分享者索取新的分享链接",
                               self._data(STATUS_EXPIRED, parsed, status_text=status_text))

        if getattr(status, "is_deleted", False):
            return self._reply(False, "分享文件已被删除，无法转存",
                               self._data(STATUS_DELETED, parsed, status_text=status_text))

        if getattr(status, "is_cancelled", False):
            return self._reply(False, "分享已被取消，无法转存",
                               self._data(STATUS_CANCELLED, parsed, status_text=status_text))

        if self._needs_password(status):
            return self._reply(
                False,
                "该分享需要访问码，请补充访问码后重试"
                + ("（当前已填写的访问码可能不正确）" if parsed.get("has_password") else ""),
                self._data(STATUS_PASSWORD_REQUIRED, parsed, status_text=status_text,
                           needs_password=True),
            )

        return self._reply(False, f"分享链接不可用：{status_text}",
                           self._data(STATUS_ERROR, parsed, status_text=status_text))

    def resolve_batch(self, texts: Any) -> Dict[str, Any]:
        """批量解析链接。返回逐条结果与有效 / 无效统计。"""
        items = list(texts or [])
        if not items:
            return {
                "success": False,
                "message": "没有需要解析的链接",
                "data": {"results": [], "total": 0, "valid": 0, "invalid": 0},
            }
        results = [self.resolve(item) for item in items]
        valid = sum(1 for item in results if item["data"]["status"] == STATUS_VALID)
        invalid = sum(1 for item in results if item["data"]["status"] == STATUS_INVALID)
        data = {"results": results, "total": len(results), "valid": valid, "invalid": invalid}
        return {
            "success": True,
            "message": f"共解析 {len(results)} 条链接，有效 {valid} 条，无效 {invalid} 条",
            "data": data,
        }

    # ------------------------------------------------------------ 内部工具

    @staticmethod
    def _share_title(status: Any) -> str:
        info = getattr(status, "share_info", None)
        if isinstance(info, dict):
            return str(info.get("share_title") or info.get("title") or "")
        return ""

    @staticmethod
    def _needs_password(status: Any) -> bool:
        error_code = getattr(status, "error_code", None)
        try:
            if int(error_code) in _PASSWORD_ERROR_CODES:
                return True
        except (TypeError, ValueError):
            pass
        error_message = str(getattr(status, "error_message", "") or "")
        if "访问码" in error_message or "密码" in error_message:
            return True
        return "password" in error_message.lower()

    @staticmethod
    def _data(status: str, parsed: Dict[str, Any], status_text: str = "",
              file_count: int = 0, share_title: str = "",
              needs_password: bool = False) -> Dict[str, Any]:
        # 注意：这里只输出 share_code 与 has_password 布尔量，绝不回显访问码
        return {
            "status": status,
            "status_text": status_text,
            "is_share_link": bool(parsed.get("is_share_link")),
            "share_code": parsed.get("share_code") or "",
            "has_password": bool(parsed.get("has_password")),
            "file_count": file_count,
            "share_title": share_title,
            "needs_password": needs_password,
        }

    @staticmethod
    def _reply(success: bool, message: str, data: Dict[str, Any]) -> Dict[str, Any]:
        return {"success": bool(success), "message": message, "data": data}
