"""Agent 启动组合根的对象身份与装配时序测试。"""

from __future__ import annotations

import json
import subprocess
import sys
from types import SimpleNamespace

from app.startup.composition import agent as agent_composition
from app.startup.composition.runtime import RuntimeDependencies


class _StartedWorker:
    """仅允许在已启动状态读取容量的数据库 worker 桩。"""

    def __init__(self, events: list[str]) -> None:
        """保存顺序记录并模拟已完成启动。"""
        self.started = True
        self.events = events

    def snapshot(self) -> SimpleNamespace:
        """确认启动已完成后返回测试容量。"""
        assert self.started is True
        self.events.append("read-capacity")
        return SimpleNamespace(capacity=17)


def _compose() -> tuple[agent_composition.AgentComposition, _StartedWorker]:
    """构造一组无需真实数据库连接的 Agent composition。"""
    events = ["worker-started"]
    worker = _StartedWorker(events)
    runtime = SimpleNamespace(
        worker=worker,
        transaction=SimpleNamespace(sync=lambda operation: operation(object())),
    )
    system_config = SimpleNamespace(publish_many=lambda _values: None)
    dependencies = RuntimeDependencies(
        download_history=object(),
        transfer_history=object(),
        site=object(),
        subscription=object(),
        subscription_history=object(),
        transfer_execution=object(),
        message_helper=object(),
        message_queue=object(),
    )
    composition = agent_composition.compose_agent(
        runtime=runtime,
        system_config=system_config,
        dependencies=dependencies,
    )
    return composition, worker


def test_agent_composition_reuses_shared_objects_and_started_worker() -> None:
    """HostRuntime、Agent 数据与任务入口必须复用同一批对象。"""
    composition, worker = _compose()

    assert composition.data.tasks is composition.tasks
    assert composition.persistence is composition.data.chat_persistence
    assert composition.data.chat_persistence._async_executor is worker
    assert composition.data.chat_persistence._capacity == 17
    assert composition.execution._async_executor is worker
    assert worker.events == ["worker-started", "read-capacity"]


def test_publish_agent_services_registers_composed_identity(monkeypatch) -> None:
    """兼容 provider 与 Agent initializer 必须收到 composition 中的原对象。"""
    composition, _worker = _compose()
    published: dict[str, object] = {}

    monkeypatch.setattr(
        agent_composition,
        "configure_agent_chat_service",
        lambda service: published.update(chat=service),
    )
    monkeypatch.setattr(
        agent_composition,
        "configure_agent_chat_persistence",
        lambda service: published.update(persistence=service),
    )
    monkeypatch.setattr(
        agent_composition,
        "configure_agent_task_execution",
        lambda service: published.update(execution=service),
    )

    agent_composition.publish_agent_services(
        composition,
        data_context_registrar=lambda data: published.update(data=data),
    )

    assert published == {
        "chat": composition.data.chat,
        "persistence": composition.data.chat_persistence,
        "execution": composition.execution,
        "data": composition.data,
    }


def test_agent_composition_import_is_cold() -> None:
    """导入 Agent composition 不得登记服务或加载 Agent 实现层。"""
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            r"""
import json
import sys

from app.application.agenttask import get_agent_task_execution_service
from app.application.messaging.chat import (
    get_configured_agent_chat_persistence,
    get_configured_agent_chat_service,
)

import app.startup.composition.agent

unconfigured = []
for getter in (
    get_agent_task_execution_service,
    get_configured_agent_chat_persistence,
    get_configured_agent_chat_service,
):
    try:
        getter()
    except RuntimeError:
        unconfigured.append(True)

loaded_agent_implementation = sorted(
    name for name in sys.modules if name == "app.agent" or name.startswith("app.agent.")
)
print(json.dumps({
    "initializer_loaded": "app.startup.initializers.agent" in sys.modules,
    "loaded_agent_implementation": loaded_agent_implementation,
    "unconfigured": len(unconfigured),
}))
""",
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(completed.stdout) == {
        "initializer_loaded": False,
        "loaded_agent_implementation": [],
        "unconfigured": 3,
    }
