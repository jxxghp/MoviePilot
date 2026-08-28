import asyncio
from collections.abc import Callable
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from app.application.messaging.interaction import InteractionContext
from app.chain.message import MessageChain
from app.chain.transfer import TransferChain
from app.runtime.config import settings
from app.runtime.loop import main_loop_registry
from app.runtime.tasks import TaskRegistry
from app.schemas.types import NotificationChannel


@pytest.fixture
def replace_main_loop() -> Callable[[object], None]:
    """临时替换主循环登记，并在用例结束后恢复原值。"""
    original = main_loop_registry.current
    try:
        yield main_loop_registry.replace_compat
    finally:
        main_loop_registry.replace_compat(original)


def test_build_failed_transfer_buttons():
    """整理失败消息应提供重试与智能助手接管按钮。"""
    buttons = TransferChain.build_failed_transfer_buttons(12)

    assert buttons == [[
        {"text": "重试", "callback_data": "transfer_retry_12"},
        {
            "text": "智能助手接管",
            "callback_data": "transfer_ai_retry_12",
        },
    ]]


def test_remote_transfer_supports_history_only_retry():
    """远程整理入口应支持只按历史 ID 重试。"""
    chain = TransferChain()

    with patch.object(chain, "redo_transfer_history", return_value=(True, "")) as redo:
        with patch.object(chain, "post_message") as post_message:
            chain.remote_transfer(
                "12",
                channel=NotificationChannel.Telegram,
                userid="10001",
                source="telegram-test",
            )

    redo.assert_called_once_with(12)
    post_message.assert_not_called()

def test_message_chain_routes_transfer_callback_to_transfer_chain():
    """MessageChain 收到整理失败按钮回调时委托 TransferChain 处理。"""
    chain = MessageChain()

    with patch("app.chain.message.TransferChain") as transfer_cls:
        transfer_cls.return_value.handle_failed_transfer_callback.return_value = True
        chain._handle_callback(
            callback_data="transfer_retry_12",
            context=InteractionContext(
                channel=NotificationChannel.Telegram,
                source="telegram-test",
                user_id="10001",
                username="tester",
            ),
        )

    transfer_cls.return_value.handle_failed_transfer_callback.assert_called_once_with(
        callback_data="transfer_retry_12",
        channel=NotificationChannel.Telegram,
        source="telegram-test",
        userid="10001",
        username="tester",
    )

def test_transfer_retry_callback_retries_history():
    """重试回调应执行历史重整并发送开始、完成消息。"""
    chain = TransferChain()

    with patch.object(chain, "redo_transfer_history", return_value=(True, "")) as redo:
        with patch.object(chain, "post_message") as post_message:
            handled = chain.handle_failed_transfer_callback(
                callback_data="transfer_retry_12",
                channel=NotificationChannel.Telegram,
                source="telegram-test",
                userid="10001",
                username="tester",
            )

    assert handled
    redo.assert_called_once_with(12)
    assert post_message.call_count == 2
    assert post_message.call_args_list[0].args[0].title == "开始重新整理记录 #12 ..."
    assert post_message.call_args_list[1].args[0].title == "整理记录 #12 已重新整理"


def test_transfer_ai_retry_callback_schedules_agent_takeover(replace_main_loop):
    """智能接管回调应向已登记主循环提交受管后台任务。"""
    chain = TransferChain()
    chain.runtime_config = replace(chain.runtime_config, ai_agent_enable=True)
    history = SimpleNamespace(
        id=34,
        status=False,
        title="Test Show",
        type="电视剧",
        category=None,
        year="2024",
        seasons="S01",
        episodes="E01",
        src="/downloads/Test.Show.S01E01.mkv",
        src_storage="local",
        src_fileitem={"path": "/downloads/Test.Show.S01E01.mkv"},
        dest=None,
        dest_storage=None,
        dest_fileitem=None,
        mode="copy",
        tmdbid=123,
        doubanid=None,
        bangumiid=None,
        anilistid=None,
        media_source="themoviedb",
        media_id="123",
        errmsg="未识别到媒体信息",
    )
    chain.transfer_history_repository = SimpleNamespace(
        get=lambda history_id: history
    )

    async_messages = []

    def _run_pending_coro(coro, *args, **kwargs):
        asyncio.run(coro)

    async def _capture_message(message):
        async_messages.append(message)

    async def _finish_immediately(**kwargs):
        kwargs["output_callback"]("ok")

    manager = SimpleNamespace(run_background_prompt=_finish_immediately)
    loop = Mock(**{"is_running.return_value": True, "is_closed.return_value": False})
    replace_main_loop(loop)
    with patch.object(settings, "AI_AGENT_ENABLE", True):
        with patch(
            "app.chain.transfer.retry.build_manual_redo_prompt",
            return_value="retry transfer prompt",
        ), patch(
            "app.chain.transfer.retry.get_running_agent_manager", return_value=manager
        ), patch("app.chain.transfer.retry.get_task_registry") as get_registry:
            get_registry.return_value.submit_threadsafe.side_effect = (
                _run_pending_coro
            )
            with patch.object(chain, "async_post_message", side_effect=_capture_message):
                chain.handle_failed_transfer_callback(
                    callback_data="transfer_ai_retry_34",
                    channel=NotificationChannel.Telegram,
                    source="telegram-test",
                    userid="10001",
                    username="tester",
                )

    get_registry.return_value.submit_threadsafe.assert_called_once()
    assert (
        get_registry.return_value.submit_threadsafe.call_args.kwargs["owner"]
        == "chain.transfer.ai_takeover"
    )
    assert len(async_messages) == 2
    assert async_messages[0].title == "已将整理记录 #34 交给智能助手处理"
    assert async_messages[1].title == "智能助手整理完成"


def test_transfer_ai_retry_callback_reports_closed_task_registry(replace_main_loop):
    """宿主停止接收任务时，不得向用户报告智能助手已接管。"""
    chain = TransferChain()
    chain.runtime_config = replace(chain.runtime_config, ai_agent_enable=True)
    history = SimpleNamespace(id=34)
    chain.transfer_history_repository = SimpleNamespace(
        get=lambda history_id: history
    )
    registry = TaskRegistry()
    asyncio.run(registry.shutdown(timeout_seconds=0.01))
    loop = Mock(**{"is_running.return_value": True, "is_closed.return_value": False})
    replace_main_loop(loop)

    with patch.object(settings, "AI_AGENT_ENABLE", True), patch(
        "app.chain.transfer.retry.build_manual_redo_prompt",
        return_value="retry transfer prompt",
    ), patch(
        "app.chain.transfer.retry.get_task_registry", return_value=registry
    ), patch(
        "app.chain.transfer.retry.logger"
    ) as logger, patch.object(
        chain, "post_message"
    ) as post_message:
        chain.handle_failed_transfer_callback(
            callback_data="transfer_ai_retry_34",
            channel=NotificationChannel.Telegram,
            source="telegram-test",
            userid="10001",
            username="tester",
        )

    logger.warning.assert_called_once()
    assert post_message.call_count == 1
    assert post_message.call_args.args[0].title == "智能助手整理失败"
    assert "已将" not in post_message.call_args.args[0].title


def test_transfer_ai_retry_callback_reports_unavailable_event_loop(replace_main_loop):
    """主循环不可用时，应在创建后台协程前返回明确失败提示。"""
    chain = TransferChain()
    chain.runtime_config = replace(chain.runtime_config, ai_agent_enable=True)
    history = SimpleNamespace(id=34)
    chain.transfer_history_repository = SimpleNamespace(
        get=lambda history_id: history
    )
    replace_main_loop(None)

    with patch.object(settings, "AI_AGENT_ENABLE", True), patch(
        "app.chain.transfer.retry.build_manual_redo_prompt",
        return_value="retry transfer prompt",
    ), patch(
        "app.chain.transfer.retry.get_task_registry"
    ) as get_registry, patch(
        "app.chain.transfer.retry.logger"
    ) as logger, patch.object(
        chain, "post_message"
    ) as post_message:
        chain.handle_failed_transfer_callback(
            callback_data="transfer_ai_retry_34",
            channel=NotificationChannel.Telegram,
            source="telegram-test",
            userid="10001",
            username="tester",
        )

    get_registry.return_value.submit_threadsafe.assert_not_called()
    logger.warning.assert_called_once()
    assert post_message.call_count == 1
    assert post_message.call_args.args[0].title == "智能助手整理失败"


def test_transfer_ai_retry_callback_uses_successful_move_dest_as_source(
        replace_main_loop,
):
    """移动成功历史应以目标文件作为智能接管的重做源路径。"""
    chain = TransferChain()
    chain.runtime_config = replace(chain.runtime_config, ai_agent_enable=True)
    captured = {}
    history = SimpleNamespace(
        id=35,
        status=True,
        title="Test Show",
        type="电视剧",
        category=None,
        year="2024",
        seasons="S01",
        episodes="E01",
        src="/downloads/Test.Show.S01E01.mkv",
        src_storage="local",
        src_fileitem={"path": "/downloads/Test.Show.S01E01.mkv"},
        dest="/library/Test Show (2024)/Season 1/Test.Show.S01E01.mkv",
        dest_storage="local",
        dest_fileitem={
            "storage": "local",
            "path": "/library/Test Show (2024)/Season 1/Test.Show.S01E01.mkv",
            "name": "Test.Show.S01E01.mkv",
            "type": "file",
        },
        mode="move",
        tmdbid=123,
        doubanid=None,
        bangumiid=None,
        anilistid=None,
        media_source="themoviedb",
        media_id="123",
        errmsg=None,
    )
    chain.transfer_history_repository = SimpleNamespace(
        get=lambda history_id: history
    )

    def _run_pending_coro(coro, *args, **kwargs):
        asyncio.run(coro)
        return SimpleNamespace()

    async def fake_run_background_prompt(**kwargs):
        captured["message"] = kwargs["message"]
        output_callback = kwargs.get("output_callback")
        if output_callback:
            output_callback("ok")

    async def fake_async_post_message(*args, **kwargs):
        return None

    from app.agent.prompt.transfer_redo import build_manual_redo_prompt

    manager = SimpleNamespace(run_background_prompt=fake_run_background_prompt)
    loop = Mock(**{"is_running.return_value": True, "is_closed.return_value": False})
    replace_main_loop(loop)
    with patch.object(settings, "AI_AGENT_ENABLE", True):
        with patch(
            "app.chain.transfer.retry.build_manual_redo_prompt",
            side_effect=build_manual_redo_prompt,
        ), patch(
            "app.chain.transfer.retry.get_running_agent_manager",
            return_value=manager,
        ), patch("app.chain.transfer.retry.get_task_registry") as get_registry:
            get_registry.return_value.submit_threadsafe.side_effect = (
                _run_pending_coro
            )
            with patch.object(chain, "post_message"), patch.object(
                chain, "async_post_message", side_effect=fake_async_post_message
            ):
                chain.handle_failed_transfer_callback(
                    callback_data="transfer_ai_retry_35",
                    channel=NotificationChannel.Telegram,
                    source="telegram-test",
                    userid="10001",
                    username="tester",
                )

    assert (
        "- Source path: /library/Test Show (2024)/Season 1/Test.Show.S01E01.mkv"
        in captured["message"]
    )
