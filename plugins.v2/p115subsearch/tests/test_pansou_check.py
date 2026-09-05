# -*- coding: utf-8 -*-
"""
PanSou check_links 单元测试（standalone，无 MoviePilot 运行时依赖）
覆盖：ok/bad/locked 状态透传（客户端不做判定）、请求异常降级、404 降级
"""
import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

# 桩掉 app.log（pansou.py 顶部 from app.log import logger）
_app = types.ModuleType('app')
_log = types.ModuleType('app.log')


class _Logger:
    def info(self, *a, **k):
        pass

    def warning(self, *a, **k):
        pass

    def error(self, *a, **k):
        pass

    def debug(self, *a, **k):
        pass


_log.logger = _Logger()
sys.modules['app'] = _app
sys.modules['app.log'] = _log

PLUGIN = Path(__file__).resolve().parent.parent

spec = importlib.util.spec_from_file_location('pansou', PLUGIN / 'clients' / 'pansou.py')
pansou = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pansou)

import requests


def make_client():
    return pansou.PanSouClient(base_url='http://127.0.0.1:9080', auth_enabled=False)


def _mock_response(status_code=200, results=None):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = {'results': results if results is not None else []}
    return resp


def test_check_links_state_ok_passthrough():
    """a) ok -> 方法原样返回 state=ok 的结果项（客户端不做判定，只透传）"""
    client = make_client()
    ok_item = {'disk_type': '115', 'url': 'https://115.com/s/abcdef1234?password=u9t5', 'state': 'ok', 'summary': '有效'}
    with patch.object(pansou.requests, 'post', return_value=_mock_response(results=[ok_item])) as mock_post:
        results = client.check_links([{'disk_type': '115', 'url': 'https://115.com/s/abcdef1234?password=u9t5', 'password': 'u9t5'}])
    assert len(results) == 1
    assert results[0]['state'] == 'ok'
    # 请求体校验：端点 + items 结构
    args, kwargs = mock_post.call_args
    assert args[0] == 'http://127.0.0.1:9080/api/check/links'
    assert kwargs['json'] == {'items': [{'disk_type': '115', 'url': 'https://115.com/s/abcdef1234?password=u9t5', 'password': 'u9t5'}]}
    print('[PASS] a) ok state passthrough')


def test_check_links_state_bad_passthrough():
    """b) bad -> 返回该结果项（跳过动作由 sync 层决定）"""
    client = make_client()
    bad_item = {'disk_type': '115', 'url': 'https://115.com/swk5yvw3v4h?password=u9t5', 'state': 'bad', 'summary': '参数错误。'}
    with patch.object(pansou.requests, 'post', return_value=_mock_response(results=[bad_item])):
        results = client.check_links([{'disk_type': '115', 'url': 'https://115.com/swk5yvw3v4h?password=u9t5', 'password': 'u9t5'}])
    assert len(results) == 1
    assert results[0]['state'] == 'bad'
    print('[PASS] b) bad state passthrough')


def test_check_links_state_locked_passthrough():
    """c) locked -> 原样透传，客户端绝不把 locked 当死链（无码链接与假链接均返回 locked）"""
    client = make_client()
    locked_item = {'disk_type': '115', 'url': 'https://115.com/s/fakefake1234', 'state': 'locked', 'summary': '115 需要提取码'}
    with patch.object(pansou.requests, 'post', return_value=_mock_response(results=[locked_item])):
        results = client.check_links([{'disk_type': '115', 'url': 'https://115.com/s/fakefake1234', 'password': ''}])
    assert len(results) == 1
    assert results[0]['state'] == 'locked'
    print('[PASS] c) locked state passthrough (never treated as dead)')


def test_check_links_request_exception_degrades():
    """d) requests 异常（超时/网络）-> 返回空列表（静默降级）"""
    client = make_client()
    with patch.object(pansou.requests, 'post', side_effect=requests.exceptions.Timeout('timeout')):
        results = client.check_links([{'disk_type': '115', 'url': 'https://115.com/s/abcdef1234', 'password': ''}])
    assert results == []
    print('[PASS] d) request exception -> []')


def test_check_links_404_degrades():
    """e) 404（旧版 pansou 无该端点）-> 返回空列表（静默降级）"""
    client = make_client()
    with patch.object(pansou.requests, 'post', return_value=_mock_response(status_code=404)):
        results = client.check_links([{'disk_type': '115', 'url': 'https://115.com/s/abcdef1234', 'password': ''}])
    assert results == []
    print('[PASS] e) 404 -> []')


if __name__ == '__main__':
    test_check_links_state_ok_passthrough()
    test_check_links_state_bad_passthrough()
    test_check_links_state_locked_passthrough()
    test_check_links_request_exception_degrades()
    test_check_links_404_degrades()
    print('ALL PASS')
