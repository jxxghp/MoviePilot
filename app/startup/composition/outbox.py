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
from app.application.messaging.delivery import (
    claim_notification_part,
    freeze_notification_parts,
    merge_notification_progress,
    notification_part_key,
    notification_plan_incomplete,
    notification_rule_time_allows,
    observe_notification_progress,
    pending_notification_parts,
    read_or_freeze_notification_connections,
    read_or_freeze_notification_rules,
    reserve_legacy_notification_dispatch,
    seal_notification_plan,
    uses_legacy_attachment_layout,
)
from app.application.notification import get_notification_configs
from app.application.outbox import (
    CHANNEL_AUTHORIZATION_RECEIPT_TOPIC,
    DOWNLOAD_MODULE_TOPIC,
    DOWNLOAD_NOTIFICATION_TOPIC,
    DOWNLOAD_SUBTITLE_TOPIC,
    DURABLE_EVENT_TOPICS,
    EVENT_HANDLER_TOPIC_PREFIX,
    ClaimedOutboxMessage,
    OutboxDispatcher,
    OutboxIntent,
    OutboxLeaseLostError,
    OutboxRetryDelay,
    durable_event_topic,
    reset_outbox_dispatcher,
    validate_durable_event_handlers,
)
from app.application.security.authorization import get_channel_authorization
from app.command import CommandChain
from app.db.adapters.outbox import SqlAlchemyOutboxDispatchStore
from app.db.session import SessionFactory
from app.runtime.correlation import correlation_scope
from app.runtime.events import EventManager
from app.runtime.observability import record_metric
from app.schemas.common import JsonData
from app.schemas.delivery import DeliveryResult
from app.schemas.message import Message
from app.schemas.types import EventType, NotificationChannel


class _StaleAuthorizationReceipt(Exception):
    """
    回执的审批版本或连接身份在出队后已经失效。
    """


def _dispatch_notification(message: ClaimedOutboxMessage, *, subscription: bool = False) -> None:
    """
    恢复业务通知快照，并在当前认领有效的短事务中保存投递观察结果。
    """
    snapshot = message.payload.get("message") or {}
    if not isinstance(snapshot, dict):
        raise RuntimeError("通知快照格式无效")
    notification = Message.model_validate(snapshot)
    if subscription and notification.subscription_owner_identity is None:
        notification.subscription_owner_identity = ""
    _deliver_outbox_notification(message, notification)


def _deliver_outbox_notification(
    message: ClaimedOutboxMessage, notification: Message, *, revalidate: Callable[[], None] | None = None,
) -> None:
    """
    在当前 Outbox 租约下严格投递已解析的通知。
    """
    store = SqlAlchemyOutboxDispatchStore(SessionFactory)
    candidates = _notification_connection_candidates(notification) if message.attempt == 1 else None
    allowed = store.update_notification_progress(
        message, lambda current: read_or_freeze_notification_connections(current, candidates),
    )
    notification.limit_delivery_connections(allowed)
    if notification.delivery_kind in {None, "event_notification"} and not notification.private_delivery:
        current_rules = {
            conf.id: tuple(rule for rule in conf.notification_rules if notification_rule_time_allows(rule))
            for conf in get_notification_configs()
            if (conf.id, conf.identity_namespace or conf.id) in allowed and conf.notification_rules is not None
        }
        rules = store.update_notification_progress(
            message, lambda current: read_or_freeze_notification_rules(current, current_rules),
        )
    else:
        rules = None
    seen_parts: set[str] = set()
    planned_only = False
    retry_after = 0
    pending, can_prepare, legacy_attachment_layout = store.update_notification_progress(
        message, lambda current: ((
            pending_notification_parts(current), notification_plan_incomplete(current), uses_legacy_attachment_layout(current),
        ), current),
    )

    def record(results: tuple[DeliveryResult, ...]) -> None:
        """
        将实际回执合并进当前通知意图，Outbox 结算仍由派发器持有。
        """
        nonlocal retry_after
        retry_after = max(retry_after, max((
            result.retry_after or 0 for result in results if result.status == "retryable_failure"
        ), default=0))
        store.update_notification_progress(
            message, lambda current: (None, merge_notification_progress(current, results)),
        )

    def reserve(part: DeliveryResult, should_submit: bool) -> DeliveryResult | None:
        """
        发送前取得当前分段的提交资格。
        """
        if should_submit and revalidate is not None:
            revalidate()
        seen_parts.add(notification_part_key(part))
        return store.update_notification_progress(
            message, lambda current: claim_notification_part(
                current, part, initial_attempt=message.attempt == 1, should_submit=should_submit,
                planned_only=planned_only,
            ),
        )

    def plan(parts: tuple[DeliveryResult, ...]) -> None:
        """
        在首次网络提交之前持久化所有已知分段，恢复时不接纳额外目标。
        """
        if message.attempt == 1 and not planned_only:
            store.update_notification_progress(
                message, lambda current: (None, freeze_notification_parts(current, parts)),
            )

    def freeze(parts: tuple[DeliveryResult, ...]) -> None:
        """
        在首个宿主发送前，以单次写入固定本次的全部目标。
        """
        nonlocal pending, planned_only

        def freeze_and_read(current: dict[str, JsonData]) -> tuple[frozenset[str], dict[str, JsonData]]:
            """
            返回本次必须实际派发或保留为未完成的分段。
            """
            updated = seal_notification_plan(current, parts)
            return pending_notification_parts(updated), updated

        pending = store.update_notification_progress(message, freeze_and_read)
        planned_only = True

    def claim_legacy_notice() -> bool:
        """
        先持久化旧派发的认领状态。历史重试可能已经产生插件副作用，直接恢复宿主分段。
        """
        return store.update_notification_progress(message, lambda current: reserve_legacy_notification_dispatch(
            current, allow_first_dispatch=message.attempt == 1 or can_prepare,
        ))

    try:
        with observe_notification_progress(record, reserve=reserve, plan=plan, freeze=freeze if can_prepare else None,
                                           claim_legacy_notice=claim_legacy_notice,
                                           legacy_attachment_layout=legacy_attachment_layout, rules=rules):
            CommandChain().post_message_strict(notification, event_key=message.event_key)
        if pending - seen_parts:
            raise RuntimeError("通知原投递目标已不可用，仍有未确认的分段")
    except RuntimeError as error:
        if retry_after and not isinstance(error, OutboxLeaseLostError):
            raise OutboxRetryDelay(str(error), retry_after) from error
        raise


def _notification_connection_candidates(notification: Message) -> frozenset[tuple[str, str]]:
    """
    冻结当前启用的投递连接，审批回执沿用已验证的唯一连接。

    :param notification: 即将派发的消息
    :return: 可以参与当前通知的连接身份
    """
    if notification.delivery_connection is not None:
        return frozenset((notification.delivery_connection,))
    interaction = notification.delivery_kind in {"reply", "task_result"}
    return frozenset(
        (conf.id, conf.identity_namespace or conf.id)
        for conf in get_notification_configs()
        if (conf.interaction_enabled if interaction else conf.notification_enabled) is not False
    )


def _dispatch_authorization_receipt(message: ClaimedOutboxMessage) -> None:
    """
    核对审批及连接仍有效后私发回执，废弃的回执直接结算。
    """
    service = get_channel_authorization()
    if service is None:
        raise RuntimeError("渠道授权服务尚未就绪")
    payload = message.payload
    target = service.receipt_target(
        payload["member_id"], payload["version"], payload["connection_id"], payload["identity_namespace"],
    )
    if target is None:
        return
    member, source = target
    labels = {
        "approved": "已授权，请重新发送要执行的操作。",
        "rejected": "授权申请未通过，请联系管理员。",
        "disabled": "渠道授权已停用，请联系管理员。",
    }
    notification = Message(
        channel=NotificationChannel(member.channel), source=source, userid=member.sender_id,
        title=labels[member.status], save_history=False, private_delivery=True, delivery_kind="authorization_receipt",
    )
    notification.bind_delivery_connection(payload["connection_id"], payload["identity_namespace"])

    def revalidate() -> None:
        """
        在每次实际提交前确认审批和接收人仍与回执一致。
        """
        current = service.receipt_target(
            payload["member_id"], payload["version"], payload["connection_id"], payload["identity_namespace"],
        )
        if current is None or current[0].sender_id != member.sender_id or current[0].status != member.status:
            raise _StaleAuthorizationReceipt

    try:
        _deliver_outbox_notification(message, notification, revalidate=revalidate)
    except _StaleAuthorizationReceipt:
        return

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
        _dispatch_notification(message, subscription=True)

    def dispatch_subscribe_added_notification(message: ClaimedOutboxMessage) -> None:
        """
        恢复订阅新增通知；恢复使用提交前冻结的渲染消息快照。
        """
        _dispatch_notification(message, subscription=True)

    def dispatch_download_notification(message: ClaimedOutboxMessage) -> None:
        """
        按稳定事件键同步恢复已渲染的下载通知。
        """
        _dispatch_notification(message)

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
        CHANNEL_AUTHORIZATION_RECEIPT_TOPIC: _dispatch_authorization_receipt,
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
