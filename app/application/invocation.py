"""
Agent 写工具调用的持久身份、回执与原子认领端口。
"""

from dataclasses import dataclass
from typing import Literal, Protocol

InvocationStatus = Literal["running", "succeeded", "failed", "pending", "unknown"]
InvocationFinalStatus = Literal["succeeded", "failed", "pending", "unknown"]


@dataclass(frozen=True, slots=True)
class InvocationIdentity:
    """
    以宿主用户、会话和工具调用 ID 隔离一次写入。

    :param principal_id: 宿主确认的真实用户标识
    :param session_id: 发起写工具调用的服务端会话标识
    :param invocation_id: 本次工具调用的稳定标识
    """

    principal_id: str
    session_id: str
    invocation_id: str


@dataclass(frozen=True, slots=True)
class InvocationSnapshot:
    """
    脱离数据库会话的回执；只包含身份、指纹和宿主固定摘要。

    :param identity: 由用户、会话和调用标识组成的完整身份
    :param tool_name: 实际执行的写工具名称
    :param arguments_digest: 规范化工具参数摘要，不保存原始敏感参数
    :param claim_token: 当前执行拥有者的认领令牌，用于拒绝陈旧结算
    :param status: 调用状态：running 执行中、succeeded 成功、failed 失败、pending 已提交待完成、unknown 结果未知
    :param summary: 宿主按状态生成的固定摘要，不包含工具原始输出
    :param created_at: 回执创建时间字符串
    :param updated_at: 回执最近更新时间字符串
    """

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
    """
    仅 acquired 为真时允许发起副作用，已有回执不能再次执行。

    :param record: 本次新建或读取到的调用回执快照
    :param acquired: 是否首次取得执行权；只有为 True 才可发起副作用
    """

    record: InvocationSnapshot
    acquired: bool


class InvocationConflictError(ValueError):
    """
    同一调用 ID 携带不同工具或参数，拒绝重新解释已有写入。
    """


class InvocationRepository(Protocol):
    """
    每次操作使用独立短事务，取消或超时不构成重放写入的授权。
    """

    def claim(
        self,
        identity: InvocationIdentity,
        *,
        tool_name: str,
        arguments_digest: str,
    ) -> InvocationClaim:
        """
        原子首次认领；相同输入返回已有回执，不同输入抛冲突异常。

        :param identity: 包含真实用户、服务端会话和工具调用标识的完整身份
        :param tool_name: 待执行的写工具名称
        :param arguments_digest: 规范化工具参数的稳定摘要，用于检测同标识输入冲突
        :return: 新建或已有回执及是否取得执行权；未取得执行权时不得执行副作用
        :raises InvocationConflictError: 同一调用身份已绑定不同工具或参数摘要
        """
        ...

    def get(self, identity: InvocationIdentity) -> InvocationSnapshot | None:
        """
        按完整 owner 身份读取状态，不暴露原始参数或输出。

        :param identity: 包含真实用户、服务端会话和工具调用标识的完整身份
        :return: 匹配的调用回执快照，不存在时为 None
        """
        ...

    def find_unresolved(
        self,
        principal_id: str,
        session_id: str,
        *,
        tool_name: str,
        arguments_digest: str,
    ) -> InvocationSnapshot | None:
        """
        读取同会话最近的同参数不确定写入；已确认提交的 pending 不阻止新意图。

        :param principal_id: 宿主确认的真实用户标识
        :param session_id: 查询范围内的服务端会话标识
        :param tool_name: 需要避免重复执行的写工具名称
        :param arguments_digest: 待比对的规范化参数摘要
        :return: 最近的同工具同参数未决回执，不存在时为 None；已确认提交的 pending 不算未决
        """
        ...

    def finish(
        self,
        identity: InvocationIdentity,
        *,
        claim_token: str,
        status: InvocationFinalStatus,
    ) -> bool:
        """
        以当前 token 收口执行或核验结果；陈旧 owner 不得覆盖状态。

        :param identity: 包含真实用户、服务端会话和工具调用标识的完整身份
        :param claim_token: 当前认领者持有的令牌，陈旧令牌不能覆盖状态
        :param status: 待记录的终态：succeeded、failed、pending 或 unknown
        :return: 匹配当前令牌并完成状态更新时为 True，令牌失效或状态不允许更新时为 False
        """
        ...

    def recover_running(self) -> int:
        """
        仅由冷启动调用：运行中记录转未知并轮换 token，禁止自动重放。

        :return: 转为未知状态并轮换认领令牌的遗留运行记录数
        """
        ...

    def delete_session(self, principal_id: str, session_id: str) -> int:
        """
        在会话结束后移除已确认提交或终态回执，保留运行中和未知记录。

        :param principal_id: 会话所属的真实用户标识
        :param session_id: 需要回收已确认回执的服务端会话标识
        :return: 已删除的终态或已确认提交回执数量，不包含运行中及未知记录
        """
        ...
