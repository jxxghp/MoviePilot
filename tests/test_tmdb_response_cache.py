import asyncio
import pickle
from threading import RLock
from unittest import TestCase

from app.modules.themoviedb.tmdbv3api.tmdb import TMDb
from app.modules.themoviedb.tmdbv3api.exceptions import TMDbException


class _FakeResponse:
    def __init__(self, payload, headers: dict, status_code: int = 200, text: str = ""):
        self._payload = payload
        self.headers = headers
        self.status_code = status_code
        self.text = text
        self._lock = RLock()

    def json(self):
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


class TmdbResponseCacheTest(TestCase):
    def test_request_returns_pickleable_snapshot(self):
        tmdb = TMDb()
        response = _FakeResponse(
            payload={"id": 1, "page": 2},
            headers={"X-RateLimit-Remaining": "39", "X-RateLimit-Reset": "1234567890"},
        )
        tmdb._req.get_res = lambda *args, **kwargs: response

        result = TMDb.request.__wrapped__(tmdb, "GET", "https://example.com", None, None)

        self.assertTrue(result[TMDb._RESPONSE_SNAPSHOT_MARKER])
        self.assertEqual(result["json"], {"id": 1, "page": 2})
        self.assertEqual(result["headers"]["X-RateLimit-Remaining"], "39")
        pickle.dumps(result)

    def test_request_rejects_scalar_json_response(self):
        """
        标量JSON响应不应进入TMDB响应缓存，避免后续按对象解析崩溃。
        """
        tmdb = TMDb()
        response = _FakeResponse(payload="upstream error", headers={})
        tmdb._req.get_res = lambda *args, **kwargs: response

        with self.assertRaisesRegex(TMDbException, "返回数据格式异常"):
            TMDb.request.__wrapped__(tmdb, "GET", "https://example.com", None, None)

    def test_request_rejects_invalid_json_response(self):
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

        with self.assertRaisesRegex(TMDbException, "不是有效JSON.*HTTP状态码：502.*bad gateway"):
            TMDb.request.__wrapped__(tmdb, "GET", "https://example.com", None, None)

    def test_request_rejects_unicode_decode_error_response(self):
        """
        错误编码的响应体也应转换为TMDbException，避免UnicodeDecodeError直接冒泡。
        """
        tmdb = TMDb()
        tmdb._req.get_res = lambda *args, **kwargs: _UnicodeDecodeErrorResponse(
            text="乱码内容不应进入日志"
        )

        with self.assertRaisesRegex(
                TMDbException,
                "不是有效JSON.*Content-Encoding：gzip.*响应内容编码异常，已省略原始内容",
        ) as cm:
            TMDb.request.__wrapped__(tmdb, "GET", "https://example.com", None, None)
        self.assertNotIn("乱码内容", str(cm.exception))

    def test_get_response_json_rejects_invalid_live_response(self):
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

        with self.assertRaisesRegex(TMDbException, "不是有效JSON.*响应内容为空"):
            TMDb._get_response_json(_InvalidJsonResponse())

    def test_async_request_returns_pickleable_snapshot(self):
        tmdb = TMDb()
        response = _FakeResponse(
            payload={"id": 2, "page": 3},
            headers={"x-ratelimit-remaining": "38", "x-ratelimit-reset": "1234567891"},
        )

        async def _fake_get_res(*args, **kwargs):
            return response

        tmdb._async_req.get_res = _fake_get_res

        result = asyncio.run(
            TMDb.async_request.__wrapped__(tmdb, "GET", "https://example.com", None, None)
        )

        self.assertTrue(result[TMDb._RESPONSE_SNAPSHOT_MARKER])
        self.assertEqual(result["json"], {"id": 2, "page": 3})
        self.assertEqual(result["headers"]["x-ratelimit-remaining"], "38")
        pickle.dumps(result)

    def test_handle_headers_accepts_snapshot_headers(self):
        tmdb = TMDb()

        tmdb._handle_headers({"x-ratelimit-remaining": "7", "x-ratelimit-reset": "99"})

        self.assertEqual(tmdb._remaining, 7)
        self.assertEqual(tmdb._reset, 99)

    def test_get_response_json_returns_snapshot_copy(self):
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

        self.assertEqual(second_json["results"][0]["media_type"], "movie")
        self.assertIsNot(first_json, second_json)
        self.assertIsNot(first_json["results"][0], second_json["results"][0])

    def test_async_request_obj_returns_copied_key_from_snapshot(self):
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
            return snapshot

        tmdb.async_request = _fake_async_request

        first_results = asyncio.run(tmdb._async_request_obj("/search/multi", key="results"))
        first_results[0]["media_type"] = "电影"

        second_results = asyncio.run(tmdb._async_request_obj("/search/multi", key="results"))

        self.assertEqual(second_results[0]["media_type"], "movie")
        self.assertIsNot(first_results, second_results)
        self.assertIsNot(first_results[0], second_results[0])

    def test_request_obj_rejects_scalar_snapshot_before_key_lookup(self):
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

        with self.assertRaisesRegex(TMDbException, "返回数据格式异常"):
            tmdb._request_obj("/search/movie", key="results")

    def test_async_request_obj_rejects_scalar_snapshot_before_key_lookup(self):
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

        with self.assertRaisesRegex(TMDbException, "返回数据格式异常"):
            asyncio.run(tmdb._async_request_obj("/search/movie", key="results"))
