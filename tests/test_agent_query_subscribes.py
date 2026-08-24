import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.agent.tools.impl.query_subscribes import QuerySubscribesTool
from app.db.models.subscribe import Subscribe
from app.schemas.types import MediaSource, MediaType


def test_agent_query_subscribes_returns_manual_total_episode():
    """订阅查询应返回手动总集数标记，供 Agent 判断元数据是否可自动刷新。"""
    subscribe = Subscribe(
        id=160,
        name="测试剧集",
        type=MediaType.TV.value,
        media_source=MediaSource.TMDB.value,
        media_id="224839",
        season=1,
        total_episode=175,
        manual_total_episode=1,
        state="P",
    )

    with patch(
        "app.agent.tools.impl.query_subscribes.get_agent_subscribe_port",
        return_value=SimpleNamespace(
            async_list=AsyncMock(return_value=[subscribe])
        ),
    ):
        result = asyncio.run(
            QuerySubscribesTool(session_id="session-1", user_id="10001").run(
                tmdb_id=224839,
            )
        )

    _, result_json = result.split("\n\n", maxsplit=1)
    payload = json.loads(result_json)
    assert payload[0]["manual_total_episode"] == 1
