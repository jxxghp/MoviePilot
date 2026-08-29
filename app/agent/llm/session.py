"""LLM Provider 临时授权会话状态的唯一 owner。"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from app.agent.llm.runtime import LLMProviderAuthError


@dataclass
class PendingAuthSession:
    """保存临时鉴权会话，避免把 PKCE/device code 等状态写回配置。"""

    session_id: str
    provider_id: str
    method_id: str
    flow_type: str
    status: str = "pending"
    message: str = ""
    authorize_url: Optional[str] = None
    instructions: Optional[str] = None
    verification_url: Optional[str] = None
    user_code: Optional[str] = None
    interval_seconds: int = 5
    expires_at: float = 0
    created_at: float = field(default_factory=time.time)
    context: Dict[str, Any] = field(default_factory=dict)


class _ProviderSession:
    """LLM Provider 临时授权会话状态的唯一 owner。"""

    _lock: Any
    _pending_sessions: dict[str, PendingAuthSession]
    _oauth_state_index: dict[str, str]

    def __getattr__(self, name: str) -> Any:
        """将跨 owner 调用交给最终 Facade 的 MRO 解析。"""
        raise AttributeError(name)

    _AUTH_SESSION_DONE_RETENTION = 300

    def _cleanup_auth_sessions_locked(self, now: Optional[float] = None) -> None:
        """
        清理过期或已完成一段时间的临时授权会话。

        调用方必须已经持有 `_lock`，这样 `_pending_sessions` 与
        `_oauth_state_index` 能保持一致，避免 state 残留。
        """
        now = time.time() if now is None else now
        expired_session_ids = []
        for session_id, session in self._pending_sessions.items():
            expires_at = session.expires_at or session.created_at + 600
            if session.status == "pending":
                if expires_at <= now:
                    expired_session_ids.append(session_id)
            elif expires_at + self._AUTH_SESSION_DONE_RETENTION <= now:
                expired_session_ids.append(session_id)

        if not expired_session_ids:
            return

        expired_session_ids_set = set(expired_session_ids)
        for session_id in expired_session_ids:
            self._pending_sessions.pop(session_id, None)
        for state, session_id in list(self._oauth_state_index.items()):
            if session_id in expired_session_ids_set:
                self._oauth_state_index.pop(state, None)

    def get_session_status(self, session_id: str) -> dict[str, Any]:
        """读取临时授权会话状态。"""
        with self._lock:
            self._cleanup_auth_sessions_locked()
            session = self._pending_sessions.get(session_id)
            if not session:
                raise LLMProviderAuthError("授权会话不存在或已过期")
            return {
                "session_id": session.session_id,
                "provider_id": session.provider_id,
                "status": session.status,
                "message": session.message,
                "user_code": session.user_code,
                "verification_url": session.verification_url,
                "authorize_url": session.authorize_url,
                "instructions": session.instructions,
                "interval_seconds": session.interval_seconds,
                "expires_at": session.expires_at,
            }

    async def _mark_session_success(self, session: PendingAuthSession, auth_data: dict[str, Any]) -> None:
        """标记授权会话为成功，并保存认证信息。"""
        auth_data["updated_at"] = int(time.time())
        await self.save_auth(session.provider_id, auth_data)
        session.status = "authorized"
        session.message = "授权成功"

    @staticmethod
    def _mark_session_error(session: PendingAuthSession, message: str) -> None:
        """标记授权会话为失败，并记录错误信息。"""
        session.status = "failed"
        session.message = message

    async def poll_auth_session(self, session_id: str) -> dict[str, Any]:
        """
        执行一次 device code 轮询，并返回最新状态。

        前端可按 interval_seconds 轮询，直到状态变为 authorized / failed。
        """
        with self._lock:
            self._cleanup_auth_sessions_locked()
            session = self._pending_sessions.get(session_id)
        if not session:
            raise LLMProviderAuthError("授权会话不存在或已过期")
        if session.status != "pending":
            return self.get_session_status(session_id)

        try:
            if session.provider_id == "chatgpt" and session.method_id == "device_code":
                await self._poll_chatgpt_device_auth(session)
            elif session.provider_id == "github-copilot" and session.method_id == "device_code":
                await self._poll_copilot_device_auth(session)
            else:
                raise LLMProviderAuthError("当前授权会话不支持轮询")
        except Exception as err:
            self._mark_session_error(session, str(err))
        return self.get_session_status(session_id)
