"""
由平台证明与当前实例策略计算有限的接入权限。
"""

from dataclasses import dataclass

from app.schemas.channel import InteractionMatch, InteractionPolicy

_OPEN_SCOPES = frozenset({"search", "subscribe", "download"})


@dataclass(frozen=True, slots=True)
class AdmissionProof:
    """
    平台适配器核验后的短时事件证明，公开消息 JSON 无法提交该字段。

    :param connection_id: 提供已认证事件的稳定连接 ID
    :param revision: 认证时的交互策略版本，用于拒绝策略变化后的旧事件
    :param sender: 平台验证的个人发言人标识
    :param conversation: 事件所属群组或会话地址，未知时为空
    :param expires_at: 证明失效的 UTC Unix 秒
    :param private: 平台是否已确认本次事件来自私聊
    """

    connection_id: str
    revision: int
    sender: str
    conversation: str | None
    expires_at: float
    private: bool = False

    def matches(self, rule: InteractionMatch) -> bool:
        """
        在已确认的发言人和会话范围内匹配一条规则。
        """
        if rule.kind == "user":
            return self.sender == rule.target
        if rule.kind == "group":
            return self.conversation == rule.target
        return self.private


def _current_proof(policy: InteractionPolicy, proof: AdmissionProof, actor: str, now: float) -> bool:
    """
    复核事件、发言人、策略版本和时效。
    """
    return (
        proof.connection_id == policy.connection_id and proof.revision == policy.revision
        and proof.sender == actor and now < proof.expires_at
    )


def allowed_context_scopes(
    policy: InteractionPolicy | None, proof: AdmissionProof | None, actor: str, now: float,
) -> frozenset[str]:
    """
    合并命中规则的业务权限，审批状态由授权服务另行检查。
    """
    if not policy or not policy.enabled or policy.admission == "manual":
        return frozenset()
    if policy.admission == "open":
        return _OPEN_SCOPES
    if proof is None or not _current_proof(policy, proof, actor, now):
        return frozenset()
    return frozenset(scope for rule in policy.admission_rules if rule.enabled and proof.matches(rule) for scope in rule.scopes)


def matches_auto_download(
    policy: InteractionPolicy | None, proof: AdmissionProof | None, actor: str, now: float,
) -> bool:
    """
    从当前连接的独立偏好决定是否自动择优，下载仍须获得业务授权。
    """
    if not policy or not policy.enabled:
        return False
    if policy.auto_download == "all":
        return True
    return (
        policy.auto_download == "specified" and proof is not None
        and _current_proof(policy, proof, actor, now)
        and any(rule.enabled and proof.matches(rule) for rule in policy.auto_download_rules)
    )
