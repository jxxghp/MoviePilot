from app.modules.listenbrainz import ListenBrainzModule


def test_recording_to_info_maps_listenbrainz_payload():
    """ListenBrainz 榜单录音应转换为可搜索和订阅的 MusicInfo。"""
    info = ListenBrainzModule._recording_to_info(
        {
            "artist_name": "Daft Punk",
            "listen_count": 12345,
            "recording_mbid": "recording-1",
            "release_name": "Random Access Memories",
            "caa_release_mbid": "release-1",
            "track_name": "Get Lucky",
        }
    )

    assert info is not None
    assert info.media_source.value == "musicbrainz"
    assert info.media_id == "recording-1"
    assert info.artists == ["Daft Punk"]
    assert info.album == "Random Access Memories"
    assert info.listen_count == 12345
    assert info.cover_url.endswith("/release-1/front-500")


def test_music_chart_requests_requested_page(monkeypatch):
    """音乐榜单模块应传递实体、周期、偏移量和数量并过滤无身份记录。"""
    module = ListenBrainzModule()
    requested = {}

    def fake_request(entity, range_name, offset, count):
        """记录榜单请求参数并返回一条有效录音。"""
        requested.update(entity=entity, range_name=range_name, offset=offset, count=count)
        return {
            "payload": {
                "recordings": [
                    {
                        "artist_name": "周杰伦",
                        "recording_mbid": "recording-1",
                        "track_name": "晴天",
                    },
                    {"track_name": "缺少 ID"},
                ]
            }
        }

    monkeypatch.setattr(module, "_request_chart", fake_request)

    results = module.music_chart(range_name="this_month", offset=30, count=30)

    assert requested == {
        "entity": "recordings",
        "range_name": "this_month",
        "offset": 30,
        "count": 30,
    }
    assert [item.media_id for item in results] == ["recording-1"]


def test_music_chart_supports_album_entity(monkeypatch):
    """热门专辑榜单应请求官方 release-groups 接口并返回专辑实体。"""
    module = ListenBrainzModule()
    requested = {}

    def fake_request(entity, range_name, offset, count):
        """记录请求实体并返回一条热门专辑。"""
        requested.update(entity=entity, range_name=range_name)
        return {
            "payload": {
                "release_groups": [
                    {
                        "artist_name": "BTS",
                        "artist_mbids": ["artist-1"],
                        "listen_count": 999,
                        "release_group_mbid": "release-group-1",
                        "release_group_name": "ARIRANG",
                    }
                ]
            }
        }

    monkeypatch.setattr(module, "_request_chart", fake_request)

    results = module.music_chart(range_name="week", offset=0, count=10, entity="album")

    assert requested == {"entity": "release-groups", "range_name": "week"}
    assert [item.media_id for item in results] == ["release-group-1"]
    assert results[0].music_type == "album"
    assert results[0].album_id == "release-group-1"
    assert results[0].artist_ids == ["artist-1"]


def test_music_chart_falls_back_to_supported_range(monkeypatch):
    """非官方周期应回退到默认周期，避免请求被官方接口拒绝。"""
    module = ListenBrainzModule()
    requested = {}

    def fake_request(entity, range_name, offset, count):
        """仅记录周期取值。"""
        requested.update(range_name=range_name)
        return {"payload": {"recordings": []}}

    monkeypatch.setattr(module, "_request_chart", fake_request)
    module.music_chart(range_name="last_decade")

    assert requested == {"range_name": "this_month"}


def test_music_fresh_releases_pages_official_window(monkeypatch):
    """新发行探索应按官方排序请求并在结果集上分页。"""
    module = ListenBrainzModule()
    requested = {}

    def fake_releases(days, sort, past, future):
        """记录官方新发行请求参数并返回两条发行。"""
        requested.update(days=days, sort=sort, past=past, future=future)
        return [
            {
                "artist_credit_name": "Artist A",
                "artist_mbids": ["artist-1"],
                "release_date": "2026-08-01",
                "release_group_mbid": "release-group-1",
                "release_group_primary_type": "Album",
                "release_name": "First",
            },
            {
                "artist_credit_name": "Artist B",
                "release_date": "2026-08-02",
                "release_group_mbid": "release-group-2",
                "release_name": "Second",
            },
        ]

    monkeypatch.setattr(module, "_fresh_releases", fake_releases)

    results = module.music_fresh_releases(
        days=200,
        sort="unsupported",
        past=True,
        future=False,
        offset=1,
        count=1,
    )

    assert requested == {"days": 90, "sort": "release_date", "past": True, "future": False}
    assert [item.media_id for item in results] == ["release-group-2"]
    assert results[0].music_type == "album"


class _FakeListenBrainzResponse:
    """模拟 ListenBrainz HTTP 响应，便于缓存回归测试统计网络调用次数。"""

    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = ""

    def json(self):
        """返回预设的 JSON 负载。"""
        return self._payload

    def __bool__(self):
        """模拟 requests.Response：HTTP 错误状态在布尔判断中为 False。"""
        return self.status_code < 400

    def close(self):
        """无需释放的资源。"""


def test_request_json_caches_repeated_calls(monkeypatch):
    """相同路径与参数的 ListenBrainz 请求应命中缓存，避免重复发起网络调用。"""
    import app.modules.listenbrainz as listenbrainz_module

    network_calls = {"count": 0}

    def fake_get_res(_self, url, params=None):
        """记录网络调用次数并返回固定的榜单负载。"""
        network_calls["count"] += 1
        return _FakeListenBrainzResponse(
            {"payload": {"recordings": [{"recording_mbid": "recording-cache", "track_name": "晴天"}]}}
        )

    monkeypatch.setattr(listenbrainz_module.RequestUtils, "get_res", fake_get_res)
    # 清理缓存区，排除其他用例残留
    ListenBrainzModule._request_json.cache_clear()

    first = ListenBrainzModule._request_json(
        "/stats/sitewide/recordings", params={"range": "this_week", "offset": 0, "count": 30}
    )
    second = ListenBrainzModule._request_json(
        "/stats/sitewide/recordings", params={"range": "this_week", "offset": 0, "count": 30}
    )

    assert first == second
    assert network_calls["count"] == 1


def test_request_json_does_not_cache_errors(monkeypatch):
    """失败请求返回的 None 不应缓存，以便下次重试。"""
    import app.modules.listenbrainz as listenbrainz_module

    network_calls = {"count": 0}

    def fake_get_res(_self, url, params=None):
        """始终返回 500，用于验证空结果不会被缓存。"""
        network_calls["count"] += 1
        return _FakeListenBrainzResponse(None, status_code=500)

    monkeypatch.setattr(listenbrainz_module.RequestUtils, "get_res", fake_get_res)
    ListenBrainzModule._request_json.cache_clear()

    ListenBrainzModule._request_json("/stats/sitewide/recordings", params={"range": "this_week"})
    ListenBrainzModule._request_json("/stats/sitewide/recordings", params={"range": "this_week"})

    assert network_calls["count"] == 2
