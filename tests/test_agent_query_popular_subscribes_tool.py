import asyncio
import json

from app.agent.tools.impl.query_popular_subscribes import QueryPopularSubscribesTool


def test_popular_subscribe_title_distinguishes_special_season_zero(monkeypatch):
    """热门订阅结果应在标题中明确标识特别季，同时保留数值季号。"""
    async def fake_statistics(**_kwargs):
        return [{
            "type": "tv",
            "name": "Demo Show",
            "season": 0,
            "tmdbid": 1,
            "count": 5,
        }]

    monkeypatch.setattr(
        "app.agent.tools.impl.query_popular_subscribes."
        "MoviePilotServerHelper.async_get_subscribe_statistic",
        fake_statistics,
    )

    tool = QueryPopularSubscribesTool(session_id="session-1", user_id="10001")
    result = asyncio.run(tool.run(media_type="tv"))
    payload = json.loads(result.split("\n\n", 1)[1])

    assert payload[0]["title"] == "Demo Show 第零季"
    assert payload[0]["season"] == 0


def test_popular_music_subscribes_can_filter_complete_albums(monkeypatch):
    """热门音乐订阅应能区分单曲和整张专辑。"""
    async def fake_statistics(**_kwargs):
        """返回一条单曲和一条专辑统计。"""
        return [
            {
                "type": "music",
                "name": "晴天",
                "music_type": "recording",
                "media_source": "musicbrainz",
                "media_id": "recording-1",
                "count": 8,
            },
            {
                "type": "音乐",
                "name": "叶惠美",
                "music_type": "album",
                "media_source": "musicbrainz",
                "media_id": "release-group-1",
                "total_tracks": 11,
                "count": 6,
            },
        ]

    monkeypatch.setattr(
        "app.agent.tools.impl.query_popular_subscribes."
        "MoviePilotServerHelper.async_get_subscribe_statistic",
        fake_statistics,
    )

    tool = QueryPopularSubscribesTool(session_id="session-1", user_id="10001")
    result = asyncio.run(
        tool.run(media_type="music", music_type="album")
    )
    payload = json.loads(result.split("\n\n", 1)[1])

    assert len(payload) == 1
    assert payload[0]["music_type"] == "album"
    assert payload[0]["total_tracks"] == 11
    assert payload[0]["media_id"] == "release-group-1"
