"""Agent 数据上下文与类型化任务适配器测试。"""

from contextlib import asynccontextmanager
from dataclasses import FrozenInstanceError, replace
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest

import app.agent.tools.factory as tool_factory_module
import app.application.agent as agent_services
from app.agent.memory import MemoryManager
from app.agent.tools.factory import MoviePilotToolFactory
from app.agent.tools.impl.agent_task import AgentTaskTool
from app.agent.tools.manager import MoviePilotToolsManager
from app.application.agent import AgentDataContext
from app.application.agenttask import AgentTaskRepository, AgentTaskSnapshot
from app.application.filtering import FilterRuleService
from app.db.adapters.agent import TransactionalAgentTaskRepository
from app.db.session import SessionFactory

# pylint: disable=no-name-in-module  # Scheduler 由惰性稳定入口导出，Pylint 无法静态解析。
from app.scheduler import Scheduler
from app.schemas.rule import CustomRule
from app.schemas.system import FilterRuleGroup
from app.schemas.types import SystemConfigKey
from app.startup.initializers import agent as agent_initializer


def _context(tasks: AgentTaskRepository) -> AgentDataContext:
    """构造仅用于验证依赖身份的 Agent 数据上下文。"""
    dependency = cast(object, SimpleNamespace())
    return AgentDataContext(
        chat=cast(object, dependency),
        chat_persistence=cast(object, dependency),
        tasks=tasks,
        users=cast(object, dependency),
        sites=cast(object, dependency),
        subscriptions=cast(object, dependency),
        subscription_mutation_scope=cast(object, dependency),
        subscription_delete_scope=cast(object, dependency),
        async_rule_group_mutation_scope=cast(object, dependency),
        subscription_history=cast(object, dependency),
        transfer_history=cast(object, dependency),
        transfer_execution=cast(object, dependency),
        download_history=cast(object, dependency),
        plugin_data=cast(object, dependency),
    )


def test_agent_tool_receives_exact_data_context() -> None:
    """内置工具只能消费构造时注入的同一上下文，不再查询全局注册表。"""
    repository = TransactionalAgentTaskRepository(SessionFactory)
    context = _context(repository)
    tool = AgentTaskTool(session_id="session", user_id="user", data=context)

    assert tool.data is context
    assert tool.data.tasks is repository


@pytest.mark.asyncio
async def test_delete_rule_group_uses_injected_atomic_mutation_scope(monkeypatch) -> None:
    """Agent 删除规则组必须一次提交定义和全部引用并在释放作用域后广播。"""
    mutation = MagicMock()
    mutation.apply = AsyncMock(return_value=SimpleNamespace(to_dict=lambda: {"subscribes": []}))

    @asynccontextmanager
    async def mutation_scope():
        """提供可观测的异步规则组事务作用域。"""
        yield mutation

    context = replace(
        _context(TransactionalAgentTaskRepository(SessionFactory)),
        async_rule_group_mutation_scope=mutation_scope,
    )
    monkeypatch.setattr(
        "app.application.filtering.get_rule_groups",
        lambda: [FilterRuleGroup(name="old", rule_string="4K")],
    )
    publish = AsyncMock()
    service = FilterRuleService(context.subscriptions, mutation_scope, publish)

    result = await service.delete_group("old")

    assert result["count"] == 0
    mutation.apply.assert_awaited_once_with(
        [],
        expected_rule_groups=[{"name": "old", "rule_string": "4K"}],
        previous_name="old",
    )
    publish.assert_awaited_once()


@pytest.mark.asyncio
async def test_custom_rule_rename_commits_rule_and_group_definitions_together(
    monkeypatch,
) -> None:
    """自定义规则改名必须用一个事务同时提交规则本体和规则组表达式。"""
    mutation = MagicMock()
    mutation.apply = AsyncMock(return_value=SimpleNamespace())

    @asynccontextmanager
    async def mutation_scope():
        """提供可观测的异步组合事务作用域。"""
        yield mutation

    context = replace(
        _context(TransactionalAgentTaskRepository(SessionFactory)),
        async_rule_group_mutation_scope=mutation_scope,
    )
    monkeypatch.setattr(
        "app.application.filtering.get_custom_rules",
        lambda: [CustomRule(id="OLD", name="旧规则", include="old")],
    )
    monkeypatch.setattr(
        "app.application.filtering.get_rule_groups",
        lambda: [FilterRuleGroup(name="group", rule_string="OLD & 4K")],
    )
    publish = AsyncMock()
    service = FilterRuleService(context.subscriptions, mutation_scope, publish)

    result = await service.update_custom(
        current_rule_id="OLD",
        new_rule_id="NEW",
    )

    assert result["custom_rule"]["id"] == "NEW"
    mutation.apply.assert_awaited_once_with(
        [{"name": "group", "rule_string": "NEW & 4K"}],
        expected_rule_groups=[{"name": "group", "rule_string": "OLD & 4K"}],
        custom_rules=[{"id": "NEW", "name": "旧规则", "include": "old"}],
        expected_custom_rules=[{"id": "OLD", "name": "旧规则", "include": "old"}],
    )
    assert publish.await_count == 2


@pytest.mark.asyncio
async def test_custom_rule_reorder_preserves_latest_definitions_and_checks_expected_order(
    monkeypatch,
) -> None:
    """自定义规则重排只能改变顺序，并拒绝过期顺序覆盖当前列表。"""
    mutation = MagicMock()
    mutation.apply = AsyncMock(return_value=SimpleNamespace())

    @asynccontextmanager
    async def mutation_scope():
        """提供可观测的异步组合事务作用域。"""
        yield mutation

    rules = [
        CustomRule(id="A", name="A", include="latest-a"),
        CustomRule(id="B", name="B", exclude="latest-b"),
    ]
    groups = [FilterRuleGroup(name="group", rule_string="A > B")]
    monkeypatch.setattr("app.application.filtering.get_custom_rules", lambda: rules)
    monkeypatch.setattr("app.application.filtering.get_rule_groups", lambda: groups)
    publish = AsyncMock()
    service = FilterRuleService(cast(object, MagicMock()), mutation_scope, publish)

    result = await service.reorder_custom(["B", "A"], expected_rule_ids=["A", "B"])

    assert result["rule_ids"] == ["B", "A"]
    mutation.apply.assert_awaited_once_with(
        [{"name": "group", "rule_string": "A > B"}],
        expected_rule_groups=[{"name": "group", "rule_string": "A > B"}],
        custom_rules=[
            {"id": "B", "name": "B", "exclude": "latest-b"},
            {"id": "A", "name": "A", "include": "latest-a"},
        ],
        expected_custom_rules=[
            {"id": "A", "name": "A", "include": "latest-a"},
            {"id": "B", "name": "B", "exclude": "latest-b"},
        ],
    )
    publish.assert_awaited_once_with(
        SystemConfigKey.CustomFilterRules,
        [
            {"id": "B", "name": "B", "exclude": "latest-b"},
            {"id": "A", "name": "A", "include": "latest-a"},
        ],
    )

    with pytest.raises(ValueError, match="顺序已被其他请求修改"):
        await service.reorder_custom(["B", "A"], expected_rule_ids=["B", "A"])


@pytest.mark.asyncio
async def test_rule_group_reorder_uses_atomic_scope_and_rejects_changed_collection(
    monkeypatch,
) -> None:
    """规则组重排必须走原子作用域，并拒绝缺项或新增项的列表。"""
    mutation = MagicMock()
    mutation.apply = AsyncMock(return_value=SimpleNamespace())

    @asynccontextmanager
    async def mutation_scope():
        """提供可观测的规则组异步事务作用域。"""
        yield mutation

    groups = [
        FilterRuleGroup(name="first", rule_string="4K"),
        FilterRuleGroup(name="second", rule_string="1080P"),
    ]
    monkeypatch.setattr("app.application.filtering.get_rule_groups", lambda: groups)
    publish = AsyncMock()
    service = FilterRuleService(cast(object, MagicMock()), mutation_scope, publish)

    result = await service.reorder_groups(
        ["second", "first"],
        expected_group_names=["first", "second"],
    )

    assert result["group_names"] == ["second", "first"]
    mutation.apply.assert_awaited_once_with(
        [
            {"name": "second", "rule_string": "1080P"},
            {"name": "first", "rule_string": "4K"},
        ],
        expected_rule_groups=[
            {"name": "first", "rule_string": "4K"},
            {"name": "second", "rule_string": "1080P"},
        ],
    )
    publish.assert_awaited_once()

    with pytest.raises(ValueError, match="集合已变化"):
        await service.reorder_groups(["first"])


def test_agent_service_facade_resolves_registered_dependencies(monkeypatch) -> None:
    """Agent 服务门面应稳定处理未装配状态并转发组合根注入能力。"""
    provider_names = (
        "_agent_manager_provider",
        "_running_agent_manager_provider",
        "_prompt_manager_provider",
        "_agent_capability_manager_provider",
        "_llm_helper_provider",
        "_manual_redo_prompt_builder_provider",
    )
    for provider_name in provider_names:
        monkeypatch.setattr(agent_services, provider_name, None)

    assert agent_services.get_running_agent_manager() is None
    with pytest.raises(RuntimeError, match="agent_manager 未注册"):
        agent_services.get_agent_manager()

    manager = object()
    prompt_manager = object()
    capability_manager = MagicMock()
    capability_manager.is_audio_input_available.return_value = True
    capability_manager.transcribe_audio.return_value = "转写结果"
    llm_helper = MagicMock()
    llm_helper.supports_image_input.return_value = True
    agent_services.register_agent_services(
        agent_manager=manager,
        prompt_manager=prompt_manager,
        capability_manager=capability_manager,
        llm_helper=llm_helper,
    )

    assert agent_services.get_agent_manager() is manager
    assert agent_services.get_running_agent_manager() is manager
    assert agent_services.get_prompt_manager() is prompt_manager
    assert agent_services.supports_image_input(
        provider="openai",
        model="vision-model",
        base_url="https://example.com",
        base_url_preset="custom",
    )
    llm_helper.supports_image_input.assert_called_once_with(
        provider="openai",
        model="vision-model",
        base_url="https://example.com",
        base_url_preset="custom",
    )
    assert agent_services.is_audio_input_available()
    assert agent_services.transcribe_audio(b"audio", filename="voice.wav") == "转写结果"
    capability_manager.transcribe_audio.assert_called_once_with(
        b"audio",
        filename="voice.wav",
    )
    with pytest.raises(RuntimeError, match="提示词构建器未注册"):
        agent_services.build_manual_redo_prompt(object())

    agent_services.register_agent_services(
        agent_manager=manager,
        prompt_manager=prompt_manager,
        capability_manager=capability_manager,
        llm_helper=llm_helper,
        manual_redo_prompt_builder=lambda history: f"redo:{history}",
    )
    assert agent_services.build_manual_redo_prompt("history") == "redo:history"


def test_tool_factory_injects_context_only_into_builtin_tools(monkeypatch) -> None:
    """工具工厂必须把上下文传给宿主工具，同时保持插件构造 ABI 不变。"""
    repository = TransactionalAgentTaskRepository(SessionFactory)
    context = _context(repository)
    monkeypatch.setattr(
        MoviePilotToolFactory,
        "_get_builtin_tool_classes",
        classmethod(lambda _cls, _channel=None: [AgentTaskTool]),
    )
    monkeypatch.setattr(tool_factory_module, "_get_plugin_agent_tools", lambda: [])

    tools = MoviePilotToolFactory.create_tools(
        session_id="session",
        user_id="user",
        data=context,
    )

    assert len(tools) == 1
    assert tools[0].data is context


def test_tool_manager_reassembly_invalidates_factory_catalog() -> None:
    """重复装配数据上下文时必须丢弃持有旧仓储的工厂工具快照。"""
    first = _context(TransactionalAgentTaskRepository(SessionFactory))
    second = _context(TransactionalAgentTaskRepository(SessionFactory))
    manager = MoviePilotToolsManager(data=first)
    manager.tools = [AgentTaskTool(session_id="session", user_id="user", data=first)]
    manager._catalog_materialized = True
    manager._catalog_managed_by_factory = True

    manager.set_data_context(second)

    assert manager._data is second
    assert manager.tools == []
    assert manager.catalog is None
    assert manager._catalog_materialized is False


def test_memory_manager_reads_only_injected_chat_service() -> None:
    """记忆恢复必须调用构造时注入的会话服务，不再查询数据 locator。"""
    chat = MagicMock()
    chat.get_sync.return_value = SimpleNamespace(agent_messages=[])
    manager = MemoryManager(chat=chat, persistence=MagicMock())

    assert manager.get_agent_messages("session", "user") == []
    chat.get_sync.assert_called_once_with(session_id="session", user_id="user")


def test_scheduler_reads_injected_agent_task_repository() -> None:
    """Scheduler 注册任务时必须读取显式注入的 AgentTask 仓储。"""
    repository = MagicMock()
    repository.list.return_value = [SimpleNamespace(id=9)]
    scheduler = object.__new__(Scheduler)
    Scheduler.__init__(scheduler)
    scheduler.update_agent_task_job = MagicMock()
    scheduler.configure_agent_tasks(repository)

    scheduler.init_agent_task_jobs()

    repository.list.assert_called_once_with(enabled=True)
    scheduler.update_agent_task_job.assert_called_once_with(9)


def test_agent_initializer_reassembly_replaces_cached_manager(monkeypatch) -> None:
    """组合根重装配后不得复用持有旧数据上下文的 manager。"""
    first = _context(TransactionalAgentTaskRepository(SessionFactory))
    second = _context(TransactionalAgentTaskRepository(SessionFactory))
    monkeypatch.setattr(agent_initializer, "_agent_data_context", None)
    monkeypatch.setattr(agent_initializer, "_injected_agent_manager", None)
    monkeypatch.setattr(
        "app.agent.orchestrator.memory_manager",
        MemoryManager(),
    )

    agent_initializer.configure_agent_data_context(first)
    first_manager = agent_initializer._get_injected_agent_manager()
    agent_initializer.configure_agent_data_context(second)
    second_manager = agent_initializer._get_injected_agent_manager()

    assert first_manager is not second_manager
    assert first_manager._data is first
    assert second_manager._data is second


def test_transactional_agent_task_repository_projects_frozen_snapshots() -> None:
    """任务写入提交后必须返回脱离 Session 的冻结快照。"""
    repository = TransactionalAgentTaskRepository(SessionFactory)
    task = repository.add(
        name="架构检查",
        content="检查 Agent 数据边界",
        trigger_type="cron",
        cron_expression="0 8 * * *",
        run_at=None,
        user_id="user-1",
        username="tester",
        session_id="session-1",
        channel=None,
        source="test",
        original_chat_id=None,
    )

    assert isinstance(task, AgentTaskSnapshot)
    assert repository.get(task.id, user_id="user-1") == task
    assert repository.list(user_id="user-1") == [task]
    with pytest.raises(FrozenInstanceError):
        task.name = "不可修改"  # type: ignore[misc]

    assert repository.update(
        task.id,
        {"name": "已更新"},
        user_id="user-1",
    )
    updated = repository.get(task.id, user_id="user-1")
    assert updated is not None
    assert updated.name == "已更新"
    assert repository.delete(task.id, user_id="user-1")
    assert repository.get(task.id, user_id="user-1") is None
