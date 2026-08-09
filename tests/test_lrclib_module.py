from app.core.context import MusicInfo
from app.modules.lrclib import LrclibModule


class _FakeLrclibResponse:
    """模拟 LRCLIB HTTP 响应，供解析、缓存和限流测试复用。"""

    def __init__(self, payload, status_code=200, headers=None):
        self._payload = payload
        self.status_code = status_code
        self.headers = headers or {}
        self.text = ""
        self.closed = False

    def json(self):
        """返回预设 JSON 负载。"""
        return self._payload

    def close(self):
        """记录响应已关闭。"""
        self.closed = True

    def __bool__(self):
        """复现 requests.Response 对 4xx/5xx 响应返回 False 的行为。"""
        return self.status_code < 400


def test_music_lyrics_prefers_exact_signature_and_synced_lyrics(monkeypatch) -> None:
    """元数据完整时应调用精确接口并优先返回同步歌词。"""
    module = LrclibModule()
    requested = []

    def fake_request(path, params=None):
        """记录精确查询参数并返回同步歌词。"""
        requested.append((path, params))
        return {
            "id": 3396226,
            "instrumental": False,
            "plainLyrics": "plain",
            "syncedLyrics": "[00:01.00]synced",
        }

    monkeypatch.setattr(module, "_request_json", fake_request)

    lyrics = module.music_lyrics(
        MusicInfo(
            title="I Want to Live",
            artists=["Borislav Slavov"],
            album="Baldur's Gate 3",
            duration=233,
        )
    )

    assert lyrics is not None
    assert lyrics.provider == "lrclib"
    assert lyrics.provider_id == "3396226"
    assert lyrics.content == "[00:01.00]synced"
    assert lyrics.extension == ".lrc"
    assert requested == [
        (
            "/api/get",
            {
                "track_name": "I Want to Live",
                "artist_name": "Borislav Slavov",
                "album_name": "Baldur's Gate 3",
                "duration": 233,
            },
        )
    ]


def test_music_lyrics_search_fallback_rejects_wrong_duration(monkeypatch) -> None:
    """精确接口未命中后只能选取标题、艺术家和时长均可信的搜索结果。"""
    module = LrclibModule()

    def fake_request(path, params=None):
        """精确查询返回未命中，搜索返回一条错误版本和一条正确版本。"""
        if path == "/api/get":
            return {}
        return [
            {
                "id": 1,
                "trackName": "晴天",
                "artistName": "周杰伦",
                "albumName": "演唱会",
                "duration": 310,
                "plainLyrics": "wrong",
            },
            {
                "id": 2,
                "trackName": "晴天",
                "artistName": "周杰伦",
                "albumName": "叶惠美",
                "duration": 269,
                "plainLyrics": "correct",
            },
        ]

    monkeypatch.setattr(module, "_request_json", fake_request)

    lyrics = module.music_lyrics(
        MusicInfo(
            title="晴天",
            artists=["周杰伦"],
            album="叶惠美",
            duration=270,
        )
    )

    assert lyrics is not None
    assert lyrics.provider_id == "2"
    assert lyrics.content == "correct"
    assert lyrics.extension == ".txt"


def test_request_json_honors_retry_after_once(monkeypatch) -> None:
    """LRCLIB 返回带 Retry-After 的过载响应时应等待并串行重试一次。"""
    responses = iter(
        [
            _FakeLrclibResponse(None, status_code=503, headers={"Retry-After": "2"}),
            _FakeLrclibResponse([{"id": 1}], status_code=200),
        ]
    )
    sleeps = []
    monkeypatch.setattr(LrclibModule, "_request_once", lambda *_args, **_kwargs: next(responses))
    monkeypatch.setattr("app.modules.lrclib.time.sleep", lambda seconds: sleeps.append(seconds))
    LrclibModule._request_json.cache_clear()

    result = LrclibModule._request_json(
        "/api/search",
        params={"track_name": "晴天", "artist_name": "周杰伦"},
    )

    assert result == [{"id": 1}]
    assert sleeps == [2.0]


def test_request_json_retry_network_failure_is_not_cached(monkeypatch) -> None:
    """过载重试遇到网络失败时应安全返回且不把失败写入缓存。"""
    responses = iter(
        [
            _FakeLrclibResponse(None, status_code=429, headers={"Retry-After": "0"}),
            None,
            _FakeLrclibResponse([{"id": 3}], status_code=200),
        ]
    )
    monkeypatch.setattr(LrclibModule, "_request_once", lambda *_args, **_kwargs: next(responses))
    monkeypatch.setattr("app.modules.lrclib.time.sleep", lambda _seconds: None)
    LrclibModule._request_json.cache_clear()
    params = {"track_name": "retry"}

    first = LrclibModule._request_json("/api/search", params=params)
    second = LrclibModule._request_json("/api/search", params=params)

    assert first is None
    assert second == [{"id": 3}]


def test_request_json_caches_not_found_response(monkeypatch) -> None:
    """未匹配结果也应缓存，避免整库重复刮削持续请求同一首歌。"""
    calls = {"count": 0}

    def fake_request(*_args, **_kwargs):
        """记录请求次数并返回 404。"""
        calls["count"] += 1
        return _FakeLrclibResponse(None, status_code=404)

    monkeypatch.setattr(LrclibModule, "_request_once", fake_request)
    LrclibModule._request_json.cache_clear()

    first = LrclibModule._request_json("/api/get", params={"track_name": "missing"})
    second = LrclibModule._request_json("/api/get", params={"track_name": "missing"})

    assert first == second == {}
    assert calls["count"] == 1


def test_request_json_caches_success_response(monkeypatch) -> None:
    """成功歌词响应应进入有界 TLRU 缓存，相同签名只访问一次外部 API。"""
    calls = {"count": 0}

    def fake_request(*_args, **_kwargs):
        """记录请求次数并返回固定歌词。"""
        calls["count"] += 1
        return _FakeLrclibResponse(
            {"id": 2, "syncedLyrics": "[00:01.00]晴天"},
            status_code=200,
        )

    monkeypatch.setattr(LrclibModule, "_request_once", fake_request)
    LrclibModule._request_json.cache_clear()
    params = {
        "track_name": "晴天",
        "artist_name": "周杰伦",
        "album_name": "叶惠美",
        "duration": 269,
    }

    first = LrclibModule._request_json("/api/get", params=params)
    second = LrclibModule._request_json("/api/get", params=params)

    assert first == second == {"id": 2, "syncedLyrics": "[00:01.00]晴天"}
    assert calls["count"] == 1
