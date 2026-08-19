from unittest.mock import patch

from app.agent.tools.factory import MoviePilotToolFactory


def test_factory_registers_plural_download_task_tool_names():
    """
    下载任务工具应统一使用 *_download_tasks 命名。
    """
    with patch(
        "app.agent.tools.factory._get_plugin_agent_tools",
        return_value=[],
    ):
        tools = MoviePilotToolFactory.create_tools(
            session_id="download-task-names",
            user_id="10001",
        )

    tool_names = {tool.name for tool in tools}
    assert {
        "add_download_tasks",
        "query_download_tasks",
        "update_download_tasks",
        "delete_download_tasks",
    } <= tool_names
    assert "add_download" not in tool_names
    assert "delete_download" not in tool_names
