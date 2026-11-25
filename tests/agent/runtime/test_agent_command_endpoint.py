"""
验证 Agent 命令端点的参数校验、权限边界及调用结果。
"""

from types import SimpleNamespace
from typing import Optional
from unittest.mock import patch
from urllib.parse import quote

import pytest

from app.api.endpoints.agent import run_agent_command
from app.schemas.agent import AgentCommandRunRequest
from app.schemas.token import TokenPayload
from app.schemas.types import NotificationChannel

_COMMAND_RESULT = {
    "message": "命令已触发",
    "command": "/demo",
    "command_desc": "示例命令",
}


async def _run_command(
    channel: Optional[NotificationChannel],
    *,
    source: Optional[str] = None,
    claim_channel: Optional[NotificationChannel] = None,
    claim_user: Optional[str] = None,
):
    """
    以用户表主键为 1 的超级管理员调用命令端点，返回响应和命令分发替身。

    :param channel: 请求头中的消息渠道
    :param source: 请求头中 URL 编码的消息来源
    :param claim_channel: 宿主令牌签名声明中的渠道；None 表示普通登录或 API 令牌
    :param claim_user: 宿主令牌签名声明中的渠道用户标识
    """
    user = SimpleNamespace(id=1, is_superuser=True)
    token_data = TokenPayload(
        sub=1,
        super_user=True,
        agent_channel=claim_channel.value if claim_channel else None,
        agent_channel_user=claim_user,
    )
    with patch(
        "app.api.endpoints.agent.web_agent_application.dispatch_command",
        return_value=_COMMAND_RESULT,
    ) as dispatch_command:
        response = await run_agent_command(
            AgentCommandRunRequest(command="/demo"),
            current_user=user,
            token_data=token_data,
            agent_channel=quote(channel.value, safe="") if channel else None,
            agent_source=source,
        )
    return response, dispatch_command


@pytest.mark.asyncio
async def test_agent_command_endpoint_decodes_internal_channel_headers() -> None:
    """
    命令端点应解析 ASCII 编码的渠道与来源，并使用令牌声明中的渠道用户。
    """
    response, dispatch_command = await _run_command(
        NotificationChannel.Wechat,
        source="%E4%BC%81%E4%B8%9A%E5%BE%AE%E4%BF%A1",
        claim_channel=NotificationChannel.Wechat,
        claim_user="张三",
    )

    assert response.success is True
    dispatch_command.assert_called_once_with(
        "/demo",
        user_id="张三",
        channel=NotificationChannel.Wechat,
        source="企业微信",
        publish_event=dispatch_command.call_args.kwargs["publish_event"],
    )


@pytest.mark.asyncio
async def test_agent_command_receipt_targets_signed_channel_user() -> None:
    """
    Telegram 等通知渠道的命令按签名声明中的渠道用户执行与回执，不能把用户表主键当作 chat id。
    """
    response, dispatch_command = await _run_command(
        NotificationChannel.Telegram,
        claim_channel=NotificationChannel.Telegram,
        claim_user="123456789",
    )

    assert response.success is True
    assert dispatch_command.call_args.kwargs["user_id"] == "123456789"


@pytest.mark.parametrize("claim_channel,claim_user", [
    (None, None),
    (NotificationChannel.Wechat, "123456789"),
    (NotificationChannel.Telegram, None),
], ids=["no-claim", "other-channel-claim", "missing-user"])
@pytest.mark.asyncio
async def test_agent_command_rejects_unverified_channel_identity(claim_channel, claim_user) -> None:
    """
    通知渠道缺少与该渠道一致的签名渠道用户时拒绝执行，不信任请求头也不回退到用户表主键。
    """
    response, dispatch_command = await _run_command(
        NotificationChannel.Telegram,
        claim_channel=claim_channel,
        claim_user=claim_user,
    )

    assert response.success is False
    assert "无法确认命令发起人" in response.message
    dispatch_command.assert_not_called()


@pytest.mark.parametrize("channel", [NotificationChannel.Web, NotificationChannel.WebAgent])
@pytest.mark.asyncio
async def test_agent_command_web_receipt_keeps_moviepilot_user_id(channel) -> None:
    """
    Web 与 WebAgent 按 MoviePilot 用户 ID 执行，忽略令牌中的渠道用户声明。
    """
    response, dispatch_command = await _run_command(
        channel,
        claim_channel=NotificationChannel.Telegram,
        claim_user="123456789",
    )

    assert response.success is True
    assert dispatch_command.call_args.kwargs["user_id"] == "1"


@pytest.mark.asyncio
async def test_agent_command_without_channel_has_no_recipient() -> None:
    """
    无渠道调用没有真实渠道用户，不能把内部主键交给插件作为通知目标。
    """
    response, dispatch_command = await _run_command(None)

    assert response.success is True
    assert dispatch_command.call_args.kwargs["user_id"] is None
