"""
Agent API 工具合同检查：Skill 分类文档、MCP schema 与固定路由注册表保持一致。
"""

import re
from pathlib import Path

from app.agent.policy.api import API_OPERATION_ROUTES
from app.agent.tools.impl.api import MoviePilotApiTool

PROJECT_ROOT = Path(__file__).resolve().parents[1]
API_SKILL = PROJECT_ROOT / "skills/moviepilot-api/SKILL.md"


def _read_api_skill_contract() -> str:
    """
    读取 API Skill 入口及全部分类契约文档。
    """
    skill_root = API_SKILL.parent
    paths = [API_SKILL, *sorted((skill_root / "api").glob("*.md"))]
    return "\n".join(path.read_text(encoding="utf-8") for path in paths)


def test_moviepilot_api_skill_routes_contracts_to_category_files() -> None:
    """
    API Skill 通过分类文档索引全部 operation 及请求体合同。
    """
    entrypoint = API_SKILL.read_text(encoding="utf-8")
    category_files = {
        path.stem: path.read_text(encoding="utf-8")
        for path in sorted((API_SKILL.parent / "api").glob("*.md"))
    }

    assert "### `" not in entrypoint
    assert len(category_files) == 21
    assert "models" not in category_files
    assert all(
        f"### `{operation_id}`" in category_files[operation_id.split(".", 1)[0]]
        for operation_id in API_OPERATION_ROUTES
    )
    assert all("## Body Models" in content for content in category_files.values())


def test_every_gateway_operation_has_exact_skill_and_chinese_mcp_contract() -> None:
    """
    每个获准操作都必须有唯一的 Skill 契约及对应的中文 MCP 契约。
    """
    skill = _read_api_skill_contract()
    schema = MoviePilotApiTool(session_id="audit", user_id="1").get_mcp_input_schema()
    branches = {
        branch["properties"]["operation_id"]["const"]: branch
        for branch in schema["oneOf"]
    }

    assert set(branches) == set(API_OPERATION_ROUTES)
    for operation_id, route in API_OPERATION_ROUTES.items():
        assert skill.count(f"### `{operation_id}`") == 1
        branch = branches[operation_id]
        assert branch["description"].strip()
        assert re.search(r"[\u3400-\u9fff]", branch["description"]), operation_id
        assert route.method in skill.split(f"### `{operation_id}`", 1)[1].split("\n### `", 1)[0]
        assert route.path in skill.split(f"### `{operation_id}`", 1)[1].split("\n### `", 1)[0]


def test_every_gateway_path_placeholder_is_required_by_its_mcp_branch() -> None:
    """
    固定路由的每个路径占位符都必须在工具 schema 中以同名必填字段暴露。
    """
    schema = MoviePilotApiTool(session_id="audit", user_id="1").get_mcp_input_schema()
    branches = {
        branch["properties"]["operation_id"]["const"]: branch
        for branch in schema["oneOf"]
    }

    for operation_id, route in API_OPERATION_ROUTES.items():
        expected = set(re.findall(r"{([^}]+)}", route.path))
        path_schema = branches[operation_id].get("properties", {}).get("path_params", {})
        assert set(path_schema.get("properties", {})) == expected, operation_id
        assert set(path_schema.get("required", [])) == expected, operation_id
