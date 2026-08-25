"""事件 payload 的只读契约快照访问。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ValidationError

from app.runtime.event.contracts import get_event_contract, normalize_event_type
from app.schemas.types import ChainEventType, EventType


@dataclass(frozen=True, slots=True)
class EventPayloadSnapshot:
    """保存原始 payload 及按登记契约解析出的独立快照。"""

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
        """返回已知事件的全部登记模型是否都通过校验。"""
        return self.known and not self.errors


def _model_snapshot(
    model: type[BaseModel] | None,
    payload: Any,
    label: str,
    cache: dict[type[BaseModel], BaseModel | None],
    errors: list[str],
) -> BaseModel | None:
    """按单个模型构造快照，同一模型在一次访问中只解析一次。"""
    if model is None:
        return None
    if model in cache:
        return cache[model]
    if isinstance(payload, model):
        cache[model] = payload.model_copy(deep=True)
        return cache[model]
    try:
        cache[model] = model.model_validate(payload)
    except (ValidationError, TypeError, ValueError) as error:
        cache[model] = None
        errors.append(f"{label}:{model.__name__}: {error}")
    return cache[model]


def snapshot_event_data(
    event_type: EventType | ChainEventType | str,
    event_data: Any,
) -> EventPayloadSnapshot:
    """按事件注册契约解析 payload，未知自定义事件保持原始数据并返回未登记状态。"""
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
