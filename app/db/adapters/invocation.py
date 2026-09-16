"""Agent 写工具持久回执端口的 SQLAlchemy 短事务实现。"""

import re
from collections.abc import Callable
from datetime import datetime, timezone
from typing import cast
from uuid import uuid4

from sqlalchemy.orm import Session

from app.application.invocation import (
    InvocationClaim,
    InvocationConflictError,
    InvocationFinalStatus,
    InvocationIdentity,
    InvocationSnapshot,
    InvocationStatus,
)
from app.db.models.agentinvocation import AgentInvocation
from app.db.oper.agentinvocation import AgentInvocationOper
from app.db.uow import SqlAlchemyUnitOfWork

_SUMMARIES = {
    "running": "写操作已开始，等待执行结果",
    "succeeded": "写操作已确认成功",
    "failed": "写操作已确认失败",
    "pending": "写操作已确认提交，后续任务完成情况尚未观测",
    "unknown": "写操作结果未知，必须核验状态后再决定下一步",
}


def _now() -> str:
    """记录带时区时间；时间只用于审计，不用于允许重放。"""
    return datetime.now(timezone.utc).isoformat()


def _validate_identity(identity: InvocationIdentity) -> None:
    """拒绝缺失或过长身份，避免调用落入共享空 owner 命名空间。"""
    for value in (identity.principal_id, identity.session_id, identity.invocation_id):
        if not isinstance(value, str) or not value.strip() or len(value) > 255:
            raise ValueError("Agent 写调用身份必须为非空且不超过 255 字符的字符串")


def _project(record: AgentInvocation) -> InvocationSnapshot:
    """在事务内投影不可变回执，ORM 和 Session 不越过端口。"""
    return InvocationSnapshot(
        identity=InvocationIdentity(record.principal_id, record.session_id, record.invocation_id),
        tool_name=record.tool_name,
        arguments_digest=record.arguments_digest,
        claim_token=record.claim_token,
        status=cast(InvocationStatus, record.status),
        summary=record.summary,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


class TransactionalInvocationRepository:
    """每次端口调用独占一个短事务，构造本身不访问数据库。"""

    def __init__(self, session_factory: Callable[[], Session]) -> None:
        """保存会话工厂，允许测试使用独立 SQLite 文件验证竞争。"""
        self._session_factory = session_factory

    def claim(
        self,
        identity: InvocationIdentity,
        *,
        tool_name: str,
        arguments_digest: str,
    ) -> InvocationClaim:
        """提交成功后才返回执行权，工具和参数不匹配时拒绝复用调用 ID。"""
        _validate_identity(identity)
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", tool_name):
            raise ValueError("Agent 工具名称无效")
        if not re.fullmatch(r"[0-9a-f]{64}", arguments_digest):
            raise ValueError("Agent 参数指纹必须为 SHA-256 摘要")
        now = _now()
        record = AgentInvocation(
            principal_id=identity.principal_id,
            session_id=identity.session_id,
            invocation_id=identity.invocation_id,
            tool_name=tool_name,
            arguments_digest=arguments_digest,
            claim_token=uuid4().hex,
            status="running", summary=_SUMMARIES["running"],
            created_at=now, updated_at=now,
        )
        with self._session_factory() as session:
            oper = AgentInvocationOper(session)
            acquired = oper.stage_claim(record)
            stored = oper.get(identity.principal_id, identity.session_id, identity.invocation_id)
            if stored is None:
                raise RuntimeError("Agent 调用认领后回执不存在")
            if stored.tool_name != tool_name or stored.arguments_digest != arguments_digest:
                raise InvocationConflictError("已有 Agent 写调用的工具或参数不同，禁止重放")
            snapshot = _project(stored)
            SqlAlchemyUnitOfWork(session).commit()
            return InvocationClaim(record=snapshot, acquired=acquired)

    def get(self, identity: InvocationIdentity) -> InvocationSnapshot | None:
        """仅以完整身份读取回执。"""
        _validate_identity(identity)
        with self._session_factory() as session:
            record = AgentInvocationOper(session).get(
                identity.principal_id, identity.session_id, identity.invocation_id,
            )
            return _project(record) if record is not None else None

    def finish(
        self,
        identity: InvocationIdentity,
        *,
        claim_token: str,
        status: InvocationFinalStatus,
    ) -> bool:
        """按 token 收口，摘要来自宿主固定文案，凭据与任意结果文本不落盘。"""
        _validate_identity(identity)
        if status not in ("succeeded", "failed", "pending", "unknown"):
            raise ValueError("Agent 调用收口状态无效")
        with self._session_factory() as session:
            oper = AgentInvocationOper(session)
            record = oper.get(identity.principal_id, identity.session_id, identity.invocation_id)
            changed = record is not None and oper.stage_finish(
                record, claim_token=claim_token, status=status,
                summary=_SUMMARIES[status], updated_at=_now(),
            )
            SqlAlchemyUnitOfWork(session).commit()
            return changed

    def find_unresolved(
        self,
        principal_id: str,
        session_id: str,
        *,
        tool_name: str,
        arguments_digest: str,
    ) -> InvocationSnapshot | None:
        """以只读短事务查找旧未收口副作用，新请求不得盲目重复执行。"""
        with self._session_factory() as session:
            record = AgentInvocationOper(session).find_unresolved(
                principal_id, session_id,
                tool_name=tool_name, arguments_digest=arguments_digest,
            )
            return _project(record) if record is not None else None

    def recover_running(self) -> int:
        """冷启动撤销上轮执行 token，保留不可自动重放的未知回执。"""
        with self._session_factory() as session:
            changed = AgentInvocationOper(session).stage_recover(
                claim_token=uuid4().hex, summary=_SUMMARIES["unknown"], updated_at=_now(),
            )
            SqlAlchemyUnitOfWork(session).commit()
            return changed

    def delete_session(self, principal_id: str, session_id: str) -> int:
        """随会话移除已确认提交和终态历史，运行中与未知记录保留到核验。"""
        with self._session_factory() as session:
            changed = AgentInvocationOper(session).stage_delete_session(principal_id, session_id)
            SqlAlchemyUnitOfWork(session).commit()
            return changed
