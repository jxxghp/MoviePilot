"""Agent 删除订阅工具的事务作用域委托测试。"""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from app.agent.tools.impl.delete_subscribe import DeleteSubscribeTool


@asynccontextmanager
async def _scope(value):
    """把测试替身包装成工具使用的异步作用域。"""
    yield value


def test_agent_delete_subscribe_uses_transactional_delete_command():
    """Agent 删除必须委托带 UoW/outbox 的应用命令，不能直接调用 Oper。"""
    subscribe = SimpleNamespace(id=7, name="测试订阅", year="2026")
    mutation = SimpleNamespace(get_accessible=AsyncMock(return_value=subscribe))
    command = SimpleNamespace(execute=AsyncMock(return_value=True))
    data = SimpleNamespace(
        subscription_mutation_scope=lambda: _scope(mutation),
        subscription_delete_scope=lambda: _scope(command),
    )

    result = asyncio.run(
        DeleteSubscribeTool(
            session_id="session-1",
            user_id="10001",
            data=data,
        ).run(
            subscribe_id=7
        )
    )

    assert result == "成功删除订阅：测试订阅 (2026)"
    mutation.get_accessible.assert_awaited_once()
    command.execute.assert_awaited_once()
    subscribe_id, actor = command.execute.await_args.args
    assert subscribe_id == 7
    assert actor.is_superuser is True


def test_agent_delete_subscribe_skips_command_when_record_is_missing():
    """预读未命中时保持原有不存在提示，且不创建删除副作用。"""
    mutation = SimpleNamespace(get_accessible=AsyncMock(return_value=None))
    delete_scope = MagicMock()
    data = SimpleNamespace(
        subscription_mutation_scope=lambda: _scope(mutation),
        subscription_delete_scope=delete_scope,
    )

    result = asyncio.run(
        DeleteSubscribeTool(
            session_id="session-1",
            user_id="10001",
            data=data,
        ).run(
            subscribe_id=404
        )
    )

    assert result == "订阅 ID 404 不存在"
    delete_scope.assert_not_called()
