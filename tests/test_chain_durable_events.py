"""下载与整理 durable 事件的原子写入和对象恢复测试。"""

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from unittest.mock import Mock
from uuid import UUID

import pytest
from sqlalchemy import create_engine, delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.application.chain.events import (
    TransferResultSettlement,
    download_added_event_key,
    download_effect_event_key,
    restore_download_added,
    restore_download_processing,
    restore_transfer_result,
    snapshot_download_added,
    snapshot_download_processing,
    snapshot_transfer_result,
    transfer_result_event_key,
)
from app.application.history import (
    DownloadFileWrite,
    DownloadHistoryWrite,
    TransferHistoryMutationCommand,
    TransferHistoryWrite,
)
from app.application.outbox import (
    DOWNLOAD_ADDED_TOPIC,
    DOWNLOAD_MODULE_TOPIC,
    DOWNLOAD_NOTIFICATION_TOPIC,
    DOWNLOAD_SUBTITLE_TOPIC,
    PostCommitEffectError,
)
from app.application.transfer.execution import (
    TransferExecutionCheckpoint,
    TransferExecutionConflictError,
    TransferExecutionLeaseLostError,
    TransferSettlementResult,
    build_transfer_checkpoint_fingerprint,
)
from app.db.adapters.chain import TransactionalChainDurableEventWriter
from app.db.base import Base
from app.db.models.downloadhistory import DownloadFiles, DownloadHistory
from app.db.models.outbox import OutboxMessage
from app.db.models.transferexecutionstep import TransferExecutionStep
from app.db.models.transferhistory import TransferHistory
from app.db.models.transferpending import TransferPending
from app.db.models.transfersettlementreceipt import TransferSettlementReceipt
from app.db.oper.transferhistory import TransferHistoryOper
from app.db.uow import SqlAlchemyUnitOfWork
from app.domain.context import Context, MediaInfo, TorrentInfo
from app.domain.metainfo import MetaInfo
from app.schemas.file import FileItem
from app.schemas.transfer import TransferInfo
from app.schemas.types import MediaSource, MediaType


def _session_factory():
    """创建只服务当前测试的内存数据库和同步 Session 工厂。"""
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def _objects():
    """构造下载和整理事件共用的最小真实领域对象。"""
    meta = MetaInfo("Demo.2026.1080p.mkv")
    media = MediaInfo(
        type=MediaType.MOVIE,
        title="Demo",
        year="2026",
        media_source=MediaSource.TMDB,
        media_id="42",
    )
    torrent = TorrentInfo(title="Demo torrent", site_name="test")
    context = Context(meta_info=meta, media_info=media, torrent_info=torrent)
    fileitem = FileItem(
        storage="local",
        path="/downloads/Demo.mkv",
        name="Demo.mkv",
        type="file",
    )
    transferinfo = TransferInfo(
        success=True,
        fileitem=fileitem,
        target_item=FileItem(
            storage="local",
            path="/library/Demo (2026)/Demo.mkv",
            name="Demo.mkv",
            type="file",
        ),
        transfer_type="copy",
    )
    return meta, media, context, fileitem, transferinfo


def _execution_checkpoint(
    *,
    outcome: str,
    identity: str = "execution-1",
) -> TransferExecutionCheckpoint:
    """构造 outcome 与完整指纹一致的测试执行检查点。"""
    overwrite_skipped = outcome == "overwrite_skipped"
    transferinfo = (
        TransferInfo(success=False, overwrite_skipped=True).model_dump(mode="json")
        if overwrite_skipped
        else None
    )
    return TransferExecutionCheckpoint.create(
        payload={
            "outcome": outcome,
            "test_identity": identity,
            **({"transferinfo": transferinfo} if transferinfo else {}),
        },
        operation_ids=() if overwrite_skipped else ("operation-1",),
        skip_reason="overwrite_skipped" if overwrite_skipped else None,
    )


def _add_settling_pending(
    factory,
    *,
    task_id: str = "task-1",
    lease_token: str = "lease-1",
    execution_outcome: str = "succeeded",
    execution_identity: str = "execution-1",
    settlement_revision: int = 0,
    src_path: str | None = None,
) -> TransferExecutionCheckpoint:
    """写入具备有效长租约和执行检查点的待结算任务。"""
    checkpoint = _execution_checkpoint(
        outcome=execution_outcome,
        identity=execution_identity,
    )
    with factory() as session:
        session.add(TransferPending(
            task_id=task_id,
            storage="local",
            src_path=src_path or f"/downloads/{task_id}.mkv",
            created_at="2026-08-27 09:00:00",
            state="planned",
            updated_at="2026-08-27 09:00:00",
            input_version=1,
            planning_input={"schema_version": 1, "source": task_id},
            input_fingerprint=f"input-{task_id}",
            checkpoint_version=1,
            checkpoint_payload={"schema_version": 1, "task_id": task_id},
            planned_at="2026-08-27 09:00:00",
            lease_owner="worker-1",
            lease_token=lease_token,
            lease_expires_at="2099-01-01 00:00:00.000000",
            heartbeat_at="2026-08-27 01:00:00.000000",
            attempt_count=1,
            execution_state="settling",
            execution_version=checkpoint.version,
            execution_payload=checkpoint.to_payload(),
            execution_fingerprint=checkpoint.fingerprint,
            retry_generation=0,
            retry_count=0,
            settlement_revision=settlement_revision,
        ))
        session.commit()
    return checkpoint


def _settlement(
    *,
    outcome: str,
    task_id: str = "task-1",
    lease_token: str = "lease-1",
    checkpoint_outcome: str | None = None,
    execution_identity: str = "execution-1",
) -> TransferResultSettlement:
    """构造测试使用的稳定终态结算身份。"""
    checkpoint = _execution_checkpoint(
        outcome=checkpoint_outcome or outcome,
        identity=execution_identity,
    )
    return TransferResultSettlement(
        task_id=task_id,
        lease_token=lease_token,
        execution_fingerprint=checkpoint.fingerprint,
        outcome=outcome,
        error="目标文件校验失败" if outcome == "failed" else None,
    )


def _stage_result_history(
    repository,
    *,
    task_id: str,
    succeeded: bool,
    src_path: str | None = None,
):
    """通过类型化历史端口暂存一条最小任务结算记录。"""
    return repository.replace(TransferHistoryWrite(
        src=src_path or f"/downloads/{task_id}.mkv",
        src_storage="local",
        status=succeeded,
        errmsg=None if succeeded else "目标文件校验失败",
    ))


def _assert_event_key(
    event_key: str,
    topic: str,
    history_id: int,
) -> None:
    """校验事件键保留业务关联，并以合法 UUID 区分每次事实。"""
    key_topic, key_history_id, occurrence_id, version = event_key.split(":")
    assert key_topic == topic
    assert key_history_id == str(history_id)
    assert UUID(occurrence_id).hex == occurrence_id
    assert version == "v1"


def test_durable_snapshots_are_json_and_restore_plugin_runtime_objects():
    """outbox 只存 JSON 快照，重放时恢复插件一直收到的对象类型。"""
    meta, media, context, fileitem, transferinfo = _objects()
    download_payload = {
        "hash": "hash-1",
        "context": context,
        "username": "alice",
        "downloader": "qb",
        "episodes": [1, 2],
        "source": "manual",
        "idempotency_key": "download.added:qb:hash-1:v1",
    }
    transfer_payload = {
        "fileitem": fileitem,
        "meta": meta,
        "mediainfo": media,
        "transferinfo": transferinfo,
        "downloader": "qb",
        "download_hash": "hash-1",
        "transfer_history_id": 9,
        "idempotency_key": "transfer.completed:9:v1",
    }

    download_snapshot = snapshot_download_added(download_payload)
    transfer_snapshot = snapshot_transfer_result(transfer_payload)
    json.dumps(download_snapshot)
    json.dumps(transfer_snapshot)

    restored_download = restore_download_added(download_snapshot)
    processing_snapshot = snapshot_download_processing(
        context=context,
        download_dir=Path("/downloads"),
        torrent_content=b"torrent-bytes",
        download_hash="hash-1",
        downloader="qb",
    )
    restored_processing = restore_download_processing(processing_snapshot)
    restored_transfer = restore_transfer_result(transfer_snapshot)
    assert isinstance(restored_download["context"], Context)
    assert isinstance(restored_download["context"].media_info, MediaInfo)
    assert isinstance(restored_processing.context, Context)
    assert restored_processing.download_dir == Path("/downloads")
    assert restored_processing.torrent_content == b"torrent-bytes"
    assert restored_processing.download_hash == "hash-1"
    assert restored_processing.downloader == "qb"
    assert isinstance(restored_transfer["fileitem"], FileItem)
    assert type(restored_transfer["meta"]) is type(meta)
    assert isinstance(restored_transfer["mediainfo"], MediaInfo)
    assert isinstance(restored_transfer["transferinfo"], TransferInfo)


def test_download_history_and_event_intent_share_one_transaction():
    """下载历史、文件和四项具名 intent 在同一事务内提交。"""
    factory = _session_factory()
    writer = TransactionalChainDurableEventWriter(factory)
    _, _, context, _, _ = _objects()
    calls = []

    writer.download_added(
        history=DownloadHistoryWrite(
            path="/downloads/Demo.mkv",
            type=MediaType.MOVIE.value,
            title="Demo",
            download_hash="hash-2",
        ),
        files=(
            DownloadFileWrite(
                download_hash="hash-2",
                downloader="qb",
                fullpath="/downloads/Demo.mkv",
                savepath="/downloads",
                filepath="Demo.mkv",
                torrentname="Demo torrent",
            ),
        ),
        event_payload={
            "hash": "hash-2",
            "context": context,
            "username": "alice",
            "downloader": "qb",
            "episodes": [],
            "source": "manual",
        },
        notification_payload={"message": {"title": "下载完成"}},
        processing_payload=snapshot_download_processing(
            context=context,
            download_dir=Path("/downloads"),
            torrent_content=b"torrent",
        ),
        publish=lambda payload: calls.append(("event", payload)),
    )

    with factory() as session:
        history = session.execute(select(DownloadHistory)).scalar_one()
        download_file = session.execute(select(DownloadFiles)).scalar_one()
        outboxes = session.execute(
            select(OutboxMessage).order_by(OutboxMessage.id)
        ).scalars().all()
    assert history.download_hash == "hash-2"
    assert download_file.fullpath == "/downloads/Demo.mkv"
    assert [message.topic for message in outboxes] == [
        DOWNLOAD_ADDED_TOPIC,
        DOWNLOAD_NOTIFICATION_TOPIC,
        DOWNLOAD_MODULE_TOPIC,
        DOWNLOAD_SUBTITLE_TOPIC,
    ]
    assert [message.status for message in outboxes] == [
        "completed", "pending", "pending", "pending"
    ]
    for message in outboxes:
        _assert_event_key(message.event_key, message.topic, history.id)
    occurrence = outboxes[0].event_key.removeprefix(f"{DOWNLOAD_ADDED_TOPIC}:")
    assert all(message.event_key.endswith(occurrence) for message in outboxes)
    assert [call[0] for call in calls] == ["event"]

    writer.download_added(
        history=DownloadHistoryWrite(
            path="/downloads/duplicate.mkv",
            type=MediaType.MOVIE.value,
            title="Duplicate",
            download_hash="hash-2",
        ),
        files=(),
        event_payload={
            "hash": "hash-2",
            "context": context,
            "downloader": "qb",
            "episodes": [],
        },
        notification_payload={"message": {"title": "下载完成"}},
        processing_payload=snapshot_download_processing(
            context=context,
            download_dir=Path("/downloads"),
            torrent_content="magnet:?xt=demo",
        ),
        publish=lambda _payload: None,
    )
    with factory() as session:
        histories = session.execute(
            select(DownloadHistory).order_by(DownloadHistory.id)
        ).scalars().all()
        outboxes = session.execute(
            select(OutboxMessage).order_by(OutboxMessage.id)
        ).scalars().all()
    assert len(histories) == 2
    assert len(outboxes) == 8
    for history, offset in zip(histories, (0, 4), strict=True):
        group = outboxes[offset:offset + 4]
        for message in group:
            _assert_event_key(message.event_key, message.topic, history.id)
    assert outboxes[0].event_key != outboxes[4].event_key


def test_download_post_commit_effects_do_not_run_when_commit_fails(monkeypatch):
    """下载历史提交失败时不得执行通知、后处理或即时事件。"""
    factory = _session_factory()
    writer = TransactionalChainDurableEventWriter(factory)
    _, _, context, _, _ = _objects()
    calls: list[str] = []

    def fail_commit(_unit_of_work) -> None:
        """模拟历史与 outbox intent 的原子提交失败。"""
        raise RuntimeError("commit failed")

    monkeypatch.setattr(SqlAlchemyUnitOfWork, "commit", fail_commit)

    with pytest.raises(RuntimeError, match="commit failed"):
        writer.download_added(
            history=DownloadHistoryWrite(
                path="/downloads/Failed.mkv",
                type=MediaType.MOVIE.value,
                title="Failed",
                download_hash="hash-failed",
            ),
            files=(),
            event_payload={
                "hash": "hash-failed",
                "context": context,
                "episodes": [],
            },
            notification_payload={"message": {"title": "失败"}},
            processing_payload=snapshot_download_processing(
                context=context,
                download_dir=Path("/downloads"),
                torrent_content=b"torrent",
            ),
            publish=lambda _payload: calls.append("event"),
        )

    with factory() as session:
        assert session.execute(select(DownloadHistory)).scalar_one_or_none() is None
        assert session.execute(select(OutboxMessage)).scalar_one_or_none() is None
    assert calls == []


def test_event_keys_distinguish_reused_history_ids():
    """历史主键被数据库复用时，每次业务事实仍获得不同的幂等键。"""
    download_keys = {download_added_event_key(7) for _ in range(2)}
    transfer_keys = {
        transfer_result_event_key("transfer.completed", 7)
        for _ in range(2)
    }
    assert len(download_keys) == 2
    assert len(transfer_keys) == 2
    for event_key in download_keys:
        _assert_event_key(event_key, "download.added", 7)
    for event_key in transfer_keys:
        _assert_event_key(event_key, "transfer.completed", 7)
    base_key = next(iter(download_keys))
    assert download_effect_event_key(
        base_key, DOWNLOAD_MODULE_TOPIC
    ).endswith(base_key.removeprefix(f"{DOWNLOAD_ADDED_TOPIC}:"))


def test_task_settlement_event_key_is_deterministic_and_revision_scoped():
    """任务结算按稳定任务、修订号和结果生成可重放的唯一事件键。"""
    succeeded = TransferResultSettlement(
        task_id="task-1",
        lease_token="lease-1",
        execution_fingerprint="execution-1",
        outcome="succeeded",
    )
    failed = TransferResultSettlement(
        task_id="task-1",
        lease_token="lease-1",
        execution_fingerprint="execution-1",
        outcome="failed",
        error="目标文件校验失败",
    )

    assert transfer_result_event_key(
        "transfer.completed", 7, settlement=succeeded, settlement_revision=2
    ) == "transfer.result:task-1:2:succeeded:v1"
    assert transfer_result_event_key(
        "transfer.completed", 99, settlement=succeeded, settlement_revision=2
    ) == "transfer.result:task-1:2:succeeded:v1"
    assert transfer_result_event_key(
        "transfer.failed", 7, settlement=failed, settlement_revision=3
    ) == "transfer.result:task-1:3:failed:v1"


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"task_id": ""}, "缺少任务"),
        ({"outcome": "unknown"}, "不支持的整理终态"),
        ({"outcome": "failed", "error": None}, "必须包含可诊断原因"),
    ],
)
def test_task_settlement_rejects_incomplete_identity(kwargs, message):
    """任务结算在进入数据库适配器前拒绝不完整的 fencing 身份。"""
    values = {
        "task_id": "task-1",
        "lease_token": "lease-1",
        "execution_fingerprint": "execution-1",
        "outcome": "succeeded",
        "error": None,
    }
    values.update(kwargs)
    with pytest.raises(ValueError, match=message):
        TransferResultSettlement(**values)


def test_task_settlement_event_key_requires_transaction_revision():
    """任务结算事件键只能使用持久层已取得的正向修订号。"""
    settlement = TransferResultSettlement(
        task_id="task-1",
        lease_token="lease-1",
        execution_fingerprint="execution-1",
        outcome="succeeded",
    )
    with pytest.raises(ValueError, match="缺少有效结算修订号"):
        transfer_result_event_key(
            "transfer.completed",
            7,
            settlement=settlement,
        )


def test_transfer_succeeds_when_history_id_is_reused_with_retained_outbox():
    """整理历史删除而 outbox 保留时，复用主键不得阻断新整理记录。"""
    factory = _session_factory()
    writer = TransactionalChainDurableEventWriter(factory)

    def transfer(src: str):
        """写入一条最小成功整理事实并完成即时事件。"""
        return writer.transfer_result(
            topic="transfer.completed",
            stage_history=lambda repository: repository.replace(TransferHistoryWrite(
                src=src,
                src_storage="local",
                status=True,
            )),
            event_payload={},
            publish=lambda _payload: None,
        )

    first_history = transfer("/downloads/first.mkv")
    with factory() as session:
        session.execute(delete(TransferHistory))
        session.commit()

    second_history = transfer("/downloads/second.mkv")

    with factory() as session:
        histories = session.execute(select(TransferHistory)).scalars().all()
        outboxes = session.execute(
            select(OutboxMessage).order_by(OutboxMessage.id)
        ).scalars().all()
    assert first_history is not None
    assert second_history is not None
    assert first_history.id == second_history.id
    assert len(histories) == 1
    assert len(outboxes) == 2
    assert all(message.status == "completed" for message in outboxes)
    assert outboxes[0].event_key != outboxes[1].event_key
    for message in outboxes:
        _assert_event_key(
            message.event_key,
            "transfer.completed",
            second_history.id,
        )


def test_transfer_event_failure_leaves_committed_intent_pending():
    """整理历史提交后即时广播失败不得误删可供恢复的 pending intent。"""
    factory = _session_factory()
    writer = TransactionalChainDurableEventWriter(factory)
    meta, media, _, fileitem, transferinfo = _objects()

    def stage_history(repository):
        """通过应用历史端口名暂存一条成功整理记录。"""
        return repository.replace(TransferHistoryWrite(
            src=fileitem.path,
            src_storage=fileitem.storage,
            src_fileitem=fileitem.model_dump(mode="json"),
            dest=transferinfo.target_item.path,
            dest_storage=transferinfo.target_item.storage,
            dest_fileitem=transferinfo.target_item.model_dump(mode="json"),
            status=True,
        ))

    def fail_publish(_payload):
        """模拟插件事件总线在业务提交后失败。"""
        raise RuntimeError("event failed")

    with pytest.raises(PostCommitEffectError, match="提交后的相关处理未完成") as error:
        writer.transfer_result(
            topic="transfer.completed",
            stage_history=stage_history,
            event_payload={
                "fileitem": fileitem,
                "meta": meta,
                "mediainfo": media,
                "transferinfo": transferinfo,
                "downloader": "qb",
                "download_hash": "hash-3",
                "transfer_history_id": None,
            },
            publish=fail_publish,
        )
    assert len(error.value.errors) == 1
    assert str(error.value.errors[0]) == "event failed"

    with factory() as session:
        history = session.execute(select(TransferHistory)).scalar_one()
        outbox = session.execute(select(OutboxMessage)).scalar_one()
    assert history.status is True
    assert outbox.status == "pending"
    _assert_event_key(outbox.event_key, "transfer.completed", history.id)


def test_task_success_settlement_atomically_deletes_pending_and_steps():
    """成功终态原子提交历史、pending、步骤和待异步投递的 intent。"""
    factory = _session_factory()
    _add_settling_pending(factory)
    with factory() as session:
        session.add(TransferExecutionStep(
            task_id="task-1",
            operation_id="operation-1",
            checkpoint_fingerprint="plan-1",
            ordinal=0,
            phase="transfer",
            kind="copy",
            state="succeeded",
            attempt_count=1,
            intent_version=1,
            intent_payload={"src": "/downloads/task-1.mkv"},
            result_version=1,
            result_payload={"dest": "/library/task-1.mkv"},
            prepared_at="2026-08-27 09:00:00",
            completed_at="2026-08-27 09:01:00",
            updated_at="2026-08-27 09:01:00",
        ))
        session.commit()
    writer = TransactionalChainDurableEventWriter(factory)
    published = []

    def publish(payload):
        """验证即时发布只能观察到已提交的完整终态。"""
        with factory() as session:
            assert session.execute(select(TransferPending)).scalar_one_or_none() is None
            assert session.execute(select(TransferHistory)).scalar_one().status is True
            assert session.execute(select(OutboxMessage)).scalar_one().status == "pending"
        published.append(dict(payload))

    result = writer.transfer_result(
        topic="transfer.completed",
        stage_history=lambda repository: _stage_result_history(
            repository,
            task_id="task-1",
            succeeded=True,
        ),
        event_payload={},
        publish=publish,
        settlement=_settlement(outcome="succeeded"),
    )

    assert result == TransferSettlementResult(
        history_id=1,
        settlement_revision=1,
        pending_deleted=True,
    )
    with factory() as session:
        history = session.execute(select(TransferHistory)).scalar_one()
        receipt = session.execute(select(TransferSettlementReceipt)).scalar_one()
        outbox = session.execute(select(OutboxMessage)).scalar_one()
        assert session.execute(select(TransferPending)).scalar_one_or_none() is None
        assert session.execute(select(TransferExecutionStep)).scalar_one_or_none() is None
    assert history.transfer_task_id is None
    assert history.transfer_settlement_revision is None
    assert receipt.task_id == "task-1"
    assert receipt.history_id == history.id
    assert receipt.outcome == "succeeded"
    assert receipt.execution_fingerprint == _execution_checkpoint(
        outcome="succeeded"
    ).fingerprint
    assert receipt.lease_token == "lease-1"
    assert receipt.history_status is True
    assert receipt.src == "/downloads/task-1.mkv"
    assert receipt.src_storage == "local"
    assert receipt.pending_deleted is True
    assert outbox.event_key == "transfer.result:task-1:1:succeeded:v1"
    assert outbox.status == "pending"
    assert outbox.payload["idempotency_key"] == outbox.event_key
    assert "task_id" not in outbox.payload
    assert published == []


def test_task_success_replay_reads_history_without_new_event():
    """成功删除 pending 后重复结算只回读历史，不重复登记或发布事件。"""
    factory = _session_factory()
    _add_settling_pending(factory)
    writer = TransactionalChainDurableEventWriter(factory)
    settlement = _settlement(outcome="succeeded")
    calls = []

    first = writer.transfer_result(
        topic="transfer.completed",
        stage_history=lambda repository: _stage_result_history(
            repository,
            task_id="task-1",
            succeeded=True,
        ),
        event_payload={},
        publish=lambda _payload: calls.append("first"),
        settlement=settlement,
    )
    replay = writer.transfer_result(
        topic="transfer.completed",
        stage_history=lambda _repository: pytest.fail("幂等回读不得重写历史"),
        event_payload={},
        publish=lambda _payload: pytest.fail("幂等回读不得重复发布"),
        settlement=settlement,
    )

    assert isinstance(first, TransferSettlementResult)
    assert replay == TransferSettlementResult(
        history_id=first.history_id,
        settlement_revision=1,
        pending_deleted=True,
        already_settled=True,
    )
    with factory() as session:
        assert len(session.execute(select(TransferHistory)).scalars().all()) == 1
        assert len(session.execute(select(OutboxMessage)).scalars().all()) == 1
    assert calls == []


def test_multiple_same_source_tasks_keep_independent_replay_receipts():
    """同源多代任务可依次完成，旧任务仍由独立回执幂等回读。"""
    factory = _session_factory()
    writer = TransactionalChainDurableEventWriter(factory)
    shared_src = "/downloads/shared-generation.mkv"
    _add_settling_pending(
        factory,
        task_id="old-task",
        src_path=shared_src,
    )
    old_settlement = _settlement(outcome="succeeded", task_id="old-task")
    old_result = writer.transfer_result(
        topic="transfer.completed",
        stage_history=lambda repository: _stage_result_history(
            repository,
            task_id="old-task",
            succeeded=True,
            src_path=shared_src,
        ),
        event_payload={},
        publish=None,
        settlement=old_settlement,
    )
    _add_settling_pending(
        factory,
        task_id="new-task",
        lease_token="lease-2",
        execution_identity="execution-2",
        src_path=shared_src,
    )

    new_result = writer.transfer_result(
        topic="transfer.completed",
        stage_history=lambda repository: _stage_result_history(
            repository,
            task_id="new-task",
            succeeded=True,
            src_path=shared_src,
        ),
        event_payload={},
        publish=None,
        settlement=_settlement(
            outcome="succeeded",
            task_id="new-task",
            lease_token="lease-2",
            execution_identity="execution-2",
        ),
    )
    old_replay = writer.transfer_result(
        topic="transfer.completed",
        stage_history=lambda _repository: pytest.fail("旧任务不得改写最新投影"),
        event_payload={},
        publish=None,
        settlement=old_settlement,
    )

    with factory() as session:
        history = session.execute(select(TransferHistory)).scalar_one()
        receipts = session.execute(
            select(TransferSettlementReceipt).order_by(TransferSettlementReceipt.id)
        ).scalars().all()
        assert session.execute(select(TransferPending)).scalar_one_or_none() is None
    assert isinstance(old_result, TransferSettlementResult)
    assert isinstance(new_result, TransferSettlementResult)
    assert old_replay == TransferSettlementResult(
        history_id=old_result.history_id,
        settlement_revision=1,
        pending_deleted=True,
        already_settled=True,
    )
    assert history.transfer_task_id is None
    assert history.transfer_settlement_revision is None
    assert [receipt.task_id for receipt in receipts] == ["old-task", "new-task"]
    assert [receipt.history_id for receipt in receipts] == [history.id, history.id]


def test_task_settlement_without_public_topic_commits_no_outbox():
    """无公共事件的文件仍原子结算历史和 pending，且不登记或发布事件。"""
    factory = _session_factory()
    _add_settling_pending(factory, task_id="lyrics-task")
    writer = TransactionalChainDurableEventWriter(factory)

    result = writer.transfer_result(
        topic=None,
        stage_history=lambda repository: _stage_result_history(
            repository,
            task_id="lyrics-task",
            succeeded=True,
        ),
        event_payload={"unexpected": "must-not-publish"},
        publish=lambda _payload: pytest.fail("无公共 topic 不得发布"),
        settlement=_settlement(
            outcome="succeeded",
            task_id="lyrics-task",
        ),
    )

    assert result == TransferSettlementResult(
        history_id=1,
        settlement_revision=1,
        pending_deleted=True,
    )
    with factory() as session:
        history = session.execute(select(TransferHistory)).scalar_one()
        assert history.transfer_task_id is None
        assert session.execute(select(TransferPending)).scalar_one_or_none() is None
        assert session.execute(select(OutboxMessage)).scalar_one_or_none() is None


def test_task_settlement_binds_receipt_without_overwriting_success_history():
    """不覆盖裁决只绑定任务回执，保留旧成功历史的全部业务字段。"""
    factory = _session_factory()
    _add_settling_pending(
        factory,
        task_id="declined-task",
        execution_outcome="overwrite_skipped",
    )
    with factory() as session:
        session.add(TransferHistory(
            src="/downloads/declined-task.mkv",
            src_storage="local",
            dest="/library/original.mkv",
            title="Original",
            status=True,
            date="2026-08-26 20:00:00",
        ))
        session.commit()
    writer = TransactionalChainDurableEventWriter(factory)
    settlement = _settlement(
        outcome="succeeded",
        task_id="declined-task",
        checkpoint_outcome="overwrite_skipped",
    )

    first = writer.transfer_result(
        topic=None,
        stage_history=lambda repository: repository.get_success_by_src(
            "/downloads/declined-task.mkv",
            "local",
        ),
        event_payload={},
        publish=None,
        settlement=settlement,
    )
    replay = writer.transfer_result(
        topic=None,
        stage_history=lambda _repository: pytest.fail("回执重放不得重新查写历史"),
        event_payload={},
        publish=None,
        settlement=settlement,
    )

    assert isinstance(first, TransferSettlementResult)
    assert replay == TransferSettlementResult(
        history_id=first.history_id,
        settlement_revision=1,
        pending_deleted=True,
        already_settled=True,
    )
    with factory() as session:
        history = session.execute(select(TransferHistory)).scalar_one()
        assert session.execute(select(TransferPending)).scalar_one_or_none() is None
        assert session.execute(select(OutboxMessage)).scalar_one_or_none() is None
    assert history.dest == "/library/original.mkv"
    assert history.title == "Original"
    assert history.status is True
    assert history.date == "2026-08-26 20:00:00"
    assert history.transfer_task_id is None
    assert history.transfer_settlement_revision is None


def test_task_overwrite_skip_without_success_history_settles_failed():
    """覆盖跳过找不到成功历史时，显式执行事实仍可裁决为失败。"""
    factory = _session_factory()
    _add_settling_pending(factory, execution_outcome="overwrite_skipped")
    writer = TransactionalChainDurableEventWriter(factory)

    result = writer.transfer_result(
        topic="transfer.failed",
        stage_history=lambda repository: _stage_result_history(
            repository,
            task_id="task-1",
            succeeded=False,
        ),
        event_payload={},
        publish=None,
        settlement=_settlement(
            outcome="failed",
            checkpoint_outcome="overwrite_skipped",
        ),
    )

    assert isinstance(result, TransferSettlementResult)
    assert result.pending_deleted is False
    with factory() as session:
        pending = session.execute(select(TransferPending)).scalar_one()
        history = session.execute(select(TransferHistory)).scalar_one()
        receipt = session.execute(select(TransferSettlementReceipt)).scalar_one()
    assert pending.execution_state == "failed"
    assert history.status is False
    assert receipt.outcome == "failed"


@pytest.mark.parametrize(
    ("checkpoint_outcome", "settlement_outcome"),
    [
        ("failed", "succeeded"),
        ("succeeded", "failed"),
    ],
)
def test_task_settlement_rejects_checkpoint_outcome_conflicts_before_writes(
    checkpoint_outcome,
    settlement_outcome,
):
    """执行证据与结算方向冲突或未知时，不得进入历史暂存。"""
    factory = _session_factory()
    _add_settling_pending(
        factory,
        execution_outcome=checkpoint_outcome,
    )
    writer = TransactionalChainDurableEventWriter(factory)

    with pytest.raises(TransferExecutionConflictError):
        writer.transfer_result(
            topic="transfer.completed",
            stage_history=lambda _repository: pytest.fail("冲突结算不得写历史"),
            event_payload={},
            publish=lambda _payload: pytest.fail("冲突结算不得发布"),
            settlement=_settlement(
                outcome=settlement_outcome,
                checkpoint_outcome=checkpoint_outcome,
            ),
        )

    with factory() as session:
        pending = session.execute(select(TransferPending)).scalar_one()
        assert session.execute(select(TransferHistory)).scalar_one_or_none() is None
        assert session.execute(
            select(TransferSettlementReceipt)
        ).scalar_one_or_none() is None
        assert session.execute(select(OutboxMessage)).scalar_one_or_none() is None
    assert pending.execution_state == "settling"
    assert pending.settlement_revision == 0


def test_task_settlement_rejects_corrupted_checkpoint_outcome_before_writes():
    """持久层出现未知执行 outcome 时必须隔离，不得构造历史或事件。"""
    factory = _session_factory()
    _add_settling_pending(factory)
    corrupted_payload = {
        "schema_version": 1,
        "payload": {"outcome": "unknown", "test_identity": "execution-1"},
        "operation_ids": ["operation-1"],
        "skip_reason": None,
    }
    corrupted_fingerprint = build_transfer_checkpoint_fingerprint(
        corrupted_payload
    )
    with factory() as session:
        pending = session.execute(select(TransferPending)).scalar_one()
        pending.execution_payload = corrupted_payload
        pending.execution_fingerprint = corrupted_fingerprint
        session.commit()
    writer = TransactionalChainDurableEventWriter(factory)
    settlement = TransferResultSettlement(
        task_id="task-1",
        lease_token="lease-1",
        execution_fingerprint=corrupted_fingerprint,
        outcome="succeeded",
    )

    with pytest.raises(TransferExecutionConflictError):
        writer.transfer_result(
            topic="transfer.completed",
            stage_history=lambda _repository: pytest.fail("损坏检查点不得写历史"),
            event_payload={},
            publish=lambda _payload: pytest.fail("损坏检查点不得发布"),
            settlement=settlement,
        )

    with factory() as session:
        pending = session.execute(select(TransferPending)).scalar_one()
        assert session.execute(select(TransferHistory)).scalar_one_or_none() is None
        assert session.execute(
            select(TransferSettlementReceipt)
        ).scalar_one_or_none() is None
        assert session.execute(select(OutboxMessage)).scalar_one_or_none() is None
    assert pending.execution_state == "settling"
    assert pending.settlement_revision == 0


def test_task_settlement_rejects_malformed_checkpoint_before_writes():
    """指纹自洽但结构损坏的数据库检查点也不得驱动终态写入。"""
    factory = _session_factory()
    _add_settling_pending(factory)
    malformed_payload = {
        "schema_version": 1,
        "payload": {"outcome": "succeeded"},
        "operation_ids": "operation-1",
        "skip_reason": None,
    }
    fingerprint = build_transfer_checkpoint_fingerprint(malformed_payload)
    with factory() as session:
        pending = session.execute(select(TransferPending)).scalar_one()
        pending.execution_payload = malformed_payload
        pending.execution_fingerprint = fingerprint
        session.commit()
    writer = TransactionalChainDurableEventWriter(factory)
    settlement = TransferResultSettlement(
        task_id="task-1",
        lease_token="lease-1",
        execution_fingerprint=fingerprint,
        outcome="succeeded",
    )

    with pytest.raises(TransferExecutionConflictError):
        writer.transfer_result(
            topic="transfer.completed",
            stage_history=lambda _repository: pytest.fail("损坏检查点不得写历史"),
            event_payload={},
            publish=lambda _payload: pytest.fail("损坏检查点不得发布"),
            settlement=settlement,
        )

    with factory() as session:
        pending = session.execute(select(TransferPending)).scalar_one()
        assert session.execute(select(TransferHistory)).scalar_one_or_none() is None
        assert session.execute(
            select(TransferSettlementReceipt)
        ).scalar_one_or_none() is None
        assert session.execute(select(OutboxMessage)).scalar_one_or_none() is None
    assert pending.execution_state == "settling"
    assert pending.settlement_revision == 0


@pytest.mark.parametrize("cleanup", ["delete", "truncate"])
def test_receipt_replay_survives_real_history_command_cleanup(cleanup):
    """真实历史删除或清空命令执行后，独立回执仍可重放成功终态。"""
    factory = _session_factory()
    _add_settling_pending(factory, task_id="cleanup-task")
    writer = TransactionalChainDurableEventWriter(factory)
    settlement = _settlement(outcome="succeeded", task_id="cleanup-task")
    first = writer.transfer_result(
        topic=None,
        stage_history=lambda repository: _stage_result_history(
            repository,
            task_id="cleanup-task",
            succeeded=True,
        ),
        event_payload={},
        publish=None,
        settlement=settlement,
    )
    with factory() as session:
        command = TransferHistoryMutationCommand(
            repository=TransferHistoryOper(session),
            download_repository=Mock(),
            transfer_execution_repository=Mock(),
            unit_of_work=SqlAlchemyUnitOfWork(session),
            file_item_factory=Mock(),
            delete_media_file=Mock(return_value=True),
            publish_download_file_deleted=Mock(),
            clear_failures=Mock(),
        )
        cleanup_result = (
            command.delete(first.history_id)
            if cleanup == "delete"
            else command.truncate()
        )
        assert cleanup_result.success is True

    replay = writer.transfer_result(
        topic=None,
        stage_history=lambda _repository: pytest.fail("清理后重放不得重建历史"),
        event_payload={},
        publish=None,
        settlement=settlement,
    )

    with factory() as session:
        assert session.execute(select(TransferHistory)).scalar_one_or_none() is None
        receipts = session.execute(
            select(TransferSettlementReceipt)
            .order_by(TransferSettlementReceipt.settlement_revision)
        ).scalars().all()
    assert isinstance(first, TransferSettlementResult)
    assert replay == TransferSettlementResult(
        history_id=first.history_id,
        settlement_revision=1,
        pending_deleted=True,
        already_settled=True,
    )
    assert len(receipts) == 1
    assert receipts[0].task_id == "cleanup-task"
    assert receipts[0].history_id == first.history_id


def test_success_receipt_allows_expiry_and_legacy_same_source_replace():
    """成功回执不锁死业务历史，过期清理和旧兼容替换仍按原契约工作。"""
    factory = _session_factory()
    shared_src = "/downloads/cleanup-compatible.mkv"
    _add_settling_pending(
        factory,
        task_id="compatible-task",
        src_path=shared_src,
    )
    writer = TransactionalChainDurableEventWriter(factory)
    settlement = _settlement(outcome="succeeded", task_id="compatible-task")
    first = writer.transfer_result(
        topic=None,
        stage_history=lambda repository: _stage_result_history(
            repository,
            task_id="compatible-task",
            succeeded=True,
            src_path=shared_src,
        ),
        event_payload={},
        publish=None,
        settlement=settlement,
    )

    with factory() as session:
        assert TransferHistory.delete_before(
            session,
            before_time="9999-12-31 23:59:59",
            limit=100,
        ) == 1
        session.commit()
        legacy = TransferHistoryOper(session).stage_replace_by_src(
            src=shared_src,
            src_storage="local",
            status=True,
        )
        session.commit()
        assert legacy.transfer_task_id is None

    replay = writer.transfer_result(
        topic=None,
        stage_history=lambda _repository: pytest.fail("旧兼容替换后不得重写历史"),
        event_payload={},
        publish=None,
        settlement=settlement,
    )
    assert isinstance(first, TransferSettlementResult)
    assert replay == TransferSettlementResult(
        history_id=first.history_id,
        settlement_revision=1,
        pending_deleted=True,
        already_settled=True,
    )


def test_task_failure_settlement_is_replayable_and_retry_advances_revision():
    """失败保留终态证据，重复调用幂等，显式重试后才递增修订号。"""
    factory = _session_factory()
    _add_settling_pending(factory, execution_outcome="failed")
    writer = TransactionalChainDurableEventWriter(factory)
    calls = []
    first_settlement = _settlement(outcome="failed")

    first = writer.transfer_result(
        topic="transfer.failed",
        stage_history=lambda repository: _stage_result_history(
            repository,
            task_id="task-1",
            succeeded=False,
        ),
        event_payload={},
        publish=lambda _payload: calls.append("first"),
        settlement=first_settlement,
    )
    replay = writer.transfer_result(
        topic="transfer.failed",
        stage_history=lambda _repository: pytest.fail("失败回读不得重写历史"),
        event_payload={},
        publish=lambda _payload: pytest.fail("失败回读不得重复发布"),
        settlement=first_settlement,
    )
    assert isinstance(first, TransferSettlementResult)
    assert replay.already_settled is True
    assert replay.settlement_revision == 1

    with factory() as session:
        pending = session.execute(select(TransferPending)).scalar_one()
        assert pending.execution_state == "failed"
        assert pending.lease_token is None
        assert pending.settlement_revision == 1
        assert pending.terminal_history_id == first.history_id
        retry_checkpoint = _execution_checkpoint(
            outcome="failed",
            identity="execution-2",
        )
        pending.execution_state = "settling"
        pending.execution_version = retry_checkpoint.version
        pending.execution_payload = retry_checkpoint.to_payload()
        pending.execution_fingerprint = retry_checkpoint.fingerprint
        pending.lease_owner = "worker-2"
        pending.lease_token = "lease-2"
        pending.lease_expires_at = "2099-01-01 00:00:00.000000"
        session.commit()

    retried = writer.transfer_result(
        topic="transfer.failed",
        stage_history=lambda repository: _stage_result_history(
            repository,
            task_id="task-1",
            succeeded=False,
        ),
        event_payload={},
        publish=lambda _payload: calls.append("retry"),
        settlement=_settlement(
            outcome="failed",
            lease_token="lease-2",
            execution_identity="execution-2",
        ),
    )

    assert retried == TransferSettlementResult(
        history_id=first.history_id,
        settlement_revision=2,
        pending_deleted=False,
    )
    stale_replay = writer.transfer_result(
        topic="transfer.failed",
        stage_history=lambda _repository: pytest.fail(
            "旧修订延迟重放不得覆盖最新历史"
        ),
        event_payload={},
        publish=None,
        settlement=first_settlement,
    )
    assert stale_replay == TransferSettlementResult(
        history_id=first.history_id,
        settlement_revision=1,
        pending_deleted=False,
        already_settled=True,
    )
    with factory() as session:
        histories = session.execute(select(TransferHistory)).scalars().all()
        outboxes = session.execute(
            select(OutboxMessage).order_by(OutboxMessage.id)
        ).scalars().all()
        pending = session.execute(select(TransferPending)).scalar_one()
        receipts = session.execute(
            select(TransferSettlementReceipt)
            .order_by(TransferSettlementReceipt.settlement_revision)
        ).scalars().all()
    assert len(histories) == 1
    assert histories[0].transfer_settlement_revision == 2
    assert pending.settlement_revision == 2
    assert [receipt.settlement_revision for receipt in receipts] == [1, 2]
    assert all(receipt.task_id == "task-1" for receipt in receipts)
    assert all(receipt.history_id == first.history_id for receipt in receipts)
    assert receipts[0].execution_fingerprint == _execution_checkpoint(
        outcome="failed"
    ).fingerprint
    assert receipts[0].lease_token == "lease-1"
    assert receipts[1].outcome == "failed"
    assert receipts[1].execution_fingerprint == _execution_checkpoint(
        outcome="failed",
        identity="execution-2",
    ).fingerprint
    assert receipts[1].lease_token == "lease-2"
    assert receipts[1].pending_deleted is False
    assert receipts[1].error == "目标文件校验失败"
    assert [item.event_key for item in outboxes] == [
        "transfer.result:task-1:1:failed:v1",
        "transfer.result:task-1:2:failed:v1",
    ]
    assert calls == []


def test_failed_revision_replays_after_later_success_deleted_pending():
    """后续重试成功删除 pending 后，旧失败修订仍按原执行身份幂等回读。"""
    factory = _session_factory()
    _add_settling_pending(factory, execution_outcome="failed")
    writer = TransactionalChainDurableEventWriter(factory)
    failed_settlement = _settlement(outcome="failed")
    failed = writer.transfer_result(
        topic="transfer.failed",
        stage_history=lambda repository: _stage_result_history(
            repository,
            task_id="task-1",
            succeeded=False,
        ),
        event_payload={},
        publish=None,
        settlement=failed_settlement,
    )
    with factory() as session:
        pending = session.execute(select(TransferPending)).scalar_one()
        retry_checkpoint = _execution_checkpoint(
            outcome="succeeded",
            identity="execution-2",
        )
        pending.execution_state = "settling"
        pending.execution_version = retry_checkpoint.version
        pending.execution_payload = retry_checkpoint.to_payload()
        pending.execution_fingerprint = retry_checkpoint.fingerprint
        pending.lease_owner = "worker-2"
        pending.lease_token = "lease-2"
        pending.lease_expires_at = "2099-01-01 00:00:00.000000"
        session.commit()
    succeeded_settlement = _settlement(
        outcome="succeeded",
        lease_token="lease-2",
        execution_identity="execution-2",
    )
    succeeded = writer.transfer_result(
        topic="transfer.completed",
        stage_history=lambda repository: _stage_result_history(
            repository,
            task_id="task-1",
            succeeded=True,
        ),
        event_payload={},
        publish=None,
        settlement=succeeded_settlement,
    )

    stale_replay = writer.transfer_result(
        topic="transfer.failed",
        stage_history=lambda _repository: pytest.fail("旧失败回执不得重写历史"),
        event_payload={},
        publish=None,
        settlement=failed_settlement,
    )
    success_replay = writer.transfer_result(
        topic="transfer.completed",
        stage_history=lambda _repository: pytest.fail("成功回执不得重写历史"),
        event_payload={},
        publish=None,
        settlement=succeeded_settlement,
    )

    assert isinstance(failed, TransferSettlementResult)
    assert isinstance(succeeded, TransferSettlementResult)
    assert stale_replay == TransferSettlementResult(
        history_id=failed.history_id,
        settlement_revision=1,
        pending_deleted=False,
        already_settled=True,
    )
    assert success_replay == TransferSettlementResult(
        history_id=succeeded.history_id,
        settlement_revision=2,
        pending_deleted=True,
        already_settled=True,
    )
    with factory() as session:
        history = session.execute(select(TransferHistory)).scalar_one()
        receipts = session.execute(
            select(TransferSettlementReceipt)
            .order_by(TransferSettlementReceipt.settlement_revision)
        ).scalars().all()
        assert session.execute(select(TransferPending)).scalar_one_or_none() is None
    assert history.transfer_task_id is None
    assert history.transfer_settlement_revision is None
    assert [receipt.outcome for receipt in receipts] == ["failed", "succeeded"]


def test_task_settlement_outbox_conflict_rolls_back_history_and_pending():
    """intent 唯一键冲突时回滚此前已暂存的历史与 pending 终态。"""
    factory = _session_factory()
    _add_settling_pending(factory)
    event_key = "transfer.result:task-1:1:succeeded:v1"
    with factory() as session:
        session.add(OutboxMessage(
            event_key=event_key,
            topic="transfer.completed",
            payload_version=1,
            payload={},
            status="completed",
            attempt=0,
            next_retry_at="2026-08-27T01:00:00+00:00",
            created_at="2026-08-27T01:00:00+00:00",
        ))
        session.commit()
    writer = TransactionalChainDurableEventWriter(factory)

    with pytest.raises(IntegrityError):
        writer.transfer_result(
            topic="transfer.completed",
            stage_history=lambda repository: _stage_result_history(
                repository,
                task_id="task-1",
                succeeded=True,
            ),
            event_payload={},
            publish=lambda _payload: pytest.fail("事务失败不得发布"),
            settlement=_settlement(outcome="succeeded"),
        )

    with factory() as session:
        pending = session.execute(select(TransferPending)).scalar_one()
        histories = session.execute(select(TransferHistory)).scalars().all()
        receipts = session.execute(select(TransferSettlementReceipt)).scalars().all()
        outboxes = session.execute(select(OutboxMessage)).scalars().all()
    assert pending.execution_state == "settling"
    assert pending.settlement_revision == 0
    assert pending.lease_token == "lease-1"
    assert histories == []
    assert receipts == []
    assert len(outboxes) == 1
    assert outboxes[0].event_key == event_key


def test_task_settlement_rejects_stale_lease_without_business_writes():
    """陈旧 lease 在历史回调前即被 fencing，不能留下历史或 intent。"""
    factory = _session_factory()
    _add_settling_pending(factory)
    writer = TransactionalChainDurableEventWriter(factory)

    with pytest.raises(TransferExecutionLeaseLostError):
        writer.transfer_result(
            topic="transfer.completed",
            stage_history=lambda _repository: pytest.fail("陈旧 lease 不得写历史"),
            event_payload={},
            publish=lambda _payload: pytest.fail("陈旧 lease 不得发布"),
            settlement=_settlement(
                outcome="succeeded",
                lease_token="stale-lease",
            ),
        )

    with factory() as session:
        pending = session.execute(select(TransferPending)).scalar_one()
        assert session.execute(select(TransferHistory)).scalar_one_or_none() is None
        assert session.execute(
            select(TransferSettlementReceipt)
        ).scalar_one_or_none() is None
        assert session.execute(select(OutboxMessage)).scalar_one_or_none() is None
    assert pending.execution_state == "settling"
    assert pending.lease_token == "lease-1"


def test_concurrent_duplicate_settlement_returns_one_commit_and_one_replay(
        tmp_path,
        monkeypatch,
):
    """并发重复结算只有一个事务写入，竞争输家回读同一不可变回执。"""
    engine = create_engine(
        f"sqlite+pysqlite:///{tmp_path / 'settlement-race.db'}",
        connect_args={"check_same_thread": False, "timeout": 10},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    _add_settling_pending(factory)
    writer = TransactionalChainDurableEventWriter(factory)
    settlement = _settlement(outcome="succeeded")
    barrier = Barrier(2)
    original_read = TransactionalChainDurableEventWriter._read_settlement_result

    def synchronized_read(**kwargs):
        """让两个调用都先观察到未结算，再同时进入事务竞争。"""
        result = original_read(**kwargs)
        if result is None:
            barrier.wait(timeout=10)
        return result

    monkeypatch.setattr(
        TransactionalChainDurableEventWriter,
        "_read_settlement_result",
        staticmethod(synchronized_read),
    )

    def settle_once():
        """用相同 fencing 身份提交同一成功终态。"""
        return writer.transfer_result(
            topic=None,
            stage_history=lambda repository: _stage_result_history(
                repository,
                task_id="task-1",
                succeeded=True,
            ),
            event_payload={},
            publish=None,
            settlement=settlement,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _index: settle_once(), range(2)))

    assert sorted(result.already_settled for result in results) == [False, True]
    assert {result.history_id for result in results} == {1}
    with factory() as session:
        assert len(session.execute(select(TransferHistory)).scalars().all()) == 1
        assert len(
            session.execute(select(TransferSettlementReceipt)).scalars().all()
        ) == 1
        assert session.execute(select(TransferPending)).scalar_one_or_none() is None
