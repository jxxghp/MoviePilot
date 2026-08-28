"""插件安装事务用例的端到端副作用顺序与补偿测试。"""

import asyncio
from contextlib import nullcontext
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, Mock

import pytest

from app.application.plugin.admission import (
    PluginInstallAdmission,
    PluginInstallAdmissionRequest,
    admit_plugin_install,
)
from app.application.plugin.declaration import PluginDeclaredMetadata
from app.application.plugin.identity import (
    PluginBindingBasis,
    PluginIdentity,
    PluginPayloadSourceType,
    TrustedPluginSourceType,
)
from app.application.plugin.install import PluginInstallCommand
from app.application.plugin.recovery import PluginInstallationRecoveryService
from app.application.plugin.source import (
    CandidateInventory,
    MarketRead,
    PluginMarketCandidate,
)
from app.application.plugin.transaction import (
    PluginInstallationConflictError,
    PluginInstallationPhase,
    PluginInstallationRecord,
)
from app.runtime.native_dependencies import NativeDependencyChange
from app.schemas.exception import (
    DatabaseWorkerClosedError,
    PersistenceUnavailableError,
    PluginMutationRejectedError,
)
from app.schemas.plugin import PluginRuntimeStatus

NOW = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)
REPO_URL = "https://github.com/jxxghp/MoviePilot-Plugins"
SOURCE_KEY = "github:jxxghp/moviepilot-plugins"
RECEIPT = "sha256:" + "a" * 64


def _identity(*, version: str = "1.0.0", revision: int = 1) -> PluginIdentity:
    """构造与市场候选匹配的已提交来源身份。"""
    return PluginIdentity(
        plugin_id="DemoPlugin",
        normalized_plugin_id="demoplugin",
        trusted_source_type=TrustedPluginSourceType.OFFICIAL,
        trusted_source_key=SOURCE_KEY,
        binding_basis=PluginBindingBasis.OFFICIAL_DEFAULT,
        payload_source_type=PluginPayloadSourceType.OFFICIAL,
        payload_source_key=SOURCE_KEY,
        declared_version=version,
        package_generation="v3",
        declared_metadata=PluginDeclaredMetadata.from_package(
            {"name": "Demo", "v3": True, "v3t": False},
            declaration_version=version,
            manifest_matches_payload=True,
        ),
        payload_receipt=RECEIPT,
        revision=revision,
        created_at=NOW,
        updated_at=NOW,
        bound_at=NOW,
        payload_applied_at=NOW,
    )


def _admission(
    *,
    identity: PluginIdentity | None = None,
    version: str = "1.0.0",
) -> PluginInstallAdmission:
    """冻结一个可供安装用例消费的官方 V3 候选。"""
    candidate = PluginMarketCandidate(
        plugin_id="DemoPlugin",
        source_key=SOURCE_KEY,
        source_type=TrustedPluginSourceType.OFFICIAL,
        repo_url=REPO_URL,
        package_generation="v3",
        plugin_version=version,
        dto={"v3": True},
    )
    inventory = CandidateInventory(
        (
            MarketRead.present(
                REPO_URL,
                (candidate,),
                package_generation="v3",
            ),
        )
    )
    return admit_plugin_install(
        inventory,
        request=PluginInstallAdmissionRequest(
            plugin_id="DemoPlugin",
            generations=("v3", "v2", "v1"),
            requested_repo_url=REPO_URL,
            explicit_source=identity is None,
        ),
        identity=identity,
        now=NOW,
    )


class _PersistenceSpy:
    """记录安装 journal 的异步状态转移，并允许注入持久化失败。"""

    def __init__(
        self,
        calls: list[str],
        *,
        create_error: Exception | None = None,
        target_error: Exception | None = None,
        commit_error: Exception | None = None,
        delete_error: Exception | None = None,
        identity: PluginIdentity | None = None,
    ) -> None:
        self.calls = calls
        self.records: dict[str, PluginInstallationRecord] = {}
        self.create_error = create_error
        self.target_error = target_error
        self.commit_error = commit_error
        self.delete_error = delete_error
        self.identity = identity

    async def create_installation(
        self,
        record: PluginInstallationRecord,
    ) -> PluginInstallationRecord:
        """保存 PREPARED journal。"""
        self.calls.append("journal_create")
        if self.create_error:
            raise self.create_error
        if any(
            existing.plugin_id.lower() == record.plugin_id.lower()
            and existing.phase
            in {
                PluginInstallationPhase.PREPARED,
                PluginInstallationPhase.COMMITTED,
            }
            for existing in self.records.values()
        ):
            raise PluginInstallationConflictError(
                f"插件 {record.plugin_id} 存在未收尾安装事务"
            )
        self.records[record.transaction_id] = record
        return record

    async def get_installation(
        self,
        transaction_id: str,
    ) -> PluginInstallationRecord | None:
        """返回 journal 创建结果，模拟超时后的确认读取。"""
        self.calls.append("journal_get")
        return self.records.get(transaction_id)

    async def list_installations(self) -> list[PluginInstallationRecord]:
        """返回当前未收尾 journal，供连续事务恢复测试复用。"""
        return list(self.records.values())

    async def get_identity(self, _plugin_id: str) -> PluginIdentity | None:
        """返回恢复核验使用的当前身份事实。"""
        return self.identity

    async def set_installation_target(
        self,
        transaction_id: str,
        *,
        membership_target: bool,
        identity_target: PluginIdentity | None,
    ) -> PluginInstallationRecord:
        """保存最终 membership 与身份 revision。"""
        self.calls.append("journal_target")
        if self.target_error:
            raise self.target_error
        record = self.records[transaction_id]
        record = replace(
            record,
            membership_target=membership_target,
            identity_target_revision=(
                identity_target.revision if identity_target else None
            ),
        )
        self.records[transaction_id] = record
        return record

    async def commit_installation(
        self,
        transaction_id: str,
        *,
        identity_target: PluginIdentity | None,
    ) -> PluginInstallationRecord:
        """把 journal 推进到 COMMITTED。"""
        self.calls.append("journal_commit")
        if self.commit_error:
            raise self.commit_error
        if identity_target is not None:
            self.identity = identity_target
        record = self.records[transaction_id]
        record = replace(
            record,
            phase=PluginInstallationPhase.COMMITTED,
            membership_target=True,
            identity_target_revision=(
                identity_target.revision if identity_target else None
            ),
        )
        self.records[transaction_id] = record
        return record

    async def delete_installation(
        self,
        transaction_id: str,
        *,
        expected_phase: PluginInstallationPhase,
    ) -> bool:
        """按 phase 删除已补偿或已收尾 journal。"""
        self.calls.append("journal_delete")
        if self.delete_error:
            raise self.delete_error
        record = self.records.get(transaction_id)
        if record is None or record.phase is not expected_phase:
            return False
        del self.records[transaction_id]
        return True


def _command(
    *,
    persistence: _PersistenceSpy | None = None,
    installed: list[str] | None = None,
    plugin_ids: list[str] | None = None,
    installer=None,
    checkpointer=None,
    package_restore=None,
    package_rollback=None,
    package_cleanup=None,
    package_stage_backup=None,
    package_activate_backup=None,
    package_finalize_backup=None,
    package_commit=None,
    payload_receipt=None,
    native_dependency_changes=None,
    reporter=None,
    target_reloader=None,
    rollback_reloader=None,
    registration_refresher=None,
    mutation=None,
    package_write_guard=None,
    restart_required_recorder=None,
    transaction_id: str = "txn-demo",
) -> tuple[PluginInstallCommand, _PersistenceSpy, list[str]]:
    """构造只含窄端口的安装命令，并返回可观测调用记录。"""
    calls: list[str] = persistence.calls if persistence else []
    persistence = persistence or _PersistenceSpy(calls)
    checkpoint = SimpleNamespace(
        plugin_existed=False,
        persistent_backup_existed=False,
    )

    async def default_checkpoint(_plugin_id: str, _transaction_id: str):
        """返回事务级文件快照。"""
        calls.append("checkpoint")
        return checkpoint

    async def default_installer(**_kwargs):
        """表示包载荷安装成功。"""
        calls.append("package")
        return True, "installed"

    async def default_receipt(_plugin_id: str):
        """返回稳定的已落盘载荷收据。"""
        calls.append("receipt")
        return RECEIPT

    async def default_reporter(_plugin_id: str, _repo_url: str | None):
        """表示远程安装上报成功。"""
        calls.append("report")
        return True

    packages = SimpleNamespace(
        async_checkpoint=checkpointer or default_checkpoint,
        async_install=installer or default_installer,
        async_restore=package_restore or AsyncMock(),
        async_cleanup=package_cleanup or AsyncMock(),
        async_stage_persistent_backup=package_stage_backup or AsyncMock(),
        async_activate_persistent_backup=package_activate_backup or AsyncMock(),
        async_finalize_persistent_backup=package_finalize_backup or AsyncMock(),
        async_commit=package_commit or AsyncMock(),
        async_payload_receipt=payload_receipt or default_receipt,
        async_native_dependency_changes=native_dependency_changes
        or AsyncMock(return_value=()),
    )
    command = PluginInstallCommand(
        persistence=persistence,
        installed_plugins_reader=lambda: installed or [],
        plugin_ids_provider=lambda: plugin_ids or [],
        packages=packages,
        install_reporter=reporter or default_reporter,
        target_reloader=target_reloader
        or AsyncMock(return_value=PluginRuntimeStatus.ACTIVE),
        rollback_reloader=rollback_reloader or AsyncMock(),
        registration_refresher=registration_refresher or AsyncMock(),
        mutation=mutation or (lambda _operation: nullcontext()),
        package_write_guard=package_write_guard
        or (lambda _plugin_id: nullcontext()),
        restart_required_recorder=restart_required_recorder or Mock(),
        clock=lambda: NOW,
        transaction_id_factory=lambda: transaction_id,
    )
    return command, persistence, calls


def _journal_record(
    phase: PluginInstallationPhase,
    *,
    transaction_id: str,
) -> PluginInstallationRecord:
    """构造占用同一物理插件槽位的旧安装 journal。"""
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


async def _execute(
    command: PluginInstallCommand,
    admission=None,
    *,
    release_version: str | None = None,
    force: bool = False,
    **kwargs,
):
    """执行测试安装并默认使用当前冻结候选。"""
    return await command.execute(
        admission=admission or _admission(),
        release_version=release_version,
        force=force,
        **kwargs,
    )


@pytest.mark.asyncio
async def test_success_commits_journal_before_report_and_cleans_package_snapshot():
    """成功路径按快照、journal、运行态、数据库提交、清理和上报顺序执行。"""
    calls: list[str] = []

    def mark(name: str):
        async def action(_value):
            calls.append(name)

        return action

    async def target_reload(_plugin_id):
        calls.append("target_reload")
        return PluginRuntimeStatus.ACTIVE

    async def installer(**_kwargs):
        calls.append("package")
        return True, "installed"

    async def receipt(_plugin_id):
        calls.append("receipt")
        return RECEIPT

    async def report(_plugin_id, _repo_url):
        calls.append("report")
        return True

    persistence = _PersistenceSpy(calls)
    command, _, _ = _command(
        persistence=persistence,
        installer=installer,
        payload_receipt=receipt,
        package_stage_backup=mark("stage_backup"),
        package_activate_backup=mark("activate_backup"),
        target_reloader=target_reload,
        registration_refresher=mark("registrations"),
        package_finalize_backup=mark("finalize_backup"),
        package_commit=mark("package_commit"),
        reporter=report,
    )

    result = await _execute(command)

    assert result.success is True
    assert result.package_installed is True
    assert result.installed_list_persisted is True
    assert result.runtime_reloaded is True
    assert result.registrations_refreshed is True
    assert result.reported is True
    assert calls == [
        "checkpoint",
        "journal_create",
        "package",
        "receipt",
        "journal_target",
        "stage_backup",
        "activate_backup",
        "target_reload",
        "registrations",
        "journal_commit",
        "finalize_backup",
        "package_commit",
        "journal_delete",
        "report",
    ]
    assert persistence.records == {}


@pytest.mark.asyncio
async def test_non_active_runtime_status_compensates_before_database_commit():
    """运行态重载未激活时，安装必须在数据库提交前补偿并返回失败。"""
    package_restore = AsyncMock()
    package_cleanup = AsyncMock()
    rollback_reloader = AsyncMock(return_value=PluginRuntimeStatus.ACTIVE)
    registration_refresher = AsyncMock()
    target_reloader = AsyncMock(return_value=PluginRuntimeStatus.LOAD_FAILED)
    reporter = AsyncMock()

    command, persistence, calls = _command(
        package_restore=package_restore,
        package_cleanup=package_cleanup,
        target_reloader=target_reloader,
        rollback_reloader=rollback_reloader,
        registration_refresher=registration_refresher,
        reporter=reporter,
    )

    result = await _execute(command)

    assert result.success is False
    assert result.failure_stage == "runtime_reload"
    assert result.package_installed is True
    assert result.installed_list_persisted is False
    assert result.runtime_reloaded is False
    assert result.registrations_refreshed is False
    assert result.reported is False
    package_restore.assert_awaited_once()
    rollback_reloader.assert_awaited_once_with("DemoPlugin")
    registration_refresher.assert_awaited_once_with("DemoPlugin")
    reporter.assert_not_awaited()
    assert "journal_commit" not in calls
    assert persistence.records == {}


@pytest.mark.asyncio
async def test_native_dependency_change_commits_payload_when_reload_waits_for_restart():
    """原生依赖已经落盘后，重载失败不得恢复为与新依赖不匹配的旧插件。"""
    change = NativeDependencyChange(
        distribution="native-demo",
        previous_version="1.0.0",
        current_version="2.0.0",
        artifacts=("native_demo.so",),
    )
    package_restore = AsyncMock()
    package_cleanup = AsyncMock()
    rollback_reloader = AsyncMock()
    registration_refresher = AsyncMock()
    restart_required_recorder = Mock()
    command, persistence, calls = _command(
        native_dependency_changes=AsyncMock(return_value=(change,)),
        package_restore=package_restore,
        package_cleanup=package_cleanup,
        target_reloader=AsyncMock(return_value=PluginRuntimeStatus.LOAD_FAILED),
        rollback_reloader=rollback_reloader,
        registration_refresher=registration_refresher,
        restart_required_recorder=restart_required_recorder,
    )

    result = await _execute(command)

    assert result.success is True
    assert result.package_installed is True
    assert result.installed_list_persisted is True
    assert result.runtime_reloaded is False
    assert result.registrations_refreshed is False
    assert result.restart_required is True
    assert persistence.identity is not None
    assert persistence.records == {}
    assert "journal_commit" in calls
    package_restore.assert_not_awaited()
    package_cleanup.assert_not_awaited()
    rollback_reloader.assert_not_awaited()
    registration_refresher.assert_not_awaited()
    restart_required_recorder.assert_called_once_with(
        "DemoPlugin",
        ("native-demo",),
    )


@pytest.mark.asyncio
async def test_native_dependency_change_keeps_active_plugin_available():
    """当前进程仍可重载时，插件保持 ACTIVE 并独立记录重启要求。"""
    change = NativeDependencyChange(
        distribution="native-demo",
        previous_version="1.0.0",
        current_version="2.0.0",
        artifacts=("native_demo.pyd",),
    )
    registration_refresher = AsyncMock()
    restart_required_recorder = Mock()
    command, _, _ = _command(
        native_dependency_changes=AsyncMock(return_value=(change,)),
        target_reloader=AsyncMock(return_value=PluginRuntimeStatus.ACTIVE),
        registration_refresher=registration_refresher,
        restart_required_recorder=restart_required_recorder,
    )

    result = await _execute(command)

    assert result.success is True
    assert result.runtime_reloaded is True
    assert result.registrations_refreshed is True
    assert result.restart_required is True
    registration_refresher.assert_awaited_once_with("DemoPlugin")
    restart_required_recorder.assert_called_once_with(
        "DemoPlugin",
        ("native-demo",),
    )


@pytest.mark.asyncio
async def test_native_dependency_detection_failure_keeps_normal_install_result():
    """原生依赖诊断不可用时应保持普通插件安装语义。"""
    restart_required_recorder = Mock()
    command, _, _ = _command(
        native_dependency_changes=AsyncMock(side_effect=OSError("probe unavailable")),
        restart_required_recorder=restart_required_recorder,
    )

    result = await _execute(command)

    assert result.success is True
    assert result.runtime_reloaded is True
    assert result.restart_required is False
    restart_required_recorder.assert_not_called()


@pytest.mark.asyncio
async def test_failed_package_install_keeps_detected_native_restart_requirement():
    """依赖部分落盘后安装失败，文件补偿不能清除进程级激活要求。"""
    change = NativeDependencyChange(
        distribution="native-demo",
        previous_version="1.0.0",
        current_version="2.0.0",
        artifacts=("native_demo.so",),
    )
    package_restore = AsyncMock()
    restart_required_recorder = Mock()
    command, _, _ = _command(
        installer=AsyncMock(return_value=(False, "dependency failed")),
        native_dependency_changes=AsyncMock(return_value=(change,)),
        package_restore=package_restore,
        restart_required_recorder=restart_required_recorder,
    )

    result = await _execute(command)

    assert result.success is False
    assert result.failure_stage == "package_install"
    package_restore.assert_awaited_once()
    restart_required_recorder.assert_called_once_with(
        "DemoPlugin",
        ("native-demo",),
    )


@pytest.mark.asyncio
async def test_existing_plugin_with_non_active_runtime_status_is_not_reported_successfully():
    """已有载荷刷新失败时不得伪装成运行态成功或发送安装上报。"""
    target_reloader = AsyncMock(return_value=PluginRuntimeStatus.LOAD_FAILED)
    reporter = AsyncMock()
    command, persistence, _ = _command(
        installed=["DemoPlugin"],
        plugin_ids=["DemoPlugin"],
        target_reloader=target_reloader,
        reporter=reporter,
    )

    result = await _execute(command, admission=_admission(identity=_identity()))

    assert result.success is False
    assert result.refreshed_only is True
    assert result.failure_stage == "runtime_reload"
    assert result.runtime_reloaded is False
    assert result.registrations_refreshed is False
    assert result.reported is False
    target_reloader.assert_awaited_once_with("DemoPlugin")
    reporter.assert_not_awaited()
    assert persistence.records == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "phase",
    [PluginInstallationPhase.PREPARED, PluginInstallationPhase.COMMITTED],
)
async def test_unfinished_journal_blocks_follow_up_before_payload_write(
    phase: PluginInstallationPhase,
) -> None:
    """旧 journal 未收尾时，新安装不得写载荷或推进身份 revision。"""
    persistence = _PersistenceSpy([])
    old_record = _journal_record(phase, transaction_id=f"old-{phase.value}")
    persistence.records[old_record.transaction_id] = old_record
    package_install = AsyncMock()
    package_restore = AsyncMock()
    package_cleanup = AsyncMock()
    command, _, _ = _command(
        persistence=persistence,
        installer=package_install,
        package_restore=package_restore,
        package_cleanup=package_cleanup,
    )

    admission = (
        _admission(identity=_identity(revision=2))
        if phase is PluginInstallationPhase.COMMITTED
        else _admission()
    )
    result = await _execute(command, admission=admission, force=True)

    assert result.success is False
    assert result.failure_stage == "journal_prepare_conflict"
    assert "未收尾安装事务" in result.message
    package_install.assert_not_awaited()
    package_restore.assert_awaited_once()
    package_cleanup.assert_awaited_once()
    assert persistence.records == {old_record.transaction_id: old_record}

    if phase is PluginInstallationPhase.COMMITTED:
        checkpoint = SimpleNamespace(
            plugin_existed=True,
            persistent_backup_existed=True,
        )
        recovery_packages = SimpleNamespace(
            restore_checkpoint=Mock(return_value=checkpoint),
            async_restore=AsyncMock(),
            async_cleanup=AsyncMock(),
            async_committed_payload_receipt=AsyncMock(return_value=RECEIPT),
            async_finalize_persistent_backup=AsyncMock(),
            async_commit=AsyncMock(),
        )
        persistence.identity = _identity(revision=2)
        recovery = PluginInstallationRecoveryService(
            persistence=persistence,
            packages=recovery_packages,
        )

        recovery_result = await recovery.replay()

        assert recovery_result.finalized == 1
        assert persistence.records == {}


@pytest.mark.asyncio
async def test_package_rejection_restores_files_and_removes_prepared_journal():
    """包安装返回失败时不得切换运行态，且必须删除 PREPARED journal。"""
    package_restore = AsyncMock()
    package_cleanup = AsyncMock()
    target_reloader = AsyncMock()
    reporter = AsyncMock()

    async def installer(**_kwargs):
        return False, "download failed"

    command, persistence, _ = _command(
        installer=installer,
        package_restore=package_restore,
        package_cleanup=package_cleanup,
        target_reloader=target_reloader,
        reporter=reporter,
    )

    result = await _execute(command)

    assert result.success is False
    assert result.failure_stage == "package_install"
    assert result.rollback.file_restored is True
    assert result.rollback.journal_deleted is True
    package_restore.assert_awaited_once()
    package_cleanup.assert_awaited_once()
    target_reloader.assert_not_awaited()
    reporter.assert_not_awaited()
    assert persistence.records == {}


@pytest.mark.asyncio
async def test_journal_create_failure_uses_legacy_rollback_without_journal_cleanup():
    """journal 创建失败时使用兼容回滚，不尝试删除不存在的 journal。"""
    package_restore = AsyncMock()
    package_cleanup = AsyncMock()
    persistence = _PersistenceSpy(
        [],
        create_error=RuntimeError("database unavailable"),
    )
    command, _, _ = _command(
        persistence=persistence,
        package_restore=package_restore,
        package_cleanup=package_cleanup,
    )

    result = await _execute(command)

    assert result.success is False
    assert result.failure_stage == "journal_prepare"
    assert result.rollback.file_restored is True
    package_restore.assert_awaited_once()
    package_cleanup.assert_awaited_once()


@pytest.mark.asyncio
async def test_persistence_unavailable_during_journal_prepare_is_rethrown():
    """数据库 worker 暂不可用时完成无 journal 回滚后保留异常语义。"""
    package_restore = AsyncMock()
    package_cleanup = AsyncMock()
    persistence = _PersistenceSpy(
        [],
        create_error=DatabaseWorkerClosedError("worker closed"),
    )
    command, _, _ = _command(
        persistence=persistence,
        package_restore=package_restore,
        package_cleanup=package_cleanup,
    )

    with pytest.raises(PersistenceUnavailableError):
        await _execute(command)

    package_restore.assert_awaited_once()
    package_cleanup.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "failure_stage", "runtime_touched"),
    [
        ("receipt", "payload_receipt", False),
        ("target", "payload_receipt", False),
        ("stage", "persistent_backup_stage", False),
        ("activate", "persistent_backup_activate", False),
        ("reload", "runtime_reload", True),
        ("registrations", "registration_refresh", True),
    ],
)
async def test_precommit_failure_restores_files_and_runtime(
    failure: str,
    failure_stage: str,
    runtime_touched: bool,
):
    """每个提交前阶段失败都恢复文件，运行态失败还要恢复旧运行态。"""
    package_restore = AsyncMock()
    rollback_reloader = AsyncMock()
    registration_refresher = AsyncMock()

    async def failing_receipt(_plugin_id):
        raise RuntimeError("receipt failed")

    async def failing_stage(_value):
        raise RuntimeError("backup stage failed")

    async def failing_activate(_value):
        raise RuntimeError("backup activation failed")

    async def failing_reload(_plugin_id):
        raise RuntimeError("reload failed")

    registration_attempts = 0

    async def failing_registrations(_plugin_id):
        nonlocal registration_attempts
        registration_attempts += 1
        if registration_attempts == 1:
            raise RuntimeError("registration failed")

    persistence = _PersistenceSpy(
        [],
        target_error=RuntimeError("target failed") if failure == "target" else None,
    )
    command, _, _ = _command(
        persistence=persistence,
        package_restore=package_restore,
        payload_receipt=(failing_receipt if failure == "receipt" else None),
        package_stage_backup=(failing_stage if failure == "stage" else None),
        package_activate_backup=(
            failing_activate if failure == "activate" else None
        ),
        target_reloader=failing_reload if failure == "reload" else None,
        rollback_reloader=rollback_reloader,
        registration_refresher=(
            failing_registrations
            if failure == "registrations"
            else registration_refresher
        ),
    )

    result = await _execute(command)

    assert result.success is False
    assert result.failure_stage == failure_stage
    assert result.rollback.file_restored is True
    assert result.rollback.journal_deleted is True
    if runtime_touched:
        rollback_reloader.assert_awaited_once_with("DemoPlugin")
    else:
        rollback_reloader.assert_not_awaited()


@pytest.mark.asyncio
async def test_precommit_failure_keeps_previous_declared_metadata_snapshot():
    """提交前失败不得把新声明快照写成当前身份事实。"""
    previous = _identity(version="1.0.0", revision=3)
    persistence = _PersistenceSpy([], identity=previous)
    rollback_reloader = AsyncMock()
    target_reloader = AsyncMock(side_effect=RuntimeError("reload failed"))
    command, _, _ = _command(
        persistence=persistence,
        target_reloader=target_reloader,
        rollback_reloader=rollback_reloader,
    )

    result = await _execute(
        command,
        admission=_admission(identity=previous),
    )

    assert result.success is False
    assert result.failure_stage == "runtime_reload"
    assert result.rollback.journal_deleted is True
    assert persistence.identity is previous
    assert persistence.identity.declared_version == "1.0.0"
    assert persistence.identity.declared_metadata is previous.declared_metadata
    rollback_reloader.assert_awaited_once_with("DemoPlugin")


@pytest.mark.asyncio
async def test_database_commit_failure_restores_runtime_before_deleting_journal():
    """数据库最终提交失败时，文件、运行态和 PREPARED journal 必须一起补偿。"""
    package_restore = AsyncMock()
    rollback_reloader = AsyncMock()
    persistence = _PersistenceSpy(
        [],
        commit_error=RuntimeError("commit failed"),
    )
    command, _, _ = _command(
        persistence=persistence,
        package_restore=package_restore,
        rollback_reloader=rollback_reloader,
    )

    result = await _execute(command)

    assert result.success is False
    assert result.failure_stage == "database_commit"
    assert result.rollback.file_restored is True
    assert result.rollback.runtime_restored is True
    assert result.rollback.journal_deleted is True
    package_restore.assert_awaited_once()
    rollback_reloader.assert_awaited_once_with("DemoPlugin")


@pytest.mark.asyncio
async def test_cleanup_failure_keeps_committed_journal_for_replay():
    """数据库已提交但清理失败时不能回滚载荷，journal 必须保留供启动重放。"""
    package_finalize = AsyncMock(side_effect=RuntimeError("cleanup unavailable"))
    rollback_reloader = AsyncMock()
    command, persistence, _ = _command(
        package_finalize_backup=package_finalize,
        rollback_reloader=rollback_reloader,
    )

    result = await _execute(command)

    assert result.success is True
    assert result.checkpoint_cleanup_error == "cleanup unavailable"
    assert not result.rollback.file_attempted
    assert persistence.records["txn-demo"].phase is PluginInstallationPhase.COMMITTED
    rollback_reloader.assert_not_awaited()


@pytest.mark.asyncio
async def test_report_failure_does_not_rollback_committed_install():
    """远程安装上报失败属于非关键副作用，不得撤销已提交本地安装。"""
    package_restore = AsyncMock()
    reporter = AsyncMock(side_effect=RuntimeError("server unavailable"))
    command, persistence, _ = _command(
        package_restore=package_restore,
        reporter=reporter,
    )

    result = await _execute(command)

    assert result.success is True
    assert result.reported is False
    assert result.report_error == "server unavailable"
    assert "不影响本地安装" in result.message
    package_restore.assert_not_awaited()
    assert persistence.records == {}


@pytest.mark.asyncio
async def test_existing_matching_payload_only_refreshes_runtime():
    """同一来源、代际和版本已提交时只刷新运行态，不重复写包或 journal。"""
    checkpointer = AsyncMock()
    installer = AsyncMock()
    reloader = AsyncMock(return_value=PluginRuntimeStatus.ACTIVE)
    refresher = AsyncMock()
    reporter = AsyncMock(return_value=True)
    command, persistence, _ = _command(
        installed=["DemoPlugin"],
        plugin_ids=["DemoPlugin"],
        checkpointer=checkpointer,
        installer=installer,
        target_reloader=reloader,
        registration_refresher=refresher,
        reporter=reporter,
    )

    result = await _execute(command, admission=_admission(identity=_identity()))

    assert result.success is True
    assert result.refreshed_only is True
    assert result.package_installed is False
    assert result.runtime_reloaded is True
    checkpointer.assert_not_awaited()
    installer.assert_not_awaited()
    reloader.assert_awaited_once_with("DemoPlugin")
    refresher.assert_awaited_once_with("DemoPlugin")
    reporter.assert_awaited_once_with("DemoPlugin", REPO_URL)
    assert persistence.records == {}


@pytest.mark.asyncio
async def test_force_install_replaces_matching_payload_and_local_sync_skips_report():
    """强制安装和本地同步都绕过刷新短路，本地同步还禁止远程上报。"""
    installer = AsyncMock(return_value=(True, "synced"))
    reporter = AsyncMock(return_value=True)
    command, _, _ = _command(
        installed=["DemoPlugin"],
        plugin_ids=["DemoPlugin"],
        installer=installer,
        reporter=reporter,
    )

    result = await _execute(
        command,
        admission=_admission(identity=_identity()),
        force=True,
        local_sync=True,
    )

    assert result.success is True
    assert result.refreshed_only is False
    installer.assert_awaited_once_with(
        plugin_id="DemoPlugin",
        repo_url=REPO_URL,
        package_version="v3",
        release_version=None,
        force_install=True,
        checkpoint=ANY,
    )
    reporter.assert_not_awaited()


@pytest.mark.asyncio
async def test_mutation_rejection_happens_before_package_write_guard():
    """运行时封口后拒绝安装，不能进入包写入抑制或文件快照。"""
    checkpointer = AsyncMock()
    package_guard = Mock(return_value=nullcontext())
    rejected = Mock(side_effect=PluginMutationRejectedError("安装插件 DemoPlugin"))

    command, _, _ = _command(
        checkpointer=checkpointer,
        mutation=rejected,
        package_write_guard=package_guard,
    )

    result = await _execute(command)

    assert result.success is False
    assert result.failure_stage == "admission"
    package_guard.assert_not_called()
    checkpointer.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancelled_package_install_waits_for_compensation():
    """包安装被取消时等待文件恢复和 journal 清理完成后再传播取消。"""
    started = asyncio.Event()
    release = asyncio.Event()
    package_restore = AsyncMock()
    package_cleanup = AsyncMock()

    async def installer(**_kwargs):
        started.set()
        await release.wait()

    command, persistence, _ = _command(
        installer=installer,
        package_restore=package_restore,
        package_cleanup=package_cleanup,
    )
    task = asyncio.create_task(_execute(command))
    await started.wait()
    task.cancel()
    release.set()

    with pytest.raises(asyncio.CancelledError):
        await task

    package_restore.assert_awaited_once()
    package_cleanup.assert_awaited_once()
    assert persistence.records == {}


@pytest.mark.asyncio
async def test_cancelled_package_install_keeps_detected_native_restart_requirement():
    """取消传播前完成依赖检测，已替换的共享原生载荷仍要求重启。"""
    started = asyncio.Event()
    release = asyncio.Event()
    change = NativeDependencyChange(
        distribution="native-demo",
        previous_version="1.0.0",
        current_version="2.0.0",
        artifacts=("native_demo.pyd",),
    )
    restart_required_recorder = Mock()

    async def installer(**_kwargs):
        started.set()
        await release.wait()
        return True, "installed"

    command, _, _ = _command(
        installer=installer,
        native_dependency_changes=AsyncMock(return_value=(change,)),
        restart_required_recorder=restart_required_recorder,
    )
    task = asyncio.create_task(_execute(command))
    await started.wait()
    task.cancel()
    release.set()

    with pytest.raises(asyncio.CancelledError):
        await task

    restart_required_recorder.assert_called_once_with(
        "DemoPlugin",
        ("native-demo",),
    )


@pytest.mark.asyncio
async def test_cancelled_journal_create_resolves_persisted_record_before_rollback():
    """PREPARED 已写入但调用被取消时，必须确认记录并完成补偿。"""
    started = asyncio.Event()
    release = asyncio.Event()
    package_restore = AsyncMock()
    package_cleanup = AsyncMock()
    persistence = _PersistenceSpy([])

    async def create(record: PluginInstallationRecord):
        persistence.calls.append("journal_create")
        persistence.records[record.transaction_id] = record
        started.set()
        await release.wait()
        return record

    persistence.create_installation = create
    command, _, _ = _command(
        persistence=persistence,
        package_restore=package_restore,
        package_cleanup=package_cleanup,
    )
    task = asyncio.create_task(_execute(command))
    await started.wait()
    task.cancel()
    release.set()

    with pytest.raises(asyncio.CancelledError):
        await task

    package_restore.assert_awaited_once()
    package_cleanup.assert_awaited_once()
    assert persistence.records == {}


@pytest.mark.asyncio
async def test_cancelled_prepared_commit_rolls_back_after_outcome_check():
    """提交任务确定仍为 PREPARED 时，取消必须先完成旧载荷补偿。"""
    started = asyncio.Event()
    release = asyncio.Event()
    package_restore = AsyncMock()
    rollback_reloader = AsyncMock()
    persistence = _PersistenceSpy([])

    async def commit(
        transaction_id: str,
        *,
        identity_target: PluginIdentity | None,
    ) -> PluginInstallationRecord:
        del transaction_id, identity_target
        persistence.calls.append("journal_commit")
        started.set()
        await release.wait()
        raise RuntimeError("commit failed")

    persistence.commit_installation = commit
    command, _, _ = _command(
        persistence=persistence,
        package_restore=package_restore,
        rollback_reloader=rollback_reloader,
    )
    task = asyncio.create_task(_execute(command))
    await started.wait()
    task.cancel()
    release.set()

    with pytest.raises(asyncio.CancelledError):
        await task

    package_restore.assert_awaited_once()
    rollback_reloader.assert_awaited_once_with("DemoPlugin")
    assert persistence.records == {}


@pytest.mark.asyncio
async def test_cancelled_committed_install_keeps_journal_for_startup_cleanup():
    """数据库已提交后发生取消时不得回滚新载荷，journal 留给启动收尾。"""
    started = asyncio.Event()
    release = asyncio.Event()
    package_restore = AsyncMock()
    package_finalize = AsyncMock()
    persistence = _PersistenceSpy([])

    async def commit(
        transaction_id: str,
        *,
        identity_target: PluginIdentity | None,
    ) -> PluginInstallationRecord:
        record = replace(
            persistence.records[transaction_id],
            phase=PluginInstallationPhase.COMMITTED,
            membership_target=True,
            identity_target_revision=(
                identity_target.revision if identity_target else None
            ),
        )
        persistence.records[transaction_id] = record
        started.set()
        await release.wait()
        return record

    persistence.commit_installation = commit
    command, _, _ = _command(
        persistence=persistence,
        package_restore=package_restore,
        package_finalize_backup=package_finalize,
    )
    task = asyncio.create_task(_execute(command))
    await started.wait()
    task.cancel()
    release.set()

    with pytest.raises(asyncio.CancelledError):
        await task

    package_restore.assert_not_awaited()
    package_finalize.assert_not_awaited()
    assert persistence.records["txn-demo"].phase is PluginInstallationPhase.COMMITTED


@pytest.mark.asyncio
async def test_commit_ack_failure_uses_committed_journal_as_final_fact():
    """提交回执丢失但 journal 已为 COMMITTED 时继续完成新载荷收尾。"""
    package_restore = AsyncMock()
    persistence = _PersistenceSpy([])

    async def commit(
        transaction_id: str,
        *,
        identity_target: PluginIdentity | None,
    ) -> PluginInstallationRecord:
        record = replace(
            persistence.records[transaction_id],
            phase=PluginInstallationPhase.COMMITTED,
            membership_target=True,
            identity_target_revision=(
                identity_target.revision if identity_target else None
            ),
        )
        persistence.records[transaction_id] = record
        raise RuntimeError("commit acknowledgement lost")

    persistence.commit_installation = commit
    command, _, _ = _command(
        persistence=persistence,
        package_restore=package_restore,
    )

    result = await _execute(command)

    assert result.success is True
    package_restore.assert_not_awaited()
    assert persistence.records == {}


@pytest.mark.asyncio
async def test_unknown_commit_result_preserves_current_payload_and_journal():
    """数据库最终状态无法读取时不得猜测回滚，必须留待启动恢复。"""
    package_restore = AsyncMock()
    rollback_reloader = AsyncMock()
    persistence = _PersistenceSpy([], commit_error=RuntimeError("commit failed"))
    persistence.get_installation = AsyncMock(
        side_effect=RuntimeError("database unavailable")
    )
    command, _, _ = _command(
        persistence=persistence,
        package_restore=package_restore,
        rollback_reloader=rollback_reloader,
    )

    result = await _execute(command)

    assert result.success is False
    assert result.failure_stage == "database_commit_unknown"
    package_restore.assert_not_awaited()
    rollback_reloader.assert_not_awaited()
    assert persistence.records["txn-demo"].phase is PluginInstallationPhase.PREPARED


@pytest.mark.asyncio
async def test_runtime_compensation_failure_keeps_prepared_journal():
    """旧运行态未恢复完整时不得删除 PREPARED journal 和恢复材料。"""
    package_restore = AsyncMock()
    package_cleanup = AsyncMock()
    target_reloader = AsyncMock(side_effect=RuntimeError("reload failed"))
    rollback_reloader = AsyncMock(side_effect=RuntimeError("rollback failed"))
    command, persistence, _ = _command(
        package_restore=package_restore,
        package_cleanup=package_cleanup,
        target_reloader=target_reloader,
        rollback_reloader=rollback_reloader,
    )

    result = await _execute(command)

    assert result.success is False
    assert result.failure_stage == "runtime_reload"
    assert result.rollback.file_restored is True
    assert result.rollback.runtime_restored is False
    assert result.rollback.journal_deleted is False
    assert result.rollback.errors == ("插件运行态恢复失败：rollback failed",)
    package_cleanup.assert_not_awaited()
    assert persistence.records["txn-demo"].phase is PluginInstallationPhase.PREPARED
