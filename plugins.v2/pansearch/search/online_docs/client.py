"""通过腾讯文档和金山文档数据接口搜索资源链接。"""

import base64
import json
import re
import uuid
import zlib
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlparse

import unicodedata
from app.log import logger

from ..http_client import normalize_proxies, requests
from ..types import resource_type_from_url

_DOC_HOSTS = {"kdocs.cn", "www.kdocs.cn", "docs.qq.com", "docs.weixin.qq.com"}
_URL_RE = re.compile(
    r"(?:https?://|magnet:\?|ed2k://)"
    r"[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%|\-]+",
    re.IGNORECASE,
)
_QQ_CALLBACK_RE = re.compile(
    r"^clientVarsCallback\((.*)\)\s*;?\s*$", re.DOTALL
)
_KDOCS_ID_RE = re.compile(r"/(?:l|view)/([^/?#]+)", re.IGNORECASE)
_QQ_ID_RE = re.compile(r"/(?:doc|sheet|s)/([^/?#]+)", re.IGNORECASE)


def is_online_document_url(url: str) -> bool:
    host = (urlparse(str(url or "").strip()).hostname or "").lower()
    return host in _DOC_HOSTS or any(host.endswith("." + item) for item in _DOC_HOSTS)


def _normalize_binary_text(value: bytes, encoding: str = "utf-8") -> str:
    text = value.decode(encoding, "ignore")
    return "".join(
        char if char in "\r\n\t" or unicodedata.category(char) != "Cc" else "\n"
        for char in text
    )


def _decode_base64_payload(value: str) -> bytes:
    encoded = str(value or "").strip()
    if not encoded:
        return b""
    encoded += "=" * (-len(encoded) % 4)
    raw = base64.b64decode(encoded)
    try:
        return zlib.decompress(raw)
    except zlib.error:
        return raw


def _json_strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        if value.strip():
            yield value
        return
    if isinstance(value, dict):
        for item in value.values():
            yield from _json_strings(item)
        return
    if isinstance(value, list):
        for item in value:
            yield from _json_strings(item)


def _read_varint(data: bytes, offset: int) -> Tuple[int, int]:
    result = 0
    shift = 0
    while offset < len(data) and shift < 70:
        value = data[offset]
        offset += 1
        result |= (value & 0x7F) << shift
        if value < 0x80:
            return result, offset
        shift += 7
    raise ValueError("invalid protobuf varint")


def _protobuf_fields(data: bytes) -> Optional[List[Tuple[int, int, Any]]]:
    fields: List[Tuple[int, int, Any]] = []
    offset = 0
    try:
        while offset < len(data):
            tag, offset = _read_varint(data, offset)
            field_number, wire_type = tag >> 3, tag & 7
            if not field_number:
                return None
            if wire_type == 0:
                value, offset = _read_varint(data, offset)
            elif wire_type == 1:
                if offset + 8 > len(data):
                    return None
                value, offset = data[offset:offset + 8], offset + 8
            elif wire_type == 2:
                length, offset = _read_varint(data, offset)
                if offset + length > len(data):
                    return None
                value, offset = data[offset:offset + length], offset + length
            elif wire_type == 5:
                if offset + 4 > len(data):
                    return None
                value, offset = data[offset:offset + 4], offset + 4
            else:
                return None
            fields.append((field_number, wire_type, value))
    except (IndexError, ValueError):
        return None
    return fields


def _readable_utf8(value: bytes) -> str:
    try:
        text = value.decode("utf-8")
    except UnicodeDecodeError:
        return ""
    if not text:
        return ""
    readable = sum(char.isprintable() or char in "\r\n\t" for char in text)
    return text if readable / len(text) >= 0.9 else ""


def _protobuf_texts(data: bytes, depth: int = 0) -> List[str]:
    if depth > 8:
        return []
    fields = _protobuf_fields(data)
    if fields is None:
        return []
    values: List[str] = []
    for _, wire_type, value in fields:
        if wire_type != 2:
            continue
        text = _readable_utf8(value)
        if text:
            values.append(text)
        values.extend(_protobuf_texts(value, depth + 1))
    return values


def _qq_sheet_tabs(workbook: bytes) -> List[Tuple[str, str]]:
    tabs: List[Tuple[str, str]] = []
    seen = set()

    def visit(data: bytes, depth: int = 0) -> None:
        if depth > 6:
            return
        fields = _protobuf_fields(data)
        if fields is None:
            return
        values: List[str] = []
        for _, wire_type, value in fields:
            if wire_type != 2:
                continue
            text = _readable_utf8(value)
            if text:
                values.append(text.strip())
            values.extend(item.strip() for item in _protobuf_texts(value, depth + 1))
        tab_id = next(
            (item for item in values if re.fullmatch(r"[A-Za-z0-9]{6}", item)),
            "",
        )
        tab_name = next(
            (
                item for item in values
                if item != tab_id
                   and item != "FFFFFFFF"
                   and any(ord(char) > 127 for char in item)
                   and len(item) <= 120
            ),
            "",
        )
        if tab_id and tab_name and tab_id not in seen:
            seen.add(tab_id)
            tabs.append((tab_id, tab_name.lstrip("\r\n\t !\"#$%&'()*+,-./:;")))
        for _, wire_type, value in fields:
            if wire_type == 2:
                visit(value, depth + 1)

    visit(workbook)
    return tabs


def _qq_payload_text(value: Any) -> str:
    chunks: List[str] = []
    for item in _json_strings(value):
        if re.search(r"https?://|magnet:\?|ed2k://|[\u3400-\u9fff]", item, re.IGNORECASE):
            chunks.append(item)
    for item in value or []:
        if isinstance(item, str):
            try:
                raw = _decode_base64_payload(item)
            except (ValueError, TypeError):
                continue
            chunks.extend(_protobuf_texts(raw))
            chunks.append(_normalize_binary_text(raw))
        elif isinstance(item, dict):
            for encoded in item.values():
                if not isinstance(encoded, str):
                    continue
                if re.search(r"https?://|magnet:\?|ed2k://|[\u3400-\u9fff]", encoded, re.IGNORECASE):
                    continue
                try:
                    raw = _decode_base64_payload(encoded)
                except (ValueError, TypeError):
                    continue
                chunks.extend(_protobuf_texts(raw))
                chunks.append(_normalize_binary_text(raw))
    return "\n".join(chunk for chunk in chunks if chunk)


def _parse_jsonp(text: str) -> Dict[str, Any]:
    matched = _QQ_CALLBACK_RE.match(str(text or ""))
    if not matched:
        raise ValueError("腾讯文档接口响应格式异常")
    value = json.loads(matched.group(1))
    if not isinstance(value, dict):
        raise ValueError("腾讯文档接口未返回对象")
    return value


def _parse_qq_document(
        url: str, proxy: Any, timeout: int
) -> Tuple[str, str, List[str]]:
    matched = _QQ_ID_RE.search(urlparse(url).path)
    if not matched:
        raise ValueError("腾讯文档地址缺少文档 ID")
    document_id = matched.group(1)
    session = requests.Session(impersonate="chrome")
    try:
        session.get(
            url, proxies=normalize_proxies(proxy), timeout=(8, timeout)
        ).raise_for_status()
        logger.debug(f"[ONLINE_DOCS][QQ] 分享页读取完成：id={document_id}")
        params = {
            "u": "",
            "id": document_id,
            "normal": "1",
            "outformat": "1",
            "noEscape": "1",
            "enableSmartsheetSplit": "1",
            "startrow": "0",
            "endrow": "60",
            "needSheetState": "1",
            "sliceStates": "1",
            "block_end_col": "31",
            "block_end_row": "255",
            "block_start_col": "0",
            "block_start_row": "0",
            "wb": "1",
            "nowb": "0",
            "doc_chunk_version": "3",
            "doc_chunk_flag": "1",
            "callback": "clientVarsCallback",
            "xsrf": "",
        }
        response = session.get(
            "https://docs.qq.com/dop-api/opendoc",
            params=params,
            headers={"Referer": url},
            proxies=normalize_proxies(proxy),
            timeout=(8, timeout),
        )
        response.raise_for_status()
        payload = _parse_jsonp(response.text)
        client_vars = payload.get("clientVars") or {}
        collab = client_vars.get("collab_client_vars") or {}
        pad_type = str(
            payload.get("padType")
            or (payload.get("htmlData") or {}).get("padType")
            or client_vars.get("padType")
            or collab.get("padType")
            or ""
        ).casefold()
        initial = (collab.get("initialAttributedText") or {}).get("text") or []
        logger.debug(
            f"[ONLINE_DOCS][QQ] opendoc 完成：id={document_id}，"
            f"类型={pad_type or 'unknown'}，初始块={len(initial)}"
        )
        if pad_type != "sheet":
            text = _qq_payload_text(initial)
            logger.debug(
                f"[ONLINE_DOCS][QQ] 文档正文解码完成：id={document_id}，字符={len(text)}"
            )
            return text, "document", []

        workbook = b""
        for item in initial:
            if isinstance(item, dict) and isinstance(item.get("workbook"), str):
                workbook = _decode_base64_payload(item["workbook"])
                break
        tabs = _qq_sheet_tabs(workbook) if workbook else []
        logger.debug(
            f"[ONLINE_DOCS][QQ] 表格 Tab 枚举完成：id={document_id}，数量={len(tabs)}"
        )
        text_parts = [_qq_payload_text(initial)]
        global_pad_id = str(
            collab.get("globalPadId") or client_vars.get("globalPadId") or ""
        )
        revision = collab.get("rev") or 0
        for tab_id, tab_name in tabs:
            sheet_response = session.get(
                "https://docs.qq.com/dop-api/get/sheet",
                params={
                    "padId": global_pad_id,
                    "subId": tab_id,
                    "startrow": 0,
                    "endrow": -1,
                    "outformat": 1,
                    "nowb": 1,
                    "xsrf": "",
                    "rev": revision,
                },
                headers={"Referer": url},
                proxies=normalize_proxies(proxy),
                timeout=(8, timeout),
            )
            sheet_response.raise_for_status()
            sheet_payload = sheet_response.json()
            if int(sheet_payload.get("retcode") or 0) != 0:
                raise ValueError(
                    f"腾讯表格 Tab {tab_name} 读取失败："
                    f"{sheet_payload.get('errmsg') or sheet_payload.get('retcode')}"
                )
            sheet_initial = (
                    ((sheet_payload.get("data") or {}).get("initialAttributedText") or {})
                    .get("text") or []
            )
            text_parts.append(f"\n[[SHEET:{tab_name}]]\n")
            text_parts.append(_qq_payload_text(sheet_initial))
        text = "\n".join(text_parts)
        logger.debug(
            f"[ONLINE_DOCS][QQ] 表格数据读取完成：id={document_id}，"
            f"Tab={len(tabs)}，字符={len(text)}"
        )
        return text, "spreadsheet", [name for _, name in tabs]
    finally:
        session.close()


def _wps_environment(page: str) -> Dict[str, Any]:
    marker = "window.__WPSENV__="
    offset = str(page or "").find(marker)
    if offset < 0:
        raise ValueError("金山文档页面缺少文件元数据")
    value, _ = json.JSONDecoder().raw_decode(page[offset + len(marker):])
    if not isinstance(value, dict):
        raise ValueError("金山文档文件元数据格式异常")
    return value


def _kdocs_kind(environment: Dict[str, Any]) -> Tuple[str, str]:
    file_info = environment.get("file_info") or {}
    file_data = file_info.get("file") or {}
    name = str(file_data.get("name") or "").casefold()
    extension = name.rsplit(".", 1)[-1] if "." in name else ""
    office_type = str(file_info.get("office_type") or file_data.get("office_type") or "").casefold()
    if extension in {"xls", "xlsx", "et", "ksheet"} or office_type == "s":
        return "et", "spreadsheet"
    if extension in {"otl", "xmind"} or office_type == "o":
        return "otl", "document"
    return "wps", "document"


def _parse_kdocs_document(
        url: str, proxy: Any, timeout: int
) -> Tuple[str, str, List[str]]:
    matched = _KDOCS_ID_RE.search(urlparse(url).path)
    if not matched:
        raise ValueError("金山文档地址缺少文档 ID")
    document_id = matched.group(1)
    session = requests.Session(impersonate="chrome")
    try:
        # 匿名访问只需标记已完成单点登录检查，服务端会创建访客会话。
        session.cookies.set("hadSingleSign", "1", domain=".kdocs.cn")
        response = session.get(
            url, proxies=normalize_proxies(proxy), timeout=(8, timeout)
        )
        response.raise_for_status()
        environment = _wps_environment(response.text)
        endpoint_type, document_type = _kdocs_kind(environment)
        logger.debug(
            f"[ONLINE_DOCS][KDOCS] 分享页读取完成：id={document_id}，"
            f"接口=open/{endpoint_type}，类型={document_type}"
        )
        body = {
            "connid": uuid.uuid4().hex,
            "args": {
                "password": "",
                "readonly": False,
                "modifyPassword": "",
                "sync": True,
                "startVersion": 0,
                "endVersion": 0,
                "autoSlim": True,
            },
            "ex_args": {},
        }
        open_response = session.post(
            f"https://www.kdocs.cn/api/v3/office/file/{document_id}/open/{endpoint_type}",
            json=body,
            headers={"Referer": url, "Origin": "https://www.kdocs.cn"},
            proxies=normalize_proxies(proxy),
            timeout=(8, timeout),
        )
        open_response.raise_for_status()
        raw = open_response.content
        if endpoint_type == "otl" or raw.lstrip().startswith((b"{", b"[")):
            payload = json.loads(raw.decode("utf-8"))
            text = "\n".join(_json_strings(payload))
            logger.debug(
                f"[ONLINE_DOCS][KDOCS] open/{endpoint_type} 解码完成："
                f"id={document_id}，字节={len(raw)}，字符={len(text)}"
            )
            return text, document_type, []
        text = _normalize_binary_text(raw, "utf-16le")
        sheet_names = list(dict.fromkeys(
            item.strip() for item in re.findall(r"【[^】\r\n]{1,120}】", text)
            if item.strip()
        ))
        logger.debug(
            f"[ONLINE_DOCS][KDOCS] open/{endpoint_type} 解码完成："
            f"id={document_id}，字节={len(raw)}，字符={len(text)}，Tab={len(sheet_names)}"
        )
        return text, document_type, sheet_names
    finally:
        session.close()


def _extract_links(text: str, sheet_names: List[str]) -> List[Dict[str, Any]]:
    links: List[Dict[str, Any]] = []
    seen = set()
    for matched in _URL_RE.finditer(str(text or "")):
        url = matched.group(0).rstrip(".,;，。；）)]}&")
        resource_type = resource_type_from_url(url)
        if not resource_type or url.casefold() in seen:
            continue
        seen.add(url.casefold())
        sheet_name = ""
        if sheet_names:
            preceding = [
                (text.rfind(name, 0, matched.start()), name) for name in sheet_names
            ]
            sheet_name = max(preceding, default=(-1, ""))[1]
        links.append({
            "url": url,
            "resource_type": resource_type,
            "sheet_name": sheet_name,
            "_position": matched.start(),
        })
    return links


def parse_online_document(url: str, proxy: Any = None, timeout: int = 30) -> Dict[str, Any]:
    """调用公开文档接口，返回完整正文、文档类型和资源链接。"""
    if not is_online_document_url(url):
        return {"url": url, "text": "", "links": [], "error": "不是支持的在线文档地址"}
    normalized_timeout = max(5, min(int(timeout or 30), 120))
    try:
        logger.debug(f"[ONLINE_DOCS] 开始读取：{url}")
        host = (urlparse(url).hostname or "").casefold()
        if host == "docs.qq.com" or host.endswith(".docs.qq.com"):
            text, document_type, sheet_names = _parse_qq_document(
                url, proxy, normalized_timeout
            )
        else:
            text, document_type, sheet_names = _parse_kdocs_document(
                url, proxy, normalized_timeout
            )
        return {
            "url": url,
            "text": text,
            "links": _extract_links(text, sheet_names),
            "document_type": document_type,
            "sheet_names": sheet_names,
        }
    except Exception as error:
        logger.warning(f"[ONLINE_DOCS] 接口读取失败：{url}，{error}")
        return {"url": url, "text": "", "links": [], "error": str(error)}


class OnlineDocumentClient:
    """读取公开在线文档的轻量协议客户端。"""

    def __init__(self, documents=None, resource_types=None, proxy=None, timeout=30):
        self.documents = list(documents or [])
        self.resource_types = resource_types
        self.proxy = proxy
        self.timeout = timeout

    def clear_cache(self):
        return None

    def read(self, url: str) -> Dict[str, Any]:
        return parse_online_document(url, self.proxy, self.timeout)
