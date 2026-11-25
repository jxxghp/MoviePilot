"""
插件安装命令的事务端口与持久化观察；不注册测试用例。
"""

from contextlib import nullcontext
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from app.application.plugin.admission import PluginInstallAdmission, PluginInstallAdmissionRequest, admit_plugin_install
from app.application.plugin.identity import PluginIdentity, TrustedPluginSourceType
from app.application.plugin.install import PluginInstallCommand
from app.application.plugin.source import CandidateInventory, MarketRead, PluginMarketCandidate
from app.application.plugin.transaction import (
    PluginInstallationConflictError,
    PluginInstallationPhase,
    PluginInstallationRecord,
)
from app.schemas.plugin import PluginRuntimeStatus

NOW = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)


REPO_URL = "https://github.com/jxxghp/MoviePilot-Plugins"


SOURCE_KEY = "github:jxxghp/moviepilot-plugins"


RECEIPT = "sha256:" + "a" * 64


def _admission(
    *,
    identity: PluginIdentity | None = None,
    version: str = "1.0.0",
) -> PluginInstallAdmission:
    """
    冻结一个可供安装用例消费的官方 V3 候选。
    """
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
    """
    记录安装 journal 的异步状态转移，并允许注入持久化失败。
    """

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
        """
        初始化本用例独立持有的替身状态与可观察记录。

        :param calls: 用于断言操作顺序的记录列表
        :param create_error: 创建事务阶段注入的异常
        :param target_error: 目标设置阶段注入的异常
        :param commit_error: 提交阶段注入的异常
        :param delete_error: 删除日志阶段注入的异常
        :param identity: 本次运行使用的冻结身份快照
        """
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
        """
        保存 PREPARED journal。
        """
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
        """
        返回 journal 创建结果，模拟超时后的确认读取。
        """
        self.calls.append("journal_get")
        return self.records.get(transaction_id)

    async def list_installations(self) -> list[PluginInstallationRecord]:
        """
        返回当前未收尾 journal，供连续事务恢复测试复用。
        """
        return list(self.records.values())

    async def get_identity(self, _plugin_id: str) -> PluginIdentity | None:
        """
        返回恢复核验使用的当前身份事实。
        """
        return self.identity

    async def set_installation_target(
        self,
        transaction_id: str,
        *,
        membership_target: bool,
        identity_target: PluginIdentity | None,
    ) -> PluginInstallationRecord:
        """
        保存最终 membership 与身份 revision。
        """
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
        """
        把 journal 推进到 COMMITTED。
        """
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
        """
        按 phase 删除已补偿或已收尾 journal。
        """
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
    """
    构造只含窄端口的安装命令，并返回可观测调用记录。
    """
    calls: list[str] = persistence.calls if persistence else []
    persistence = persistence or _PersistenceSpy(calls)
    checkpoint = SimpleNamespace(
        plugin_existed=False,
        persistent_backup_existed=False,
    )

    async def default_checkpoint(_plugin_id: str, _transaction_id: str):
        """
        返回事务级文件快照。
        """
        calls.append("checkpoint")
        return checkpoint

    async def default_installer(**_kwargs):
        """
        表示包载荷安装成功。
        """
        calls.append("package")
        return True, "installed"

    async def default_receipt(_plugin_id: str):
        """
        返回稳定的已落盘载荷收据。
        """
        calls.append("receipt")
        return RECEIPT

    async def default_reporter(_plugin_id: str, _repo_url: str | None):
        """
        表示远程安装上报成功。
        """
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
        loadable_marker=lambda _plugin_id: calls.append("mark_loadable"),
        host_enabled_reader=lambda _plugin_id: True,
        host_enabled_restorer=AsyncMock(),
        plugin_ids_provider=lambda: plugin_ids or [],
        packages=packages,
        install_reporter=reporter or default_reporter,
        target_reloader=target_reloader
        or AsyncMock(return_value=PluginRuntimeStatus.ACTIVE),
        rollback_reloader=rollback_reloader or AsyncMock(return_value=PluginRuntimeStatus.ACTIVE),
        registration_refresher=registration_refresher or AsyncMock(),
        mutation=mutation or (lambda _operation: nullcontext()),
        package_write_guard=package_write_guard
        or (lambda _plugin_id: nullcontext()),
        restart_required_recorder=restart_required_recorder or Mock(),
        clock=lambda: NOW,
        transaction_id_factory=lambda: transaction_id,
    )
    return command, persistence, calls
