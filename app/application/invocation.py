"""Agent 写工具调用的持久身份、回执与原子认领端口。"""

from dataclasses import dataclass
from typing import Literal, Protocol

InvocationStatus = Literal["running", "succeeded", "failed", "pending", "unknown"]
InvocationFinalStatus = Literal["succeeded", "failed", "pending", "unknown"]


@dataclass(frozen=True, slots=True)
class InvocationIdentity:
    """以宿主用户、会话和工具调用 ID 隔离一次写入。"""

    principal_id: str
    session_id: str
    invocation_id: str


@dataclass(frozen=True, slots=True)
class InvocationSnapshot:
    """脱离数据库会话的回执；只包含身份、指纹和宿主固定摘要。"""

    identity: InvocationIdentity
    tool_name: str
    arguments_digest: str
    claim_token: str
    status: InvocationStatus
    summary: str
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class InvocationClaim:
    """仅 acquired 为真时允许发起副作用，已有回执不能再次执行。"""

    record: InvocationSnapshot
    acquired: bool


class InvocationConflictError(ValueError):
    """同一调用 ID 携带不同工具或参数，拒绝重新解释已有写入。"""


class InvocationRepository(Protocol):
    """每次操作使用独立短事务，取消或超时不构成重放写入的授权。"""

    def claim(
        self,
        identity: InvocationIdentity,
        *,
        tool_name: str,
        arguments_digest: str,
    ) -> InvocationClaim:
        """原子首次认领；相同输入返回已有回执，不同输入抛冲突异常。"""
        ...

    def get(self, identity: InvocationIdentity) -> InvocationSnapshot | None:
        """按完整 owner 身份读取状态，不暴露原始参数或输出。"""
        ...

    def find_unresolved(
        self,
        principal_id: str,
        session_id: str,
        *,
        tool_name: str,
        arguments_digest: str,
    ) -> InvocationSnapshot | None:
        """读取同会话最近的同参数不确定写入；已确认提交的 pending 不阻止新意图。"""
        ...

    def finish(
        self,
        identity: InvocationIdentity,
        *,
        claim_token: str,
        status: InvocationFinalStatus,
    ) -> bool:
        """以当前 token 收口执行或核验结果；陈旧 owner 不得覆盖状态。"""
        ...

    def recover_running(self) -> int:
        """仅由冷启动调用：运行中记录转未知并轮换 token，禁止自动重放。"""
        ...

    def delete_session(self, principal_id: str, session_id: str) -> int:
        """在会话结束后移除已确认提交或终态回执，保留运行中和未知记录。"""
        ...
