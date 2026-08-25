"""插件事件订阅与发布接口。"""

from app.runtime.event.snapshot import EventPayloadSnapshot, snapshot_event_data
from app.runtime.events import Event, EventManager, eventmanager
from app.schemas.event import (
    ContextSnapshot,
    DownloadAddedContractData,
    FileContextSnapshot,
    MediaSnapshot,
    MetadataScrapeContractData,
    MetaSnapshot,
    ResourceDownloadContractData,
    ResourceDownloadInputContractData,
    ResourceDownloadOutputContractData,
    ResourceSelectionContractData,
    ResourceSelectionInputContractData,
    ResourceSelectionOutputContractData,
    SubscribeCompletionCheckContractData,
    SubscribeCompletionCheckInputContractData,
    SubscribeCompletionCheckOutputContractData,
    TransferResultContractData,
)

__all__ = [
    "ContextSnapshot",
    "DownloadAddedContractData",
    "Event",
    "EventManager",
    "EventPayloadSnapshot",
    "FileContextSnapshot",
    "MediaSnapshot",
    "MetadataScrapeContractData",
    "MetaSnapshot",
    "ResourceDownloadContractData",
    "ResourceDownloadInputContractData",
    "ResourceDownloadOutputContractData",
    "ResourceSelectionContractData",
    "ResourceSelectionInputContractData",
    "ResourceSelectionOutputContractData",
    "SubscribeCompletionCheckContractData",
    "SubscribeCompletionCheckInputContractData",
    "SubscribeCompletionCheckOutputContractData",
    "TransferResultContractData",
    "eventmanager",
    "snapshot_event_data",
]
