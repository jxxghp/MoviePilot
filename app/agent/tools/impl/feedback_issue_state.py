"""反馈 Issue 流程的短期服务端状态。

这里保存两类只应由工具写入的状态：
- 诊断日志收集结果：证明 Agent 在提交前尝试读取过本地日志。
- 用户确认结果：证明用户通过按钮确认过某份预览草稿。

状态只保存在当前进程内，重启后失效；这符合反馈提交这种交互式流程的预期，
也避免把一次性确认 token 持久化到数据库。
"""

from __future__ import annotations

import hashlib
import time
import uuid
from dataclasses import dataclass
from threading import Lock
from typing import Optional


FEEDBACK_CONFIRM_VALUE_PREFIX = "__feedback_issue_confirm__:"
_STATE_TTL_SECONDS = 60 * 60


@dataclass
class FeedbackDiagnosticsRecord:
    """一次反馈诊断日志收集结果。"""

    diagnostics_id: str
    session_id: str
    user_id: str
    username: Optional[str]
    logs: str
    source_files: list[str]
    found: bool
    created_at: float


@dataclass
class FeedbackConfirmationRecord:
    """一次反馈 Issue 预览确认状态。"""

    confirmation_token: str
    session_id: str
    user_id: str
    username: Optional[str]
    draft_hash: str
    diagnostics_id: str
    created_at: float
    confirmed_at: Optional[float] = None


def build_feedback_draft_hash(
    *,
    title: str,
    version: str,
    environment: str,
    issue_type: str,
    description: str,
    original_user_request: str,
    logs: Optional[str],
    diagnostics_id: str,
) -> str:
    """为用户确认的 Issue 草稿生成稳定摘要。"""
    parts = (
        title.strip(),
        version.strip(),
        environment.strip(),
        issue_type.strip(),
        description.strip(),
        original_user_request.strip(),
        (logs or "").strip(),
        diagnostics_id.strip(),
    )
    return hashlib.sha256("\x00".join(parts).encode("utf-8", errors="replace")).hexdigest()


class FeedbackIssueStateStore:
    """管理反馈 Issue 流程的进程内短期状态。"""

    def __init__(self) -> None:
        self._diagnostics: dict[str, FeedbackDiagnosticsRecord] = {}
        self._confirmations: dict[str, FeedbackConfirmationRecord] = {}
        self._lock = Lock()

    def _cleanup_locked(self) -> None:
        expire_before = time.time() - _STATE_TTL_SECONDS
        for diagnostics_id, record in list(self._diagnostics.items()):
            if record.created_at < expire_before:
                self._diagnostics.pop(diagnostics_id, None)
        for token, record in list(self._confirmations.items()):
            if record.created_at < expire_before:
                self._confirmations.pop(token, None)

    def create_diagnostics(
        self,
        *,
        session_id: str,
        user_id: str,
        username: Optional[str],
        logs: str,
        source_files: list[str],
        found: bool,
    ) -> FeedbackDiagnosticsRecord:
        """登记一次日志收集结果。"""
        with self._lock:
            self._cleanup_locked()
            diagnostics_id = uuid.uuid4().hex[:12]
            while diagnostics_id in self._diagnostics:
                diagnostics_id = uuid.uuid4().hex[:12]
            record = FeedbackDiagnosticsRecord(
                diagnostics_id=diagnostics_id,
                session_id=session_id,
                user_id=str(user_id),
                username=username,
                logs=logs,
                source_files=source_files,
                found=found,
                created_at=time.time(),
            )
            self._diagnostics[diagnostics_id] = record
            return record

    def get_diagnostics(
        self,
        diagnostics_id: str,
        *,
        session_id: str,
        user_id: str,
    ) -> Optional[FeedbackDiagnosticsRecord]:
        """按会话和用户读取诊断结果，防止跨用户复用。"""
        with self._lock:
            self._cleanup_locked()
            record = self._diagnostics.get(diagnostics_id)
            if not record:
                return None
            if record.session_id != session_id or record.user_id != str(user_id):
                return None
            return record

    def find_active_confirmation(
        self,
        *,
        session_id: str,
        user_id: str,
    ) -> Optional[FeedbackConfirmationRecord]:
        """查找当前会话/用户尚未消费、且未点击确认的预览 token。

        prepare_feedback_issue 会用它判断「上一份预览还挂着，不该再发一份」，
        避免 #5806 实测里发了两次同样的确认按钮、用户点了两次的情况。"""
        with self._lock:
            self._cleanup_locked()
            for record in self._confirmations.values():
                if (
                    record.session_id == session_id
                    and record.user_id == str(user_id)
                    and record.confirmed_at is None
                ):
                    return record
            return None

    def invalidate_active_confirmations(
        self,
        *,
        session_id: str,
        user_id: str,
    ) -> int:
        """作废当前会话所有未确认的预览 token，返回作废数量。

        用户在 prepare 之后修改草稿、重新调 prepare 时调用；旧 token 失效
        后即便残留消息里的按钮被点击，``mark_confirmed`` 也会因找不到记录
        而返回 False，避免脏数据驱动提交。"""
        with self._lock:
            self._cleanup_locked()
            to_drop = [
                token
                for token, record in self._confirmations.items()
                if record.session_id == session_id
                and record.user_id == str(user_id)
                and record.confirmed_at is None
            ]
            for token in to_drop:
                self._confirmations.pop(token, None)
            return len(to_drop)

    def create_confirmation(
        self,
        *,
        session_id: str,
        user_id: str,
        username: Optional[str],
        draft_hash: str,
        diagnostics_id: str,
    ) -> FeedbackConfirmationRecord:
        """创建待用户点击确认的草稿 token。"""
        with self._lock:
            self._cleanup_locked()
            token = uuid.uuid4().hex
            while token in self._confirmations:
                token = uuid.uuid4().hex
            record = FeedbackConfirmationRecord(
                confirmation_token=token,
                session_id=session_id,
                user_id=str(user_id),
                username=username,
                draft_hash=draft_hash,
                diagnostics_id=diagnostics_id,
                created_at=time.time(),
            )
            self._confirmations[token] = record
            return record

    def mark_confirmed(
        self,
        token: str,
        *,
        session_id: str,
        user_id: str,
    ) -> bool:
        """按钮回调命中时，把 token 标记为已由用户确认。"""
        with self._lock:
            self._cleanup_locked()
            record = self._confirmations.get(token)
            if not record:
                return False
            if record.session_id != session_id or record.user_id != str(user_id):
                return False
            record.confirmed_at = time.time()
            return True

    def consume_confirmed(
        self,
        token: str,
        *,
        session_id: str,
        user_id: str,
        draft_hash: str,
    ) -> Optional[FeedbackConfirmationRecord]:
        """消费一次已确认 token；内容摘要不一致时拒绝。"""
        with self._lock:
            self._cleanup_locked()
            record = self._confirmations.get(token)
            if not record:
                return None
            if (
                record.session_id != session_id
                or record.user_id != str(user_id)
                or record.draft_hash != draft_hash
                or record.confirmed_at is None
            ):
                return None
            return self._confirmations.pop(token, None)

    def clear(self) -> None:
        """测试和重置场景使用：清空所有短期状态。"""
        with self._lock:
            self._diagnostics.clear()
            self._confirmations.clear()


feedback_issue_state_store = FeedbackIssueStateStore()
