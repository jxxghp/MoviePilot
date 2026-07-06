from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

from app.chain.message import MediaInteractionChain, MessageChain
from app.core.event import EventManager
from app.core.context import Context, MediaInfo, TorrentInfo
from app.core.meta import MetaBase
from app.helper.interaction import media_interaction_manager, plugin_input_interaction_manager
from app.schemas import CommingMessage, TransferDirectoryConf
from app.schemas.types import EventType, MediaType, MessageChannel


@pytest.fixture(autouse=True)
def clear_media_interactions():
    """清理媒体交互状态，避免用例之间共享内存会话。"""
    yield
    media_interaction_manager.clear()
    plugin_input_interaction_manager.clear()


def _build_meta(name: str) -> MetaBase:
    """构造媒体识别元数据。"""
    meta = MetaBase(name)
    meta.name = name
    meta.begin_season = 1
    return meta


def _build_context(title: str = "星际穿越") -> Context:
    """构造可用于媒体交互下载测试的资源上下文。"""
    return Context(
        meta_info=_build_meta(title),
        media_info=MediaInfo(
            type=MediaType.MOVIE,
            title=title,
            year="2014",
            tmdb_id=1,
        ),
        torrent_info=TorrentInfo(
            title=f"{title}.2014.1080p",
            site_name="TestSite",
            enclosure="https://example.com/demo.torrent",
            seeders=10,
        ),
    )


def _build_tv_context(title: str = "葬送的芙莉莲") -> Context:
    """构造可用于媒体交互下载测试的电视剧上下文。"""
    return Context(
        meta_info=_build_meta(title),
        media_info=MediaInfo(
            type=MediaType.TV,
            title=title,
            year="2023",
            tmdb_id=2,
            category="动漫",
        ),
        torrent_info=TorrentInfo(
            title=f"{title}.S01.1080p",
            site_name="TestSite",
            enclosure="https://example.com/demo-tv.torrent",
            seeders=10,
        ),
    )


def _build_download_dirs() -> list[TransferDirectoryConf]:
    """构造不同媒体类型各一个下载目录的配置。"""
    return [
        TransferDirectoryConf(
            name="电影下载",
            storage="local",
            download_path="/downloads/movies",
            priority=1,
            media_type=MediaType.MOVIE.value,
        ),
        TransferDirectoryConf(
            name="动画下载",
            storage="rclone",
            download_path="/media/anime",
            priority=2,
            media_type=MediaType.TV.value,
            media_category="动漫",
        ),
    ]


def _build_multiple_movie_download_dirs() -> list[TransferDirectoryConf]:
    """构造多个匹配电影类型的下载目录配置。"""
    return [
        TransferDirectoryConf(
            name="电影下载",
            storage="local",
            download_path="/downloads/movies",
            priority=1,
            media_type=MediaType.MOVIE.value,
        ),
        TransferDirectoryConf(
            name="4K电影下载",
            storage="local",
            download_path="/downloads/uhd-movies",
            priority=2,
            media_type=MediaType.MOVIE.value,
        ),
        TransferDirectoryConf(
            name="动画下载",
            storage="rclone",
            download_path="/media/anime",
            priority=3,
            media_type=MediaType.TV.value,
            media_category="动漫",
        ),
    ]


def _build_single_download_dir() -> list[TransferDirectoryConf]:
    """构造只有一个下载目录的配置。"""
    return [
        TransferDirectoryConf(
            name="默认下载",
            storage="local",
            download_path="/downloads",
            priority=1,
        ),
    ]


def test_message_routes_text_reply_to_media_interaction_before_ai():
    """已有传统媒体交互时，用户回复应优先交给传统交互处理。"""
    chain = MessageChain()
    request = media_interaction_manager.create_or_replace(
        user_id="10001",
        channel=MessageChannel.Wechat,
        source="wechat-test",
        username="tester",
        action="Search",
        keyword="星际穿越",
        title="星际穿越",
        meta=_build_meta("星际穿越"),
        items=[MediaInfo(title="星际穿越", year="2014")],
    )
    assert request is not None

    with patch.object(chain, "_record_user_message"), patch(
        "app.chain.message.MediaInteractionChain.handle_text_interaction",
        return_value=True,
    ) as handle_text, patch.object(chain, "_handle_ai_message") as handle_ai:
        chain.handle_message(
            channel=MessageChannel.Wechat,
            source="wechat-test",
            userid="10001",
            username="tester",
            text="1",
        )

    handle_text.assert_called_once()
    handle_ai.assert_not_called()


def test_plugin_input_session_captures_plain_text_before_media_interaction():
    """插件输入会话存在时，普通文本应派发给插件而不是媒体交互。"""
    chain = MessageChain()
    request = plugin_input_interaction_manager.create_or_replace(
        user_id="10001",
        plugin_id="demo_plugin",
        channel=MessageChannel.Wechat,
        source="wechat-test",
        username="tester",
        prompt_id="prompt-1",
        payload={"step": "name"},
    )
    media_interaction_manager.create_or_replace(
        user_id="10001",
        channel=MessageChannel.Wechat,
        source="wechat-test",
        username="tester",
        action="Search",
        keyword="星际穿越",
        title="星际穿越",
        meta=_build_meta("星际穿越"),
        items=[MediaInfo(title="星际穿越", year="2014")],
    )

    with patch.object(chain, "_record_user_message"), patch(
        "app.chain.message.MediaInteractionChain.handle_text_interaction",
        return_value=True,
    ) as handle_media, patch.object(chain.eventmanager, "send_event") as send_event:
        chain.handle_message(
            channel=MessageChannel.Wechat,
            source="wechat-test",
            userid="10001",
            username="tester",
            text="用户输入内容",
        )

    handle_media.assert_not_called()
    send_event.assert_called_once_with(
        EventType.MessageAction,
        {
            "plugin_id": "demo_plugin",
            "__mp_target_plugin_id": "demo_plugin",
            "text": f"plugin_input|{request.request_id}",
            "input_text": "用户输入内容",
            "userid": "10001",
            "channel": MessageChannel.Wechat,
            "source": "wechat-test",
            "username": "tester",
            "chat_id": None,
            "prompt_id": "prompt-1",
            "input_session_id": request.request_id,
            "payload": {"step": "name"},
        },
    )
    assert plugin_input_interaction_manager.get_by_user("10001", MessageChannel.Wechat) is None


def test_plugin_input_session_does_not_record_sensitive_text_history():
    """插件输入命中时不应先写入普通用户消息历史。"""
    chain = MessageChain()
    plugin_input_interaction_manager.create_or_replace(
        user_id="10001",
        plugin_id="demo_plugin",
        channel=MessageChannel.Telegram,
        source="telegram-test",
        username="tester",
    )

    with patch.object(chain, "_record_user_message") as record_message, patch.object(
        chain.eventmanager, "send_event"
    ):
        chain.handle_message(
            channel=MessageChannel.Telegram,
            source="telegram-test",
            userid="10001",
            username="tester",
            text="secret-value",
        )

    record_message.assert_not_called()


def test_plugin_input_session_captures_slash_like_text_before_commands():
    """插件输入会话中的 /path 文本不应被当成 slash 命令。"""
    chain = MessageChain()
    request = plugin_input_interaction_manager.create_or_replace(
        user_id="10001",
        plugin_id="demo_plugin",
        channel=MessageChannel.Telegram,
        source="telegram-test",
        username="tester",
        prompt_id="path",
    )

    with patch.object(chain, "_record_user_message"), patch.object(
        chain.eventmanager, "send_event"
    ) as send_event:
        chain.handle_message(
            channel=MessageChannel.Telegram,
            source="telegram-test",
            userid="10001",
            username="tester",
            text="/downloads/tv",
        )

    send_event.assert_called_once()
    event_type, payload = send_event.call_args.args
    assert event_type == EventType.MessageAction
    assert payload["text"] == f"plugin_input|{request.request_id}"
    assert payload["input_text"] == "/downloads/tv"
    assert payload["__mp_target_plugin_id"] == "demo_plugin"


def test_plugin_input_session_cancel_notifies_plugin_and_clears():
    """取消词应清理插件输入会话并通知目标插件。"""
    chain = MessageChain()
    plugin_input_interaction_manager.create_or_replace(
        user_id="10001",
        plugin_id="demo_plugin",
        channel=MessageChannel.Telegram,
        source="telegram-test",
        username="tester",
    )

    with patch.object(chain, "_record_user_message"), patch.object(
        chain.eventmanager, "send_event"
    ) as send_event, patch.object(chain, "post_message") as post_message:
        chain.handle_message(
            channel=MessageChannel.Telegram,
            source="telegram-test",
            userid="10001",
            username="tester",
            text="取消",
        )

    send_event.assert_called_once()
    event_type, payload = send_event.call_args.args
    assert event_type == EventType.MessageAction
    assert payload["cancelled"] is True
    assert payload["__mp_target_plugin_id"] == "demo_plugin"
    post_message.assert_called_once()
    assert plugin_input_interaction_manager.get_by_user("10001", MessageChannel.Telegram) is None


def test_plugin_input_cancel_does_not_block_next_command():
    """取消输入后下一条 slash 命令应按正常命令路由处理。"""
    chain = MessageChain()
    plugin_input_interaction_manager.create_or_replace(
        user_id="10001",
        plugin_id="demo_plugin",
        channel=MessageChannel.Telegram,
        source="telegram-test",
        username="tester",
    )

    with patch.object(chain, "_record_user_message"), patch.object(
        chain.eventmanager, "send_event"
    ) as send_event, patch.object(chain, "post_message"):
        chain.handle_message(
            channel=MessageChannel.Telegram,
            source="telegram-test",
            userid="10001",
            username="tester",
            text="取消",
        )
        chain.handle_message(
            channel=MessageChannel.Telegram,
            source="telegram-test",
            userid="10001",
            username="tester",
            text="/tvh",
        )

    assert send_event.call_args_list[0].args[0] == EventType.MessageAction
    assert send_event.call_args_list[1].args[0] == EventType.CommandExcute
    assert send_event.call_args_list[1].args[1]["cmd"] == "/tvh"


def test_plugin_input_session_ignores_non_text_messages():
    """图片/文件等非纯文本消息不应消费待输入会话。"""
    chain = MessageChain()
    request = plugin_input_interaction_manager.create_or_replace(
        user_id="10001",
        plugin_id="demo_plugin",
        channel=MessageChannel.Telegram,
        source="telegram-test",
        username="tester",
    )
    image = CommingMessage.MessageImage(ref="https://example.invalid/image.jpg")

    with patch.object(chain, "_record_user_message"), patch.object(
        chain.eventmanager, "send_event"
    ) as send_event:
        chain.handle_message(
            channel=MessageChannel.Telegram,
            source="telegram-test",
            userid="10001",
            username="tester",
            text="图片说明",
            images=[image],
        )

    assert plugin_input_interaction_manager.get_by_user(
        "10001", MessageChannel.Telegram, "telegram-test"
    ) == request
    assert not any(
        call.args and call.args[0] == EventType.MessageAction
        for call in send_event.call_args_list
    )


def test_plugin_input_session_ignores_none_text_messages():
    """文本为空时不应因 CALLBACK 检查崩溃或消费待输入会话。"""
    chain = MessageChain()
    request = plugin_input_interaction_manager.create_or_replace(
        user_id="10001",
        plugin_id="demo_plugin",
        channel=MessageChannel.Telegram,
        source="telegram-test",
        username="tester",
    )
    image = CommingMessage.MessageImage(ref="https://example.invalid/image.jpg")

    handled = chain._handle_plugin_input_interaction(
        channel=MessageChannel.Telegram,
        source="telegram-test",
        userid="10001",
        username="tester",
        text=None,
        images=[image],
    )

    assert handled is False
    assert plugin_input_interaction_manager.get_by_user(
        "10001", MessageChannel.Telegram, "telegram-test"
    ) == request


def test_plugin_input_session_is_bound_to_user_and_channel():
    """同一用户不同渠道的插件输入会话互不匹配。"""
    request = plugin_input_interaction_manager.create_or_replace(
        user_id="10001",
        plugin_id="demo_plugin",
        channel=MessageChannel.Telegram,
        source="telegram-test",
        username="tester",
    )

    assert plugin_input_interaction_manager.get_by_user("10001", MessageChannel.Wechat) is None
    assert plugin_input_interaction_manager.get_by_user(
        "10001", MessageChannel.Telegram, "telegram-test"
    ) == request
    assert plugin_input_interaction_manager.get_by_user("10002", MessageChannel.Telegram) is None


def test_plugin_input_session_does_not_capture_other_channel_text():
    """生产消费路径也必须保持用户+渠道绑定。"""
    chain = MessageChain()
    request = plugin_input_interaction_manager.create_or_replace(
        user_id="10001",
        plugin_id="demo_plugin",
        channel=MessageChannel.Telegram,
        source="telegram-test",
        username="tester",
    )

    with patch.object(chain, "_record_user_message"), patch.object(
        chain.eventmanager, "send_event"
    ) as send_event:
        chain.handle_message(
            channel=MessageChannel.Wechat,
            source="wechat-test",
            userid="10001",
            username="tester",
            text="普通搜索",
        )

    assert plugin_input_interaction_manager.get_by_user(
        "10001", MessageChannel.Telegram, "telegram-test"
    ) == request
    assert not any(
        call.args and call.args[0] == EventType.MessageAction
        for call in send_event.call_args_list
    )


def test_plugin_input_session_does_not_capture_other_source_text():
    """同一渠道不同来源的插件输入会话互不匹配。"""
    chain = MessageChain()
    request = plugin_input_interaction_manager.create_or_replace(
        user_id="10001",
        plugin_id="demo_plugin",
        channel=MessageChannel.Telegram,
        source="telegram-bot-a",
        username="tester",
    )

    with patch.object(chain, "_record_user_message"), patch.object(
        chain.eventmanager, "send_event"
    ) as send_event:
        chain.handle_message(
            channel=MessageChannel.Telegram,
            source="telegram-bot-b",
            userid="10001",
            username="tester",
            text="普通搜索",
        )

    assert plugin_input_interaction_manager.get_by_user(
        "10001", MessageChannel.Telegram, "telegram-bot-a"
    ) == request
    assert not any(
        call.args and call.args[0] == EventType.MessageAction
        for call in send_event.call_args_list
    )


def test_plugin_input_session_does_not_capture_other_chat_text():
    """同一用户同一 bot 的不同 chat 不应串扰插件输入会话。"""
    chain = MessageChain()
    request = plugin_input_interaction_manager.create_or_replace(
        user_id="10001",
        plugin_id="demo_plugin",
        channel=MessageChannel.Telegram,
        source="telegram-test",
        username="tester",
        chat_id="chat-a",
    )

    with patch.object(chain, "_record_user_message"), patch.object(
        chain.eventmanager, "send_event"
    ) as send_event:
        chain.handle_message(
            channel=MessageChannel.Telegram,
            source="telegram-test",
            userid="10001",
            username="tester",
            text="普通搜索",
            original_chat_id="chat-b",
        )

    assert plugin_input_interaction_manager.get_by_user(
        "10001", MessageChannel.Telegram, "telegram-test", "chat-a"
    ) == request
    assert not any(
        call.args and call.args[0] == EventType.MessageAction
        for call in send_event.call_args_list
    )

    with patch.object(chain, "_record_user_message"), patch.object(
        chain.eventmanager, "send_event"
    ) as send_event:
        chain.handle_message(
            channel=MessageChannel.Telegram,
            source="telegram-test",
            userid="10001",
            username="tester",
            text="真正输入",
            original_chat_id="chat-a",
        )

    send_event.assert_called_once()
    event_type, payload = send_event.call_args.args
    assert event_type == EventType.MessageAction
    assert payload["input_text"] == "真正输入"
    assert payload["chat_id"] == "chat-a"


def test_plugin_input_chatless_session_keeps_legacy_chat_fallback():
    """旧插件未绑定 chat_id 时，同 source 消息仍可兼容消费。"""
    chain = MessageChain()
    request = plugin_input_interaction_manager.create_or_replace(
        user_id="10001",
        plugin_id="demo_plugin",
        channel=MessageChannel.Telegram,
        source="telegram-test",
        username="tester",
    )

    with patch.object(chain, "_record_user_message"), patch.object(
        chain.eventmanager, "send_event"
    ) as send_event:
        chain.handle_message(
            channel=MessageChannel.Telegram,
            source="telegram-test",
            userid="10001",
            username="tester",
            text="兼容输入",
            original_chat_id="chat-a",
        )

    send_event.assert_called_once()
    event_type, payload = send_event.call_args.args
    assert event_type == EventType.MessageAction
    assert payload["input_session_id"] == request.request_id
    assert payload["input_text"] == "兼容输入"


def test_plugin_input_wildcard_session_does_not_match_missing_source_with_chat():
    """chat fallback 不应把完全 wildcard 会话扩大到有渠道但缺来源的消息。"""
    request = plugin_input_interaction_manager.create_or_replace(
        user_id="10001",
        plugin_id="demo_plugin",
        channel=None,
        source=None,
        username="tester",
    )

    assert plugin_input_interaction_manager.consume_by_user(
        "10001", MessageChannel.Telegram, None, "chat-a"
    ) == (None, None)
    assert plugin_input_interaction_manager.get_by_user("10001", None, None) == request


def test_plugin_input_chat_bound_session_does_not_match_missing_chat():
    """绑定 chat_id 的会话不应被缺少 chat_id 的消息消费。"""
    request = plugin_input_interaction_manager.create_or_replace(
        user_id="10001",
        plugin_id="demo_plugin",
        channel=MessageChannel.Telegram,
        source="telegram-test",
        username="tester",
        chat_id="chat-a",
    )

    assert plugin_input_interaction_manager.consume_by_user(
        "10001", MessageChannel.Telegram, "telegram-test"
    ) == (None, None)
    assert plugin_input_interaction_manager.get_by_user(
        "10001", MessageChannel.Telegram, "telegram-test", "chat-a"
    ) == request


def test_plugin_input_core_path_preserves_original_chat_id():
    """直接进入核心路由时也应保留 chat_id 绑定，避免防御路径漏掉插件输入。"""
    chain = MessageChain()
    request = plugin_input_interaction_manager.create_or_replace(
        user_id="10001",
        plugin_id="demo_plugin",
        channel=MessageChannel.Telegram,
        source="telegram-test",
        username="tester",
        chat_id="chat-a",
    )

    with patch.object(chain.eventmanager, "send_event") as send_event:
        chain._handle_message_core(
            channel=MessageChannel.Telegram,
            source="telegram-test",
            userid="10001",
            username="tester",
            text="核心路径输入",
            original_chat_id="chat-a",
        )

    send_event.assert_called_once()
    event_type, payload = send_event.call_args.args
    assert event_type == EventType.MessageAction
    assert payload["input_session_id"] == request.request_id
    assert payload["input_text"] == "核心路径输入"
    assert payload["chat_id"] == "chat-a"


def test_plugin_input_session_does_not_capture_missing_source_text():
    """来源缺失的入站消息不应捕获绑定到具体来源的输入会话。"""
    chain = MessageChain()
    request = plugin_input_interaction_manager.create_or_replace(
        user_id="10001",
        plugin_id="demo_plugin",
        channel=MessageChannel.Telegram,
        source="telegram-bot-a",
        username="tester",
    )

    with patch.object(chain, "_record_user_message"), patch.object(
        chain.eventmanager, "send_event"
    ) as send_event:
        chain.handle_message(
            channel=MessageChannel.Telegram,
            source=None,
            userid="10001",
            username="tester",
            text="普通搜索",
        )

    assert plugin_input_interaction_manager.get_by_user(
        "10001", MessageChannel.Telegram, "telegram-bot-a"
    ) == request
    assert not any(
        call.args and call.args[0] == EventType.MessageAction
        for call in send_event.call_args_list
    )


def test_plugin_input_session_does_not_capture_callback_payload():
    """待输入会话存在时，按钮回调仍应优先进入回调链而不是投递给插件。"""
    chain = MessageChain()
    request = plugin_input_interaction_manager.create_or_replace(
        user_id="10001",
        plugin_id="demo_plugin",
        channel=MessageChannel.Telegram,
        source="telegram-test",
        username="tester",
        chat_id="chat-a",
    )

    with patch.object(chain, "_record_user_message") as record_message, patch.object(
        chain.eventmanager, "send_event"
    ) as send_event, patch.object(chain, "_handle_callback", return_value=True) as handle_callback:
        chain.handle_message(
            channel=MessageChannel.Telegram,
            source="telegram-test",
            userid="10001",
            username="tester",
            text="CALLBACK:media:req:page-next",
            original_message_id="msg-1",
            original_chat_id="chat-a",
        )

    record_message.assert_not_called()
    handle_callback.assert_called_once()
    assert plugin_input_interaction_manager.get_by_user(
        "10001", MessageChannel.Telegram, "telegram-test", "chat-a"
    ) == request
    assert not any(
        call.args and call.args[0] == EventType.MessageAction
        for call in send_event.call_args_list
    )


def test_plugin_input_session_expires_after_timeout():
    """插件输入会话超过 TTL 后不再匹配。"""
    request = plugin_input_interaction_manager.create_or_replace(
        user_id="10001",
        plugin_id="demo_plugin",
        channel=MessageChannel.Wechat,
        source="wechat-test",
        username="tester",
        timeout_seconds=120,
    )
    request.created_at = datetime.now() - timedelta(seconds=121)

    assert plugin_input_interaction_manager.get_by_user("10001", MessageChannel.Wechat) is None


def test_plugin_input_session_expired_text_notifies_plugin_and_continues_routing():
    """过期插件输入遇到命令时应提示超时，并让命令继续正常路由。"""
    chain = MessageChain()
    request = plugin_input_interaction_manager.create_or_replace(
        user_id="10001",
        plugin_id="demo_plugin",
        channel=MessageChannel.Telegram,
        source="telegram-test",
        username="tester",
        timeout_seconds=120,
    )
    request.created_at = datetime.now() - timedelta(seconds=121)

    with patch.object(chain, "_record_user_message") as record_message, patch.object(
        chain.eventmanager, "send_event"
    ) as send_event, patch.object(chain, "post_message") as post_message:
        chain.handle_message(
            channel=MessageChannel.Telegram,
            source="telegram-test",
            userid="10001",
            username="tester",
            text="/tvh",
        )

    record_message.assert_called_once()
    post_message.assert_called_once()
    assert send_event.call_count == 2
    event_type, payload = send_event.call_args_list[0].args
    assert event_type == EventType.MessageAction
    assert payload["expired"] is True
    assert payload["input_session_id"] == request.request_id
    command_type, command_payload = send_event.call_args_list[1].args
    assert command_type == EventType.CommandExcute
    assert command_payload["cmd"] == "/tvh"


def test_plugin_input_session_expired_sensitive_text_is_not_recorded_or_routed():
    """过期插件输入中的普通文本仍可能是敏感信息，应提示后吞掉。"""
    chain = MessageChain()
    request = plugin_input_interaction_manager.create_or_replace(
        user_id="10001",
        plugin_id="demo_plugin",
        channel=MessageChannel.Telegram,
        source="telegram-test",
        username="tester",
        timeout_seconds=120,
    )
    request.created_at = datetime.now() - timedelta(seconds=121)

    with patch.object(chain, "_record_user_message") as record_message, patch.object(
        chain.eventmanager, "send_event"
    ) as send_event, patch.object(chain, "post_message") as post_message:
        chain.handle_message(
            channel=MessageChannel.Telegram,
            source="telegram-test",
            userid="10001",
            username="tester",
            text="late-secret",
        )

    record_message.assert_not_called()
    post_message.assert_called_once()
    send_event.assert_called_once()
    event_type, payload = send_event.call_args.args
    assert event_type == EventType.MessageAction
    assert payload["expired"] is True
    assert payload["input_session_id"] == request.request_id


def test_plugin_input_expired_text_after_cleanup_is_not_recorded_or_routed():
    """其他用户触发过期清理后，迟到的普通文本仍应按过期插件输入吞掉。"""
    chain = MessageChain()
    request = plugin_input_interaction_manager.create_or_replace(
        user_id="10001",
        plugin_id="demo_plugin",
        channel=MessageChannel.Telegram,
        source="telegram-test",
        username="tester",
        timeout_seconds=120,
    )
    request.created_at = datetime.now() - timedelta(seconds=121)

    plugin_input_interaction_manager.create_or_replace(
        user_id="10002",
        plugin_id="demo_plugin",
        channel=MessageChannel.Telegram,
        source="telegram-test",
        username="other",
        timeout_seconds=120,
    )
    assert request.request_id not in plugin_input_interaction_manager._by_id

    with patch.object(chain, "_record_user_message") as record_message, patch.object(
        chain.eventmanager, "send_event"
    ) as send_event, patch.object(chain, "post_message") as post_message:
        chain.handle_message(
            channel=MessageChannel.Telegram,
            source="telegram-test",
            userid="10001",
            username="tester",
            text="late-secret",
        )

    record_message.assert_not_called()
    post_message.assert_called_once()
    send_event.assert_called_once()
    event_type, payload = send_event.call_args.args
    assert event_type == EventType.MessageAction
    assert payload["expired"] is True
    assert payload["input_session_id"] == request.request_id


def test_plugin_input_session_with_no_channel_matches_specific_channel():
    """插件拿不到渠道时创建的会话应能被该用户下一条具体渠道消息消费。"""
    chain = MessageChain()
    request = plugin_input_interaction_manager.create_or_replace(
        user_id="10001",
        plugin_id="demo_plugin",
        channel=None,
        source="telegram-test",
        username="tester",
    )

    with patch.object(chain, "_record_user_message"), patch.object(
        chain.eventmanager, "send_event"
    ) as send_event:
        chain.handle_message(
            channel=MessageChannel.Telegram,
            source="telegram-test",
            userid="10001",
            username="tester",
            text="keyword",
        )

    send_event.assert_called_once()
    event_type, payload = send_event.call_args.args
    assert event_type == EventType.MessageAction
    assert payload["input_session_id"] == request.request_id
    assert payload["input_text"] == "keyword"


def test_plugin_input_session_with_no_channel_and_no_source_does_not_match_specific_message():
    """完全缺少渠道来源的会话不应跨渠道捕获具体消息。"""
    chain = MessageChain()
    request = plugin_input_interaction_manager.create_or_replace(
        user_id="10001",
        plugin_id="demo_plugin",
        channel=None,
        source=None,
        username="tester",
    )

    with patch.object(chain, "_record_user_message"), patch.object(
        chain.eventmanager, "send_event"
    ) as send_event:
        chain.handle_message(
            channel=MessageChannel.Telegram,
            source="telegram-test",
            userid="10001",
            username="tester",
            text="keyword",
        )

    assert plugin_input_interaction_manager.get_by_user("10001", None, None) == request
    assert not any(
        call.args and call.args[0] == EventType.MessageAction
        for call in send_event.call_args_list
    )


def test_plugin_input_specific_session_replaces_overlapping_no_channel_session():
    """同用户创建具体渠道会话时，应替换重叠的无渠道会话，避免下一条消息被连环接管。"""
    old_request = plugin_input_interaction_manager.create_or_replace(
        user_id="10001",
        plugin_id="old_plugin",
        channel=None,
        source="telegram-test",
        username="tester",
    )
    new_request = plugin_input_interaction_manager.create_or_replace(
        user_id="10001",
        plugin_id="demo_plugin",
        channel=MessageChannel.Telegram,
        source="telegram-test",
        username="tester",
    )

    assert plugin_input_interaction_manager.pop_by_user(
        "10001", MessageChannel.Telegram, "telegram-test"
    ) == new_request
    assert plugin_input_interaction_manager.pop_by_user(
        "10001", MessageChannel.Telegram, "telegram-test"
    ) is None


def test_plugin_input_session_pop_by_user_consumes_once():
    """原子消费应保证同一个会话只返回一次。"""
    request = plugin_input_interaction_manager.create_or_replace(
        user_id="10001",
        plugin_id="demo_plugin",
        channel=MessageChannel.Telegram,
        source="telegram-test",
        username="tester",
    )

    assert plugin_input_interaction_manager.pop_by_user(
        "10001", MessageChannel.Telegram, "telegram-test"
    ) == request
    assert plugin_input_interaction_manager.pop_by_user(
        "10001", MessageChannel.Telegram, "telegram-test"
    ) is None


def test_target_plugin_filter_only_allows_target_plugin_handler():
    """带目标插件的输入事件不应投递给其他插件或模块级处理器。"""

    def demo_plugin_handler(_event):
        return None

    def other_plugin_handler(_event):
        return None

    demo_plugin_handler.__qualname__ = "demo_plugin.handle"
    other_plugin_handler.__qualname__ = "other_plugin.handle"

    def module_handler(_event):
        return None

    should_dispatch = EventManager._EventManager__should_dispatch_to_target_plugin
    handler_id = EventManager._EventManager__get_handler_identifier(demo_plugin_handler)

    assert should_dispatch(
        demo_plugin_handler, "tests.plugins.demo_plugin.handle", "demo_plugin"
    ) is True
    assert should_dispatch(demo_plugin_handler, handler_id, "demo_plugin") is True
    assert should_dispatch(
        demo_plugin_handler, "tests.plugins.other_plugin.handle", "demo_plugin"
    ) is False
    assert should_dispatch(
        other_plugin_handler, "tests.plugins.other_plugin.handle", "demo_plugin"
    ) is False
    assert should_dispatch(module_handler, "tests.module_handler", "demo_plugin") is False


def test_noai_prefix_starts_traditional_search_when_global_ai_enabled():
    """全局 AI 开启时，/noai 前缀应让本条消息进入传统搜索交互。"""
    chain = MessageChain()
    meta = _build_meta("星际穿越")
    medias = [
        MediaInfo(title="星际穿越", year="2014"),
        MediaInfo(title="Interstellar", year="2014"),
    ]

    with patch.object(chain, "_record_user_message"), patch(
        "app.chain.message.settings.AI_AGENT_ENABLE", True
    ), patch(
        "app.chain.message.settings.AI_AGENT_GLOBAL", True
    ), patch(
        "app.chain.media.MediaChain.search",
        return_value=(meta, medias),
    ) as search_media, patch(
        "app.chain.message.MediaInteractionChain.post_medias_message"
    ) as post_medias_message, patch.object(
        chain, "_handle_ai_message"
    ) as handle_ai:
        chain.handle_message(
            channel=MessageChannel.Telegram,
            source="telegram-test",
            userid="10001",
            username="tester",
            text="/noai 星际穿越",
        )

    search_media.assert_called_once_with("星际穿越")
    post_medias_message.assert_called_once()
    handle_ai.assert_not_called()

    request = media_interaction_manager.get_by_user("10001")
    assert request is not None
    assert request.action == "Search"
    assert request.keyword == "星际穿越"
    assert len(request.items) == 2


def test_noai_prefix_preserves_traditional_interaction_priority_after_search():
    """通过 /noai 进入传统交互后，后续选择应继续优先走传统交互。"""
    chain = MessageChain()
    request = media_interaction_manager.create_or_replace(
        user_id="10001",
        channel=MessageChannel.Wechat,
        source="wechat-test",
        username="tester",
        action="Search",
        keyword="星际穿越",
        title="星际穿越",
        meta=_build_meta("星际穿越"),
        items=[MediaInfo(title="星际穿越", year="2014")],
    )
    assert request is not None

    with patch.object(chain, "_record_user_message"), patch(
        "app.chain.message.settings.AI_AGENT_ENABLE", True
    ), patch(
        "app.chain.message.settings.AI_AGENT_GLOBAL", True
    ), patch(
        "app.chain.message.MediaInteractionChain.handle_text_interaction",
        return_value=True,
    ) as handle_text, patch.object(chain, "_handle_ai_message") as handle_ai:
        chain.handle_message(
            channel=MessageChannel.Wechat,
            source="wechat-test",
            userid="10001",
            username="tester",
            text="1",
        )

    handle_text.assert_called_once()
    handle_ai.assert_not_called()


def test_callback_routes_to_media_interaction_chain():
    """媒体按钮回调应路由到媒体交互链。"""
    chain = MessageChain()
    request = media_interaction_manager.create_or_replace(
        user_id="10001",
        channel=MessageChannel.Telegram,
        source="telegram-test",
        username="tester",
        action="Search",
        keyword="星际穿越",
        title="星际穿越",
        meta=_build_meta("星际穿越"),
        items=[MediaInfo(title="星际穿越", year="2014")],
    )

    with patch(
        "app.chain.message.MediaInteractionChain.handle_callback_interaction",
        return_value=True,
    ) as handle_callback:
        chain._handle_callback(
            text=f"CALLBACK:media:{request.request_id}:page-next",
            channel=MessageChannel.Telegram,
            source="telegram-test",
            userid="10001",
            username="tester",
        )

    handle_callback.assert_called_once()


def test_media_interaction_starts_search_and_posts_media_list():
    """传统媒体交互应能搜索媒体并发送候选列表。"""
    chain = MediaInteractionChain()
    meta = _build_meta("星际穿越")
    medias = [
        MediaInfo(title="星际穿越", year="2014"),
        MediaInfo(title="Interstellar", year="2014"),
    ]

    with patch(
        "app.chain.media.MediaChain.search",
        return_value=(meta, medias),
    ), patch.object(chain, "post_medias_message") as post_medias_message:
        handled = chain.handle_text_interaction(
            channel=MessageChannel.Telegram,
            source="telegram-test",
            userid="10001",
            username="tester",
            text="星际穿越",
        )

    assert handled
    post_medias_message.assert_called_once()
    notification = post_medias_message.call_args.args[0]
    assert notification.save_history is False
    assert notification.buttons
    assert notification.buttons[0][0]["callback_data"].startswith("media:")

    request = media_interaction_manager.get_by_user("10001")
    assert request is not None
    assert request.action == "Search"
    assert len(request.items) == 2


def test_media_interaction_legacy_page_callback_updates_existing_request():
    """旧格式翻页回调仍应更新当前媒体交互请求。"""
    chain = MediaInteractionChain()
    request = media_interaction_manager.create_or_replace(
        user_id="10001",
        channel=MessageChannel.Telegram,
        source="telegram-test",
        username="tester",
        action="Search",
        keyword="星际穿越",
        title="星际穿越",
        meta=_build_meta("星际穿越"),
        items=[
            MediaInfo(title=f"资源 {index}", year="2024")
            for index in range(1, 11)
        ],
    )

    with patch.object(chain, "post_medias_message") as post_medias_message:
        handled = chain.handle_callback_interaction(
            callback_data="page_n",
            channel=MessageChannel.Telegram,
            source="telegram-test",
            userid="10001",
            username="tester",
            original_message_id=123,
            original_chat_id="456",
        )

    assert handled
    assert request.page == 1
    post_medias_message.assert_called_once()
    notification = post_medias_message.call_args.args[0]
    assert notification.original_message_id == 123
    assert notification.original_chat_id == "456"


def test_torrent_selection_prompts_download_dir_buttons_before_download():
    """匹配当前媒体的目录有多个时，应先发送下载目录按钮而不是立即下载。"""
    chain = MediaInteractionChain()
    context = _build_context()
    request = media_interaction_manager.create_or_replace(
        user_id="10001",
        channel=MessageChannel.Telegram,
        source="telegram-test",
        username="tester",
        action="Search",
        keyword="星际穿越",
        title="星际穿越",
        meta=_build_meta("星际穿越"),
        items=[context],
    )
    request.phase = "torrent"

    with patch(
        "app.chain.message.DirectoryHelper.get_download_dirs",
        return_value=_build_multiple_movie_download_dirs(),
    ), patch.object(chain, "post_message") as post_message, patch(
        "app.chain.message.DownloadChain.download_single"
    ) as download_single:
        handled = chain.handle_text_interaction(
            channel=MessageChannel.Telegram,
            source="telegram-test",
            userid="10001",
            username="tester",
            text="1",
        )

    assert handled
    download_single.assert_not_called()
    assert request.phase == "download-dir"
    post_message.assert_called_once()
    notification = post_message.call_args.args[0]
    assert notification.save_history is False
    assert "请选择下载目录" in notification.title
    assert "1. 自动匹配目录" in notification.text
    assert "2. 电影下载 (/downloads/movies)" in notification.text
    assert "3. 4K电影下载 (/downloads/uhd-movies)" in notification.text
    assert "动画下载" not in notification.text
    assert notification.buttons[0][0]["callback_data"] == f"media:{request.request_id}:download-dir:1"


def test_torrent_selection_skips_download_dir_when_only_one_dir_matches_media():
    """匹配当前媒体的目录只有一个时，应跳过目录选择并交给下载链自动匹配。"""
    chain = MediaInteractionChain()
    context = _build_context()
    request = media_interaction_manager.create_or_replace(
        user_id="10001",
        channel=MessageChannel.Telegram,
        source="telegram-test",
        username="tester",
        action="Search",
        keyword="星际穿越",
        title="星际穿越",
        meta=_build_meta("星际穿越"),
        items=[context],
    )
    request.phase = "torrent"

    with patch(
        "app.chain.message.DirectoryHelper.get_download_dirs",
        return_value=_build_download_dirs(),
    ), patch.object(chain, "post_message") as post_message, patch(
        "app.chain.message.DownloadChain.download_single",
        return_value="hash",
    ) as download_single:
        handled = chain.handle_text_interaction(
            channel=MessageChannel.Telegram,
            source="telegram-test",
            userid="10001",
            username="tester",
            text="1",
        )

    assert handled
    assert request.phase == "torrent"
    post_message.assert_not_called()
    download_single.assert_called_once()
    assert download_single.call_args.args[0] is context
    assert "save_path" not in download_single.call_args.kwargs


def test_torrent_selection_skips_download_dir_when_user_has_single_dir():
    """用户只有一个下载目录时，也应跳过目录选择并交给下载链自动匹配。"""
    chain = MediaInteractionChain()
    context = _build_context()
    request = media_interaction_manager.create_or_replace(
        user_id="10001",
        channel=MessageChannel.Telegram,
        source="telegram-test",
        username="tester",
        action="Search",
        keyword="星际穿越",
        title="星际穿越",
        meta=_build_meta("星际穿越"),
        items=[context],
    )
    request.phase = "torrent"

    with patch(
        "app.chain.message.DirectoryHelper.get_download_dirs",
        return_value=_build_single_download_dir(),
    ), patch.object(chain, "post_message") as post_message, patch(
        "app.chain.message.DownloadChain.download_single",
        return_value="hash",
    ) as download_single:
        handled = chain.handle_text_interaction(
            channel=MessageChannel.Telegram,
            source="telegram-test",
            userid="10001",
            username="tester",
            text="1",
        )

    assert handled
    assert request.phase == "torrent"
    post_message.assert_not_called()
    download_single.assert_called_once()
    assert download_single.call_args.args[0] is context
    assert "save_path" not in download_single.call_args.kwargs


def test_torrent_selection_prompts_text_download_dir_for_plain_channel():
    """不支持按钮的渠道在多个匹配目录时，应提示用户回复数字选择下载目录。"""
    chain = MediaInteractionChain()
    context = _build_context()
    request = media_interaction_manager.create_or_replace(
        user_id="wechat-user",
        channel=MessageChannel.Wechat,
        source="wechat-test",
        username="tester",
        action="Search",
        keyword="星际穿越",
        title="星际穿越",
        meta=_build_meta("星际穿越"),
        items=[context],
    )
    request.phase = "torrent"

    with patch(
        "app.chain.message.DirectoryHelper.get_download_dirs",
        return_value=_build_multiple_movie_download_dirs(),
    ), patch.object(chain, "post_message") as post_message:
        handled = chain.handle_text_interaction(
            channel=MessageChannel.Wechat,
            source="wechat-test",
            userid="wechat-user",
            username="tester",
            text="1",
        )

    assert handled
    notification = post_message.call_args.args[0]
    assert notification.save_history is False
    assert "请回复对应数字" in notification.title
    assert notification.buttons is None
    assert "1. 自动匹配目录" in notification.text
    assert "2. 电影下载 (/downloads/movies)" in notification.text
    assert "3. 4K电影下载 (/downloads/uhd-movies)" in notification.text
    assert "动画下载" not in notification.text


def test_download_dir_callback_runs_pending_single_download_without_save_path_for_auto():
    """下载目录选择自动匹配时，应不传 save_path 继续执行挂起的单资源下载。"""
    chain = MediaInteractionChain()
    context = _build_context()
    request = media_interaction_manager.create_or_replace(
        user_id="10001",
        channel=MessageChannel.Telegram,
        source="telegram-test",
        username="tester",
        action="Search",
        keyword="星际穿越",
        title="星际穿越",
        meta=_build_meta("星际穿越"),
        items=[context],
    )
    request.phase = "download-dir"
    request.pending_download_mode = "single"
    request.pending_download_context = context

    with patch(
        "app.chain.message.DirectoryHelper.get_download_dirs",
        return_value=_build_multiple_movie_download_dirs(),
    ), patch(
        "app.chain.message.DownloadChain.download_single",
        return_value="hash",
    ) as download_single:
        request.download_dirs = chain._get_download_dirs(context.media_info)
        handled = chain.handle_callback_interaction(
            callback_data=f"media:{request.request_id}:download-dir:1",
            channel=MessageChannel.Telegram,
            source="telegram-test",
            userid="10001",
            username="tester",
        )

    assert handled
    assert request.phase == "torrent"
    download_single.assert_called_once()
    assert download_single.call_args.args[0] is context
    assert download_single.call_args.kwargs["save_path"] is None


def test_download_dir_callback_runs_pending_single_download_with_save_path():
    """下载目录按钮回调应使用所选 save_path 继续执行挂起的单资源下载。"""
    chain = MediaInteractionChain()
    context = _build_context()
    request = media_interaction_manager.create_or_replace(
        user_id="10001",
        channel=MessageChannel.Telegram,
        source="telegram-test",
        username="tester",
        action="Search",
        keyword="星际穿越",
        title="星际穿越",
        meta=_build_meta("星际穿越"),
        items=[context],
    )
    request.phase = "download-dir"
    request.pending_download_mode = "single"
    request.pending_download_context = context

    with patch(
        "app.chain.message.DirectoryHelper.get_download_dirs",
        return_value=_build_multiple_movie_download_dirs(),
    ), patch(
        "app.chain.message.DownloadChain.download_single",
        return_value="hash",
    ) as download_single:
        request.download_dirs = chain._get_download_dirs(context.media_info)
        handled = chain.handle_callback_interaction(
            callback_data=f"media:{request.request_id}:download-dir:2",
            channel=MessageChannel.Telegram,
            source="telegram-test",
            userid="10001",
            username="tester",
        )

    assert handled
    assert request.phase == "torrent"
    download_single.assert_called_once()
    assert download_single.call_args.args[0] is context
    assert download_single.call_args.kwargs["save_path"] == "/downloads/movies"


def test_download_dir_text_reply_runs_pending_single_download_without_save_path():
    """下载目录文本回复选择自动匹配时应不传 save_path。"""
    chain = MediaInteractionChain()
    context = _build_context()
    request = media_interaction_manager.create_or_replace(
        user_id="wechat-user",
        channel=MessageChannel.Wechat,
        source="wechat-test",
        username="tester",
        action="Search",
        keyword="星际穿越",
        title="星际穿越",
        meta=_build_meta("星际穿越"),
        items=[context],
    )
    request.phase = "download-dir"
    request.pending_download_mode = "single"
    request.pending_download_context = context

    with patch(
        "app.chain.message.DirectoryHelper.get_download_dirs",
        return_value=_build_multiple_movie_download_dirs(),
    ), patch(
        "app.chain.message.DownloadChain.download_single",
        return_value="hash",
    ) as download_single:
        request.download_dirs = chain._get_download_dirs()
        handled = chain.handle_text_interaction(
            channel=MessageChannel.Wechat,
            source="wechat-test",
            userid="wechat-user",
            username="tester",
            text="1",
        )

    assert handled
    assert request.phase == "torrent"
    download_single.assert_called_once()
    assert download_single.call_args.args[0] is context
    assert download_single.call_args.kwargs["save_path"] is None


def test_get_download_dirs_keeps_matching_tv_category_dir():
    """目录列表应保留匹配当前电视剧类别的下载目录。"""
    chain = MediaInteractionChain()
    context = _build_tv_context()

    with patch(
        "app.chain.message.DirectoryHelper.get_download_dirs",
        return_value=_build_download_dirs(),
    ):
        download_dirs = chain._get_download_dirs(context.media_info)

    assert [download_dir.name for download_dir in download_dirs] == [
        "动画下载",
    ]
    assert download_dirs[0].save_path == "rclone:/media/anime"
