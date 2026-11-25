"""
插件安装恢复的持久日志与包端口；不注册测试用例。
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from app.application.plugin.identity import PluginIdentity
from app.application.plugin.transaction import PluginInstallationPhase, PluginInstallationRecord

RECEIPT = "sha256:" + "1" * 64


class _Persistence:
    """
    保存恢复测试所需 journal、身份和删除故障。
    """

    def __init__(
        self,
        records: list[PluginInstallationRecord],
        *,
        identity: PluginIdentity | None = None,
        delete_errors: list[Exception | None] | None = None,
    ) -> None:
        """
        初始化本用例独立持有的替身状态与可观察记录。

        :param records: 用于恢复的持久事务记录
        :param identity: 本次运行使用的冻结身份快照
        :param delete_errors: 按调用顺序注入的删除异常队列
        """
        self.records = {record.transaction_id: record for record in records}
        self.identity = identity
        self.delete_errors = list(delete_errors or [])
        self.delete_calls: list[tuple[str, PluginInstallationPhase]] = []

    async def list_installations(self) -> list[PluginInstallationRecord]:
        """
        按创建顺序返回当前 journal。
        """
        return list(self.records.values())

    async def get_identity(self, _plugin_id: str) -> PluginIdentity | None:
        """
        返回已提交身份。
        """
        return self.identity

    async def delete_installation(
        self,
        transaction_id: str,
        *,
        expected_phase: PluginInstallationPhase,
    ) -> bool:
        """
        按 phase 删除 journal，并可注入一次性错误。
        """
        self.delete_calls.append((transaction_id, expected_phase))
        if self.delete_errors:
            error = self.delete_errors.pop(0)
            if error is not None:
                raise error
        record = self.records.get(transaction_id)
        if record is None:
            return False
        assert record.phase is expected_phase
        del self.records[transaction_id]
        return True


def _packages(**overrides):
    """
    构造恢复服务消费的单一包事务端口。
    """
    checkpoint = SimpleNamespace(
        plugin_existed=True,
        persistent_backup_existed=True,
    )
    values = {
        "restore_checkpoint": Mock(return_value=checkpoint),
        "async_restore": AsyncMock(),
        "async_cleanup": AsyncMock(),
        "async_committed_payload_receipt": AsyncMock(return_value=RECEIPT),
        "async_finalize_persistent_backup": AsyncMock(),
        "async_commit": AsyncMock(),
    }
    values.update(overrides)
    return SimpleNamespace(**values)
