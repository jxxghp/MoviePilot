"""调用方事务内的 Agent 写工具回执数据操作。"""

from typing import cast

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session
from sqlalchemy.sql.dml import Delete

from app.db.base import DbOper, execute_dml
from app.db.models.agentinvocation import AgentInvocation


def terminal_session_delete(principal_id: str, session_id: str) -> Delete:
    """只构造已确认提交和终态历史删除语句，不创建会话或访问数据库。"""
    return delete(AgentInvocation).where(
        AgentInvocation.principal_id == principal_id,
        AgentInvocation.session_id == session_id,
        AgentInvocation.status.in_(("succeeded", "failed", "pending")),
    )


class AgentInvocationOper(DbOper):
    """必须显式传入 Session；唯一键认领和 token 条件更新均由数据库保证。"""

    def __init__(self, db: Session) -> None:
        """保存调用方事务的独占 Session，不自行提交。"""
        super().__init__(db)
        self._session = db

    def get(
        self, principal_id: str, session_id: str, invocation_id: str,
    ) -> AgentInvocation | None:
        """按完整身份读取单条回执，避免不同用户和会话交叉复用。"""
        return cast(AgentInvocation | None, self._session.execute(
            select(AgentInvocation).where(
                AgentInvocation.principal_id == principal_id,
                AgentInvocation.session_id == session_id,
                AgentInvocation.invocation_id == invocation_id,
            )
        ).scalar_one_or_none())

    def find_unresolved(
        self,
        principal_id: str,
        session_id: str,
        *,
        tool_name: str,
        arguments_digest: str,
    ) -> AgentInvocation | None:
        """查询最近的相同参数恢复状态，不跨用户、会话或工具复用。"""
        return cast(AgentInvocation | None, self._session.execute(
            select(AgentInvocation).where(
                AgentInvocation.principal_id == principal_id,
                AgentInvocation.session_id == session_id,
                AgentInvocation.tool_name == tool_name,
                AgentInvocation.arguments_digest == arguments_digest,
                AgentInvocation.status.in_(("running", "unknown")),
            ).order_by(AgentInvocation.id.desc()).limit(1)
        ).scalar_one_or_none())

    def stage_claim(self, record: AgentInvocation) -> bool:
        """唯一身份首次插入才获得执行权，并发竞争不会先查后写。"""
        dialect = self._session.get_bind().dialect.name
        if dialect == "postgresql":
            statement = postgres_insert(AgentInvocation)
        elif dialect == "sqlite":
            statement = sqlite_insert(AgentInvocation)
        else:
            raise RuntimeError(f"不支持的 Agent 调用数据库：{dialect}")
        values = {
            column.name: getattr(record, column.name)
            for column in AgentInvocation.__table__.columns
            if column.name != "id"
        }
        inserted = self._session.execute(
            statement.values(**values).on_conflict_do_nothing(
                index_elements=["principal_id", "session_id", "invocation_id"],
            ).returning(AgentInvocation.id)
        ).scalar_one_or_none()
        return inserted is not None

    def stage_finish(
        self,
        record: AgentInvocation,
        *,
        claim_token: str,
        status: str,
        summary: str,
        updated_at: str,
    ) -> bool:
        """只有当前未收口 owner 可以记录执行或核验结果。"""
        return bool(execute_dml(
            self._session,
            update(AgentInvocation).where(
                AgentInvocation.id == record.id,
                AgentInvocation.claim_token == claim_token,
                AgentInvocation.status.in_(("running", "unknown")),
            ).values(status=status, summary=summary, updated_at=updated_at),
            execution_options={"synchronize_session": False},
        ))

    def stage_recover(self, *, claim_token: str, summary: str, updated_at: str) -> int:
        """冷启动撤销旧执行权，未知状态不会依据时间再次进入运行态。"""
        return execute_dml(
            self._session,
            update(AgentInvocation).where(AgentInvocation.status == "running").values(
                status="unknown", claim_token=claim_token,
                summary=summary, updated_at=updated_at,
            ),
            execution_options={"synchronize_session": False},
        )

    def stage_delete_session(self, principal_id: str, session_id: str) -> int:
        """仅暂存指定会话的已确认提交和终态回执删除。"""
        return execute_dml(
            self._session,
            terminal_session_delete(principal_id, session_id),
            execution_options={"synchronize_session": False},
        )
