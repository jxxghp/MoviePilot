"""下载失败冷却切片显式 UoW 的回归测试。"""

from dataclasses import FrozenInstanceError
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.application.download.failures import (
    DownloadFailureSnapshot,
    DownloadFailureWrite,
)
from app.db.adapters.download import TransactionalDownloadFailureRepository
from app.db.base import Base
from app.db.models.downloadfailure import DownloadFailure
from app.db.session import SessionFactory


def _session_factory(session: MagicMock) -> MagicMock:
    """返回支持 context manager 的固定会话工厂。"""
    session.__enter__.return_value = session
    session.__exit__.return_value = False
    return MagicMock(return_value=session)


def _failure_write() -> DownloadFailureWrite:
    """构造事务测试共用的最小下载失败写入 DTO。"""
    return DownloadFailureWrite(
        fingerprint="fp",
        failed_at="now",
        next_retry_at="next",
        title="片名",
    )


@pytest.fixture
def real_session_factory(tmp_path):
    """创建隔离的下载失败冷却数据库与会话工厂。"""
    engine = create_engine(f"sqlite:///{tmp_path / 'download-failure.db'}")
    factory = sessionmaker(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield factory
    engine.dispose()


def test_real_session_round_trip_returns_detached_snapshot(real_session_factory) -> None:
    """真实写入提交后，查询结果在 Session 关闭后仍是可读冻结快照。"""
    repository = TransactionalDownloadFailureRepository(real_session_factory)

    assert repository.record_failure(_failure_write()) is None
    result = repository.get_active_by_fingerprints(["fp"], "before-next")

    assert result == {
        "fp": DownloadFailureSnapshot(
            fingerprint="fp",
            error_message=None,
            next_retry_at="next",
        )
    }
    with pytest.raises(FrozenInstanceError):
        result["fp"].next_retry_at = "changed"


def test_active_failures_are_projected_to_detached_frozen_snapshots() -> None:
    """只读适配器必须在会话关闭前投影，不得返回 ORM 记录。"""
    session = MagicMock()
    record = SimpleNamespace(
        fingerprint="fp",
        error_message="无法读取种子文件",
        next_retry_at="next",
    )
    oper = MagicMock()
    oper.get_active_by_fingerprints.return_value = {"fp": record}
    repository = TransactionalDownloadFailureRepository(_session_factory(session))

    with patch(
        "app.db.adapters.download.DownloadFailureOper",
        return_value=oper,
    ):
        result = repository.get_active_by_fingerprints(["fp"], "now")

    snapshot = result["fp"]
    assert snapshot == DownloadFailureSnapshot(
        fingerprint="fp",
        error_message="无法读取种子文件",
        next_retry_at="next",
    )
    assert snapshot is not record
    with pytest.raises(FrozenInstanceError):
        setattr(snapshot, "error_message", "changed")
    session.__exit__.assert_called_once()


def test_sqlite_snapshot_survives_repository_session_close(db) -> None:
    """真实 SQLite 查询关闭适配器 Session 后仍可完整读取快照。"""
    db.watermark(DownloadFailure)
    repository = TransactionalDownloadFailureRepository(SessionFactory)
    repository.record_failure(DownloadFailureWrite(
        fingerprint="typed-detached-snapshot",
        failed_at="2026-08-28 00:00:00",
        next_retry_at="2026-08-29 00:00:00",
        error_message="无法读取种子文件",
    ))

    snapshots = repository.get_active_by_fingerprints(
        ["typed-detached-snapshot"],
        "2026-08-28 12:00:00",
    )

    assert snapshots["typed-detached-snapshot"] == DownloadFailureSnapshot(
        fingerprint="typed-detached-snapshot",
        error_message="无法读取种子文件",
        next_retry_at="2026-08-29 00:00:00",
    )


def test_record_failure_commits_explicit_unit_of_work() -> None:
    """写入成功后只由适配器显式提交一次事务。"""
    session = MagicMock()
    oper = MagicMock()
    repository = TransactionalDownloadFailureRepository(_session_factory(session))

    with patch(
        "app.db.adapters.download.DownloadFailureOper",
        return_value=oper,
    ):
        result = repository.record_failure(_failure_write())

    assert result is None
    assert oper.record_failure.call_args.kwargs["fingerprint"] == "fp"
    assert oper.record_failure.call_args.kwargs["now_time"] == "now"
    assert oper.record_failure.call_args.kwargs["next_retry_at"] == "next"
    assert oper.record_failure.call_args.kwargs["title"] == "片名"
    session.commit.assert_called_once_with()
    session.rollback.assert_not_called()


def test_record_failure_rolls_back_explicit_unit_of_work() -> None:
    """写入异常时回滚并保留原始异常。"""
    session = MagicMock()
    oper = MagicMock()
    oper.record_failure.side_effect = ValueError("duplicate")
    repository = TransactionalDownloadFailureRepository(_session_factory(session))

    with patch(
        "app.db.adapters.download.DownloadFailureOper",
        return_value=oper,
    ), pytest.raises(ValueError, match="duplicate"):
        repository.record_failure(_failure_write())

    session.rollback.assert_called_once_with()
    session.commit.assert_not_called()


def test_record_failure_rolls_back_commit_failure() -> None:
    """提交失败时也必须回滚并传播提交异常。"""
    session = MagicMock()
    session.commit.side_effect = RuntimeError("commit failed")
    repository = TransactionalDownloadFailureRepository(_session_factory(session))

    with patch("app.db.adapters.download.DownloadFailureOper"), pytest.raises(
        RuntimeError,
        match="commit failed",
    ):
        repository.record_failure(_failure_write())

    session.commit.assert_called_once_with()
    session.rollback.assert_called_once_with()
