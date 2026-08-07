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
    assert info.source == "musicbrainz"
    assert info.media_id == "recording-1"
    assert info.artists == ["Daft Punk"]
    assert info.album == "Random Access Memories"
    assert info.listen_count == 12345
    assert info.cover_url.endswith("/release-1/front-500")


def test_music_chart_requests_requested_page(monkeypatch):
    """音乐榜单模块应传递周期、偏移量和数量并过滤无身份记录。"""
    module = ListenBrainzModule()
    requested = {}

    def fake_request(range_name, offset, count):
        """记录榜单请求参数并返回一条有效录音。"""
        requested.update(range_name=range_name, offset=offset, count=count)
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

    assert requested == {"range_name": "this_month", "offset": 30, "count": 30}
    assert [item.media_id for item in results] == ["recording-1"]
