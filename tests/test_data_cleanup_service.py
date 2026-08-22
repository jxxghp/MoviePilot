import ast
from contextlib import nullcontext
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.application.maintenance import CleanupPolicy, DataCleanupService
from app.application.orchestration.scheduler import SchedulerChain


class FakeCleanupRepository:
    """记录维护用例调用，并可模拟单表失败。"""

    def __init__(self, *, failing_table: str | None = None) -> None:
        """保存故障表名并初始化调用记录。"""
        self.failing_table = failing_table
        self.calls: list[str] = []
        self._message_results = iter((2, 1, 0))
        self.commits = 0
        self.rollbacks = 0

    def session(self):
        """返回无需真实数据库的上下文。"""
        return nullcontext(object())

    def unit_of_work(self, db):
        """返回记录提交和回滚次数的测试事务边界。"""
        return self

    def commit(self) -> None:
        """记录一个成功清理批次。"""
        self.commits += 1

    def rollback(self) -> None:
        """记录一个失败清理批次。"""
        self.rollbacks += 1

    def _delete(self, name: str) -> int:
        """记录删除调用并按配置模拟结果或异常。"""
        self.calls.append(name)
        if name == self.failing_table:
            raise RuntimeError("boom")
        if name == "message":
            return next(self._message_results)
        return 0

    def delete_messages(self, db, cutoff: str, limit: int) -> int:
        """模拟消息删除。"""
        return self._delete("message")

    def delete_download_history(self, db, cutoff: str, limit: int) -> int:
        """模拟下载历史删除。"""
        return self._delete("downloadhistory")

    def delete_download_orphans(self, db, limit: int) -> int:
        """模拟孤儿文件删除。"""
        return self._delete("downloadfiles")

    def delete_site_userdata(self, db, cutoff: str, limit: int) -> int:
        """模拟站点用户数据删除。"""
        return self._delete("siteuserdata")

    def delete_transfer_history(self, db, cutoff: str, limit: int) -> int:
        """模拟整理历史删除。"""
        return self._delete("transferhistory")

    def delete_download_failures(self, db, cutoff: str, limit: int) -> int:
        """模拟下载失败记录删除。"""
        return self._delete("downloadfailure")


def _policy(**overrides) -> CleanupPolicy:
    """构造所有表默认启用的测试策略。"""
    values = {
        "enabled": True,
        "message_days": 1,
        "download_history_days": 1,
        "site_userdata_days": 1,
        "transfer_history_days": 1,
        "download_failure_days": 1,
    }
    values.update(overrides)
    return CleanupPolicy(**values)


def test_cleanup_service_owns_batching_report_and_progress() -> None:
    """应用服务应独立完成分批循环、报告和进度语义。"""
    repository = FakeCleanupRepository()
    progress = MagicMock()
    service = DataCleanupService(
        repository=repository,
        policy_reader=_policy,
        clock=lambda: datetime(2026, 8, 17, 12, 0, 0),
    )

    report = service.execute(batch_size=2, progress_callback=progress)

    assert report["started_at"] == "2026-08-17 12:00:00"
    assert report["tables"]["message"]["deleted"] == 3
    assert report["tables"]["message"]["batches"] == 2
    assert report["total_deleted"] == 3
    assert repository.commits == 2
    assert repository.rollbacks == 0
    assert repository.calls == [
        "message",
        "message",
        "message",
        "downloadhistory",
        "downloadfiles",
        "siteuserdata",
        "transferhistory",
        "downloadfailure",
    ]
    assert progress.call_args.kwargs["value"] == 100


def test_cleanup_service_finishes_other_tables_before_raising_partial_failure() -> None:
    """单表失败应被汇总，后续表仍继续处理，再按旧契约抛出异常。"""
    repository = FakeCleanupRepository(failing_table="downloadhistory")
    service = DataCleanupService(
        repository=repository,
        policy_reader=_policy,
        clock=lambda: datetime(2026, 8, 17, 12, 0, 0),
    )

    with pytest.raises(RuntimeError, match="downloadhistory: boom"):
        service.execute(batch_size=2)

    assert repository.calls[-1] == "downloadfailure"
    assert repository.rollbacks == 1


def test_scheduler_cleanup_is_a_compatibility_delegate() -> None:
    """旧 SchedulerChain 入口应原样转发参数和返回值。"""
    governance = MagicMock()
    governance.cleanup.return_value = {"enabled": True}
    progress = MagicMock()

    with patch("app.application.orchestration.scheduler.get_database_governance", return_value=governance):
        result = SchedulerChain().cleanup(batch_size=7, progress_callback=progress)

    assert result == {"enabled": True}
    governance.cleanup.assert_called_once_with(
        batch_size=7,
        progress_callback=progress,
    )


def test_scheduler_does_not_reclaim_database_cleanup_ownership() -> None:
    """调度模块不得重新导入清理模型或数据库会话。"""
    scheduler_root = Path(__file__).parents[1] / "app" / "scheduler"
    imports = {
        node.module
        for path in sorted(scheduler_root.glob("*.py"))
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.ImportFrom) and node.module
    }

    assert "app.db.session" not in imports
    assert not any(module.startswith("app.db.models") for module in imports)
