"""
验证交互状态的实例、会话、线程与发送者归属。
"""

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.application.messaging.interaction import SlashInteractionManager, interaction_key, scoped_interaction
from app.application.messaging.interaction.agent import AgentInteractionManager, AgentInteractionOption
from app.application.messaging.media import MediaInteractionManager
from app.application.messaging.skill import SkillInteractionManager
from app.application.security import authorization
from app.schemas.types import NotificationChannel


@pytest.mark.parametrize("kind", ["media", "sites", "subscribes", "skills", "update"])
def test_traditional_state_cannot_cross_instance_chat_thread_or_actor(monkeypatch, kind):
    """
    每个类别的同一发送者可在多处独立交互，老按钮不能消费其他上下文的状态。
    """
    config = {"bot": "stable-id", "other": "other-id"}
    monkeypatch.setattr(authorization, "get_channel_authorization", lambda: SimpleNamespace(
        notification=lambda _channel, source: SimpleNamespace(id=config[source]),
    ))
    channel = NotificationChannel.Telegram
    if kind == "media":
        store = MediaInteractionManager()
        arguments = {"action": "Search", "keyword": "测试"}
    elif kind == "skills":
        store = SkillInteractionManager()
        arguments = {}
    else:
        store = SlashInteractionManager()
        arguments = {"command": "/" + kind}

    @scoped_interaction
    def enter(channel, source, userid, original_chat_id, thread_id, request_id=None, *, create=False):
        """
        通过与命令入口相同的作用域建立或查找真实存储记录。
        """
        if create:
            return store.create_or_replace(user_id=userid, channel=channel, source=source, username=None, **arguments)
        if request_id:
            return store.get_by_id(request_id, userid, channel, source)
        return store.get_by_user(userid, channel, source)

    one = enter(channel, "bot", "42", "room", "topic-1", create=True)
    two = enter(channel, "bot", "42", "room", "topic-2", create=True)
    assert enter(channel, "bot", "42", "room", "topic-1") is one
    assert enter(channel, "bot", "42", "room", "topic-2") is two
    for source, actor, room, topic in (
        ("other", "42", "room", "topic-1"),
        ("bot", "77", "room", "topic-1"),
        ("bot", "42", "other-room", "topic-1"),
        ("bot", "42", "room", "topic-2"),
    ):
        assert enter(channel, source, actor, room, topic, one.request_id) is None
    assert store.get_by_user("42") is None
    config["renamed"] = config.pop("bot")
    assert enter(channel, "renamed", "42", "room", "topic-1", one.request_id) is one
    config["renamed"] = "replacement-instance"
    assert enter(channel, "renamed", "42", "room", "topic-1", one.request_id) is None


def test_agent_choice_rejects_wrong_address_and_expires_without_consuming_other_requests(monkeypatch):
    """
    同一裸用户 ID 的其他按钮入口不能消费原请求，原上下文只能成功一次。
    """
    monkeypatch.setattr(authorization, "get_channel_authorization", lambda: None)
    store = AgentInteractionManager()
    channel = NotificationChannel.Telegram
    home = interaction_key("42", channel, "bot", "room")
    request = store.create_request(
        "session", "42", channel.value, "bot", None, None, "请选择",
        [AgentInteractionOption(label="确认", value="确认")], address=home,
    )
    assert store.resolve_web_choice(request.request_id, 1, "42") is None
    assert store.resolve(request.request_id, 1, "42", address=interaction_key("42", channel, "bot", "other")) is None
    assert store.resolve(request.request_id, 1, "42", address=home) is not None
    assert store.resolve(request.request_id, 1, "42", address=home) is None

    expired = store.create_request(
        "session", "42", channel.value, "bot", None, None, "请选择",
        [AgentInteractionOption(label="确认", value="确认")], address=home,
    )
    expired.created_at = datetime.now() - timedelta(hours=25)
    assert store.resolve(expired.request_id, 1, "42", address=home) is None


def test_revoked_agent_choice_does_not_consume_request(monkeypatch):
    """
    Agent 按钮在消费前重新读取当前授权，失败不更改选择或启动 Agent。
    """
    from app.application.messaging.interaction import InteractionContext
    from app.chain.message import MessageChain

    channel = NotificationChannel.Telegram
    active = Mock()
    active.notification.return_value = SimpleNamespace(id="instance")
    active.resolve.side_effect = PermissionError("已停用")
    monkeypatch.setattr(authorization, "get_channel_authorization", lambda: active)
    monkeypatch.setattr("app.chain.message.get_channel_authorization", lambda: active)
    from app.application.messaging.interaction.agent import agent_interaction_manager
    request = agent_interaction_manager.create_request(
        "session", "42", channel.value, "bot", None, None, "请选择",
        [AgentInteractionOption(label="确认", value="确认")],
        address=interaction_key("42", channel, "bot", "room"),
    )
    chain = MessageChain()
    monkeypatch.setattr(chain, "post_message", Mock())
    monkeypatch.setattr(chain, "_handle_ai_message", Mock())
    context = InteractionContext(channel, "bot", "42", None, original_chat_id="room")
    try:
        assert not chain._handle_agent_choice_callback(
            callback_data=f"agent_choice:{request.request_id}:1", context=context,
        )
        chain._handle_ai_message.assert_not_called()
        active.resolve.assert_called_once_with(channel.value, "bot", "42")
        assert agent_interaction_manager.get_request(request.request_id, "42") is request
    finally:
        agent_interaction_manager.clear()


@pytest.mark.parametrize(("choice", "phase", "intent", "operation", "handler"), [
    ("select", "media", "Search", "search.torrents", "_handle_media_selection"),
    ("select", "media", "Subscribe", "subscription.add", "_handle_media_selection"),
    ("download", "torrent", "Search", "download.add", "_handle_torrent_selection"),
    ("download-dir", "download-dir", "Search", "download.add", "_handle_download_dir_selection"),
])
def test_revoked_media_choice_does_not_consume_button(monkeypatch, choice, phase, intent, operation, handler):
    """
    撤权不会执行选择或消耗按钮。恢复授权后原按钮只能执行一次。
    """
    from app.application.messaging.media import media_interaction_manager
    from app.chain.interaction import MediaInteractionChain

    channel = NotificationChannel.Telegram
    chain = MediaInteractionChain()
    resolve = Mock(side_effect=[PermissionError("已停用"), SimpleNamespace(name="owner", notification_identity="owner")])
    monkeypatch.setattr("app.chain.interaction.get_channel_authorization", lambda: SimpleNamespace(resolve_operation=resolve))
    perform = Mock()
    monkeypatch.setattr(chain, handler, perform)
    monkeypatch.setattr(chain, "post_message", Mock())
    request = media_interaction_manager.create_or_replace(
        user_id="42", channel=channel, source="bot", username="成员", action=intent, keyword="合成候选",
    )
    request.phase = phase
    callback = f"media:{request.request_id}:{choice}:1"
    try:
        assert chain.handle_callback_interaction(callback, channel, "bot", "42", "成员")
        resolve.assert_called_once_with(channel.value, "bot", "42", operation)
        perform.assert_not_called()
        resolve.side_effect = None
        resolve.return_value = SimpleNamespace(name="owner", notification_identity="owner")
        assert chain.handle_callback_interaction(callback, channel, "bot", "42", "成员")
        assert chain.handle_callback_interaction(callback, channel, "bot", "42", "成员")
        perform.assert_called_once()
    finally:
        media_interaction_manager.clear()
