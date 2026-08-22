"""类型化 HostRuntime 与 FastAPI AppState 注入测试。"""

from dataclasses import FrozenInstanceError
from types import SimpleNamespace

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.api.context import (
    get_agent_chat_repository,
    get_agent_chat_transaction,
)
from app.api.data import (
    ApiDataPorts,
    configure_api_data_runtime,
    get_api_data_ports,
)
from app.startup import lifecycle
from app.startup.context import AgentChatRuntime, HostRuntime, SubscriptionRuntime
from app.application.configuration import (
    ApiRuntimeConfig,
    ChainRuntimeConfig,
    RuntimeConfiguration,
    SchedulerRuntimeConfig,
)


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


class _Outbox:
    """记录绑定会话的异步 outbox 替身。"""

    def __init__(self, session: object) -> None:
        """保存与订阅仓储相同的请求会话。"""
        self.session = session

    async def stage(self, intent, now) -> None:
        """模拟暂存 durable intent。"""

    async def complete_by_event_key(self, event_key, completed_at) -> None:
        """模拟收口 durable intent。"""


def _runtime() -> HostRuntime:
    """构造不加载数据库引擎或 PluginManager 的假宿主运行时。"""
    async def async_session():
        """生成一个可被 FastAPI 依赖缓存的会话标记。"""
        yield object()

    def sync_session():
        """提供兼容 ApiDataPorts 所需的空同步生成器。"""
        if False:
            yield object()

    compatibility = ApiDataPorts(
        sync_session=sync_session,
        async_session=async_session,
        repositories={},
        standalone={},
        unit_of_work={},
    )
    return HostRuntime(
        agent_chat=AgentChatRuntime(
            async_session=async_session,
            repository=_Repository,
            transaction=_UnitOfWork,
        ),
        subscription=SubscriptionRuntime(
            async_session=async_session,
            repository=_Repository,
            history_repository=_Repository,
            transaction=_UnitOfWork,
            outbox=_Outbox,
        ),
        configuration=RuntimeConfiguration(
            api=lambda: ApiRuntimeConfig(False, 60, False, True),
            scheduler=lambda: SchedulerRuntimeConfig(
                False, "Asia/Shanghai", 1, False, "", None, None,
                False, 24, "rss", 30, False, None, None, True, 1, False, None,
            ),
            chain=lambda: ChainRuntimeConfig(media_extensions=(".mkv",)),
        ),
        compatibility_api_data=compatibility,
    )


def test_host_runtime_is_frozen_slotted_and_reuses_compatibility_facade() -> None:
    """运行时不可动态扩字段，旧 Facade 必须指向同一个端口实例。"""
    runtime = _runtime()

    configure_api_data_runtime(runtime.compatibility_api_data)

    assert not hasattr(runtime, "__dict__")
    assert get_api_data_ports() is runtime.compatibility_api_data
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
