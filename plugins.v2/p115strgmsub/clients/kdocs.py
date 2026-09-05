# -*- coding: utf-8 -*-
"""
KDocs 在线文档库搜索客户端。

读取金山文档在线表格数据，
按关键词匹配并提取网盘分享链接，
统一转换为盘搜格式返回。
实现依据任务书附录的实测接口契约。
"""
import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

from app.log import logger

# 开放接口地址
SKILL_HUB_URL = "https://mcp-center.wps.cn/skill_hub/api/v1/tool"
KDOCS_PAGE_URL = "https://www.kdocs.cn/l/{link_id}"
KDOCS_OPEN_ET_URL = "https://www.kdocs.cn/api/v3/office/file/{link_id}/open/et"
# 默认文档库：影巢资源分享备份
DEFAULT_DOC_URL = "https://www.kdocs.cn/l/cuHiffeVSTQd"
# 结果来源标注
CHANNEL_NAME = "KDocs\u5728\u7ebf\u6587\u6863\u5e93"
# 核心数据表名称关键字
CORE_SHEET_KEYWORD = "\u5206\u4eab\u660e\u7ec6"
# 表头列名
HDR_TYPE = "\u7c7b\u578b"
HDR_RECORD = "\u8bb0\u5f55ID"
HDR_TITLE = "\u6807\u9898"
HDR_MEDIA_TITLE = "\u5a92\u4f53\u6807\u9898"
HDR_LINK = "\u94fe\u63a5"
HDR_ACCESS_CODE = "\u8bbf\u95ee\u7801"
HDR_NOTE = "\u5907\u6ce8"
HDR_CREATE_TIME = "\u521b\u5efa\u65f6\u95f4"
# 表头定位失败时的默认列序（实测契约：十二列）
DEFAULT_COL = {
    "title": 5, "media_title": 6, "link": 7,
    "access_code": 8, "note": 9, "create_time": 10,
}
# 分批拉取与多库拉取的并发上限
BATCH_CONCURRENCY = 4
# 网盘类型识别
BUCKET_115 = "115\u7f51\u76d8"
BUCKET_QUARK = "\u5938\u514b\u7f51\u76d8"
BUCKET_BAIDU = "\u767e\u5ea6\u7f51\u76d8"
BUCKET_ALI = "\u963f\u91cc\u4e91\u76d8"
BUCKET_123 = "123\u4e91\u76d8"
BUCKET_MOBILE = "\u79fb\u52a8\u4e91\u76d8"
BUCKET_TIANYI = "\u5929\u7ffc\u4e91\u76d8"
BUCKET_OTHER = "\u5176\u4ed6\u7f51\u76d8"
_HOST_PAN_MAP = [
    ("115cdn.com", BUCKET_115),
    ("115.com", BUCKET_115),
    ("anxia.com", BUCKET_115),
    ("pan.quark.cn", BUCKET_QUARK),
    ("pan.baidu.com", BUCKET_BAIDU),
    ("alipan.com", BUCKET_ALI),
    ("aliyundrive.com", BUCKET_ALI),
    ("123pan.com", BUCKET_123),
    ("caiyun.139.com", BUCKET_MOBILE),
    ("cloud.189.cn", BUCKET_TIANYI),
]


def extract_link_id(doc_url: str) -> str:
    """从分享链接中提取 link_id（支持完整 URL 或裸分享码）。"""
    doc_url = (doc_url or "").strip()
    if not doc_url:
        return ""
    if "/" in doc_url:
        doc_url = doc_url.rstrip("/")
        link_id = doc_url.rsplit("/", 1)[-1]
    else:
        link_id = doc_url
    if link_id.startswith("l-") or link_id.isdigit():
        return ""
    return link_id


def classify_pan_url(url: str) -> str:
    """根据域名识别网盘类型，未识别时归入其他网盘。"""
    low = (url or "").lower()
    for host, bucket in _HOST_PAN_MAP:
        if host in low:
            return bucket
    return BUCKET_OTHER


class KDocsError(Exception):
    """KDocs 客户端异常。"""


class KDocsClient:
    """
    金山文档开放接口客户端。

    请求统一走技能中心端点，
    响应为 SSE 流，逐行解析出数据载荷。
    """

    def __init__(
        self,
        token: str,
        doc_urls: str = "",
        cache_ttl_hours: int = 6,
        batch_rows: int = 1000,
        cookie: str = "",
        data_dir: Optional[Path] = None,
        timeout: int = 30,
    ):
        self.token = (token or "").strip()
        self.doc_urls = (doc_urls or "").strip()
        self.cache_ttl_hours = max(1, int(cache_ttl_hours or 6))
        self.batch_rows = max(100, min(int(batch_rows or 1000), 1000))
        self.cookie = (cookie or "").strip()
        self.data_dir = Path(data_dir) if data_dir else None
        self.timeout = timeout
        self._cache_lock = threading.Lock()
        self._cache: Optional[Dict[str, List[Dict[str, Any]]]] = None
        self._cache_loaded_at: float = 0.0

    @property
    def is_ready(self) -> bool:
        """已配置 Token 即视为可用。"""
        return bool(self.token)

    @property
    def doc_url_list(self) -> List[str]:
        """解析多行文档链接配置，空配置回退默认文档库。"""
        urls = [
            u.strip() for u in self.doc_urls.splitlines()
            if u.strip()
        ]
        return urls or [DEFAULT_DOC_URL]

    # ------------------ 底层请求 ------------------

    def _post_tool(self, tool: str, args: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        调用技能中心工具端点并解析 SSE 响应。
        成功时返回 data 字段内容，失败时返回 None 并记录日志。
        """
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }
        payload = {"tool": tool, "args": args}
        try:
            resp = requests.post(
                SKILL_HUB_URL,
                headers=headers,
                json=payload,
                stream=True,
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            logger.error(f"KDocs: 请求失败: {exc.__class__.__name__}: {exc}")
            raise KDocsError("KDocs 请求失败") from exc
        if resp.status_code != 200:
            text = resp.text[:200] if resp.text else ""
            resp.close()
            logger.error(f"KDocs: 接口返回状态 {resp.status_code} {text}")
            raise KDocsError(f"KDocs 接口状态码 {resp.status_code}")
        data = None
        try:
            for raw_line in resp.iter_lines(decode_unicode=False):
                if not raw_line:
                    continue
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line.startswith("data:"):
                    continue
                body = line[5:].strip()
                if not body:
                    continue
                try:
                    item = json.loads(body)
                except (ValueError, TypeError):
                    continue
                if isinstance(item, dict) and item.get("code") == 0:
                    data = item.get("data")
                    break
        finally:
            resp.close()
        if data is None:
            raise KDocsError(f"KDocs 工具 {tool} 未返回有效数据")
        return data

    # ------------------ OpenAPI wrappers ------------------

    def get_share_info(self, link_id: str) -> Dict[str, Any]:
        """
        Fetch share doc basic info.
        """
        data = self._post_tool("get_share_info", {"link_id": link_id})
        if not isinstance(data, dict):
            return {}
        result = {
            "file_id": data.get("file_id") or "",
            "url": data.get("url") or "",
        }
        if not result["file_id"]:
            logger.warning("KDocs: get_share_info no file_id")
        return result

    def get_sheets_info(self, link_id: str) -> List[Dict[str, Any]]:
        """
        List worksheets of the doc.
        """
        data = self._post_tool("sheet.get_sheets_info", {"link_id": link_id})
        detail = data.get("detail") if isinstance(data, dict) else None
        infos = detail.get("sheetsInfo") if isinstance(detail, dict) else None
        if not isinstance(infos, list):
            logger.error("KDocs: get_sheets_info bad structure")
            return []
        sheets = []
        for info in infos:
            if not isinstance(info, dict):
                continue
            sheets.append({
                "sheetId": info.get("sheetId"),
                "sheetName": info.get("sheetName") or "",
                "rowTo": int(info.get("rowTo") or 0),
                "colTo": int(info.get("colTo") or 0),
            })
        return sheets

    def get_range_data(
        self, link_id: str, worksheet_id: int,
        row_from: int, row_to: int, col_to: int = 11,
    ) -> List[Dict[str, Any]]:
        """
        Fetch a range of cells from a worksheet.
        """
        args = {
            "link_id": link_id,
            "worksheet_id": int(worksheet_id),
            "range": {
                "rowFrom": int(row_from),
                "rowTo": int(row_to),
                "colFrom": 0,
                "colTo": int(col_to),
            },
        }
        data = self._post_tool("sheet.get_range_data", args)
        detail = data.get("detail") if isinstance(data, dict) else None
        rows = detail.get("rangeData") if isinstance(detail, dict) else None
        if not isinstance(rows, list):
            rows = []
        return rows

    # ------------------ cookie verify ------------------

    def verify_cookie(self) -> Tuple[bool, str]:
        """
        Verify the configured KDocs cookie.
        Visit the share page first, then POST the open-et endpoint;
        HTTP 200 means valid, 403 means invalid.
        """
        if not self.cookie:
            return False, "cookie-empty"
        link_id = DEFAULT_DOC_URL.rstrip('/').rsplit('/', 1)[-1]
        page_url = KDOCS_PAGE_URL.format(link_id=link_id)
        open_url = KDOCS_OPEN_ET_URL.format(link_id=link_id)
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0 Safari/537.36"
            ),
            "Referer": page_url,
            "Cookie": self.cookie,
        }
        session = requests.Session()
        try:
            session.get(page_url, headers=headers, timeout=self.timeout)
            resp = session.post(open_url, headers=headers, json={}, timeout=self.timeout)
        except requests.RequestException:
            logger.error("KDocs: cookie verify request error")
            return False, "network-error"
        finally:
            session.close()
        if resp.status_code == 200:
            return True, "cookie-valid"
        if resp.status_code == 403:
            return False, "cookie-invalid"
        return False, "status-" + str(resp.status_code)

    # ------------------ cache ------------------

    def _cache_path(self) -> Optional[Path]:
        """JSON cache file inside plugin data dir."""
        if not self.data_dir:
            return None
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            return None
        return self.data_dir / "kdocs_cache.json"

    def _load_cache(self) -> Optional[Dict[str, List[Dict[str, Any]]]]:
        """Load rows cache from disk if fresh enough."""
        if self._cache is not None and (time.time() - self._cache_loaded_at) < self.cache_ttl_hours * 3600:
            return self._cache
        path = self._cache_path()
        if not path or not path.exists():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            saved_at = float(raw.get("saved_at") or 0)
            if (time.time() - saved_at) >= self.cache_ttl_hours * 3600:
                return None
            data = raw.get("data")
            if not isinstance(data, dict):
                return None
            with self._cache_lock:
                self._cache = data
                self._cache_loaded_at = saved_at
            return data
        except Exception as exc:
            logger.error(f"KDocs: load cache failed: {exc.__class__.__name__}")
            return None

    def _save_cache(self, data: Dict[str, List[Dict[str, Any]]]) -> None:
        """Persist rows cache to disk; keep old file on failure."""
        path = self._cache_path()
        if not path:
            return
        payload = {"saved_at": time.time(), "data": data}
        tmp = path.with_suffix(".json.tmp")
        try:
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            tmp.replace(path)
            with self._cache_lock:
                self._cache = data
                self._cache_loaded_at = time.time()
        except Exception as exc:
            logger.error(f"KDocs: save cache failed: {exc.__class__.__name__}")

    # ------------------ table parsing ------------------

    @staticmethod
    def _cells_to_grid(cells: List[Dict[str, Any]]) -> Dict[Tuple[int, int], str]:
        """Convert rangeData cell list to {(row, col): text} map."""
        grid = {}
        for cell in cells:
            if not isinstance(cell, dict):
                continue
            row = cell.get("originRow")
            col = cell.get("originCol")
            if row is None or col is None:
                continue
            text = cell.get("cellText")
            if text is None:
                text = cell.get("text") or ""
            grid[(int(row), int(col))] = str(text).strip()
        return grid

    @staticmethod
    def _find_header(grid: Dict[Tuple[int, int], str]) -> Optional[int]:
        """
        Locate the header row: a row containing the link column
        title plus type/record-id markers.
        """
        if not grid:
            return None
        max_row = max(r for (r, c) in grid)
        for row in range(0, max_row + 1):
            row_text = ""
            for (r, c), t in grid.items():
                if r == row:
                    row_text += t
            if HDR_LINK in row_text:
                return row
        return None

    @staticmethod
    def _header_cols(grid: Dict[Tuple[int, int], str], header_row: int) -> Dict[str, int]:
        """Map header names to column indexes on the header row."""
        cols = dict(DEFAULT_COL)
        # single-row header is the normal case
        found = {}
        for (row, col), text in grid.items():
            if row != header_row:
                continue
            for key, name in (
                ("title", HDR_TITLE),
                ("media_title", HDR_MEDIA_TITLE),
                ("link", HDR_LINK),
                ("access_code", HDR_ACCESS_CODE),
                ("note", HDR_NOTE),
                ("create_time", HDR_CREATE_TIME),
            ):
                if name in text and key not in found:
                    found[key] = col
        for key, col in found.items():
            cols[key] = col
        return cols

    def _fetch_doc_rows(self, doc_url: str) -> Optional[List[Dict[str, Any]]]:
        """
        Fetch full data rows of one doc.
        Returns list of row dicts, or None when fetch failed.
        """
        link_id = extract_link_id(doc_url)
        if not link_id:
            logger.error("KDocs: bad doc url, skip")
            return None
        info = self.get_share_info(link_id)
        if not info.get("file_id"):
            logger.error("KDocs: get_share_info failed, skip")
            return None
        sheets = self.get_sheets_info(link_id)
        target = None
        for sheet in sheets:
            if CORE_SHEET_KEYWORD in (sheet.get("sheetName") or ""):
                target = sheet
                break
        if target is None and sheets:
            # fall back to the sheet with the most rows
            target = max(sheets, key=lambda s: s.get("rowTo") or 0)
        if target is None:
            logger.error("KDocs: no worksheet found")
            return None
        sheet_id = target.get("sheetId")
        row_to = int(target.get("rowTo") or 0)
        col_to = min(int(target.get("colTo") or 11), 11)
        if not sheet_id or row_to < 4:
            logger.error("KDocs: worksheet empty")
            return None
        # first probe batch: locate the header row
        probe_rows = min(row_to, 50)
        cells = self.get_range_data(link_id, sheet_id, 1, probe_rows, col_to)
        grid = self._cells_to_grid(cells)
        header_row = self._find_header(grid)
        if header_row is None:
            logger.error("KDocs: header row not found")
            return None
        cols = self._header_cols(grid, header_row)
        data_start = header_row + 1
        # parse the probe batch into rows
        rows = self._grid_to_rows(grid, cols, data_start, probe_rows)
        # fetch remaining batches concurrently
        if row_to > probe_rows:
            batches = []
            start = probe_rows + 1
            while start <= row_to:
                end = min(start + self.batch_rows - 1, row_to)
                batches.append((start, end))
                start = end + 1

            def fetch_batch(bounds):
                b_start, b_end = bounds
                b_cells = self.get_range_data(link_id, sheet_id, b_start, b_end, col_to)
                b_grid = self._cells_to_grid(b_cells)
                return self._grid_to_rows(b_grid, cols, b_start, b_end)

            with ThreadPoolExecutor(max_workers=BATCH_CONCURRENCY) as pool:
                futures = {pool.submit(fetch_batch, b): b for b in batches}
                for future in as_completed(futures):
                    try:
                        rows.extend(future.result())
                    except Exception as exc:
                        logger.error(f"KDocs: batch fetch failed: {exc.__class__.__name__}")
        return rows

    @staticmethod
    def _grid_to_rows(
        grid: Dict[Tuple[int, int], str],
        cols: Dict[str, int],
        row_from: int, row_to: int,
    ) -> List[Dict[str, Any]]:
        """Convert grid cells to row dicts between two row bounds."""
        out = []
        link_col = cols.get("link", DEFAULT_COL["link"])
        for row in range(row_from, row_to + 1):
            link = grid.get((row, link_col), "")
            if not link or "http" not in link.lower():
                continue
            out.append({
                "title": grid.get((row, cols.get("title", 5)), ""),
                "media_title": grid.get((row, cols.get("media_title", 6)), ""),
                "link": link,
                "access_code": grid.get((row, cols.get("access_code", 8)), ""),
                "note": grid.get((row, cols.get("note", 9)), ""),
                "create_time": grid.get((row, cols.get("create_time", 10)), ""),
            })
        return out

    def load_all_rows(self, force: bool = False) -> List[Dict[str, Any]]:
        """
        Load all rows from every configured doc,
        with TTL cache and stale-cache fallback on failure.
        """
        if not self.is_ready:
            logger.warning("KDocs: token missing, skip")
            return []
        if not force:
            cached = self._load_cache()
            if cached:
                merged = []
                for value in cached.values():
                    merged.extend(value)
                if merged:
                    logger.info(f"KDocs: cache hit, {len(merged)} rows")
                    return merged
        # fetch every doc, keep per-doc results
        per_doc: Dict[str, List[Dict[str, Any]]] = {}
        doc_urls = self.doc_url_list
        with ThreadPoolExecutor(max_workers=BATCH_CONCURRENCY) as pool:
            futures = {pool.submit(self._fetch_doc_rows, u): u for u in doc_urls}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    result = future.result()
                except Exception as exc:
                    logger.error(f"KDocs: doc fetch failed: {exc.__class__.__name__}")
                    result = None
                if result:
                    per_doc[url] = result
        if per_doc:
            self._save_cache(per_doc)
            total = sum(len(v) for v in per_doc.values())
            logger.info(f"KDocs: fetched {total} rows from {len(per_doc)} docs")
            merged = []
            for value in per_doc.values():
                merged.extend(value)
            return merged
        # all failed: fall back to stale cache
        path = self._cache_path()
        if path and path.exists():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                data = raw.get("data")
                if isinstance(data, dict):
                    merged = []
                    for value in data.values():
                        merged.extend(value)
                    if merged:
                        logger.warning(f"KDocs: fetch failed, stale cache fallback {len(merged)} rows")
                        return merged
            except Exception:
                pass
        logger.warning("KDocs: no data available")
        return []

    # ------------------ search ------------------

    @staticmethod
    def _match_keyword(keyword: str, row: Dict[str, Any]) -> bool:
        """Case-insensitive keyword match on title fields."""
        key = (keyword or "").strip().lower()
        if not key:
            return False
        hay = " ".join([
            str(row.get("title") or ""),
            str(row.get("media_title") or ""),
            str(row.get("note") or ""),
        ]).lower()
        return key in hay

    def search(self, keyword: str, only_115: bool = True) -> List[Dict[str, Any]]:
        """
        Search rows by keyword and convert hits to pansou format.
        """
        rows = self.load_all_rows()
        if not rows:
            return []
        hits = [r for r in rows if self._match_keyword(keyword, r)]
        results = []
        dropped = 0
        for row in hits:
            url = str(row.get("link") or "")
            code = str(row.get("access_code") or "")
            pan_type = classify_pan_url(url)
            if only_115 and pan_type != BUCKET_115:
                dropped += 1
                continue
            title = str(row.get("title") or row.get("media_title") or "")
            note = str(row.get("note") or "")
            if note and note not in title:
                title = f"{title} {note}"
            item = {
                "url": url,
                "title": f"[{CHANNEL_NAME}] {title}",
                "update_time": str(row.get("create_time") or ""),
            }
            if code:
                item["password"] = code
            results.append(item)
        if dropped:
            logger.info(f"KDocs: dropped {dropped} non-115 links")
        logger.info(f"KDocs: keyword matched {len(hits)} rows, kept {len(results)}")
        return results

    # ------------------ grid helpers ------------------

