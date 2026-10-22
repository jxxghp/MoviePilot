"""
为一次命令行 Agent 请求装配数据、配置和能力运行时，并按依赖顺序收口。

此组合根不启动 Web、前端、站点认证、插件同步或定时服务；按需工具仍使用宿主端口。
"""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from functools import partial
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.runtime.config import Settings
    from app.runtime.tasks import TaskRegistry
    from app.startup.composition.agent import AgentComposition
    from app.startup.composition.classification import ClassificationComposition
    from app.startup.composition.configuration import ConfigurationComposition
    from app.startup.composition.database import DatabaseRuntime


def _compose_cli_agent(
    database: DatabaseRuntime,
    configuration: ConfigurationComposition,
    classification: ClassificationComposition,
    tasks: TaskRegistry,
) -> AgentComposition:
    """
    在线程内复用宿主组合函数，为 Agent 和 Chain 发布同一批持久化端口。
    """
    from app.agent.loader import get_agent_manager
    from app.runtime.extensions.plugin.manager import configure_plugin_runtime_factory
    from app.startup.composition.agent import compose_agent, publish_agent_services
    from app.startup.composition.chain import configure_chain_runtime_context
    from app.startup.composition.database import compose_database_services, publish_database_services
    from app.startup.composition.runtime import compose_runtime_dependencies
    from app.startup.composition.subscription import async_rule_group_mutation_scope
    from app.startup.composition.system import compose_system_service
    from app.startup.initializers.plugins import build_plugin_runtime_graph

    configure_plugin_runtime_factory(build_plugin_runtime_graph)
    publish_database_services(compose_database_services(runtime=database, system_config=configuration.system_config))
    dependencies = compose_runtime_dependencies()
    composition = compose_agent(
        runtime=database,
        system_config=configuration.system_config,
        dependencies=dependencies,
        tasks=tasks,
    )
    system = compose_system_service(
        settings=configuration.settings,
        system_config=configuration.system_service,
        rule_group_mutation=partial(async_rule_group_mutation_scope, configuration.system_config.publish_many),
    )
    configure_chain_runtime_context(
        dependencies=dependencies,
        system_config=configuration.system_config,
        configuration=configuration.runtime.chain,
        classification_service=classification.execution,
        system_service=system,
    )
    publish_agent_services(composition, data_context_registrar=get_agent_manager().configure_data_context)
    return composition


async def _close_cli_extensions(*, events_started: bool) -> None:
    """
    只关闭工具按需物化的宿主模块、插件及共享线程，不反向创建运行时对象。
    """
    from app.runtime.events import EventManager
    from app.runtime.execution import run_in_threadpool_to_completion
    from app.runtime.extensions.module.manager import ModuleManager
    from app.runtime.extensions.plugin.manager import PluginManager
    from app.runtime.thread import ThreadHelper

    plugin = PluginManager.get_existing_instance()
    events = EventManager.get_existing_instance()
    if plugin is not None and not await plugin.quiesce_plugins(timeout=30):
        raise RuntimeError("CLI Agent 插件任务尚未收敛")
    if events_started and events is not None and await events.drain_async(timeout=30, seal=False) is False:
        raise RuntimeError("CLI Agent 在途事件尚未收敛")
    if plugin is not None:
        if not await plugin.quiesce_plugin_services(timeout=30):
            raise RuntimeError("CLI Agent 插件服务尚未收敛")
        if await run_in_threadpool_to_completion(plugin.finalize_plugins) is False:
            raise RuntimeError("CLI Agent 插件 owner 尚未释放")
    modules = ModuleManager.get_existing_instance()
    if modules is not None and await run_in_threadpool_to_completion(modules.shutdown) is False:
        raise RuntimeError("CLI Agent 模块 owner 尚未释放")
    if events is not None:
        await events.stop_async()
    threads = ThreadHelper.get_existing_instance()
    if threads is not None and await run_in_threadpool_to_completion(threads.shutdown) is False:
        raise RuntimeError("CLI Agent 共享线程尚未收敛")


async def run_cli_agent_request(*, message: str, session_id: str, user_id: str, settings: Settings) -> dict[str, str]:
    """
    在独占 CLI 进程完成一轮对话；成功、模型异常或装配失败均按 owner 收尾。

    :param message: 已去除首尾空白的用户输入
    :param session_id: 本次使用的会话标识，可复用同一用户的持久历史
    :param user_id: 本地执行者的用户标识
    :param settings: CLI 入口读取并显式注入的部署配置对象
    :return: 实际会话标识及最终捕获文本
    :raises RuntimeError: Agent 未启用、进程已有数据库 owner，或关键关闭未收敛
    """
    from app.startup.initializers.cache import configure_cache_dependencies

    configure_cache_dependencies()

    from app.adapters.network.http import aclose_shared_async_transports
    from app.agent.loader import activate_agent_service
    from app.db.session import close_database
    from app.runtime.events import EventManager
    from app.runtime.execution import run_in_threadpool_to_completion
    from app.runtime.extensions.plugin.manager import reset_plugin_runtime_factory
    from app.runtime.loop import main_loop_registry
    from app.runtime.tasks import TaskRegistry, configure_task_registry
    from app.schemas.types import NotificationChannel, ReplyMode
    from app.startup.composition.agent import reset_agent_services
    from app.startup.composition.chain import reset_chain_services
    from app.startup.composition.classification import compose_classification
    from app.startup.composition.configuration import compose_configuration, publish_configuration, reset_configuration
    from app.startup.composition.database import (
        database_runtime_active,
        reset_database_services,
        start_database_runtime,
        stop_database_runtime,
    )
    from app.startup.composition.network import configure_application_network_ports, reset_application_network_ports
    from app.startup.initializers.agent import configure_agent_ports, reset_agent_ports, stop_agent
    from app.startup.initializers.chain import init_chain_ports, reset_chain_ports
    from app.startup.initializers.database import prepare_database
    from app.startup.initializers.domain import configure_domain_dependencies
    from app.startup.initializers.modules import configure_runtime_data_providers, reset_runtime_data_providers
    from app.startup.initializers.network import init_chain_network_ports, reset_chain_network_ports
    from app.startup.initializers.site import init_site_access_ports, reset_site_access_ports

    if not settings.AI_AGENT_ENABLE:
        raise RuntimeError("MoviePilot 智能体未启用，请先在配置中打开 AI_AGENT_ENABLE")
    if database_runtime_active():
        raise RuntimeError("CLI Agent 需要独占进程，不能复用已运行的数据库 runtime")

    resources = AsyncExitStack()
    composition = None
    agent_owned = False
    database_owned = False
    events_started = False
    tasks = TaskRegistry()
    loop_owner = main_loop_registry.register(asyncio.get_running_loop())
    configure_task_registry(tasks)
    resources.callback(main_loop_registry.release, loop_owner)
    resources.callback(configure_task_registry, None)
    resources.push_async_callback(aclose_shared_async_transports)
    resources.push_async_callback(close_database)
    try:
        await run_in_threadpool_to_completion(prepare_database)
        database = await start_database_runtime()
        database_owned = True
        configuration = await compose_configuration(executor=database.worker, settings=settings)
        resources.callback(reset_configuration)
        publish_configuration(configuration, settings)
        configure_domain_dependencies()
        classification = await compose_classification(
            executor=database.worker,
            settings=settings,
            system_config=configuration.system_config,
        )
        resources.callback(reset_chain_services)
        resources.callback(reset_agent_services, data_context_resetter=reset_agent_ports)
        resources.callback(reset_plugin_runtime_factory)
        resources.callback(reset_database_services)
        composition = await run_in_threadpool_to_completion(_compose_cli_agent, database, configuration, classification, tasks)
        resources.callback(reset_runtime_data_providers)
        configure_runtime_data_providers()
        resources.callback(reset_application_network_ports)
        configure_application_network_ports()
        resources.callback(reset_chain_ports)
        init_chain_ports()
        resources.callback(reset_chain_network_ports)
        init_chain_network_ports()
        resources.callback(reset_site_access_ports)
        init_site_access_ports()
        EventManager().start()
        events_started = True
        configure_agent_ports()
        agent_owned = True
        manager = await activate_agent_service()
        if manager is None:
            raise RuntimeError("MoviePilot 智能体启动失败")
        output: list[str] = []
        result = await manager.process_message(
            session_id=session_id,
            user_id=user_id,
            message=message,
            channel=NotificationChannel.Web.value,
            source="cli",
            reply_mode=ReplyMode.CAPTURE_ONLY,
            output_callback=output.append,
            allow_message_tools=False,
            wait_for_completion=True,
        )
        return {"session_id": session_id, "result": (result if isinstance(result, str) else "".join(output)).strip()}
    finally:
        # 必须先释放 Agent 及后台任务，才能关闭它们仍可能使用的写入与数据库依赖。
        if agent_owned and await stop_agent() is False:
            raise RuntimeError("CLI Agent 尚有任务未收敛，保留其数据库依赖")
        if await tasks.shutdown(timeout_seconds=30) is False:
            raise RuntimeError("CLI Agent 后台任务未收敛，保留其数据库依赖")
        if composition is not None:
            composition.persistence.begin_shutdown()
            await composition.persistence.shutdown()
        await _close_cli_extensions(events_started=events_started)
        if database_owned:
            await stop_database_runtime()
        await resources.aclose()
