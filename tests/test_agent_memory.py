import asyncio
import json
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from app.agent.middleware.memory import (
    SEARCH_MEMORY_TOOL_NAME,
    MemoryMiddleware,
    _summarize_with_llm,
    query_memory_files,
)
from app.agent.tools.factory import MoviePilotToolFactory
from app.agent.tools.tags import ToolTag
from app.runtime.tasks import TaskRegistry


def _write_activity_log(activity_dir, date_str: str, lines: list[str]) -> None:
    """写入测试用活动记忆文件。"""
    activity_dir.mkdir(parents=True, exist_ok=True)
    body = "\n".join(lines)
    (activity_dir / f"{date_str}.md").write_text(
        f"# {date_str} 活动记忆\n\n{body}\n",
        encoding="utf-8",
    )


async def _wait_memory_tasks(middleware: MemoryMiddleware) -> None:
    """等待活动记忆后台任务完成，避免测试与后台写入竞态。"""
    tasks = list(middleware._background_tasks)
    if tasks:
        await asyncio.gather(*tasks)


def test_memory_loads_only_primary_file(tmp_path):
    """每轮默认上下文只能装载 MEMORY.md，不应装载主题或活动正文。"""
    primary = tmp_path / "MEMORY.md"
    primary.write_text("用户偏好：简洁回复。", encoding="utf-8")
    (tmp_path / "MEDIA_RULES.md").write_text("主题记忆：优先 Remux。", encoding="utf-8")
    _write_activity_log(
        tmp_path / "activity",
        datetime.now().strftime("%Y-%m-%d"),
        ["- **10:00** 活动正文不应自动进入上下文"],
    )
    middleware = MemoryMiddleware(
        memory_dir=str(tmp_path),
        activity_dir=str(tmp_path / "activity"),
    )

    state_update = asyncio.run(middleware.abefore_agent({}, runtime=None, config=None))
    request = SimpleNamespace(
        state=state_update,
        system_message=SystemMessage(content="SYSTEM"),
        override=lambda **kwargs: SimpleNamespace(
            state=state_update,
            system_message=kwargs["system_message"],
        ),
    )
    modified = middleware.modify_request(request)
    system_text = str(modified.system_message.content)

    assert state_update["memory_contents"] == {str(primary): "用户偏好：简洁回复。"}
    assert "用户偏好：简洁回复" in system_text
    assert "主题记忆：优先 Remux" not in system_text
    assert "活动正文不应自动进入上下文" not in system_text
    assert "search_memory" in system_text
    assert "first tool call" in system_text


def test_memory_onboarding_still_requires_search_before_task(tmp_path):
    """主记忆为空时也必须要求 Agent 在执行任务前检索其它记忆。"""
    (tmp_path / "MEDIA_RULES.md").write_text("主题记忆：偏好 HEVC。", encoding="utf-8")
    middleware = MemoryMiddleware(memory_dir=str(tmp_path))
    state_update = asyncio.run(middleware.abefore_agent({}, runtime=None, config=None))
    prompt = middleware._format_agent_memory(
        state_update["memory_contents"],
        memory_empty=state_update["memory_empty"],
    )

    assert "primary memory file is empty" in prompt
    assert "first tool call" in prompt
    assert "search_memory" in prompt
    assert "偏好 HEVC" not in prompt


def test_query_memory_files_searches_topic_and_activity_categories(tmp_path):
    """统一检索工具应能按分类、关键词和日期读取主题与活动记忆。"""
    (tmp_path / "MEDIA_RULES.md").write_text(
        "# 媒体规则\n优先 Remux，字幕使用简体中文。\n",
        encoding="utf-8",
    )
    activity_dir = tmp_path / "activity"
    _write_activity_log(
        activity_dir,
        "2026-06-18",
        [
            "- **10:00** 帮用户整理了电影 A",
            "- **10:30** 查询了站点状态",
        ],
    )

    topic_payload = query_memory_files(
        str(tmp_path),
        activity_dir=str(activity_dir),
        query="Remux",
        category="topic",
        limit=10,
    )
    activity_payload = query_memory_files(
        str(tmp_path),
        activity_dir=str(activity_dir),
        query="整理",
        category="activity",
        date="2026-06-18",
        limit=10,
    )

    assert topic_payload["success"] is True
    assert topic_payload["entries"][0]["category"] == "topic"
    assert topic_payload["entries"][0]["text"] == "优先 Remux，字幕使用简体中文。"
    assert activity_payload["success"] is True
    assert activity_payload["entries"][0]["category"] == "activity"
    assert activity_payload["entries"][0]["summary"] == "帮用户整理了电影 A"
    assert activity_payload["entries"][0]["date"] == "2026-06-18"


def test_query_memory_files_supports_regex_and_bounds_results(tmp_path):
    """记忆检索应支持显式正则，并对返回条数做有界处理。"""
    (tmp_path / "one.md").write_text("整理电影 A\n查询站点\n", encoding="utf-8")
    (tmp_path / "two.md").write_text("整理电影 B\n", encoding="utf-8")

    payload = query_memory_files(
        str(tmp_path),
        query="整理|站点",
        category="topic",
        use_regex=True,
        limit=1,
    )
    invalid = query_memory_files(
        str(tmp_path),
        query="[",
        category="topic",
        use_regex=True,
    )

    assert payload["success"] is True
    assert payload["total_count"] == 3
    assert payload["returned_count"] == 1
    assert payload["truncated"] is True
    assert invalid["success"] is False
    assert "无效的记忆检索正则表达式" in invalid["message"]


def test_memory_middleware_exposes_search_tool(tmp_path):
    """统一记忆中间件应通过一个只读系统工具提供按需检索。"""
    middleware = MemoryMiddleware(memory_dir=str(tmp_path))

    assert [tool.name for tool in middleware.tools] == [SEARCH_MEMORY_TOOL_NAME]
    assert ToolTag.Read in middleware.tools[0].tags
    assert ToolTag.System in middleware.tools[0].tags
    assert middleware.tools[0]._agent_tool_source == "middleware:memory"
    assert "topic memory" in middleware.tools[0].description


def test_memory_search_tool_returns_json_payload(tmp_path):
    """search_memory 工具应返回结构化 JSON，而不是将文件正文隐式注入上下文。"""
    (tmp_path / "MEDIA_RULES.md").write_text("优先 Remux。\n", encoding="utf-8")
    middleware = MemoryMiddleware(memory_dir=str(tmp_path))

    result = asyncio.run(
        middleware.tools[0].ainvoke({"query": "Remux", "category": "topic", "limit": 5})
    )
    payload = json.loads(result)

    assert payload["success"] is True
    assert payload["returned_count"] == 1
    assert payload["entries"][0]["text"] == "优先 Remux。"


def test_memory_search_tool_reports_streaming_execution(tmp_path):
    """search_memory 工具执行时应复用统一的工具显示策略。"""

    async def _run_test():
        calls = []
        stream_handler = SimpleNamespace(
            is_streaming=True,
            report_tool_call=lambda **kwargs: calls.append(kwargs) or "tool-1",
            tool_call_finished=MagicMock(),
        )
        middleware = MemoryMiddleware(
            memory_dir=str(tmp_path),
            stream_handler=stream_handler,
        )
        request = SimpleNamespace(
            tool=SimpleNamespace(name=SEARCH_MEMORY_TOOL_NAME),
            tool_call={"args": {"query": "整理", "category": "activity"}},
        )

        async def _fake_handler(_request):
            """返回模拟工具结果。"""
            return "ok"

        result = await middleware.awrap_tool_call(request, _fake_handler)
        return result, calls, stream_handler.tool_call_finished

    result, calls, finished = asyncio.run(_run_test())

    assert result == "ok"
    assert calls == [
        {
            "tool_name": SEARCH_MEMORY_TOOL_NAME,
            "tool_message": '检索记忆，主要参数：{"query": "整理", "category": "activity"}',
            "tool_kwargs": {"query": "整理", "category": "activity"},
        }
    ]
    finished.assert_called_once_with("tool-1", "done")


def test_memory_middleware_sanitizes_its_own_logs(tmp_path):
    """记忆中间件读取参数和异常写日志时必须脱敏。"""

    async def _run_test():
        secret_marker = "memory-secret-marker-6825"
        stream_handler = SimpleNamespace(is_streaming=True, report_tool_call=MagicMock())
        middleware = MemoryMiddleware(
            memory_dir=str(tmp_path),
            stream_handler=stream_handler,
        )
        request = SimpleNamespace(
            tool=SimpleNamespace(name=SEARCH_MEMORY_TOOL_NAME),
            tool_call={"args": {"query": f"token={secret_marker}"}},
        )
        mock_logger = MagicMock()

        async def _failing_handler(_request):
            raise RuntimeError(f"Authorization: Bearer {secret_marker}")

        with patch("app.agent.middleware.memory.logger", mock_logger):
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


def test_memory_provider_error_does_not_echo_secret(tmp_path):
    """记忆 provider 内部异常不能进入日志或模型错误结果。"""
    secret_marker = "memory-provider-secret-3584"
    middleware = MemoryMiddleware(memory_dir=str(tmp_path))
    mock_logger = MagicMock()

    with (
        patch(
            "app.agent.middleware.memory.query_memory_files",
            side_effect=RuntimeError(f"OPENAI_API_KEY={secret_marker}"),
        ),
        patch("app.agent.middleware.memory.logger", mock_logger),
    ):
        result = asyncio.run(
            middleware._tool_provider.search_memory(query="visible")
        )

    assert secret_marker not in result
    assert secret_marker not in str(mock_logger.method_calls)
    assert "***" in result
    assert "***" in str(mock_logger.method_calls)


def test_activity_memory_records_under_unified_memory_directory(tmp_path):
    """活动摘要应写入 memory/activity，而不是旧的 agent/activity 目录。"""
    summary = "用户要求整理电影文件，助手调用 transfer_file 完成处理，结果成功。"
    activity_dir = tmp_path / "activity"

    async def _run_test():
        middleware = MemoryMiddleware(
            memory_dir=str(tmp_path),
            activity_dir=str(activity_dir),
        )
        with patch(
            "app.agent.middleware.memory._summarize_with_llm",
            new=AsyncMock(return_value=summary),
        ):
            await middleware.aafter_agent(
                {
                    "messages": [
                        HumanMessage(content="帮我整理电影"),
                        AIMessage(
                            content="",
                            tool_calls=[
                                {"name": "transfer_file", "args": {}, "id": "call_1"}
                            ],
                        ),
                        ToolMessage(content='{"success": true}', tool_call_id="call_1"),
                    ]
                },
                runtime=None,
            )
            await _wait_memory_tasks(middleware)

    asyncio.run(_run_test())

    log_files = list(activity_dir.glob("*.md"))
    assert len(log_files) == 1
    assert summary in log_files[0].read_text(encoding="utf-8")


def test_activity_memory_skips_trivial_greeting_without_llm(tmp_path):
    """无实际任务的寒暄不应调用 LLM，也不应写入活动记忆。"""

    async def _run_test():
        middleware = MemoryMiddleware(
            memory_dir=str(tmp_path),
            activity_dir=str(tmp_path / "activity"),
        )
        summarize_mock = AsyncMock(return_value="不应写入")
        with patch("app.agent.middleware.memory._summarize_with_llm", new=summarize_mock):
            await middleware.aafter_agent(
                {
                    "messages": [
                        HumanMessage(content="你好"),
                        AIMessage(content="你好，有什么可以帮你？"),
                    ]
                },
                runtime=None,
            )
            await _wait_memory_tasks(middleware)
        return summarize_mock

    summarize_mock = asyncio.run(_run_test())

    summarize_mock.assert_not_awaited()
    assert not list((tmp_path / "activity").glob("*.md"))


def test_activity_summary_background_task_follows_host_shutdown(tmp_path):
    """活动摘要任务必须登记统一 owner，并随宿主关停取消和收敛。"""

    async def _run_test():
        registry = TaskRegistry()
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def _blocked_record(_messages: list) -> None:
            """保持记录任务运行，直到宿主关停发出取消。"""
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

        middleware = MemoryMiddleware(
            memory_dir=str(tmp_path),
            activity_dir=str(tmp_path / "activity"),
            task_registry=registry,
        )
        with patch.object(middleware, "_record_activity", side_effect=_blocked_record):
            middleware._schedule_activity_recording([])
            await started.wait()
            owners = tuple(record.owner for record in registry.records)
            converged = await registry.shutdown(timeout_seconds=1.0)
            await asyncio.sleep(0)
            return owners, converged, cancelled.is_set(), middleware._background_tasks

    owners, converged, cancelled, background_tasks = asyncio.run(_run_test())

    assert owners == ("agent.memory.activity_record",)
    assert converged is True
    assert cancelled is True
    assert background_tasks == set()


def test_summarize_with_llm_ignores_skip_marker():
    """LLM 返回 SKIP 时应视为无需记录活动记忆。"""
    llm = SimpleNamespace(ainvoke=AsyncMock(return_value=SimpleNamespace(content="SKIP")))

    with patch(
        "app.agent.llm.LLMHelper.get_llm",
        new=AsyncMock(return_value=llm),
    ):
        summary = asyncio.run(_summarize_with_llm("用户: 你好"))

    assert summary is None
    llm.ainvoke.assert_awaited_once()


def test_activity_summary_hides_image_payload():
    """活动摘要输入只能保留图片占位符，不能把 Base64 写入活动记忆。"""
    from app.agent.middleware.memory import _format_conversation_for_summary

    content = [
        {"type": "text", "text": "请看看图片"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,secret"}},
    ]

    formatted = _format_conversation_for_summary([HumanMessage(content=content)])

    assert "[图片]" in formatted
    assert "secret" not in formatted


def test_factory_does_not_register_memory_tool():
    """记忆检索工具由统一中间件注册，不应进入全局工具工厂。"""
    with patch(
        "app.agent.tools.factory._get_plugin_agent_tools",
        return_value=[],
    ):
        tools = MoviePilotToolFactory.create_tools(
            session_id="memory-session",
            user_id="10001",
        )

    assert SEARCH_MEMORY_TOOL_NAME not in {tool.name for tool in tools}
