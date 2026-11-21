"""
持久化 Outbox handler 与 dispatcher 的宿主组合装配。
"""

from collections.abc import Callable
from datetime import datetime, timezone
from functools import partial
from hashlib import sha256

from app.adapters.external.server import MoviePilotServerHelper
from app.application.chain.events import (
    restore_download_added,
    restore_download_processing,
    restore_transfer_result,
)
from app.application.outbox import (
    DOWNLOAD_MODULE_TOPIC,
    DOWNLOAD_NOTIFICATION_TOPIC,
    DOWNLOAD_SUBTITLE_TOPIC,
    DURABLE_EVENT_TOPICS,
    EVENT_HANDLER_TOPIC_PREFIX,
    ClaimedOutboxMessage,
    OutboxDispatcher,
    OutboxIntent,
    durable_event_topic,
    reset_outbox_dispatcher,
    validate_durable_event_handlers,
)
from app.command import CommandChain
from app.db.adapters.outbox import SqlAlchemyOutboxDispatchStore
from app.db.session import SessionFactory
from app.runtime.correlation import correlation_scope
from app.runtime.events import EventManager
from app.runtime.observability import record_metric
from app.schemas.message import Message
from app.schemas.types import EventType


def _stage_event_handlers(event_type: EventType, message: ClaimedOutboxMessage) -> None:
    """
    冻结启用订阅并持久化逐处理器投递，避免第三方调用阻塞父主题的恢复。
    """
    intents = []
    for handler_id in EventManager().broadcast_handler_ids(event_type):
        handler_digest = sha256(handler_id.encode()).hexdigest()
        delivery_digest = sha256(f"{message.event_key}\0{handler_id}".encode()).hexdigest()
        intents.append(OutboxIntent(
            event_key=f"event.handler:{delivery_digest}",
            topic=f"{EVENT_HANDLER_TOPIC_PREFIX}{handler_digest}",
            payload={
                "event_type": event_type.value,
                "event_key": message.event_key,
                "handler_id": handler_id,
                "event_payload": message.payload,
            },
        ))
    SqlAlchemyOutboxDispatchStore(SessionFactory).stage_fanout(
        message, tuple(intents), datetime.now(timezone.utc),
    )


def build_outbox_handlers() -> dict[
    str,
    Callable[[ClaimedOutboxMessage], None],
]:
    """
    构造持久事件展开、通知和统计 handler，事件处理器由独立消息等待真实结算。
    """
    def dispatch_subscribe_deleted_report(message: ClaimedOutboxMessage) -> None:
        """
        重放订阅删除统计；未确认时抛错以进入有限重试。
        """
        if not MoviePilotServerHelper.sub_done_durable(message.payload.get("subscribe_info") or {}):
            raise RuntimeError("订阅删除统计上报未确认")

    def dispatch_subscribe_added_report(message: ClaimedOutboxMessage) -> None:
        """
        重放订阅新增统计；未确认时抛错以进入有限重试。
        """
        if not MoviePilotServerHelper.sub_reg_durable(message.payload.get("subscribe_info") or {}):
            raise RuntimeError("订阅新增统计上报未确认")

    def dispatch_subscribe_complete_report(message: ClaimedOutboxMessage) -> None:
        """
        重放订阅完成统计；未确认时抛错以进入有限重试。
        """
        if not MoviePilotServerHelper.sub_done_durable(message.payload.get("subscribe_info") or {}):
            raise RuntimeError("订阅完成统计上报未确认")

    def dispatch_subscribe_notification(message: ClaimedOutboxMessage) -> None:
        """
        恢复订阅完成通知；消息快照无需重建领域对象。
        """
        snapshot = message.payload.get("message") or {}
        if not isinstance(snapshot, dict):
            raise RuntimeError("订阅完成通知快照格式无效")
        CommandChain().post_message_strict(
            Message.model_validate(snapshot),
            event_key=message.event_key,
        )

    def dispatch_subscribe_added_notification(message: ClaimedOutboxMessage) -> None:
        """
        恢复订阅新增通知；恢复使用提交前冻结的渲染消息快照。
        """
        snapshot = message.payload.get("message") or {}
        if not isinstance(snapshot, dict):
            raise RuntimeError("订阅新增通知快照格式无效")
        CommandChain().post_message_strict(
            Message.model_validate(snapshot),
            event_key=message.event_key,
        )

    def dispatch_download_notification(message: ClaimedOutboxMessage) -> None:
        """
        按稳定事件键同步恢复已渲染的下载通知。
        """
        snapshot = message.payload.get("message") or {}
        if not isinstance(snapshot, dict):
            raise RuntimeError("下载通知快照格式无效")
        CommandChain().post_message_strict(
            Message.model_validate(snapshot),
            event_key=message.event_key,
        )

    def dispatch_download_module(message: ClaimedOutboxMessage) -> None:
        """
        恢复模块下载后处理，并向模块边界传播稳定幂等键。
        """
        from app.chain.download import DownloadChain

        snapshot = restore_download_processing(message.payload)
        with correlation_scope(message.event_key):
            DownloadChain().download_added(
                context=snapshot.context,
                download_dir=snapshot.download_dir,
                torrent_content=snapshot.torrent_content,
            )

    def dispatch_download_subtitle(message: ClaimedOutboxMessage) -> None:
        """
        恢复站点字幕处理，避免模块成功后因字幕失败重复执行模块。
        """
        from app.chain.download import DownloadChain

        snapshot = restore_download_processing(message.payload)
        with correlation_scope(message.event_key):
            DownloadChain().download_site_subtitles(
                context=snapshot.context,
                download_dir=snapshot.download_dir,
                torrent_content=snapshot.torrent_content,
                download_hash=snapshot.download_hash,
                downloader=snapshot.downloader,
            )

    handlers: dict[str, Callable[[ClaimedOutboxMessage], None]] = {
        durable_event_topic(event_type): partial(_stage_event_handlers, event_type)
        for event_type in DURABLE_EVENT_TOPICS
    }
    handlers.update({
        "subscribe.added.report": dispatch_subscribe_added_report,
        "subscribe.added.notification": dispatch_subscribe_added_notification,
        "subscribe.deleted.report": dispatch_subscribe_deleted_report,
        "subscribe.complete.report": dispatch_subscribe_complete_report,
        "subscribe.complete.notification": dispatch_subscribe_notification,
        DOWNLOAD_NOTIFICATION_TOPIC: dispatch_download_notification,
        DOWNLOAD_MODULE_TOPIC: dispatch_download_module,
        DOWNLOAD_SUBTITLE_TOPIC: dispatch_download_subtitle,
    })
    validate_durable_event_handlers(handlers)
    return handlers


def dispatch_event_handler(message: ClaimedOutboxMessage) -> None:
    """
    恢复一个处理器的事件对象并等待真实完成，失败只重试该处理器的投递记录。

    保留父事件的幂等键与关联上下文，子消息身份仅用于隔离租约及结算。
    不取消仍在执行的第三方代码，关闭与续租继续由 OutboxRecovery 持有。
    """
    if not message.topic.startswith(EVENT_HANDLER_TOPIC_PREFIX):
        raise ValueError(f"未登记的 Outbox 主题：{message.topic}")
    event_type = EventType(message.payload["event_type"])
    if event_type not in DURABLE_EVENT_TOPICS:
        raise ValueError(f"未登记的持久事件：{event_type.value}")
    payload = message.payload["event_payload"]
    if event_type == EventType.DownloadAdded:
        payload = restore_download_added(payload)
    elif event_type in {
        EventType.TransferComplete, EventType.TransferFailed,
        EventType.SubtitleTransferComplete, EventType.SubtitleTransferFailed,
        EventType.AudioTransferComplete, EventType.AudioTransferFailed,
    }:
        payload = restore_transfer_result(payload)
    with correlation_scope(message.payload["event_key"]):
        manager = EventManager()
        manager.send_event_strict(
            event_type, payload, handler_id=message.payload["handler_id"],
        )


def build_outbox_dispatcher() -> OutboxDispatcher:
    """
    创建使用独立短事务和 attempt fencing 的恢复 dispatcher。
    """
    return OutboxDispatcher(
        repository=SqlAlchemyOutboxDispatchStore(SessionFactory),
        handlers=build_outbox_handlers(),
        fallback_handler=dispatch_event_handler,
        failure_observer=lambda dead: record_metric(
            "scheduler.job.dead_letter" if dead else "scheduler.job.retry",
            owner="outbox",
        ),
    )


def reset_outbox_services() -> None:
    """
    撤销当前 lifespan 发布的 Outbox dispatcher 工厂。
    """
    reset_outbox_dispatcher()
