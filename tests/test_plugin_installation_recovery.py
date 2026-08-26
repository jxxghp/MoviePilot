"""插件安装 journal 启动重放与阻断边界测试。"""

from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.application.plugin.declaration import PluginDeclaredMetadata
from app.application.plugin.identity import (
    PluginBindingBasis,
    PluginIdentity,
    PluginPayloadSourceType,
    TrustedPluginSourceType,
)
from app.application.plugin.recovery import (
    PluginInstallationRecoveryError,
    PluginInstallationRecoveryService,
)
from app.application.plugin.transaction import (
    PluginInstallationPhase,
    PluginInstallationRecord,
)

NOW = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)
RECEIPT = "sha256:" + "1" * 64


def _identity(*, revision: int = 2, receipt: str = RECEIPT) -> PluginIdentity:
    """构造一份已提交载荷身份。"""
    return PluginIdentity(
        plugin_id="DemoPlugin",
        normalized_plugin_id="demoplugin",
        trusted_source_type=TrustedPluginSourceType.OFFICIAL,
        trusted_source_key="github:jxxghp/moviepilot-plugins",
        binding_basis=PluginBindingBasis.OFFICIAL_DEFAULT,
        payload_source_type=PluginPayloadSourceType.OFFICIAL,
        payload_source_key="github:jxxghp/moviepilot-plugins",
        declared_version="2.0.0",
        package_generation="v3",
        declared_metadata=PluginDeclaredMetadata.from_package(
            {"name": "Demo", "v3": True, "v3t": True},
            declaration_version="2.0.0",
            manifest_matches_payload=True,
        ),
        payload_receipt=receipt,
        revision=revision,
        created_at=NOW,
        updated_at=NOW,
        bound_at=NOW,
        payload_applied_at=NOW,
    )


def _record(
    *,
    phase: PluginInstallationPhase,
    transaction_id: str = "txn-demo",
) -> PluginInstallationRecord:
    """构造 PREPARED 或 COMMITTED 恢复记录。"""
    committed = phase is PluginInstallationPhase.COMMITTED
    return PluginInstallationRecord(
        transaction_id=transaction_id,
        plugin_id="DemoPlugin",
        phase=phase,
        membership_before=True,
        membership_target=True if committed else None,
        identity_before_revision=1,
        identity_target_revision=2 if committed else None,
        package_existed=True,
        persistent_backup_existed=True,
        created_at=NOW,
        updated_at=NOW,
    )


class _Persistence:
    """保存恢复测试所需 journal、身份和删除故障。"""

    def __init__(
        self,
        records: list[PluginInstallationRecord],
        *,
        identity: PluginIdentity | None = None,
        delete_errors: list[Exception | None] | None = None,
    ) -> None:
        self.records = {record.transaction_id: record for record in records}
        self.identity = identity
        self.delete_errors = list(delete_errors or [])
        self.delete_calls: list[tuple[str, PluginInstallationPhase]] = []

    async def list_installations(self) -> list[PluginInstallationRecord]:
        """按创建顺序返回当前 journal。"""
        return list(self.records.values())

    async def get_identity(self, _plugin_id: str) -> PluginIdentity | None:
        """返回已提交身份。"""
        return self.identity

    async def delete_installation(
        self,
        transaction_id: str,
        *,
        expected_phase: PluginInstallationPhase,
    ) -> bool:
        """按 phase 删除 journal，并可注入一次性错误。"""
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
    """构造恢复服务消费的单一包事务端口。"""
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


@pytest.mark.asyncio
async def test_prepared_replay_restores_before_releasing_journal() -> None:
    """PREPARED 必须先恢复旧载荷，再删除 journal 和恢复材料。"""
    persistence = _Persistence([_record(phase=PluginInstallationPhase.PREPARED)])
    packages = _packages()
    service = PluginInstallationRecoveryService(
        persistence=persistence,
        packages=packages,
    )

    result = await service.replay()

    assert result.restored == 1
    assert persistence.records == {}
    packages.async_restore.assert_awaited_once()
    packages.async_cleanup.assert_awaited_once()
    assert persistence.delete_calls == [
        ("txn-demo", PluginInstallationPhase.PREPARED)
    ]


@pytest.mark.asyncio
async def test_prepared_delete_failure_keeps_replayable_journal() -> None:
    """恢复完成但 journal 删除失败时，下次启动仍可幂等重放。"""
    persistence = _Persistence(
        [_record(phase=PluginInstallationPhase.PREPARED)],
        delete_errors=[RuntimeError("database unavailable"), None],
    )
    packages = _packages()
    service = PluginInstallationRecoveryService(
        persistence=persistence,
        packages=packages,
    )

    with pytest.raises(PluginInstallationRecoveryError, match="未提交安装恢复失败"):
        await service.replay()
    assert "txn-demo" in persistence.records
    packages.async_cleanup.assert_not_awaited()

    result = await service.replay()

    assert result.restored == 1
    assert persistence.records == {}
    assert packages.async_restore.await_count == 2
    packages.async_cleanup.assert_awaited_once()


@pytest.mark.asyncio
async def test_prepared_restore_failure_blocks_plugin_import() -> None:
    """旧载荷无法恢复时必须保留 journal，并让启动阶段失败。"""
    persistence = _Persistence([_record(phase=PluginInstallationPhase.PREPARED)])
    packages = _packages(
        async_restore=AsyncMock(side_effect=RuntimeError("snapshot missing"))
    )
    service = PluginInstallationRecoveryService(
        persistence=persistence,
        packages=packages,
    )

    with pytest.raises(PluginInstallationRecoveryError, match="snapshot missing"):
        await service.replay()

    assert "txn-demo" in persistence.records
    assert persistence.delete_calls == []
    packages.async_cleanup.assert_not_awaited()


@pytest.mark.asyncio
async def test_committed_replay_verifies_identity_and_receipt_before_cleanup() -> None:
    """COMMITTED 只在身份 revision 和载荷收据一致时完成幂等收尾。"""
    persistence = _Persistence(
        [_record(phase=PluginInstallationPhase.COMMITTED)],
        identity=_identity(),
    )
    packages = _packages()
    service = PluginInstallationRecoveryService(
        persistence=persistence,
        packages=packages,
    )

    result = await service.replay()

    assert result.finalized == 1
    assert persistence.records == {}
    packages.async_committed_payload_receipt.assert_awaited_once()
    packages.async_finalize_persistent_backup.assert_awaited_once()
    packages.async_commit.assert_awaited_once()
    assert persistence.delete_calls == [
        ("txn-demo", PluginInstallationPhase.COMMITTED)
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("identity", "receipt", "message"),
    [
        (replace(_identity(), revision=3), RECEIPT, "身份与安装 journal 不一致"),
        (_identity(), "sha256:" + "2" * 64, "载荷收据不一致"),
    ],
)
async def test_committed_fact_mismatch_blocks_plugin_import(
    identity: PluginIdentity,
    receipt: str,
    message: str,
) -> None:
    """已提交数据库事实与可恢复载荷不一致时不得继续加载插件。"""
    persistence = _Persistence(
        [_record(phase=PluginInstallationPhase.COMMITTED)],
        identity=identity,
    )
    packages = _packages(
        async_committed_payload_receipt=AsyncMock(return_value=receipt)
    )
    service = PluginInstallationRecoveryService(
        persistence=persistence,
        packages=packages,
    )

    with pytest.raises(PluginInstallationRecoveryError, match=message):
        await service.replay()

    assert "txn-demo" in persistence.records
    packages.async_finalize_persistent_backup.assert_not_awaited()
    packages.async_commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_committed_cleanup_failure_is_retried_without_rollback() -> None:
    """COMMITTED 收尾失败只保留 journal，下一次启动继续清理。"""
    persistence = _Persistence(
        [_record(phase=PluginInstallationPhase.COMMITTED)],
        identity=_identity(),
    )
    package_commit = AsyncMock(
        side_effect=[RuntimeError("snapshot busy"), None]
    )
    packages = _packages(async_commit=package_commit)
    service = PluginInstallationRecoveryService(
        persistence=persistence,
        packages=packages,
    )

    first = await service.replay()

    assert first.cleanup_pending == 1
    assert "txn-demo" in persistence.records
    assert persistence.delete_calls == []

    second = await service.replay()

    assert second.finalized == 1
    assert persistence.records == {}
    assert packages.async_finalize_persistent_backup.await_count == 2
    assert package_commit.await_count == 2
