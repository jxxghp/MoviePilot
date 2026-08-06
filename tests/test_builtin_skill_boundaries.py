from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SKILLS_ROOT = PROJECT_ROOT / "skills"
CORE_PROMPT_PATH = PROJECT_ROOT / "app/agent/prompt/System Core Prompt.txt"


def _read_skill(skill_name: str) -> str:
    """读取内置技能的 SKILL.md 内容。"""
    return (SKILLS_ROOT / skill_name / "SKILL.md").read_text(encoding="utf-8")


def _frontmatter_value(content: str, key: str) -> str:
    """从 SKILL.md frontmatter 中读取单行字段值。"""
    for line in content.splitlines():
        if line.startswith(f"{key}:"):
            return line.split(":", 1)[1].strip()
    return ""


def test_modified_builtin_skills_have_incremented_versions() -> None:
    """本次修改过的内置技能必须递增版本，确保用户端同步更新。"""
    expected_versions = {
        "database-operation": "3",
        "moviepilot-api": "8",
        "moviepilot-cli": "6",
        "moviepilot-update": "3",
        "transfer-failed-retry": "2",
        "create-moviepilot-plugin": "3",
    }

    for skill_name, expected_version in expected_versions.items():
        content = _read_skill(skill_name)

        assert _frontmatter_value(content, "version") == expected_version


def test_moviepilot_cli_skill_uses_local_tool_boundary() -> None:
    """CLI 技能应只描述本地 MCP tool 边界，不再默认使用旧 Node 脚本。"""
    content = _read_skill("moviepilot-cli")

    assert "moviepilot tool" in content
    assert "scripts/mp-cli.js" not in content
    assert "Use `scripts/mp-cli.js`" not in content
    assert "node scripts/mp-cli.js" not in content
    assert "any request involving movies" not in content
    assert "whenever the user explicitly mentions MoviePilot" not in content
    assert "Do not ask the user" in content
    assert "moviepilot-api" in content
    assert "database-operation" in content


def test_api_and_database_skills_declare_fallback_boundaries() -> None:
    """API 和数据库技能应明确各自兜底边界，避免抢占普通产品操作。"""
    api_content = _read_skill("moviepilot-api")
    db_content = _read_skill("database-operation")

    assert "REST API bridge" in api_content
    assert "Do not use this skill just because MoviePilot is mentioned" in api_content
    assert "moviepilot-cli" in api_content
    assert "Direct SQL query or database update" in api_content

    assert "direct SQL boundary" in db_content
    assert "Use this skill as the final fallback" in db_content
    assert "INSERT" in db_content
    assert "UPDATE" in db_content
    assert "DELETE" in db_content


def test_agent_core_prompt_does_not_block_plugin_source_edits() -> None:
    """核心提示词不应禁止插件开发技能写入源码。"""
    core_prompt = CORE_PROMPT_PATH.read_text(encoding="utf-8")
    plugin_skill = _read_skill("create-moviepilot-plugin")
    allowed_tools = _frontmatter_value(plugin_skill, "allowed-tools")

    assert "file editing tools, or generated patches to change code" not in core_prompt
    assert "write_file" in allowed_tools
    assert "edit_file" in allowed_tools
    assert "search_web" in allowed_tools
    assert "browse_webpage" in allowed_tools


def test_agent_core_prompt_routes_code_tools_safely() -> None:
    """核心提示词应区分代码搜索、精确编辑和交互式命令场景。"""
    core_prompt = CORE_PROMPT_PATH.read_text(encoding="utf-8")

    assert '`execute_command(action="run")` with `rg`' in core_prompt
    assert "`replace_all=true` only when every match must change" in core_prompt
    assert "Use `action=run` for short bounded commands" in core_prompt
    assert "including SSH" in core_prompt
    assert "Never use shell redirection" in core_prompt
    assert "matching version of the official documentation" in core_prompt
    assert "Do not guess signatures from memory" in core_prompt
