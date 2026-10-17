"""
斜杠命令交互路由测试。
"""

# 可选运行时依赖必须在导入消息 Chain 前完成隔离。
# ruff: noqa: E402

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.testing.bootstrap import ensure_optional_stub

ensure_optional_stub("qbittorrentapi", TorrentFilesList=list)
ensure_optional_stub("transmission_rpc", File=object)
ensure_optional_stub("psutil")
ensure_optional_stub("aioshutil")
ensure_optional_stub("pyquery", PyQuery=object)

from app.application.messaging.interaction import InteractionContext
from app.application.messaging.site import site_interaction_manager
from app.application.messaging.skill import skill_interaction_manager
from app.application.messaging.subscribe import subscribe_interaction_manager
from app.chain.message import MessageChain
from app.chain.site import SiteChain
from app.chain.subscribe.facade import SubscribeChain
from app.schemas.types import NotificationChannel


@pytest.fixture
def _clear_slash_interactions():
    """
    隔离测试之间共享的会话与交互状态，并在结束时清理。
    """
    try:
        yield
    finally:
        skill_interaction_manager.clear()
        site_interaction_manager.clear()
        subscribe_interaction_manager.clear()


@pytest.mark.usefixtures("_clear_slash_interactions")
def test_message_routes_text_reply_to_latest_sites_interaction() -> None:
    """
    文本回复优先路由到最新的站点交互会话。
    """
    chain = MessageChain()
    skill_interaction_manager.create_or_replace(
        user_id="10001",
        channel=NotificationChannel.Wechat,
        source="wechat-test",
        username="tester",
    )
    site_interaction_manager.create_or_replace(
        user_id="10001",
        command="/sites",
        channel=NotificationChannel.Wechat,
        source="wechat-test",
        username="tester",
    )

    with (
        patch.object(chain, "_record_user_message"),
        patch(
            "app.chain.message.SiteChain.handle_text_interaction",
            return_value=True,
        ) as handle_site,
        patch("app.chain.message.SkillInteractionHandler.handle_text_interaction") as handle_skills,
    ):
        chain.handle_message(
            channel=NotificationChannel.Wechat,
            source="wechat-test",
            userid="10001",
            username="tester",
            text="禁用 1",
        )

    handle_site.assert_called_once()
    handle_skills.assert_not_called()


@pytest.mark.usefixtures("_clear_slash_interactions")
def test_message_routes_text_reply_to_latest_subscribes_interaction() -> None:
    """
    文本回复优先路由到最新的订阅交互会话。
    """
    chain = MessageChain()
    site_interaction_manager.create_or_replace(
        user_id="10001",
        command="/sites",
        channel=NotificationChannel.Wechat,
        source="wechat-test",
        username="tester",
    )
    subscribe_interaction_manager.create_or_replace(
        user_id="10001",
        command="/subscribes",
        channel=NotificationChannel.Wechat,
        source="wechat-test",
        username="tester",
    )

    with (
        patch.object(chain, "_record_user_message"),
        patch(
            "app.chain.message.SubscribeChain.handle_text_interaction",
            return_value=True,
        ) as handle_subscribes,
        patch("app.chain.message.SiteChain.handle_text_interaction") as handle_sites,
    ):
        chain.handle_message(
            channel=NotificationChannel.Wechat,
            source="wechat-test",
            userid="10001",
            username="tester",
            text="搜索 all",
        )

    handle_subscribes.assert_called_once()
    handle_sites.assert_not_called()


@pytest.mark.usefixtures("_clear_slash_interactions")
def test_callback_routes_to_sites_chain() -> None:
    """
    站点按钮回调路由至站点交互处理器。
    """
    chain = MessageChain()
    request = site_interaction_manager.create_or_replace(
        user_id="10001",
        command="/sites",
        channel=NotificationChannel.Telegram,
        source="telegram-test",
        username="tester",
    )

    with patch(
        "app.chain.message.SiteChain.handle_callback_interaction",
        return_value=True,
    ) as handle_callback:
        chain._handle_callback(
            callback_data=f"sites:{request.request_id}:refresh",
            context=InteractionContext(
                channel=NotificationChannel.Telegram,
                source="telegram-test",
                user_id="10001",
                username="tester",
            ),
        )

    handle_callback.assert_called_once()


@pytest.mark.usefixtures("_clear_slash_interactions")
def test_callback_routes_to_subscribes_chain() -> None:
    """
    订阅按钮回调路由至订阅交互处理器。
    """
    chain = MessageChain()
    request = subscribe_interaction_manager.create_or_replace(
        user_id="10001",
        command="/subscribes",
        channel=NotificationChannel.Telegram,
        source="telegram-test",
        username="tester",
    )

    with patch(
        "app.chain.message.SubscribeChain.handle_callback_interaction",
        return_value=True,
    ) as handle_callback:
        chain._handle_callback(
            callback_data=f"subscribes:{request.request_id}:refresh",
            context=InteractionContext(
                channel=NotificationChannel.Telegram,
                source="telegram-test",
                user_id="10001",
                username="tester",
            ),
        )

    handle_callback.assert_called_once()


@pytest.mark.usefixtures("_clear_slash_interactions")
def test_sites_text_exit_skips_notification_history() -> None:
    """
    退出站点交互的提示不写入通知历史。
    """
    chain = SiteChain()
    site_interaction_manager.create_or_replace(
        user_id="10001",
        command="/sites",
        channel=NotificationChannel.Telegram,
        source="telegram-test",
        username="tester",
    )

    with patch.object(chain, "post_message") as post_message:
        handled = chain.handle_text_interaction(
            channel=NotificationChannel.Telegram,
            source="telegram-test",
            userid="10001",
            username="tester",
            text="退出",
        )

    assert handled
    notification = post_message.call_args.args[0]
    assert notification.title == "站点交互已结束"
    assert not notification.save_history
    assert site_interaction_manager.get_by_user("10001") is None


@pytest.mark.usefixtures("_clear_slash_interactions")
def test_subscribes_text_exit_skips_notification_history() -> None:
    """
    退出订阅交互的提示不写入通知历史。
    """
    chain = SubscribeChain()
    subscribe_interaction_manager.create_or_replace(
        user_id="10001",
        command="/subscribes",
        channel=NotificationChannel.Telegram,
        source="telegram-test",
        username="tester",
    )

    with patch.object(chain, "post_message") as post_message:
        handled = chain.handle_text_interaction(
            channel=NotificationChannel.Telegram,
            source="telegram-test",
            userid="10001",
            username="tester",
            text="退出",
        )

    assert handled
    notification = post_message.call_args.args[0]
    assert notification.title == "订阅交互已结束"
    assert not notification.save_history
    assert subscribe_interaction_manager.get_by_user("10001") is None


@pytest.mark.usefixtures("_clear_slash_interactions")
def test_sites_renders_markdown_table_when_channel_supports_markdown() -> None:
    """
    渠道支持 Markdown 时用表格展示站点信息。
    """
    chain = SiteChain()
    fake_sites = [
        SimpleNamespace(
            id=1,
            name="M-Team",
            is_active=True,
            cookie="cookie=value",
            render=1,
            domain="m-team.io",
            url="https://m-team.io/",
        )
    ]

    chain.site_repository = SimpleNamespace(list=lambda: fake_sites)
    with patch.object(chain, "post_message") as post_message:
        chain.remote_list(channel=NotificationChannel.Web, userid="u1", source="web")

    notification = post_message.call_args[0][0]
    assert "| ID | 站点 | 状态 | Cookie | 渲染 | 域名 |" in notification.text
    assert "| 1 | M-Team | 启用 | 已配置 | 是 | m-team.io |" in notification.text


@pytest.mark.usefixtures("_clear_slash_interactions")
def test_subscribes_renders_markdown_table_when_channel_supports_markdown() -> None:
    """
    渠道支持 Markdown 时用表格展示订阅信息。
    """
    chain = SubscribeChain()
    fake_subscribes = [
        SimpleNamespace(
            id=12,
            name="Example Show",
            type="电视剧",
            year="2024",
            season=1,
            total_episode=10,
            lack_episode=3,
            state="R",
        )
    ]

    subscribe_port = SimpleNamespace(list=lambda: fake_subscribes)
    chain.subscription_repository = subscribe_port
    with patch.object(chain, "post_message") as post_message:
        chain.remote_list(channel=NotificationChannel.Web, userid="u1", source="web")

    notification = post_message.call_args[0][0]
    assert "| ID | 名称 | 类型 | 年份 | 季/进度 | 状态 |" in notification.text
    assert "| 12 | Example Show | 电视剧 | 2024 | 第1季 [7/10] | 订阅中 |" in notification.text


def test_fallback_post_keeps_original_message_context() -> None:
    """
    编辑失败回退发新消息时，必须保留原消息/会话上下文，供渠道回复到原会话。
    """
    from app.application.messaging.interaction import update_or_post_message

    chain = SimpleNamespace(
        edit_message=MagicMock(return_value=False),
        post_message=MagicMock(),
    )

    update_or_post_message(
        chain=chain,
        channel=NotificationChannel.Feishu,
        source="feishu-main",
        userid="ou_user",
        username="tester",
        title="标题",
        text="正文",
        original_message_id="om_origin",
        original_chat_id="oc_group",
    )

    chain.post_message.assert_called_once()
    notification = chain.post_message.call_args[0][0]
    assert notification.original_message_id == "om_origin"
    assert notification.original_chat_id == "oc_group"
