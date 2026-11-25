"""
安全覆盖检查点、执行与持久仓储载具；不注册测试用例。
"""

from datetime import datetime, timezone
from pathlib import Path
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.application.transfer.workflow import TransferPlanCheckpoint, TransferPlanItem, TransferPlanningInput
from app.chain.transfer import execution as execution_module
from app.db.adapters.transfer.execution import TransactionalTransferExecutionRepository
from app.db.base import Base
from app.db.models.transferexecutionstep import TransferExecutionStep
from app.db.models.transferhistory import TransferHistory
from app.db.models.transferpending import TransferPending
from app.domain.context import MediaInfo
from app.domain.meta.metabase import MetaBase
from app.modules.filemanager.storages.local import LocalStorage
from app.modules.filemanager.transhandler import TransHandler
from app.schemas.types import MediaType


def _checkpoint(source: Path, target: Path, *, transfer_type="copy", mode="always"):
    """
    构造使用真实本地文件的冻结单文件计划。
    """
    source_item = object.__new__(LocalStorage).get_item_strict(source).model_dump(mode="json")
    planning_input = TransferPlanningInput(source_fileitem=source_item)
    return TransferPlanCheckpoint(
        planning_input=planning_input, target_storage="local",
        root_target_path=target.parent.as_posix(), final_target_path=target.as_posix(),
        resolved_transfer_type=transfer_type, overwrite_mode=mode,
        items=(TransferPlanItem(
            sequence=0, source_fileitem=source_item,
            target_storage="local", target_path=target.as_posix(),
        ),),
    )


def _execute(checkpoint, monkeypatch, runner):
    """
    使用真实本地存储执行计划，隔离插件及进度展示。
    """
    storage = object.__new__(LocalStorage)
    handler = TransHandler()
    monkeypatch.setattr(handler, "_TransHandler__intercept_transfer", lambda **_kwargs: (True, ""))
    monkeypatch.setattr(LocalStorage, "_LocalStorage__should_show_progress", lambda *_args: False)
    return handler.execute_transfer_plan(
        checkpoint, meta=MetaBase("Movie.2026.mkv"),
        mediainfo=MediaInfo(type=MediaType.MOVIE, title="Movie", year="2026"),
        source_oper=storage, target_oper=storage, step_runner=runner,
    )


@pytest.fixture
def repository_factory():
    """
    为真实文件操作建立独立 SQLite 账本并绑定实际冻结计划。
    """
    engines = []

    def create(checkpoint):
        """
        创建带有效租约的单任务步骤仓储。
        """
        engine = create_engine("sqlite+pysqlite:///:memory:")
        engines.append(engine)
        Base.metadata.create_all(engine, tables=[
            TransferPending.__table__, TransferHistory.__table__, TransferExecutionStep.__table__,
        ])
        factory = sessionmaker(bind=engine, expire_on_commit=False)
        planning_input = checkpoint.planning_input
        with factory() as session:
            session.add(TransferPending(
                task_id="safe-overwrite", storage="local", src_path=planning_input.source_fileitem["path"],
                created_at="2026-08-27 09:00:00", updated_at="2026-08-27 09:00:00", state="planned",
                input_version=1, planning_input=planning_input.to_payload(),
                input_fingerprint=planning_input.fingerprint,
                checkpoint_version=checkpoint.schema_version, checkpoint_payload=checkpoint.to_payload(),
                planned_at="2026-08-27 09:00:00", lease_owner="worker", lease_token="lease",
                lease_expires_at="2099-01-01 00:00:00.000000", heartbeat_at="2026-08-27 01:00:00.000000",
                attempt_count=1, execution_state="not_started", retry_generation=0,
                retry_count=0, settlement_revision=0,
            ))
            session.commit()
        return TransactionalTransferExecutionRepository(
            factory, local_clock=lambda: datetime(2026, 8, 27, 9, 30, 0),
            lease_clock=lambda: datetime(2026, 8, 27, 1, 30, 0, tzinfo=timezone.utc),
        )

    yield create
    for engine in engines:
        engine.dispose()


def _durable_runner(repository, checkpoint):
    """
    在每次重启后以同一个冻结计划重建真实持久 runner。
    """
    return execution_module._DurableTransferStepRunner(
        task_id="safe-overwrite", lease_token="lease",
        checkpoint_fingerprint=checkpoint.fingerprint, repository=repository,
    )
