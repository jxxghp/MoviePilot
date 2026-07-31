import asyncio
import json
from types import SimpleNamespace
from typing import Iterator
from unittest.mock import patch

import pytest

from app.agent.tools.base import MoviePilotTool
from app.agent.tools.factory import MoviePilotToolFactory
from app.agent.tools.manager import MoviePilotToolsManager
from app.api.endpoints import mcp
from app.core.plugin import PluginManager
from app.utils.singleton import Singleton


class DemoPluginTool(MoviePilotTool):
    """测试用插件 MCP 工具。"""

    name: str = "demo_plugin_tool"
    description: str = "测试插件动态注册的 MCP 工具"

    async def run(self, **kwargs) -> str:
        """返回固定测试结果。"""
        return "plugin-ok"


@pytest.fixture
def plugin_manager() -> Iterator[PluginManager]:
    """构造隔离的插件管理器并在测试后恢复原单例。"""
    singleton_key = (PluginManager, (), frozenset())
    previous_instance = Singleton._instances.pop(singleton_key, None)
    manager = PluginManager()
    yield manager
    Singleton._instances.pop(singleton_key, None)
    if previous_instance is not None:
        Singleton._instances[singleton_key] = previous_instance


def _build_plugin() -> SimpleNamespace:
    """构造声明一个 Agent 工具的已启用插件。"""
    return SimpleNamespace(
        plugin_name="Demo Plugin",
        get_state=lambda: True,
        get_agent_tools=lambda: [DemoPluginTool],
    )


def test_mcp_refreshes_tools_after_plugin_lifecycle_change(
    plugin_manager: PluginManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MCP 管理器应发现初始化后新增的插件工具，并在插件移除后停止暴露。"""
    with patch.object(
        MoviePilotToolFactory,
        "_get_builtin_tool_classes",
        return_value=[],
    ):
        tool_manager = MoviePilotToolsManager(
            session_id="mcp-plugin-test",
            user_id="api_user",
        )
        monkeypatch.setattr(mcp, "moviepilot_tool_manager", tool_manager)

        assert asyncio.run(mcp.handle_tools_list()) == {"tools": []}

        plugin_manager.running_plugins["DemoPlugin"] = _build_plugin()
        plugin_manager.clear_plugin_agent_tools_cache()

        listed_tools = asyncio.run(mcp.handle_tools_list())["tools"]
        assert [tool["name"] for tool in listed_tools] == ["demo_plugin_tool"]

        call_result = asyncio.run(
            mcp.handle_tools_call(
                {
                    "name": "demo_plugin_tool",
                    "arguments": {},
                }
            )
        )
        assert call_result == {
            "content": [{"type": "text", "text": "plugin-ok"}]
        }

        plugin_manager.running_plugins.pop("DemoPlugin")
        plugin_manager.clear_plugin_agent_tools_cache()

        assert asyncio.run(mcp.handle_tools_list()) == {"tools": []}
        missing_result = asyncio.run(
            mcp.handle_tools_call(
                {
                    "name": "demo_plugin_tool",
                    "arguments": {},
                }
            )
        )
        missing_payload = json.loads(missing_result["content"][0]["text"])
        assert "未找到" in missing_payload["error"]
