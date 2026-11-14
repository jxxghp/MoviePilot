"""
订阅删除快照进入宿主事件总线时的容器兼容合同。
"""

from types import MappingProxyType
from unittest.mock import AsyncMock

import pytest

from app.api.dependencies import subscription
from app.application.subscription.contract import SubscribeDeletionCandidate
from app.schemas.types import EventType


@pytest.mark.asyncio
@pytest.mark.parametrize("frozen", [False, True])
async def test_deleted_event_preserves_existing_dictionary_identity(monkeypatch, frozen):
    """
    普通字典和领域冻结字典保持身份及原有只读语义。
    """
    payload = {"id": 7, "name": "订阅", "filters": {"quality": ["1080p"]}}
    if frozen:
        payload = SubscribeDeletionCandidate(7, None, payload).event_payload
    publish = AsyncMock()
    monkeypatch.setattr(subscription.eventmanager, "async_send_event", publish)

    await subscription._publish_subscribe_deleted(payload)

    publish.assert_awaited_once_with(EventType.SubscribeDeleted, payload)
    assert publish.call_args.args[1] is payload
    if frozen:
        with pytest.raises(TypeError, match="不可修改"):
            publish.call_args.args[1]["id"] = 8


@pytest.mark.asyncio
async def test_deleted_event_accepts_read_only_mapping_without_mutating_source(monkeypatch):
    """
    只读 Mapping 在边界转换为字典，保留字段与嵌套对象。
    """
    nested = {"quality": ["1080p"]}
    source = {"id": 7, "filters": nested}
    payload = MappingProxyType(source)
    publish = AsyncMock()
    monkeypatch.setattr(subscription.eventmanager, "async_send_event", publish)

    await subscription._publish_subscribe_deleted(payload)

    publish.assert_awaited_once_with(EventType.SubscribeDeleted, source)
    event_payload = publish.call_args.args[1]
    assert type(event_payload) is dict
    assert event_payload is not source
    assert event_payload["filters"] is nested
    event_payload["id"] = 8
    assert source["id"] == 7


@pytest.mark.asyncio
@pytest.mark.parametrize("read_only", [False, True])
async def test_deleted_event_preserves_publish_failure(monkeypatch, read_only):
    """
    事件发送失败原样向上传播，供既有 outbox 重试逻辑处理。
    """
    payload = {"id": 7}
    if read_only:
        payload = MappingProxyType(payload)
    failure = RuntimeError("事件总线停止")
    monkeypatch.setattr(
        subscription.eventmanager, "async_send_event", AsyncMock(side_effect=failure)
    )

    with pytest.raises(RuntimeError) as raised:
        await subscription._publish_subscribe_deleted(payload)

    assert raised.value is failure
