"""Agent 命名数据端口工厂测试。"""

from app.application import agentdata


def test_named_agent_data_getters_use_registered_factories(monkeypatch) -> None:
    """每个命名 getter 都应创建组合根登记的对应端口实例。"""
    names = {
        "agent_chat": agentdata.get_agent_chat_port,
        "agent_task": agentdata.get_agent_task_port,
        "user": agentdata.get_agent_user_port,
        "site": agentdata.get_agent_site_port,
        "subscribe": agentdata.get_agent_subscribe_port,
        "subscribe_history": agentdata.get_agent_subscribe_history_port,
        "transfer_history": agentdata.get_agent_transfer_history_port,
        "download_history": agentdata.get_agent_download_history_port,
        "workflow": agentdata.get_agent_workflow_port,
        "plugin_data": agentdata.get_agent_plugin_data_port,
    }
    factories = {
        name: (lambda current=name: current)
        for name in names
    }
    monkeypatch.setattr(agentdata, "_ports", agentdata.AgentDataPorts(**factories))

    assert {name: getter() for name, getter in names.items()} == {
        name: name for name in names
    }
