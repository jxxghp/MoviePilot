import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from langchain_core.language_models.fake_chat_models import FakeListChatModel

import app.agent.middleware.subagents as subagent_module
from app.agent.middleware.policy import AgentPolicyMiddleware
from app.agent.middleware.subagents import (
    MoviePilotSubAgentMiddleware,
    SUBAGENT_CONTROL_TOOL_NAME,
    SUBAGENT_TASK_TOOL_NAME,
    SubAgentTaskControlMiddleware,
    create_subagent_middlewares,
)
from app.agent.policy import AuthSource, PrincipalType, ToolOrigin, ToolPolicyContext
from app.agent.tools.tags import ToolTag


def test_create_subagent_middlewares_registers_task_tool():
    """子代理中间件应向主 Agent 注册 task 委派工具。"""
    model = FakeListChatModel(responses=["ok"])

    middlewares, task_tools = create_subagent_middlewares(
        model=model,
        tools=[],
        stream_handler=None,
    )

    assert len(middlewares) == 2
    assert isinstance(middlewares[0], MoviePilotSubAgentMiddleware)
    assert isinstance(middlewares[1], SubAgentTaskControlMiddleware)
    assert [tool.name for tool in task_tools] == [
        SUBAGENT_TASK_TOOL_NAME,
        SUBAGENT_CONTROL_TOOL_NAME,
    ]
    assert "media-researcher" in task_tools[0].description
    assert "moviepilot-explorer" in task_tools[0].description
    assert "system-diagnostician" in task_tools[0].description
    assert "action=start" in task_tools[1].description
    assert "action=wait" in task_tools[1].description
    assert "action=pipeline" in task_tools[1].description


def test_subagent_tools_are_selected_by_tags():
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

    assert [tool.name for tool in captured["tools"]] == ["custom_media_lookup"]


def test_subagent_graph_registers_policy_middleware_as_outermost():
    """懒加载的子代理图必须继承宿主上下文并先经过 policy middleware。"""
    model = FakeListChatModel(responses=["ok"])
    context = ToolPolicyContext(
        session_id="subagent-session",
        user_id="user-1",
        origin=ToolOrigin.SUBAGENT,
        principal_type=PrincipalType.SUBAGENT,
        auth_source=AuthSource.INTERNAL,
        agent_context={"is_admin": True},
        channel="Telegram",
        source="telegram",
    )
    captured = {}

    def _fake_create_agent(**kwargs):
        captured.update(kwargs)
        return kwargs

    middleware = MoviePilotSubAgentMiddleware(
        model=model,
        profiles=subagent_module._builtin_subagent_profiles(),
        tools=[],
        policy_context=context,
    )

    with patch.object(subagent_module, "create_agent", side_effect=_fake_create_agent):
        middleware._get_agent("general-purpose")

    assert isinstance(captured["middleware"][0], AgentPolicyMiddleware)
    assert captured["middleware"][0].context is context
    assert captured["middleware"][0].context.origin is ToolOrigin.SUBAGENT


def test_moviepilot_explorer_selects_code_and_settings_tools():
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

    assert [tool.name for tool in captured["tools"]] == [
        "custom_code_reader",
        "custom_directory_lister",
        "custom_settings_reader",
        "custom_command_runner",
    ]


def test_builtin_tools_declare_tags_in_implementation():
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

    assert missing_tools == []


def test_task_tool_call_records_streaming_summary():
    """task 子代理工具执行时应记录流式聚合摘要。"""

    async def _run_test():
        calls = []
        stream_handler = SimpleNamespace(
            is_streaming=True,
            record_tool_call=lambda **kwargs: calls.append(kwargs),
        )
        middleware = MoviePilotSubAgentMiddleware(
            model=FakeListChatModel(responses=["ok"]),
            profiles=subagent_module._builtin_subagent_profiles(),
            tools=[],
            stream_handler=stream_handler,
        )
        request = SimpleNamespace(
            tool=SimpleNamespace(name=SUBAGENT_TASK_TOOL_NAME),
            tool_call={
                "args": {
                    "description": "检查媒体信息",
                    "subagent_type": "media-researcher",
                }
            },
        )

        async def _fake_handler(_request):
            return "ok"

        result = await middleware.awrap_tool_call(request, _fake_handler)
        return result, calls

    result, calls = asyncio.run(_run_test())

    assert result == "ok"
    assert calls == [
        {
            "tool_name": SUBAGENT_TASK_TOOL_NAME,
            "tool_message": "Subagent invoked",
            "tool_kwargs": {
                "description": "检查媒体信息",
                "subagent_type": "media-researcher",
            },
        }
    ]


def test_task_middleware_sanitizes_its_own_logs():
    """子代理中间件读取任务参数和异常写日志时必须脱敏。"""

    async def _run_test():
        secret_marker = "subagent-secret-marker-7316"
        stream_handler = SimpleNamespace(
            is_streaming=True,
            record_tool_call=MagicMock(),
        )
        middleware = MoviePilotSubAgentMiddleware(
            model=FakeListChatModel(responses=["ok"]),
            profiles=subagent_module._builtin_subagent_profiles(),
            tools=[],
            stream_handler=stream_handler,
        )
        request = SimpleNamespace(
            tool=SimpleNamespace(name=SUBAGENT_TASK_TOOL_NAME),
            tool_call={
                "args": {
                    "description": f"password={secret_marker}",
                    "subagent_type": "media-researcher",
                }
            },
        )
        mock_logger = MagicMock()

        async def _failing_handler(_request):
            raise RuntimeError(f"Authorization: Bearer {secret_marker}")

        with patch.object(subagent_module, "logger", mock_logger):
            try:
                await middleware.awrap_tool_call(request, _failing_handler)
            except RuntimeError:
                pass
            else:
                raise AssertionError("middleware should re-raise handler errors")

        return secret_marker, mock_logger

    secret_marker, mock_logger = asyncio.run(_run_test())

    assert secret_marker not in str(mock_logger.method_calls)
    assert "***" in str(mock_logger.method_calls)


def test_control_tool_call_records_streaming_summary():
    """subagent_task 子代理工具执行时应记录流式聚合摘要。"""

    async def _run_test():
        calls = []
        stream_handler = SimpleNamespace(
            is_streaming=True,
            record_tool_call=lambda **kwargs: calls.append(kwargs),
        )
        middleware = SubAgentTaskControlMiddleware(
            model=FakeListChatModel(responses=["ok"]),
            profiles=subagent_module._builtin_subagent_profiles(),
            tools=[],
            stream_handler=stream_handler,
        )
        request = SimpleNamespace(
            tool=SimpleNamespace(name=SUBAGENT_CONTROL_TOOL_NAME),
            tool_call={
                "args": {
                    "action": "start",
                    "tasks": [
                        {"subagent_type": "media-researcher"},
                        {"subagent_type": "download-diagnostician"},
                    ],
                }
            },
        )

        async def _fake_handler(_request):
            return "ok"

        result = await middleware.awrap_tool_call(request, _fake_handler)
        return result, calls

    result, calls = asyncio.run(_run_test())

    assert result == "ok"
    assert calls == [
        {
            "tool_name": SUBAGENT_CONTROL_TOOL_NAME,
            "tool_message": "Subagent invoked",
            "tool_kwargs": {
                "action": "start",
                "tasks": [
                    {"subagent_type": "media-researcher"},
                    {"subagent_type": "download-diagnostician"},
                ],
            },
        }
    ]


def test_control_tool_starts_tasks_concurrently_and_waits():
    """异步子代理管控工具应批量启动任务，并在 wait 时收集结果。"""

    async def _run_test():
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

        assert start_payload["success"]
        assert len(task_ids) == 2
        assert running_descriptions == ["检查媒体库", "检查下载器"]
        assert [task["status"] for task in wait_payload["tasks"]] == [
            "completed",
            "completed",
        ]
        assert "media-researcher:检查媒体库" in wait_payload["tasks"][0]["result"]
        assert (
            "download-diagnostician:检查下载器"
            in wait_payload["tasks"][1]["result"]
        )

    asyncio.run(_run_test())


def test_control_tool_pipeline_passes_previous_results_to_next_step():
    """管道模式应顺序执行子代理，并把上一步结果作为下一步私有上下文。"""

    async def _run_test():
        model = FakeListChatModel(responses=["ok"])
        middleware = SubAgentTaskControlMiddleware(
            model=model,
            profiles=subagent_module._builtin_subagent_profiles(),
            tools=[],
        )
        calls = []

        async def _fake_run_task(self, *, description, subagent_type, task_id=None):
            calls.append(
                {
                    "description": description,
                    "subagent_type": subagent_type,
                    "task_id": task_id,
                }
            )
            return f"结果-{len(calls)}"

        with patch.object(
            subagent_module._SubAgentAgentProvider,
            "run_task",
            new=_fake_run_task,
        ):
            payload = json.loads(
                await middleware._control_task(
                    action="pipeline",
                    tasks=[
                        {
                            "description": "识别媒体",
                            "subagent_type": "media-researcher",
                        },
                        {
                            "description": "检查下载",
                            "subagent_type": "download-diagnostician",
                        },
                        {
                            "description": "汇总结论",
                            "subagent_type": "general-purpose",
                        },
                    ],
                    timeout_ms=1000,
                )
            )

        assert payload["success"]
        assert [call["subagent_type"] for call in calls] == [
            "media-researcher",
            "download-diagnostician",
            "general-purpose",
        ]
        assert calls[0]["description"] == "识别媒体"
        assert "结果-1" in calls[1]["description"]
        assert "结果-1" in calls[2]["description"]
        assert "结果-2" in calls[2]["description"]
        assert [task["status"] for task in payload["tasks"]] == [
            "completed",
            "completed",
            "completed",
        ]
        assert [task["result"] for task in payload["tasks"]] == [
            "结果-1",
            "结果-2",
            "结果-3",
        ]

    asyncio.run(_run_test())


def test_control_tool_pipeline_stops_after_failed_step():
    """管道模式遇到失败步骤时应中断后续子代理。"""

    async def _run_test():
        model = FakeListChatModel(responses=["ok"])
        middleware = SubAgentTaskControlMiddleware(
            model=model,
            profiles=subagent_module._builtin_subagent_profiles(),
            tools=[],
        )
        calls = []
        secret_marker = "subagent-runtime-secret-9042"

        async def _fake_run_task(self, *, description, subagent_type, task_id=None):
            calls.append(subagent_type)
            if subagent_type == "download-diagnostician":
                raise RuntimeError(
                    f"下载器不可用 DATABASE_PASSWORD={secret_marker}"
                )
            return f"{subagent_type}:ok"

        with patch.object(
            subagent_module._SubAgentAgentProvider,
            "run_task",
            new=_fake_run_task,
        ):
            payload = json.loads(
                await middleware._control_task(
                    action="pipeline",
                    tasks=[
                        {
                            "description": "识别媒体",
                            "subagent_type": "media-researcher",
                        },
                        {
                            "description": "检查下载",
                            "subagent_type": "download-diagnostician",
                        },
                        {
                            "description": "汇总结论",
                            "subagent_type": "general-purpose",
                        },
                    ],
                    timeout_ms=1000,
                )
            )

        assert not payload["success"]
        assert "第 2 个管道子代理任务执行失败" in payload["error"]
        assert calls == ["media-researcher", "download-diagnostician"]
        assert [task["status"] for task in payload["tasks"]] == [
            "completed",
            "failed",
        ]
        assert "下载器不可用" in payload["tasks"][1]["error"]
        assert secret_marker not in payload["error"]
        assert secret_marker not in payload["tasks"][1]["error"]
        assert "***" in payload["error"]
        assert "***" in payload["tasks"][1]["error"]

    asyncio.run(_run_test())


def test_control_tool_pipeline_timeout_is_bounded_when_task_ignores_cancel():
    """管道步骤忽略取消时，等待上限仍必须按时返回失败。"""

    async def _run_test():
        model = FakeListChatModel(responses=["ok"])
        middleware = SubAgentTaskControlMiddleware(
            model=model,
            profiles=subagent_module._builtin_subagent_profiles(),
            tools=[],
        )
        release = asyncio.Event()
        cancelled = asyncio.Event()

        async def _ignore_cancel(self, *, description, subagent_type, task_id=None):
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                cancelled.set()
                await release.wait()
                return "late-result"

        with patch.object(
            subagent_module._SubAgentAgentProvider,
            "run_task",
            new=_ignore_cancel,
        ):
            pipeline = asyncio.create_task(
                middleware._control_task(
                    action="pipeline",
                    description="慢任务",
                    timeout_ms=10,
                )
            )
            payload = json.loads(await asyncio.wait_for(pipeline, timeout=0.2))

        assert payload["success"] is False
        assert "等待超时" in payload["error"]
        assert payload["tasks"][0]["status"] == "running"
        assert cancelled.is_set()

        release.set()
        await asyncio.wait_for(
            middleware._tasks[payload["tasks"][0]["task_id"]].task,
            timeout=0.2,
        )

    asyncio.run(_run_test())


def test_after_agent_cancels_unfinished_tasks():
    """Agent 结束时应取消仍在运行的异步子代理任务。"""

    async def _run_test():
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
            task_id = start_payload["tasks"][0]["task_id"]
            await middleware.aafter_agent({}, None)
            status_payload = json.loads(
                await middleware._control_task(
                    action="status",
                    task_ids=[task_id],
                )
            )

        assert status_payload["tasks"] == []
        assert status_payload["missing_task_ids"] == [task_id]

    asyncio.run(_run_test())
