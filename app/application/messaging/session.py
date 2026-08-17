"""消息入口的用户会话状态用例。"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, MutableMapping, Optional, Union


UserId = Union[str, int]
SessionEntry = tuple[str, datetime]
ExpiredSessionHandler = Callable[[str, UserId], None]
Clock = Callable[[], datetime]
SessionIdFactory = Callable[[UserId, datetime], str]


@dataclass(frozen=True, slots=True)
class SessionResolution:
    """描述用户会话解析结果及是否复用了旧会话。"""

    session_id: str
    reused: bool
    inactive_minutes: float = 0.0


class MessageSessionService:
    """管理消息用户到 Agent 会话的绑定、复用和过期清理。"""

    def __init__(
            self,
            *,
            sessions: MutableMapping[UserId, SessionEntry],
            timeout_minutes: int,
            expired_handler: ExpiredSessionHandler,
            clock: Clock = datetime.now,
            session_id_factory: Optional[SessionIdFactory] = None,
    ) -> None:
        """保存共享会话映射和由 Chain 提供的 Agent 清理端口。"""
        self._sessions = sessions
        self._timeout = timedelta(minutes=timeout_minutes)
        self._expired_handler = expired_handler
        self._clock = clock
        self._session_id_factory = session_id_factory or self._default_session_id

    @staticmethod
    def _default_session_id(user_id: UserId, now: datetime) -> str:
        """按历史格式生成新的用户会话 ID。"""
        return f"user_{user_id}_{int(now.timestamp())}"

    def cleanup(self, now: Optional[datetime] = None) -> None:
        """移除超时绑定，并通知拥有者释放对应 Agent 会话。"""
        current_time = now or self._clock()
        for user_id, (session_id, last_time) in list(self._sessions.items()):
            if current_time - last_time <= self._timeout:
                continue
            self._sessions.pop(user_id, None)
            self._expired_handler(session_id, user_id)

    def resolve(self, user_id: UserId) -> SessionResolution:
        """复用有效绑定或为用户创建新会话。"""
        current_time = self._clock()
        self.cleanup(current_time)
        current = self._sessions.get(user_id)
        if current:
            session_id, last_time = current
            inactive = current_time - last_time
            if inactive <= self._timeout:
                self._sessions[user_id] = (session_id, current_time)
                return SessionResolution(
                    session_id=session_id,
                    reused=True,
                    inactive_minutes=inactive.total_seconds() / 60,
                )

        session_id = self._session_id_factory(user_id, current_time)
        self._sessions[user_id] = (session_id, current_time)
        return SessionResolution(session_id=session_id, reused=False)

    def bind(self, user_id: UserId, session_id: str) -> None:
        """绑定指定会话，并在替换时释放旧会话。"""
        current = self._sessions.get(user_id)
        if current and current[0] != session_id:
            self._expired_handler(current[0], user_id)
        self._sessions[user_id] = (session_id, self._clock())

    def clear(self, user_id: UserId) -> Optional[str]:
        """清除用户绑定并返回被移除的会话 ID。"""
        current = self._sessions.pop(user_id, None)
        return current[0] if current else None

    def get(self, user_id: UserId) -> Optional[SessionEntry]:
        """读取用户当前会话绑定，不改变最后活动时间。"""
        return self._sessions.get(user_id)
