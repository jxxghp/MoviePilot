"""
验证自动发现保留既有持久任务，并继续处理同批未准入文件。
"""

from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.application.transfer.workflow import TransferPlanningInput
from app.chain.transfer.facade import TransferChain
from app.db.adapters.transfer.admission import TransactionalTransferAdmissionRepository
from app.db.models.transferhistory import TransferHistory
from app.db.models.transferpending import TransferPending
from app.schemas.file import FileItem
from tests.test_transfer_job_manager import make_transfer_chain


@pytest.fixture
def pending_source(tmp_path):
    """
    创建仅包含一个已准入源文件的独立仓储。
    """
    engine = create_engine(f"sqlite:///{tmp_path / 'pending.db'}")
    TransferHistory.__table__.create(engine)
    TransferPending.__table__.create(engine)
    factory = sessionmaker(bind=engine)
    repository = TransactionalTransferAdmissionRepository(factory)
    source = FileItem(storage="local", path="/downloads/E01.mkv", type="file",
                      name="E01.mkv", basename="E01", extension="mkv", size=1024)
    admission = repository.admit(
        storage=source.storage, src_path=source.path,
        planning_input=TransferPlanningInput(source_fileitem=source.model_dump(mode="json")),
    )
    chain = object.__new__(TransferChain)
    chain.transfer_admission_repository = repository
    try:
        yield SimpleNamespace(chain=chain, repository=repository, source=source,
                              admission=admission, factory=factory)
    finally:
        engine.dispose()


@pytest.mark.parametrize("state", ["not_started", "retry_wait", "settling", "manual_review", "failed"])
def test_automatic_discovery_leaves_existing_task_unchanged_and_keeps_new_members(pending_source, state):
    """
    等待、失败和结算中的原任务均不重新准入，同批新文件与其他存储仍可处理。
    """
    row = pending_source
    with row.factory() as session:
        pending = session.execute(select(TransferPending)).scalar_one()
        pending.execution_state = state
        session.commit()
        before = pending.to_dict()
    new = FileItem(storage="local", path="/downloads/E02.mkv", type="file")
    other_storage = row.source.model_copy(update={"storage": "other"})
    candidates = [(row.source, False), (new, False), (other_storage, False)]
    record = Mock()
    assert row.chain._filter_automatic_pending_transfers(candidates, True, record) == candidates[1:]
    record.assert_called_once()
    assert record.call_args.args[0] is row.source
    assert record.call_args.args[1] == "skipped"
    with row.factory() as session:
        assert session.execute(select(TransferPending)).scalar_one().to_dict() == before


def test_manual_preview_and_explicit_recovery_keep_their_candidates(pending_source):
    """
    禁用自动发现过滤时不读取仓储，也不改变原候选列表。
    """
    row = pending_source
    repository = Mock()
    repository.get_task_id_by_identity.side_effect = AssertionError("不应读取仓储")
    row.chain.transfer_admission_repository = repository
    candidates = [(row.source, False)]
    record = Mock()
    assert row.chain._filter_automatic_pending_transfers(candidates, False, record) is candidates
    record.assert_not_called()
    assert repository.mock_calls == []


def test_automatic_workflow_does_not_replan_or_readmit_existing_source(pending_source):
    """
    真实自动整理入口在规划和准入前跳过原任务，不把等待结算报告为本次新入队。
    """
    row = pending_source
    chain = make_transfer_chain()
    chain.transfer_admission_repository = row.repository
    chain._TransferChain__get_trans_fileitems = lambda *_args, **_kwargs: [(row.source, False)]
    chain.put_to_queue = Mock(side_effect=AssertionError("已有源文件不得重新准入"))
    with patch("app.chain.transfer.workflow.get_configured_system_config",
               return_value=SimpleNamespace(get=lambda _key: None)):
        state, _ = chain.do_transfer(fileitem=row.source, background=True, manual=False)
    assert state is True
    chain.put_to_queue.assert_not_called()
    chain._transfer_admissions.admit.assert_not_called()
    assert row.repository.get_task_id_by_identity(storage="local", src_path=row.source.path) == row.admission.task_id
