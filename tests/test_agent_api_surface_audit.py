"""Agent API surface inventory and generated contract drift tests."""

import json
import re
import runpy
from pathlib import Path

from app.agent.policy.api import API_OPERATION_ROUTES
from app.agent.tools.impl.api import MoviePilotApiTool

PROJECT_ROOT = Path(__file__).resolve().parents[1]
AUDIT_JSON = PROJECT_ROOT / "docs/architecture/agent-api-surface-audit.json"
AUDIT_MARKDOWN = PROJECT_ROOT / "docs/architecture/agent-api-surface-audit.md"
API_SKILL = PROJECT_ROOT / "skills/moviepilot-api/SKILL.md"


def _load_generator() -> dict:
    """Load the audit generator without invoking its file-writing entrypoint."""
    return runpy.run_path(str(PROJECT_ROOT / "scripts/generate_agent_api_surface_audit.py"))


def test_agent_api_surface_audit_matches_live_openapi_and_registry() -> None:
    """The checked-in complete inventory must match live OpenAPI and the gateway registry."""
    generator = _load_generator()
    live = generator["generate_audit"]()
    checked_in = json.loads(AUDIT_JSON.read_text(encoding="utf-8"))

    assert checked_in == live
    assert AUDIT_MARKDOWN.read_text(encoding="utf-8") == generator["render_markdown"](live)
    assert live["openapi_operation_count"] == len(live["operations"])
    assert live["gateway_operation_count"] == len(API_OPERATION_ROUTES)
    assert sum(live["disposition_counts"].values()) == live["openapi_operation_count"]
    assert {item["disposition"] for item in live["operations"]} == {
        "alternate-auth-duplicate",
        "consolidated",
        "gateway",
        "provider-skill",
        "stream_or_binary",
        "transport_or_identity",
        "ui_presentation",
    }


def test_every_gateway_operation_has_one_exact_english_skill_and_mcp_contract() -> None:
    """Every approved operation must be discoverable with matching exact English contracts."""
    skill = API_SKILL.read_text(encoding="utf-8")
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
        assert not re.search(r"[\u3400-\u9fff]", branch["description"]), operation_id
        assert route.method in skill.split(f"### `{operation_id}`", 1)[1].split("\n### `", 1)[0]
        assert route.path in skill.split(f"### `{operation_id}`", 1)[1].split("\n### `", 1)[0]


def test_every_gateway_path_placeholder_is_required_by_its_mcp_branch() -> None:
    """固定路由的每个路径占位符都必须在工具 schema 中以同名必填字段暴露。"""
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


def test_every_non_gateway_openapi_route_has_an_explicit_owner_and_reason() -> None:
    """Unexposed REST routes must remain visible and deliberately owned, never silently omitted."""
    audit = json.loads(AUDIT_JSON.read_text(encoding="utf-8"))
    keys = set()
    for item in audit["operations"]:
        key = (item["method"], item["path"])
        assert key not in keys
        keys.add(key)
        assert item["owner"].strip()
        assert item["reason"].strip()
        if item["disposition"] != "gateway":
            assert item["disposition"] in {
                "alternate-auth-duplicate",
                "consolidated",
                "provider-skill",
                "stream_or_binary",
                "transport_or_identity",
                "ui_presentation",
            }

    dynamic = audit["dynamic_gateway_routes"]
    assert dynamic == [
        {
            "method": "GET",
            "path": "/api/v1/{source}/person/credits/{person_id}",
            "operation_ids": ["media.person.credits"],
            "reason": (
                "The executor validates and expands this bounded source placeholder to one of "
                "tmdb, douban, bangumi, or anilist before calling the corresponding concrete OpenAPI route."
            ),
        }
    ]
