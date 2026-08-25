"""插件 SDK 事件快照访问测试。"""

from app.domain.context import Context, MediaInfo, MusicInfo
from app.runtime.event.snapshot import (
    EventPayloadSnapshot as RuntimeEventPayloadSnapshot,
)
from app.runtime.event.snapshot import snapshot_event_data as runtime_snapshot_event_data
from app.schemas.event import ResourceSelectionEventData
from app.schemas.types import ChainEventType, EventType, MediaType
from app.sdk.events import (
    DownloadAddedContractData,
    Event,
    EventPayloadSnapshot,
    ResourceSelectionInputContractData,
    ResourceSelectionOutputContractData,
    snapshot_event_data,
)


def test_sdk_exports_canonical_snapshot_interfaces() -> None:
    """SDK 只公开 canonical 快照实现，不复制事件解析逻辑。"""
    assert EventPayloadSnapshot is RuntimeEventPayloadSnapshot
    assert snapshot_event_data is runtime_snapshot_event_data


def test_event_snapshot_exposes_music_type_without_changing_raw_payload() -> None:
    """插件可从快照稳定识别音乐，原 Context 和 MusicInfo 继续按旧 ABI 投递。"""
    media = MusicInfo(title="Song", artists=["Artist"], album="Album")
    context = Context(media_info=media)
    raw = {"hash": "music-hash", "context": context}
    event = Event(EventType.DownloadAdded, raw)

    snapshot = event.snapshot()

    assert snapshot.known is True
    assert snapshot.valid is True
    assert snapshot.raw is raw
    assert isinstance(snapshot.payload, DownloadAddedContractData)
    assert snapshot.input is snapshot.payload
    assert snapshot.output is None
    assert snapshot.payload.context.media_info.type == MediaType.MUSIC.value
    assert snapshot.payload.context.media_info.music_type == "recording"
    assert event.event_data["context"] is context

    snapshot.payload.context.media_info.title = "Snapshot Song"
    assert media.title == "Song"


def test_chain_snapshot_separates_plugin_input_and_output_models() -> None:
    """链式事件同时提供输入和回写快照，且不替换可变运行时事件模型。"""
    context = Context(
        media_info=MediaInfo(type=MediaType.MOVIE, title="Movie", year="2026")
    )
    raw = ResourceSelectionEventData(
        contexts=[context],
        updated=True,
        updated_contexts=[context],
        source="plugin",
    )
    event = Event(ChainEventType.ResourceSelection, raw)

    snapshot = event.snapshot()

    assert snapshot.raw is raw
    assert isinstance(snapshot.input, ResourceSelectionInputContractData)
    assert isinstance(snapshot.output, ResourceSelectionOutputContractData)
    assert snapshot.input.contexts[0].media_info.type == MediaType.MOVIE.value
    assert snapshot.output.updated is True
    assert snapshot.output.updated_contexts[0].media_info.title == "Movie"
    assert event.event_data is raw


def test_unknown_plugin_event_returns_unregistered_snapshot_without_error() -> None:
    """插件自定义字符串事件没有宿主契约时保留原 payload，不制造假模型。"""
    raw = {"plugin_owned": {"value": 1}}

    snapshot = snapshot_event_data("plugin.custom.event", raw)

    assert snapshot.known is False
    assert snapshot.valid is False
    assert snapshot.raw is raw
    assert snapshot.payload is None
    assert snapshot.input is None
    assert snapshot.output is None
    assert snapshot.errors == ()


def test_invalid_known_event_returns_validation_errors_instead_of_raising() -> None:
    """坏 payload 由 SDK 结果携带错误，插件可自行决定降级策略。"""
    raw = {"context": {"media_info": {"type": "音乐"}}}

    snapshot = snapshot_event_data(EventType.DownloadAdded, raw)

    assert snapshot.known is True
    assert snapshot.valid is False
    assert snapshot.raw is raw
    assert snapshot.payload is None
    assert snapshot.errors
    assert "DownloadAddedContractData" in snapshot.errors[0]
