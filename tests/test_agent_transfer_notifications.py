"""
自动整理接管的统一推送策略、真实状态判定和持久回执去重回归。
"""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from app.application.history import TransferHistorySnapshot
from app.application.notification import agent_transfer_notification_key, apply_notification_policy
from app.application.outbox import (
    AGENT_TRANSFER_NOTIFICATION_TOPIC,
    ClaimedOutboxMessage,
    OutboxDispatcher,
    OutboxIntent,
)
from app.application.settings.contract import SYSTEMCONFIG_CONTRACTS
from app.application.transfer.execution import TransferExecutionState, TransferSettlementSnapshot, TransferTerminalState
from app.application.transfer.recovery import ai_takeover_result
from app.chain import _messaging
from app.chain._messaging import MessageProcessingMixin, NotificationMixin
from app.chain.transfer import retry
from app.chain.transfer.facade import TransferChain
from app.db.adapters.chain import TransactionalChainDurableEventWriter
from app.db.adapters.outbox import SqlAlchemyAsyncOutboxStager, SqlAlchemyOutboxDispatchStore, SqlAlchemyOutboxStager
from app.db.models.outbox import OutboxMessage
from app.schemas.message import Message, MessageNotificationContext
from app.schemas.system import NotificationPoliciesConf
from app.schemas.types import MessageType, NotificationChannel, SystemConfigKey
from app.startup.composition import outbox as outbox_composition


class _MessageHarness(MessageProcessingMixin, NotificationMixin):
    """
    保留真实消息渲染和路由，隔离数据库历史、插件事件与外部渠道。
    """

    def __init__(self) -> None:
        """
        为同步、异步和严格投递提供可断言的无网络边界。
        """
        self.runtime_config = SimpleNamespace(superuser="admin")
        self.user_repository = Mock()
        self.user_repository.get_notification_settings.return_value = {}
        self.user_repository.async_get_notification_settings = AsyncMock(return_value={})
        self.messageoper = SimpleNamespace(
            add=Mock(), async_add=AsyncMock(), exists_by_source=Mock(return_value=False),
        )
        self.eventmanager = SimpleNamespace(send_event=Mock(), async_send_event=AsyncMock())
        self.messagequeue = SimpleNamespace(send_message=Mock(), async_send_message=AsyncMock())
        self.run_module_strict = Mock()


@pytest.fixture
def policy(monkeypatch):
    """
    提供可在恢复前切换的配置快照，不修改实际系统配置。

    :param monkeypatch: 隔离配置服务和通知接收范围的夹具
    :return: 保存当前策略的可变测试字典
    """
    value = {"agent_transfer_takeover": "all"}
    configuration = Mock()
    configuration.get.side_effect = lambda _key: dict(value)
    monkeypatch.setattr("app.application.notification.get_configured_system_config", lambda: configuration)
    monkeypatch.setattr(_messaging, "get_notification_switch", lambda _type: "admin")
    return value


@pytest.fixture
def outbox_store(tmp_path):
    """
    用独立 SQLite 文件验证跨实例回执，不读取或写入生产数据库。

    :param tmp_path: pytest 提供的独立临时目录
    :yields: 独占会话工厂及真实持久事件写入器
    """
    engine = create_engine(f"sqlite:///{tmp_path / 'notifications.db'}")
    OutboxMessage.__table__.create(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    async_engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'notifications.db'}", poolclass=NullPool)
    async_factory = async_sessionmaker(async_engine, expire_on_commit=False)
    try:
        yield factory, TransactionalChainDurableEventWriter(factory, async_factory)
    finally:
        asyncio.run(async_engine.dispose())
        engine.dispose()


def _message(stage="attention", *, trigger="automatic", revision=1):
    """
    构造带宿主任务身份的示例消息，完整报告与外部摘要刻意不同。
    """
    return Message(
        mtype=MessageType.Agent, title="智能助手分析结果", text="模型完整分析报告，声称已经成功",
        link="https://example.invalid/#/history", save_history=True,
        notification_context=MessageNotificationContext(
            scenario="agent.transfer_takeover", trigger=trigger, stage=stage,
            task_id="task-fixture", settlement_revision=revision, history_id=11,
            summary="任务仍需人工核对，请查看整理历史。",
        ),
    )


@pytest.mark.parametrize("mode", ["all", "attention", "off"])
@pytest.mark.parametrize("stage", ["started", "queued", "resolved", "attention", "unknown"])
@pytest.mark.parametrize("entry", ["sync", "async", "strict"])
def test_policy_precedes_both_channel_and_plugin_dispatch(policy, mode, stage, entry):
    """
    三种入口使用同一策略，关闭时既不发送宿主渠道，也不泄漏 NoticeMessage 给 Bark。
    """
    policy["agent_transfer_takeover"] = mode
    harness = _MessageHarness()
    message = _message(stage)
    expected = mode == "all" or (mode == "attention" and stage in {"attention", "unknown"})
    if entry == "async":
        asyncio.run(harness.async_post_message(message))
        events = harness.eventmanager.async_send_event
        delivery = harness.messagequeue.async_send_message
        history = harness.messageoper.async_add
    elif entry == "strict":
        harness.post_message_strict(message, event_key="fixture-event")
        events = harness.eventmanager.send_event
        delivery = harness.run_module_strict
        history = harness.messageoper.add
    else:
        harness.post_message(message)
        events = harness.eventmanager.send_event
        delivery = harness.messagequeue.send_message
        history = harness.messageoper.add
    assert events.call_count == delivery.call_count == int(expected)
    assert history.call_count == 1
    assert history.call_args.kwargs["text"] == message.text
    assert "notification_context" not in history.call_args.kwargs
    if expected:
        payload = events.call_args.kwargs["data"]
        assert payload["type"] is MessageType.Agent
        assert "notification_context" not in payload
        assert payload["text"] == (message.notification_context.summary if mode == "attention" else message.text)


@pytest.mark.parametrize("message_type", [MessageType.Agent, MessageType.Organize, MessageType.Manual])
def test_unrelated_messages_and_explicit_manual_replies_are_not_muted(policy, message_type):
    """
    其他智能体任务、普通入库和失败通知不受该场景影响，空渠道不被误认作自动来源。
    """
    policy["agent_transfer_takeover"] = "off"
    message = Message(mtype=message_type, title="普通消息", channel=None, userid=None)
    assert apply_notification_policy(message) is message
    manual = _message(trigger="manual")
    assert apply_notification_policy(manual) is manual


def test_missing_policy_preserves_all_and_schema_rejects_invalid_modes(policy):
    """
    未配置时保持全部通知，非法策略在配置边界被拒绝。
    """
    policy.clear()
    message = _message("started")
    assert apply_notification_policy(message) is message
    assert NotificationPoliciesConf().agent_transfer_takeover == "all"
    assert SystemConfigKey.NotificationPolicies in SYSTEMCONFIG_CONTRACTS
    with pytest.raises(ValidationError):
        NotificationPoliciesConf(agent_transfer_takeover="invalid")


def test_identity_is_stable_but_new_settlement_and_stage_are_distinct():
    """
    文本变化不生成新回执，新的结算版本和阶段仍可独立投递。
    """
    first = _message()
    changed = first.model_copy(update={"title": "另一个标题", "text": "不同模型文字"})
    assert agent_transfer_notification_key(first.notification_context) == agent_transfer_notification_key(changed.notification_context)
    assert agent_transfer_notification_key(first.notification_context) != agent_transfer_notification_key(_message(revision=2).notification_context)
    assert agent_transfer_notification_key(first.notification_context) != agent_transfer_notification_key(_message("started").notification_context)


def test_durable_notification_deduplicates_after_restart_and_preserves_new_revision(outbox_store):
    """
    新写入器模拟重启后仍复用旧回执，新的失败结算版本不会被屏蔽。
    """
    factory, writer = outbox_store
    publish = Mock()
    assert writer.agent_transfer_notification(message=_message(), publish=publish)
    restarted = TransactionalChainDurableEventWriter(factory)
    assert not restarted.agent_transfer_notification(message=_message(), publish=publish)
    assert restarted.agent_transfer_notification(message=_message(revision=2), publish=publish)
    assert publish.call_count == 2
    with factory() as session:
        rows = session.scalars(select(OutboxMessage)).all()
        assert len(rows) == 2
        assert all(row.status == "completed" for row in rows)
        assert rows[0].payload["notification_context"]["trigger"] == "automatic"


def test_notification_failure_is_recovered_using_current_policy(outbox_store, policy, monkeypatch):
    """
    发送失败不伪造完成；重启恢复使用当前关闭策略，保留完整历史但不补发外部消息。
    """
    factory, writer = outbox_store
    assert writer.agent_transfer_notification(message=_message(), publish=Mock(side_effect=RuntimeError("offline")))
    with factory() as session:
        assert session.scalars(select(OutboxMessage)).one().status == "pending"
    harness = _MessageHarness()
    monkeypatch.setattr(outbox_composition, "CommandChain", lambda: harness)
    policy["agent_transfer_takeover"] = "off"
    dispatcher = OutboxDispatcher(SqlAlchemyOutboxDispatchStore(factory), outbox_composition.build_outbox_handlers())
    assert dispatcher.dispatch_one()
    harness.messageoper.add.assert_called_once()
    harness.eventmanager.send_event.assert_not_called()
    harness.run_module_strict.assert_not_called()
    with factory() as session:
        assert session.scalars(select(OutboxMessage)).one().status == "completed"
    policy["agent_transfer_takeover"] = "all"
    assert not dispatcher.dispatch_one()


def test_concurrent_notifications_share_one_durable_identity(outbox_store):
    """
    并发登记受到数据库唯一约束保护，不依赖进程内标题缓存。
    """
    _factory, writer = outbox_store
    publish = Mock()
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(writer.agent_transfer_notification, message=_message(), publish=publish) for _ in range(2)]
        assert sorted(future.result(timeout=10) for future in futures) == [False, True]
    publish.assert_called_once()


def test_missing_automatic_context_cannot_enter_durable_notification(outbox_store):
    """
    手动回复和缺少上下文的旧消息不能被持久自动通知通道接纳。
    """
    _factory, writer = outbox_store
    with pytest.raises(ValueError, match="自动任务身份"):
        writer.agent_transfer_notification(message=Message(title="手动回复"), publish=Mock())


@pytest.mark.parametrize("winner_committed", [True, False], ids=["duplicate", "other-constraint"])
def test_sync_registration_only_deduplicates_verified_unique_conflicts(
        outbox_store: tuple[sessionmaker, TransactionalChainDurableEventWriter],
        monkeypatch: pytest.MonkeyPatch, winner_committed: bool,
) -> None:
    """
    回滚后仅在同一通知确已落库时去重，其他完整性错误必须原样传播。

    :param outbox_store: 独立临时库与真实事务写入器
    :param monkeypatch: 只在暂存边界注入确定性的数据库冲突
    :param winner_committed: 是否模拟另一个事务已提交同一通知
    """
    factory, writer = outbox_store
    original_stage = SqlAlchemyOutboxStager.stage
    error = IntegrityError("INSERT", {}, RuntimeError("测试约束冲突"))
    publish = Mock()

    def inject_conflict(_stager: SqlAlchemyOutboxStager, intent: OutboxIntent, now: datetime) -> None:
        """
        在首次查询后提交竞争事务，再让当前暂存收到完整性错误。

        :param _stager: 当前事务的暂存器，不使用其会话制造竞争记录
        :param intent: 真实写入器构造的稳定通知意图
        :param now: 写入器传入的登记时间
        :raises IntegrityError: 模拟当前事务被数据库拒绝
        """
        if winner_committed:
            with factory() as winner:
                original_stage(SqlAlchemyOutboxStager(winner), intent, now)
                winner.commit()
        raise error

    monkeypatch.setattr(SqlAlchemyOutboxStager, "stage", inject_conflict)
    if winner_committed:
        assert not writer.agent_transfer_notification(message=_message(), publish=publish)
    else:
        with pytest.raises(IntegrityError) as caught:
            writer.agent_transfer_notification(message=_message(), publish=publish)
        assert caught.value is error
    publish.assert_not_called()
    with factory() as session:
        rows = session.scalars(select(OutboxMessage)).all()
        assert len(rows) == int(winner_committed)
        if rows:
            assert rows[0].status == "pending" and rows[0].attempt == 0
            assert rows[0].event_key == agent_transfer_notification_key(_message().notification_context)


@pytest.mark.parametrize("winner_committed", [True, False], ids=["duplicate", "other-constraint"])
def test_async_registration_only_deduplicates_verified_unique_conflicts(
        outbox_store: tuple[sessionmaker, TransactionalChainDurableEventWriter],
        monkeypatch: pytest.MonkeyPatch, winner_committed: bool,
) -> None:
    """
    异步回滚同样核实唯一身份，不将无关约束失败当作重复或成功。

    :param outbox_store: 独立临时库与配置异步会话工厂的写入器
    :param monkeypatch: 在异步暂存边界注入竞争事务和约束失败
    :param winner_committed: 是否存在真正提交成功的竞争通知
    """
    factory, writer = outbox_store
    async_factory = writer._async_session_factory
    assert async_factory is not None
    original_stage = SqlAlchemyAsyncOutboxStager.stage
    error = IntegrityError("INSERT", {}, RuntimeError("测试约束冲突"))

    async def inject_conflict(_stager: SqlAlchemyAsyncOutboxStager, intent: OutboxIntent, now: datetime) -> None:
        """
        用另一个真实异步短事务登记竞争记录，再拒绝当前事务。

        :param _stager: 被拒绝事务的暂存器，不复用其会话
        :param intent: 写入器生成的完整通知意图
        :param now: 当前登记时间
        :raises IntegrityError: 模拟唯一键或其他约束失败
        """
        if winner_committed:
            async with async_factory() as winner:
                await original_stage(SqlAlchemyAsyncOutboxStager(winner), intent, now)
                await winner.commit()
        raise error

    monkeypatch.setattr(SqlAlchemyAsyncOutboxStager, "stage", inject_conflict)
    if winner_committed:
        assert not asyncio.run(writer.async_agent_transfer_notification(message=_message()))
    else:
        with pytest.raises(IntegrityError) as caught:
            asyncio.run(writer.async_agent_transfer_notification(message=_message()))
        assert caught.value is error
    with factory() as session:
        rows = session.scalars(select(OutboxMessage)).all()
        assert len(rows) == int(winner_committed)
        if rows:
            assert rows[0].status == "pending" and rows[0].attempt == 0
            assert rows[0].event_key == agent_transfer_notification_key(_message().notification_context)


def test_async_registration_requires_explicit_session_composition() -> None:
    """
    异步工厂未装配时明确报错，不悄悄使用同步会话或丢弃通知。
    """
    sync_factory = Mock()
    writer = TransactionalChainDurableEventWriter(sync_factory)
    with pytest.raises(RuntimeError, match="未装配异步 Session 工厂"):
        asyncio.run(writer.async_agent_transfer_notification(message=_message()))
    sync_factory.assert_not_called()


@pytest.mark.parametrize("entry", ["sync", "async"])
def test_explicit_manual_reply_cannot_enter_automatic_outbox(
        outbox_store: tuple[sessionmaker, TransactionalChainDurableEventWriter], entry: str,
) -> None:
    """
    手动来源即使具备完整上下文，也不能进入自动通知的持久登记通道。

    :param outbox_store: 真实临时 Outbox 与同步、异步写入器
    :param entry: 当前验证的同步或异步入口
    """
    factory, writer = outbox_store
    publish = Mock()
    with pytest.raises(ValueError, match="自动任务身份"):
        if entry == "sync":
            writer.agent_transfer_notification(message=_message(trigger="manual"), publish=publish)
        else:
            asyncio.run(writer.async_agent_transfer_notification(message=_message(trigger="manual")))
    publish.assert_not_called()
    with factory() as session:
        assert session.scalars(select(OutboxMessage)).all() == []


@pytest.fixture
def notification_outbox_message() -> ClaimedOutboxMessage:
    """
    提供提交前冻结的消息和独立策略上下文，不接触外部渠道。

    :return: 已认领的自动接管通知投影
    """
    message = _message()
    return ClaimedOutboxMessage(
        message_id=1, event_key=agent_transfer_notification_key(message.notification_context),
        topic=AGENT_TRANSFER_NOTIFICATION_TOPIC, payload_version=1, attempt=1,
        payload={"message": message.model_dump(mode="json"),
                 "notification_context": message.notification_context.model_dump(mode="json")},
    )


def test_notification_restore_rejects_unknown_payload_version(
        notification_outbox_message: ClaimedOutboxMessage, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    未知快照版本先拒绝，不调用通知对象或降级成普通广播。

    :param notification_outbox_message: 合法自动通知快照
    :param monkeypatch: 隔离真实通知对象构造
    """
    command_factory = Mock()
    monkeypatch.setattr(outbox_composition, "CommandChain", command_factory)
    handler = outbox_composition.build_outbox_handlers()[AGENT_TRANSFER_NOTIFICATION_TOPIC]
    with pytest.raises(ValueError, match="快照版本不受支持"):
        handler(replace(notification_outbox_message, payload_version=2))
    command_factory.assert_not_called()


@pytest.mark.parametrize("field", ["message", "notification_context"])
@pytest.mark.parametrize("shape", ["missing", "null", "array", "string"])
def test_notification_restore_rejects_corrupt_snapshot_parts(
        notification_outbox_message: ClaimedOutboxMessage, monkeypatch: pytest.MonkeyPatch,
        field: str, shape: str,
) -> None:
    """
    缺失或类型损坏的消息与上下文均先拒绝，不能绕过场景策略投递。

    :param notification_outbox_message: 用于构造损坏快照的合法投影
    :param monkeypatch: 监视是否错误创建真实通知对象
    :param field: 当前损坏的消息或上下文字段
    :param shape: 缺失、空值、数组或字符串等持久数据形态
    """
    payload = dict(notification_outbox_message.payload)
    if shape == "missing":
        payload.pop(field)
    else:
        payload[field] = {"null": None, "array": [], "string": "损坏快照"}[shape]
    command_factory = Mock()
    monkeypatch.setattr(outbox_composition, "CommandChain", command_factory)
    handler = outbox_composition.build_outbox_handlers()[AGENT_TRANSFER_NOTIFICATION_TOPIC]
    with pytest.raises(ValueError, match="缺少消息或策略上下文"):
        handler(replace(notification_outbox_message, payload=payload))
    command_factory.assert_not_called()


@pytest.mark.parametrize("trigger,error_type", [("manual", ValueError), ("invalid", ValidationError)])
def test_notification_restore_rejects_nonautomatic_context(
        notification_outbox_message: ClaimedOutboxMessage, monkeypatch: pytest.MonkeyPatch,
        trigger: str, error_type: type[ValueError],
) -> None:
    """
    手动和非法触发来源不能通过伪装快照进入自动通知恢复流程。

    :param notification_outbox_message: 其他字段合法的自动通知快照
    :param monkeypatch: 隔离通知对象构造
    :param trigger: 被篡改的触发来源
    :param error_type: 模型或宿主边界应抛出的校验错误
    """
    payload = {**notification_outbox_message.payload,
               "notification_context": {**notification_outbox_message.payload["notification_context"], "trigger": trigger}}
    command_factory = Mock()
    monkeypatch.setattr(outbox_composition, "CommandChain", command_factory)
    handler = outbox_composition.build_outbox_handlers()[AGENT_TRANSFER_NOTIFICATION_TOPIC]
    with pytest.raises(error_type):
        handler(replace(notification_outbox_message, payload=payload))
    command_factory.assert_not_called()


@pytest.mark.parametrize("delivery_fails", [False, True])
def test_notification_restore_preserves_context_and_delivery_failure(
        notification_outbox_message: ClaimedOutboxMessage, monkeypatch: pytest.MonkeyPatch,
        delivery_fails: bool,
) -> None:
    """
    恢复保留完整策略身份和稳定事件键，严格投递失败仍向 Outbox 传播。

    :param notification_outbox_message: 包含完整分析与独立摘要的合法通知快照
    :param monkeypatch: 只替换实际渠道投递边界
    :param delivery_fails: 是否模拟提供方拒绝投递
    """
    command = Mock()
    error = RuntimeError("模拟渠道拒绝")
    if delivery_fails:
        command.post_message_strict.side_effect = error
    monkeypatch.setattr(outbox_composition, "CommandChain", lambda: command)
    handler = outbox_composition.build_outbox_handlers()[AGENT_TRANSFER_NOTIFICATION_TOPIC]
    if delivery_fails:
        with pytest.raises(RuntimeError) as caught:
            handler(notification_outbox_message)
        assert caught.value is error
    else:
        handler(notification_outbox_message)
    command.post_message_strict.assert_called_once()
    restored = command.post_message_strict.call_args.args[0]
    assert restored.model_dump(mode="json") == notification_outbox_message.payload["message"]
    assert restored.notification_context.model_dump(mode="json") == notification_outbox_message.payload["notification_context"]
    assert command.post_message_strict.call_args.kwargs == {"event_key": notification_outbox_message.event_key}


@pytest.fixture
def failed_history():
    """
    提供一条有明确原始失败版本的不可变历史快照。
    """
    return TransferHistorySnapshot(
        id=11, transfer_task_id="task-fixture", transfer_settlement_revision=1,
        src="/fixture/video.mkv", src_storage="u115", status=False, errmsg="识别未能修正",
    )


@pytest.mark.parametrize("state", list(TransferExecutionState))
def test_outcome_is_decided_by_task_state_not_model_text(failed_history, state):
    """
    任务仍失败或待复核时需要人工，排队、运行和结算中均不能报告入库成功。
    """
    histories = Mock(get=Mock(return_value=failed_history))
    executions = Mock(get_snapshot=Mock(return_value=SimpleNamespace(state=state, last_error="识别未能修正")))
    stage, _summary = ai_takeover_result(history=failed_history, repository=executions, histories=histories)
    assert stage == ("attention" if state in {TransferExecutionState.FAILED, TransferExecutionState.MANUAL_REVIEW} else "queued")


def test_outcome_requires_new_success_receipt_and_preserves_unknown(failed_history):
    """
    pending 消失不能证明成功，只有更新的成功历史可确认结果，身份变化保持未知。
    """
    executions = Mock(get_snapshot=Mock(return_value=None), get_latest_settlement=Mock(return_value=None))
    histories = Mock(get=Mock(return_value=failed_history))
    assert ai_takeover_result(history=failed_history, repository=executions, histories=histories)[0] == "unknown"
    histories.get.return_value = replace(failed_history, status=True, transfer_task_id=None, transfer_settlement_revision=None)
    assert ai_takeover_result(history=failed_history, repository=executions, histories=histories)[0] == "unknown"
    executions.get_latest_settlement.return_value = TransferSettlementSnapshot(
        task_id="task-fixture", history_id=11, settlement_revision=2,
        outcome=TransferTerminalState.SUCCEEDED, src=failed_history.src, src_storage=failed_history.src_storage,
    )
    assert ai_takeover_result(history=failed_history, repository=executions, histories=histories)[0] == "resolved"
    executions.get_latest_settlement.return_value = None
    for current in (None, replace(failed_history, src="/another"), replace(failed_history, transfer_task_id="other-task")):
        histories.get.return_value = current
        assert ai_takeover_result(history=failed_history, repository=executions, histories=histories)[0] == "unknown"


@pytest.mark.parametrize("automatic", [True, False])
@pytest.mark.parametrize("mode", ["all", "attention", "off"])
def test_muting_notifications_keeps_ai_running_and_manual_reply_intact(
        policy, outbox_store, failed_history, monkeypatch, automatic, mode,
):
    """
    关闭主动推送仍执行模型分析；手动回复保留原目标，模型虚构成功也不会改变失败判定。
    """
    policy["agent_transfer_takeover"] = mode
    _factory, writer = outbox_store
    chain = object.__new__(TransferChain)
    chain.runtime_config = SimpleNamespace(ai_agent_enable=True, history_url="https://example.invalid/#/history")
    chain.transfer_history_repository = Mock(
        get=Mock(return_value=failed_history), async_get=AsyncMock(return_value=failed_history),
    )
    snapshot = SimpleNamespace(state=TransferExecutionState.FAILED, terminal_history_id=11, last_error="识别未能修正")
    chain.transfer_execution_repository = Mock(
        get_snapshot=Mock(return_value=snapshot), async_get_snapshot=AsyncMock(return_value=snapshot),
    )
    chain.durable_event_writer = writer
    harness = _MessageHarness()
    chain.post_message_strict = harness.post_message_strict
    chain.post_message = Mock()
    chain.async_post_message = AsyncMock()
    submitted = []
    registry = Mock()
    registry.submit_threadsafe.side_effect = lambda coroutine, **_kwargs: submitted.append(coroutine)
    monkeypatch.setattr(retry, "get_task_registry", lambda: registry)
    monkeypatch.setattr(retry.main_loop_registry, "require", lambda: object())
    monkeypatch.setattr(retry, "build_manual_redo_prompt", lambda _history: "只分析测试资源")

    async def run_prompt(**kwargs):
        """
        模拟模型声称成功，但不写入任何任务状态。
        """
        kwargs["output_callback"]("所有资源已成功整理，这是故意不可信的模型声明")

    manager = SimpleNamespace(run_background_prompt=AsyncMock(side_effect=run_prompt))
    monkeypatch.setattr(retry, "get_running_agent_manager", lambda: manager)
    chain._take_over_transfer_history_by_ai(
        11, channel=None if automatic else NotificationChannel.Wechat,
        source=None if automatic else "fixture-channel", userid=None if automatic else "fixture-user",
        username="tester", automatic=automatic,
    )
    assert len(submitted) == 1
    asyncio.run(submitted[0])
    manager.run_background_prompt.assert_awaited_once()
    if automatic:
        monkeypatch.setattr(outbox_composition, "CommandChain", lambda: harness)
        dispatcher = OutboxDispatcher(SqlAlchemyOutboxDispatchStore(_factory), outbox_composition.build_outbox_handlers())
        while dispatcher.dispatch_one():
            pass
        assert harness.eventmanager.send_event.call_count == {"all": 2, "attention": 1, "off": 0}[mode]
        assert harness.messageoper.add.call_count == 1
        if mode != "off":
            payload = harness.eventmanager.send_event.call_args.kwargs["data"]
            assert payload["title"] == "智能助手处理后仍需人工核对"
            if mode == "attention":
                assert "不可信的模型声明" not in payload["text"]
    else:
        assert chain.async_post_message.await_count == 2
        reply = chain.async_post_message.await_args.args[0]
        assert reply.userid == "fixture-user" and reply.source == "fixture-channel"
        assert reply.notification_context is None


def test_async_registration_is_durable_before_dispatch_and_deduplicated(outbox_store):
    """
    异步入口只提交真实短事务，同一身份不重复登记，也不等待外部通知渠道。
    """
    factory, writer = outbox_store

    async def register():
        """
        在一个受控事件循环中完成两次登记。
        """
        assert await writer.async_agent_transfer_notification(message=_message())
        assert not await writer.async_agent_transfer_notification(message=_message())

    asyncio.run(register())
    with factory() as session:
        row = session.scalars(select(OutboxMessage)).one()
        assert row.status == "pending" and row.attempt == 0


def test_original_success_receipt_survives_cleared_history_mapping(outbox_store, failed_history):
    """
    真实 DAO 仍能读取独立成功回执，历史的任务与结算映射都已清空时不误发待核实通知。
    """
    from app.db.adapters.transfer.execution import TransactionalTransferExecutionRepository
    from app.db.models.transferpending import TransferPending
    from app.db.models.transfersettlementreceipt import TransferSettlementReceipt

    factory, writer = outbox_store
    engine = factory.kw["bind"]
    TransferPending.__table__.create(engine)
    TransferSettlementReceipt.__table__.create(engine)
    with factory() as session:
        session.add(TransferSettlementReceipt(
            task_id="task-fixture", history_id=11, settlement_revision=2, outcome="succeeded",
            execution_fingerprint="fixture", lease_token="fixture", history_status=True,
            src=failed_history.src, src_storage=failed_history.src_storage, pending_deleted=True,
            created_at="2026-11-03T00:00:00+00:00", updated_at="2026-11-03T00:00:00+00:00",
        ))
        session.commit()
    executions = TransactionalTransferExecutionRepository(factory, async_session=writer._async_session_factory)
    histories = Mock(get=Mock(return_value=replace(
        failed_history, transfer_task_id=None, transfer_settlement_revision=None, status=True,
    )))
    assert ai_takeover_result(history=failed_history, repository=executions, histories=histories)[0] == "resolved"
    receipt = asyncio.run(executions.async_get_latest_settlement(task_id="task-fixture"))
    assert receipt == executions.get_latest_settlement(task_id="task-fixture")
