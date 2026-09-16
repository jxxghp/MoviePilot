"""Agent 运行中消息的会话级排队、注入与状态回调。"""

from __future__ import annotations

import asyncio
import contextvars
import uuid
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Deque, Optional

STEERING_QUEUE_MAX_SIZE = 8


@dataclass(frozen=True, slots=True)
class SteeringMessage:
    """保存一条等待注入当前 Agent 运行的用户消息。"""

    message_id: str
    session_id: str
    user_id: str
    text: str
    images: tuple[str, ...] = ()
    files: tuple[dict[str, Any], ...] = ()
    created_at: str = ""

    @classmethod
    def create(
        cls,
        *,
        session_id: str,
        user_id: str,
        text: str,
        images: Optional[list[str]] = None,
        files: Optional[list[dict[str, Any]]] = None,
    ) -> "SteeringMessage":
        """生成宿主侧消息 ID，并复制输入附件以隔离调用方可变对象。"""
        return cls(
            message_id=uuid.uuid4().hex,
            session_id=session_id,
            user_id=user_id,
            text=str(text or ""),
            images=tuple(str(image) for image in (images or [])),
            files=tuple(dict(file) for file in (files or [])),
            created_at=datetime.now(timezone.utc).isoformat(),
        )


SteeringStatusCallback = Callable[[SteeringMessage, str], None]


class SteeringInbox:
    """为一个用户会话管理运行中消息，并以锁定义提交与收尾的原子边界。"""

    def __init__(self, session_id: str, user_id: str) -> None:
        """创建空 inbox；消息只接受固定会话和用户身份。"""
        self.session_id = session_id
        self.user_id = str(user_id)
        self._pending: Deque[SteeringMessage] = deque()
        self._lock = asyncio.Lock()
        self._running = False
        self._closed = False
        self._status_callback: Optional[SteeringStatusCallback] = None

    @property
    def running(self) -> bool:
        """返回当前运行是否仍接受 steering 消息。"""
        return self._running and not self._closed

    async def begin_run(self, status_callback: Optional[SteeringStatusCallback] = None) -> None:
        """原子打开一次运行窗口，并绑定当前运行的状态发布回调。"""
        async with self._lock:
            if self._closed:
                return
            self._running = True
            self._status_callback = status_callback

    async def finish_run(self) -> tuple[SteeringMessage, ...]:
        """原子关闭运行窗口并返回收尾竞态中尚未注入的消息。"""
        async with self._lock:
            self._running = False
            self._status_callback = None
            messages = tuple(self._pending)
            self._pending.clear()
            return messages

    async def enqueue(
        self,
        *,
        user_id: str,
        text: str,
        images: Optional[list[str]] = None,
        files: Optional[list[dict[str, Any]]] = None,
    ) -> Optional[SteeringMessage]:
        """在运行窗口内排队消息；窗口结束的瞬间会原子地回退到普通轮次。"""
        async with self._lock:
            if self._closed or not self._running or str(user_id) != self.user_id:
                return None
            if len(self._pending) >= STEERING_QUEUE_MAX_SIZE:
                return None
            message = SteeringMessage.create(
                session_id=self.session_id,
                user_id=self.user_id,
                text=text,
                images=images,
                files=files,
            )
            self._pending.append(message)
            return message

    async def consume(self) -> tuple[SteeringMessage, ...]:
        """在下一次模型调用前一次性取出消息，并通知原 SSE 已应用。"""
        async with self._lock:
            if not self._pending:
                return ()
            messages = tuple(self._pending)
            self._pending.clear()
            callback = self._status_callback
        if callback is not None:
            for message in messages:
                try:
                    callback(message, "applied")
                except Exception:
                    # 状态展示失败不能阻断真实模型请求；消息已经进入图状态。
                    continue
        return messages

    def consume_nowait(self) -> tuple[SteeringMessage, ...]:
        """为同步 Agent 调用提供无等待消费；并发持锁时安全地等待下一轮。"""
        if self._lock.locked() or not self._pending:
            return ()
        messages = tuple(self._pending)
        self._pending.clear()
        callback = self._status_callback
        if callback is not None:
            for message in messages:
                try:
                    callback(message, "applied")
                except Exception:
                    continue
        return messages

    @property
    def pending_count(self) -> int:
        """返回当前尚未进入模型上下文的消息数量。"""
        return len(self._pending)

    async def close(self) -> tuple[SteeringMessage, ...]:
        """关闭 inbox 并返回仍待处理的消息，供 owner 在停止路径决定收口策略。"""
        async with self._lock:
            self._closed = True
            self._running = False
            self._status_callback = None
            messages = tuple(self._pending)
            self._pending.clear()
            return messages


_current_steering_inbox: contextvars.ContextVar[Optional[SteeringInbox]] = contextvars.ContextVar(
    "moviepilot_current_steering_inbox",
    default=None,
)


def bind_steering_inbox(inbox: Optional[SteeringInbox]) -> contextvars.Token[Optional[SteeringInbox]]:
    """把当前 Agent 图执行绑定到对应会话的 inbox。"""
    return _current_steering_inbox.set(inbox)


def reset_steering_inbox(token: contextvars.Token[Optional[SteeringInbox]]) -> None:
    """恢复图执行前的 inbox 上下文。"""
    _current_steering_inbox.reset(token)


def current_steering_inbox() -> Optional[SteeringInbox]:
    """读取当前模型调用所属的会话 inbox。"""
    return _current_steering_inbox.get()


__all__ = [
    "STEERING_QUEUE_MAX_SIZE",
    "SteeringInbox",
    "SteeringMessage",
    "SteeringStatusCallback",
    "bind_steering_inbox",
    "current_steering_inbox",
    "reset_steering_inbox",
]
