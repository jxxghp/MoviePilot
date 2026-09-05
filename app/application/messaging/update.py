"""通知渠道中的主程序更新检查、下载进度和重启确认交互。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass
from threading import Lock
from typing import Any, Optional, Protocol, Union

from app.application.messaging.interaction import (
    MessageGateway,
    PendingSlashInteraction,
    SlashInteractionManager,
    supports_interaction_buttons,
    update_or_post_message,
)
from app.application.system import SystemOperationResult
from app.runtime.log import logger
from app.schemas.message import Message
from app.schemas.notification import ChannelCapabilityManager
from app.schemas.system import SystemUpdateItemStatus, SystemUpdateStatus, SystemUpdateType
from app.schemas.types import NotificationChannel

update_interaction_manager = SlashInteractionManager()

_monitor_lock = Lock()
_monitored_requests: set[str] = set()


class SystemUpdateInteractionActions(Protocol):
    """声明更新交互调用的主程序升级应用用例。"""

    def update_status(self) -> SystemUpdateStatus:
        """读取当前后台更新状态。"""
        ...

    def check_update(self) -> SystemUpdateStatus:
        """立即检查主程序正式版本更新。"""
        ...

    def download_update(self, target: SystemUpdateType = "application") -> SystemOperationResult:
        """启动主程序更新包下载。"""
        ...

    def install_update(self, target: SystemUpdateType = "application") -> SystemOperationResult:
        """确认主程序更新包并请求重启安装。"""
        ...


UpdateMonitorSubmitter = Callable[[Coroutine[Any, Any, None]], Any]
UpdateOperationRunner = Callable[..., Awaitable[Any]]
RestartMarker = Callable[[NotificationChannel, Union[str, int], Optional[str]], None]
RestartMarkerClearer = Callable[[], None]


@dataclass(frozen=True, slots=True)
class SystemUpdateInteractionView:
    """保存一次渠道无关的更新交互展示内容。"""

    title: str
    text: str
    buttons: Optional[list[list[dict[str, str]]]] = None


class SystemUpdateInteractionHandler:
    """编排 `/update` 的检查、下载进度编辑和重启确认流程。"""

    _poll_interval_seconds = 3.0

    def __init__(
        self,
        *,
        messenger: MessageGateway,
        actions: SystemUpdateInteractionActions,
        submit_monitor: UpdateMonitorSubmitter,
        run_sync: UpdateOperationRunner,
        mark_restart: RestartMarker,
        clear_restart_marker: RestartMarkerClearer,
        poll_interval_seconds: float = _poll_interval_seconds,
    ) -> None:
        """注入消息网关、系统更新用例和受管后台任务提交器。"""
        self._messenger = messenger
        self._actions = actions
        self._mark_restart = mark_restart
        self._clear_restart_marker = clear_restart_marker
        self._renderer = _SystemUpdateRenderer(messenger=messenger, actions=actions)
        self._progress_monitor = _SystemUpdateProgressMonitor(
            actions=actions, renderer=self._renderer, submit_monitor=submit_monitor,
            run_sync=run_sync,
            poll_interval_seconds=poll_interval_seconds,
        )

    def remote_update(
        self,
        arg_str: str = "",
        channel: Optional[NotificationChannel] = None,
        userid: Optional[Union[str, int]] = None,
        source: Optional[str] = None,
    ) -> None:
        """执行 `/update`，检查正式版本并创建后续确认会话。"""
        if channel is None or userid is None:
            return
        request = update_interaction_manager.create_or_replace(
            user_id=userid,
            command="/update",
            channel=channel,
            source=source,
            username=None,
        )
        try:
            status = self._actions.check_update()
        except Exception as error:  # noqa: BLE001  交互入口必须回显稳定错误
            logger.warning(f"检查 MoviePilot 更新失败：{error}")
            self._renderer.render_check_failure(
                request=request,
                channel=channel,
                source=source,
                userid=userid,
                username="",
                error=str(error),
            )
            return

        normalized_arg = str(arg_str or "").strip()
        if normalized_arg and self.handle_text_interaction(
            channel=channel,
            source=source,
            userid=userid,
            username="",
            text=normalized_arg,
        ):
            return
        self._renderer.render_status(
            request=request,
            status=status,
            channel=channel,
            source=source,
            userid=userid,
            username="",
        )

    @staticmethod
    def parse_callback(callback_data: str) -> Optional[tuple[str, str]]:
        """解析 `/update` 的按钮回调。"""
        if not str(callback_data or "").startswith("update:"):
            return None
        parts = str(callback_data).split(":")
        if len(parts) != 3 or not parts[1] or not parts[2]:
            return None
        return parts[1], parts[2]

    def handle_callback_interaction(
        self,
        callback_data: str,
        channel: NotificationChannel,
        source: Optional[str],
        userid: Union[str, int],
        username: Optional[str],
        original_message_id: Optional[Union[str, int]] = None,
        original_chat_id: Optional[str] = None,
    ) -> bool:
        """消费 `/update` 按钮回调并保持原消息作为进度锚点。"""
        parsed = self.parse_callback(callback_data)
        if not parsed:
            return False
        request_id, action = parsed
        request = update_interaction_manager.get_by_id(request_id, userid)
        if request is None:
            self._messenger.post_message(
                Message(
                    channel=channel,
                    source=source,
                    userid=userid,
                    username=username,
                    title="升级交互已失效，请重新发送 /update",
                    save_history=False,
                )
            )
            return True

        request.channel = channel
        request.source = source
        request.username = username
        if action == "close":
            update_interaction_manager.remove(request.request_id)
            update_or_post_message(
                chain=self._messenger,
                channel=channel,
                source=source,
                userid=userid,
                username=username,
                title="MoviePilot 更新",
                text="本次升级交互已结束，已开始的后台下载不会被取消。",
                original_message_id=original_message_id,
                original_chat_id=original_chat_id,
            )
            return True
        if action == "refresh":
            self._check_and_render(
                request=request,
                channel=channel,
                source=source,
                userid=userid,
                username=username,
                original_message_id=original_message_id,
                original_chat_id=original_chat_id,
            )
            return True
        if action == "download":
            self._start_download(
                request=request,
                channel=channel,
                source=source,
                userid=userid,
                username=username,
                original_message_id=original_message_id,
                original_chat_id=original_chat_id,
            )
            return True
        if action == "install":
            self._install_and_restart(
                request=request,
                channel=channel,
                source=source,
                userid=userid,
                username=username,
                original_message_id=original_message_id,
                original_chat_id=original_chat_id,
            )
            return True
        return False

    def handle_text_interaction(
        self,
        channel: NotificationChannel,
        source: Optional[str],
        userid: Union[str, int],
        username: Optional[str],
        text: str,
    ) -> bool:
        """消费不支持按钮渠道或用户主动输入的升级确认文本。"""
        request = update_interaction_manager.get_by_user(userid)
        if request is None:
            return False
        request.channel = channel
        request.source = source
        request.username = username
        normalized = str(text or "").strip().lower()

        if normalized in {"稍后", "取消", "关闭", "退出", "cancel", "close", "quit", "exit"}:
            update_interaction_manager.remove(request.request_id)
            self._messenger.post_message(
                Message(
                    channel=channel,
                    source=source,
                    userid=userid,
                    username=username,
                    title="本次升级交互已结束",
                    save_history=False,
                )
            )
            return True
        if normalized in {"刷新", "检查", "状态", "refresh", "check", "status"}:
            self._check_and_render(
                request=request,
                channel=channel,
                source=source,
                userid=userid,
                username=username,
            )
            return True

        if normalized == "确认":
            normalized = "确认重启" if request.awaiting_input == "install" else "确认升级"
        if normalized in {"确认升级", "升级", "下载", "重试", "update", "download", "retry"}:
            self._start_download(
                request=request,
                channel=channel,
                source=source,
                userid=userid,
                username=username,
            )
            return True
        if normalized in {"确认重启", "重启", "安装", "restart", "install"}:
            self._install_and_restart(
                request=request,
                channel=channel,
                source=source,
                userid=userid,
                username=username,
            )
            return True

        try:
            status = self._actions.update_status()
        except Exception as error:  # noqa: BLE001  文本交互错误必须回显
            self._renderer.render_check_failure(
                request=request,
                channel=channel,
                source=source,
                userid=userid,
                username=username,
                error=str(error),
            )
            return True
        self._renderer.render_status(
            request=request,
            status=status,
            channel=channel,
            source=source,
            userid=userid,
            username=username,
        )
        return True

    def _check_and_render(
        self,
        *,
        request: PendingSlashInteraction,
        channel: NotificationChannel,
        source: Optional[str],
        userid: Union[str, int],
        username: Optional[str],
        original_message_id: Optional[Union[str, int]] = None,
        original_chat_id: Optional[str] = None,
    ) -> None:
        """重新检查版本并在原交互消息中展示结果。"""
        try:
            status = self._actions.check_update()
        except Exception as error:  # noqa: BLE001  交互入口必须回显稳定错误
            logger.warning(f"检查 MoviePilot 更新失败：{error}")
            self._renderer.render_check_failure(
                request=request,
                channel=channel,
                source=source,
                userid=userid,
                username=username,
                error=str(error),
                original_message_id=original_message_id,
                original_chat_id=original_chat_id,
            )
            return
        self._renderer.render_status(
            request=request,
            status=status,
            channel=channel,
            source=source,
            userid=userid,
            username=username,
            original_message_id=original_message_id,
            original_chat_id=original_chat_id,
        )

    def _start_download(
        self,
        *,
        request: PendingSlashInteraction,
        channel: NotificationChannel,
        source: Optional[str],
        userid: Union[str, int],
        username: Optional[str],
        original_message_id: Optional[Union[str, int]] = None,
        original_chat_id: Optional[str] = None,
    ) -> None:
        """启动主程序下载，立即展示状态并登记持续进度监视。"""
        try:
            result = self._actions.download_update("application")
            status = result.data if isinstance(result.data, SystemUpdateStatus) else self._actions.update_status()
        except Exception as error:  # noqa: BLE001  交互入口必须回显稳定错误
            logger.warning(f"启动 MoviePilot 更新下载失败：{error}")
            self._renderer.render_operation_failure(
                request=request,
                channel=channel,
                source=source,
                userid=userid,
                username=username,
                error=str(error),
                original_message_id=original_message_id,
                original_chat_id=original_chat_id,
            )
            return

        operation_error = None if result.success else result.message or "无法启动更新包下载"
        self._renderer.render_status(
            request=request,
            status=status,
            channel=channel,
            source=source,
            userid=userid,
            username=username,
            operation_error=operation_error,
            original_message_id=original_message_id,
            original_chat_id=original_chat_id,
        )
        item = self._renderer.application_item(status)
        if result.success and item.state == "downloading":
            self._progress_monitor.schedule(
                request=request,
                channel=channel,
                source=source,
                userid=userid,
                username=username,
                initial_item=item,
                original_message_id=original_message_id,
                original_chat_id=original_chat_id,
            )

    def _install_and_restart(
        self,
        *,
        request: PendingSlashInteraction,
        channel: NotificationChannel,
        source: Optional[str],
        userid: Union[str, int],
        username: Optional[str],
        original_message_id: Optional[Union[str, int]] = None,
        original_chat_id: Optional[str] = None,
    ) -> None:
        """先更新提示并记录重启目标，再确认安装和请求受管重启。"""
        try:
            status = self._actions.update_status()
        except Exception as error:  # noqa: BLE001  交互入口必须回显稳定错误
            self._renderer.render_operation_failure(
                request=request,
                channel=channel,
                source=source,
                userid=userid,
                username=username,
                error=str(error),
                original_message_id=original_message_id,
                original_chat_id=original_chat_id,
            )
            return
        item = self._renderer.application_item(status)
        if item.state != "ready":
            self._renderer.render_status(
                request=request,
                status=status,
                channel=channel,
                source=source,
                userid=userid,
                username=username,
                operation_error="更新包尚未下载完成",
                original_message_id=original_message_id,
                original_chat_id=original_chat_id,
            )
            return

        installing = item.model_copy(update={"state": "installing", "can_install": False})
        self._renderer.render_item(
            request=request,
            item=installing,
            channel=channel,
            source=source,
            userid=userid,
            username=username,
            original_message_id=original_message_id,
            original_chat_id=original_chat_id,
        )
        self._mark_restart(channel, userid, source)
        try:
            result = self._actions.install_update("application")
        except Exception as error:  # noqa: BLE001  重启失败必须恢复交互
            self._clear_restart_marker()
            logger.warning(f"安装 MoviePilot 更新失败：{error}")
            self._renderer.render_operation_failure(
                request=request,
                channel=channel,
                source=source,
                userid=userid,
                username=username,
                error=str(error),
                original_message_id=original_message_id,
                original_chat_id=original_chat_id,
            )
            return
        if result.success:
            update_interaction_manager.remove(request.request_id)
            return

        self._clear_restart_marker()
        try:
            status = self._actions.update_status()
        except Exception:  # noqa: BLE001  优先保留安装用例的稳定错误
            status = SystemUpdateStatus(
                state="ready",
                current_version=item.current_version or "unknown",
                updates=[item],
            )
        self._renderer.render_status(
            request=request,
            status=status,
            channel=channel,
            source=source,
            userid=userid,
            username=username,
            operation_error=result.message or "无法重启并安装更新",
            original_message_id=original_message_id,
            original_chat_id=original_chat_id,
        )

class _SystemUpdateProgressMonitor:
    """管理主程序更新下载的后台轮询与消息刷新。"""

    _terminal_download_states = {"idle", "available", "ready", "failed", "installing"}

    def __init__(
        self, *, actions: SystemUpdateInteractionActions,
        renderer: _SystemUpdateRenderer, submit_monitor: UpdateMonitorSubmitter,
        run_sync: UpdateOperationRunner,
        poll_interval_seconds: float,
    ) -> None:
        """注入状态读取、消息渲染和后台任务提交能力。"""
        self._actions = actions
        self._renderer = renderer
        self._submit_monitor = submit_monitor
        self._run_sync = run_sync
        self._poll_interval_seconds = max(0.0, poll_interval_seconds)

    def schedule(
        self, *, request: PendingSlashInteraction, channel: NotificationChannel,
        source: Optional[str], userid: Union[str, int], username: Optional[str],
        initial_item: SystemUpdateItemStatus,
        original_message_id: Optional[Union[str, int]], original_chat_id: Optional[str],
    ) -> None:
        """确保同一交互只登记一个非阻塞下载进度监视任务。"""
        with _monitor_lock:
            if request.request_id in _monitored_requests:
                return
            _monitored_requests.add(request.request_id)
        monitor = self._monitor_download(
            request_id=request.request_id,
            channel=channel,
            source=source,
            userid=userid,
            username=username,
            initial_item=initial_item,
            original_message_id=original_message_id,
            original_chat_id=original_chat_id,
        )
        try:
            self._submit_monitor(monitor)
        except Exception as error:  # noqa: BLE001  下载继续运行，交互降级为手动刷新
            monitor.close()
            with _monitor_lock:
                _monitored_requests.discard(request.request_id)
            logger.warning(f"登记 MoviePilot 更新进度监视失败：{error}")
            self._renderer.post_view(
                view=SystemUpdateInteractionView(
                    title="更新包已开始下载",
                    text="自动进度更新暂不可用，可重新发送 /update 查看状态。",
                ),
                channel=channel, source=source, userid=userid, username=username,
                original_message_id=original_message_id,
                original_chat_id=original_chat_id,
            )

    async def _monitor_download(
        self, *, request_id: str, channel: NotificationChannel,
        source: Optional[str], userid: Union[str, int], username: Optional[str],
        initial_item: SystemUpdateItemStatus,
        original_message_id: Optional[Union[str, int]], original_chat_id: Optional[str],
    ) -> None:
        """按 Web 端三秒节奏轮询状态，并持续编辑原消息直到下载终态。"""
        last_fingerprint = self._renderer.item_fingerprint(initial_item)
        last_progress_bucket = initial_item.progress // 10
        edit_fallback_sent = False
        try:
            while True:
                await asyncio.sleep(self._poll_interval_seconds)
                request = update_interaction_manager.get_by_id(request_id, userid)
                if request is None:
                    return
                status = await self._run_sync(self._actions.update_status)
                item = self._renderer.application_item(status)
                fingerprint = self._renderer.item_fingerprint(item)
                if fingerprint != last_fingerprint:
                    request.awaiting_input = self._renderer.awaiting_input(item)
                    view = self._renderer.build_view(
                        request=request,
                        item=item,
                        channel=channel,
                    )
                    if original_message_id and original_chat_id and ChannelCapabilityManager.supports_editing(channel):
                        edited = await self._run_sync(
                            self._renderer.edit_view,
                            view=view,
                            channel=channel,
                            source=source,
                            userid=userid,
                            original_message_id=original_message_id,
                            original_chat_id=original_chat_id,
                        )
                        if not edited and (not edit_fallback_sent or item.state in self._terminal_download_states):
                            await self._run_sync(
                                self._renderer.post_view,
                                view=view,
                                channel=channel,
                                source=source,
                                userid=userid,
                                username=username,
                                original_message_id=original_message_id,
                                original_chat_id=original_chat_id,
                            )
                            edit_fallback_sent = True
                    else:
                        progress_bucket = item.progress // 10
                        if progress_bucket != last_progress_bucket or item.state in self._terminal_download_states:
                            await self._run_sync(
                                self._renderer.post_view,
                                view=view,
                                channel=channel,
                                source=source,
                                userid=userid,
                                username=username,
                            )
                            last_progress_bucket = progress_bucket
                    last_fingerprint = fingerprint

                if item.state != "downloading":
                    if item.state == "idle":
                        update_interaction_manager.remove(request_id)
                    return
        except Exception as error:  # noqa: BLE001  监视错误不能影响实际下载
            logger.warning(f"监视 MoviePilot 更新下载进度失败：{error}")
            request = update_interaction_manager.get_by_id(request_id, userid)
            if request is not None:
                await self._run_sync(
                    self._renderer.render_operation_failure,
                    request=request,
                    channel=channel,
                    source=source,
                    userid=userid,
                    username=username,
                    error=f"读取下载进度失败：{error}",
                    original_message_id=original_message_id,
                    original_chat_id=original_chat_id,
                )
        finally:
            with _monitor_lock:
                _monitored_requests.discard(request_id)

class _SystemUpdateRenderer:
    """把主程序更新状态转换为渠道消息并负责发送或编辑。"""

    def __init__(
        self, *, messenger: MessageGateway, actions: SystemUpdateInteractionActions,
    ) -> None:
        """注入消息网关与更新状态读取用例。"""
        self._messenger = messenger
        self._actions = actions

    def render_status(
        self, *, request: PendingSlashInteraction, status: SystemUpdateStatus,
        channel: NotificationChannel, source: Optional[str],
        userid: Union[str, int], username: Optional[str],
        operation_error: Optional[str] = None,
        original_message_id: Optional[Union[str, int]] = None, original_chat_id: Optional[str] = None,
    ) -> None:
        """从聚合更新快照提取主程序状态并更新交互消息。"""
        item = self.application_item(status)
        self.render_item(
            request=request,
            item=item,
            channel=channel,
            source=source,
            userid=userid,
            username=username,
            operation_error=operation_error,
            original_message_id=original_message_id,
            original_chat_id=original_chat_id,
        )
        if item.state == "idle" and not item.error and not operation_error:
            update_interaction_manager.remove(request.request_id)

    def render_item(
        self, *, request: PendingSlashInteraction, item: SystemUpdateItemStatus,
        channel: NotificationChannel, source: Optional[str],
        userid: Union[str, int], username: Optional[str],
        operation_error: Optional[str] = None,
        original_message_id: Optional[Union[str, int]] = None, original_chat_id: Optional[str] = None,
    ) -> None:
        """更新会话阶段并优先编辑原消息展示指定主程序状态。"""
        request.awaiting_input = self.awaiting_input(item)
        view = self.build_view(
            request=request,
            item=item,
            channel=channel,
            operation_error=operation_error,
        )
        update_or_post_message(
            chain=self._messenger,
            channel=channel,
            source=source,
            userid=userid,
            username=username,
            title=view.title,
            text=view.text,
            buttons=view.buttons,
            original_message_id=original_message_id,
            original_chat_id=original_chat_id,
        )

    def render_check_failure(
        self, *, request: PendingSlashInteraction, channel: NotificationChannel,
        source: Optional[str], userid: Union[str, int], username: Optional[str], error: str,
        original_message_id: Optional[Union[str, int]] = None, original_chat_id: Optional[str] = None,
    ) -> None:
        """展示版本检查失败并保留刷新入口。"""
        request.awaiting_input = "refresh"
        buttons = self._buttons(
            request=request,
            channel=channel,
            actions=(("重新检查", "refresh"), ("关闭", "close")),
        )
        text = f"{error or '无法读取更新状态'}\n\n"
        if not buttons:
            text += "回复“刷新”重试，回复“关闭”结束。"
        update_or_post_message(
            chain=self._messenger,
            channel=channel,
            source=source,
            userid=userid,
            username=username,
            title="检查 MoviePilot 更新失败",
            text=text.strip(),
            buttons=buttons,
            original_message_id=original_message_id,
            original_chat_id=original_chat_id,
        )

    def render_operation_failure(
        self, *, request: PendingSlashInteraction, channel: NotificationChannel,
        source: Optional[str], userid: Union[str, int], username: Optional[str], error: str,
        original_message_id: Optional[Union[str, int]] = None, original_chat_id: Optional[str] = None,
    ) -> None:
        """读取最新状态后展示下载或安装动作失败。"""
        try:
            status = self._actions.update_status()
        except Exception:  # noqa: BLE001  保留原始动作错误
            self.render_check_failure(
                request=request,
                channel=channel,
                source=source,
                userid=userid,
                username=username,
                error=error,
                original_message_id=original_message_id,
                original_chat_id=original_chat_id,
            )
            return
        self.render_status(
            request=request,
            status=status,
            channel=channel,
            source=source,
            userid=userid,
            username=username,
            operation_error=error,
            original_message_id=original_message_id,
            original_chat_id=original_chat_id,
        )

    def build_view(
        self, *, request: PendingSlashInteraction, item: SystemUpdateItemStatus,
        channel: NotificationChannel,
        operation_error: Optional[str] = None,
    ) -> SystemUpdateInteractionView:
        """把主程序更新状态转换为与 Web 流程一致的渠道展示。"""
        actions: tuple[tuple[str, str], ...]
        if item.state == "available":
            title = "发现 MoviePilot 主程序更新"
            lines = [f"当前版本：{item.current_version or '未知'}", f"目标版本：{item.version or '未知'}"]
            if item.frontend_version:
                lines.append(f"配套前端：{item.frontend_version}")
            if item.release_name and item.release_name != item.version:
                lines.append(f"发布名称：{item.release_name}")
            if item.published_at:
                lines.append(f"发布时间：{item.published_at}")
            notes = self._release_notes(item.release_notes)
            if notes:
                lines.extend(("", "更新说明：", notes))
            actions = (("确认升级", "download"), ("稍后", "close"))
            fallback = "回复“确认升级”开始下载，回复“稍后”关闭本次交互。"
        elif item.state == "downloading":
            title = "正在下载 MoviePilot 更新"
            lines = [
                f"目标版本：{item.version or '未知'}",
                self._progress_line(item),
                "下载完成后会继续提示确认重启。",
            ]
            actions = ()
            fallback = ""
        elif item.state == "ready":
            title = "MoviePilot 更新包已准备完成"
            lines = [
                f"目标版本：{item.version or '未知'}",
                "更新包已下载并校验完成。确认后系统将重启并安装更新。",
            ]
            if item.frontend_version:
                lines.insert(1, f"配套前端：{item.frontend_version}")
            actions = (("确认重启", "install"), ("稍后重启", "close"))
            fallback = "回复“确认重启”开始安装，回复“稍后”关闭本次交互。"
        elif item.state == "installing":
            title = "正在重启并安装 MoviePilot 更新"
            lines = ["重启请求已提交，请等待服务恢复。"]
            actions = ()
            fallback = ""
        elif item.state == "failed":
            title = "MoviePilot 更新下载失败"
            lines = [item.error or "更新包下载失败，请重试。"]
            actions = (("重试", "download"), ("关闭", "close"))
            fallback = "回复“重试”重新下载，回复“关闭”结束。"
        elif item.error:
            title = "检查 MoviePilot 更新失败"
            lines = [item.error]
            actions = (("重新检查", "refresh"), ("关闭", "close"))
            fallback = "回复“刷新”重试，回复“关闭”结束。"
        else:
            title = "MoviePilot 已是最新版本"
            lines = [f"当前版本：{item.current_version or '未知'}"]
            actions = ()
            fallback = ""

        if operation_error:
            title = "MoviePilot 升级操作失败"
            lines = [operation_error, "", *lines]
        buttons = self._buttons(request=request, channel=channel, actions=actions)
        if fallback and not buttons:
            lines.extend(("", fallback))
        return SystemUpdateInteractionView(
            title=title,
            text="\n".join(lines).strip(),
            buttons=buttons,
        )

    @staticmethod
    def application_item(status: SystemUpdateStatus) -> SystemUpdateItemStatus:
        """读取主程序明细，并兼容旧版只有聚合字段的状态。"""
        item = next((value for value in status.updates if value.type == "application"), None)
        if item is not None:
            return item
        return SystemUpdateItemStatus(
            type="application",
            state=status.state,
            current_version=status.current_version,
            version=status.version,
            frontend_version=status.frontend_version,
            release_name=status.release_name,
            release_notes=status.release_notes,
            published_at=status.published_at,
            checked_at=status.checked_at,
            downloaded_bytes=status.downloaded_bytes,
            total_bytes=status.total_bytes,
            progress=status.progress,
            error=status.error,
            can_update=status.can_update,
            can_install=status.can_install,
        )

    @staticmethod
    def awaiting_input(item: SystemUpdateItemStatus) -> Optional[str]:
        """把更新状态映射为文本渠道下一步输入阶段。"""
        if item.state in {"available", "failed"}:
            return "download"
        if item.state == "ready":
            return "install"
        if item.error:
            return "refresh"
        return None

    @staticmethod
    def item_fingerprint(item: SystemUpdateItemStatus) -> tuple[Any, ...]:
        """生成需要刷新消息的状态指纹。"""
        return (
            item.state,
            item.version,
            item.frontend_version,
            item.downloaded_bytes,
            item.total_bytes,
            item.progress,
            item.error,
            item.can_update,
            item.can_install,
        )

    @staticmethod
    def _release_notes(notes: Optional[str], limit: int = 1200) -> str:
        """限制发布说明长度，避免超过通知渠道单条消息上限。"""
        normalized = str(notes or "").strip()
        if len(normalized) <= limit:
            return normalized
        return f"{normalized[:limit].rstrip()}..."

    @staticmethod
    def _format_bytes(value: int) -> str:
        """使用与 Web 端相同的 MB 展示下载量。"""
        megabytes = max(0, int(value or 0)) / 1024 / 1024
        return f"{megabytes:.0f} MB" if megabytes >= 100 else f"{megabytes:.1f} MB"

    @classmethod
    def _progress_line(cls, item: SystemUpdateItemStatus) -> str:
        """构造宽度稳定的文本进度条和下载量。"""
        progress = min(100, max(0, int(item.progress or 0)))
        filled = min(10, progress // 10)
        progress_bar = f"[{'=' * filled}{'.' * (10 - filled)}] {progress}%"
        downloaded = cls._format_bytes(item.downloaded_bytes)
        if item.total_bytes > 0:
            return f"{progress_bar}\n{downloaded} / {cls._format_bytes(item.total_bytes)}"
        return f"{progress_bar}\n已下载 {downloaded}"

    @staticmethod
    def _buttons(
        *,
        request: PendingSlashInteraction,
        channel: NotificationChannel,
        actions: tuple[tuple[str, str], ...],
    ) -> Optional[list[list[dict[str, str]]]]:
        """为支持按钮回调的渠道构造单行操作按钮。"""
        if not actions or not supports_interaction_buttons(channel):
            return None
        return [
            [
                {
                    "text": text,
                    "callback_data": f"update:{request.request_id}:{action}",
                }
                for text, action in actions
            ]
        ]

    def edit_view(
        self, *, view: SystemUpdateInteractionView, channel: NotificationChannel,
        source: Optional[str], userid: Union[str, int],
        original_message_id: Union[str, int],
        original_chat_id: str,
    ) -> bool:
        """直接编辑进度锚点，避免轮询阶段重复发送消息。"""
        if not source:
            return False
        metadata = {"userid": userid} if channel == NotificationChannel.WebAgent else None
        return bool(
            self._messenger.edit_message(
                channel=channel,
                source=source,
                message_id=original_message_id,
                chat_id=original_chat_id,
                title=view.title,
                text=view.text,
                buttons=view.buttons,
                metadata=metadata,
            )
        )

    def post_view(
        self, *, view: SystemUpdateInteractionView, channel: NotificationChannel,
        source: Optional[str], userid: Union[str, int], username: Optional[str],
        original_message_id: Optional[Union[str, int]] = None, original_chat_id: Optional[str] = None,
    ) -> None:
        """在无法编辑时发送一次进度或终态消息。"""
        self._messenger.post_message(
            Message(
                channel=channel,
                source=source,
                userid=userid,
                username=username,
                title=view.title,
                text=view.text,
                buttons=view.buttons,
                original_message_id=original_message_id,
                original_chat_id=original_chat_id,
                save_history=False,
            )
        )
