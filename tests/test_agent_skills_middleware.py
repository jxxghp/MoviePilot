import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from anyio import Path as AsyncPath
from langchain.agents.middleware.types import ModelRequest
from langchain_core.messages import SystemMessage

from app.agent.callback import StreamingHandler
from app.agent.middleware.skills import (
    MAX_SKILL_CONTENT_BYTES,
    SKILL_TOOL_NAME,
    SkillsMiddleware,
    _alist_skills,
)
from app.agent.tools.base import DEFAULT_TOOL_RESULT_MAX_CHARS
from app.agent.tools.tags import ToolTag

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def anyio_backend():
    """使用 asyncio 后端运行 anyio 异步测试。"""
    return "asyncio"


def _write_skill(
    root,
    skill_id: str,
    name: str | None = None,
    allowed_api_operations: str = "",
) -> None:
    """写入测试用 Skill 文件。"""
    skill_dir = root / skill_id
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        f"""---
name: {name or skill_id}
description: test skill {skill_id}
allowed-tools: "read_file execute_command"
{f'allowed-api-operations: "{allowed_api_operations}"' if allowed_api_operations else ""}
---
# {skill_id}

Use this skill carefully.
""",
        encoding="utf-8",
    )


@pytest.mark.anyio
async def test_alist_skills_sorts_skill_directories_by_name(tmp_path):
    """异步扫描技能目录时应按目录名稳定排序。"""
    for skill_id in ("z-skill", "a-skill", "m-skill"):
        _write_skill(tmp_path, skill_id)

    skills = await _alist_skills(AsyncPath(str(tmp_path)))

    assert ["a-skill", "m-skill", "z-skill"] == [skill["id"] for skill in skills]


def test_skills_middleware_exposes_read_skill_tool(tmp_path):
    """SkillsMiddleware 应以中间件私有工具形式暴露 read_skill。"""
    _write_skill(tmp_path, "moviepilot-api")

    middleware = SkillsMiddleware(sources=[str(tmp_path)])

    assert [tool.name for tool in middleware.tools] == [SKILL_TOOL_NAME]
    assert SKILL_TOOL_NAME == "read_skill"
    assert ToolTag.Read in middleware.tools[0].tags
    assert ToolTag.Skill in middleware.tools[0].tags
    assert "moviepilot-api" in middleware.tools[0].description


@pytest.mark.anyio
async def test_read_skill_loads_body_and_supporting_files_by_id_and_name(tmp_path):
    """read_skill 应按 id 或 name 返回完整主体及稳定的辅助文件清单。"""
    _write_skill(tmp_path, "moviepilot-api", name="MoviePilot API")
    skill_dir = tmp_path / "moviepilot-api"
    (skill_dir / "references").mkdir()
    (skill_dir / "references" / "usage.md").write_text("usage", encoding="utf-8")
    (skill_dir / "scripts").mkdir()
    (skill_dir / "scripts" / "run.py").write_text("print('ok')", encoding="utf-8")
    middleware = SkillsMiddleware(sources=[str(tmp_path)])
    skill_tool = middleware.tools[0]

    by_id = json.loads(await skill_tool.ainvoke({"name": "moviepilot-api"}))
    by_name = json.loads(await skill_tool.ainvoke({"name": "MoviePilot API"}))

    assert by_id["success"] is True
    assert by_id["skill"]["id"] == "moviepilot-api"
    assert "# moviepilot-api" in by_id["content"]
    assert by_id["skill"]["allowed_api_operations"] == []
    assert by_id["supporting_files"] == [
        "references/usage.md",
        "scripts/run.py",
    ]
    assert by_id["truncated"] is False
    assert by_name["success"] is True
    assert by_name["skill"]["name"] == "MoviePilot API"


@pytest.mark.anyio
async def test_read_skill_caps_body_at_512_kib_without_global_64_kib_truncation(tmp_path):
    """read_skill 应绕过普通裁剪，但将技能主体限制为 512 KiB。"""
    _write_skill(tmp_path, "large-skill")
    skill_path = tmp_path / "large-skill" / "SKILL.md"
    final_marker = "END-OF-LARGE-SKILL"
    with skill_path.open("a", encoding="utf-8") as file_handle:
        file_handle.write("\n" + ("large-line\n" * 120000) + final_marker)

    middleware = SkillsMiddleware(sources=[str(tmp_path)])
    result = await middleware.tools[0].ainvoke({"name": "large-skill"})
    payload = json.loads(result)

    assert len(result) > DEFAULT_TOOL_RESULT_MAX_CHARS
    assert payload["success"] is True
    assert payload["content_limit_bytes"] == MAX_SKILL_CONTENT_BYTES
    assert len(payload["content"].encode("utf-8")) <= MAX_SKILL_CONTENT_BYTES
    assert payload["truncated"] is True
    assert "512 KiB" in payload["truncation_message"]
    assert final_marker not in payload["content"]
    assert payload["supporting_files"] == []


@pytest.mark.anyio
async def test_bundled_moviepilot_api_skill_loads_complete_contract() -> None:
    """内置 API Skill 的完整 operation 合同必须在运行时上限内且不截断。"""
    middleware = SkillsMiddleware(sources=[str(PROJECT_ROOT / "skills")])

    result = await middleware.tools[0].ainvoke({"name": "moviepilot-api"})
    payload = json.loads(result)

    assert payload["success"] is True
    assert payload["content_limit_bytes"] == MAX_SKILL_CONTENT_BYTES
    assert payload["truncated"] is False
    assert payload["truncation_message"] is None
    assert len(payload["skill"]["allowed_api_operations"]) == 205
    assert "### `workflow.update`" in payload["content"]


@pytest.mark.anyio
async def test_skill_tool_returns_not_found_for_unknown_skill(tmp_path):
    """read_skill 找不到技能时应返回结构化失败信息。"""
    middleware = SkillsMiddleware(sources=[str(tmp_path)])
    skill_tool = middleware.tools[0]

    result = json.loads(await skill_tool.ainvoke({"name": "missing-skill"}))

    assert result["success"] is False
    assert "missing-skill" in result["message"]


@pytest.mark.anyio
async def test_skill_operation_scope_is_enforced_for_api_gateway(tmp_path):
    """声明 API 操作范围的 Skill 只能调用联合授权范围内的 operation。"""
    _write_skill(
        tmp_path,
        "media-skill",
        allowed_api_operations="media.search media.detail",
    )
    middleware = SkillsMiddleware(sources=[str(tmp_path)])
    skill_tool = middleware.tools[0]
    await skill_tool.ainvoke({"name": "media-skill"})

    denied_request = SimpleNamespace(
        tool=SimpleNamespace(name="moviepilot_api"),
        tool_call={
            "id": "call-1",
            "args": {"operation_id": "subscription.delete"},
        },
    )
    handler = AsyncMock(return_value="should-not-run")

    result = await middleware.awrap_tool_call(denied_request, handler)

    assert result.name == "moviepilot_api"
    assert "skill_operation_denied" in result.content
    handler.assert_not_awaited()


@pytest.mark.anyio
async def test_api_gateway_auto_loads_builtin_moviepilot_skill(tmp_path):
    """直接调用 API 网关时应自动加载内置 MoviePilot API Skill。"""
    _write_skill(
        tmp_path,
        "moviepilot-api",
        allowed_api_operations="subscription.list",
    )
    middleware = SkillsMiddleware(sources=[str(tmp_path)])
    request = SimpleNamespace(
        tool=SimpleNamespace(name="moviepilot_api"),
        tool_call={
            "id": "call-auto-load",
            "args": {"operation_id": "subscription.list"},
        },
    )
    handler = AsyncMock(return_value="allowed-after-auto-load")

    result = await middleware.awrap_tool_call(request, handler)

    assert result == "allowed-after-auto-load"
    handler.assert_awaited_once_with(request)


@pytest.mark.anyio
async def test_api_gateway_requires_skill_declared_operation(tmp_path):
    """未加载声明 operation 的 Skill 时 API 网关必须默认拒绝。"""
    _write_skill(tmp_path, "plain-skill")
    middleware = SkillsMiddleware(sources=[str(tmp_path)])
    await middleware.tools[0].ainvoke({"name": "plain-skill"})
    request = SimpleNamespace(
        tool=SimpleNamespace(name="moviepilot_api"),
        tool_call={
            "id": "call-default-deny",
            "args": {"operation_id": "media.search"},
        },
    )
    handler = AsyncMock(return_value="should-not-run")

    result = await middleware.awrap_tool_call(request, handler)

    assert "skill_operation_denied" in result.content
    handler.assert_not_awaited()


@pytest.mark.anyio
async def test_skill_operation_scope_allows_declared_api_operation(tmp_path):
    """声明范围内的 API operation 应继续进入真实工具 handler。"""
    _write_skill(
        tmp_path,
        "media-skill",
        allowed_api_operations="media.search",
    )
    middleware = SkillsMiddleware(sources=[str(tmp_path)])
    await middleware.tools[0].ainvoke({"name": "media-skill"})
    request = SimpleNamespace(
        tool=SimpleNamespace(name="moviepilot_api"),
        tool_call={
            "id": "call-2",
            "args": {"operation_id": "media.search"},
        },
    )
    handler = AsyncMock(return_value="allowed")

    result = await middleware.awrap_tool_call(request, handler)

    assert result == "allowed"
    handler.assert_awaited_once_with(request)


def test_modify_request_instructs_model_to_use_read_skill_without_paths(tmp_path):
    """系统提示应要求用 read_skill 加载主体，而不是 read_file 或裸路径。"""
    _write_skill(tmp_path, "moviepilot-api")
    middleware = SkillsMiddleware(sources=[str(tmp_path)])
    skills_metadata = middleware._load_skills_metadata()
    request = ModelRequest(
        model=None,
        messages=[],
        system_message=SystemMessage(content="BASE"),
        state={"skills_metadata": skills_metadata},
        runtime=None,
    )

    modified = middleware.modify_request(request)
    system_content = str(modified.system_message.content)

    assert "`read_skill` tool" in system_content
    assert "never `read_file`" in system_content
    assert "up to 512 KiB" in system_content
    assert "moviepilot-api" in system_content
    assert "Read `" not in system_content
    assert str(tmp_path) not in system_content


@pytest.mark.anyio
async def test_read_skill_tool_call_reports_streaming_execution(tmp_path):
    """read_skill 工具执行时应使用统一的工具显示策略。"""
    _write_skill(tmp_path, "moviepilot-api")
    calls = []
    stream_handler = SimpleNamespace(
        is_streaming=True,
        report_tool_call=lambda **kwargs: calls.append(kwargs),
    )
    middleware = SkillsMiddleware(
        sources=[str(tmp_path)],
        stream_handler=stream_handler,
    )
    request = SimpleNamespace(
        tool=SimpleNamespace(name=SKILL_TOOL_NAME),
        tool_call={
            "args": {
                "name": "moviepilot-api",
            }
        },
    )

    async def _fake_handler(_request):
        """返回模拟工具结果。"""
        return "ok"

    result = await middleware.awrap_tool_call(request, _fake_handler)

    assert result == "ok"
    assert calls == [
        {
            "tool_name": SKILL_TOOL_NAME,
            "tool_message": "读取技能说明：moviepilot-api",
            "tool_kwargs": {
                "name": "moviepilot-api",
            },
        }
    ]


@pytest.mark.anyio
async def test_read_skill_tool_call_emits_detail_in_verbose_mode(tmp_path):
    """啰嗦模式下 read_skill 应逐条显示详情而不是生成技能汇总。"""
    _write_skill(tmp_path, "moviepilot-api")
    stream_handler = StreamingHandler()
    await stream_handler.start_streaming()
    middleware = SkillsMiddleware(
        sources=[str(tmp_path)],
        stream_handler=stream_handler,
    )
    request = SimpleNamespace(
        tool=SimpleNamespace(name=SKILL_TOOL_NAME),
        tool_call={"args": {"name": "moviepilot-api"}},
    )

    async def _fake_handler(_request):
        """返回模拟工具结果。"""
        return "ok"

    with patch("app.agent.callback.get_runtime_setting", return_value=True):
        result = await middleware.awrap_tool_call(request, _fake_handler)
    buffered_message = await stream_handler.take()

    assert result == "ok"
    assert buffered_message == "\n\n⚙️ => 读取技能说明：moviepilot-api\n\n"
    assert "查询了 1 个技能说明" not in buffered_message


@pytest.mark.anyio
async def test_skill_middleware_sanitizes_its_own_logs(tmp_path):
    """Skill 中间件读取参数和异常写日志时必须脱敏。"""
    secret_marker = "skill-secret-marker-2471"
    stream_handler = SimpleNamespace(
        is_streaming=True,
        report_tool_call=MagicMock(),
    )
    middleware = SkillsMiddleware(
        sources=[str(tmp_path)],
        stream_handler=stream_handler,
    )
    request = SimpleNamespace(
        tool=SimpleNamespace(name=SKILL_TOOL_NAME),
        tool_call={"args": {"name": f"api_key={secret_marker}"}},
    )
    mock_logger = MagicMock()

    async def _failing_handler(_request):
        raise RuntimeError(f"Authorization: Bearer {secret_marker}")

    with (
        patch("app.agent.middleware.skills.logger", mock_logger),
        pytest.raises(RuntimeError),
    ):
        await middleware.awrap_tool_call(request, _failing_handler)

    assert secret_marker not in str(mock_logger.method_calls)
    assert "***" in str(mock_logger.method_calls)


@pytest.mark.anyio
async def test_skill_provider_error_does_not_echo_secret(tmp_path):
    """Skill provider 内部捕获的异常不能进入日志或模型错误结果。"""
    secret_marker = "skill-provider-secret-6518"
    middleware = SkillsMiddleware(sources=[str(tmp_path)])
    mock_logger = MagicMock()

    with (
        patch.object(
            middleware._skill_provider,
            "_find_skill",
            new=AsyncMock(side_effect=RuntimeError(f"DATABASE_PASSWORD={secret_marker}")),
        ),
        patch("app.agent.middleware.skills.logger", mock_logger),
    ):
        result = await middleware._skill_provider.load_skill("visible-skill")

    assert secret_marker not in result
    assert secret_marker not in str(mock_logger.method_calls)
    assert "***" in result
    assert "***" in str(mock_logger.method_calls)
