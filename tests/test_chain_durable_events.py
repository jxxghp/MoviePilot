"""下载与整理 durable 事件的原子写入和对象恢复测试。"""

import json

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.application.orchestration.durable_events import (
    restore_download_added,
    restore_transfer_result,
    snapshot_download_added,
    snapshot_transfer_result,
)
from app.db.base import Base
from app.db.models.downloadhistory import DownloadFiles, DownloadHistory
from app.db.models.outbox import OutboxMessage
from app.db.models.transferhistory import TransferHistory
from app.domain.context import Context, MediaInfo, TorrentInfo
from app.domain.metainfo import MetaInfo
from app.schemas.file import FileItem
from app.schemas.transfer import TransferInfo
from app.schemas.types import MediaSource, MediaType
from app.startup.ports.chain_events import TransactionalChainDurableEventWriter


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
    restored_transfer = restore_transfer_result(transfer_snapshot)
    assert isinstance(restored_download["context"], Context)
    assert isinstance(restored_download["context"].media_info, MediaInfo)
    assert isinstance(restored_transfer["fileitem"], FileItem)
    assert type(restored_transfer["meta"]) is type(meta)
    assert isinstance(restored_transfer["mediainfo"], MediaInfo)
    assert isinstance(restored_transfer["transferinfo"], TransferInfo)


def test_download_history_and_event_intent_share_one_transaction():
    """下载历史、文件清单和 intent 提交后才执行通知与即时事件。"""
    factory = _session_factory()
    writer = TransactionalChainDurableEventWriter(factory)
    _, _, context, _, _ = _objects()
    calls = []

    writer.download_added(
        history_payload={
            "path": "/downloads/Demo.mkv",
            "type": MediaType.MOVIE.value,
            "title": "Demo",
            "download_hash": "hash-2",
        },
        file_payloads=[
            {
                "download_hash": "hash-2",
                "downloader": "qb",
                "fullpath": "/downloads/Demo.mkv",
                "savepath": "/downloads",
                "filepath": "Demo.mkv",
                "torrentname": "Demo torrent",
            }
        ],
        event_payload={
            "hash": "hash-2",
            "context": context,
            "username": "alice",
            "downloader": "qb",
            "episodes": [],
            "source": "manual",
        },
        after_commit=lambda: calls.append("after_commit"),
        publish=lambda payload: calls.append(("event", payload)),
    )

    with factory() as session:
        history = session.execute(select(DownloadHistory)).scalar_one()
        download_file = session.execute(select(DownloadFiles)).scalar_one()
        outbox = session.execute(select(OutboxMessage)).scalar_one()
    assert history.download_hash == "hash-2"
    assert download_file.fullpath == "/downloads/Demo.mkv"
    assert outbox.status == "completed"
    assert outbox.event_key == "download.added:qb:hash-2:v1"
    assert [call if isinstance(call, str) else call[0] for call in calls] == [
        "after_commit",
        "event",
    ]

    with pytest.raises(IntegrityError):
        writer.download_added(
            history_payload={
                "path": "/downloads/duplicate.mkv",
                "type": MediaType.MOVIE.value,
                "title": "Duplicate",
                "download_hash": "hash-2",
            },
            file_payloads=[],
            event_payload={
                "hash": "hash-2",
                "context": context,
                "downloader": "qb",
                "episodes": [],
            },
            after_commit=lambda: None,
            publish=lambda _payload: None,
        )
    with factory() as session:
        assert len(session.execute(select(DownloadHistory)).scalars().all()) == 1


def test_transfer_event_failure_leaves_committed_intent_pending():
    """整理历史提交后即时广播失败不得误删可供恢复的 pending intent。"""
    factory = _session_factory()
    writer = TransactionalChainDurableEventWriter(factory)
    meta, media, _, fileitem, transferinfo = _objects()

    def stage_history(repository):
        """通过应用历史端口名暂存一条成功整理记录。"""
        return repository.add_force(
            src=fileitem.path,
            src_storage=fileitem.storage,
            src_fileitem=fileitem.model_dump(mode="json"),
            dest=transferinfo.target_item.path,
            dest_storage=transferinfo.target_item.storage,
            dest_fileitem=transferinfo.target_item.model_dump(mode="json"),
            status=1,
        )

    def fail_publish(_payload):
        """模拟插件事件总线在业务提交后失败。"""
        raise RuntimeError("event failed")

    with pytest.raises(RuntimeError, match="event failed"):
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

    with factory() as session:
        history = session.execute(select(TransferHistory)).scalar_one()
        outbox = session.execute(select(OutboxMessage)).scalar_one()
    assert history.status is True
    assert outbox.status == "pending"
    assert outbox.event_key == f"transfer.completed:{history.id}:v1"
