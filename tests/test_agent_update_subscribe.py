import asyncio
import json
from contextlib import asynccontextmanager
from unittest.mock import patch

from app.agent.tools.impl.update_subscribe import UpdateSubscribeTool
from app.application.subscription.mutation import SubscriptionMutation
from app.schemas.types import MediaType


def test_agent_update_subscribe_sends_modified_event_payload_with_agent_scene():
    """
    Agent 更新订阅后只发送 modify 事件，并标记 agent_update 场景。
    """
    subscribe = _AgentSubscribe(id=9, name="旧标题", state="R", total_episode=8)
    oper = _SubscribeOperStub(subscribe)

    mutation = _MutationServiceStub(oper)
    with patch(
        "app.agent.tools.impl.update_subscribe.get_subscription_mutation_scope",
        side_effect=lambda: _mutation_scope(mutation),
    ):
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
    assert mutation.calls == [(9, {"name": "新标题", "state": "S"}, "agent_update")]


def test_agent_update_subscribe_ignores_unchanged_total_episode():
    """Agent 回传相同总集数时，不应产生数据库写入或订阅调整事件。"""
    subscribe = _AgentSubscribe(
        id=160,
        name="测试剧集",
        type=MediaType.TV.value,
        state="R",
        total_episode=175,
        lack_episode=0,
        manual_total_episode=0,
    )
    oper = _SubscribeOperStub(subscribe)

    mutation = _MutationServiceStub(oper)
    with patch(
        "app.agent.tools.impl.update_subscribe.get_subscription_mutation_scope",
        side_effect=lambda: _mutation_scope(mutation),
    ):
        result = asyncio.run(
            UpdateSubscribeTool(session_id="session-1", user_id="10001").run(
                subscribe_id=160,
                total_episode=175,
            )
        )

    payload = json.loads(result)
    assert payload == {"success": False, "message": "没有提供要更新的字段"}
    assert oper.updates == []
    assert mutation.calls == []


def test_agent_update_subscribe_only_updates_other_fields_with_unchanged_total_episode():
    """Agent 同时回传相同总集数和洗版设置时，只更新实际请求的其他字段。"""
    subscribe = _AgentSubscribe(
        id=160,
        name="测试剧集",
        type=MediaType.TV.value,
        state="R",
        total_episode=175,
        lack_episode=0,
        manual_total_episode=0,
        best_version=0,
    )
    oper = _SubscribeOperStub(subscribe)

    mutation = _MutationServiceStub(oper)
    with patch(
        "app.agent.tools.impl.update_subscribe.get_subscription_mutation_scope",
        side_effect=lambda: _mutation_scope(mutation),
    ):
        result = asyncio.run(
            UpdateSubscribeTool(session_id="session-1", user_id="10001").run(
                subscribe_id=160,
                total_episode=175,
                best_version=1,
            )
        )

    payload = json.loads(result)
    assert payload["success"] is True
    assert payload["updated_fields"] == ["best_version"]
    assert payload["subscribe"]["manual_total_episode"] == 0
    assert oper.updates == [(160, {"best_version": 1})]
    assert mutation.calls == [(160, {"best_version": 1}, "agent_update")]


def test_agent_update_subscribe_marks_changed_total_episode_as_manual():
    """Agent 真正修改总集数时，保持 Web API 的手动总集数语义。"""
    subscribe = _AgentSubscribe(
        id=160,
        name="测试剧集",
        type=MediaType.TV.value,
        state="R",
        total_episode=175,
        lack_episode=0,
        manual_total_episode=0,
    )
    oper = _SubscribeOperStub(subscribe)

    mutation = _MutationServiceStub(oper)
    with patch(
        "app.agent.tools.impl.update_subscribe.get_subscription_mutation_scope",
        side_effect=lambda: _mutation_scope(mutation),
    ):
        result = asyncio.run(
            UpdateSubscribeTool(session_id="session-1", user_id="10001").run(
                subscribe_id=160,
                total_episode=190,
            )
        )

    payload = json.loads(result)
    assert payload["success"] is True
    assert payload["subscribe"]["manual_total_episode"] == 1
    assert oper.updates == [
        (
            160,
            {
                "total_episode": 190,
                "lack_episode": 15,
                "manual_total_episode": 1,
            },
        )
    ]


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


class _MutationServiceStub:
    """让 Agent 工具测试观察事务化修改服务收到的最终 payload。"""

    def __init__(self, oper):
        """保存内存 Oper 与调用记录。"""
        self.oper = oper
        self.calls = []

    async def get_accessible(self, subscribe_id, _actor):
        """模拟事务作用域内的权限读取。"""
        return await self.oper.async_get(subscribe_id)

    async def update(self, subscribe_id, payload, _actor, scene="update"):
        """模拟事务化更新并返回稳定快照。"""
        old = self.oper.subscribe.to_dict()
        updated = await self.oper.async_update(subscribe_id, payload)
        self.calls.append((subscribe_id, dict(payload), scene))
        return SubscriptionMutation(
            old=old,
            new=updated.to_dict(),
            event_published=True,
        )


@asynccontextmanager
async def _mutation_scope(service):
    """把测试修改服务包装成 Agent 使用的异步事务作用域。"""
    yield service
