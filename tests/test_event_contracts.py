"""Event Contract Registry 完整性和兼容校验测试。"""

from unittest.mock import patch

import pytest

from app.application.outbox import (
    DURABLE_EVENT_TOPICS,
    validate_durable_event_handlers,
)
from app.runtime.event.contracts import (
    EVENT_CONTRACTS,
    EventDelivery,
    get_event_contract,
    validate_event_payload,
)
from app.runtime.events import Event
from app.domain.context import Context, MediaInfo
from app.domain.metainfo import MetaInfo
from app.schemas.event import ConfigChangeEventData
from app.schemas.file import FileItem
from app.schemas.transfer import TransferInfo
from app.schemas.types import ChainEventType, EventType, MediaType


def test_every_event_enum_has_complete_contract() -> None:
    """53 个广播/链式事件必须全部登记并绑定 typed payload。"""
    expected = {*EventType, *ChainEventType}

    assert set(EVENT_CONTRACTS) == expected
    assert len(EVENT_CONTRACTS) == 53
    for contract in EVENT_CONTRACTS.values():
        assert contract.mode in {"broadcast", "chain"}
        assert contract.payload_contract
        assert contract.payload_model is not None
        assert contract.payload_contract != "legacy_dict"
        assert contract.legacy_reason is None


def test_extensible_plugin_payload_accepts_custom_fields_without_shape_change() -> None:
    """插件动作 contract 只校验公共字段，插件自定义字段和原始 dict 均保持不变。"""
    payload = {
        "plugin_id": "DemoPlugin",
        "action": "refresh",
        "plugin_owned_field": {"value": 1},
    }

    assert validate_event_payload(EventType.PluginAction, payload) == ()
    event = Event(EventType.PluginAction, payload)

    assert event.event_data is payload
    assert event.event_data["plugin_owned_field"] == {"value": 1}


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
    """所有 durable-required 事件必须一一登记唯一的恢复 topic。"""
    durable_events = {
        event_type
        for event_type, contract in EVENT_CONTRACTS.items()
        if contract.delivery is EventDelivery.DURABLE_REQUIRED
    }

    assert set(DURABLE_EVENT_TOPICS) == durable_events
    assert len(set(DURABLE_EVENT_TOPICS.values())) == len(durable_events)


def test_durable_event_dispatcher_requires_every_recovery_handler() -> None:
    """恢复 dispatcher 缺少任一登记 topic 时必须在构造边界失败。"""
    handlers = {
        topic: lambda _message: None
        for topic in DURABLE_EVENT_TOPICS.values()
    }
    validate_durable_event_handlers(handlers)

    handlers.pop(next(iter(DURABLE_EVENT_TOPICS.values())))
    with pytest.raises(RuntimeError, match="缺少 durable 事件 handler"):
        validate_durable_event_handlers(handlers)


def test_download_and_transfer_typed_contracts_accept_legacy_runtime_objects() -> None:
    """新增 typed contract 只做诊断，不把插件收到的领域对象替换成 dict。"""
    meta = MetaInfo("Demo.2026.mkv")
    media = MediaInfo(type=MediaType.MOVIE, title="Demo", year="2026")
    context = Context(meta_info=meta, media_info=media)
    fileitem = FileItem(storage="local", path="/downloads/Demo.mkv", type="file")
    transferinfo = TransferInfo(success=True, fileitem=fileitem)

    assert validate_event_payload(
        EventType.DownloadAdded,
        {
            "hash": "hash-1",
            "context": context,
            "downloader": "qb",
            "episodes": [],
        },
    ) == ()
    for event_type in (EventType.TransferComplete, EventType.TransferFailed):
        assert validate_event_payload(
            event_type,
            {
                "fileitem": fileitem,
                "meta": meta,
                "mediainfo": media,
                "transferinfo": transferinfo,
                "transfer_history_id": 1,
            },
        ) == ()


def test_model_instance_remains_mutable_chain_payload() -> None:
    """链式处理器继续接收原 model 实例，确保输出字段可原地接力。"""
    payload = ConfigChangeEventData(key={"PROXY_HOST"})
    event = Event(EventType.ConfigChanged, payload)

    assert event.event_data is payload
