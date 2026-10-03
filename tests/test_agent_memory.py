import asyncio
import json
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from langchain_core.messages import SystemMessage

from app.agent.middleware.memory import (
    SEARCH_MEMORY_TOOL_NAME,
    MemoryMiddleware,
    query_memory_files,
)
from app.agent.tools.factory import MoviePilotToolFactory
from app.agent.tools.tags import ToolTag


def _write_activity_log(activity_dir, date_str: str, lines: list[str]) -> None:
    """写入测试用活动记忆文件。"""
    activity_dir.mkdir(parents=True, exist_ok=True)
    body = "\n".join(lines)
    (activity_dir / f"{date_str}.md").write_text(
        f"# {date_str} 活动记忆\n\n{body}\n",
        encoding="utf-8",
    )


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
    assert "session_search" in system_text


def test_memory_loads_global_and_current_user_primary_files_only(tmp_path):
    """默认上下文应按全局再当前用户顺序加载，且不触及其他用户记忆。"""
    global_memory = tmp_path / "MEMORY.md"
    global_memory.write_text("公共规则：使用简洁风格。", encoding="utf-8")
    user_memory_dir = tmp_path / "users" / "user-a"
    user_memory = user_memory_dir / "MEMORY.md"
    user_memory.parent.mkdir(parents=True)
    user_memory.write_text("用户偏好：优先中文回答。", encoding="utf-8")
    other_memory = tmp_path / "users" / "user-b" / "MEMORY.md"
    other_memory.parent.mkdir(parents=True)
    other_memory.write_text("其他用户的私密偏好。", encoding="utf-8")

    middleware = MemoryMiddleware(
        memory_dir=str(tmp_path),
        user_memory_dir=str(user_memory_dir),
    )
    state_update = asyncio.run(middleware.abefore_agent({}, runtime=None, config=None))
    prompt = middleware._format_agent_memory(
        state_update["memory_contents"],
        memory_empty=state_update["memory_empty"],
    )

    assert state_update["memory_contents"] == {
        str(global_memory): "公共规则：使用简洁风格。",
        str(user_memory): "用户偏好：优先中文回答。",
    }
    assert prompt.index("公共规则：使用简洁风格") < prompt.index("用户偏好：优先中文回答")
    assert "其他用户的私密偏好" not in prompt
    assert str(user_memory) in prompt


def test_memory_onboarding_exposes_relevant_retrieval(tmp_path):
    """主记忆为空时仍提供按需主题与历史证据检索。"""
    (tmp_path / "MEDIA_RULES.md").write_text("主题记忆：偏好 HEVC。", encoding="utf-8")
    middleware = MemoryMiddleware(memory_dir=str(tmp_path))
    state_update = asyncio.run(middleware.abefore_agent({}, runtime=None, config=None))
    prompt = middleware._format_agent_memory(
        state_update["memory_contents"],
        memory_empty=state_update["memory_empty"],
    )

    assert "No primary durable memory is saved" in prompt
    assert "session_search" in prompt
    assert "search_memory" in prompt
    assert "偏好 HEVC" not in prompt


def test_query_memory_files_excludes_obsolete_activity_but_preserves_files(tmp_path):
    """旧活动文件原地保留，但所有分类与显式路径都不能把摘要当历史证据。"""
    (tmp_path / "MEDIA_RULES.md").write_text("优先 Remux", encoding="utf-8")
    _write_activity_log(tmp_path / "activity", "2026-06-18", ["- **10:00** 旧摘要"])
    payload = query_memory_files(str(tmp_path))
    assert [entry["text"] for entry in payload["entries"]] == ["优先 Remux"]
    assert query_memory_files(str(tmp_path), category="activity")["success"] is False
    old_file = tmp_path / "activity" / "2026-06-18.md"
    assert query_memory_files(str(tmp_path), file_path=str(old_file))["success"] is False
    assert old_file.exists()


def test_query_memory_files_scopes_global_and_current_user_memory(tmp_path):
    """检索只能看到公共记忆与当前用户记忆，不能跨用户读取。"""
    (tmp_path / "PUBLIC_RULES.md").write_text("公共主题：优先 Remux。\n", encoding="utf-8")
    user_memory_dir = tmp_path / "users" / "user-a"
    (user_memory_dir / "USER_RULES.md").parent.mkdir(parents=True)
    (user_memory_dir / "USER_RULES.md").write_text("当前用户主题：偏好 HEVC。\n", encoding="utf-8")
    other_memory = tmp_path / "users" / "user-b" / "USER_RULES.md"
    other_memory.parent.mkdir(parents=True)
    other_memory.write_text("其他用户主题：偏好 AV1。\n", encoding="utf-8")

    payload = query_memory_files(
        str(tmp_path),
        user_memory_dir=str(user_memory_dir),
        query="偏好",
        category="topic",
        limit=10,
    )
    other_file = query_memory_files(
        str(tmp_path),
        user_memory_dir=str(user_memory_dir),
        file_path=str(other_memory),
    )

    assert payload["success"] is True
    assert {entry["text"] for entry in payload["entries"]} == {
        "当前用户主题：偏好 HEVC。",
    }
    assert all(entry["scope"] == "user" for entry in payload["entries"])
    assert other_file["success"] is False


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
            tool_call={"args": {"query": "整理", "category": "topic"}},
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
            "tool_message": '检索记忆，主要参数：{"query": "整理", "category": "topic"}',
            "tool_kwargs": {"query": "整理", "category": "topic"},
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


def test_memory_has_no_activity_summary_hooks(tmp_path):
    """稳定记忆中间件不再生成或清理逐轮活动摘要。"""
    middleware = MemoryMiddleware(memory_dir=str(tmp_path))
    assert "aafter_agent" not in MemoryMiddleware.__dict__
    assert not hasattr(middleware, "_record_activity")
    assert not hasattr(middleware, "_background_tasks")
    assert "activity" not in middleware.tools[0].args_schema.model_json_schema()["properties"]["category"]["enum"]


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
