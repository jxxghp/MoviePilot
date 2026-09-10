"""WebAgent 主运行流和运行中补充消息的展示桥接。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from functools import partial
from typing import Any, AsyncIterator, Awaitable, Callable, Optional, cast

from app.agent.steering import SteeringMessage
from app.application import agent as agent_application
from app.runtime.log import logger
from app.runtime.stop import runtime_stop_state
from app.schemas.message import Message
from app.schemas.types import NotificationChannel, ReplyMode


@dataclass(frozen=True, slots=True)
class WebAgentStreamDependencies:
    """注入 WebAgent 流所需的展示、事件和后台任务能力，避免模块相互导入。"""

    event_publisher_factory: Callable[[], Any]
    bind_user_session: Callable[[str, str], None]
    apply_display_event: Callable[[dict[str, Any], dict[str, Any]], None]
    build_display_message: Callable[..., dict[str, Any]]
    build_input_attachments: Callable[..., list[dict[str, Any]]]
    build_message_events_async: Callable[[Message], Awaitable[list[dict[str, Any]]]]
    save_display_snapshot: Callable[..., Awaitable[None]]
    split_output: Callable[[str], list[dict[str, Any]]]
    create_background_task: Callable[[Awaitable[object]], asyncio.Task[object]]
    source: str
    heartbeat_seconds: float


async def submit_web_agent_steering(
    *,
    manager: Any,
    session_id: str,
    user_id: str,
    prompt: str,
    images: list[str],
    files: list[dict[str, Any]],
    audio_refs: list[str],
) -> Optional[SteeringMessage]:
    """把活动 WebAgent 的补充输入提交到会话 inbox；无活动运行时返回 None。"""
    steering_files = [file for file in files if str(file.get("ref") or "") not in set(audio_refs)]
    steering_files.extend({"ref": audio_ref, "mime_type": "audio/*"} for audio_ref in audio_refs)
    submit = getattr(manager, "submit_steering_message", None)
    if not callable(submit):
        return None
    return cast(
        Optional[SteeringMessage],
        await submit(
            session_id=session_id,
            user_id=user_id,
            message=prompt,
            images=images,
            files=steering_files or None,
        ),
    )


def _build_steering_ack_stream(
    *,
    session_id: str,
    message: SteeringMessage,
) -> AsyncIterator[dict[str, Any]]:
    """为运行中补充消息返回短 ACK 流，不创建第二个助手展示气泡。"""

    async def event_generator() -> AsyncIterator[dict[str, Any]]:
        """按统一 WebAgent SSE 协议报告已排队的消息 ID。"""
        yield {"type": "start", "session_id": session_id}
        yield {
            "type": "steering",
            "status": "queued",
            "message_id": message.message_id,
            "content": message.text,
        }
        yield {"type": "done"}

    return event_generator()


def _publish_web_agent_protected_output(
    event_publisher: Any,
    content: str,
) -> bool:
    """发布不进入普通展示快照的敏感交互结果。"""
    return bool(event_publisher.publish({"type": "interaction-protected", "content": content}))


def _build_web_agent_steering_callback(
    *,
    display_messages: list[dict[str, Any]],
    event_publisher: Any,
    build_display_message: Callable[..., dict[str, Any]],
    build_input_attachments: Callable[..., list[dict[str, Any]]],
) -> Callable[[SteeringMessage, str], None]:
    """构造运行中补充消息的应用回调，并同步展示快照与 SSE。"""

    def steering_status_callback(message: SteeringMessage, status: str) -> None:
        """把补充消息应用状态投影到当前助手流，保持单一助手气泡。"""
        if status != "applied":
            return
        display_message = build_display_message(
            role="user",
            content=message.text,
            attachments=build_input_attachments(
                images=list(message.images),
                files=list(message.files),
                audio_refs=[],
            ),
        )
        # 当前助手气泡必须继续保持最后一项，补充的用户消息插入其前面。
        display_messages.insert(max(0, len(display_messages) - 1), display_message)
        event_publisher.publish(
            {
                "type": "steering",
                "status": "applied",
                "message_id": message.message_id,
                "content": message.text,
                "display_message": display_message,
            }
        )

    return steering_status_callback


def _build_web_agent_message_callback(
    *,
    assistant_display_message: dict[str, Any],
    event_publisher: Any,
    build_message_events_async: Callable[[Message], Awaitable[list[dict[str, Any]]]],
    apply_display_event: Callable[[dict[str, Any], dict[str, Any]], None],
) -> Callable[[Message], Awaitable[None]]:
    """构造 Agent 主动消息的 Web 展示回调。"""

    async def message_callback(message: Message) -> None:
        """接收 Agent 工具主动发送的 Web 通知。"""
        for item in await build_message_events_async(message):
            apply_display_event(item, assistant_display_message)
            event_publisher.publish(item)

    return message_callback


def _build_web_agent_output_callback(
    *,
    assistant_display_message: dict[str, Any],
    event_publisher: Any,
    split_output: Callable[[str], list[dict[str, Any]]],
    apply_display_event: Callable[[dict[str, Any], dict[str, Any]], None],
) -> Callable[[str], None]:
    """构造 Agent 文本增量的 Web 展示回调。"""

    def output_callback(delta: str) -> None:
        """接收 Agent 文本增量并投影为展示事件。"""
        for item in split_output(delta):
            apply_display_event(item, assistant_display_message)
            event_publisher.publish(item)

    return output_callback


def _build_web_agent_files(command: Any) -> list[dict[str, Any]]:
    """把 WebAgent 命令中的普通文件和音频引用整理成 Agent 输入。"""
    audio_refs = {str(audio_ref) for audio_ref in command.audio_refs}
    files = [file for file in command.files if str(file.get("ref") or "") not in audio_refs]
    files.extend({"ref": audio_ref, "mime_type": "audio/*"} for audio_ref in command.audio_refs)
    return files


def _build_web_agent_event_generator(
    *,
    command: Any,
    current_user: Any,
    session_id: str,
    prompt: str,
    display_messages: list[dict[str, Any]],
    assistant_display_message: dict[str, Any],
    has_audio_input: bool,
    is_secret_confirmation_control: bool,
    protected_transport_supported: bool,
    service: Any,
    persistence: Any,
    is_disconnected: Callable[[], Awaitable[bool]],
    dependencies: WebAgentStreamDependencies,
    event_publisher: Any,
    output_callback: Callable[[str], None],
    message_callback: Callable[[Message], Awaitable[None]],
    protected_output_callback: Callable[[str], bool],
    steering_status_callback: Callable[[SteeringMessage, str], None],
) -> AsyncIterator[dict[str, Any]]:
    """构造后台 Agent 任务并按断线与终态语义消费展示事件。"""
    files = _build_web_agent_files(command)

    async def run_agent() -> None:
        """后台执行 Agent，并在完成后持久化展示快照。"""
        try:
            runtime_manager = agent_application.get_running_agent_manager()
            if runtime_manager is None:
                raise RuntimeError("智能助手服务尚未就绪，请稍后重试。")
            await runtime_manager.process_message(
                session_id=session_id,
                user_id=str(current_user.id),
                message=prompt,
                images=command.images,
                files=files or None,
                has_audio_input=has_audio_input,
                channel=NotificationChannel.WebAgent.value,
                source=dependencies.source,
                username=current_user.name,
                reply_mode=ReplyMode.CAPTURE_ONLY,
                allow_message_tools=True,
                output_callback=output_callback,
                protected_output_callback=(protected_output_callback if protected_transport_supported else None),
                message_callback=message_callback,
                steering_status_callback=steering_status_callback,
                agent_factory=agent_application.get_web_agent_type(),
                wait_for_completion=True,
            )
        except asyncio.CancelledError:
            # 显式停止会话沿用正常终止语义；服务关闭由 manager 的稳定异常分支处理。
            pass
        except Exception as err:
            logger.error(f"Web智能助手执行失败: {str(err)}")
            error_event = {
                "type": "error",
                "message": "智能助手执行失败，请稍后重试",
            }
            dependencies.apply_display_event(error_event, assistant_display_message)
            event_publisher.publish(error_event)
        finally:
            done_event = {"type": "done"}
            dependencies.apply_display_event(done_event, assistant_display_message)
            # 终态先进入事件队列，避免展示快照落库延迟前端结束动画。
            event_publisher.publish(done_event)
            if not is_secret_confirmation_control:
                try:
                    await dependencies.save_display_snapshot(
                        session_id=session_id,
                        current_user=current_user,
                        messages=display_messages,
                        client_session_id=command.session_id or session_id,
                        service=service,
                        persistence=persistence,
                    )
                except Exception as err:
                    logger.error(f"保存WebAgent展示历史失败：{err}")

    async def event_generator() -> AsyncIterator[dict[str, Any]]:
        """执行后台 Agent 并在客户端断线时保留运行任务。"""
        task = dependencies.create_background_task(run_agent())
        disconnected = False
        terminal_sent = False
        try:
            yield {"type": "start", "session_id": session_id}
            while not runtime_stop_state.is_system_stopped:
                if await is_disconnected():
                    disconnected = True
                    break
                try:
                    event = await asyncio.wait_for(
                        event_publisher.get(),
                        timeout=dependencies.heartbeat_seconds,
                    )
                except asyncio.TimeoutError:
                    yield {"type": "heartbeat"}
                    continue
                if event.get("type") == "done":
                    terminal_sent = True
                yield event
                if event.get("type") == "done":
                    break
        except asyncio.CancelledError:
            disconnected = True
            return
        finally:
            if not task.done() and not disconnected and not terminal_sent:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            await event_publisher.aclose()
            # 客户端断线后保留 Agent 继续执行；发布器关闭后拒绝受保护结果。

    return event_generator()


def build_agent_web_agent_stream(
    *,
    command: Any,
    current_user: Any,
    session_id: str,
    prompt: str,
    display_prompt: str,
    has_audio_input: bool,
    is_secret_confirmation_control: bool,
    protected_transport_supported: bool,
    service: Any,
    persistence: Any,
    is_disconnected: Callable[[], Awaitable[bool]],
    dependencies: WebAgentStreamDependencies,
) -> AsyncIterator[dict[str, Any]]:
    """构造标准 Agent 执行链路的 WebAgent 事件流。"""
    dependencies.bind_user_session(str(current_user.id), session_id)
    event_publisher = dependencies.event_publisher_factory()
    user_attachments = dependencies.build_input_attachments(
        images=command.images,
        files=command.files,
        audio_refs=command.audio_refs,
    )
    display_messages = []
    if command.echo_user and not is_secret_confirmation_control:
        user_display_message = dependencies.build_display_message(
            role="user",
            content=display_prompt or prompt,
            attachments=user_attachments,
        )
        if command.choice_selection:
            user_display_message["choice_selection"] = command.choice_selection
        display_messages.append(user_display_message)
    assistant_display_message = dependencies.build_display_message(
        role="assistant",
        status="streaming",
    )
    display_messages.append(assistant_display_message)
    output_callback = _build_web_agent_output_callback(
        assistant_display_message=assistant_display_message,
        event_publisher=event_publisher,
        split_output=dependencies.split_output,
        apply_display_event=dependencies.apply_display_event,
    )
    message_callback = _build_web_agent_message_callback(
        assistant_display_message=assistant_display_message,
        event_publisher=event_publisher,
        build_message_events_async=dependencies.build_message_events_async,
        apply_display_event=dependencies.apply_display_event,
    )
    protected_output_callback = partial(_publish_web_agent_protected_output, event_publisher)
    steering_status_callback = _build_web_agent_steering_callback(
        display_messages=display_messages,
        event_publisher=event_publisher,
        build_display_message=dependencies.build_display_message,
        build_input_attachments=dependencies.build_input_attachments,
    )

    return _build_web_agent_event_generator(
        command=command,
        current_user=current_user,
        session_id=session_id,
        prompt=prompt,
        display_messages=display_messages,
        assistant_display_message=assistant_display_message,
        has_audio_input=has_audio_input,
        is_secret_confirmation_control=is_secret_confirmation_control,
        protected_transport_supported=protected_transport_supported,
        service=service,
        persistence=persistence,
        is_disconnected=is_disconnected,
        dependencies=dependencies,
        event_publisher=event_publisher,
        output_callback=output_callback,
        message_callback=message_callback,
        protected_output_callback=protected_output_callback,
        steering_status_callback=steering_status_callback,
    )


__all__ = [
    "WebAgentStreamDependencies",
    "_build_steering_ack_stream",
    "build_agent_web_agent_stream",
    "submit_web_agent_steering",
]
