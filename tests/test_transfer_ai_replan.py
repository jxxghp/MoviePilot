"""
验证 AI 识别修正在真实事务中保留任务、失败回执、原始证据和总重试预算。
"""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.api.endpoints import history as history_endpoint
from app.application.chain.events import TransferResultSettlement
from app.application.history import TransferHistoryWrite
from app.application.transfer.execution import (
    TransferExecutionCommand,
    TransferExecutionConflictError,
    TransferExecutionState,
    TransferOperationObservation,
    TransferOperationObservationState,
    TransferStepResult,
)
from app.application.transfer.workflow import (
    TransferAdmissionConflictError,
    TransferPlanCheckpoint,
    TransferPlanningInput,
)
from app.chain.transfer.execution import _DurableTransferStepRunner
from app.chain.transfer.facade import TransferChain
from app.db.adapters.chain import TransactionalChainDurableEventWriter
from app.db.adapters.transfer.admission import TransactionalTransferAdmissionRepository
from app.db.adapters.transfer.execution import TransactionalTransferExecutionRepository
from app.db.models.transferexecutionstep import TransferExecutionStep
from app.db.models.transferhistory import TransferHistory
from app.db.models.transferpending import TransferPending
from app.db.models.transfersettlementreceipt import TransferSettlementReceipt
from app.domain.meta.metavideo import MetaVideo
from app.schemas.file import FileItem
from app.schemas.history import TransferHistoryReplanRequest
from app.schemas.transfer import TransferInfo
from app.schemas.types import MediaSource, MediaType


@pytest.fixture
def replan_store(tmp_path):
    """
    提供独立 SQLite 事务和真实持久化端口，结束后释放数据库连接。

    :param tmp_path: pytest 独立临时目录
    :yields: 准入端口、执行端口、会话工厂及终态写入器
    """
    engine = create_engine(f"sqlite:///{tmp_path / 'ai-replan.db'}")
    for model in (TransferPending, TransferHistory, TransferExecutionStep, TransferSettlementReceipt):
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


def _correct(executions, history_id, task_id):
    """
    模拟 Agent 经媒体查询核实后提交本 Issue 的 S02E03 修正。

    :param executions: 执行仓储端口
    :param history_id: 精确失败历史标识
    :param task_id: 原始失败回执关联的持久任务
    :return: 持久调度受理结果
    """
    return TransferExecutionCommand(executions).request_replan(
        history_id=history_id, task_id=task_id, settlement_revision=1, media_source='themoviedb', media_id='117061', media_type='tv',
        season=2, episode=3, requested_by='test-agent',
    )


def test_replan_preserves_identity_evidence_budget_and_replays_correct_season(replan_store, tmp_path):
    """
    修正重启后仍使用同一任务与预算，并从 accepted 输入恢复正确身份和 S02E03。
    """
    admissions, executions, factory, _writer = replan_store
    claimed, planning, receipt = _failed_recognition(replan_store, tmp_path)
    old = executions.get_snapshot(task_id=claimed.task_id)
    result = _correct(executions, receipt.history_id, claimed.task_id)
    assert result.accepted and result.state is TransferExecutionState.RETRY_WAIT
    with factory() as session:
        pending = session.scalars(select(TransferPending)).one()
        history = session.get(TransferHistory, receipt.history_id)
        assert pending.task_id == claimed.task_id and pending.state == 'accepted'
        assert pending.retry_count == old.retry_count + 1
        assert pending.retry_generation == old.retry_generation + 1
        assert pending.settlement_revision == receipt.settlement_revision
        assert pending.terminal_history_id == history.id
        assert pending.checkpoint_payload is None and pending.execution_payload is None
        assert history.transfer_task_id == claimed.task_id and history.retry_count == 1
        assert session.scalars(select(TransferExecutionStep)).all() == []
        saved_receipt = session.scalars(select(TransferSettlementReceipt)).one()
        assert saved_receipt.history_id == receipt.history_id and saved_receipt.outcome == 'failed'
        frozen = pending.planning_input
        assert frozen['source_fileitem'] == planning.source_fileitem
        assert frozen['target_path'] == planning.target_path
        assert frozen['requested_transfer_type'] == 'copy'
        archive = frozen['options']['ai_replan_evidence']
        assert archive['plan']['rejection_error'] == '未识别到媒体信息'
        assert archive['execution']['steps'][0]['operation_id'] == old.steps[0].operation_id
        assert archive['execution']['checkpoint']['fingerprint'] == old.checkpoint.fingerprint
    # 新实例模拟恢复进程；旧输入不能替换修正，修正后的同源输入仍复用原任务。
    restarted = TransactionalTransferAdmissionRepository(factory)
    with pytest.raises(TransferAdmissionConflictError):
        restarted.admit(storage='local', src_path=planning.source_fileitem['path'], planning_input=planning)
    duplicate = restarted.admit(storage='local', src_path=planning.source_fileitem['path'],
                                planning_input=TransferPlanningInput.from_payload(frozen))
    assert duplicate.task_id == claimed.task_id
    replay = restarted.claim_task(task_id=claimed.task_id, owner_id='after-restart', lease_seconds=120)
    assert replay is not None and replay.planning_input.media_id == '117061'
    chain = object.__new__(TransferChain)
    chain.put_to_queue = Mock(return_value=True)
    chain._TransferChain__bind_claimed_admission = Mock()
    assert chain._TransferChain__queue_accepted_replay(FileItem.model_validate(planning.source_fileitem), replay)
    task = chain.put_to_queue.call_args.args[0]
    assert task.admission_task_id == claimed.task_id
    assert task.media_source is MediaSource.TMDB and task.media_id == '117061'
    assert task.mediainfo is None  # 不回放失败的识别缓存。
    assert task.meta.begin_season == 2 and task.meta.begin_episode == 3
    assert task.target_path.as_posix() == planning.target_path
    assert task.transfer_type == 'copy'
    admissions.release_claim(task_id=claimed.task_id, lease_token=replay.lease_token)


def test_repeated_replan_is_rejected_without_extra_generation(replan_store, tmp_path):
    """
    重复或并发旧请求不能绕过排队状态、再次消耗预算或另建同源任务。
    """
    _admissions, executions, factory, _writer = replan_store
    claimed, _planning, receipt = _failed_recognition(replan_store, tmp_path)
    first = _correct(executions, receipt.history_id, claimed.task_id)
    with pytest.raises(TransferExecutionConflictError):
        _correct(executions, receipt.history_id, claimed.task_id)
    current = executions.get_snapshot(task_id=claimed.task_id)
    assert current.retry_generation == first.retry_generation
    with factory() as session:
        assert len(session.scalars(select(TransferPending)).all()) == 1
        assert len(session.scalars(select(TransferSettlementReceipt)).all()) == 1


@pytest.mark.parametrize('blocker', ['manual_review', 'running', 'active_lease', 'history_revision',
                                     'receipt_revision', 'paused', 'history_budget', 'task_budget', 'file_step', 'storage', 'corrupt_input'])
def test_replan_rejects_unsafe_or_stale_tasks_atomically(replan_store, tmp_path, blocker):
    """
    复核、租约、版本、预算、存储故障及操作证据均受原子保护，拒绝后证据不变。
    """
    _admissions, executions, factory, _writer = replan_store
    claimed, _planning, receipt = _failed_recognition(replan_store, tmp_path)
    with factory() as session:
        pending = session.scalars(select(TransferPending)).one()
        history = session.get(TransferHistory, receipt.history_id)
        step = session.scalars(select(TransferExecutionStep)).one()
        if blocker in {'manual_review', 'running'}:
            pending.execution_state = blocker
        elif blocker == 'active_lease':
            pending.lease_owner, pending.lease_token = 'other-worker', 'other-lease'
            pending.lease_expires_at = '2099-01-01 00:00:00.000000'
        elif blocker == 'history_revision':
            history.transfer_settlement_revision += 1
        elif blocker == 'receipt_revision':
            pending.settlement_revision = history.transfer_settlement_revision = 2
        elif blocker == 'paused':
            history.auto_paused = True
        elif blocker == 'history_budget':
            history.retry_count = 10
        elif blocker == 'task_budget':
            pending.retry_count = 10
        elif blocker == 'file_step':
            step.phase, step.kind = 'transfer', 'materialize_target'
        elif blocker == 'storage':
            history.failure_stage = 'destination_access'
        elif blocker == 'corrupt_input':
            pending.planning_input = {**pending.planning_input, 'target_path': '/wrong'}
        session.commit()
    before = executions.get_snapshot(task_id=claimed.task_id)
    with pytest.raises(TransferExecutionConflictError):
        _correct(executions, receipt.history_id, claimed.task_id)
    assert executions.get_snapshot(task_id=claimed.task_id) == before
    with factory() as session:
        assert session.get(TransferHistory, receipt.history_id).transfer_task_id == claimed.task_id
        assert len(session.scalars(select(TransferExecutionStep)).all()) == 1
        assert len(session.scalars(select(TransferSettlementReceipt)).all()) == 1


def test_replan_commit_failure_restores_original_internal_step(replan_store, tmp_path, monkeypatch):
    """
    写入失败时原任务、拒绝步骤、计划、历史和回执在同一事务内回滚。
    """
    _admissions, executions, factory, _writer = replan_store
    claimed, _planning, receipt = _failed_recognition(replan_store, tmp_path)
    before = executions.get_snapshot(task_id=claimed.task_id)
    monkeypatch.setattr('app.db.uow.SqlAlchemyUnitOfWork.commit', Mock(side_effect=RuntimeError('write failed')))
    with pytest.raises(RuntimeError, match='write failed'):
        _correct(executions, receipt.history_id, claimed.task_id)
    assert executions.get_snapshot(task_id=claimed.task_id) == before
    with factory() as session:
        pending = session.scalars(select(TransferPending)).one()
        assert pending.state == 'planned' and pending.checkpoint_payload is not None
        assert len(session.scalars(select(TransferSettlementReceipt)).all()) == 1


@pytest.mark.parametrize('overrides', [{'media_id': ' '}, {'media_id': '0'}, {'season': -1},
                                      {'episode': 0}, {'media_type': 'music'}, {'settlement_revision': 0}, {'target_path': '/other'}])
def test_replan_request_rejects_invalid_or_unscoped_fields(overrides):
    """
    修正接口拒绝空身份、非法季集及修改源目标或模式的额外字段。
    """
    payload = {'task_id': 'test-task', 'settlement_revision': 1, 'media_source': 'themoviedb', 'media_id': '117061', 'media_type': 'tv', **overrides}
    with pytest.raises(ValidationError):
        TransferHistoryReplanRequest.model_validate(payload)


@pytest.mark.parametrize('overrides', [
    {'history_id': 0}, {'settlement_revision': 0}, {'task_id': ''}, {'requested_by': ''},
    {'media_id': ' '}, {'media_id': '0'}, {'media_type': 'music'}, {'media_type': 'unknown'},
    {'media_source': 'not a source'}, {'season': -1}, {'episode': 0},
    {'media_type': 'movie', 'season': 1}, {'media_type': 'movie', 'episode': 1},
])
def test_replan_command_rejects_invalid_internal_requests_before_writing(overrides):
    """
    内部调用绕过 HTTP 模型时仍拒绝非法身份、陈旧版本和电影季集，且不触及持久层。
    """
    repository = Mock()
    payload = {'history_id': 1, 'task_id': 'test-task', 'settlement_revision': 1,
               'media_source': 'themoviedb', 'media_id': '117061', 'media_type': 'tv',
               'requested_by': 'test-agent', **overrides}
    with pytest.raises(ValueError):
        TransferExecutionCommand(repository).request_replan(**payload)
    repository.request_replan.assert_not_called()


def test_replan_command_normalizes_movie_identity_without_inventing_seasons():
    """
    无季集的电影修正保留来源原生身份，并归一化后交给持久端口执行安全校验。
    """
    repository = Mock()
    result = TransferExecutionCommand(repository).request_replan(
        history_id=1, task_id='test-task', settlement_revision=1, media_source='themoviedb',
        media_id=' 550 ', media_type='movie', requested_by='test-agent',
    )
    assert result is repository.request_replan.return_value
    repository.request_replan.assert_called_once_with(
        history_id=1, task_id='test-task', settlement_revision=1, media_source='themoviedb',
        media_id='550', media_type=MediaType.MOVIE.value, season=None, episode=None,
        requested_by='test-agent',
    )


def test_replan_endpoint_returns_admission_instead_of_file_success(replan_store, tmp_path):
    """
    REST 端点向 Agent 明确返回排队受理，并在重复请求时返回受保护的失败结果。
    """
    _admissions, executions, _factory, _writer = replan_store
    claimed, _planning, receipt = _failed_recognition(replan_store, tmp_path)
    payload = TransferHistoryReplanRequest(task_id=claimed.task_id, settlement_revision=1, media_source='themoviedb', media_id='117061',
                                          media_type='tv', season=2, episode=3)
    response = history_endpoint.replan_transfer_history(receipt.history_id, payload, executions, None)
    assert response.success and '等待后台' in response.message and response.data is None
    repeated = history_endpoint.replan_transfer_history(receipt.history_id, payload, executions, None)
    assert not repeated.success


@pytest.mark.parametrize('blocker', [None, 'ai_disabled', 'retry_disabled', 'source_access', 'destination_access',
                                     'execution', 'budget', 'task_budget', 'manual_review', 'running', 'file_step'])
def test_automatic_takeover_only_submits_safe_new_failures(replan_store, tmp_path, blocker):
    """
    自动接管只消费开关开启、预算未耗尽且无文件副作用的识别拒绝。
    """
    _admissions, executions, _factory, _writer = replan_store
    claimed, _planning, receipt = _failed_recognition(replan_store, tmp_path)
    snapshot = executions.get_snapshot(task_id=claimed.task_id)
    if blocker in {'manual_review', 'running'}:
        snapshot = replace(snapshot, state=TransferExecutionState(blocker))
    elif blocker == 'task_budget':
        snapshot = replace(snapshot, retry_count=10)
    elif blocker == 'file_step':
        snapshot = replace(snapshot, steps=(replace(snapshot.steps[0], phase='transfer', kind='materialize_target'),))
    chain = object.__new__(TransferChain)
    chain.runtime_config = SimpleNamespace(ai_agent_enable=blocker != 'ai_disabled',
                                           ai_agent_retry_transfer=blocker != 'retry_disabled')
    chain.transfer_execution_repository = Mock()
    chain.transfer_execution_repository.get_snapshot.return_value = snapshot
    chain._take_over_transfer_history_by_ai = Mock()
    chain._maybe_take_over_failed_transfer(
        history_id=receipt.history_id, task_id=claimed.task_id, error='未识别到媒体信息',
        failure_stage=blocker if blocker in {'source_access', 'destination_access', 'execution'} else None,
        retry_count=10 if blocker == 'budget' else 1, username='tester',
    )
    if blocker is None:
        chain._take_over_transfer_history_by_ai.assert_called_once_with(
            history_id=receipt.history_id, channel=None, source=None, userid=None, username='tester',
        )
    else:
        chain._take_over_transfer_history_by_ai.assert_not_called()
    chain.transfer_execution_repository.request_retry.assert_not_called()


@pytest.mark.parametrize('outcome', ['committed', 'duplicate', 'rollback'])
def test_failure_settlement_triggers_ai_only_after_new_commit(monkeypatch, outcome):
    """
    真实失败回调在提交后触发 AI；重复回执及事务失败均不重复接管。
    """
    from app.application.transfer.execution import TransferSettlementResult
    from tests.test_transfer_job_manager import FakeMedia, bind_terminal_checkpoint, make_task, make_transfer_chain

    chain = make_transfer_chain()
    chain.transfer_history_repository.get_by_src.return_value = None
    chain.jobview = Mock()
    chain.jobview.get_job_id.return_value = 'test-job'
    chain.jobview.is_finished.return_value = False
    chain.queue_failed_transfer_notification = Mock()
    chain._TransferChain__mark_torrent_completed_if_done = Mock()
    chain._maybe_take_over_failed_transfer = Mock()
    result = TransferSettlementResult(history_id=77, settlement_revision=1,
                                      pending_deleted=False, already_settled=outcome == 'duplicate')
    chain.durable_event_writer.transfer_result = Mock(
        side_effect=RuntimeError('write failed') if outcome == 'rollback' else None,
        return_value=result,
    )
    monkeypatch.setattr('app.chain.transfer.settlement.next_failed_retry_count', lambda *_args, **_kwargs: 1)
    monkeypatch.setattr('app.chain.transfer.settlement.record_transfer_failure', Mock())
    task = make_task(3)
    task.mediainfo = FakeMedia()
    info = TransferInfo(success=False, fileitem=task.fileitem, transfer_type='copy',
                        message='未识别到媒体信息', failure_stage='recognition')
    bind_terminal_checkpoint(task, info)
    if outcome == 'rollback':
        with pytest.raises(RuntimeError, match='write failed'):
            chain._TransferChain__default_callback(task, info)
    else:
        assert chain._TransferChain__default_callback(task, info)[0] is False
    if outcome == 'committed':
        assert task.terminal_settled
        chain._maybe_take_over_failed_transfer.assert_called_once_with(
            history_id=77, task_id=task.admission_task_id, error='未识别到媒体信息',
            failure_stage='recognition', retry_count=1, username=task.username,
        )
    else:
        chain._maybe_take_over_failed_transfer.assert_not_called()
