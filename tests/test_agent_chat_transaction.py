"""Agent 会话删除的请求级事务边界测试。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.application.messaging.chat import AgentChatService


def _principal() -> SimpleNamespace:
    """构造拥有目标会话的普通用户。"""
    return SimpleNamespace(id=1, name="alice", is_superuser=False)


def _chat() -> SimpleNamespace:
    """构造应用服务投影所需的最小会话记录。"""
    return SimpleNamespace(
        id=9,
        session_id="session-9",
        client_session_id=None,
        title="事务会话",
        channel=None,
        source=None,
        user_id="1",
        username="alice",
        original_chat_id=None,
        message_count=0,
        created_at=None,
        updated_at=None,
        display_messages=[],
    )


@pytest.mark.asyncio
async def test_delete_stages_then_commits_once() -> None:
    """有权限的会话删除只能由 Service 在暂存成功后提交一次。"""
    calls: list[str] = []
    repository = Mock()
    repository.async_get = AsyncMock(return_value=_chat())
    repository.async_stage_delete = AsyncMock(
        side_effect=lambda **_kwargs: calls.append("stage") or True
    )
    unit_of_work = Mock()
    unit_of_work.commit = AsyncMock(
        side_effect=lambda: calls.append("commit")
    )
    unit_of_work.rollback = AsyncMock()
    service = AgentChatService(repository, unit_of_work)

    assert await service.delete("session-9", _principal()) is True

    assert calls == ["stage", "commit"]
    unit_of_work.rollback.assert_not_awaited()


@pytest.mark.asyncio
async def test_delete_rolls_back_flush_failure() -> None:
    """暂存删除失败必须回滚并传播原异常。"""
    error = RuntimeError("flush failed")
    repository = Mock()
    repository.async_get = AsyncMock(return_value=_chat())
    repository.async_stage_delete = AsyncMock(side_effect=error)
    unit_of_work = Mock()
    unit_of_work.commit = AsyncMock()
    unit_of_work.rollback = AsyncMock()
    service = AgentChatService(repository, unit_of_work)

    with pytest.raises(RuntimeError) as raised:
        await service.delete("session-9", _principal())

    assert raised.value is error
    unit_of_work.commit.assert_not_awaited()
    unit_of_work.rollback.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_delete_missing_chat_does_not_open_write_transaction() -> None:
    """会话不存在时保持旧 False 返回，且不执行 stage 或 commit。"""
    repository = Mock()
    repository.async_get = AsyncMock(return_value=None)
    repository.async_stage_delete = AsyncMock()
    unit_of_work = Mock()
    unit_of_work.commit = AsyncMock()
    unit_of_work.rollback = AsyncMock()
    service = AgentChatService(repository, unit_of_work)

    assert await service.delete("missing", _principal()) is False

    repository.async_stage_delete.assert_not_awaited()
    unit_of_work.commit.assert_not_awaited()
    unit_of_work.rollback.assert_not_awaited()
