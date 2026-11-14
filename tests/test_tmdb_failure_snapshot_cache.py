"""
TMDB request 层业务失败响应不缓存测试(§6.4)。

TMDB 对 404 等业务失败返回合法 JSON(success=false),原实现会随快照缓存
12 小时;瞬时的服务端错误也会被同样固化,期间同 key 请求直接命中失败快照。
业务失败响应必须跳过缓存,允许下次请求重新确认。
"""

from unittest.mock import patch

from app.modules.themoviedb.tmdbv3api.tmdb import TMDb
from app.runtime.cache import cached
from tests.test_tmdb_response_cache import _FakeResponse

_NOT_FOUND_PAYLOAD = {
    "success": False,
    "status_code": 34,
    "status_message": "The resource you requested could not be found.",
}
_HEADERS = {"Content-Type": "application/json"}


def test_skip_if_prevents_caching_matching_values() -> None:
    """
    skip_if 命中的返回值不入缓存,后续调用重新执行函数。
    """
    calls = {"bad": 0, "good": 0}

    @cached(region="test_skip_if", ttl=60, skip_if=lambda value: value.get("bad"))
    def fetch(kind: str) -> dict:
        """
        记录底层调用次数，返回用于判断是否跳过缓存的标志。
        """
        calls[kind] += 1
        return {"bad": kind == "bad"}

    fetch("bad")
    fetch("bad")
    assert calls["bad"] == 2
    fetch("good")
    fetch("good")
    assert calls["good"] == 1


def _make_tmdb() -> TMDb:
    """
    构造带测试密钥的 TMDB 客户端，实际请求由各用例替换。
    """
    tmdb = TMDb()
    tmdb.api_key = "test-key"
    return tmdb


def test_business_failure_response_is_not_cached() -> None:
    """
    404 业务失败 JSON 不入缓存,同参数再次请求会重新访问 TMDB。
    """
    tmdb = _make_tmdb()
    fake = _FakeResponse(_NOT_FOUND_PAYLOAD, _HEADERS, status_code=404)
    with patch.object(TMDb, "_request_once", return_value=fake) as req:
        tmdb.request("GET", "https://api.tmdb.test/failure-not-cached", None, None)
        tmdb.request("GET", "https://api.tmdb.test/failure-not-cached", None, None)
    assert req.call_count == 2


def test_success_response_is_still_cached() -> None:
    """
    成功响应保持缓存,同参数第二次请求命中快照。
    """
    tmdb = _make_tmdb()
    fake = _FakeResponse({"id": 98865, "title": "Test"}, _HEADERS)
    with patch.object(TMDb, "_request_once", return_value=fake) as req:
        tmdb.request("GET", "https://api.tmdb.test/success-cached", None, None)
        tmdb.request("GET", "https://api.tmdb.test/success-cached", None, None)
    assert req.call_count == 1
