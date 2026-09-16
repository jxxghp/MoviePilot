"""持久化 Outbox handler 与 dispatcher 的宿主组合装配。"""

from collections.abc import Callable

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
    ClaimedOutboxMessage,
    OutboxDispatcher,
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


def build_outbox_handlers() -> dict[
    str,
    Callable[[ClaimedOutboxMessage], None],
]:
    """构造等待真实执行边界的 at-least-once 通知、事件和统计 handler。"""
    event_manager_factory: Callable[[], EventManager] = EventManager

    def discard_event_receipt(_event: object) -> None:
        """丢弃普通事件 API 的回执，使 outbox handler 仅表达结算成功。"""

    def dispatch_subscribe_deleted_report(message: ClaimedOutboxMessage) -> None:
        """重放订阅删除统计；未确认时抛错以进入有限重试。"""
        if not MoviePilotServerHelper.sub_done_durable(message.payload.get("subscribe_info") or {}):
            raise RuntimeError("订阅删除统计上报未确认")

    def dispatch_subscribe_added_report(message: ClaimedOutboxMessage) -> None:
        """重放订阅新增统计；未确认时抛错以进入有限重试。"""
        if not MoviePilotServerHelper.sub_reg_durable(message.payload.get("subscribe_info") or {}):
            raise RuntimeError("订阅新增统计上报未确认")

    def dispatch_subscribe_complete_report(message: ClaimedOutboxMessage) -> None:
        """重放订阅完成统计；未确认时抛错以进入有限重试。"""
        if not MoviePilotServerHelper.sub_done_durable(message.payload.get("subscribe_info") or {}):
            raise RuntimeError("订阅完成统计上报未确认")

    def dispatch_subscribe_notification(message: ClaimedOutboxMessage) -> None:
        """恢复订阅完成通知；消息快照无需重建领域对象。"""
        snapshot = message.payload.get("message") or {}
        if not isinstance(snapshot, dict):
            raise RuntimeError("订阅完成通知快照格式无效")
        CommandChain().post_message_strict(
            Message.model_validate(snapshot),
            event_key=message.event_key,
        )

    def dispatch_subscribe_added_notification(message: ClaimedOutboxMessage) -> None:
        """恢复订阅新增通知；恢复使用提交前冻结的渲染消息快照。"""
        snapshot = message.payload.get("message") or {}
        if not isinstance(snapshot, dict):
            raise RuntimeError("订阅新增通知快照格式无效")
        CommandChain().post_message_strict(
            Message.model_validate(snapshot),
            event_key=message.event_key,
        )

    def dispatch_download_notification(message: ClaimedOutboxMessage) -> None:
        """按稳定事件键同步恢复已渲染的下载通知。"""
        snapshot = message.payload.get("message") or {}
        if not isinstance(snapshot, dict):
            raise RuntimeError("下载通知快照格式无效")
        CommandChain().post_message_strict(
            Message.model_validate(snapshot),
            event_key=message.event_key,
        )

    def dispatch_download_module(message: ClaimedOutboxMessage) -> None:
        """恢复模块下载后处理，并向模块边界传播稳定幂等键。"""
        from app.chain.download import DownloadChain

        snapshot = restore_download_processing(message.payload)
        with correlation_scope(message.event_key):
            DownloadChain().download_added(
                context=snapshot.context,
                download_dir=snapshot.download_dir,
                torrent_content=snapshot.torrent_content,
            )

    def dispatch_download_subtitle(message: ClaimedOutboxMessage) -> None:
        """恢复站点字幕处理，避免模块成功后因字幕失败重复执行模块。"""
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
        durable_event_topic(EventType.SubscribeAdded): lambda message: discard_event_receipt(
            event_manager_factory().send_event_strict(
                EventType.SubscribeAdded,
                message.payload,
            )
        ),
        "subscribe.added.report": dispatch_subscribe_added_report,
        "subscribe.added.notification": dispatch_subscribe_added_notification,
        durable_event_topic(EventType.SubscribeModified): lambda message: discard_event_receipt(
            event_manager_factory().send_event_strict(
                EventType.SubscribeModified,
                message.payload,
            )
        ),
        durable_event_topic(EventType.SubscribeDeleted): lambda message: discard_event_receipt(
            event_manager_factory().send_event_strict(
                EventType.SubscribeDeleted,
                message.payload,
            )
        ),
        "subscribe.deleted.report": dispatch_subscribe_deleted_report,
        durable_event_topic(EventType.SubscribeComplete): lambda message: discard_event_receipt(
            event_manager_factory().send_event_strict(
                EventType.SubscribeComplete,
                message.payload,
            )
        ),
        "subscribe.complete.report": dispatch_subscribe_complete_report,
        "subscribe.complete.notification": dispatch_subscribe_notification,
        durable_event_topic(EventType.DownloadAdded): lambda message: discard_event_receipt(
            event_manager_factory().send_event_strict(
                EventType.DownloadAdded,
                restore_download_added(message.payload),
            )
        ),
        DOWNLOAD_NOTIFICATION_TOPIC: dispatch_download_notification,
        DOWNLOAD_MODULE_TOPIC: dispatch_download_module,
        DOWNLOAD_SUBTITLE_TOPIC: dispatch_download_subtitle,
        durable_event_topic(EventType.TransferComplete): lambda message: discard_event_receipt(
            event_manager_factory().send_event_strict(
                EventType.TransferComplete,
                restore_transfer_result(message.payload),
            )
        ),
        durable_event_topic(EventType.TransferFailed): lambda message: discard_event_receipt(
            event_manager_factory().send_event_strict(
                EventType.TransferFailed,
                restore_transfer_result(message.payload),
            )
        ),
        durable_event_topic(EventType.SubtitleTransferComplete): lambda message: discard_event_receipt(
            event_manager_factory().send_event_strict(
                EventType.SubtitleTransferComplete,
                restore_transfer_result(message.payload),
            )
        ),
        durable_event_topic(EventType.SubtitleTransferFailed): lambda message: discard_event_receipt(
            event_manager_factory().send_event_strict(
                EventType.SubtitleTransferFailed,
                restore_transfer_result(message.payload),
            )
        ),
        durable_event_topic(EventType.AudioTransferComplete): lambda message: discard_event_receipt(
            event_manager_factory().send_event_strict(
                EventType.AudioTransferComplete,
                restore_transfer_result(message.payload),
            )
        ),
        durable_event_topic(EventType.AudioTransferFailed): lambda message: discard_event_receipt(
            event_manager_factory().send_event_strict(
                EventType.AudioTransferFailed,
                restore_transfer_result(message.payload),
            )
        ),
    }
    validate_durable_event_handlers(handlers)
    return handlers


def build_outbox_dispatcher() -> OutboxDispatcher:
    """创建使用独立短事务和 attempt fencing 的恢复 dispatcher。"""
    return OutboxDispatcher(
        repository=SqlAlchemyOutboxDispatchStore(SessionFactory),
        handlers=build_outbox_handlers(),
        failure_observer=lambda dead: record_metric(
            "scheduler.job.dead_letter" if dead else "scheduler.job.retry",
            owner="outbox",
        ),
    )


def reset_outbox_services() -> None:
    """撤销当前 lifespan 发布的 Outbox dispatcher 工厂。"""
    reset_outbox_dispatcher()
