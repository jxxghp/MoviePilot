import json
from types import SimpleNamespace

import pytest
from anyio import Path as AsyncPath
from langchain.agents.middleware.types import ModelRequest
from langchain_core.messages import SystemMessage

from app.agent.middleware.skills import (
    MAX_SKILL_RESULT_CHARS,
    SKILL_TOOL_NAME,
    SkillsMiddleware,
    _alist_skills,
)
from app.agent.tools.tags import ToolTag


@pytest.fixture
def anyio_backend():
    """使用 asyncio 后端运行 anyio 异步测试。"""
    return "asyncio"


def _write_skill(root, skill_id: str, name: str | None = None) -> None:
    """写入测试用 Skill 文件。"""
    skill_dir = root / skill_id
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        f"""---
name: {name or skill_id}
description: test skill {skill_id}
allowed-tools: "read_file execute_command"
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

    assert ["a-skill", "m-skill", "z-skill"] == [
        skill["id"] for skill in skills
    ]


def test_skills_middleware_exposes_skill_tool(tmp_path):
    """SkillsMiddleware 应以中间件工具形式暴露 skill。"""
    _write_skill(tmp_path, "moviepilot-cli")

    middleware = SkillsMiddleware(sources=[str(tmp_path)])

    assert [tool.name for tool in middleware.tools] == [SKILL_TOOL_NAME]
    assert ToolTag.Read in middleware.tools[0].tags
    assert ToolTag.Skill in middleware.tools[0].tags
    assert "moviepilot-cli" in middleware.tools[0].description


@pytest.mark.anyio
async def test_skill_tool_loads_skill_by_id_and_name(tmp_path):
    """skill 工具应支持按 id 或 name 加载完整 SKILL.md。"""
    _write_skill(tmp_path, "moviepilot-cli", name="MoviePilot CLI")
    middleware = SkillsMiddleware(sources=[str(tmp_path)])
    skill_tool = middleware.tools[0]

    by_id = json.loads(await skill_tool.ainvoke({"name": "moviepilot-cli"}))
    by_name = json.loads(await skill_tool.ainvoke({"name": "MoviePilot CLI"}))

    assert by_id["success"] is True
    assert by_id["skill"]["id"] == "moviepilot-cli"
    assert "# moviepilot-cli" in by_id["content"]
    assert by_name["success"] is True
    assert by_name["skill"]["name"] == "MoviePilot CLI"


@pytest.mark.anyio
async def test_skill_tool_caps_large_result_before_model_context(tmp_path):
    """超大 Skill 内容应在工具返回前限制到模型上下文上限。"""
    _write_skill(tmp_path, "large-skill")
    skill_path = tmp_path / "large-skill" / "SKILL.md"
    with skill_path.open("a", encoding="utf-8") as file_handle:
        file_handle.write("\n" + ("large-line\n" * 30000))

    middleware = SkillsMiddleware(sources=[str(tmp_path)])
    result = await middleware.tools[0].ainvoke({"name": "large-skill"})
    payload = json.loads(result)

    assert len(result) <= MAX_SKILL_RESULT_CHARS
    assert payload["success"] is True
    assert payload["truncated"] is True
    assert "Skill 内容已截断" in payload["content"]


@pytest.mark.anyio
async def test_skill_tool_returns_not_found_for_unknown_skill(tmp_path):
    """skill 工具找不到技能时应返回结构化失败信息。"""
    middleware = SkillsMiddleware(sources=[str(tmp_path)])
    skill_tool = middleware.tools[0]

    result = json.loads(await skill_tool.ainvoke({"name": "missing-skill"}))

    assert result["success"] is False
    assert "missing-skill" in result["message"]


def test_modify_request_instructs_model_to_use_skill_tool_without_paths(tmp_path):
    """系统提示应要求通过 skill 工具加载，而不是直接暴露文件读取路径。"""
    _write_skill(tmp_path, "moviepilot-cli")
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

    assert "`skill` tool" in system_content
    assert "moviepilot-cli" in system_content
    assert "Read `" not in system_content
    assert str(tmp_path) not in system_content


@pytest.mark.anyio
async def test_skill_tool_call_records_streaming_summary(tmp_path):
    """skill 工具执行时应记录流式聚合摘要。"""
    _write_skill(tmp_path, "moviepilot-cli")
    calls = []
    stream_handler = SimpleNamespace(
        is_streaming=True,
        record_tool_call=lambda **kwargs: calls.append(kwargs),
    )
    middleware = SkillsMiddleware(
        sources=[str(tmp_path)],
        stream_handler=stream_handler,
    )
    request = SimpleNamespace(
        tool=SimpleNamespace(name=SKILL_TOOL_NAME),
        tool_call={
            "args": {
                "name": "moviepilot-cli",
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
            "tool_message": "Skill loaded",
            "tool_kwargs": {
                "name": "moviepilot-cli",
            },
        }
    ]
