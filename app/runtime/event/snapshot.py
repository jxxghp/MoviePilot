"""
事件 payload 的只读契约快照访问。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ValidationError

from app.runtime.event.contracts import (
    describe_event_validation_error,
    get_event_contract,
    normalize_event_type,
)
from app.schemas.types import ChainEventType, EventType


@dataclass(frozen=True, slots=True)
class EventPayloadSnapshot:
    """
    保存原始 payload 及按登记契约解析出的独立快照。

    :param event_type: 原事件的枚举或字符串名称
    :param raw: 未经模型解析的原始载荷
    :param payload: 按完整载荷模型解析的快照；未声明或失败时为 None
    :param input: 按链式输入模型解析的快照；未声明或失败时为 None
    :param output: 按链式输出模型解析的快照；未声明或失败时为 None
    :param known: 事件是否在契约目录中登记
    :param schema_version: 登记契约的结构版本；未知事件时为 None
    :param payload_mode: 登记契约的载荷保存模式；未知事件时为 None
    :param validation_mode: 登记契约的校验策略；未知事件时为 None
    :param errors: 模型解析和契约校验的错误说明，成功时为空元组
    """

    event_type: EventType | ChainEventType | str
    raw: Any
    payload: BaseModel | None
    input: BaseModel | None
    output: BaseModel | None
    known: bool
    schema_version: int | None
    payload_mode: str | None
    validation_mode: str | None
    errors: tuple[str, ...] = ()

    @property
    def valid(self) -> bool:
        """
        判断已知事件的全部登记模型是否都通过校验。

        :return: 事件已登记且没有校验错误时返回 True
        """
        return self.known and not self.errors


def _model_snapshot(
    model: type[BaseModel] | None,
    payload: Any,
    label: str,
    cache: dict[type[BaseModel], BaseModel | None],
    errors: list[str],
) -> BaseModel | None:
    """
    按单个模型构造快照，同一模型在一次访问中只解析一次。
    """
    if model is None:
        return None
    if model in cache:
        return cache[model]
    if isinstance(payload, model):
        cache[model] = payload.model_copy(deep=True)
        return cache[model]
    try:
        cache[model] = model.model_validate(payload).model_copy(deep=True)
    except (ValidationError, TypeError, ValueError) as error:
        cache[model] = None
        errors.append(f"{label}:{describe_event_validation_error(model, error)}")
    return cache[model]


def snapshot_event_data(
    event_type: EventType | ChainEventType | str,
    event_data: Any,
) -> EventPayloadSnapshot:
    """
    按事件注册契约解析 payload，未知自定义事件保持原始数据并返回未登记状态。
    """
    normalized = normalize_event_type(event_type)
    try:
        contract = get_event_contract(normalized)
    except KeyError:
        return EventPayloadSnapshot(
            event_type=normalized,
            raw=event_data,
            payload=None,
            input=None,
            output=None,
            known=False,
            schema_version=None,
            payload_mode=None,
            validation_mode=None,
        )

    errors: list[str] = []
    cache: dict[type[BaseModel], BaseModel | None] = {}
    payload_snapshot = _model_snapshot(
        contract.payload_model,
        event_data,
        "payload",
        cache,
        errors,
    )
    input_snapshot = _model_snapshot(
        contract.input_model,
        event_data,
        "input",
        cache,
        errors,
    )
    output_snapshot = _model_snapshot(
        contract.output_model,
        event_data,
        "output",
        cache,
        errors,
    )
    return EventPayloadSnapshot(
        event_type=normalized,
        raw=event_data,
        payload=payload_snapshot,
        input=input_snapshot,
        output=output_snapshot,
        known=True,
        schema_version=contract.schema_version,
        payload_mode=contract.payload_mode.value,
        validation_mode=contract.validation_mode.value,
        errors=tuple(errors),
    )


__all__ = ["EventPayloadSnapshot", "snapshot_event_data"]
