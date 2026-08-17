from datetime import datetime, timedelta
from unittest.mock import Mock

from app.application.messaging.session import MessageSessionService


def test_message_session_service_reuses_and_refreshes_active_session():
    """复用窗口内应返回原会话并刷新最后活动时间。"""
    now = datetime(2026, 8, 17, 12, 0, 0)
    sessions = {"user": ("session-1", now - timedelta(minutes=5))}
    service = MessageSessionService(
        sessions=sessions,
        timeout_minutes=60,
        expired_handler=Mock(),
        clock=lambda: now,
    )

    result = service.resolve("user")

    assert result.session_id == "session-1"
    assert result.reused is True
    assert result.inactive_minutes == 5
    assert sessions["user"] == ("session-1", now)


def test_message_session_service_cleans_expired_before_creating_session():
    """创建新会话前应释放同一映射中的全部过期 Agent 会话。"""
    now = datetime(2026, 8, 17, 12, 0, 0)
    sessions = {"old": ("session-old", now - timedelta(minutes=61))}
    expired_handler = Mock()
    service = MessageSessionService(
        sessions=sessions,
        timeout_minutes=60,
        expired_handler=expired_handler,
        clock=lambda: now,
        session_id_factory=lambda user_id, _now: f"new-{user_id}",
    )

    result = service.resolve("new")

    assert result.session_id == "new-new"
    assert result.reused is False
    assert "old" not in sessions
    expired_handler.assert_called_once_with("session-old", "old")


def test_message_session_service_bind_and_clear_preserve_old_cleanup_contract():
    """替换绑定时释放旧会话，显式清理只返回被移除的会话 ID。"""
    now = datetime(2026, 8, 17, 12, 0, 0)
    sessions = {"user": ("session-old", now)}
    expired_handler = Mock()
    service = MessageSessionService(
        sessions=sessions,
        timeout_minutes=60,
        expired_handler=expired_handler,
        clock=lambda: now,
    )

    service.bind("user", "session-new")

    expired_handler.assert_called_once_with("session-old", "user")
    assert service.get("user") == ("session-new", now)
    assert service.clear("user") == "session-new"
    assert service.clear("user") is None
