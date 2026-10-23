"""
整理步骤执行检查点、人工判定与终态结算的应用契约。
"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Protocol
from uuid import uuid4

from app.schemas.exception import BusinessRejectedError
from app.schemas.types import MediaSource, MediaType

# 聚合执行检查点的当前持久格式版本
TRANSFER_EXECUTION_VERSION = 1
# 单步操作意图的当前持久格式版本
TRANSFER_STEP_INTENT_VERSION = 1
# 单步执行结果与探测证据的当前持久格式版本
TRANSFER_STEP_RESULT_VERSION = 1


class TransferExecutionState(StrEnum):
    """
    描述与规划阶段相互独立的持久执行状态。
    """

    # 尚未开始执行外部步骤
    NOT_STARTED = "not_started"
    # 正在执行持久步骤
    RUNNING = "running"
    # 已知失败，等待重试到期
    RETRY_WAIT = "retry_wait"
    # 步骤结果已确定，等待历史与事件共同结算
    SETTLING = "settling"
    # 已结算为失败，等待用户恢复或放弃
    FAILED = "failed"
    # 外部结果无法安全推断，暂停自动执行并等待人工复核
    MANUAL_REVIEW = "manual_review"


class TransferStepState(StrEnum):
    """
    描述单个稳定外部操作的持久执行状态。
    """

    # 操作意图已持久化，尚未开始副作用
    PREPARED = "prepared"
    # 副作用已经发起，尚未持久确认结果
    STARTED = "started"
    # 步骤已完成且成功证据已保存
    SUCCEEDED = "succeeded"
    # 步骤已确定失败，可按任务策略恢复
    FAILED = "failed"
    # 步骤结果不明确，需要人工判定
    MANUAL_REVIEW = "manual_review"


class TransferTerminalState(StrEnum):
    """
    描述允许写入整理历史的确定终态。
    """

    # 整理成功，可以写入成功历史
    SUCCEEDED = "succeeded"
    # 整理失败，可以写入失败历史
    FAILED = "failed"


class TransferExecutionOutcome(StrEnum):
    """
    描述执行检查点可裁决的业务结果。
    """

    # 文件整理已成功完成
    SUCCEEDED = "succeeded"
    # 文件整理明确失败
    FAILED = "failed"
    # 因覆盖策略跳过现有目标，没有执行覆盖
    OVERWRITE_SKIPPED = "overwrite_skipped"


class TransferOperationObservationState(StrEnum):
    """
    描述重启后对遗留 STARTED 外部操作的严格探测结论。
    """

    # 证据确认外部操作已经发生
    APPLIED = "applied"
    # 证据确认外部操作尚未发生，可开始新尝试
    NOT_APPLIED = "not_applied"
    # 证据不足，无法确认操作是否发生
    UNKNOWN = "unknown"
    # 当前外部状态与操作意图或已有证据矛盾
    CONFLICT = "conflict"


class TransferManualReviewDecision(StrEnum):
    """
    描述人工对外部结果不确定步骤作出的显式判定。
    """

    # 管理员确认尚未执行，允许交回调度器重试
    NOT_APPLIED = "not_applied"
    # 管理员确认已经执行，以证据收口该步骤
    APPLIED = "applied"
    # 管理员确认失败，按明确失败恢复任务
    FAILED = "failed"


class TransferExecutionError(RuntimeError):
    """
    表示整理执行持久化状态无法按请求推进。
    """


class TransferExecutionConflictError(TransferExecutionError):
    """
    表示稳定操作身份、尝试身份或检查点证据发生冲突。
    """


class TransferExecutionLeaseLostError(TransferExecutionError):
    """
    表示持久化写入时任务租约已失效或已被其他执行者接管。
    """


class TransferPlanningRejectedError(ValueError, BusinessRejectedError):
    """
    规划输入缺少业务必需信息且继续自动重试不会改变结果。
    """


def _canonical_json(payload: Mapping[str, Any]) -> str:
    """
    生成稳定操作身份使用的规范 JSON。
    """
    return json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def build_transfer_overwrite_paths(
        *,
        source: Mapping[str, Any],
        target_storage: str,
        target_path: str,
        transfer_type: str,
) -> tuple[str, str]:
    """
    从冻结叶操作推导同目录暂存和备份路径，保证执行与账本验证使用相同身份。

    115 重命名会分割主文件名与扩展名，不能使用仅含点号前缀的无扩展名临时文件；
    其临时名保留目标扩展名，其他存储沿用原路径以保持已有步骤的恢复身份。
    """
    identity = _canonical_json({
        "source": dict(source), "storage": target_storage,
        "path": target_path, "type": transfer_type,
    })
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
    target = Path(target_path)
    prefix = "mp" if target_storage == "u115" else ".mp"
    suffix = target.suffix if target_storage == "u115" else ""
    return (
        target.with_name(f"{prefix}-stage-{digest}{suffix}").as_posix(),
        target.with_name(f"{prefix}-backup-{digest}{suffix}").as_posix(),
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
    由冻结计划和操作语义生成跨租约、执行尝试与重启稳定的身份。

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
    """
    由版本化执行检查点数据快照生成稳定 SHA-256 指纹。
    """
    return hashlib.sha256(
        _canonical_json(checkpoint_payload).encode("utf-8")
    ).hexdigest()


@dataclass(frozen=True, slots=True)
class TransferStepIntent:
    """
    保存一次可持久化外部操作的稳定意图。

    :param operation_id: 在同一任务与计划内稳定标识外部操作的键
    :param checkpoint_fingerprint: 所属规划检查点的内容指纹，防止旧步骤应用到新计划
    :param ordinal: 步骤在计划中的顺序，从 0 开始
    :param phase: 步骤所属的整理阶段
    :param kind: 外部操作种类，例如文件转移、覆盖或下载器清理
    :param payload: 外部操作所需的可序列化参数，构造时深复制
    :param version: 该持久数据结构的版本，必须为当前受支持版本
    """

    operation_id: str
    checkpoint_fingerprint: str
    ordinal: int
    phase: str
    kind: str
    payload: dict[str, Any]
    version: int = TRANSFER_STEP_INTENT_VERSION

    def __post_init__(self) -> None:
        """
        拒绝版本未知、身份不完整或不可 JSON 序列化的操作意图。
        """
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
        """
        构造并绑定稳定操作标识的步骤意图。
        """
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
    """
    保存严格执行或探测后得到的版本化步骤证据。

    :param payload: 执行或探测得到的可序列化证据，构造时深复制
    :param version: 该持久数据结构的版本，必须为当前受支持版本
    """

    payload: dict[str, Any]
    version: int = TRANSFER_STEP_RESULT_VERSION

    def __post_init__(self) -> None:
        """
        拒绝未知版本或不可 JSON 序列化的步骤结果。
        """
        object.__setattr__(self, "payload", deepcopy(self.payload))
        if self.version != TRANSFER_STEP_RESULT_VERSION:
            raise ValueError(f"不支持的整理步骤结果版本：{self.version}")
        _canonical_json(self.payload)


@dataclass(frozen=True, slots=True)
class TransferOperationObservation:
    """
    保存外部操作探测结论及其版本化事实证据。

    :param state: 外部操作是否已经发生的探测结论
    :param evidence: 支持该探测结论的版本化事实证据
    """

    state: TransferOperationObservationState
    evidence: TransferStepResult


@dataclass(frozen=True, slots=True)
class TransferExecutionStep:
    """
    提供脱离 ORM Session 的单步骤持久状态投影。

    :param task_id: 持久整理任务的稳定标识
    :param operation_id: 在同一任务与计划内稳定标识外部操作的键
    :param checkpoint_fingerprint: 所属规划检查点的内容指纹，防止旧步骤应用到新计划
    :param ordinal: 步骤在计划中的顺序，从 0 开始
    :param phase: 步骤所属的整理阶段
    :param kind: 外部操作种类，例如文件转移、覆盖或下载器清理
    :param state: 该操作的持久步骤状态
    :param attempt_token: 当前步骤尝试的唯一令牌；尚未开始时为 None
    :param attempt_count: 该步骤已开始执行的尝试次数
    :param intent: 执行前保存的稳定操作意图
    :param result: 成功执行或恢复探测后保存的结果；尚无结果时为 None
    :param last_error: 最近失败的可读原因；未失败时为 None
    :param prepared_at: 步骤意图首次持久化的时间文本
    :param started_at: 当前尝试开始时间；尚未开始时为 None
    :param completed_at: 步骤成功完成时间；尚未完成时为 None
    :param updated_at: 最近一次持久状态更新时间文本
    """

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
    """
    保存所有必要步骤完成后供终态结算重放的聚合结果。

    :param fingerprint: 完整序列化检查点的 SHA-256 指纹
    :param payload: 含明确业务结果与整理结果快照的可序列化内容
    :param operation_ids: 本次聚合结果引用的已完成步骤标识，不允许重复
    :param skip_reason: 未产生外部操作的明确原因；有步骤时可为 None
    :param version: 该持久数据结构的版本，必须为当前受支持版本
    """

    fingerprint: str
    payload: dict[str, Any]
    operation_ids: tuple[str, ...]
    skip_reason: Optional[str] = None
    version: int = TRANSFER_EXECUTION_VERSION

    def __post_init__(self) -> None:
        """
        拒绝重复步骤、空指纹和不可序列化的执行检查点。
        """
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
        """
        构造并绑定完整序列化内容指纹的执行检查点。
        """
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
        """
        编码可跨重启恢复的完整版本化执行检查点。
        """
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
        """
        解析执行事实，并校验同事务历史裁决给出的结算方向。
        """
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
        """
        解析并校验数据库中的执行检查点版本与稳定指纹。
        """
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
    """
    提供任务执行检查点、重试和人工状态的稳定投影。

    :param task_id: 持久整理任务的稳定标识
    :param state: 任务当前的持久执行状态，独立于规划状态
    :param checkpoint: 供终态结算恢复使用的执行检查点；未生成时为 None
    :param retry_generation: 人工重新请求重试后推进的代数，用于区分不同恢复轮次
    :param retry_count: 当前恢复周期已经消耗的重试次数
    :param retry_due_at: 自动重试最早可被认领的时间；未安排重试时为 None
    :param settlement_revision: 终态结算版本，用于识别重复结算和拒绝旧操作
    :param terminal_history_id: 终态回执关联的整理历史主键；未结算时为 None
    :param last_error: 最近失败的可读原因；未失败时为 None
    :param steps: 按稳定顺序保存的全部执行步骤快照
    """

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
    """
    描述已确定结果对应的历史写入与待处理记录终态。

    :param terminal_state: 允许写入历史的明确成功或失败终态
    :param history_payload: 终态历史的可序列化字段，必须包含源路径
    """

    terminal_state: TransferTerminalState
    history_payload: dict[str, Any]

    def __post_init__(self) -> None:
        """
        确保历史数据快照可持久化且包含稳定源身份。
        """
        object.__setattr__(self, "history_payload", deepcopy(self.history_payload))
        if not self.history_payload.get("src"):
            raise ValueError("整理终态历史缺少源路径")
        _canonical_json(self.history_payload)


@dataclass(frozen=True, slots=True)
class TransferSettlementResult:
    """
    描述终态历史是否新提交以及待处理记录是否已删除。

    :param history_id: 关联整理历史的数据库主键
    :param settlement_revision: 终态结算版本，用于识别重复结算和拒绝旧操作
    :param pending_deleted: 本次结算是否已经删除待处理任务记录
    :param already_settled: 是否复用已存在的结算回执，默认 False
    """

    history_id: int
    settlement_revision: int
    pending_deleted: bool
    already_settled: bool = False


@dataclass(frozen=True, slots=True)
class TransferRetryRequestResult:
    """
    描述用户重试请求是否被持久调度器接受。

    :param accepted: 人工重试请求是否被接受
    :param state: 处理重试请求后任务的执行状态
    :param retry_generation: 人工重新请求重试后推进的代数，用于区分不同恢复轮次
    :param message: 操作结果或拒绝原因的可读说明
    """

    accepted: bool
    state: TransferExecutionState
    retry_generation: int
    message: str


@dataclass(frozen=True, slots=True)
class TransferFailureDiscardResult:
    """
    描述终态失败任务是否已解除历史绑定并删除执行证据。

    :param discarded: 是否成功放弃失败任务并解除历史绑定
    :param state: 处理后的任务执行状态；任务不存在时为 None
    :param message: 操作结果或拒绝原因的可读说明
    """

    discarded: bool
    state: Optional[TransferExecutionState]
    message: str


@dataclass(frozen=True, slots=True)
class TransferManualReviewResult:
    """
    描述一次已持久审计的人工判定及后续调度状态。

    :param task_id: 持久整理任务的稳定标识
    :param operation_id: 在同一任务与计划内稳定标识外部操作的键
    :param decision: 本次已接受的人工判定
    :param state: 人工判定后的任务执行状态
    :param review_revision: 人工复核审计版本，随已接受的判定递增
    :param step: 判定完成后保存的步骤快照
    """

    task_id: str
    operation_id: str
    decision: TransferManualReviewDecision
    state: TransferExecutionState
    review_revision: int
    step: TransferExecutionStep


@dataclass(frozen=True, slots=True)
class TransferManualReviewDiscardResult:
    """描述人工明确放弃一个不确定整理任务的结果。

    该操作只解除 durable task 及其步骤证据；绝不执行、回滚或删除任何媒体文件。
    """

    task_id: str
    operation_id: str
    discarded: bool
    message: str


@dataclass(frozen=True, slots=True)
class TransferManualReviewSource:
    """
    提供人工复核任务的最小源文件身份。

    :param storage: 源文件所在的存储配置标识
    :param path: 源文件在存储中的路径
    """

    storage: str
    path: str


@dataclass(frozen=True, slots=True)
class TransferManualReviewStepView:
    """
    提供人工复核所需且不含租约或尝试令牌的步骤证据。

    :param operation_id: 在同一任务与计划内稳定标识外部操作的键
    :param kind: 外部操作种类，例如文件转移、覆盖或下载器清理
    :param intent: 供管理员检查的操作意图，不包含租约或尝试令牌
    :param evidence: 外部结果的已有探测证据；尚未取得时为 None
    :param error: 操作失败的可读原因；无错误时为 None
    """

    operation_id: str
    kind: str
    intent: dict[str, Any]
    evidence: Optional[dict[str, Any]]
    error: Optional[str]


@dataclass(frozen=True, slots=True)
class TransferManualReviewTaskView:
    """
    提供管理员发现与复核持久化整理任务的公开投影。

    :param task_id: 持久整理任务的稳定标识
    :param source: 复核目标的源存储与路径
    :param state: 当前需要人工关注的执行状态
    :param step: 本次需要复核的步骤及证据
    :param review_revision: 人工复核审计版本，随已接受的判定递增
    """

    task_id: str
    source: TransferManualReviewSource
    state: TransferExecutionState
    step: TransferManualReviewStepView
    review_revision: int


@dataclass(frozen=True, slots=True)
class TransferManualReviewPage:
    """
    提供稳定分页的人工复核任务结果。

    :param items: 当前页的人工复核任务
    :param total: 满足查询状态的任务总数
    :param page: 当前页码，从 1 开始
    :param page_size: 每页最多返回的任务数量
    """

    items: tuple[TransferManualReviewTaskView, ...]
    total: int
    page: int
    page_size: int


class TransferExecutionRepository(Protocol):
    """
    定义步骤状态、执行检查点与终态历史的持久化端口。

    写操作以独立短事务保存状态，外部副作用由调用方在事务外执行。
    需要租约的状态推进同时核对步骤尝试身份，冲突不得覆盖既有证据。
    """

    def get_snapshot(self, *, task_id: str) -> Optional[TransferExecutionSnapshot]:
        """
        读取任务执行快照。

        :param task_id: 目标持久整理任务的稳定标识
        :return: 当前执行快照；任务不存在时为 None
        """

    def isolate_unhandled_failure(
            self, *, task_id: str, lease_token: str, error: str,
    ) -> bool:
        """
        将步骤账本之外的异常计入失败次数并隔离复核，不推测外部操作结果。

        :param task_id: 目标持久整理任务的稳定标识
        :param lease_token: 当前认领获得的有效租约令牌，用于拒绝过期执行者
        :param error: 需要持久记录的可读失败原因
        :return: 有效租约下成功隔离时为 True；任务或租约条件不匹配时为 False
        """

    async def async_get_snapshot(
            self,
            *,
            task_id: str,
    ) -> Optional[TransferExecutionSnapshot]:
        """
        异步读取任务执行快照，供事件循环内状态观察使用。

        :param task_id: 目标持久整理任务的稳定标识
        :return: 当前执行快照；任务不存在时为 None
        """

    def list_manual_reviews(
            self,
            *,
            state: TransferExecutionState,
            page: int,
            page_size: int,
    ) -> TransferManualReviewPage:
        """
        按严格公开状态分页读取人工复核任务。

        :param state: 待查询的公开执行状态，限定需要人工关注的任务
        :param page: 页码，从 1 开始
        :param page_size: 每页最多返回的任务数量
        :return: 满足状态条件的任务分页及总数
        """

    def get_manual_review(
            self,
            *,
            task_id: str,
    ) -> Optional[TransferManualReviewTaskView]:
        """
        按任务标识读取人工复核公开详情。

        :param task_id: 目标持久整理任务的稳定标识
        :return: 任务的公开复核详情；不存在或不在可复核范围时为 None
        """

    def prepare_step(
            self,
            *,
            task_id: str,
            lease_token: str,
            intent: TransferStepIntent,
    ) -> TransferExecutionStep:
        """
        在外部副作用前持久化稳定步骤意图。

        :param task_id: 目标持久整理任务的稳定标识
        :param lease_token: 当前认领获得的有效租约令牌，用于拒绝过期执行者
        :param intent: 准备写入的稳定操作意图，必须属于当前计划
        :return: 已持久化的准备步骤；相同意图可复用原步骤
        """

    def start_step(
            self,
            *,
            task_id: str,
            lease_token: str,
            operation_id: str,
            attempt_token: str,
    ) -> TransferExecutionStep:
        """
        以当前租约和新尝试令牌标记步骤开始。

        :param task_id: 目标持久整理任务的稳定标识
        :param lease_token: 当前认领获得的有效租约令牌，用于拒绝过期执行者
        :param operation_id: 目标步骤的稳定操作标识
        :param attempt_token: 本次步骤执行的唯一尝试令牌
        :return: 写入本次尝试令牌后的已开始步骤
        """

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
        """
        严格探测为未发生后，以新尝试令牌安全重启遗留步骤。

        :param task_id: 目标持久整理任务的稳定标识
        :param lease_token: 当前认领获得的有效租约令牌，用于拒绝过期执行者
        :param operation_id: 目标步骤的稳定操作标识
        :param previous_attempt_token: 待恢复的旧尝试令牌，必须仍与持久步骤匹配
        :param attempt_token: 本次步骤执行的唯一尝试令牌
        :param evidence: 证明旧尝试尚未产生外部副作用的探测证据
        :return: 根据未发生证据启用新尝试后的步骤
        """

    def resume_failed_step(
            self,
            *,
            task_id: str,
            lease_token: str,
            operation_id: str,
            attempt_token: str,
    ) -> TransferExecutionStep:
        """
        任务到期并重新认领后，以新尝试令牌重试 FAILED 步骤。

        :param task_id: 目标持久整理任务的稳定标识
        :param lease_token: 当前认领获得的有效租约令牌，用于拒绝过期执行者
        :param operation_id: 目标步骤的稳定操作标识
        :param attempt_token: 本次步骤执行的唯一尝试令牌
        :return: 重新认领并开始新尝试后的步骤
        """

    def complete_step(
            self,
            *,
            task_id: str,
            lease_token: str,
            operation_id: str,
            attempt_token: str,
            result: TransferStepResult,
    ) -> TransferExecutionStep:
        """
        以租约和 attempt 双 CAS 提交成功证据。

        :param task_id: 目标持久整理任务的稳定标识
        :param lease_token: 当前认领获得的有效租约令牌，用于拒绝过期执行者
        :param operation_id: 目标步骤的稳定操作标识
        :param attempt_token: 本次步骤执行的唯一尝试令牌
        :param result: 本次操作已经完成的版本化事实证据
        :return: 已保存成功证据的步骤快照
        """

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
        """
        持久化已知失败、到期时间并原子释放当前租约。

        :param task_id: 目标持久整理任务的稳定标识
        :param lease_token: 当前认领获得的有效租约令牌，用于拒绝过期执行者
        :param operation_id: 目标步骤的稳定操作标识
        :param attempt_token: 本次步骤执行的唯一尝试令牌
        :param error: 需要持久记录的可读失败原因
        :param retry_due_at: 下一次自动认领的最早时间文本
        :param evidence: 支持失败或人工隔离判断的探测证据；None 表示无额外证据
        :return: 已记录失败和重试时间、释放租约后的任务快照
        """

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
        """
        重试预算耗尽时保留租约，并建立可持久化失败结算的检查点。

        :param task_id: 目标持久整理任务的稳定标识
        :param lease_token: 当前认领获得的有效租约令牌，用于拒绝过期执行者
        :param operation_id: 目标步骤的稳定操作标识
        :param attempt_token: 本次步骤执行的唯一尝试令牌
        :param error: 需要持久记录的可读失败原因
        :param evidence: 支持失败或人工隔离判断的探测证据；None 表示无额外证据
        :return: 保存失败结算检查点后的任务快照，租约仍供当前执行者结算使用
        """

    def request_retry(
            self,
            *,
            task_id: str,
            reason: str,
            requested_by: str,
    ) -> TransferRetryRequestResult:
        """
        仅把 FAILED 终态转入到期可认领的 retry_wait。

        :param task_id: 目标持久整理任务的稳定标识
        :param reason: 发起恢复操作的可读理由，用于审计
        :param requested_by: 发起人工重试的用户或系统身份
        :return: 是否接受重试、当前执行状态与重试代数
        """

    def request_replan(
            self, *, history_id: int, task_id: str, settlement_revision: int,
            media_source: str, media_id: str,
            media_type: str, season: Optional[int], episode: Optional[int],
            requested_by: str,
    ) -> TransferRetryRequestResult:
        """
        为尚未发生文件副作用的识别拒绝更新规划输入，并保留原拒绝证据。

        :param history_id: 当前失败终态关联的历史标识
        :param task_id: 分析开始时历史关联的持久任务标识
        :param settlement_revision: 分析开始时的结算版本，用于拒绝陈旧修正
        :param media_source: 已核实的媒体来源
        :param media_id: 已核实的来源原生标识
        :param media_type: 已核实的电影或电视剧类型
        :param season: 修正后的季号；None 保留原值
        :param episode: 修正后的单集集号；None 保留原值
        :param requested_by: 提交修正的身份，用于审计
        :return: 是否接纳重新规划及持久调度状态
        """

    def discard_corrupt_task(
            self,
            *,
            task_id: str,
            lease_token: str,
            error: str,
    ) -> bool:
        """
        在当前租约下原子清除无法继续执行的损坏任务及其恢复证据。

        :param task_id: 目标持久整理任务的稳定标识
        :param lease_token: 当前认领获得的有效租约令牌，用于拒绝过期执行者
        :param error: 需要持久记录的可读失败原因
        :return: 完成损坏任务清理时为 True；必需身份或原因为空时为 False
        """

    def discard_corrupt_by_history(
            self,
            *,
            task_id: str,
            history_id: int,
    ) -> TransferFailureDiscardResult:
        """
        清理无活动租约的损坏任务，并解除对应历史绑定。

        :param task_id: 目标持久整理任务的稳定标识
        :param history_id: 当前任务绑定的失败历史主键
        :return: 任务清理结果及拒绝原因；目标已不存在时也视为已清理
        """

    def discard_failed(
            self,
            *,
            task_id: str,
            history_id: int,
            settlement_revision: int,
    ) -> TransferFailureDiscardResult:
        """
        仅放弃与指定失败回执完全匹配的无租约 FAILED 任务。

        :param task_id: 目标持久整理任务的稳定标识
        :param history_id: 当前任务绑定的失败历史主键
        :param settlement_revision: 调用方读取的结算版本，必须与当前失败回执匹配
        :return: 是否解除匹配的失败任务、当前状态及结果说明
        """

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
        """
        无租约地原子提交人工判定审计并交回唯一调度器。

        :param task_id: 目标持久整理任务的稳定标识
        :param operation_id: 目标步骤的稳定操作标识
        :param decision: 管理员对外部操作结果的明确判定
        :param actor: 作出人工判定的管理员身份
        :param reason: 发起恢复操作的可读理由，用于审计
        :param result: 人工确认操作已发生时提供的结果证据；其他判定可为 None
        :return: 已持久审计的判定、复核版本及后续步骤状态
        """

    def discard_manual_review(
            self,
            *,
            task_id: str,
            operation_id: str,
            actor: str,
            reason: str,
    ) -> TransferManualReviewDiscardResult:
        """放弃无租约人工复核任务，且不得触碰源文件或目标文件。"""

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
        """
        持久化不可判定结果并从自动调度中隔离任务。

        :param task_id: 目标持久整理任务的稳定标识
        :param lease_token: 当前认领获得的有效租约令牌，用于拒绝过期执行者
        :param operation_id: 目标步骤的稳定操作标识
        :param error: 需要持久记录的可读失败原因
        :param evidence: 支持失败或人工隔离判断的探测证据；None 表示无额外证据
        :param attempt_token: 需要核对的当前步骤尝试令牌；None 表示该调用不指定尝试
        :return: 已从自动调度隔离的任务快照
        """

    def checkpoint_execution(
            self,
            *,
            task_id: str,
            lease_token: str,
            checkpoint: TransferExecutionCheckpoint,
    ) -> TransferExecutionSnapshot:
        """
        确认所有引用步骤成功后提交聚合执行检查点。

        :param task_id: 目标持久整理任务的稳定标识
        :param lease_token: 当前认领获得的有效租约令牌，用于拒绝过期执行者
        :param checkpoint: 引用已完成步骤的完整执行结果检查点
        :return: 保存完整执行检查点后的任务快照
        """


class TransferStepRunner(Protocol):
    """
    定义文件执行方可注入的单步骤持久执行边界。
    """

    def run(
            self,
            *,
            phase: str,
            kind: str,
            payload: Mapping[str, Any],
            execute: Callable[[], TransferStepResult],
            observe: Callable[[], TransferOperationObservation],
    ) -> TransferStepResult:
        """
        持久编排一次外部副作用，并在遗留尝试时先严格探测。

        :param phase: 本步骤所属的执行阶段
        :param kind: 本步骤执行的外部操作种类
        :param payload: 生成稳定操作意图的可序列化参数
        :param execute: 真正执行外部副作用并返回证据的回调，在事务外运行
        :param observe: 遇到未确认旧尝试时探测外部结果的回调
        :return: 新执行或已确认旧执行产生的稳定结果证据
        """


class TransferExecutionCommand:
    """
    以类型化命令收口步骤状态机，外部 I/O 由调用方在事务外执行。
    """

    def __init__(
            self,
            repository: TransferExecutionRepository,
            *,
            attempt_token_factory: Callable[[], str] = lambda: uuid4().hex,
    ) -> None:
        """
        保存持久化端口和可替换的尝试令牌工厂。
        """
        self._repository = repository
        self._attempt_token_factory = attempt_token_factory

    def prepare(
            self,
            *,
            task_id: str,
            lease_token: str,
            intent: TransferStepIntent,
    ) -> TransferExecutionStep:
        """
        在外部调用前提交步骤意图。
        """
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
        """
        生成单次尝试令牌并持久化开始状态。
        """
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
        """
        在严格 NOT_APPLIED 证据成立时轮换尝试令牌并继续执行。
        """
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
        """
        使用重试调度取得的新租约为 FAILED 步骤创建下一次尝试。
        """
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
        """
        使用已开始步骤携带的尝试令牌提交成功证据。
        """
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
        """
        记录已知失败并把唯一重试权交回持久调度器。
        """
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
        """
        提交达到预算的确定失败，并把任务交给唯一持久化终态 writer。
        """
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
        """
        登记用户重试意图，不直接认领或执行任务。
        """
        if not task_id or not reason or not requested_by:
            raise ValueError("用户重试请求缺少任务、原因或请求身份")
        return self._repository.request_retry(
            task_id=task_id,
            reason=reason,
            requested_by=requested_by,
        )

    def request_replan(
            self, *, history_id: int, task_id: str, settlement_revision: int,
            media_source: str, media_id: str,
            media_type: str, season: Optional[int] = None, episode: Optional[int] = None,
            requested_by: str,
    ) -> TransferRetryRequestResult:
        """
        登记已核实的媒体和季集修正，具体安全校验及原子更新由仓储执行。

        :param history_id: 当前失败终态关联的历史标识
        :param task_id: 分析开始时历史关联的持久任务标识
        :param settlement_revision: 分析开始时的结算版本，用于拒绝陈旧修正
        :param media_source: 已核实的媒体来源
        :param media_id: 已核实的来源原生标识
        :param media_type: 已核实的电影或电视剧类型
        :param season: 修正后的季号；None 保留原值
        :param episode: 修正后的单集集号；None 保留原值
        :param requested_by: 提交修正的身份，用于审计
        :return: 是否接纳重新规划及持久调度状态
        """
        if (history_id <= 0 or settlement_revision <= 0
                or not all((task_id, media_source, media_id, media_type, requested_by))):
            raise ValueError("重新规划缺少历史、媒体身份或请求身份")
        normalized_type = MediaType.from_agent(media_type)
        if normalized_type is None or normalized_type not in {MediaType.MOVIE, MediaType.TV}:
            raise ValueError("识别修正只支持 movie 或 tv")
        if not media_id.strip() or media_id.strip() == "0":
            raise ValueError("media_id 必须是非零的来源原生 ID")
        if (season is not None and season < 0) or (episode is not None and episode < 1):
            raise ValueError("季集修正超出有效范围")
        if normalized_type is MediaType.MOVIE and (season is not None or episode is not None):
            raise ValueError("电影识别修正不能指定季集")
        return self._repository.request_replan(
            history_id=history_id, task_id=task_id, settlement_revision=settlement_revision,
            media_source=MediaSource(media_source).value, media_id=media_id.strip(),
            media_type=normalized_type.value, season=season, episode=episode, requested_by=requested_by,
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
        """
        提交人工判定，理由可留空；FAILED 仍须经租约持久化结算。
        """
        if not all((task_id, operation_id, actor)):
            raise ValueError("人工判定缺少任务、步骤或操作者")
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

    def discard_manual_review(
            self,
            *,
            task_id: str,
            operation_id: str,
            actor: str,
            reason: str,
    ) -> TransferManualReviewDiscardResult:
        """显式解除卡住的人工复核任务；文件状态仍由用户自行处理。"""
        if not all((task_id, operation_id, actor)):
            raise ValueError("放弃人工复核缺少任务、步骤或操作者")
        return self._repository.discard_manual_review(
            task_id=task_id,
            operation_id=operation_id,
            actor=actor,
            reason=reason,
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
        """
        隔离无法严格判断外部结果的步骤，禁止自动再次执行。
        """
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
        """
        提交可独立重放终态结算的聚合执行结果。
        """
        return self._repository.checkpoint_execution(
            task_id=task_id,
            lease_token=lease_token,
            checkpoint=checkpoint,
        )


class TransferManualReviewQuery:
    """
    收口管理员可发现的人工复核只读用例。
    """

    _visible_states = frozenset({
        TransferExecutionState.MANUAL_REVIEW,
        TransferExecutionState.RETRY_WAIT,
    })

    def __init__(self, repository: TransferExecutionRepository) -> None:
        """
        保存整理执行查询端口。
        """
        self._repository = repository

    @classmethod
    def _validate_state(cls, state: TransferExecutionState) -> None:
        """
        拒绝把内部执行状态扩展到人工复核查询面。
        """
        if state not in cls._visible_states:
            raise ValueError(f"人工复核查询不支持状态：{state.value}")

    def list(
            self,
            *,
            state: TransferExecutionState = TransferExecutionState.MANUAL_REVIEW,
            page: int = 1,
            page_size: int = 30,
    ) -> TransferManualReviewPage:
        """
        分页返回待复核或刚完成复核并等待恢复的任务。
        """
        self._validate_state(state)
        if page < 1 or not 1 <= page_size <= 100:
            raise ValueError("人工复核分页参数超出允许范围")
        return self._repository.list_manual_reviews(
            state=state,
            page=page,
            page_size=page_size,
        )

    def get(self, *, task_id: str) -> Optional[TransferManualReviewTaskView]:
        """
        读取一条人工复核详情，内部状态任务按不存在处理。
        """
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
    "TransferFailureDiscardResult",
    "TransferExecutionLeaseLostError",
    "TransferOperationObservation",
    "TransferOperationObservationState",
    "TransferRetryRequestResult",
    "TransferExecutionRepository",
    "TransferExecutionSnapshot",
    "TransferExecutionState",
    "TransferExecutionStep",
    "TransferManualReviewDecision",
    "TransferManualReviewDiscardResult",
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
