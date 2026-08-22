"""下载失败冷却切片显式 UoW 的回归测试。"""

from unittest.mock import MagicMock, patch

import pytest

from app.startup.ports.download_failure import TransactionalDownloadFailureRepository


def _session_factory(session: MagicMock):
    """返回支持 context manager 的固定会话工厂。"""
    session.__enter__.return_value = session
    session.__exit__.return_value = False
    return MagicMock(return_value=session)


def test_record_failure_commits_explicit_unit_of_work() -> None:
    """写入成功后只由适配器显式提交一次事务。"""
    session = MagicMock()
    oper = MagicMock()
    oper.record_failure.return_value = object()
    repository = TransactionalDownloadFailureRepository(_session_factory(session))

    with patch(
        "app.startup.ports.download_failure.DownloadFailureOper",
        return_value=oper,
    ):
        result = repository.record_failure("fp", "now", "next", title="片名")

    assert result is oper.record_failure.return_value
    session.commit.assert_called_once_with()
    session.rollback.assert_not_called()


def test_record_failure_rolls_back_explicit_unit_of_work() -> None:
    """写入异常时回滚并保留原始异常。"""
    session = MagicMock()
    oper = MagicMock()
    oper.record_failure.side_effect = ValueError("duplicate")
    repository = TransactionalDownloadFailureRepository(_session_factory(session))

    with patch(
        "app.startup.ports.download_failure.DownloadFailureOper",
        return_value=oper,
    ), pytest.raises(ValueError, match="duplicate"):
        repository.record_failure("fp", "now", "next")

    session.rollback.assert_called_once_with()
    session.commit.assert_not_called()
