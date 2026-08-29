"""数据库治理能力的宿主组合根。"""

from __future__ import annotations

from dataclasses import dataclass

from app.adapters.system.backup.database import (
    PostgreSQLBackupBackend,
    SQLiteBackupBackend,
)
from app.adapters.system.backup.files import BackupFiles
from app.api.data import ApiDataPorts
from app.application.backup import BackupPolicy, DatabaseBackupService
from app.application.database import (
    DatabaseGovernance,
    DatabaseHealthService,
    configure_database_governance,
)
from app.application.maintenance import (
    DataCleanupService,
    read_cleanup_policy,
)
from app.application.plugin.transaction import (
    PluginPersistenceService,
    configure_plugin_persistence,
)
from app.application.query import DataQueryService, configure_data_query_service
from app.application.workflow import WorkflowQueryService, configure_workflow_query
from app.db.adapters.history.download import SessionDownloadHistoryRepository
from app.db.adapters.pluginidentity import TransactionalPluginIdentityStore
from app.db.adapters.plugininstallation import TransactionalPluginInstallationStore
from app.db.adapters.query import SqlAlchemyDataQueryAdapter
from app.db.adapters.site import SessionSiteRepository
from app.db.adapters.subscription import (
    SessionSubscriptionHistoryRepository,
    SessionSubscriptionRepository,
)
from app.db.adapters.transaction import TransactionalWriteRunner
from app.db.adapters.user import SqlAlchemyUserRepository, TransactionalUserRepository
from app.db.adapters.workflow import TransactionalWorkflowQueryRepository
from app.db.engine import get_engine
from app.db.health import probe_database
from app.db.maintenance import DatabaseCleanupRepository
from app.db.oper.mediaserver import MediaServerOper
from app.db.oper.message import MessageOper
from app.db.oper.passkey import PassKeyOper
from app.db.oper.systemconfig import SystemConfigOper
from app.db.oper.workflow import WorkflowOper
from app.db.session import (
    SessionFactory,
    async_session_scope,
    get_async_db,
    get_db,
)
from app.db.uow import (
    SqlAlchemyAsyncUnitOfWork,
    SqlAlchemyUnitOfWork,
    configure_transaction_runners,
)
from app.db.worker import DatabaseWorker
from app.runtime.settings import get_runtime_setting


@dataclass(frozen=True, slots=True)
class DatabaseRuntime:
    """保存数据库 worker 与兼容写入口共享的事务执行器。"""

    worker: DatabaseWorker
    transaction: TransactionalWriteRunner


@dataclass(frozen=True, slots=True)
class DatabaseComposition:
    """保存初始化器后续装配所需的数据库查询能力。"""

    api_data: ApiDataPorts
    workflow_query: WorkflowQueryService


_database_runtime: DatabaseRuntime | None = None


async def start_database_runtime() -> DatabaseRuntime:
    """启动进程唯一数据库 worker，并登记兼容写事务 runner。"""
    global _database_runtime
    transaction = TransactionalWriteRunner(
        sync_session=SessionFactory,
        async_session=async_session_scope,
    )
    configure_transaction_runners(
        sync=transaction.sync,
        async_=transaction.async_,
    )
    worker = DatabaseWorker()
    await worker.start()
    runtime = DatabaseRuntime(worker=worker, transaction=transaction)
    _database_runtime = runtime
    return runtime


async def stop_database_runtime() -> None:
    """关闭数据库 worker；失败时保留 owner 供诊断和重试。"""
    global _database_runtime
    runtime = _database_runtime
    if runtime is not None:
        await runtime.worker.shutdown()
        _database_runtime = None


def database_runtime_active() -> bool:
    """返回数据库 worker 是否仍由当前进程持有。"""
    return _database_runtime is not None


def build_transactional_user_repository() -> TransactionalUserRepository:
    """构造供 Chain、Agent 与进程级认证共享的短会话用户仓储。"""
    return TransactionalUserRepository(
        sync_session=SessionFactory,
        async_session=async_session_scope,
    )


def compose_database_services(
    *,
    runtime: DatabaseRuntime,
    system_config: SystemConfigOper,
) -> DatabaseComposition:
    """组合查询、插件持久化与旧 API 数据 Facade 所需实现。"""
    data_query_adapter = SqlAlchemyDataQueryAdapter(SessionFactory)
    configure_data_query_service(
        DataQueryService(
            subscriptions=data_query_adapter,
            histories=data_query_adapter,
            async_executor=runtime.worker,
        )
    )
    configure_plugin_persistence(
        PluginPersistenceService(
            executor=runtime.worker,
            identities=TransactionalPluginIdentityStore(SessionFactory),
            installations=TransactionalPluginInstallationStore(
                SessionFactory,
                system_config.update_atomically,
            ),
        )
    )
    workflow_query = WorkflowQueryService(
        repository=TransactionalWorkflowQueryRepository(
            sync_session=SessionFactory,
            async_session=async_session_scope,
        )
    )
    configure_workflow_query(workflow_query)
    return DatabaseComposition(
        api_data=ApiDataPorts(
            sync_session=get_db,
            async_session=get_async_db,
            repositories={
                "download_history": SessionDownloadHistoryRepository,
                "media_server": MediaServerOper,
                "message": MessageOper,
                "passkey": PassKeyOper,
                "site": SessionSiteRepository,
                "subscribe": SessionSubscriptionRepository,
                "subscribe_history": SessionSubscriptionHistoryRepository,
                "user": SqlAlchemyUserRepository,
                "workflow": WorkflowOper,
            },
            standalone={
                "passkey": PassKeyOper,
                "system_config": SystemConfigOper,
                "user": build_transactional_user_repository,
            },
            unit_of_work={
                "async": SqlAlchemyAsyncUnitOfWork,
                "sync": SqlAlchemyUnitOfWork,
            },
        ),
        workflow_query=workflow_query,
    )


def build_database_governance() -> DatabaseGovernance:
    """以缓存同步引擎为事实源构造一个完整数据库治理门面。"""
    engine = get_engine()
    dialect = engine.dialect.name
    if dialect == "sqlite":
        backup_backend = SQLiteBackupBackend(engine)
    elif dialect == "postgresql":
        backup_backend = PostgreSQLBackupBackend(engine)
    else:
        raise RuntimeError(f"不支持的数据库类型：{dialect}")

    return DatabaseGovernance(
        health=DatabaseHealthService(probe_database),
        cleanup=DataCleanupService(
            repository=DatabaseCleanupRepository(session_factory=SessionFactory),
            policy_reader=read_cleanup_policy,
        ),
        backup=DatabaseBackupService(
            backend=backup_backend,
            artifact_store_factory=BackupFiles,
            policy_reader=read_backup_policy,
        ),
    )


def configure_database() -> None:
    """构造并登记宿主进程唯一的数据库治理门面。"""
    configure_database_governance(build_database_governance())


def read_backup_policy() -> BackupPolicy:
    """读取一次可热更新的数据库备份目录与保留策略。"""
    return BackupPolicy(
        root=get_runtime_setting('DATABASE_BACKUP_PATH'),
        retention_days=get_runtime_setting('DB_BACKUP_RETENTION_DAYS'),
        max_count=get_runtime_setting('DB_BACKUP_MAX_COUNT'),
    )
