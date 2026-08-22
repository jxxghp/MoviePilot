"""类型化 HostRuntime 与 FastAPI AppState 注入测试。"""

import ast
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.api.context import (
    get_agent_chat_repository,
    get_agent_chat_transaction,
)
from app.startup import lifecycle
from app.startup.ports.context import (
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
from app.application.configuration import (
    ApiRuntimeConfig,
    ChainRuntimeConfig,
    RuntimeConfiguration,
    RuntimeSettingsService,
    SchedulerRuntimeConfig,
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

    async def complete_by_event_key(self, event_key, completed_at) -> None:
        """模拟收口 durable intent。"""


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
        agent_chat=AgentChatRuntime(
            async_session=async_session,
            repository=_Repository,
            transaction=_UnitOfWork,
        ),
        persistence=PersistenceRuntime(
            sync_session=sync_session,
            async_session=async_session,
            sync_transaction=_SyncUnitOfWork,
            async_transaction=_UnitOfWork,
        ),
        authentication=AuthenticationRuntime(
            user_repository=_Repository,
            standalone_user=lambda: _Repository(object()),
            system_config=lambda: _Repository(object()),
            passkey=lambda: _Repository(object()),
        ),
        messaging=MessagingRuntime(repository=_Repository),
        history=HistoryRuntime(
            download_repository=_Repository,
            transfer_repository=_Repository,
            media_server_repository=_Repository,
        ),
        site=SiteRuntime(repository=_Repository),
        subscription=SubscriptionRuntime(
            async_session=async_session,
            repository=_Repository,
            history_repository=_Repository,
            transaction=_UnitOfWork,
            outbox=_Outbox,
        ),
        workflow=WorkflowRuntime(
            repository=_Repository,
            system_config=lambda: _Repository(object()),
        ),
        configuration=RuntimeConfiguration(
            api=lambda: ApiRuntimeConfig(False, 60, False, True),
            scheduler=lambda: SchedulerRuntimeConfig(
                False, "Asia/Shanghai", 1, False, "", None, None,
                False, 24, "rss", 30, False, None, None, True, 1, False, None,
            ),
            chain=lambda: ChainRuntimeConfig(media_extensions=(".mkv",)),
        ),
        settings=RuntimeSettingsService(_RuntimeSettings()),
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
    ) -> dict[str, bool]:
        """返回两个类型化能力是否绑定同一请求会话。"""
        return {"same_session": repository.session is unit_of_work.session}

    with TestClient(app) as client:
        response = client.get("/probe")

    assert response.status_code == 200
    assert response.json() == {"same_session": True}


def test_official_api_dependencies_do_not_use_string_data_locator() -> None:
    """正式业务依赖只能读取 HostRuntime 命名领域，禁止回退字符串注册表。"""
    dependency_root = PROJECT_ROOT / "app" / "api" / "dependencies"
    official_modules = {
        "agent.py",
        "auth.py",
        "history.py",
        "site.py",
        "subscription.py",
        "workflow.py",
    }
    for filename in official_modules:
        tree = ast.parse(
            (dependency_root / filename).read_text(encoding="utf-8")
        )
        imported_modules = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        }
        assert "app.api.data" not in imported_modules
        assert "app.api.dependencies.data" not in imported_modules


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
