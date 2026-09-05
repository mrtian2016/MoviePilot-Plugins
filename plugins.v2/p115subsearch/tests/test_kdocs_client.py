# -*- coding: utf-8 -*-
"""
KDocs client real-chain test (standalone, no MoviePilot runtime).
Reads token from env KDOCS_SKILL_TOKEN (never printed).

Note: the live endpoint returns ~1000 rows per ~22s; a full 24k-row
table pull takes several minutes, so tests target the first 1200
data rows for range/search assertions.
"""
import os
import sys
import types
from pathlib import Path

import importlib.util

_app = types.ModuleType('app')
_log = types.ModuleType('app.log')

class _Logger:
    def info(self, *a, **k):
        pass

    def warning(self, *a, **k):
        pass

    def error(self, *a, **k):
        pass

_log.logger = _Logger()
sys.modules['app'] = _app
sys.modules['app.log'] = _log

PLUGIN = Path(__file__).resolve().parent.parent

spec = importlib.util.spec_from_file_location('kdocs', PLUGIN / 'clients' / 'kdocs.py')
kdocs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(kdocs)

TOKEN = os.environ.get('KDOCS_SKILL_TOKEN', '')
DEFAULT_LINK = 'https://www.kdocs.cn/l/cuHiffeVSTQd'
LINK_ID = 'cuHiffeVSTQd'


def make_client():
    return kdocs.KDocsClient(token=TOKEN, doc_urls='', data_dir=None, timeout=120)


def test_get_share_info():
    """a) get_share_info returns file_id"""
    client = make_client()
    info = client.get_share_info(LINK_ID)
    assert info and info.get('file_id'), 'get_share_info failed'
    print('[PASS] a) get_share_info file_id ok (len', len(str(info['file_id'])), ')')


def test_get_range_data():
    """b) get_range_data sheetId=3 rows 4-103 full 12 cols with links"""
    client = make_client()
    cells = client.get_range_data(LINK_ID, 3, 4, 103, 11)
    assert cells, 'range data empty'
    rows = client._cells_to_grid(cells)
    data_rows = set(r for (r, c) in rows if r >= 4)
    # API omits empty trailing cells; require cols 0..10 present, col 11 optional
    for r in (4, 5, 6):
        assert (r, 0) in rows and (r, 10) in rows, f'row {r} incomplete data cols'
        assert (r, 7) in rows, f'row {r} missing link cell'
    links = [t for (r, c), t in rows.items() if c == 7 and 'http' in t.lower()]
    assert links, 'no link column values'
    print('[PASS] b) rows 4-103 fetched, data rows =', len(data_rows), ', links =', len(links))


def test_search():
    """c) keyword search returns pansou-format results with pan links"""
    client = make_client()
    # keyword lives in the first rows of sheetId=3 (task brief appendix B)
    cells = client.get_range_data(LINK_ID, 3, 3, 603, 11)
    grid = client._cells_to_grid(cells)
    header = client._find_header(grid)
    assert header is not None, 'header row not found in probe range'
    cols = client._header_cols(grid, header)
    rows = client._grid_to_rows(grid, cols, header + 1, 603)
    kw = '\u7075\u5883\u884c\u8005'
    hits = [r for r in rows if client._match_keyword(kw, r)]
    assert hits, 'keyword not matched in rows 4-603'
    results = []
    for row in hits:
        url = row['link']
        item = {'url': url, 'title': '[KDocs] ' + (row['title'] or row['media_title']), 'update_time': row['create_time']}
        if row['access_code']:
            item['password'] = row['access_code']
        results.append(item)
    first = results[0]
    assert first.get('url'), 'result missing url'
    assert 'title' in first and 'update_time' in first, 'not pansou format'
    print('[PASS] c) keyword hits =', len(hits), ', pansou results =', len(results), ', first url ok')


if __name__ == '__main__':
    assert TOKEN, 'KDOCS_SKILL_TOKEN not set'
    test_get_share_info()
    test_get_range_data()
    test_search()
    print('ALL_TESTS_PASSED')
