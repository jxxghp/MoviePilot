"""类型化 HostRuntime 与 FastAPI AppState 注入测试。"""

import ast
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.api.context import (
    get_agent_chat_repository,
    get_agent_chat_transaction,
)
from app.api.dependencies.agent import get_agent_chat_persistence
from app.application.agent import AgentDataContext
from app.application.configuration import (
    ApiRuntimeConfig,
    ChainRuntimeConfig,
    RuntimeConfiguration,
    RuntimeSettingsService,
    SchedulerRuntimeConfig,
)
from app.runtime.tasks import TaskRegistry
from app.startup import lifecycle
from app.startup.composition.context import (
    AgentChatRuntime,
    AuthenticationRuntime,
    HistoryRuntime,
    HostRuntime,
    MessagingRuntime,
    PersistenceRuntime,
    SiteRuntime,
    SubscriptionRuntime,
    WorkflowRuntime,
)

PROJECT_ROOT = Path(__file__).parents[1]


class _Repository:
    """记录绑定会话的 Agent 会话仓储替身。"""

    def __init__(self, session: object) -> None:
        """保存由类型化运行时提供的请求会话。"""
        self.session = session


class _UnitOfWork:
    """记录绑定会话的异步事务替身。"""

    def __init__(self, session: object) -> None:
        """保存与仓储相同的请求会话。"""
        self.session = session

    async def commit(self) -> None:
        """模拟提交。"""

    async def rollback(self) -> None:
        """模拟回滚。"""


class _AgentChatPersistence:
    """提供 AgentChat 运行时所需的最小写端口。"""


class _SyncUnitOfWork:
    """记录绑定会话的同步事务替身。"""

    def __init__(self, session: object) -> None:
        """保存与仓储相同的请求会话。"""
        self.session = session

    def commit(self) -> None:
        """模拟提交。"""

    def rollback(self) -> None:
        """模拟回滚。"""


class _Outbox:
    """记录绑定会话的异步 outbox 替身。"""

    def __init__(self, session: object) -> None:
        """保存与订阅仓储相同的请求会话。"""
        self.session = session

    async def stage(self, intent, now) -> None:
        """模拟暂存 durable intent。"""


class _DispatchStore:
    """提供订阅即时副作用所需的独立派发存储替身。"""

    async def claim_by_event_key(self, event_key, now, lease_until):
        """模拟未取得指定消息 lease。"""
        return None

    async def complete(self, message_id, attempt, completed_at) -> bool:
        """模拟按 attempt 完成消息。"""
        return True

    async def retry(self, message_id, attempt, **kwargs) -> bool:
        """模拟按 attempt 释放消息。"""
        return True


class _RuntimeSettings:
    """提供 HostRuntime 设置服务所需的最小测试合同。"""

    def model_dump(self, *, include=None, exclude=None):
        """返回空设置快照。"""
        return {}

    def update_settings(self, env):
        """返回批量更新成功结果。"""
        return {key: (True, "") for key in env}

    def update_setting(self, key, value):
        """返回单项更新成功结果。"""
        return True, ""


def _runtime() -> HostRuntime:
    """构造不加载数据库引擎或 PluginManager 的假宿主运行时。"""

    async def async_session():
        """生成一个可被 FastAPI 依赖缓存的会话标记。"""
        yield object()

    def sync_session():
        """提供兼容 ApiDataPorts 所需的空同步生成器。"""
        if False:
            yield object()

    return HostRuntime(
        agent=AgentDataContext(
            chat=SimpleNamespace(),
            chat_persistence=_AgentChatPersistence(),
            tasks=SimpleNamespace(),
            users=SimpleNamespace(),
            sites=SimpleNamespace(),
            subscriptions=SimpleNamespace(),
            subscription_mutation_scope=SimpleNamespace(),
            subscription_delete_scope=SimpleNamespace(),
            async_rule_group_mutation_scope=SimpleNamespace(),
            subscription_history=SimpleNamespace(),
            transfer_history=SimpleNamespace(),
            transfer_execution=SimpleNamespace(),
            download_history=SimpleNamespace(),
            plugin_data=SimpleNamespace(),
        ),
        agent_chat=AgentChatRuntime(
            async_session=async_session,
            repository=_Repository,
            transaction=_UnitOfWork,
            persistence=_AgentChatPersistence(),
        ),
        persistence=PersistenceRuntime(
            sync_session=sync_session,
            async_session=async_session,
            sync_transaction=_SyncUnitOfWork,
            async_transaction=_UnitOfWork,
        ),
        authentication=AuthenticationRuntime(
            user_repository=_Repository,
            passkey_repository=_Repository,
            standalone_user=lambda: _Repository(object()),
            system_config=lambda: _Repository(object()),
            passkey=lambda: _Repository(object()),
        ),
        messaging=MessagingRuntime(
            repository=_Repository,
            helper=SimpleNamespace(),
            queue=SimpleNamespace(),
        ),
        history=HistoryRuntime(
            download_repository=_Repository,
            transfer_repository=_Repository,
            transfer_mutation_repository=_Repository,
            media_server_repository=_Repository,
            transfer_execution_repository=_Repository(object()),
        ),
        site=SiteRuntime(
            repository=_Repository,
            standalone=_Repository(object()),
        ),
        subscription=SubscriptionRuntime(
            async_session=async_session,
            repository=_Repository,
            history_repository=_Repository,
            transaction=_UnitOfWork,
            outbox=_Outbox,
            dispatch_store=_DispatchStore(),
            batch_writer=SimpleNamespace(),
            rule_group_mutation_scope=SimpleNamespace(),
            async_rule_group_mutation_scope=SimpleNamespace(),
            site_reference_mutation_scope=SimpleNamespace(),
        ),
        workflow=WorkflowRuntime(
            query=SimpleNamespace(),
            repository=_Repository,
            system_config=lambda: _Repository(object()),
        ),
        classification=SimpleNamespace(),
        classification_execution=SimpleNamespace(),
        system=SimpleNamespace(),
        configuration=RuntimeConfiguration(
            api=lambda: ApiRuntimeConfig(60, False, True),
            scheduler=lambda: SchedulerRuntimeConfig(
                False,
                "Asia/Shanghai",
                1,
                False,
                "",
                None,
                None,
                False,
                24,
                "rss",
                30,
                False,
                None,
                None,
                True,
                1,
                False,
                None,
            ),
            chain=lambda: ChainRuntimeConfig(media_extensions=(".mkv",)),
        ),
        settings=RuntimeSettingsService(_RuntimeSettings()),
        tasks=TaskRegistry(),
    )


def test_host_runtime_is_frozen_slotted_and_covers_all_api_domains() -> None:
    """运行时不可动态扩字段，且全部正式 API 领域都有命名能力。"""
    runtime = _runtime()

    assert not hasattr(runtime, "__dict__")
    assert runtime.authentication.user_repository is _Repository
    assert runtime.messaging.repository is _Repository
    assert runtime.history.download_repository is _Repository
    assert runtime.site.repository is _Repository
    assert runtime.subscription.repository is _Repository
    assert runtime.workflow.repository is _Repository
    with pytest.raises(FrozenInstanceError):
        runtime.agent_chat = runtime.agent_chat


def test_fastapi_dependencies_use_fake_runtime_without_real_services() -> None:
    """请求依赖可只注入假 Runtime，且仓储与 UoW 共享同一请求会话。"""
    app = FastAPI()
    app.state.host_runtime = _runtime()

    @app.get("/probe")
    async def probe(
        repository=Depends(get_agent_chat_repository),
        unit_of_work=Depends(get_agent_chat_transaction),
        persistence=Depends(get_agent_chat_persistence),
    ) -> dict[str, bool]:
        """返回两个类型化能力是否绑定同一请求会话。"""
        return {
            "same_session": repository.session is unit_of_work.session,
            "has_persistence": persistence is app.state.host_runtime.agent_chat.persistence,
        }

    with TestClient(app) as client:
        response = client.get("/probe")

    assert response.status_code == 200
    assert response.json() == {"same_session": True, "has_persistence": True}


def test_string_api_data_locator_is_confined_to_compatibility_boundary() -> None:
    """字符串数据注册表只能由 startup 注入并经旧 Facade 转发。"""
    importers = set()
    for path in (PROJECT_ROOT / "app").rglob("*.py"):
        if path.is_relative_to(PROJECT_ROOT / "app" / "plugins"):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imported_modules = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module}
        if imported_modules & {"app.api.data", "app.api.dependencies.data"}:
            importers.add(path.relative_to(PROJECT_ROOT).as_posix())

    assert importers == {
        "app/api/dependencies/data.py",
        "app/startup/composition/runtime.py",
    }


@pytest.mark.asyncio
async def test_lifecycle_component_attaches_init_modules_result(monkeypatch) -> None:
    """模块组件把 init_modules 的构建结果发布到当前 AppState。"""
    runtime = _runtime()
    app = FastAPI()

    async def init_modules() -> HostRuntime:
        """返回不触发真实启动副作用的假运行时。"""
        return runtime

    monkeypatch.setattr(lifecycle, "init_modules", init_modules)

    await lifecycle.initialize_modules_component(app)

    assert app.state.host_runtime is runtime


@pytest.mark.asyncio
async def test_lifecycle_component_revokes_host_runtime_after_converged_stop(
    monkeypatch,
) -> None:
    """模块 owner 完整关闭后 AppState 不得继续暴露上一 lifespan 的运行时。"""
    app = FastAPI()
    runtime = _runtime()
    app.state.host_runtime = runtime
    monkeypatch.setattr(lifecycle, "stop_modules", AsyncMock(return_value=True))

    assert await lifecycle.stop_modules_component(app) is True
    assert app.state.host_runtime is None


@pytest.mark.asyncio
async def test_lifecycle_component_retains_host_runtime_after_failed_stop(
    monkeypatch,
) -> None:
    """模块 owner 未收敛时保留 HostRuntime，供诊断与后续重试。"""
    app = FastAPI()
    runtime = _runtime()
    app.state.host_runtime = runtime
    monkeypatch.setattr(lifecycle, "stop_modules", AsyncMock(return_value=False))

    assert await lifecycle.stop_modules_component(app) is False
    assert app.state.host_runtime is runtime


@pytest.mark.asyncio
async def test_init_modules_cleans_partial_message_owner_on_failure(monkeypatch) -> None:
    """直接调用模块初始化时，中途失败也不得遗留消息缓存单例。"""
    from app.application.messaging.message import MessageHelper
    from app.foundation.singleton import Singleton, SingletonClass
    from app.startup.initializers import modules as modules_initializer

    monkeypatch.setattr(Singleton, "_instances", {})
    monkeypatch.setattr(SingletonClass, "_instances", {})
    close = MagicMock()
    startup_error = RuntimeError("module startup failed")

    async def failing_initialize_modules() -> HostRuntime:
        """构造消息 owner 后模拟组合逻辑失败。"""
        helper = MessageHelper()
        monkeypatch.setattr(helper._recent_notification_keys, "close", close)
        raise startup_error

    monkeypatch.setattr(
        modules_initializer,
        "_initialize_modules",
        failing_initialize_modules,
    )
    stop_modules = AsyncMock(return_value=True)
    monkeypatch.setattr(
        modules_initializer,
        "stop_modules",
        stop_modules,
    )

    with pytest.raises(RuntimeError) as raised:
        await modules_initializer.init_modules()

    assert raised.value is startup_error
    close.assert_called_once_with()
    assert MessageHelper.get_existing_instance() is None
    stop_modules.assert_awaited_once_with()
