"""
交互链委托入口的行为测试。
"""

from unittest.mock import Mock

import pytest

from app.chain._interaction import InteractionChainMixin
from app.schemas.types import NotificationChannel


def test_interaction_chain_delegates_command_and_replies() -> None:
    """
    保证斜杠命令、按钮和文本参数原样交给当前处理器。
    """
    handler = Mock()
    handler.remote_list.return_value = None
    handler.handle_callback_interaction.return_value = True
    handler.handle_text_interaction.return_value = False

    class Chain(InteractionChainMixin):
        """
        为委托测试注入可观察处理器。
        """

        def _interaction_handler(self):
            """
            返回当前测试的交互处理器。
            """
            return handler

    chain = Chain()
    channel = NotificationChannel.Telegram
    assert chain.remote_list("next", channel, 42, "source") is None
    handler.remote_list.assert_called_once_with(
        arg_str="next", channel=channel, userid=42, source="source"
    )

    assert chain.handle_callback_interaction(
        "sites:id:next", channel, "source", 42, "user", 7, "chat"
    ) is True
    handler.handle_callback_interaction.assert_called_once_with(
        callback_data="sites:id:next",
        channel=channel,
        source="source",
        userid=42,
        username="user",
        original_message_id=7,
        original_chat_id="chat",
    )

    assert chain.handle_text_interaction(channel, "source", 42, "user", "reply") is False
    handler.handle_text_interaction.assert_called_once_with(
        channel=channel, source="source", userid=42, username="user", text="reply"
    )


def test_interaction_chain_uses_handler_callback_parser() -> None:
    """
    回调解析使用子类配置的处理器，缺少配置时保留既有异常。
    """
    parser = Mock(return_value=("id", "next"))

    class ConfiguredChain(InteractionChainMixin):
        """
        带有回调解析器的交互链测试类型。
        """

        _interaction_handler_type = type("CallbackHandler", (), {"parse_callback": parser})

    assert ConfiguredChain.parse_callback("sites:id:next") == ("id", "next")
    parser.assert_called_once_with("sites:id:next")

    with pytest.raises(AttributeError, match="parse_callback"):
        InteractionChainMixin.parse_callback("sites:id:next")
