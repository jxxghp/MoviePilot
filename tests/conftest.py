"""
pytest 全局引导、隔离装配、数据库测试载具与资源收尾。

配置目录隔离及网络守卫复用 app.testing；当前模块在隔离数据库上装配应用端口，
为测试提供配置替换、缓存重装配和精确数据清理能力，不执行完整应用启动流程。
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

# 隔离配置后才导入自动启用的网络守卫；pytest 需要保留此未直接调用的导入。
from app.testing.network import block_real_network  # noqa: E402,F401

TResult = TypeVar("TResult")


class _TestDatabaseExecutor:
    """
    为未执行完整启动流程的测试提供同步数据库操作的线程入口。
    """

    async def run(self, operation):
        """
        在线程中执行一个同步数据库操作。

        :param operation: 无需额外参数的同步回调，其事务边界由回调定义
        :return: 回调结果；回调异常向上传播
        """
        return await asyncio.to_thread(operation)


class _TestRuntimeSettingsProxy:
    """
    把历史配置替换点转发到隔离测试使用的配置对象。

    记录每个字段首次写入前的值；删除代理属性时恢复原值或删除新增字段。
    直接赋值不会在代理销毁时自动恢复，测试仍需使用可撤销的替换工具。

    :ivar _originals: 字段名到是否原先存在及原值的映射
    """

    def __init__(self) -> None:
        """
        创建用于恢复被替换配置字段的记录表。
        """
        self._originals: dict[str, tuple[bool, object]] = {}

    def __getattr__(self, key: str):
        """
        从隔离测试配置中读取字段。

        :param key: 配置字段名
        :return: 当前字段值；不存在时保留原 AttributeError
        """
        from app.runtime.config import settings

        return getattr(settings, key)

    def __setattr__(self, key: str, value):
        """
        记录字段原值后写入隔离测试配置。

        同一字段多次写入仍保留首次写入前的值。

        :param key: 被替换的配置字段名；_originals 属于代理自身
        :param value: 测试需要使用的临时字段值
        """
        if key == "_originals":
            object.__setattr__(self, key, value)
            return
        from app.runtime.config import settings

        if key not in self._originals:
            self._originals[key] = (hasattr(settings, key), getattr(settings, key, None))
        setattr(settings, key, value)

    def __delattr__(self, key: str) -> None:
        """
        撤销一个已登记的配置字段替换。

        :raises AttributeError: 字段没有可恢复的替换记录
        :param key: 此前通过代理写入的字段名
        """
        if key in self._originals:
            from app.runtime.config import settings

            had_value, original = self._originals.pop(key)
            if had_value:
                setattr(settings, key, original)
            elif hasattr(settings, key):
                delattr(settings, key)
            return
        raise AttributeError(key)


@pytest.fixture
def compose_cache_backend():
    """
    提供缓存后端重装配入口，并在用例结束时恢复原配置和装配。

    仅修改配置不会切换已创建的缓存路由，必须重新调用启动组合根。恢复不依赖其他夹具撤销顺序。

    :yields: 接受缓存类型字符串的同步装配函数
    """
    from app.runtime.config import settings
    from app.startup.composition.cache import configure_cache_composition

    original = settings.CACHE_BACKEND_TYPE

    def compose(backend_type: str) -> None:
        """
        设置缓存类型并立即重装配平台缓存。

        :param backend_type: 运行配置支持的缓存类型标识
        """
        settings.CACHE_BACKEND_TYPE = backend_type
        configure_cache_composition()

    try:
        yield compose
    finally:
        settings.CACHE_BACKEND_TYPE = original
        configure_cache_composition()


@pytest.fixture(autouse=True)
def install_runtime_settings_test_proxies(monkeypatch):
    """
    向已导入的历史配置替换点注入本用例专用代理。

    配置字段本身仍由测试使用的替换工具恢复，代理不会在销毁时自动回滚全部字段。

    :yields: 已安装配置代理的测试执行阶段，不提供额外值
    :param monkeypatch: 负责用例结束时撤销模块属性注入的 pytest 夹具
    """
    proxy = _TestRuntimeSettingsProxy()
    _install_runtime_settings_test_proxies(proxy, monkeypatch)
    yield


def _install_runtime_settings_test_proxies(proxy, monkeypatch=None) -> None:
    """
    为已导入且使用运行配置接口的宿主模块补充测试替换点。

    仅扫描模块、Agent、启动、适配器及主入口；已有 settings 属性时不覆盖。

    :param proxy: 用于转发配置字段读写的测试对象
    :param monkeypatch: 可选的属性撤销工具；None 时直接写模块属性并保留到后续清理
    """
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
    """
    在用例执行前为显式夹具新导入的模块补充配置替换点。

    直接注入代理，不使用自动撤销工具；配置字段的替换仍由测试自身负责恢复。

    :param item: pytest 当前用例节点，当前实现不读取其内容
    """
    _install_runtime_settings_test_proxies(_TestRuntimeSettingsProxy())


@pytest.fixture(autouse=True)
def configure_plugin_system_services():
    """
    为单元测试装配应用、存储和插件端口，复用隔离数据库。

    替换运行配置、事务、Chain、Agent 与插件服务的进程级提供方；不执行完整应用生命周期。
    收尾显式重置 Chain 网络端口、Chain 端口、插件系统及插件运行时工厂；其他测试专用装配
    由下一用例重装或会话结束清理，不承诺恢复到夹具调用前的全部全局状态。

    :yields: 应用端口已装配的测试执行阶段，不提供额外值
    """
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
    from app.runtime.extensions.plugin.storage import (
        get_plugin_instance_directory,
        get_plugin_storage,
    )
    from app.runtime.extensions.plugin.system import get_plugin_system
    from app.runtime.extensions.service import ServiceConfigHelper
    from app.startup.initializers.plugins import (
        _clear_plugin_default_target,
        _read_plugin_runtime_declaration,
        _set_plugin_default_target,
    )

    configure_service_directory(
        configs=ServiceConfigHelper.get_configs,
        modules=lambda module_type: ModuleManager().get_running_type_modules(module_type),
    )

    def build_test_plugin_runtime(host):
        """
        在测试组合根构造插件运行时及其依赖适配器。

        :param host: 需要插件运行时的管理器实例，提供远端入口查询
        :return: 绑定测试配置、存储及生命周期钩子的插件运行时
        """
        return build_plugin_runtime(
            host,
            PluginRuntimeEnvironment(
                plugins_root=settings.ROOT_PATH / "app" / "plugins",
                storage=get_plugin_storage,
                instance_directory=get_plugin_instance_directory,
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
                set_default_target=_set_plugin_default_target,
                clear_default_target=_clear_plugin_default_target,
                runtime_declaration=_read_plugin_runtime_declaration,
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
        """
        为无显式会话的持久化测试入口创建独占同步会话。

        :return: 新的隔离数据库会话，由事务执行器负责关闭
        """
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
        """
        按生产组合方式构造显式事务站点仓储。

        :return: 使用隔离同步、异步会话工厂的站点仓储
        """
        return TransactionalSiteRepository(
            sync_session=SessionFactory,
            async_session=async_session_scope,
        )

    def user_repository() -> TransactionalUserRepository:
        """
        按生产组合方式构造用户短会话仓储。

        :return: 使用隔离同步、异步会话工厂的用户仓储
        """
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
    """
    为隔离数据库测试记录新增行水位并在用例结束时回收。

    同一 pytest 会话共用隔离库，只删除已登记表中主键大于初始水位的行。
    不恢复原有行的修改或删除，也不回收未登记的表；这些操作需要测试另行隔离。

    :ivar session: 供用例读写及清理使用的同步数据库会话
    :ivar _watermarks: 已登记模型到初始最大主键的映射
    """

    def __init__(self, session):
        """
        绑定测试会话并创建空的表水位记录。

        :param session: 由 db 夹具创建并负责关闭的同步会话
        """
        self.session = session
        self._watermarks = {}

    def watermark(self, *models) -> None:
        """
        登记表的初始最大主键，重复登记不改变水位。

        必须在被测操作新增数据之前登记；已有水位保留首次值。

        :param models: 需要回收新增行且提供 id 字段的模型类
        """
        from sqlalchemy import func, select

        for model in models:
            if model in self._watermarks:
                continue
            current = self.session.execute(select(func.max(model.id))).scalar()
            self._watermarks[model] = current or 0

    def add(self, *rows):
        """
        登记对应表水位后插入测试数据并提交事务。

        同表中随后由被测代码新增的较大主键行也会纳入清理。

        :param rows: 要插入的模型实例
        :return: 单个输入返回该实例，多个或零个输入返回列表
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
        """
        在新事件循环的异步会话中执行被测操作。

        调用 asyncio.run，不适用于当前线程已有运行中事件循环的场景；会话在作用域结束时关闭。

        :param operation: 接收异步会话的协程回调，提交或回滚由回调负责
        :return: 等待回调后的结果
        """
        from app.db.session import async_session_scope

        async def execute() -> TResult:
            """
            打开异步会话并等待测试回调，不代替回调提交事务。

            :return: 回调返回值
            """
            async with async_session_scope() as session:
                return await operation(session)

        return asyncio.run(execute())

    def cleanup(self) -> None:
        """
        尽力按主键水位回收已登记表中的新增行。

        先回滚残留事务，再按外键依赖的反向顺序逐表删除和提交；单表清理失败会回滚该事务，
        继续后续表，避免覆盖测试原有断言结果。
        """
        from sqlalchemy import delete

        # 用例可能因约束冲突等原因让事务处于待回滚状态，此时任何语句都会被拒绝；
        # 先回滚再清理，否则清理会整体失效、数据泄漏到后续用例
        try:
            self.session.rollback()
        # 会话已不可用时仍继续尝试逐表清理，避免影响其他用例。
        except Exception:  # noqa: BLE001
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
            # 清理失败不应掩盖用例本身的断言结果，回滚后继续其他表。
            except Exception:  # noqa: BLE001
                self.session.rollback()


@pytest.fixture
def db():
    """
    提供隔离数据库的同步测试载具，并在结束时清理新增行和关闭会话。

    只有通过载具登记过水位的表会回收新增行；对旧行的修改或删除需由用例单独处理。

    :yields: 绑定当前同步会话的 DbHarness
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
    提供只冻结指定模块 time.time 的局部时钟替换工具。

    其他时间函数仍委托标准库，便于验证时间窗口包含或排除边界的行为。

    :param monkeypatch: 在用例结束时恢复模块 time 属性的 pytest 夹具
    :return: freeze(module) 函数，安装冻结时钟并返回冻结的 Unix 秒数
    """
    import time as real_time

    class _FrozenClock:
        """
        固定时间戳并将其他时间能力透传给标准库。

        :ivar now: 构造时保存的 Unix 时间戳，单位为秒
        """

        def __init__(self, now: float):
            """
            保存本次冻结的时间点。

            :param now: Unix 时间戳，单位为秒
            """
            self.now = now

        def time(self) -> float:
            """
            读取固定的当前时间。

            :return: 构造时记录的 Unix 秒数
            """
            return self.now

        def __getattr__(self, name):
            """
            将未覆盖的时间属性委托给标准库 time。

            :param name: 标准库时间属性或函数名
            :return: 原始属性或函数，不改变其行为
            """
            return getattr(real_time, name)

    def freeze(module) -> float:
        """
        以当前实际时间为基准冻结指定模块的时钟。

        :param module: 使用 time.time 取当前时间的被测模块
        :return: 冻结的 Unix 秒级时间戳
        """
        clock = _FrozenClock(real_time.time())
        monkeypatch.setattr(module, "time", clock)
        return clock.now

    return freeze


def _report_session_cleanup_error(session, name: str, err: Exception) -> None:
    """
    记录资源收尾错误，并把原本成功的测试会话标为失败。

    :param session: 可更新退出状态的 pytest 会话对象
    :param name: 失败资源的诊断名称
    :param err: 收尾过程中捕获的异常
    """
    sys.stderr.write(f"\npytest session cleanup failed: {name}: {err!r}\n")
    if session.exitstatus == 0:
        session.exitstatus = 1


def pytest_sessionfinish(session, exitstatus):
    """
    在会话结束时关闭按需创建的后台执行器、消息服务和日志资源。

    各资源分别尝试关闭，失败时记录错误并继续；原测试成功时，收尾失败使会话退出状态变为失败。

    :param session: 当前 pytest 会话，收尾失败时可修改其退出状态
    :param exitstatus: pytest 原始退出状态，当前实现直接读取 session.exitstatus
    """
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


@pytest.fixture
def terminal_scope():
    """
    为终端契约测试绑定固定宿主身份，并在退出时恢复原上下文。

    :yields: 用户 terminal-test-owner、任务 terminal-test 和 test 类型组成的终端作用域
    """
    from app.agent.terminal.ownership import TerminalScope, bind_terminal_scope

    scope = TerminalScope(user_id="terminal-test-owner", task_id="terminal-test", kind="test")
    with bind_terminal_scope(scope):
        yield scope
