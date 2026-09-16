from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import quote

import pytest

from app.api.endpoints.agent import run_agent_command
from app.schemas.agent import AgentCommandRunRequest
from app.schemas.types import NotificationChannel


@pytest.mark.asyncio
async def test_agent_command_endpoint_decodes_internal_channel_headers() -> None:
    """命令端点应解析 ASCII 编码的渠道，并还原编码后的来源名称。"""
    user = SimpleNamespace(id=1, is_superuser=True)
    command_result = {
        "message": "命令已触发",
        "command": "/demo",
        "command_desc": "示例命令",
    }

    with patch(
        "app.api.endpoints.agent.web_agent_application.dispatch_command",
        return_value=command_result,
    ) as dispatch_command:
        response = await run_agent_command(
            AgentCommandRunRequest(command="/demo"),
            current_user=user,
            agent_channel=quote(NotificationChannel.Wechat.value, safe=""),
            agent_source="%E4%BC%81%E4%B8%9A%E5%BE%AE%E4%BF%A1",
        )

    assert response.success is True
    dispatch_command.assert_called_once_with(
        "/demo",
        user_id="1",
        channel=NotificationChannel.Wechat,
        source="企业微信",
        publish_event=dispatch_command.call_args.kwargs["publish_event"],
    )
