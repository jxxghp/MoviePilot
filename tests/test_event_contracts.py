"""Event Contract Registry 完整性和兼容校验测试。"""

from unittest.mock import patch

from app.runtime.event.contracts import (
    EVENT_CONTRACTS,
    EventDelivery,
    get_event_contract,
    validate_event_payload,
)
from app.runtime.events import Event
from app.schemas.event import ConfigChangeEventData
from app.schemas.types import ChainEventType, EventType


def test_every_event_enum_has_complete_contract() -> None:
    """53 个广播/链式事件必须全部登记且 legacy 项必须解释原因。"""
    expected = {*EventType, *ChainEventType}

    assert set(EVENT_CONTRACTS) == expected
    assert len(EVENT_CONTRACTS) == 53
    for contract in EVENT_CONTRACTS.values():
        assert contract.mode in {"broadcast", "chain"}
        assert contract.payload_contract
        if contract.payload_contract == "legacy_dict":
            assert contract.legacy_reason


def test_typed_payload_is_validated_without_changing_public_shape() -> None:
    """旧 dict 通过 model 校验后仍以同一个 dict 对象投递给插件。"""
    payload = {"key": "PROXY_HOST", "value": "http://proxy"}

    assert validate_event_payload(EventType.ConfigChanged, payload) == ()
    event = Event(EventType.ConfigChanged, payload)

    assert event.event_data is payload
    assert isinstance(event.event_data, dict)


def test_invalid_typed_payload_is_diagnostic_only() -> None:
    """兼容阶段坏 payload 产生诊断但不阻断既有投递。"""
    payload = {"value": "missing-key"}

    with patch("app.runtime.events.logger.warning") as warning:
        event = Event(EventType.ConfigChanged, payload)

    assert event.event_data is payload
    warning.assert_called_once()


def test_selected_user_side_effects_are_marked_durable_required() -> None:
    """订阅、下载和整理完成事件必须明确暴露后续 durable pilot 要求。"""
    for event_type in (
        EventType.SubscribeAdded,
        EventType.SubscribeModified,
        EventType.SubscribeDeleted,
        EventType.DownloadAdded,
        EventType.TransferComplete,
        EventType.TransferFailed,
    ):
        assert get_event_contract(event_type).delivery is EventDelivery.DURABLE_REQUIRED


def test_model_instance_remains_mutable_chain_payload() -> None:
    """链式处理器继续接收原 model 实例，确保输出字段可原地接力。"""
    payload = ConfigChangeEventData(key={"PROXY_HOST"})
    event = Event(EventType.ConfigChanged, payload)

    assert event.event_data is payload
