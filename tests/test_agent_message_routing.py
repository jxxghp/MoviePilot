from unittest.mock import patch

from app.chain.message import MessageChain
from app.helper.interaction import media_interaction_manager
from app.schemas.types import MessageChannel


def test_explicit_ai_message_bypasses_pending_media_interaction():
    """显式 /ai 消息应绕过误触发的媒体交互状态并回到 Agent 会话。"""
    chain = MessageChain()
    media_interaction_manager.clear()
    media_interaction_manager.create_or_replace(
        user_id="10001",
        channel=MessageChannel.Wechat,
        source="wechat-test",
        username="tester",
        action="Search",
        keyword="确认",
        title="确认",
    )

    try:
        with patch.object(chain, "_record_user_message"), patch(
            "app.chain.message.MediaInteractionChain.handle_text_interaction",
            return_value=True,
        ) as handle_media_interaction, patch.object(
            chain, "_handle_ai_message", return_value=True
        ) as handle_ai_message:
            chain.handle_message(
                channel=MessageChannel.Wechat,
                source="wechat-test",
                userid="10001",
                username="tester",
                text="/ai 确认",
            )
    finally:
        media_interaction_manager.clear()

    handle_ai_message.assert_called_once()
    handle_media_interaction.assert_not_called()
