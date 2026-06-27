import asyncio
import json
from unittest.mock import AsyncMock, patch

from app.agent.tools.impl.update_subscribe import UpdateSubscribeTool
from app.schemas.types import EventType


def test_agent_update_subscribe_sends_modified_event_payload_with_agent_scene():
    """
    Agent 更新订阅后只发送 modify 事件，并标记 agent_update 场景。
    """
    subscribe = _AgentSubscribe(id=9, name="旧标题", state="R", total_episode=8)
    oper = _SubscribeOperStub(subscribe)

    with patch(
        "app.agent.tools.impl.update_subscribe.SubscribeOper",
        return_value=oper,
    ), patch(
        "app.agent.tools.impl.update_subscribe.eventmanager.async_send_event",
        new=AsyncMock(),
    ) as send_event:
        result = asyncio.run(
            UpdateSubscribeTool(session_id="session-1", user_id="10001").run(
                subscribe_id=9,
                name="新标题",
                state="S",
            )
        )

    payload = json.loads(result)
    assert payload["success"] is True
    assert oper.updates == [(9, {"name": "新标题", "state": "S"})]
    send_event.assert_awaited_once()
    event_type, event_payload = send_event.await_args.args
    assert event_type == EventType.SubscribeModified
    assert event_payload["subscribe_id"] == 9
    assert event_payload["scene"] == "agent_update"
    assert event_payload["fields"] == ["name", "state"]
    assert event_payload["old_subscribe_info"]["name"] == "旧标题"
    assert event_payload["subscribe_info"]["name"] == "新标题"


class _AgentSubscribe:
    """
    最小订阅替身，模拟 Agent 工具依赖的订阅对象接口。
    """

    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)

    def __getattr__(self, item):
        return None

    def to_dict(self):
        return dict(self.__dict__)


class _SubscribeOperStub:
    """
    内存订阅操作替身，记录工具最终提交的更新字段。
    """

    def __init__(self, subscribe):
        self.subscribe = subscribe
        self.updates = []

    async def async_get(self, subscribe_id):
        return self.subscribe if subscribe_id == self.subscribe.id else None

    async def async_update(self, subscribe_id, payload):
        self.updates.append((subscribe_id, dict(payload)))
        self.subscribe.__dict__.update(payload)
        return self.subscribe
