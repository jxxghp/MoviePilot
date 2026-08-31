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
        "browser-use": "2",
        "command-dispatch": "2",
        "database-operation": "5",
        "feedback-issue": "9",
        "moviepilot-api": "16",
        "moviepilot-update": "4",
        "organize-files": "5",
        "transfer-failed-retry": "5",
        "generate-identifiers": "4",
        "create-moviepilot-plugin": "5",
        "create-moviepilot-skill": "3",
        "publish-moviepilot-plugin": "3",
        "downloader-operation": "2",
        "mediaserver-operation": "2",
    }

    for skill_name, expected_version in expected_versions.items():
        content = _read_skill(skill_name)

        assert _frontmatter_value(content, "version") == expected_version


def test_retired_moviepilot_cli_skill_is_removed() -> None:
    """正式 API 方案不得保留旧 MCP CLI Skill。"""
    assert not (SKILLS_ROOT / "moviepilot-cli" / "SKILL.md").exists()


def test_api_and_database_skills_declare_final_boundaries() -> None:
    """API Skill 应成为产品操作入口，数据库仍是显式最终兜底。"""
    api_content = _read_skill("moviepilot-api")
    db_content = _read_skill("database-operation")

    assert "allowed-tools: moviepilot_api" in api_content
    assert "allowed-api-operations:" in api_content
    assert "Never provide a URL, method, authentication header, API key" in api_content
    assert "retired tool name" in api_content
    assert "moviepilot tool" in api_content

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
    assert "apply_patch" in allowed_tools
    assert "search_web" in allowed_tools
    assert "browse_webpage" in allowed_tools


def test_agent_core_prompt_routes_code_tools_safely() -> None:
    """核心提示词应区分代码搜索、精确编辑和交互式命令场景。"""
    core_prompt = CORE_PROMPT_PATH.read_text(encoding="utf-8")

    assert '`execute_command(action="run")` with `rg`' in core_prompt
    assert "`replace_all=true` only when every match must change" in core_prompt
    assert "pick the editing tool by scope" in core_prompt
    assert "Use `apply_patch` when one logical change spans multiple files" in core_prompt
    assert "Use `action=run` for short bounded commands" in core_prompt
    assert "including SSH" in core_prompt
    assert "Never use shell redirection" in core_prompt
    assert "matching version of the official documentation" in core_prompt
    assert "Do not guess signatures from memory" in core_prompt
