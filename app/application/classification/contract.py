"""媒体分类策略配置、外部引用校验和稳定错误合同。"""

from collections.abc import Callable
from typing import Any, Protocol

from app.schemas.category import (
    ClassificationPolicy,
    ClassificationPolicyState,
    ClassificationValidationResult,
)

ClassificationReferenceSnapshotValidator = Callable[
    [ClassificationPolicy, Any],
    ClassificationValidationResult,
]
"""在持久化事务内校验候选策略与外部引用快照的纯函数。"""


class ClassificationPolicyConflictError(RuntimeError):
    """表示策略发布使用了已经过期的 revision。"""

    def __init__(self, *, expected_revision: int, current_revision: int) -> None:
        """保存客户端期望和持久化事实源中的当前 revision。"""
        self.expected_revision = expected_revision
        self.current_revision = current_revision
        super().__init__(
            "分类策略已被其他请求修改："
            f"期望 revision {expected_revision}，当前 revision {current_revision}"
        )


class ClassificationPolicyStateCorruptError(RuntimeError):
    """表示持久化分类策略状态包无法通过强类型结构校验。"""


class ClassificationPolicyReferenceViolationError(RuntimeError):
    """表示事务内外部稳定引用会被候选策略破坏。"""

    def __init__(self, result: ClassificationValidationResult) -> None:
        """保存事务内复验产生的完整结构化问题。"""
        self.result = result.model_copy(deep=True)
        super().__init__("分类策略会破坏现有稳定引用")


class ClassificationPolicyReferenceValidator(Protocol):
    """从当前运行配置读取外部稳定引用并校验候选策略。"""

    def validate(
        self,
        policy: ClassificationPolicy,
    ) -> ClassificationValidationResult:
        """返回候选策略对当前外部引用的结构化校验结果。"""
        ...


class ClassificationPolicyStore(Protocol):
    """Application 使用的分类策略状态原子持久化端口。"""

    def load(self) -> ClassificationPolicyState | None:
        """读取当前完整状态包；配置不存在时返回空值。"""
        ...

    def compare_and_set(
        self,
        *,
        expected_revision: int,
        state: ClassificationPolicyState,
    ) -> None:
        """仅在当前 revision 等于期望值时原子替换完整状态包。"""
        ...
