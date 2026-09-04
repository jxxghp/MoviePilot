import re
import runpy
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SKILLS_ROOT = PROJECT_ROOT / "skills"
CORE_PROMPT_PATH = PROJECT_ROOT / "app/agent/prompt/System Core Prompt.txt"

RETIRED_TOOL_COVERAGE: dict[str, tuple[str, ...]] = {
    "add_custom_filter_rule": ("api:filter.custom.add",),
    "add_download_tasks": ("api:download.add",),
    "add_rule_group": ("api:filter.group.add",),
    "add_subscribe": ("api:subscription.add",),
    "create_agent_task": ("native:agent_task",),
    "delete_agent_task": ("native:agent_task",),
    "delete_custom_filter_rule": ("api:filter.custom.delete",),
    "delete_download_history": ("api:download.history.delete",),
    "delete_download_tasks": ("downloader:tasks.delete",),
    "delete_rule_group": ("api:filter.group.delete",),
    "delete_subscribe": ("api:subscription.delete",),
    "delete_transfer_history": ("api:transfer.history.delete",),
    "get_recommendations": ("api:recommendation.list",),
    "get_search_results": ("api:search.results",),
    "install_plugin": ("api:plugin.install", "api:plugin.source.install"),
    "list_directory": ("api:storage.list",),
    "list_slash_commands": ("api:slash.list",),
    "query_agent_tasks": ("native:agent_task",),
    "query_builtin_filter_rules": ("api:filter.builtin",),
    "query_custom_filter_rules": ("api:filter.custom",),
    "query_custom_identifiers": ("api:config.identifiers.get",),
    "query_directory_settings": ("api:storage.settings",),
    "query_download_tasks": ("downloader:tasks.list",),
    "query_downloaders": ("api:download.clients", "downloader:instances.list"),
    "query_episode_schedule": ("api:media.episode_schedule",),
    "query_installed_plugins": ("api:plugin.installed",),
    "query_library_exists": ("api:library.exists",),
    "query_library_latest": ("api:library.latest", "mediaserver:activity.latest"),
    "query_market_plugins": ("api:plugin.market",),
    "query_media_detail": ("api:media.detail",),
    "query_personas": ("native:persona",),
    "query_plugin_capabilities": ("api:plugin.capabilities",),
    "query_plugin_config": ("api:plugin.config.get",),
    "query_plugin_data": ("api:plugin.data",),
    "query_popular_subscribes": ("api:subscription.popular",),
    "query_rule_groups": ("api:filter.groups",),
    "query_schedulers": ("api:scheduler.list",),
    "query_site_userdata": ("api:site.userdata",),
    "query_sites": ("api:site.list",),
    "query_subscribe_history": ("api:subscription.history",),
    "query_subscribe_shares": ("api:subscription.shares",),
    "query_subscribes": ("api:subscription.list",),
    "query_system_settings": ("api:config.system.get",),
    "query_transfer_history": ("api:transfer.history",),
    "query_workflows": ("api:workflow.list",),
    "recognize_media": ("api:media.recognize",),
    "reload_plugin": ("api:plugin.reload",),
    "run_agent_task": ("native:agent_task",),
    "run_scheduler": ("api:scheduler.run",),
    "run_slash_command": ("api:slash.run",),
    "run_workflow": ("api:workflow.run",),
    "scrape_metadata": ("api:media.scrape",),
    "search_media": ("api:media.search",),
    "search_person": ("api:media.person.search",),
    "search_person_credits": ("api:media.person.credits",),
    "search_subscribe": ("api:subscription.search",),
    "search_torrents": ("api:search.torrents",),
    "switch_persona": ("native:persona",),
    "test_site": ("api:site.test",),
    "transfer_file": ("api:transfer.file",),
    "uninstall_plugin": ("api:plugin.uninstall",),
    "update_agent_task": ("native:agent_task",),
    "update_custom_filter_rule": ("api:filter.custom.update",),
    "update_custom_identifiers": ("api:config.identifiers.update",),
    "update_download_tasks": (
        "downloader:tasks.start",
        "downloader:tasks.stop",
        "downloader:tasks.tags.set",
        "downloader:tasks.properties.set",
        "downloader:tasks.trackers.update",
        "downloader:tasks.location.set",
        "downloader:tasks.category.set",
    ),
    "update_persona_definition": ("native:persona",),
    "update_plugin_config": ("api:plugin.config.get", "api:plugin.config.update"),
    "update_rule_group": ("api:filter.group.update",),
    "update_site": ("api:site.update",),
    "update_site_cookie": ("api:site.cookie.update",),
    "update_subscribe": ("api:subscription.update",),
    "update_system_settings": ("api:config.system.get", "api:config.system.update"),
}


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
        "database-operation": "6",
        "feedback-issue": "9",
        "moviepilot-api": "25",
        "moviepilot-update": "5",
        "organize-files": "5",
        "transfer-failed-retry": "5",
        "generate-identifiers": "4",
        "create-moviepilot-plugin": "5",
        "create-moviepilot-skill": "3",
        "publish-moviepilot-plugin": "3",
        "downloader-operation": "3",
        "mediaserver-operation": "3",
    }

    for skill_name, expected_version in expected_versions.items():
        content = _read_skill(skill_name)

        assert _frontmatter_value(content, "version") == expected_version


def test_retired_moviepilot_cli_skill_is_removed() -> None:
    """正式 API 方案不得保留旧 MCP CLI Skill。"""
    assert not (SKILLS_ROOT / "moviepilot-cli" / "SKILL.md").exists()
    assert not (SKILLS_ROOT / "moviepilot-api" / "scripts" / "mp-api.py").exists()
    assert not (SKILLS_ROOT / "moviepilot-update" / "scripts" / "mp-update.py").exists()


def test_core_prompt_requires_read_skill_for_skill_documents() -> None:
    """核心提示必须阻止模型用 read_file 分段读取 SKILL.md。"""
    core_prompt = CORE_PROMPT_PATH.read_text(encoding="utf-8")

    assert "Always use `read_skill`, never `read_file`, to load a skill's SKILL.md" in core_prompt
    assert "returns up to 512 KiB of the skill body" in core_prompt
    assert "do not use `read_file` to bypass the limit" in core_prompt


def test_api_collection_counts_must_use_gateway_metadata_before_database() -> None:
    """核心提示与 API Skill 必须阻止列表截断后错误回退数据库统计。"""
    core_prompt = CORE_PROMPT_PATH.read_text(encoding="utf-8")
    api_skill = _read_skill("moviepilot-api")

    assert "optional legacy pagination should use `page=1,count=1`" in core_prompt
    assert "Do not query the database merely because" in core_prompt
    assert 'send `query={"page":1,"count":1}`' in api_skill
    assert "Never query the MoviePilot" in api_skill
    assert "database merely to recover a total" in api_skill


def test_every_retired_business_tool_has_a_live_precise_owner() -> None:
    """全部 72 个退出业务工具必须由 API、provider Skill 或统一原生工具承接。"""
    from app.agent.policy.api import API_OPERATION_ROUTES
    from app.agent.tools.factory import MoviePilotToolFactory

    downloader_actions = runpy.run_path(
        str(SKILLS_ROOT / "downloader-operation/scripts/mp-downloader.py")
    )["ACTIONS"]
    mediaserver_actions = runpy.run_path(
        str(SKILLS_ROOT / "mediaserver-operation/scripts/mp-mediaserver.py")
    )["ACTIONS"]
    native_tools = {
        MoviePilotToolFactory._tool_class_name(tool_class)
        for tool_class in MoviePilotToolFactory.BUILTIN_TOOL_CLASSES
    }

    assert len(RETIRED_TOOL_COVERAGE) == 72
    for retired_name, owners in RETIRED_TOOL_COVERAGE.items():
        assert not (PROJECT_ROOT / f"app/agent/tools/impl/{retired_name}.py").exists()
        assert owners, retired_name
        for owner in owners:
            owner_type, owner_name = owner.split(":", 1)
            if owner_type == "api":
                assert owner_name in API_OPERATION_ROUTES, (retired_name, owner)
            elif owner_type == "downloader":
                assert owner_name in downloader_actions, (retired_name, owner)
            elif owner_type == "mediaserver":
                assert owner_name in mediaserver_actions, (retired_name, owner)
            else:
                assert owner_type == "native"
                assert owner_name in native_tools, (retired_name, owner)


def test_api_and_database_skills_declare_final_boundaries() -> None:
    """API Skill 应成为产品操作入口，数据库仍是显式最终兜底。"""
    api_content = _read_skill("moviepilot-api")
    db_content = _read_skill("database-operation")

    assert "allowed-tools: moviepilot_api" in api_content
    assert "allowed-api-operations:" in api_content
    assert "Never provide a URL, method, authentication header, API key" in api_content
    assert "retired tool name" in api_content
    assert "moviepilot tool" in api_content

    update_content = _read_skill("moviepilot-update")
    assert "allowed-tools: moviepilot_api" in update_content
    assert "system.update.install" in update_content
    assert "mp-api.py" not in update_content
    assert "mp-update.py" not in update_content

    assert "direct SQL boundary" in db_content
    assert "Use this skill as the final fallback" in db_content
    assert "INSERT" in db_content
    assert "UPDATE" in db_content
    assert "DELETE" in db_content
    assert "allowed-tools: execute_command" in db_content
    from app.db.base import Base
    from app.db.models import load_all_models

    load_all_models()
    for table_name in Base.metadata.tables:
        heading = f"### `{table_name}`"
        assert heading in db_content
        section = db_content.split(heading, 1)[1].split("\n### `", 1)[0]
        assert "- Purpose:" in section
        assert "- Useful queries:" in section
        assert "- Write boundary:" in section
        assert "- Columns:" in section
    assert "### `alembic_version`" in db_content
    alembic_section = db_content.split("### `alembic_version`", 1)[1].split(
        "\n### `", 1
    )[0]
    assert "- Purpose:" in alembic_section
    assert "- Useful queries:" in alembic_section
    assert "Never edit it directly" in alembic_section


def test_api_skill_uses_runtime_system_setting_discovery() -> None:
    """系统设置 Skill 应指导动态发现定义，而不是复制不断变化的键清单。"""
    api_content = _read_skill("moviepilot-api")

    assert "## System Settings Contract" in api_content
    assert "Do not enumerate setting keys in this Skill" in api_content
    assert '`query={"group":"settings","keyword":"LLM"}`' in api_content
    assert "`declared_type`" in api_content
    assert "`update_operations`" in api_content
    assert "`persistence`" in api_content
    assert "### Settings variables" not in api_content
    assert "### SystemConfig keys" not in api_content


def test_refactored_agent_skills_use_english_guidance() -> None:
    """四个重构 Skill 的模型指导文本必须统一为英文。"""
    for skill_name in (
        "moviepilot-api",
        "downloader-operation",
        "mediaserver-operation",
        "database-operation",
        "moviepilot-update",
    ):
        content = _read_skill(skill_name)
        assert not re.search(r"[\u3400-\u9fff]", content), skill_name
        assert "按接口模型语义传值" not in content


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
