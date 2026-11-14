"""
验证整理识别失败在自动重试预算耗尽后的人工身份修正授权。

授权由管理用户点击「智能助手整理」签发，只补全预算门：绑定历史、任务和结算版本，
消费一次即失效，并且不绕过文件副作用、失败阶段等原有安全校验。
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.api.endpoints import history as history_endpoint
from app.application.chain.events import TransferResultSettlement
from app.application.configuration import ApiRuntimeConfig
from app.application.history import TransferHistoryWrite
from app.application.history.retry import max_failed_retries
from app.application.transfer.authorization import (
    MANUAL_IDENTITY_AUTHORIZATION_SOURCE,
    ManualIdentityAuthorization,
    authorization_actor_name,
    authorization_expiry,
    authorization_rejection,
)
from app.application.transfer.execution import (
    TransferExecutionCommand,
    TransferExecutionConflictError,
    TransferExecutionState,
    TransferOperationObservation,
    TransferOperationObservationState,
    TransferStepResult,
)
from app.application.transfer.workflow import TransferPlanCheckpoint, TransferPlanningInput
from app.chain.transfer.execution import _DurableTransferStepRunner
from app.db.adapters.chain import TransactionalChainDurableEventWriter
from app.db.adapters.transfer.admission import TransactionalTransferAdmissionRepository
from app.db.adapters.transfer.execution import TransactionalTransferExecutionRepository
from app.db.models.transferauthorization import TransferManualIdentityAuthorization
from app.db.models.transferexecutionstep import TransferExecutionStep
from app.db.models.transferhistory import TransferHistory
from app.db.models.transferpending import TransferPending
from app.db.models.transfersettlementreceipt import TransferSettlementReceipt
from app.domain.meta.metavideo import MetaVideo
from app.schemas.file import FileItem
from app.schemas.history import TransferHistory as TransferHistoryDTO
from app.schemas.transfer import TransferInfo
from app.schemas.types import MediaType

NOW = '2026-01-01 00:00:00.000000'


@pytest.fixture
def grant_store(tmp_path):
    """
    提供独立 SQLite 事务和真实持久化端口，结束后释放数据库连接。

    :param tmp_path: pytest 独立临时目录
    :yields: 准入端口、执行端口、会话工厂及终态写入器
    """
    engine = create_engine(f"sqlite:///{tmp_path / 'identity-grant.db'}")
    for model in (TransferPending, TransferHistory, TransferExecutionStep, TransferSettlementReceipt,
                  TransferManualIdentityAuthorization):
        model.__table__.create(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    try:
        yield (TransactionalTransferAdmissionRepository(factory),
               TransactionalTransferExecutionRepository(factory), factory,
               TransactionalChainDurableEventWriter(factory))
    finally:
        engine.dispose()


def _failed_recognition(store, tmp_path):
    """
    经正式准入、步骤执行及原子结算建立可重新规划的识别失败。

    :param store: 隔离的真实持久化端口
    :param tmp_path: 源文件所在的独立临时目录
    :return: 原准入、规划输入及失败结算回执
    """
    admissions, executions, _factory, writer = store
    source = tmp_path / '[ANi] 偵探已經，死了。Season 2 - 03.strm'
    source.write_text('https://example.invalid/video')
    item = FileItem(storage='local', path=str(source), name=source.name, type='file', extension='strm')
    meta = MetaVideo('Incorrect.S01E03')
    planning = TransferPlanningInput(
        source_fileitem=item.model_dump(mode='json'), meta=meta.to_dict(),
        target_storage='local', target_path=str(tmp_path / 'library'),
        requested_transfer_type='copy', media_type=MediaType.TV.value,
        options={'_meta_kind': 'MetaVideo', 'username': 'tester'},
    )
    admitted = admissions.admit(storage='local', src_path=str(source), planning_input=planning)
    claimed = admissions.claim_task(task_id=admitted.task_id, owner_id='test', lease_seconds=120)
    checkpoint = TransferPlanCheckpoint(
        planning_input=planning, target_storage='local', root_target_path=str(tmp_path / 'library'),
        final_target_path=str(tmp_path / 'library'), resolved_transfer_type='copy', items=(),
        rejection_error='未识别到媒体信息', resolved_meta_kind='MetaVideo', resolved_meta=meta.to_dict(),
    )
    admissions.checkpoint_plan(task_id=claimed.task_id, lease_token=claimed.lease_token,
                              input_fingerprint=planning.fingerprint, checkpoint=checkpoint)
    runner = _DurableTransferStepRunner(task_id=claimed.task_id, lease_token=claimed.lease_token,
                                       checkpoint_fingerprint=checkpoint.fingerprint, repository=executions)
    evidence = TransferStepResult(payload={'error': checkpoint.rejection_error})
    runner.run(phase='planning', kind='reject', payload=evidence.payload, execute=lambda: evidence,
               observe=lambda: TransferOperationObservation(state=TransferOperationObservationState.APPLIED,
                                                             evidence=evidence))
    result = runner.checkpoint(TransferInfo(success=False, fileitem=item, message=checkpoint.rejection_error))
    receipt = writer.transfer_result(
        topic=None, event_payload={}, publish=None,
        stage_history=lambda repository: repository.replace(TransferHistoryWrite(
            src=str(source), src_storage='local', status=False, mode='copy',
            type=MediaType.TV.value, errmsg=checkpoint.rejection_error, retry_count=1,
        )),
        settlement=TransferResultSettlement(task_id=claimed.task_id, lease_token=claimed.lease_token,
                                            execution_fingerprint=result.fingerprint, outcome='failed',
                                            error=checkpoint.rejection_error),
    )
    return claimed, planning, receipt


def _exhaust_budget(factory, history_id):
    """
    把失败历史推进到自动重试预算耗尽且已暂停的终态。

    :param factory: 隔离会话工厂
    :param history_id: 失败历史标识
    :return: 无返回值
    """
    with factory() as session:
        history = session.get(TransferHistory, history_id)
        history.retry_count = max_failed_retries()
        history.auto_paused = True
        session.commit()


def _grant(executions, history_id, task_id, issued_by='admin'):
    """
    以管理用户名义签发一次身份修正授权。

    :param executions: 执行仓储端口
    :param history_id: 失败历史标识
    :param task_id: 失败任务标识
    :param issued_by: 签发者名称
    :return: 是否签发成功
    """
    return executions.grant_manual_identity_authorization(
        history_id=history_id, task_id=task_id, settlement_revision=1, issued_by=issued_by,
    )


def _correct(executions, history_id, task_id):
    """
    模拟智能助手核实身份后提交的 S02E03 修正。

    :param executions: 执行仓储端口
    :param history_id: 精确失败历史标识
    :param task_id: 原始失败回执关联的持久任务
    :return: 持久调度受理结果
    """
    return TransferExecutionCommand(executions).request_replan(
        history_id=history_id, task_id=task_id, settlement_revision=1, media_source='themoviedb',
        media_id='117061', media_type='tv', season=2, episode=3, requested_by='test-agent',
    )


def _redo_endpoint(history_id, task_id, executions, runtime_config=None):
    """
    以管理员身份调用单条「智能助手重新整理」端点。

    :param history_id: 失败历史标识
    :param task_id: 失败任务标识
    :param executions: 执行仓储端口
    :param runtime_config: 可选运行时配置，默认开启智能助手
    :return: 端点响应
    """
    class _Query:
        async def get_transfer(self, target_history_id):
            """
            返回携带持久任务与结算版本的历史视图。

            :param target_history_id: 端点请求的历史标识
            :return: 历史 DTO
            """
            return TransferHistoryDTO(id=target_history_id, transfer_task_id=task_id,
                                     transfer_settlement_revision=1)

    return asyncio.run(history_endpoint.ai_redo_transfer_history(
        history_id, query=_Query(),
        runtime_config=runtime_config or ApiRuntimeConfig(
            access_token_expire_minutes=30, btrfs_fsid_dedup=False, ai_agent_enable=True),
        current_user=SimpleNamespace(name='管理员', id=1), task_registry=object(),
        execution_repository=executions,
    ))


def test_authorization_expiry_uses_fixed_width_utc_and_rejects_zero_ttl():
    """
    授权到期时间与租约列同格式，非正有效期直接拒绝。
    """
    assert authorization_expiry(NOW) == '2026-01-01 00:30:00.000000'
    assert authorization_expiry(NOW, ttl_seconds=1) == '2026-01-01 00:00:01.000000'
    with pytest.raises(ValueError, match='有效期'):
        authorization_expiry(NOW, ttl_seconds=0)


@pytest.mark.parametrize('authorization, expected', [
    (None, '自动重试次数已耗尽'),
    (ManualIdentityAuthorization(history_id=1, task_id='t', settlement_revision=1, issued_by='admin',
                                issued_at=NOW, expires_at='2026-01-01 00:30:00.000000'), None),
    (ManualIdentityAuthorization(history_id=1, task_id='other', settlement_revision=1, issued_by='admin',
                                issued_at=NOW, expires_at='2026-01-01 00:30:00.000000'), '不匹配'),
    (ManualIdentityAuthorization(history_id=1, task_id='t', settlement_revision=2, issued_by='admin',
                                issued_at=NOW, expires_at='2026-01-01 00:30:00.000000'), '不匹配'),
    (ManualIdentityAuthorization(history_id=1, task_id='t', settlement_revision=1, issued_by='admin',
                                issued_at=NOW, expires_at='2026-01-01 00:00:00.000000'), '已过期'),
    (ManualIdentityAuthorization(history_id=1, task_id='t', settlement_revision=1, issued_by='admin',
                                issued_at=NOW, expires_at='2026-01-01 00:30:00.000000',
                                used_at=NOW, used_by='agent'), '已用于一次身份修正'),
])
def test_authorization_rejection_covers_staleness_and_replay(authorization, expected):
    """
    授权核对只判断绑定关系、有效性与是否已消费。
    """
    reason = authorization_rejection(authorization, history_id=1, task_id='t', settlement_revision=1,
                                     now_utc=NOW)
    if expected is None:
        assert reason is None
    else:
        assert reason is not None and expected in reason


@pytest.mark.parametrize('user, expected', [
    (SimpleNamespace(name='admin', id=1), 'admin'),
    (SimpleNamespace(name='  管理员  ', id=2), '管理员'),
    (SimpleNamespace(name=None, id=3), '#3'),
    (None, 'unknown'),
])
def test_authorization_actor_name_normalizes_missing_identity(user, expected):
    """
    签发者列必须非空，缺失名称时退化为可读标识。
    """
    assert authorization_actor_name(user) == expected


def test_exhausted_budget_still_rejects_without_authorization(grant_store, tmp_path):
    """
    未签发授权时，预算耗尽的识别失败保持原拒绝语义且不消耗重试代数。
    """
    _admissions, executions, factory, _writer = grant_store
    claimed, _planning, receipt = _failed_recognition(grant_store, tmp_path)
    _exhaust_budget(factory, receipt.history_id)
    before = executions.get_snapshot(task_id=claimed.task_id)
    with pytest.raises(TransferExecutionConflictError, match='自动重试次数已耗尽'):
        _correct(executions, receipt.history_id, claimed.task_id)
    assert executions.get_snapshot(task_id=claimed.task_id) == before


def test_manual_authorization_allows_one_replan_without_resetting_budget(grant_store, tmp_path):
    """
    有效授权允许一次重新规划，仅补全预算门：预算继续累加、审计列标注人工确认。
    """
    _admissions, executions, factory, _writer = grant_store
    claimed, _planning, receipt = _failed_recognition(grant_store, tmp_path)
    _exhaust_budget(factory, receipt.history_id)
    before = executions.get_snapshot(task_id=claimed.task_id)
    assert _grant(executions, receipt.history_id, claimed.task_id)
    result = _correct(executions, receipt.history_id, claimed.task_id)
    assert result.accepted and result.state is TransferExecutionState.RETRY_WAIT
    with factory() as session:
        pending = session.scalars(select(TransferPending)).one()
        record = session.scalars(select(TransferManualIdentityAuthorization)).one()
        assert pending.retry_generation == before.retry_generation + 1
        assert pending.retry_count == before.retry_count + 1
        assert pending.retry_requested_by.endswith('+manual_identity')
        assert '管理用户确认' in pending.retry_reason
        assert record.used_at is not None and record.used_by == 'test-agent'
        assert (record.media_source, record.media_id) == ('themoviedb', '117061')
        assert record.issued_by == 'admin'
        assert record.source == MANUAL_IDENTITY_AUTHORIZATION_SOURCE == 'history_ai_redo'
        assert record.issued_at < record.expires_at
    # 授权一次性：重新规划已回到排队状态，第二次修正必须重新点击签发。
    with pytest.raises(TransferExecutionConflictError):
        _correct(executions, receipt.history_id, claimed.task_id)
    assert executions.get_snapshot(task_id=claimed.task_id).retry_generation == before.retry_generation + 1


@pytest.mark.parametrize('tamper', ['expired', 'used', 'other_revision'])
def test_stale_or_replayed_authorization_is_rejected_without_consuming(grant_store, tmp_path, tamper):
    """
    过期、已消费或结算版本不匹配的授权不能放行，也不会被再次消费或部分提交。
    """
    _admissions, executions, factory, _writer = grant_store
    claimed, _planning, receipt = _failed_recognition(grant_store, tmp_path)
    _exhaust_budget(factory, receipt.history_id)
    assert _grant(executions, receipt.history_id, claimed.task_id)
    with factory() as session:
        record = session.scalars(select(TransferManualIdentityAuthorization)).one()
        if tamper == 'expired':
            record.expires_at = '2000-01-01 00:00:00.000000'
        elif tamper == 'used':
            record.used_at, record.used_by = '2999-01-01 00:00:00.000000', 'someone'
        else:
            record.settlement_revision = 99
        session.commit()
    before = executions.get_snapshot(task_id=claimed.task_id)
    with pytest.raises(TransferExecutionConflictError, match='自动重试次数已耗尽'):
        _correct(executions, receipt.history_id, claimed.task_id)
    assert executions.get_snapshot(task_id=claimed.task_id) == before
    with factory() as session:
        record = session.scalars(select(TransferManualIdentityAuthorization)).one()
        assert record.used_by == ('someone' if tamper == 'used' else None)
        assert session.get(TransferHistory, receipt.history_id).auto_paused is True


def test_authorization_cannot_bypass_file_side_effect_checks(grant_store, tmp_path):
    """
    含文件副作用的失败即使有人工授权也仍拒绝，授权随事务回滚保持未消费。
    """
    _admissions, executions, factory, _writer = grant_store
    claimed, _planning, receipt = _failed_recognition(grant_store, tmp_path)
    _exhaust_budget(factory, receipt.history_id)
    assert _grant(executions, receipt.history_id, claimed.task_id)
    with factory() as session:
        step = session.scalars(select(TransferExecutionStep)).one()
        step.phase, step.kind = 'transfer', 'materialize_target'
        session.commit()
    with pytest.raises(TransferExecutionConflictError, match='文件操作'):
        _correct(executions, receipt.history_id, claimed.task_id)
    with factory() as session:
        record = session.scalars(select(TransferManualIdentityAuthorization)).one()
        assert record.used_at is None
        assert session.get(TransferHistory, receipt.history_id).auto_paused is True


def test_grant_requires_matching_history_task_and_revision(grant_store, tmp_path):
    """
    签发端口拒绝不存在的历史、错误任务或陈旧结算版本。
    """
    _admissions, executions, factory, _writer = grant_store
    claimed, _planning, receipt = _failed_recognition(grant_store, tmp_path)
    assert not _grant(executions, receipt.history_id + 100, claimed.task_id)
    assert not _grant(executions, receipt.history_id, 'other-task')
    with factory() as session:
        history = session.get(TransferHistory, receipt.history_id)
        history.transfer_settlement_revision += 1
        session.commit()
    assert not _grant(executions, receipt.history_id, claimed.task_id)
    with factory() as session:
        assert session.scalars(select(TransferManualIdentityAuthorization)).all() == []


def test_command_rejects_incomplete_grant_requests_before_writing():
    """
    命令层拒绝缺少历史、结算版本、任务或签发身份的人工授权请求。
    """
    repository = Mock()
    with pytest.raises(ValueError, match='人工授权'):
        TransferExecutionCommand(repository).grant_manual_identity_authorization(
            history_id=0, task_id='task', settlement_revision=1, issued_by='admin')
    with pytest.raises(ValueError, match='人工授权'):
        TransferExecutionCommand(repository).grant_manual_identity_authorization(
            history_id=1, task_id='  ', settlement_revision=1, issued_by='admin')
    with pytest.raises(ValueError, match='人工授权'):
        TransferExecutionCommand(repository).grant_manual_identity_authorization(
            history_id=1, task_id='task', settlement_revision=1, issued_by=' ')
    repository.grant_manual_identity_authorization.assert_not_called()


def test_ai_redo_endpoint_grants_budget_exhausted_replan(grant_store, tmp_path, monkeypatch):
    """
    管理用户点击「智能助手整理」即签发授权，调度侧随后可补全预算门。
    """
    _admissions, executions, factory, _writer = grant_store
    claimed, _planning, receipt = _failed_recognition(grant_store, tmp_path)
    _exhaust_budget(factory, receipt.history_id)
    started = []
    monkeypatch.setattr(history_endpoint, '_start_ai_redo_task', lambda **kwargs: started.append(kwargs))
    response = _redo_endpoint(receipt.history_id, claimed.task_id, executions)
    assert response.success and len(started) == 1
    with factory() as session:
        record = session.scalars(select(TransferManualIdentityAuthorization)).one()
        assert record.issued_by == '管理员' and record.used_at is None
    result = _correct(executions, receipt.history_id, claimed.task_id)
    assert result.accepted and result.state is TransferExecutionState.RETRY_WAIT


def test_ai_redo_endpoint_skips_grant_for_protected_state(grant_store, tmp_path, monkeypatch):
    """
    受保护状态不启动助手也不签发授权，避免为不可用任务留下凭据。
    """
    _admissions, executions, factory, _writer = grant_store
    claimed, _planning, receipt = _failed_recognition(grant_store, tmp_path)
    with factory() as session:
        pending = session.scalars(select(TransferPending)).one()
        pending.execution_state = TransferExecutionState.MANUAL_REVIEW.value
        session.commit()

    def _fail(**kwargs):
        raise AssertionError('受保护状态不应启动助手')

    monkeypatch.setattr(history_endpoint, '_start_ai_redo_task', _fail)
    response = _redo_endpoint(receipt.history_id, claimed.task_id, executions)
    assert not response.success
    with factory() as session:
        assert session.scalars(select(TransferManualIdentityAuthorization)).all() == []


def test_regrant_supersedes_previous_authorization_keeping_audit_rows(grant_store, tmp_path):
    """
    同一任务重复点击只保留最新票据可用，旧票据提前过期但仍留审计行。
    """
    _admissions, executions, factory, _writer = grant_store
    claimed, _planning, receipt = _failed_recognition(grant_store, tmp_path)
    _exhaust_budget(factory, receipt.history_id)
    assert _grant(executions, receipt.history_id, claimed.task_id, issued_by='first-admin')
    assert _grant(executions, receipt.history_id, claimed.task_id, issued_by='second-admin')
    with factory() as session:
        stale, latest = session.scalars(select(TransferManualIdentityAuthorization)
                                        .order_by(TransferManualIdentityAuthorization.id)).all()
        assert (stale.issued_by, latest.issued_by) == ('first-admin', 'second-admin')
        # 旧票据只被提前到期，未被当作已消费，因此仍可看出它从未放行任何修正。
        assert stale.used_at is None
        assert stale.expires_at <= latest.issued_at
        assert latest.expires_at > latest.issued_at
    result = _correct(executions, receipt.history_id, claimed.task_id)
    assert result.accepted and result.state is TransferExecutionState.RETRY_WAIT
    with factory() as session:
        stale, latest = session.scalars(select(TransferManualIdentityAuthorization)
                                        .order_by(TransferManualIdentityAuthorization.id)).all()
        assert stale.used_at is None
        assert latest.used_at is not None and latest.used_by == 'test-agent'


def test_regrant_preserves_consumed_authorization_audit(grant_store, tmp_path):
    """
    已消费票据不在作废范围内，重复签发不改写其消费痕迹与到期时刻。
    """
    _admissions, executions, factory, _writer = grant_store
    claimed, _planning, receipt = _failed_recognition(grant_store, tmp_path)
    _exhaust_budget(factory, receipt.history_id)
    assert _grant(executions, receipt.history_id, claimed.task_id)
    assert _correct(executions, receipt.history_id, claimed.task_id).accepted
    with factory() as session:
        consumed = session.scalars(select(TransferManualIdentityAuthorization)).one()
        consumed_expires, consumed_at = consumed.expires_at, consumed.used_at
        assert consumed_at is not None and consumed_expires > consumed.issued_at
    assert _grant(executions, receipt.history_id, claimed.task_id, issued_by='second-admin')
    with factory() as session:
        consumed, fresh = session.scalars(select(TransferManualIdentityAuthorization)
                                          .order_by(TransferManualIdentityAuthorization.id)).all()
        assert (consumed.used_at, consumed.used_by) == (consumed_at, 'test-agent')
        assert consumed.expires_at == consumed_expires
        assert fresh.used_at is None and fresh.expires_at > fresh.issued_at


def test_supersede_is_scoped_to_same_history_and_task(grant_store, tmp_path):
    """
    作废只作用于同一历史同一任务的未消费票据，其它任务或其它历史的票据保持有效。
    """
    _admissions, executions, factory, _writer = grant_store
    claimed, _planning, receipt = _failed_recognition(grant_store, tmp_path)
    _exhaust_budget(factory, receipt.history_id)
    future = '2099-01-01 00:00:00.000000'
    with factory() as session:
        session.add(TransferManualIdentityAuthorization(
            history_id=receipt.history_id, task_id='other-task', settlement_revision=1,
            source=MANUAL_IDENTITY_AUTHORIZATION_SOURCE, issued_by='other-admin',
            issued_at=NOW, expires_at=future))
        session.add(TransferManualIdentityAuthorization(
            history_id=receipt.history_id + 999, task_id=claimed.task_id, settlement_revision=1,
            source=MANUAL_IDENTITY_AUTHORIZATION_SOURCE, issued_by='other-admin',
            issued_at=NOW, expires_at=future))
        session.commit()
    assert _grant(executions, receipt.history_id, claimed.task_id)
    assert _grant(executions, receipt.history_id, claimed.task_id)
    with factory() as session:
        untouched = session.scalars(select(TransferManualIdentityAuthorization).where(
            TransferManualIdentityAuthorization.issued_by == 'other-admin')).all()
        assert len(untouched) == 2
        assert {record.expires_at for record in untouched} == {future}
        assert {record.used_at for record in untouched} == {None}
        mine = session.scalars(select(TransferManualIdentityAuthorization).where(
            TransferManualIdentityAuthorization.issued_by == 'admin')
            .order_by(TransferManualIdentityAuthorization.id)).all()
        assert len(mine) == 2 and mine[0].expires_at <= mine[1].issued_at
