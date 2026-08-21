"""事件 payload、可见性、投递与可靠性契约清单。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ValidationError

import app.schemas.event as event_schemas
from app.schemas.types import ChainEventType, EventType


class EventDelivery(StrEnum):
    """描述事件当前的交付保证。"""

    EPHEMERAL = "ephemeral"
    DURABLE_REQUIRED = "durable_required"


class EventVisibility(StrEnum):
    """描述事件是否属于插件公开扩展面。"""

    HOST_ONLY = "host_only"
    PLUGIN_PUBLIC = "plugin_public"
    TARGET_PLUGIN = "target_plugin"


class EventErrorBehavior(StrEnum):
    """描述 handler 异常后的传播规则。"""

    ISOLATE = "isolate"
    STOP_CHAIN = "stop_chain"
    NOTIFY = "notify"


@dataclass(frozen=True, slots=True)
class EventContract:
    """冻结一个事件的 payload 与运行语义。"""

    event_name: str
    payload_model: type[BaseModel] | None
    payload_contract: str
    mode: str
    visibility: EventVisibility
    delivery: EventDelivery
    error_behavior: EventErrorBehavior
    ordering: str
    sensitive_fields: tuple[str, ...] = ()
    legacy_reason: str | None = None


_PAYLOAD_MODELS: dict[EventType | ChainEventType, type[BaseModel]] = {
    EventType.ConfigChanged: event_schemas.ConfigChangeEventData,
    EventType.AgentTokensUsage: event_schemas.AgentTokensUsageEventData,
    EventType.SubscribeAdded: event_schemas.SubscribeAddedEventData,
    EventType.SubscribeDeleted: event_schemas.SubscribeDeletedEventData,
    EventType.SubscribeModified: event_schemas.SubscribeModifiedEventData,
    ChainEventType.PluginDataReset: event_schemas.PluginDataResetEventData,
    ChainEventType.AuthVerification: event_schemas.AuthCredentials,
    ChainEventType.AuthIntercept: event_schemas.AuthInterceptCredentials,
    ChainEventType.CommandRegister: event_schemas.CommandRegisterEventData,
    ChainEventType.TransferRename: event_schemas.TransferRenameEventData,
    ChainEventType.TransferRenameBuild: event_schemas.TransferRenameBuildEventData,
    ChainEventType.TransferIntercept: event_schemas.TransferInterceptEventData,
    ChainEventType.TransferOverwriteCheck: event_schemas.TransferOverwriteCheckEventData,
    ChainEventType.ResourceSelection: event_schemas.ResourceSelectionEventData,
    ChainEventType.ResourceDownload: event_schemas.ResourceDownloadEventData,
    ChainEventType.DiscoverSource: event_schemas.DiscoverSourceEventData,
    ChainEventType.MediaRecognizeConvert: event_schemas.MediaRecognizeConvertEventData,
    ChainEventType.RecommendSource: event_schemas.RecommendSourceEventData,
    ChainEventType.StorageOperSelection: event_schemas.StorageOperSelectionEventData,
    ChainEventType.AgentLLMProvider: event_schemas.AgentLLMProviderEventData,
    ChainEventType.SubscribeEpisodesRefresh: event_schemas.SubscribeEpisodesRefreshEventData,
    ChainEventType.SubscribeCompletionCheck: event_schemas.SubscribeCompletionCheckEventData,
}

_DURABLE_REQUIRED = {
    EventType.SubscribeAdded,
    EventType.SubscribeModified,
    EventType.SubscribeDeleted,
    EventType.DownloadAdded,
    EventType.TransferComplete,
    EventType.TransferFailed,
}
_TARGET_PLUGIN = {EventType.PluginAction, EventType.PluginTriggered}
_HOST_ONLY = {EventType.SystemError, EventType.ConfigChanged, EventType.ModuleReload}
_SENSITIVE_FIELDS = {
    ChainEventType.AuthVerification: ("password", "token", "mfa_code"),
    ChainEventType.AuthIntercept: ("token",),
    ChainEventType.AgentLLMProvider: ("api_key",),
}


def _build_contract(event_type: EventType | ChainEventType) -> EventContract:
    """按 enum 类别和首批 model 映射构造完整契约。"""
    payload_model = _PAYLOAD_MODELS.get(event_type)
    is_chain = isinstance(event_type, ChainEventType)
    visibility = EventVisibility.PLUGIN_PUBLIC
    if event_type in _TARGET_PLUGIN:
        visibility = EventVisibility.TARGET_PLUGIN
    elif event_type in _HOST_ONLY:
        visibility = EventVisibility.HOST_ONLY
    return EventContract(
        event_name=f"{event_type.__class__.__name__}.{event_type.name}",
        payload_model=payload_model,
        payload_contract=payload_model.__name__ if payload_model else "legacy_dict",
        mode="chain" if is_chain else "broadcast",
        visibility=visibility,
        delivery=(
            EventDelivery.DURABLE_REQUIRED
            if event_type in _DURABLE_REQUIRED
            else EventDelivery.EPHEMERAL
        ),
        error_behavior=(
            EventErrorBehavior.STOP_CHAIN if is_chain else EventErrorBehavior.NOTIFY
        ),
        ordering="priority_serial" if is_chain else "priority_queue",
        sensitive_fields=_SENSITIVE_FIELDS.get(event_type, ()),
        legacy_reason=(
            None
            if payload_model
            else "现有插件 payload 尚未收敛为稳定 model，保留原始 dict ABI"
        ),
    )


EVENT_CONTRACTS = {
    event_type: _build_contract(event_type)
    for event_type in (*tuple(EventType), *tuple(ChainEventType))
}


def normalize_event_type(
    event_type: EventType | ChainEventType | str,
) -> EventType | ChainEventType | str:
    """把旧 SDK 传入的已知字符串恢复为 enum，未知扩展值保持原样。"""
    if not isinstance(event_type, str):
        return event_type
    for enum_type in (EventType, ChainEventType):
        try:
            return enum_type(event_type)
        except ValueError:
            continue
    return event_type


def get_event_contract(
    event_type: EventType | ChainEventType | str,
) -> EventContract:
    """返回已登记事件的完整契约，兼容传入 enum value 字符串。"""
    normalized = normalize_event_type(event_type)
    if isinstance(normalized, str):
        raise KeyError(normalized)
    return EVENT_CONTRACTS[normalized]


def validate_event_payload(
    event_type: EventType | ChainEventType | str,
    payload: Any,
) -> tuple[str, ...]:
    """在发送边界诊断首批 typed payload，保持原对象和插件 dict 形状不变。"""
    try:
        model = get_event_contract(event_type).payload_model
    except KeyError:
        # 动态插件在旧 ABI 下可能使用宿主枚举之外的字符串事件。
        return ()
    if model is None or payload is None:
        return ()
    if isinstance(payload, model):
        return ()
    try:
        model.model_validate(payload)
    except (ValidationError, TypeError, ValueError) as error:
        return (str(error),)
    return ()
