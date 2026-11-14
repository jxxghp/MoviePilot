"""
验证数据库清理按保留期、批大小和开关处理记录及关联文件。
"""

from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.application.database import (
    DatabaseGovernance,
    configure_database_governance,
    get_database_governance,
    reset_database_governance,
)
from app.application.maintenance import (
    DataCleanupService,
    read_cleanup_policy,
)
from app.db.base import Base
from app.db.maintenance import DatabaseCleanupRepository
from app.db.models.downloadhistory import DownloadFiles, DownloadHistory
from app.db.models.message import Message
from app.db.models.siteuserdata import SiteUserData
from app.db.models.transferhistory import TransferHistory
from app.runtime.config import settings
from app.scheduler.chain import SchedulerChain


@pytest.fixture
def session_factory(tmp_path):
    """
    在临时 SQLite 数据库创建表，并在用例后恢复数据库治理门面。

    :param tmp_path: pytest 提供的用例独立目录
    :yields: 仅连接本用例数据库的会话工厂
    """
    try:
        original_governance = get_database_governance()
    except RuntimeError:
        original_governance = None
    db_path = tmp_path / "cleanup.db"
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        factory = sessionmaker(bind=engine)
        Base.metadata.create_all(bind=engine)
        yield factory
    finally:
        engine.dispose()
        if original_governance is None:
            reset_database_governance()
        else:
            configure_database_governance(original_governance)


def _cleanup_settings(**overrides):
    """
    临时覆盖清理开关和保留期，退出上下文时恢复原配置。

    :param overrides: 当前用例需要覆盖的清理配置
    :return: 管理配置恢复的补丁上下文
    """
    defaults = {
        "DATA_CLEANUP_ENABLE": True,
        "DATA_CLEANUP_MESSAGE_DAYS": 90,
        "DATA_CLEANUP_DOWNLOAD_HISTORY_DAYS": 180,
        "DATA_CLEANUP_SITE_USERDATA_DAYS": 180,
        "DATA_CLEANUP_TRANSFER_HISTORY_DAYS": 365 * 3,
    }
    defaults.update(overrides)
    return patch.multiple(settings, **defaults)


def _configure_cleanup_service(session_factory):
    """
    把当前测试数据库注入统一数据库治理门面。
    """
    configure_database_governance(
        DatabaseGovernance(
            health=MagicMock(),
            backup=MagicMock(),
            cleanup=DataCleanupService(
                repository=DatabaseCleanupRepository(
                    session_factory=session_factory,
                ),
                policy_reader=read_cleanup_policy,
            ),
        )
    )


def test_cleanup_removes_expired_rows_in_batches(session_factory) -> None:
    """
    指定表应按保留期分批删除，并保留仍在有效期内的数据。
    """
    now = datetime.now()
    old_message_time = (now - timedelta(days=120)).strftime("%Y-%m-%d %H:%M:%S")
    keep_message_time = (now - timedelta(days=10)).strftime("%Y-%m-%d %H:%M:%S")
    old_download_time = (now - timedelta(days=240)).strftime("%Y-%m-%d %H:%M:%S")
    keep_download_time = (now - timedelta(days=20)).strftime("%Y-%m-%d %H:%M:%S")
    old_site_day = (now - timedelta(days=240)).strftime("%Y-%m-%d")
    keep_site_day = (now - timedelta(days=2)).strftime("%Y-%m-%d")
    old_transfer_time = (now - timedelta(days=365 * 3 + 30)).strftime("%Y-%m-%d %H:%M:%S")
    keep_transfer_time = (now - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")

    with session_factory() as db:
        db.add_all(
            [
                Message(reg_time=old_message_time, title="old-1"),
                Message(reg_time=old_message_time, title="old-2"),
                Message(reg_time=old_message_time, title="old-3"),
                Message(reg_time=keep_message_time, title="keep"),
            ]
        )
        db.add_all(
            [
                DownloadHistory(
                    path="/downloads/old-1",
                    type="电影",
                    title="old-1",
                    download_hash="hash-old-1",
                    date=old_download_time,
                ),
                DownloadHistory(
                    path="/downloads/old-2",
                    type="电影",
                    title="old-2",
                    download_hash="hash-old-2",
                    date=old_download_time,
                ),
                DownloadHistory(
                    path="/downloads/keep",
                    type="电影",
                    title="keep",
                    download_hash="hash-keep",
                    date=keep_download_time,
                ),
            ]
        )
        db.add_all(
            [
                DownloadFiles(
                    download_hash="hash-old-1",
                    fullpath="/downloads/old-1/file.mkv",
                    savepath="/downloads/old-1",
                    filepath="file.mkv",
                ),
                DownloadFiles(
                    download_hash="hash-old-2",
                    fullpath="/downloads/old-2/file.mkv",
                    savepath="/downloads/old-2",
                    filepath="file.mkv",
                ),
                DownloadFiles(
                    download_hash="hash-keep",
                    fullpath="/downloads/keep/file.mkv",
                    savepath="/downloads/keep",
                    filepath="file.mkv",
                ),
                DownloadFiles(
                    download_hash="hash-orphan",
                    fullpath="/downloads/orphan/file.mkv",
                    savepath="/downloads/orphan",
                    filepath="file.mkv",
                ),
            ]
        )
        db.add_all(
            [
                SiteUserData(domain="old-1", name="old-1", updated_day=old_site_day),
                SiteUserData(domain="old-2", name="old-2", updated_day=old_site_day),
                SiteUserData(domain="keep", name="keep", updated_day=keep_site_day),
            ]
        )
        db.add_all(
            [
                TransferHistory(
                    src="/src/old",
                    title="old",
                    date=old_transfer_time,
                ),
                TransferHistory(
                    src="/src/keep",
                    title="keep",
                    date=keep_transfer_time,
                ),
            ]
        )
        db.commit()

    with _cleanup_settings():
        _configure_cleanup_service(session_factory)
        report = SchedulerChain().cleanup(batch_size=1)

    assert report["tables"]["message"]["deleted"] == 3
    assert report["tables"]["message"]["batches"] == 3
    assert report["tables"]["downloadhistory"]["deleted"] == 2
    assert report["tables"]["downloadfiles"]["deleted"] == 3
    assert report["tables"]["siteuserdata"]["deleted"] == 2
    assert report["tables"]["transferhistory"]["deleted"] == 1

    with session_factory() as db:
        assert db.query(Message).count() == 1
        assert db.query(DownloadHistory).count() == 1
        assert db.query(DownloadFiles).count() == 1
        assert db.query(SiteUserData).count() == 1
        assert db.query(TransferHistory).count() == 1

        keep_download_file = db.query(DownloadFiles).first()
        assert keep_download_file.download_hash == "hash-keep"


def test_transferhistory_keeps_boundary_records(session_factory) -> None:
    """
    恰好位于保留边界上的整理历史不应被提前清理。
    """
    now = datetime.now()
    cutoff_time = (now - timedelta(days=365 * 3)).strftime("%Y-%m-%d %H:%M:%S")

    with session_factory() as db:
        db.add(
            TransferHistory(
                src="/src/boundary",
                title="boundary",
                date=cutoff_time,
            )
        )
        db.commit()

    with _cleanup_settings():
        _configure_cleanup_service(session_factory)
        report = SchedulerChain().cleanup(batch_size=10)

    assert report["tables"]["transferhistory"]["deleted"] == 0

    with session_factory() as db:
        assert db.query(TransferHistory).count() == 1


def test_cleanup_skips_when_disabled(session_factory) -> None:
    """
    总开关关闭时应跳过清理。
    """
    now = datetime.now()
    old_message_time = (now - timedelta(days=120)).strftime("%Y-%m-%d %H:%M:%S")

    with session_factory() as db:
        db.add(Message(reg_time=old_message_time, title="old"))
        db.commit()

    with _cleanup_settings(DATA_CLEANUP_ENABLE=False):
        _configure_cleanup_service(session_factory)
        report = SchedulerChain().cleanup(batch_size=10)

    assert not report["enabled"]
    assert report["skipped_reason"] == "disabled"
    assert report["total_deleted"] == 0

    with session_factory() as db:
        assert db.query(Message).count() == 1


def test_cleanup_respects_per_table_retention_days(session_factory) -> None:
    """
    各表保留期应使用当前配置值。
    """
    now = datetime.now()
    old_message_time = (now - timedelta(days=10)).strftime("%Y-%m-%d %H:%M:%S")
    keep_message_time = (now - timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S")

    with session_factory() as db:
        db.add_all(
            [
                Message(reg_time=old_message_time, title="old"),
                Message(reg_time=keep_message_time, title="keep"),
            ]
        )
        db.commit()

    with _cleanup_settings(DATA_CLEANUP_MESSAGE_DAYS=7):
        _configure_cleanup_service(session_factory)
        report = SchedulerChain().cleanup(batch_size=10)

    assert report["tables"]["message"]["retention_days"] == 7
    assert report["tables"]["message"]["deleted"] == 1

    with session_factory() as db:
        assert db.query(Message).count() == 1


def test_cleanup_skips_table_when_retention_days_is_zero(session_factory) -> None:
    """
    单表保留期为 0 时应跳过该表及其附属孤儿记录清理。
    """
    now = datetime.now()
    old_download_time = (now - timedelta(days=240)).strftime("%Y-%m-%d %H:%M:%S")

    with session_factory() as db:
        db.add(
            DownloadHistory(
                path="/downloads/old",
                type="电影",
                title="old",
                download_hash="hash-old",
                date=old_download_time,
            )
        )
        db.add(
            DownloadFiles(
                download_hash="hash-orphan",
                fullpath="/downloads/orphan/file.mkv",
                savepath="/downloads/orphan",
                filepath="file.mkv",
            )
        )
        db.commit()

    with _cleanup_settings(DATA_CLEANUP_DOWNLOAD_HISTORY_DAYS=0):
        _configure_cleanup_service(session_factory)
        report = SchedulerChain().cleanup(batch_size=10)

    assert report["tables"]["downloadhistory"]["skipped"]
    assert report["tables"]["downloadfiles"]["skipped"]

    with session_factory() as db:
        assert db.query(DownloadHistory).count() == 1
        assert db.query(DownloadFiles).count() == 1
