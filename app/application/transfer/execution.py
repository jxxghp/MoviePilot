"""整理步骤执行检查点、人工判定与终态结算的应用契约。"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Callable, Mapping, Optional, Protocol
from uuid import uuid4

TRANSFER_EXECUTION_VERSION = 1
TRANSFER_STEP_INTENT_VERSION = 1
TRANSFER_STEP_RESULT_VERSION = 1


class TransferExecutionState(StrEnum):
    """描述 planning phase 正交的持久执行状态。"""

    NOT_STARTED = "not_started"
    RUNNING = "running"
    RETRY_WAIT = "retry_wait"
    SETTLING = "settling"
    FAILED = "failed"
    MANUAL_REVIEW = "manual_review"


class TransferStepState(StrEnum):
    """描述单个稳定外部操作的持久执行状态。"""

    PREPARED = "prepared"
    STARTED = "started"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    MANUAL_REVIEW = "manual_review"


class TransferTerminalState(StrEnum):
    """描述允许写入整理历史的确定终态。"""

    SUCCEEDED = "succeeded"
    FAILED = "failed"


class TransferExecutionOutcome(StrEnum):
    """描述执行检查点可裁决的业务结果。"""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    OVERWRITE_SKIPPED = "overwrite_skipped"


class TransferOperationObservationState(StrEnum):
    """描述重启后对遗留 STARTED 外部操作的严格探测结论。"""

    APPLIED = "applied"
    NOT_APPLIED = "not_applied"
    UNKNOWN = "unknown"
    CONFLICT = "conflict"


class TransferManualReviewDecision(StrEnum):
    """描述人工对外部结果不确定步骤作出的显式判定。"""

    NOT_APPLIED = "not_applied"
    APPLIED = "applied"
    FAILED = "failed"


class TransferExecutionError(RuntimeError):
    """表示整理执行持久化状态无法按请求推进。"""


class TransferExecutionConflictError(TransferExecutionError):
    """表示稳定操作身份、尝试身份或检查点证据发生冲突。"""


class TransferExecutionLeaseLostError(TransferExecutionError):
    """表示持久化写入时任务租约已失效或已被其他 worker 接管。"""


def _canonical_json(payload: Mapping[str, Any]) -> str:
    """生成稳定操作身份使用的规范 JSON。"""
    return json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def build_transfer_operation_id(
        *,
        task_id: str,
        checkpoint_fingerprint: str,
        ordinal: int,
        phase: str,
        kind: str,
        intent_payload: Mapping[str, Any],
) -> str:
    """
    由冻结计划和操作语义生成跨 lease、attempt 与重启稳定的身份。

    :param task_id: 稳定任务标识
    :param checkpoint_fingerprint: 冻结计划指纹
    :param ordinal: 全局执行序号
    :param phase: 执行阶段
    :param kind: 操作类型
    :param intent_payload: 冻结操作参数
    :return: SHA-256 操作标识
    """
    if not task_id or not checkpoint_fingerprint or ordinal < 0 or not phase or not kind:
        raise ValueError("整理操作身份缺少稳定任务、计划或步骤信息")
    canonical = _canonical_json({
        "schema_version": TRANSFER_STEP_INTENT_VERSION,
        "task_id": task_id,
        "checkpoint_fingerprint": checkpoint_fingerprint,
        "ordinal": ordinal,
        "phase": phase,
        "kind": kind,
        "intent_payload": dict(intent_payload),
    })
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_transfer_checkpoint_fingerprint(
        checkpoint_payload: Mapping[str, Any],
) -> str:
    """由版本化执行检查点 payload 生成稳定 SHA-256 指纹。"""
    return hashlib.sha256(
        _canonical_json(checkpoint_payload).encode("utf-8")
    ).hexdigest()


@dataclass(frozen=True, slots=True)
class TransferStepIntent:
    """保存一次可持久化外部操作的稳定意图。"""

    operation_id: str
    checkpoint_fingerprint: str
    ordinal: int
    phase: str
    kind: str
    payload: dict[str, Any]
    version: int = TRANSFER_STEP_INTENT_VERSION

    def __post_init__(self) -> None:
        """拒绝版本未知、身份不完整或不可 JSON 序列化的操作意图。"""
        object.__setattr__(self, "payload", deepcopy(self.payload))
        if self.version != TRANSFER_STEP_INTENT_VERSION:
            raise ValueError(f"不支持的整理步骤意图版本：{self.version}")
        if (
                not self.operation_id
                or not self.checkpoint_fingerprint
                or self.ordinal < 0
                or not self.phase
                or not self.kind
        ):
            raise ValueError("整理步骤意图缺少稳定身份或顺序")
        _canonical_json(self.payload)

    @classmethod
    def create(
            cls,
            *,
            task_id: str,
            checkpoint_fingerprint: str,
            ordinal: int,
            phase: str,
            kind: str,
            payload: Mapping[str, Any],
    ) -> "TransferStepIntent":
        """构造并绑定稳定 operation ID 的步骤意图。"""
        frozen_payload = dict(payload)
        return cls(
            operation_id=build_transfer_operation_id(
                task_id=task_id,
                checkpoint_fingerprint=checkpoint_fingerprint,
                ordinal=ordinal,
                phase=phase,
                kind=kind,
                intent_payload=frozen_payload,
            ),
            checkpoint_fingerprint=checkpoint_fingerprint,
            ordinal=ordinal,
            phase=phase,
            kind=kind,
            payload=frozen_payload,
        )


@dataclass(frozen=True, slots=True)
class TransferStepResult:
    """保存严格执行或探测后得到的版本化步骤证据。"""

    payload: dict[str, Any]
    version: int = TRANSFER_STEP_RESULT_VERSION

    def __post_init__(self) -> None:
        """拒绝未知版本或不可 JSON 序列化的步骤结果。"""
        object.__setattr__(self, "payload", deepcopy(self.payload))
        if self.version != TRANSFER_STEP_RESULT_VERSION:
            raise ValueError(f"不支持的整理步骤结果版本：{self.version}")
        _canonical_json(self.payload)


@dataclass(frozen=True, slots=True)
class TransferOperationObservation:
    """保存外部操作探测结论及其版本化事实证据。"""

    state: TransferOperationObservationState
    evidence: TransferStepResult


@dataclass(frozen=True, slots=True)
class TransferExecutionStep:
    """提供脱离 ORM Session 的单步骤持久状态投影。"""

    task_id: str
    operation_id: str
    checkpoint_fingerprint: str
    ordinal: int
    phase: str
    kind: str
    state: TransferStepState
    attempt_token: Optional[str]
    attempt_count: int
    intent: TransferStepIntent
    result: Optional[TransferStepResult]
    last_error: Optional[str]
    prepared_at: str
    started_at: Optional[str]
    completed_at: Optional[str]
    updated_at: str


@dataclass(frozen=True, slots=True)
class TransferExecutionCheckpoint:
    """保存所有必要步骤完成后供终态结算重放的聚合结果。"""

    fingerprint: str
    payload: dict[str, Any]
    operation_ids: tuple[str, ...]
    skip_reason: Optional[str] = None
    version: int = TRANSFER_EXECUTION_VERSION

    def __post_init__(self) -> None:
        """拒绝重复步骤、空指纹和不可序列化的执行检查点。"""
        object.__setattr__(self, "payload", deepcopy(self.payload))
        if self.version != TRANSFER_EXECUTION_VERSION or not self.fingerprint:
            raise ValueError("整理执行检查点版本或指纹无效")
        if len(set(self.operation_ids)) != len(self.operation_ids):
            raise ValueError("整理执行检查点不能引用重复操作")
        if not all(isinstance(item, str) and item for item in self.operation_ids):
            raise ValueError("整理执行检查点包含无效操作标识")
        if not self.operation_ids and not self.skip_reason:
            raise ValueError("零副作用整理执行检查点必须记录 skip_reason")
        raw_outcome = self.payload.get("outcome")
        try:
            outcome = TransferExecutionOutcome(
                raw_outcome if isinstance(raw_outcome, str) else ""
            )
        except (TypeError, ValueError) as error:
            raise ValueError("整理执行检查点 outcome 无效") from error
        transferinfo = self.payload.get("transferinfo")
        if transferinfo is not None:
            if not isinstance(transferinfo, dict):
                raise ValueError("整理执行检查点 TransferInfo 必须是 JSON 对象")
            expected_success = outcome is TransferExecutionOutcome.SUCCEEDED
            expected_overwrite_skip = (
                outcome is TransferExecutionOutcome.OVERWRITE_SKIPPED
            )
            if (
                    bool(transferinfo.get("success")) != expected_success
                    or bool(transferinfo.get("overwrite_skipped"))
                    != expected_overwrite_skip
            ):
                raise ValueError("整理执行 checkpoint outcome 与 TransferInfo 不一致")
        elif outcome is TransferExecutionOutcome.OVERWRITE_SKIPPED:
            raise ValueError("覆盖跳过执行检查点缺少冻结 TransferInfo")
        _canonical_json(self.payload)
        if build_transfer_checkpoint_fingerprint(self.to_payload()) != self.fingerprint:
            raise ValueError("整理执行检查点内容与指纹不一致")

    @classmethod
    def create(
            cls,
            *,
            payload: Mapping[str, Any],
            operation_ids: tuple[str, ...],
            skip_reason: Optional[str] = None,
    ) -> "TransferExecutionCheckpoint":
        """构造并绑定完整序列化内容指纹的执行检查点。"""
        frozen_payload = dict(payload)
        serialized = {
            "schema_version": TRANSFER_EXECUTION_VERSION,
            "payload": frozen_payload,
            "operation_ids": list(operation_ids),
            "skip_reason": skip_reason,
        }
        return cls(
            fingerprint=build_transfer_checkpoint_fingerprint(serialized),
            payload=frozen_payload,
            operation_ids=operation_ids,
            skip_reason=skip_reason,
        )

    def to_payload(self) -> dict[str, Any]:
        """编码可跨重启恢复的完整版本化执行检查点。"""
        return {
            "schema_version": self.version,
            "payload": dict(self.payload),
            "operation_ids": list(self.operation_ids),
            "skip_reason": self.skip_reason,
        }

    def validate_settlement_outcome(
            self,
            settlement_outcome: str,
    ) -> TransferTerminalState:
        """解析执行事实，并校验同事务历史裁决给出的结算方向。"""
        raw_outcome = self.payload.get("outcome")
        try:
            outcome = TransferExecutionOutcome(
                raw_outcome if isinstance(raw_outcome, str) else ""
            )
        except (TypeError, ValueError) as error:
            raise TransferExecutionConflictError(
                "整理执行检查点 outcome 无效"
            ) from error
        try:
            terminal_state = TransferTerminalState(settlement_outcome)
        except (TypeError, ValueError) as error:
            raise TransferExecutionConflictError(
                "整理结算 outcome 无效"
            ) from error
        if (
                outcome != TransferExecutionOutcome.OVERWRITE_SKIPPED
                and outcome.value != terminal_state.value
        ):
            raise TransferExecutionConflictError(
                "整理结算 outcome 与执行检查点不一致"
            )
        return terminal_state

    @classmethod
    def from_payload(
            cls,
            payload: Mapping[str, Any],
            *,
            fingerprint: str,
    ) -> "TransferExecutionCheckpoint":
        """解析并校验数据库中的执行检查点版本与稳定指纹。"""
        if not isinstance(payload, Mapping):
            raise TransferExecutionConflictError("整理执行检查点 JSON 结构无效")
        serialized = dict(payload)
        if build_transfer_checkpoint_fingerprint(serialized) != fingerprint:
            raise TransferExecutionConflictError("整理执行检查点 JSON 与指纹不一致")
        version = serialized.get("schema_version")
        if version != TRANSFER_EXECUTION_VERSION:
            raise TransferExecutionConflictError("整理执行检查点版本不受支持")
        result_payload = serialized.get("payload")
        operation_ids = serialized.get("operation_ids")
        if not isinstance(result_payload, dict) or not isinstance(operation_ids, list):
            raise TransferExecutionConflictError("整理执行检查点结构无效")
        if not all(isinstance(item, str) and item for item in operation_ids):
            raise TransferExecutionConflictError("整理执行检查点包含无效操作标识")
        skip_reason = serialized.get("skip_reason")
        if skip_reason is not None and not isinstance(skip_reason, str):
            raise TransferExecutionConflictError("整理执行跳过原因类型无效")
        try:
            return cls(
                fingerprint=fingerprint,
                payload=result_payload,
                operation_ids=tuple(operation_ids),
                skip_reason=skip_reason,
                version=version,
            )
        except ValueError as error:
            raise TransferExecutionConflictError(
                "整理执行检查点内容无效"
            ) from error


@dataclass(frozen=True, slots=True)
class TransferExecutionSnapshot:
    """提供任务执行 checkpoint、重试和人工状态的稳定投影。"""

    task_id: str
    state: TransferExecutionState
    checkpoint: Optional[TransferExecutionCheckpoint]
    retry_generation: int
    retry_count: int
    retry_due_at: Optional[str]
    settlement_revision: int
    terminal_history_id: Optional[int]
    last_error: Optional[str]
    steps: tuple[TransferExecutionStep, ...]


@dataclass(frozen=True, slots=True)
class TransferSettlementIntent:
    """描述已确定结果对应的历史写入与 pending 终态。"""

    terminal_state: TransferTerminalState
    history_payload: dict[str, Any]

    def __post_init__(self) -> None:
        """确保历史 payload 可持久化且包含稳定源身份。"""
        object.__setattr__(self, "history_payload", deepcopy(self.history_payload))
        if not self.history_payload.get("src"):
            raise ValueError("整理终态历史缺少源路径")
        _canonical_json(self.history_payload)


@dataclass(frozen=True, slots=True)
class TransferSettlementResult:
    """描述终态历史是否新提交以及 pending 是否已删除。"""

    history_id: int
    settlement_revision: int
    pending_deleted: bool
    already_settled: bool = False


@dataclass(frozen=True, slots=True)
class TransferRetryRequestResult:
    """描述用户重试请求是否被持久调度器接受。"""

    accepted: bool
    state: TransferExecutionState
    retry_generation: int
    message: str


@dataclass(frozen=True, slots=True)
class TransferManualReviewResult:
    """描述一次已持久审计的人工判定及后续调度状态。"""

    task_id: str
    operation_id: str
    decision: TransferManualReviewDecision
    state: TransferExecutionState
    review_revision: int
    step: TransferExecutionStep


@dataclass(frozen=True, slots=True)
class TransferManualReviewSource:
    """提供人工复核任务的最小源文件身份。"""

    storage: str
    path: str


@dataclass(frozen=True, slots=True)
class TransferManualReviewStepView:
    """提供人工复核所需且不含租约或尝试令牌的步骤证据。"""

    operation_id: str
    kind: str
    intent: dict[str, Any]
    evidence: Optional[dict[str, Any]]
    error: Optional[str]


@dataclass(frozen=True, slots=True)
class TransferManualReviewTaskView:
    """提供管理员发现与复核 durable 整理任务的公开投影。"""

    task_id: str
    source: TransferManualReviewSource
    state: TransferExecutionState
    step: TransferManualReviewStepView
    review_revision: int


@dataclass(frozen=True, slots=True)
class TransferManualReviewPage:
    """提供稳定分页的人工复核任务结果。"""

    items: tuple[TransferManualReviewTaskView, ...]
    total: int
    page: int
    page_size: int


class TransferExecutionRepository(Protocol):
    """定义步骤状态、执行 checkpoint 与终态历史的持久化端口。"""

    def get_snapshot(self, *, task_id: str) -> Optional[TransferExecutionSnapshot]:
        """读取任务执行快照。"""

    def list_manual_reviews(
            self,
            *,
            state: TransferExecutionState,
            page: int,
            page_size: int,
    ) -> TransferManualReviewPage:
        """按严格公开状态分页读取人工复核任务。"""

    def get_manual_review(
            self,
            *,
            task_id: str,
    ) -> Optional[TransferManualReviewTaskView]:
        """按任务标识读取人工复核公开详情。"""

    def prepare_step(
            self,
            *,
            task_id: str,
            lease_token: str,
            intent: TransferStepIntent,
    ) -> TransferExecutionStep:
        """在外部副作用前持久化稳定步骤意图。"""

    def start_step(
            self,
            *,
            task_id: str,
            lease_token: str,
            operation_id: str,
            attempt_token: str,
    ) -> TransferExecutionStep:
        """以当前 lease 和新 attempt token 标记步骤开始。"""

    def restart_after_not_applied(
            self,
            *,
            task_id: str,
            lease_token: str,
            operation_id: str,
            previous_attempt_token: str,
            attempt_token: str,
            evidence: TransferStepResult,
    ) -> TransferExecutionStep:
        """严格探测为未发生后，以新 attempt token 安全重启遗留步骤。"""

    def resume_failed_step(
            self,
            *,
            task_id: str,
            lease_token: str,
            operation_id: str,
            attempt_token: str,
    ) -> TransferExecutionStep:
        """任务到期并重新 claim 后，以新 attempt token 重试 FAILED 步骤。"""

    def complete_step(
            self,
            *,
            task_id: str,
            lease_token: str,
            operation_id: str,
            attempt_token: str,
            result: TransferStepResult,
    ) -> TransferExecutionStep:
        """以 lease 和 attempt 双 CAS 提交成功证据。"""

    def defer_step(
            self,
            *,
            task_id: str,
            lease_token: str,
            operation_id: str,
            attempt_token: str,
            error: str,
            retry_due_at: str,
            evidence: Optional[TransferStepResult] = None,
    ) -> TransferExecutionSnapshot:
        """持久化已知失败、到期时间并原子释放当前 lease。"""

    def exhaust_step(
            self,
            *,
            task_id: str,
            lease_token: str,
            operation_id: str,
            attempt_token: str,
            error: str,
            evidence: Optional[TransferStepResult] = None,
    ) -> TransferExecutionSnapshot:
        """重试预算耗尽时保留 lease，并建立可 durable 失败结算的检查点。"""

    def request_retry(
            self,
            *,
            task_id: str,
            reason: str,
            requested_by: str,
    ) -> TransferRetryRequestResult:
        """仅把 FAILED 终态转入到期可 claim 的 retry_wait。"""

    def resolve_manual_review(
            self,
            *,
            task_id: str,
            operation_id: str,
            decision: TransferManualReviewDecision,
            actor: str,
            reason: str,
            result: Optional[TransferStepResult] = None,
    ) -> TransferManualReviewResult:
        """无 lease 地原子提交人工判定审计并交回唯一调度器。"""

    def mark_manual_review(
            self,
            *,
            task_id: str,
            lease_token: str,
            operation_id: str,
            error: str,
            evidence: Optional[TransferStepResult] = None,
            attempt_token: Optional[str] = None,
    ) -> TransferExecutionSnapshot:
        """持久化不可判定结果并从自动调度中隔离任务。"""

    def checkpoint_execution(
            self,
            *,
            task_id: str,
            lease_token: str,
            checkpoint: TransferExecutionCheckpoint,
    ) -> TransferExecutionSnapshot:
        """确认所有引用步骤成功后提交聚合执行检查点。"""

class TransferStepRunner(Protocol):
    """定义文件执行方可注入的单步骤持久执行边界。"""

    def run(
            self,
            *,
            phase: str,
            kind: str,
            payload: Mapping[str, Any],
            execute: Callable[[], TransferStepResult],
            observe: Callable[[], TransferOperationObservation],
    ) -> TransferStepResult:
        """持久编排一次外部副作用，并在遗留尝试时先严格探测。"""


class TransferExecutionCommand:
    """以类型化命令收口步骤状态机，外部 I/O 由调用方在事务外执行。"""

    def __init__(
            self,
            repository: TransferExecutionRepository,
            *,
            attempt_token_factory: Callable[[], str] = lambda: uuid4().hex,
    ) -> None:
        """保存持久化端口和可替换的 attempt token 工厂。"""
        self._repository = repository
        self._attempt_token_factory = attempt_token_factory

    def prepare(
            self,
            *,
            task_id: str,
            lease_token: str,
            intent: TransferStepIntent,
    ) -> TransferExecutionStep:
        """在外部调用前提交步骤意图。"""
        return self._repository.prepare_step(
            task_id=task_id,
            lease_token=lease_token,
            intent=intent,
        )

    def begin(
            self,
            *,
            task_id: str,
            lease_token: str,
            operation_id: str,
    ) -> TransferExecutionStep:
        """生成单次 attempt token 并持久化开始状态。"""
        attempt_token = self._attempt_token_factory()
        if not attempt_token:
            raise ValueError("整理步骤 attempt token 不能为空")
        return self._repository.start_step(
            task_id=task_id,
            lease_token=lease_token,
            operation_id=operation_id,
            attempt_token=attempt_token,
        )

    def restart_after_not_applied(
            self,
            *,
            task_id: str,
            lease_token: str,
            step: TransferExecutionStep,
            evidence: TransferStepResult,
    ) -> TransferExecutionStep:
        """在严格 NOT_APPLIED 证据成立时轮换 attempt token 并继续执行。"""
        if step.state is not TransferStepState.STARTED or not step.attempt_token:
            raise ValueError("只有遗留 STARTED 步骤可按未发生证据安全重启")
        attempt_token = self._attempt_token_factory()
        if not attempt_token or attempt_token == step.attempt_token:
            raise ValueError("安全重启必须生成不同的 attempt token")
        return self._repository.restart_after_not_applied(
            task_id=task_id,
            lease_token=lease_token,
            operation_id=step.operation_id,
            previous_attempt_token=step.attempt_token,
            attempt_token=attempt_token,
            evidence=evidence,
        )

    def resume_failed(
            self,
            *,
            task_id: str,
            lease_token: str,
            step: TransferExecutionStep,
    ) -> TransferExecutionStep:
        """使用重试调度取得的新 lease 为 FAILED 步骤创建下一次尝试。"""
        if step.state is not TransferStepState.FAILED or step.attempt_token is not None:
            raise ValueError("只有已释放 attempt 的 FAILED 步骤可以恢复重试")
        attempt_token = self._attempt_token_factory()
        if not attempt_token:
            raise ValueError("恢复重试的 attempt token 不能为空")
        return self._repository.resume_failed_step(
            task_id=task_id,
            lease_token=lease_token,
            operation_id=step.operation_id,
            attempt_token=attempt_token,
        )

    def complete(
            self,
            *,
            task_id: str,
            lease_token: str,
            step: TransferExecutionStep,
            result: TransferStepResult,
    ) -> TransferExecutionStep:
        """使用已开始步骤携带的 attempt token 提交成功证据。"""
        if not step.attempt_token:
            raise ValueError("尚未开始的整理步骤不能提交成功")
        return self._repository.complete_step(
            task_id=task_id,
            lease_token=lease_token,
            operation_id=step.operation_id,
            attempt_token=step.attempt_token,
            result=result,
        )

    def defer(
            self,
            *,
            task_id: str,
            lease_token: str,
            step: TransferExecutionStep,
            error: str,
            retry_due_at: str,
            evidence: Optional[TransferStepResult] = None,
    ) -> TransferExecutionSnapshot:
        """记录已知失败并把唯一重试权交回持久调度器。"""
        if not step.attempt_token:
            raise ValueError("尚未开始的整理步骤不能进入重试等待")
        return self._repository.defer_step(
            task_id=task_id,
            lease_token=lease_token,
            operation_id=step.operation_id,
            attempt_token=step.attempt_token,
            error=error,
            retry_due_at=retry_due_at,
            evidence=evidence,
        )

    def exhaust(
            self,
            *,
            task_id: str,
            lease_token: str,
            step: TransferExecutionStep,
            error: str,
            evidence: Optional[TransferStepResult] = None,
    ) -> TransferExecutionSnapshot:
        """提交达到预算的确定失败，并把任务交给唯一 durable 终态 writer。"""
        if step.state is not TransferStepState.STARTED or not step.attempt_token:
            raise ValueError("只有 STARTED 步骤可提交预算耗尽失败")
        return self._repository.exhaust_step(
            task_id=task_id,
            lease_token=lease_token,
            operation_id=step.operation_id,
            attempt_token=step.attempt_token,
            error=error,
            evidence=evidence,
        )

    def request_retry(
            self,
            *,
            task_id: str,
            reason: str,
            requested_by: str,
    ) -> TransferRetryRequestResult:
        """登记用户重试意图，不直接 claim 或执行任务。"""
        if not task_id or not reason or not requested_by:
            raise ValueError("用户重试请求缺少任务、原因或请求身份")
        return self._repository.request_retry(
            task_id=task_id,
            reason=reason,
            requested_by=requested_by,
        )

    def resolve_manual_review(
            self,
            *,
            task_id: str,
            operation_id: str,
            decision: TransferManualReviewDecision,
            actor: str,
            reason: str,
            result: Optional[TransferStepResult] = None,
    ) -> TransferManualReviewResult:
        """提交人工判定；FAILED 在无 lease durable 结算落地前明确拒绝。"""
        if not all((task_id, operation_id, actor, reason)):
            raise ValueError("人工判定缺少任务、步骤、操作者或原因")
        if decision is TransferManualReviewDecision.APPLIED and result is None:
            raise ValueError("人工判定已发生时必须提供结果证据")
        if decision is TransferManualReviewDecision.FAILED:
            raise TransferExecutionConflictError(
                "人工失败终态尚不能绕过 lease durable 结算"
            )
        return self._repository.resolve_manual_review(
            task_id=task_id,
            operation_id=operation_id,
            decision=decision,
            actor=actor,
            reason=reason,
            result=result,
        )

    def manual_review(
            self,
            *,
            task_id: str,
            lease_token: str,
            step: TransferExecutionStep,
            error: str,
            evidence: Optional[TransferStepResult] = None,
    ) -> TransferExecutionSnapshot:
        """隔离无法严格判断外部结果的步骤，禁止自动再次执行。"""
        return self._repository.mark_manual_review(
            task_id=task_id,
            lease_token=lease_token,
            operation_id=step.operation_id,
            attempt_token=step.attempt_token,
            error=error,
            evidence=evidence,
        )

    def checkpoint(
            self,
            *,
            task_id: str,
            lease_token: str,
            checkpoint: TransferExecutionCheckpoint,
    ) -> TransferExecutionSnapshot:
        """提交可独立重放终态结算的聚合执行结果。"""
        return self._repository.checkpoint_execution(
            task_id=task_id,
            lease_token=lease_token,
            checkpoint=checkpoint,
        )


class TransferManualReviewQuery:
    """收口管理员可发现的人工复核只读用例。"""

    _visible_states = frozenset({
        TransferExecutionState.MANUAL_REVIEW,
        TransferExecutionState.RETRY_WAIT,
    })

    def __init__(self, repository: TransferExecutionRepository) -> None:
        """保存整理执行查询端口。"""
        self._repository = repository

    @classmethod
    def _validate_state(cls, state: TransferExecutionState) -> None:
        """拒绝把内部执行状态扩展到人工复核查询面。"""
        if state not in cls._visible_states:
            raise ValueError(f"人工复核查询不支持状态：{state.value}")

    def list(
            self,
            *,
            state: TransferExecutionState = TransferExecutionState.MANUAL_REVIEW,
            page: int = 1,
            page_size: int = 30,
    ) -> TransferManualReviewPage:
        """分页返回待复核或刚完成复核并等待恢复的任务。"""
        self._validate_state(state)
        if page < 1 or not 1 <= page_size <= 100:
            raise ValueError("人工复核分页参数超出允许范围")
        return self._repository.list_manual_reviews(
            state=state,
            page=page,
            page_size=page_size,
        )

    def get(self, *, task_id: str) -> Optional[TransferManualReviewTaskView]:
        """读取一条人工复核详情，内部状态任务按不存在处理。"""
        if not task_id:
            raise ValueError("人工复核详情缺少任务标识")
        result = self._repository.get_manual_review(task_id=task_id)
        if result is not None:
            self._validate_state(result.state)
        return result

__all__ = [
    "TRANSFER_EXECUTION_VERSION",
    "TRANSFER_STEP_INTENT_VERSION",
    "TRANSFER_STEP_RESULT_VERSION",
    "TransferExecutionCheckpoint",
    "TransferExecutionCommand",
    "TransferExecutionConflictError",
    "TransferExecutionError",
    "TransferExecutionLeaseLostError",
    "TransferOperationObservation",
    "TransferOperationObservationState",
    "TransferRetryRequestResult",
    "TransferExecutionRepository",
    "TransferExecutionSnapshot",
    "TransferExecutionState",
    "TransferExecutionStep",
    "TransferManualReviewDecision",
    "TransferManualReviewPage",
    "TransferManualReviewQuery",
    "TransferManualReviewResult",
    "TransferManualReviewSource",
    "TransferManualReviewStepView",
    "TransferManualReviewTaskView",
    "TransferSettlementIntent",
    "TransferSettlementResult",
    "TransferStepIntent",
    "TransferStepResult",
    "TransferStepRunner",
    "TransferStepState",
    "TransferTerminalState",
    "build_transfer_checkpoint_fingerprint",
    "build_transfer_operation_id",
]
