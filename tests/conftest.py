"""pytest 全局引导：隔离 CONFIG_DIR、补 sites 垫片、建表、装载网络守卫。

引导与网络守卫均复用 ``app/testing`` 的共享 harness（与插件仓 conftest 同源），
引导逻辑只在 ``app/testing`` 维护一处。
"""

import asyncio
import sys
from collections.abc import Awaitable, Callable
from functools import partial
from typing import TypeVar

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

# 必须早于首个牵入 app.runtime.config 的 import（app.db / app.chain.* 都会牵入）：引擎本身已惰性，
# import app.db 不再连库，但 settings 在 import 期就把 CONFIG_DIR 读进字段并建好配置目录，之后
# 改环境变量已经晚了。prepare_backend 内部先隔离 CONFIG_DIR、补 app.application.site.sites 垫片，
# 再建表。app/testing 仅依赖标准库、import 不触发 app.*，故此处先 import 再调用是安全的。
from app.testing.bootstrap import prepare_backend

prepare_backend()

# 复用共享 autouse 网络守卫；同一实现亦供各插件仓 conftest import 复用，避免逐仓维护
from app.testing.network import block_real_network  # noqa: E402,F401

TResult = TypeVar("TResult")


class _TestDatabaseExecutor:
    """让绕过完整 lifespan 的测试仍通过线程执行同步数据库写入。"""

    async def run(self, operation):
        """在线程中执行测试事务。"""
        return await asyncio.to_thread(operation)


class _TestRuntimeSettingsProxy:
    """为仍需覆盖旧配置字段的测试提供局部桩，不回到宿主模块级代理。"""

    def __init__(self) -> None:
        self._originals: dict[str, tuple[bool, object]] = {}

    def __getattr__(self, key: str):
        from app.runtime.config import settings

        return getattr(settings, key)

    def __setattr__(self, key: str, value):
        if key == "_originals":
            object.__setattr__(self, key, value)
            return
        from app.runtime.config import settings

        if key not in self._originals:
            self._originals[key] = (hasattr(settings, key), getattr(settings, key, None))
        setattr(settings, key, value)

    def __delattr__(self, key: str) -> None:
        if key in self._originals:
            from app.runtime.config import settings

            had_value, original = self._originals.pop(key)
            if had_value:
                setattr(settings, key, original)
            elif hasattr(settings, key):
                delattr(settings, key)
            return
        raise AttributeError(key)


@pytest.fixture(autouse=True)
def install_runtime_settings_test_proxies(monkeypatch):
    """给历史测试 patch 点注入测试专用对象，生产代码不保留 settings 属性。"""
    proxy = _TestRuntimeSettingsProxy()
    _install_runtime_settings_test_proxies(proxy, monkeypatch)
    yield


def _install_runtime_settings_test_proxies(proxy, monkeypatch=None) -> None:
    """把测试专用 patch 点补到当前已导入的 Agent/模块。"""
    for module_name, module in tuple(sys.modules.items()):
        if not (
            module_name.startswith("app.modules.")
            or module_name.startswith("app.agent.")
            or module_name.startswith("app.startup.")
            or module_name == "app.main"
            or module_name.startswith("app.adapters.")
        ):
            continue
        if hasattr(module, "get_runtime_setting") and "settings" not in vars(module):
            if monkeypatch is None:
                setattr(module, "settings", proxy)
            else:
                monkeypatch.setattr(module, "settings", proxy, raising=False)


def pytest_runtest_call(item):
    """显式 fixture 期间才导入的模块也要拥有同一个测试 patch 点。"""
    _install_runtime_settings_test_proxies(_TestRuntimeSettingsProxy())


@pytest.fixture(autouse=True)
def configure_plugin_system_services():
    """为绕过完整启动流程的单元测试装配真实插件系统适配器。"""
    from app.adapters.web.security.access import configure_token_codec
    from app.api.data import configure_api_data_ports
    from app.application.configuration import (
        RuntimeConfiguration,
        RuntimeSettingsService,
        SystemConfigService,
        TransferRetryConfig,
        configure_runtime_configuration,
        configure_runtime_settings,
        configure_system_config,
        configure_token_runtime_config,
        configure_transfer_retry_config,
    )
    from app.application.security.token import (
        create_access_token,
        decode_access_token,
    )
    from app.application.security.userconfig import (
        UserConfigurationService,
        configure_user_configuration,
    )
    from app.application.service import configure_service_directory
    from app.db.adapters.configuration import TransactionalUserConfigurationRepository
    from app.db.oper.systemconfig import SystemConfigOper
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
    from app.runtime.config import settings
    from app.runtime.settings import configure_runtime_setting_provider
    from app.startup.composition.configuration import (
        build_api_runtime_config,
        build_chain_runtime_config,
        build_scheduler_runtime_config,
        build_token_runtime_config,
    )
    from app.startup.composition.subscription import (
        async_rule_group_mutation_scope,
        delete_subscribe_scope,
        rule_group_mutation_scope,
        site_reference_mutation_scope,
        subscription_completion_scope,
        subscription_mutation_scope,
        sync_delete_subscribe_scope,
        sync_subscription_mutation_scope,
    )

    configure_token_codec(create_access_token, decode_access_token)
    configure_runtime_configuration(
        RuntimeConfiguration(
            api=lambda: build_api_runtime_config(settings),
            scheduler=lambda: build_scheduler_runtime_config(settings),
            chain=lambda: build_chain_runtime_config(settings),
        )
    )
    configure_runtime_settings(RuntimeSettingsService(settings))
    configure_runtime_setting_provider(lambda key: getattr(settings, key))
    configure_token_runtime_config(lambda: build_token_runtime_config(settings))
    database_executor = _TestDatabaseExecutor()
    system_config = SystemConfigOper()
    user_config = TransactionalUserConfigurationRepository(SessionFactory)
    with SessionFactory() as session:
        system_config.load_snapshot(session)
    user_config.load_snapshot()
    configure_system_config(
        SystemConfigService(
            repository=system_config,
            async_executor=database_executor,
        )
    )
    configure_user_configuration(
        UserConfigurationService(
            repository=user_config,
            async_executor=database_executor,
        )
    )
    configure_transfer_retry_config(
        lambda: TransferRetryConfig(
            max_failed_retries=settings.TRANSFER_MAX_FAILED_RETRIES,
        )
    )
    from app.application.chain.context import (
        ChainRuntimeContext,
        configure_chain_runtime_context_provider,
    )
    from app.application.messaging.chat import (
        AgentChatPersistenceService,
        AgentChatService,
        configure_agent_chat_persistence,
        configure_agent_chat_service,
    )
    from app.application.messaging.message import MessageHelper, MessageQueueManager
    from app.application.module import configure_module_runtime
    from app.application.plugin.runtime import configure_plugin_runtime
    from app.runtime.cache import AsyncFileCache, FileCache
    from app.runtime.events import EventManager
    from app.runtime.extensions.module.dispatcher import ModuleInvocationDispatcher
    from app.runtime.extensions.module.manager import ModuleManager
    from app.runtime.extensions.plugin import manager as plugin_manager_module
    from app.runtime.extensions.plugin.database import get_plugin_database
    from app.runtime.extensions.plugin.manager import (
        PluginManager,
        reset_plugin_runtime_factory,
    )
    from app.runtime.extensions.plugin.runtime import (
        PluginRuntimeEnvironment,
        build_plugin_runtime,
    )
    from app.runtime.extensions.plugin.storage import get_plugin_storage
    from app.runtime.extensions.plugin.system import get_plugin_system
    from app.runtime.extensions.service import ServiceConfigHelper

    configure_service_directory(
        configs=ServiceConfigHelper.get_configs,
        modules=lambda module_type: ModuleManager().get_running_type_modules(module_type),
    )
    def build_test_plugin_runtime(host):
        """在 pytest 组合根装配直接构造 Manager 所需的隔离 Runtime。"""
        return build_plugin_runtime(
            host,
            PluginRuntimeEnvironment(
                plugins_root=settings.ROOT_PATH / "app" / "plugins",
                storage=get_plugin_storage,
                system=get_plugin_system,
                database=get_plugin_database,
                catalog_factory=lambda mapper: (
                    plugin_manager_module._plugin_catalog_factory(mapper)
                ),
                import_preparer=lambda **kwargs: (
                    plugin_manager_module._legacy_plugin_import_preparer(**kwargs)
                ),
                import_scanner=lambda **kwargs: (
                    plugin_manager_module._legacy_import_scanner(**kwargs)
                ),
                auth_level=lambda: plugin_manager_module._site_auth_level_provider(),
                remote_entry=host.get_plugin_remote_entry,
                development=lambda: bool(
                    plugin_manager_module.get_runtime_setting('DEV')
                ),
                logger=plugin_manager_module.logger,
            ),
            tool_build_max_attempts=PluginManager.AGENT_TOOLS_BUILD_MAX_ATTEMPTS,
        )

    plugin_manager_module.configure_plugin_runtime_factory(build_test_plugin_runtime)
    configure_plugin_runtime(
        lambda: PluginManager(),
        existing_provider=PluginManager.get_existing_instance,
    )
    configure_module_runtime(lambda: ModuleManager())
    from app.application.site.health import SiteHealthService, configure_site_health_service
    from app.application.site.query import SiteQueryService, configure_site_query_service
    from app.application.workflow import (
        WorkflowQueryService,
        configure_workflow_execution,
        configure_workflow_query,
        configure_workflow_runtime,
    )
    from app.workflow import WorkflowManager

    configure_workflow_runtime(lambda: WorkflowManager())
    from app.application.agent import AgentDataContext
    from app.application.agenttask import (
        AgentTaskExecutionService,
        configure_agent_task_execution,
    )
    from app.db.adapters.agent import (
        SessionAgentTaskRepository,
        TransactionalAgentTaskRepository,
        TransactionalPluginDataRepository,
    )
    from app.db.adapters.download import TransactionalDownloadFailureRepository
    from app.db.adapters.history.download import TransactionalDownloadHistoryRepository
    from app.db.adapters.history.transfer import TransactionalTransferHistoryRepository
    from app.db.adapters.mediaserver import TransactionalMediaServerRepository
    from app.db.adapters.site import TransactionalSiteRepository
    from app.db.adapters.subscription import TransactionalSubscriptionRepository
    from app.db.adapters.transaction import TransactionalWriteRunner
    from app.db.adapters.transfer.admission import TransactionalTransferAdmissionRepository
    from app.db.adapters.transfer.execution import (
        TransactionalTransferExecutionRepository,
    )
    from app.db.adapters.user import (
        SqlAlchemyUserRepository,
        TransactionalUserRepository,
    )
    from app.db.adapters.workflow import (
        TransactionalWorkflowExecutionService,
        TransactionalWorkflowQueryRepository,
    )
    from app.db.oper.agentchat import AgentChatOper
    from app.db.oper.downloadhistory import DownloadHistoryOper
    from app.db.oper.mediaserver import MediaServerOper
    from app.db.oper.message import MessageOper
    from app.db.oper.passkey import PassKeyOper
    from app.db.oper.site import SiteOper
    from app.db.oper.subscribe import SubscribeOper
    from app.db.oper.subscribehistory import SubscribeHistoryOper
    from app.db.oper.transferhistory import TransferHistoryOper
    from app.db.oper.workflow import WorkflowOper

    def create_sync_session() -> Session:
        """为无显式会话的 Oper 测试入口创建独占同步 Session。"""
        return SessionFactory()

    transaction_runner = TransactionalWriteRunner(
        sync_session=create_sync_session,
        async_session=async_session_scope,
    )
    configure_transaction_runners(
        sync=transaction_runner.sync,
        async_=transaction_runner.async_,
    )

    workflow_execution = TransactionalWorkflowExecutionService(SessionFactory)
    configure_workflow_execution(workflow_execution)

    configure_api_data_ports(
        sync_session=get_db,
        async_session=get_async_db,
        repositories={
            "download_history": DownloadHistoryOper,
            "media_server": MediaServerOper,
            "message": MessageOper,
            "passkey": PassKeyOper,
            "site": SiteOper,
            "subscribe": SubscribeOper,
            "subscribe_history": SubscribeHistoryOper,
            "transfer_history": TransferHistoryOper,
            "user": SqlAlchemyUserRepository,
            "workflow": WorkflowOper,
        },
        standalone={
            "passkey": PassKeyOper,
            "system_config": SystemConfigOper,
            "user": lambda: TransactionalUserRepository(
                sync_session=SessionFactory,
                async_session=async_session_scope,
            ),
        },
        unit_of_work={
            "async": SqlAlchemyAsyncUnitOfWork,
            "sync": SqlAlchemyUnitOfWork,
        },
    )
    def site_repository() -> TransactionalSiteRepository:
        """按生产组合根方式创建显式事务站点仓储。"""
        return TransactionalSiteRepository(
            sync_session=SessionFactory,
            async_session=async_session_scope,
        )

    def user_repository() -> TransactionalUserRepository:
        """按生产组合根方式创建用户短会话仓储。"""
        return TransactionalUserRepository(
            sync_session=SessionFactory,
            async_session=async_session_scope,
        )

    subscription_repository = TransactionalSubscriptionRepository(
        sync_session=SessionFactory,
        async_session=async_session_scope,
    )
    download_history_repository = TransactionalDownloadHistoryRepository(
        sync_session=SessionFactory,
        async_session=async_session_scope,
    )
    transfer_history_repository = TransactionalTransferHistoryRepository(
        sync_session=SessionFactory,
        async_session=async_session_scope,
    )
    message_queue = MessageQueueManager(auto_start=False)
    configure_chain_runtime_context_provider(
        lambda: ChainRuntimeContext(
            module_manager=ModuleManager(),
            plugin_manager=PluginManager(),
            event_manager=EventManager(),
            message_oper=MessageOper(),
            message_helper=MessageHelper(),
            file_cache=FileCache(),
            async_file_cache=AsyncFileCache(),
            message_queue=message_queue,
            module_dispatcher_factory=ModuleInvocationDispatcher,
            site_repository=site_repository(),
            subscription_repository=subscription_repository,
            subscription_mutation_scope=subscription_mutation_scope,
            sync_subscription_mutation_scope=sync_subscription_mutation_scope,
            subscription_delete_scope=delete_subscribe_scope,
            sync_subscription_delete_scope=sync_delete_subscribe_scope,
            subscription_completion_scope=subscription_completion_scope,
            rule_group_mutation_scope=partial(
                rule_group_mutation_scope,
                system_config.publish_many,
            ),
            site_reference_mutation_scope=partial(
                site_reference_mutation_scope,
                system_config.publish_many,
            ),
            download_history_repository=download_history_repository,
            transfer_history_repository=transfer_history_repository,
            transfer_admission_repository=TransactionalTransferAdmissionRepository(SessionFactory),
            transfer_execution_repository=TransactionalTransferExecutionRepository(SessionFactory),
            media_server_repository=TransactionalMediaServerRepository(SessionFactory),
            download_failure_repository=TransactionalDownloadFailureRepository(SessionFactory),
            user_repository=user_repository(),
            configuration=build_chain_runtime_config(settings),
        )
    )
    from app.startup.initializers.chain import init_chain_ports, reset_chain_ports
    from app.startup.initializers.network import (
        init_chain_network_ports,
        reset_chain_network_ports,
    )

    init_chain_ports()
    init_chain_network_ports()
    configure_site_query_service(SiteQueryService(repository=site_repository()))
    configure_site_health_service(SiteHealthService(repository=site_repository()))
    configure_workflow_query(
        WorkflowQueryService(
            repository=TransactionalWorkflowQueryRepository(
                sync_session=SessionFactory,
                async_session=async_session_scope,
            )
        )
    )
    from app.db.adapters.subscription import (
        TransactionalSubscriptionHistoryRepository,
    )

    agent_chat_persistence = AgentChatPersistenceService(
        repository=lambda session: AgentChatOper(session),
        async_executor=database_executor,
        sync_transaction=transaction_runner.sync,
    )
    agent_chat_service = AgentChatService(repository=AgentChatOper())
    agent_task_repository = TransactionalAgentTaskRepository(SessionFactory)
    agent_data_context = AgentDataContext(
        chat=agent_chat_service,
        chat_persistence=agent_chat_persistence,
        tasks=agent_task_repository,
        users=user_repository(),
        sites=site_repository(),
        subscriptions=subscription_repository,
        subscription_mutation_scope=subscription_mutation_scope,
        subscription_delete_scope=delete_subscribe_scope,
        async_rule_group_mutation_scope=partial(
            async_rule_group_mutation_scope,
            system_config.publish_many,
        ),
        subscription_history=TransactionalSubscriptionHistoryRepository(
            async_session=async_session_scope,
        ),
        transfer_history=transfer_history_repository,
        transfer_execution=TransactionalTransferExecutionRepository(SessionFactory),
        download_history=download_history_repository,
        plugin_data=TransactionalPluginDataRepository(async_session_scope),
    )
    configure_agent_task_execution(
        AgentTaskExecutionService(
            repository=SessionAgentTaskRepository,
            async_executor=database_executor,
            sync_transaction=transaction_runner.sync,
        )
    )
    configure_agent_chat_persistence(agent_chat_persistence)
    configure_agent_chat_service(agent_chat_service)
    from app.agent.tools.manager import moviepilot_tool_manager
    from app.scheduler.facade import Scheduler

    moviepilot_tool_manager.set_data_context(agent_data_context)
    Scheduler().configure_agent_tasks(agent_task_repository)
    from app.adapters.external.plugin.client import (
        VERSION_BACKWARD_COMPATIBLE_FLAGS,
        PluginMarketClient,
        PluginMarketTransport,
        PluginPackageSourceClient,
    )
    from app.adapters.system.plugin.dependency import PluginDependencyInstaller
    from app.adapters.system.plugin.manifest import dependency_manifest_status
    from app.adapters.system.plugin.package import PluginPackageManager
    from app.runtime.extensions.plugin.system import (
        PluginSystemServices,
        configure_plugin_system,
        reset_plugin_system,
    )

    market_transport = PluginMarketTransport()
    configure_plugin_system(
        PluginSystemServices(
            market=PluginMarketClient(market_transport),
            package=PluginPackageManager(
                source=PluginPackageSourceClient(market_transport),
            ),
            dependency=PluginDependencyInstaller(),
            dependency_manifest_status=dependency_manifest_status,
            compatible_flags=lambda flag: [flag] + VERSION_BACKWARD_COMPATIBLE_FLAGS.get(flag, []) if flag else [],
            frozen=lambda: False,
            install=lambda **_kwargs: (False, "测试环境未装配插件安装 Gateway"),
        )
    )
    from app.agent.llm.gateway import register_llm_provider_runtime
    from app.agent.llm.provider import LLMProviderManager
    from app.agent.skills.registry import SkillHelper
    from app.application.messaging.skill import register_skill_catalog_provider

    register_skill_catalog_provider(lambda: SkillHelper())
    register_llm_provider_runtime(lambda: LLMProviderManager())
    yield
    reset_chain_network_ports()
    reset_chain_ports()
    reset_plugin_system()
    reset_plugin_runtime_factory()


class DbHarness:
    """真实数据库会话的测试载具。

    ``prepare_backend`` 已把 CONFIG_DIR 指向临时目录并建好表，操作的是一次性数据库；
    但同一次 pytest 会话内所有用例共用这一个库，因此清理必须精确到行——按主键水位回收
    用例新增的数据，而不是 truncate 整表，否则会连带删掉其他用例依赖的数据。

    水位法同时覆盖「被测代码自己写入的行」：只要在写入前登记过该表，其后新增的行
    都会被回收，测试不必持有每一个模型实例的句柄。
    """

    def __init__(self, session):
        self.session = session
        self._watermarks = {}

    def watermark(self, *models) -> None:
        """
        登记若干表的当前最大主键，用例结束时删除其后新增的全部行。
        :param models: 需要纳入回收的模型类
        """
        from sqlalchemy import func, select

        for model in models:
            if model in self._watermarks:
                continue
            current = self.session.execute(select(func.max(model.id))).scalar()
            self._watermarks[model] = current or 0

    def add(self, *rows):
        """
        写入若干行并提交，返回单行或行列表。

        写入前自动登记水位，因此这些行以及被测代码后续新增的同表行都会被回收。
        :param rows: 待写入的模型实例
        """
        self.watermark(*{type(row) for row in rows})
        for row in rows:
            self.session.add(row)
        self.session.commit()
        return rows[0] if len(rows) == 1 else list(rows)

    def run_async_session(
        self,
        operation: Callable[[AsyncSession], Awaitable[TResult]],
    ) -> TResult:
        """在临时数据库的显式 AsyncSession 中执行被测操作。"""
        from app.db.session import async_session_scope

        async def execute() -> TResult:
            """打开异步会话并把事务所有权留在测试载具。"""
            async with async_session_scope() as session:
                return await operation(session)

        return asyncio.run(execute())

    def cleanup(self) -> None:
        """按水位删除本用例新增的全部行。"""
        from sqlalchemy import delete

        # 用例可能因约束冲突等原因让事务处于待回滚状态，此时任何语句都会被拒绝；
        # 先回滚再清理，否则清理会整体失效、数据泄漏到后续用例
        try:
            self.session.rollback()
        except Exception:  # noqa: BLE001  会话已不可用时也要继续尝试清理
            pass

        from app.db.base import Base

        table_order = {table: index for index, table in enumerate(Base.metadata.sorted_tables)}
        models = sorted(
            self._watermarks,
            key=lambda model: table_order.get(model.__table__, -1),
            reverse=True,
        )
        for model in models:
            mark = self._watermarks[model]
            try:
                self.session.execute(delete(model).where(model.id > mark))
                self.session.commit()
            except Exception:  # noqa: BLE001  清理失败不应掩盖用例本身的断言结果
                self.session.rollback()


@pytest.fixture
def db():
    """
    提供真实数据库会话载具，用例结束按主键水位回收新增数据。

    数据库查询方法的行为（过滤、排序、分页、去重）无法用替身验证——替身只能证明
    「调用了什么」，证明不了「查回了什么」，而 1.x Query 到 2.0 select 的改写恰恰
    只可能在后者上出偏差。
    """
    from app.db.session import ScopedSession

    session = ScopedSession()
    harness = DbHarness(session)
    try:
        yield harness
    finally:
        harness.cleanup()
        session.close()


@pytest.fixture
def frozen_now(monkeypatch):
    """
    冻结指定模块看到的 ``time.time()``，其余时间函数原样透传标准库。

    形如 ``date >= now - 86400 * days`` 的时间窗查询，窗口起点要到调用那一刻才算得出来，
    不冻结就没法把数据精确摆在窗口起点上——而边界恰恰是 ``>=`` 与 ``>`` 唯一的分界，
    数据不压在边界上，比较符写错也查不出来。

    :return: ``freeze(module) -> float``，冻结该模块的时钟并返回冻结时刻的时间戳
    """
    import time as real_time

    class _FrozenClock:
        """只冻结 ``time()``，``localtime``/``strftime`` 等仍走标准库。"""

        def __init__(self, now: float):
            self.now = now

        def time(self) -> float:
            return self.now

        def __getattr__(self, name):
            return getattr(real_time, name)

    def freeze(module) -> float:
        """
        把模块内的 ``time`` 名字换成冻结时钟。
        :param module: 被测代码所在模块（其内以 ``time.time()`` 取当前时刻）
        :return: 冻结时刻的时间戳
        """
        clock = _FrozenClock(real_time.time())
        monkeypatch.setattr(module, "time", clock)
        return clock.now

    return freeze


def _report_session_cleanup_error(session, name: str, err: Exception) -> None:
    """记录收尾错误；原测试绿色时将会话标记为失败。"""
    sys.stderr.write(f"\npytest session cleanup failed: {name}: {err!r}\n")
    if session.exitstatus == 0:
        session.exitstatus = 1


def pytest_sessionfinish(session, exitstatus):
    """释放测试过程中按需创建的全局后台资源，避免解释器退出时等待非 daemon worker。"""
    try:
        from app.agent.tools.base import shutdown_blocking_executors

        shutdown_blocking_executors(cancel_futures=True)
    except Exception as err:
        _report_session_cleanup_error(session, "agent blocking executors", err)

    try:
        from app.runtime.thread import ThreadHelper

        helper = ThreadHelper.get_existing_instance()
        if helper and helper.shutdown() is False:
            raise RuntimeError("shared thread pool did not converge")
    except Exception as err:
        _report_session_cleanup_error(session, "thread helper", err)

    try:
        from app.application.messaging.message import stop_message

        stop_message()
    except Exception as err:
        _report_session_cleanup_error(session, "message service", err)

    try:
        from app.runtime.log import LoggerManager

        if LoggerManager.shutdown() is False:
            raise RuntimeError("log writer did not converge")
    except Exception as err:
        _report_session_cleanup_error(session, "logger manager", err)
