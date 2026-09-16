"""宿主运行时组合根的对象身份、发布与冷导入测试。"""

from __future__ import annotations

import json
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock

from app.runtime.tasks import TaskRegistry
from app.startup.composition import runtime as runtime_composition


def _inputs() -> runtime_composition.RuntimeInputs:
    """构造不连接数据库、不启动线程的最小运行时输入。"""
    dependencies = runtime_composition.RuntimeDependencies(
        download_history=Mock(name="download_history"),
        transfer_history=Mock(name="transfer_history"),
        site=Mock(name="site"),
        subscription=Mock(name="subscription"),
        subscription_history=Mock(name="subscription_history"),
        transfer_execution=Mock(name="transfer_execution"),
        message_helper=Mock(name="message_helper"),
        message_queue=Mock(name="message_queue"),
    )
    system_config = Mock(name="system_config")
    configuration = SimpleNamespace(
        system_config=system_config,
        system_service=Mock(name="system_service"),
        runtime=Mock(name="runtime_configuration"),
        settings=Mock(name="runtime_settings"),
    )
    database = SimpleNamespace(workflow_query=Mock(name="workflow_query"))
    agent_data = SimpleNamespace(
        sites=dependencies.site,
        subscriptions=dependencies.subscription,
        subscription_history=dependencies.subscription_history,
        transfer_history=dependencies.transfer_history,
        transfer_execution=dependencies.transfer_execution,
        download_history=dependencies.download_history,
    )
    agent = SimpleNamespace(
        data=agent_data,
        chat_repository=Mock(name="chat_repository"),
        persistence=Mock(name="chat_persistence"),
    )
    authentication = SimpleNamespace(
        user_repository=Mock(name="user_repository"),
        passkey_repository=Mock(name="passkey_repository"),
        standalone_user=Mock(name="standalone_user"),
        system_config=Mock(name="standalone_system_config"),
        passkey=Mock(name="standalone_passkey"),
    )
    return runtime_composition.RuntimeInputs(
        configuration=configuration,
        database=database,
        agent=agent,
        authentication=authentication,
        classification=Mock(name="classification_runtime"),
        classification_execution=Mock(name="classification_execution"),
        dependencies=dependencies,
        tasks=TaskRegistry(),
    )


def test_runtime_composition_reuses_dependencies_and_projects_api_identity() -> None:
    """HostRuntime、Agent、Chain 依赖与旧 API 投影必须保持同一对象身份。"""
    inputs = _inputs()

    composition = runtime_composition.compose_runtime(inputs)
    runtime = composition.runtime
    ports = composition.api_data

    assert composition.dependencies is inputs.dependencies
    assert runtime.agent is inputs.agent.data
    assert runtime.agent_chat.repository is inputs.agent.chat_repository
    assert runtime.agent_chat.persistence is inputs.agent.persistence
    assert runtime.authentication.user_repository is inputs.authentication.user_repository
    assert runtime.authentication.passkey_repository is inputs.authentication.passkey_repository
    assert runtime.classification is inputs.classification
    assert runtime.classification_execution is inputs.classification_execution
    assert runtime.configuration is inputs.configuration.runtime
    assert runtime.settings is inputs.configuration.settings
    assert runtime.system._system_config is inputs.configuration.system_service
    assert runtime.tasks is inputs.tasks
    assert runtime.messaging.helper is inputs.dependencies.message_helper
    assert runtime.messaging.queue is inputs.dependencies.message_queue
    assert runtime.history.transfer_repository is inputs.dependencies.transfer_history
    assert runtime.history.transfer_execution_repository is inputs.dependencies.transfer_execution
    assert runtime.site.standalone is inputs.dependencies.site
    assert inputs.agent.data.transfer_execution is runtime.history.transfer_execution_repository
    assert inputs.agent.data.sites is runtime.site.standalone
    assert ports.sync_session is runtime.persistence.sync_session
    assert ports.async_session is runtime.persistence.async_session
    assert ports.repositories == {
        "download_history": runtime.history.download_repository,
        "media_server": runtime.history.media_server_repository,
        "message": runtime.messaging.repository,
        "passkey": runtime.authentication.passkey_repository,
        "site": runtime.site.repository,
        "subscribe": runtime.subscription.repository,
        "subscribe_history": runtime.subscription.history_repository,
        "user": runtime.authentication.user_repository,
        "workflow": runtime.workflow.repository,
    }
    assert ports.standalone == {
        "passkey": runtime.authentication.passkey,
        "system_config": runtime.authentication.system_config,
        "user": runtime.authentication.standalone_user,
    }
    assert ports.unit_of_work == {
        "async": runtime.persistence.async_transaction,
        "sync": runtime.persistence.sync_transaction,
    }
    assert composition.site_query._repository is inputs.dependencies.site
    assert composition.site_health._repository is inputs.dependencies.site


def test_runtime_composition_publishes_and_resets_exact_projection(monkeypatch) -> None:
    """运行时 owner 应发布并按对称顺序撤销其拥有的全部投影。"""
    composition = runtime_composition.compose_runtime(_inputs())
    published: dict[str, object] = {}
    reset_order: list[str] = []

    monkeypatch.setattr(
        runtime_composition,
        "configure_api_data_runtime",
        lambda value: published.update(api=value),
    )
    monkeypatch.setattr(
        runtime_composition,
        "configure_transfer_history_repository",
        lambda value: published.update(transfer=value),
    )
    monkeypatch.setattr(
        runtime_composition,
        "configure_site_query_service",
        lambda value: published.update(site_query=value),
    )
    monkeypatch.setattr(
        runtime_composition,
        "configure_site_health_service",
        lambda value: published.update(site_health=value),
    )
    monkeypatch.setattr(
        runtime_composition,
        "reset_site_health_service",
        lambda: reset_order.append("site_health"),
    )
    monkeypatch.setattr(
        runtime_composition,
        "reset_site_query_service",
        lambda: reset_order.append("site_query"),
    )
    monkeypatch.setattr(
        runtime_composition,
        "reset_transfer_history_repository",
        lambda: reset_order.append("transfer_history"),
    )
    monkeypatch.setattr(
        runtime_composition,
        "reset_api_data_runtime",
        lambda: reset_order.append("api_data"),
    )

    runtime_composition.publish_runtime(composition)
    runtime_composition.reset_runtime()

    assert published["api"] is composition.api_data
    assert published["transfer"]() is composition.dependencies.transfer_history
    assert published["site_query"] is composition.site_query
    assert published["site_health"] is composition.site_health
    assert reset_order == [
        "site_health",
        "site_query",
        "transfer_history",
        "api_data",
    ]


def test_runtime_composition_import_is_cold() -> None:
    """导入 runtime owner 不得加载 initializer、启动 worker/队列或发布端口。"""
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            r"""
import json
import sys

from app.api.data import get_api_data_ports
from app.application.history import get_transfer_history_repository
from app.application.messaging.message import MessageHelper, MessageQueueManager
from app.application.site.health import get_configured_site_health_service
from app.application.site.query import get_configured_site_query_service

import app.startup.composition.runtime

unconfigured = []
for getter in (
    get_api_data_ports,
    get_transfer_history_repository,
    get_configured_site_health_service,
    get_configured_site_query_service,
):
    try:
        getter()
    except RuntimeError:
        unconfigured.append(True)

print(json.dumps({
    "database_worker_loaded": "app.db.worker" in sys.modules,
    "initializer_loaded": any(
        name == "app.startup.initializers" or name.startswith("app.startup.initializers.")
        for name in sys.modules
    ),
    "message_helper_created": MessageHelper.get_existing_instance() is not None,
    "message_queue_created": MessageQueueManager.get_existing_instance() is not None,
    "unconfigured": len(unconfigured),
}))
""",
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(completed.stdout) == {
        "database_worker_loaded": False,
        "initializer_loaded": False,
        "message_helper_created": False,
        "message_queue_created": False,
        "unconfigured": 4,
    }
