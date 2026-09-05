"""通知渠道主程序升级交互的状态机、进度编辑和路由测试。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from typing import Any
from unittest.mock import Mock, patch

import pytest

from app.application.messaging import update as update_module
from app.application.messaging.interaction import InteractionContext
from app.application.messaging.router import has_pending_interaction
from app.application.messaging.update import (
    SystemUpdateInteractionHandler,
    update_interaction_manager,
)
from app.application.system import SystemOperationResult
from app.chain.message import MessageChain
from app.chain.system import SystemChain
from app.schemas.message import Message
from app.schemas.system import SystemUpdateItemStatus, SystemUpdateStatus
from app.schemas.types import NotificationChannel


class _Messenger:
    """记录交互发送与编辑调用的内存消息网关。"""

    def __init__(self, *, edit_success: bool = True) -> None:
        """初始化消息记录并配置编辑调用结果。"""
        self.messages: list[Message] = []
        self.edits: list[dict[str, Any]] = []
        self.edit_success = edit_success

    def post_message(self, message: Message) -> None:
        """记录一条新发送的消息。"""
        self.messages.append(message)

    def edit_message(self, **kwargs: Any) -> bool:
        """记录一次原消息编辑并返回可控结果。"""
        self.edits.append(kwargs)
        return self.edit_success


class _Actions:
    """提供可排队状态和结果的系统更新应用用例替身。"""

    def __init__(self, check_status: SystemUpdateStatus) -> None:
        """使用初始检查状态构造更新用例替身。"""
        self.check_status = check_status
        self.current_status = check_status
        self.monitor_statuses: list[SystemUpdateStatus] = []
        self.download_result = SystemOperationResult(True, data=check_status)
        self.install_result = SystemOperationResult(True, "restarting")
        self.download_calls: list[str] = []
        self.install_calls: list[str] = []

    def check_update(self) -> SystemUpdateStatus:
        """返回配置的检查结果。"""
        self.current_status = self.check_status
        return self.check_status

    def update_status(self) -> SystemUpdateStatus:
        """按顺序返回监视状态，耗尽后保留最后状态。"""
        if self.monitor_statuses:
            self.current_status = self.monitor_statuses.pop(0)
        return self.current_status

    def download_update(self, target: str = "application") -> SystemOperationResult:
        """记录下载目标并返回配置结果。"""
        self.download_calls.append(target)
        if isinstance(self.download_result.data, SystemUpdateStatus):
            self.current_status = self.download_result.data
        return self.download_result

    def install_update(self, target: str = "application") -> SystemOperationResult:
        """记录安装目标并返回配置结果。"""
        self.install_calls.append(target)
        return self.install_result


def _status(
    state: str,
    *,
    progress: int = 0,
    downloaded_bytes: int = 0,
    total_bytes: int = 0,
    error: str | None = None,
) -> SystemUpdateStatus:
    """构造只包含主程序明细的聚合更新快照。"""
    version = "v3.1.0" if state != "idle" or error else None
    item = SystemUpdateItemStatus(
        type="application",
        state=state,
        current_version="v3.0.0",
        version=version,
        frontend_version="v3.1.0" if version else None,
        release_name="MoviePilot v3.1.0" if version else None,
        release_notes="修复升级流程并更新前端资源" if version else None,
        downloaded_bytes=downloaded_bytes,
        total_bytes=total_bytes,
        progress=progress,
        error=error,
        can_update=state in {"available", "failed"},
        can_install=state == "ready",
    )
    return SystemUpdateStatus(
        state=state,
        current_version="v3.0.0",
        version=version,
        frontend_version=item.frontend_version,
        downloaded_bytes=downloaded_bytes,
        total_bytes=total_bytes,
        progress=progress,
        error=error,
        can_update=item.can_update,
        can_install=item.can_install,
        updates=[item],
    )


@pytest.fixture(autouse=True)
def _reset_update_interactions() -> None:
    """隔离全局更新会话和活动监视请求。"""
    update_interaction_manager.clear()
    with update_module._monitor_lock:
        update_module._monitored_requests.clear()
    yield
    update_interaction_manager.clear()
    with update_module._monitor_lock:
        update_module._monitored_requests.clear()


def _handler(
    messenger: _Messenger,
    actions: _Actions,
    submitted: list[Coroutine[Any, Any, None]],
    *,
    mark_restart: Mock | None = None,
    clear_restart_marker: Mock | None = None,
) -> SystemUpdateInteractionHandler:
    """构造零等待且可观察后台协程的更新交互控制器。"""
    return SystemUpdateInteractionHandler(
        messenger=messenger,
        actions=actions,
        submit_monitor=submitted.append,
        run_sync=_run_sync,
        mark_restart=mark_restart or Mock(),
        clear_restart_marker=clear_restart_marker or Mock(),
        poll_interval_seconds=0,
    )


async def _run_sync(
    function: Callable[..., Any],
    *args: Any,
    **kwargs: Any,
) -> Any:
    """在测试事件循环内直接执行同步更新操作。"""
    return function(*args, **kwargs)


def test_update_command_prompts_for_download_when_release_is_available() -> None:
    """检测到新版本后应显示版本信息和确认升级按钮。"""
    messenger = _Messenger()
    actions = _Actions(_status("available"))
    submitted: list[Coroutine[Any, Any, None]] = []
    handler = _handler(messenger, actions, submitted)

    handler.remote_update(
        channel=NotificationChannel.Telegram,
        userid="10001",
        source="telegram-main",
    )

    request = update_interaction_manager.get_by_user("10001")
    assert request is not None
    assert request.awaiting_input == "download"
    assert messenger.messages[-1].title == "发现 MoviePilot 主程序更新"
    assert "当前版本：v3.0.0" in messenger.messages[-1].text
    assert messenger.messages[-1].buttons == [
        [
            {
                "text": "确认升级",
                "callback_data": f"update:{request.request_id}:download",
            },
            {"text": "稍后", "callback_data": f"update:{request.request_id}:close"},
        ]
    ]
    assert submitted == []


def test_download_callback_edits_same_message_until_restart_confirmation() -> None:
    """下载进度应持续编辑回调原消息，完成后在同一消息显示重启按钮。"""
    messenger = _Messenger()
    actions = _Actions(_status("available"))
    actions.download_result = SystemOperationResult(
        True,
        data=_status(
            "downloading",
            progress=10,
            downloaded_bytes=10 * 1024 * 1024,
            total_bytes=100 * 1024 * 1024,
        ),
    )
    actions.monitor_statuses = [
        _status(
            "downloading",
            progress=45,
            downloaded_bytes=45 * 1024 * 1024,
            total_bytes=100 * 1024 * 1024,
        ),
        _status(
            "ready",
            progress=100,
            downloaded_bytes=100 * 1024 * 1024,
            total_bytes=100 * 1024 * 1024,
        ),
    ]
    submitted: list[Coroutine[Any, Any, None]] = []
    handler = _handler(messenger, actions, submitted)
    request = update_interaction_manager.create_or_replace(
        user_id="10001",
        command="/update",
        channel=NotificationChannel.Telegram,
        source="telegram-main",
        username="tester",
    )

    handled = handler.handle_callback_interaction(
        callback_data=f"update:{request.request_id}:download",
        channel=NotificationChannel.Telegram,
        source="telegram-main",
        userid="10001",
        username="tester",
        original_message_id="message-1",
        original_chat_id="chat-1",
    )

    assert handled is True
    assert actions.download_calls == ["application"]
    assert len(submitted) == 1
    asyncio.run(submitted.pop())

    assert [edit["message_id"] for edit in messenger.edits] == [
        "message-1",
        "message-1",
        "message-1",
    ]
    assert "[=.........] 10%" in messenger.edits[0]["text"]
    assert "[====......] 45%" in messenger.edits[1]["text"]
    assert messenger.edits[-1]["title"] == "MoviePilot 更新包已准备完成"
    assert messenger.edits[-1]["buttons"][0][0]["text"] == "确认重启"
    pending = update_interaction_manager.get_by_user("10001")
    assert pending is not None
    assert pending.awaiting_input == "install"


def test_text_confirmation_uses_milestone_messages_without_editing_support() -> None:
    """无编辑能力的渠道仍应通过文本确认并按进度里程碑继续流程。"""
    messenger = _Messenger()
    actions = _Actions(_status("available"))
    actions.download_result = SystemOperationResult(
        True,
        data=_status("downloading", progress=0, total_bytes=100 * 1024 * 1024),
    )
    actions.monitor_statuses = [
        _status(
            "downloading",
            progress=51,
            downloaded_bytes=51 * 1024 * 1024,
            total_bytes=100 * 1024 * 1024,
        ),
        _status(
            "ready",
            progress=100,
            downloaded_bytes=100 * 1024 * 1024,
            total_bytes=100 * 1024 * 1024,
        ),
    ]
    submitted: list[Coroutine[Any, Any, None]] = []
    handler = _handler(messenger, actions, submitted)
    handler.remote_update(
        channel=NotificationChannel.Wechat,
        userid="wx-user",
        source="wechat-main",
    )

    assert "回复“确认升级”" in messenger.messages[-1].text
    assert (
        handler.handle_text_interaction(
            channel=NotificationChannel.Wechat,
            source="wechat-main",
            userid="wx-user",
            username="tester",
            text="确认升级",
        )
        is True
    )
    asyncio.run(submitted.pop())

    assert messenger.edits == []
    assert any("51%" in str(message.text) for message in messenger.messages)
    assert messenger.messages[-1].title == "MoviePilot 更新包已准备完成"
    assert "回复“确认重启”" in messenger.messages[-1].text


@pytest.mark.parametrize("install_success", [True, False])
def test_restart_confirmation_marks_receipt_and_recovers_failed_install(
    install_success: bool,
) -> None:
    """确认重启应先更新消息，成功结束会话，失败则清理回执并恢复按钮。"""
    messenger = _Messenger()
    ready = _status(
        "ready",
        progress=100,
        downloaded_bytes=100 * 1024 * 1024,
        total_bytes=100 * 1024 * 1024,
    )
    actions = _Actions(ready)
    actions.current_status = ready
    actions.install_result = SystemOperationResult(
        install_success,
        "restarting" if install_success else "restart failed",
    )
    mark_restart = Mock()
    clear_restart_marker = Mock()
    handler = _handler(
        messenger,
        actions,
        [],
        mark_restart=mark_restart,
        clear_restart_marker=clear_restart_marker,
    )
    request = update_interaction_manager.create_or_replace(
        user_id="10001",
        command="/update",
        channel=NotificationChannel.Telegram,
        source="telegram-main",
        username="tester",
    )

    assert (
        handler.handle_callback_interaction(
            callback_data=f"update:{request.request_id}:install",
            channel=NotificationChannel.Telegram,
            source="telegram-main",
            userid="10001",
            username="tester",
            original_message_id="message-1",
            original_chat_id="chat-1",
        )
        is True
    )

    assert messenger.edits[0]["title"] == "正在重启并安装 MoviePilot 更新"
    mark_restart.assert_called_once_with(
        NotificationChannel.Telegram,
        "10001",
        "telegram-main",
    )
    assert actions.install_calls == ["application"]
    if install_success:
        clear_restart_marker.assert_not_called()
        assert update_interaction_manager.get_by_user("10001") is None
    else:
        clear_restart_marker.assert_called_once_with()
        assert messenger.edits[-1]["title"] == "MoviePilot 升级操作失败"
        assert messenger.edits[-1]["buttons"][0][0]["text"] == "确认重启"
        assert update_interaction_manager.get_by_user("10001") is not None


def test_update_session_and_callback_are_registered_in_message_router(monkeypatch) -> None:
    """统一消息路由应识别更新会话文本和 update 回调前缀。"""
    request = update_interaction_manager.create_or_replace(
        user_id="10001",
        command="/update",
        channel=NotificationChannel.Telegram,
        source="telegram-main",
        username="tester",
    )
    assert has_pending_interaction("10001") is True
    callback = Mock(return_value=True)
    monkeypatch.setattr(
        SystemChain,
        "handle_update_callback_interaction",
        callback,
    )
    context = InteractionContext(
        channel=NotificationChannel.Telegram,
        source="telegram-main",
        user_id="10001",
        username="tester",
        original_message_id="message-1",
        original_chat_id="chat-1",
    )

    result = (
        MessageChain()
        ._interaction_router()
        .dispatch_callback(
            context,
            f"update:{request.request_id}:refresh",
        )
    )

    assert result.handled is True
    callback.assert_called_once()


def test_restart_finish_reports_actual_versions_for_update_receipt() -> None:
    """升级重启完成通知应报告当前运行版本而非旧版远端查询。"""
    chain = SystemChain()
    with (
        patch.object(
            chain,
            "load_cache",
            side_effect=[
                None,
                {
                    "channel": NotificationChannel.Telegram.value,
                    "userid": "10001",
                    "source": "telegram-main",
                },
            ],
        ),
        patch.object(chain, "post_message") as post_message,
        patch.object(chain, "remove_cache") as remove_cache,
        patch("app.chain.system.runtime_version.get_app_version", return_value="v3.1.0"),
        patch("app.chain.system.runtime_version.get_frontend_version", return_value="v3.1.0"),
    ):
        chain.restart_finish()

    message = post_message.call_args.args[0]
    assert message.source == "telegram-main"
    assert "当前后端版本：v3.1.0" in message.title
    assert "当前前端版本：v3.1.0" in message.title
    remove_cache.assert_called_once_with(chain._update_restart_file)
