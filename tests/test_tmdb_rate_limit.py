"""TMDB 限流恢复必须有界，且只使用当前响应的限流状态。"""

import asyncio
from unittest.mock import AsyncMock, Mock

import pytest
import requests

from app.modules.themoviedb.tmdbv3api import tmdb as tmdb_module
from app.modules.themoviedb.tmdbv3api.exceptions import TMDbConnectionError, TMDbException
from app.modules.themoviedb.tmdbv3api.tmdb import TMDb


@pytest.fixture(params=[False, True], ids=['sync', 'async'])
def client_case(request, monkeypatch):
    """同时驱动真实同步、异步缓存入口，只替换单次传输与等待。"""
    client = TMDb()
    client.api_key = 'test-key'
    client.domain = 'tmdb.example.test'
    transport = AsyncMock() if request.param else Mock()
    sleep = AsyncMock() if request.param else Mock()
    monkeypatch.setattr(tmdb_module.time, 'time', lambda: 1000)
    if request.param:
        monkeypatch.setattr(client, '_async_request_once', transport)
        monkeypatch.setattr(tmdb_module.asyncio, 'sleep', sleep)
    else:
        monkeypatch.setattr(client, '_request_once', transport)
        monkeypatch.setattr(tmdb_module.time, 'sleep', sleep)

    def invoke(method='GET'):
        """在同一断言中调用同步或异步的完整响应解析入口。"""
        if request.param:
            return asyncio.run(client._async_request_obj('/movie/42', method=method))
        return client._request_obj('/movie/42', method=method)

    client.cache_clear()
    asyncio.run(client.async_request.cache_clear())
    yield client, transport, sleep, invoke
    client.cache_clear()
    asyncio.run(client.async_request.cache_clear())
    client.close()


def _response(headers):
    """返回合法成功响应，保证失败来自限流处理而非响应解析。"""
    return Mock(headers=headers, json=Mock(return_value={'id': 42}))


@pytest.mark.parametrize('method', ['GET', 'POST'])
@pytest.mark.parametrize('recovered_headers', [{}, {'X-RateLimit-Remaining': '39'}])
def test_rate_limit_recovery_uses_current_headers(client_case, method, recovered_headers):
    """重试绕过旧缓存，且成功响应缺少限流头时不继承上次耗尽状态。"""
    _, transport, sleep, invoke = client_case
    transport.side_effect = [
        _response({'X-RateLimit-Remaining': '0', 'X-RateLimit-Reset': '1002'}),
        _response(recovered_headers),
    ]

    assert invoke(method) == {'id': 42}
    assert transport.call_count == 2
    sleep.assert_called_once_with(2)


def test_persistent_rate_limit_stops_after_one_retry(client_case):
    """持续限流至多重试一次，避免递归最终在 Cookie 等深层调用中爆栈。"""
    _, transport, sleep, invoke = client_case
    transport.return_value = _response(
        {'X-RateLimit-Remaining': '0', 'X-RateLimit-Reset': '1002'}
    )

    with pytest.raises(TMDbException, match='达到请求频率限制') as error:
        invoke()

    assert not isinstance(error.value, TMDbConnectionError)
    assert transport.call_count == 2
    sleep.assert_called_once_with(2)


@pytest.mark.parametrize('reset', ['998', '1000'])
def test_expired_rate_limit_does_not_sleep(client_case, reset):
    """已到期的重置时间不能取绝对值后继续等待和重试。"""
    _, transport, sleep, invoke = client_case
    transport.return_value = _response(
        {'X-RateLimit-Remaining': '0', 'X-RateLimit-Reset': reset}
    )

    assert invoke() == {'id': 42}
    assert transport.call_count == 1
    sleep.assert_not_called()


@pytest.mark.parametrize('wait_enabled', [False, True])
def test_incomplete_rate_limit_has_no_guessed_wait(client_case, wait_enabled):
    """没有重置时间时报告限流，避免沿用旧时间或发生 None 减法异常。"""
    client, transport, sleep, invoke = client_case
    client.wait_on_rate_limit = wait_enabled
    client._reset = 1002
    transport.return_value = _response({'X-RateLimit-Remaining': '0'})

    with pytest.raises(TMDbException, match='达到请求频率限制'):
        invoke()

    assert transport.call_count == 1
    sleep.assert_not_called()


def test_disabled_wait_reports_rate_limit_immediately(client_case):
    """调用方关闭限流等待时，不执行重试或休眠。"""
    client, transport, sleep, invoke = client_case
    client.wait_on_rate_limit = False
    transport.return_value = _response(
        {'X-RateLimit-Remaining': '0', 'X-RateLimit-Reset': '1002'}
    )

    with pytest.raises(TMDbException, match='达到请求频率限制'):
        invoke()

    assert transport.call_count == 1
    sleep.assert_not_called()


def test_real_cookie_session_recovers_after_rate_limit(monkeypatch):
    """保留真实 Session/Cookie 准备，验证缺失限流头的代理响应不会引发递归。"""
    sent = []

    def send(_adapter, request, **kwargs):
        """仅替换网络发送，依次返回耗尽配额与无配额头的成功响应。"""
        sent.append(request)
        assert request.headers['Cookie'] == 'probe=present'
        assert kwargs['proxies']['https'] == 'http://proxy.example.test:8080'
        response = requests.Response()
        response.request = request
        response.url = request.url
        response.status_code = 200
        response._content = b'{"id": 42}'
        if len(sent) == 1:
            response.headers.update({
                'X-RateLimit-Remaining': '0',
                'X-RateLimit-Reset': '1002',
            })
        return response

    monkeypatch.setattr(requests.adapters.HTTPAdapter, 'send', send)
    monkeypatch.setattr(tmdb_module.time, 'time', lambda: 1000)
    sleep = Mock()
    monkeypatch.setattr(tmdb_module.time, 'sleep', sleep)
    with requests.Session() as session:
        session.trust_env = False
        session.cookies.set('probe', 'present', domain='tmdb.example.test', path='/')
        client = TMDb(session=session)
        client.api_key = 'test-key'
        client.domain = 'tmdb.example.test'
        client._req._proxies = {'https': 'http://proxy.example.test:8080'}
        try:
            assert client._request_obj('/movie/42', call_cached=False) == {'id': 42}
            assert len(sent) == 2
            sleep.assert_called_once_with(2)
        finally:
            client.cache_clear()
