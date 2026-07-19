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
