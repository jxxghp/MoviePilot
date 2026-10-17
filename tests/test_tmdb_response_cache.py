"""
验证TMDB 响应缓存键、命中和更新行为。
"""

import asyncio
import pickle
from threading import RLock
from unittest.mock import Mock, patch

import pytest

from app.modules.themoviedb.tmdbapi import TmdbApi
from app.modules.themoviedb.tmdbv3api.exceptions import TMDbException
from app.modules.themoviedb.tmdbv3api.tmdb import TMDb


class _FakeResponse:
    """
    测试用响应对象，模拟 requests/httpx 响应的最小接口。
    """

    def __init__(self, payload, headers: dict, status_code: int = 200, text: str = ""):
        """
        初始化响应内容。
        """
        self._payload = payload
        self.headers = headers
        self.status_code = status_code
        self.text = text
        self._lock = RLock()

    def json(self):
        """
        返回预置JSON内容。
        """
        return self._payload


class _UnicodeDecodeErrorResponse:
    """
    模拟 httpx.Response.json() 直接抛 UnicodeDecodeError 的异常响应。
    """

    def __init__(self, content: bytes = b"\x8b", text: str = ""):
        """
        初始化一个带有压缩响应特征的伪响应对象。
        """
        self.headers = {"Content-Type": "application/json", "Content-Encoding": "gzip"}
        self.status_code = 200
        self.text = text
        self.content = content

    def json(self):
        """
        模拟 httpx.Response.json() 在遇到错误编码响应时直接抛出 UnicodeDecodeError。
        """
        raise UnicodeDecodeError("utf-8", b"\x8b", 1, 2, "invalid start byte")


def test_request_returns_pickleable_snapshot():
    """
    TMDB同步响应应转换为可序列化快照。
    """
    tmdb = TMDb()
    response = _FakeResponse(
        payload={"id": 1, "page": 2},
        headers={"X-RateLimit-Remaining": "39", "X-RateLimit-Reset": "1234567890"},
    )
    tmdb._req.get_res = lambda *args, **kwargs: response

    result = TMDb.request.__wrapped__(tmdb, "GET", "https://example.com", None, None)

    assert result[TMDb._RESPONSE_SNAPSHOT_MARKER]
    assert result["json"] == {"id": 1, "page": 2}
    assert result["headers"]["X-RateLimit-Remaining"] == "39"
    pickle.dumps(result)


def test_request_rebuilds_owned_session_after_connection_failure():
    """
    自持有Session的TMDB同步请求失败后，应重建会话并重试一次。
    """
    tmdb = TMDb()
    response = _FakeResponse(payload={"id": 1}, headers={})
    request_results = [None, response]
    sessions = []
    requests = []

    def _fake_init_session(session=None):
        """
        构造可观测的假Session和RequestUtils。
        """
        fake_session = Mock()
        fake_request = Mock()
        fake_request.get_res.side_effect = lambda *args, **kwargs: request_results.pop(0)
        tmdb._session = fake_session
        tmdb._req = fake_request
        sessions.append(fake_session)
        requests.append(fake_request)

    tmdb._init_session = _fake_init_session
    tmdb._init_session()
    tmdb._request_once = lambda method, url, data, json: tmdb._req.get_res(url)

    result = TMDb.request.__wrapped__(tmdb, "GET", "https://example.com", None, None)

    assert result["json"] == {"id": 1}
    assert len(sessions) == 2
    sessions[0].close.assert_called_once_with()
    assert requests[0].get_res.call_count == 1
    assert requests[1].get_res.call_count == 1


def test_async_request_utils_disables_http2_for_tmdb():
    """
    TMDB异步请求客户端应关闭HTTP/2，避免共享h2长连接异常影响媒体识别。
    """
    with patch("app.modules.themoviedb.tmdbv3api.tmdb.AsyncRequestUtils") as async_request_utils:
        TMDb()

    assert async_request_utils.call_args.kwargs["http2"] is False


def test_request_rejects_scalar_json_response():
    """
    标量JSON响应不应进入TMDB响应缓存，避免后续按对象解析崩溃。
    """
    tmdb = TMDb()
    response = _FakeResponse(payload="upstream error", headers={})
    tmdb._req.get_res = lambda *args, **kwargs: response

    with pytest.raises(TMDbException, match="返回数据格式异常"):
        TMDb.request.__wrapped__(tmdb, "GET", "https://example.com", None, None)


def test_request_rejects_invalid_json_response():
    """
    非JSON响应应转换为TMDbException，调用方可按连接异常统一处理。
    """

    class _InvalidJsonResponse:
        headers = {"Content-Type": "text/html"}
        status_code = 502
        text = "<html>bad gateway</html>"

        def json(self):
            """
            模拟上游返回无法解析为JSON的响应体。
            """
            raise ValueError("invalid json")

    tmdb = TMDb()
    tmdb._req.get_res = lambda *args, **kwargs: _InvalidJsonResponse()

    with pytest.raises(TMDbException, match="不是有效JSON.*HTTP状态码：502.*bad gateway"):
        TMDb.request.__wrapped__(tmdb, "GET", "https://example.com", None, None)


def test_request_rejects_unicode_decode_error_response():
    """
    错误编码的响应体也应转换为TMDbException，避免UnicodeDecodeError直接冒泡。
    """
    tmdb = TMDb()
    tmdb._req.get_res = lambda *args, **kwargs: _UnicodeDecodeErrorResponse(
        text="乱码内容不应进入日志"
    )

    with pytest.raises(
        TMDbException,
        match="不是有效JSON.*Content-Encoding：gzip.*响应内容编码异常，已省略原始内容",
    ) as exc_info:
        TMDb.request.__wrapped__(tmdb, "GET", "https://example.com", None, None)
    assert "乱码内容" not in str(exc_info.value)


def test_get_response_json_rejects_invalid_live_response():
    """
    未缓存的实时响应解析失败时也应输出统一诊断信息。
    """

    class _InvalidJsonResponse:
        headers = {}
        status_code = 200
        text = ""

        def json(self):
            """
            模拟HTTP 200但响应体为空的情况。
            """
            raise ValueError("empty")

    with pytest.raises(TMDbException, match="不是有效JSON.*响应内容为空"):
        TMDb._get_response_json(_InvalidJsonResponse())


def test_async_request_returns_pickleable_snapshot():
    """
    TMDB异步响应应转换为可序列化快照。
    """
    tmdb = TMDb()
    response = _FakeResponse(
        payload={"id": 2, "page": 3},
        headers={"x-ratelimit-remaining": "38", "x-ratelimit-reset": "1234567891"},
    )

    async def _fake_get_res(*args, **kwargs):
        """
        返回预置异步响应。
        """
        return response

    tmdb._async_req.get_res = _fake_get_res

    result = asyncio.run(
        TMDb.async_request.__wrapped__(tmdb, "GET", "https://example.com", None, None)
    )

    assert result[TMDb._RESPONSE_SNAPSHOT_MARKER]
    assert result["json"] == {"id": 2, "page": 3}
    assert result["headers"]["x-ratelimit-remaining"] == "38"
    pickle.dumps(result)


def test_handle_headers_accepts_snapshot_headers():
    """
    快照响应头应能参与限流信息解析。
    """
    tmdb = TMDb()

    tmdb._handle_headers({"x-ratelimit-remaining": "7", "x-ratelimit-reset": "99"})

    assert tmdb._remaining == 7
    assert tmdb._reset == 99


def test_get_response_json_returns_snapshot_copy():
    """
    缓存快照读取时应返回副本，避免调用方原地修改污染缓存。
    """
    snapshot = {
        TMDb._RESPONSE_SNAPSHOT_MARKER: True,
        "headers": {},
        "json": {
            "results": [
                {"id": 1, "media_type": "movie"},
                {"id": 2, "media_type": "tv"},
            ]
        },
    }

    first_json = TMDb._get_response_json(snapshot)
    first_json["results"][0]["media_type"] = "电影"

    second_json = TMDb._get_response_json(snapshot)

    assert second_json["results"][0]["media_type"] == "movie"
    assert first_json is not second_json
    assert first_json["results"][0] is not second_json["results"][0]


def test_async_request_obj_returns_copied_key_from_snapshot():
    """
    异步对象请求从快照取子字段时也应返回副本。
    """
    tmdb = TMDb()
    snapshot = {
        TMDb._RESPONSE_SNAPSHOT_MARKER: True,
        "headers": {"x-ratelimit-remaining": "39", "x-ratelimit-reset": "1234567890"},
        "json": {
            "page": 1,
            "results": [
                {"id": 1, "media_type": "movie"},
                {"id": 2, "media_type": "tv"},
            ],
        },
    }

    async def _fake_async_request(*args, **kwargs):
        """
        返回预置异步快照。
        """
        return snapshot

    tmdb.async_request = _fake_async_request

    first_results = asyncio.run(tmdb._async_request_obj("/search/multi", key="results"))
    first_results[0]["media_type"] = "电影"

    second_results = asyncio.run(tmdb._async_request_obj("/search/multi", key="results"))

    assert second_results[0]["media_type"] == "movie"
    assert first_results is not second_results
    assert first_results[0] is not second_results[0]


def test_request_obj_rejects_scalar_snapshot_before_key_lookup():
    """
    旧缓存中的标量快照不应在读取results字段时触发AttributeError。
    """
    tmdb = TMDb()
    snapshot = {
        TMDb._RESPONSE_SNAPSHOT_MARKER: True,
        "headers": {"x-ratelimit-remaining": "39", "x-ratelimit-reset": "1234567890"},
        "json": "upstream error",
    }
    tmdb.request = lambda *args, **kwargs: snapshot

    with pytest.raises(TMDbException, match="返回数据格式异常"):
        tmdb._request_obj("/search/movie", key="results")


def test_async_request_obj_rejects_scalar_snapshot_before_key_lookup():
    """
    异步对象请求读取旧标量快照时也应走统一TMDB异常路径。
    """
    tmdb = TMDb()
    snapshot = {
        TMDb._RESPONSE_SNAPSHOT_MARKER: True,
        "headers": {"x-ratelimit-remaining": "39", "x-ratelimit-reset": "1234567890"},
        "json": "upstream error",
    }

    async def _fake_async_request(*args, **kwargs):
        """
        模拟异步请求命中已缓存的异常快照。
        """
        return snapshot

    tmdb.async_request = _fake_async_request

    with pytest.raises(TMDbException, match="返回数据格式异常"):
        asyncio.run(tmdb._async_request_obj("/search/movie", key="results"))


def test_tmdb_api_close_closes_all_clients():
    """
    TmdbApi关闭时应释放所有子客户端的同步连接池。
    """
    tmdb_api = TmdbApi()
    clients = [
        tmdb_api.tmdb,
        tmdb_api.search,
        tmdb_api.movie,
        tmdb_api.tv,
        tmdb_api.season_obj,
        tmdb_api.episode_obj,
        tmdb_api.discover,
        tmdb_api.trending,
        tmdb_api.person,
        tmdb_api.collection,
    ]
    for client in clients:
        client.close = Mock()

    tmdb_api.close()

    for client in clients:
        client.close.assert_called_once_with()
