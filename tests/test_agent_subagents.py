import asyncio
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from langchain_core.language_models.fake_chat_models import FakeListChatModel

import app.agent.middleware.subagents as subagent_module
from app.agent.middleware.subagents import (
    MoviePilotSubAgentMiddleware,
    SUBAGENT_CONTROL_TOOL_NAME,
    SUBAGENT_TASK_TOOL_NAME,
    SubAgentCallSummaryMiddleware,
    SubAgentTaskControlMiddleware,
    create_subagent_middlewares,
)
from app.agent.tools.tags import ToolTag


class TestAgentSubagents(unittest.TestCase):
    def test_create_subagent_middlewares_registers_task_tool(self):
        """子代理中间件应向主 Agent 注册 task 委派工具。"""
        model = FakeListChatModel(responses=["ok"])

        middlewares, task_tools = create_subagent_middlewares(
            model=model,
            tools=[],
            stream_handler=None,
        )

        self.assertEqual(len(middlewares), 3)
        self.assertEqual(
            [tool.name for tool in task_tools],
            [SUBAGENT_TASK_TOOL_NAME, SUBAGENT_CONTROL_TOOL_NAME],
        )
        self.assertIn("media-researcher", task_tools[0].description)
        self.assertIn("moviepilot-explorer", task_tools[0].description)
        self.assertIn("system-diagnostician", task_tools[0].description)
        self.assertIn("action=start", task_tools[1].description)
        self.assertIn("action=wait", task_tools[1].description)

    def test_subagent_tools_are_selected_by_tags(self):
        """子代理应根据工具标签筛选工具，而不是依赖工具名名单。"""
        model = FakeListChatModel(responses=["ok"])
        tools = [
            SimpleNamespace(
                name="custom_media_lookup",
                tags=[ToolTag.Read.value, ToolTag.Media.value],
            ),
            SimpleNamespace(
                name="custom_media_writer",
                tags=[ToolTag.Read.value, ToolTag.Write.value, ToolTag.Media.value],
            ),
            SimpleNamespace(
                name="custom_site_lookup",
                tags=[ToolTag.Read.value, ToolTag.Site.value],
            ),
        ]
        captured = {}

        def _fake_create_agent(**kwargs):
            captured.update(kwargs)
            return kwargs

        middleware = MoviePilotSubAgentMiddleware(
            model=model,
            profiles=subagent_module._builtin_subagent_profiles(),
            tools=tools,
        )

        with patch.object(subagent_module, "create_agent", side_effect=_fake_create_agent):
            middleware._get_agent("media-researcher")

        self.assertEqual(
            [tool.name for tool in captured["tools"]],
            ["custom_media_lookup"],
        )

    def test_moviepilot_explorer_selects_code_and_settings_tools(self):
        """MoviePilot 探索子代理应能读取代码、目录、设置和命令诊断工具。"""
        model = FakeListChatModel(responses=["ok"])
        tools = [
            SimpleNamespace(
                name="custom_code_reader",
                tags=[ToolTag.Read.value, ToolTag.File.value],
            ),
            SimpleNamespace(
                name="custom_directory_lister",
                tags=[ToolTag.Read.value, ToolTag.Directory.value],
            ),
            SimpleNamespace(
                name="custom_settings_reader",
                tags=[ToolTag.Read.value, ToolTag.Settings.value],
            ),
            SimpleNamespace(
                name="custom_command_runner",
                tags=[ToolTag.Read.value, ToolTag.Command.value],
            ),
            SimpleNamespace(
                name="custom_code_writer",
                tags=[ToolTag.Read.value, ToolTag.Write.value, ToolTag.File.value],
            ),
        ]
        captured = {}

        def _fake_create_agent(**kwargs):
            captured.update(kwargs)
            return kwargs

        middleware = MoviePilotSubAgentMiddleware(
            model=model,
            profiles=subagent_module._builtin_subagent_profiles(),
            tools=tools,
        )

        with patch.object(subagent_module, "create_agent", side_effect=_fake_create_agent):
            middleware._get_agent("moviepilot-explorer")

        self.assertEqual(
            [tool.name for tool in captured["tools"]],
            [
                "custom_code_reader",
                "custom_directory_lister",
                "custom_settings_reader",
                "custom_command_runner",
            ],
        )

    def test_builtin_tools_declare_tags_in_implementation(self):
        """所有内置工具实现都应显式声明 tags。"""
        impl_dir = Path(__file__).resolve().parents[1] / "app" / "agent" / "tools" / "impl"
        missing_tools = []
        for path in sorted(impl_dir.glob("*.py")):
            text = path.read_text()
            for block in text.split("\nclass "):
                if "(MoviePilotTool)" not in block:
                    continue
                class_name = block.split("(", 1)[0].strip()
                if "tags: list[str]" not in block:
                    missing_tools.append(f"{path.name}:{class_name}")

        self.assertEqual([], missing_tools)


class TestSubAgentTaskControlMiddleware(unittest.IsolatedAsyncioTestCase):
    async def test_call_summary_middleware_logs_subagent_tool_operations(self):
        """子代理工具包装层应记录工具执行开始和完成日志。"""
        middleware = SubAgentCallSummaryMiddleware()
        request = SimpleNamespace(
            tool=SimpleNamespace(name=SUBAGENT_CONTROL_TOOL_NAME),
            tool_call={
                "args": {
                    "action": "status",
                    "subagent_type": "general-purpose",
                }
            },
        )

        async def _fake_handler(_request):
            return "ok"

        with patch.object(subagent_module.logger, "info") as log_info:
            result = await middleware.awrap_tool_call(request, _fake_handler)

        messages = [call.args[0] for call in log_info.call_args_list]
        self.assertEqual("ok", result)
        self.assertTrue(any("开始执行子代理工具" in message for message in messages))
        self.assertTrue(any("子代理工具执行完成" in message for message in messages))

    async def test_control_tool_starts_tasks_concurrently_and_waits(self):
        """异步子代理管控工具应批量启动任务，并在 wait 时收集结果。"""
        model = FakeListChatModel(responses=["ok"])
        middleware = SubAgentTaskControlMiddleware(
            model=model,
            profiles=subagent_module._builtin_subagent_profiles(),
            tools=[],
        )
        running_descriptions = []
        both_started = asyncio.Event()
        allow_finish = asyncio.Event()

        async def _fake_run_task(self, *, description, subagent_type, task_id=None):
            running_descriptions.append(description)
            if len(running_descriptions) == 2:
                both_started.set()
            await allow_finish.wait()
            return f"{subagent_type}:{description}:{task_id}"

        with patch.object(
            subagent_module._SubAgentAgentProvider,
            "run_task",
            new=_fake_run_task,
        ):
            start_payload = json.loads(
                await middleware._control_task(
                    action="start",
                    tasks=[
                        {
                            "description": "检查媒体库",
                            "subagent_type": "media-researcher",
                        },
                        {
                            "description": "检查下载器",
                            "subagent_type": "download-diagnostician",
                        },
                    ],
                )
            )

            await asyncio.wait_for(both_started.wait(), timeout=1)
            allow_finish.set()
            task_ids = [task["task_id"] for task in start_payload["tasks"]]
            wait_payload = json.loads(
                await middleware._control_task(
                    action="wait",
                    task_ids=task_ids,
                    wait_mode="all",
                    timeout_ms=1000,
                )
            )

        self.assertTrue(start_payload["success"])
        self.assertEqual(2, len(task_ids))
        self.assertEqual(["检查媒体库", "检查下载器"], running_descriptions)
        self.assertEqual(
            ["completed", "completed"],
            [task["status"] for task in wait_payload["tasks"]],
        )
        self.assertIn("media-researcher:检查媒体库", wait_payload["tasks"][0]["result"])
        self.assertIn(
            "download-diagnostician:检查下载器",
            wait_payload["tasks"][1]["result"],
        )

    async def test_after_agent_cancels_unfinished_tasks(self):
        """Agent 结束时应取消仍在运行的异步子代理任务。"""
        model = FakeListChatModel(responses=["ok"])
        middleware = SubAgentTaskControlMiddleware(
            model=model,
            profiles=subagent_module._builtin_subagent_profiles(),
            tools=[],
        )
        task_started = asyncio.Event()

        async def _fake_run_task(self, *, description, subagent_type, task_id=None):
            task_started.set()
            await asyncio.Event().wait()

        with patch.object(
            subagent_module._SubAgentAgentProvider,
            "run_task",
            new=_fake_run_task,
        ):
            start_payload = json.loads(
                await middleware._control_task(
                    action="start",
                    description="长时间诊断",
                    subagent_type="system-diagnostician",
                )
            )
            await asyncio.wait_for(task_started.wait(), timeout=1)
            await middleware.aafter_agent({}, None)
            status_payload = json.loads(
                await middleware._control_task(
                    action="status",
                    task_ids=[start_payload["tasks"][0]["task_id"]],
                )
            )

        self.assertEqual("cancelled", status_payload["tasks"][0]["status"])
