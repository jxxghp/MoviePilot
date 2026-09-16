"""Agent API surface inventory and generated contract drift tests."""

import json
import re
import runpy
from pathlib import Path

from app.agent.policy.api import API_OPERATION_ROUTES
from app.agent.tools.impl.api import MoviePilotApiTool

PROJECT_ROOT = Path(__file__).resolve().parents[1]
AUDIT_JSON = PROJECT_ROOT / "docs/refactor/agent-api-surface-audit.json"
AUDIT_MARKDOWN = PROJECT_ROOT / "docs/refactor/agent-api-surface-audit.md"
API_SKILL = PROJECT_ROOT / "skills/moviepilot-api/SKILL.md"


def _read_api_skill_contract() -> str:
    """Read the API Skill entrypoint together with all categorized contracts."""
    skill_root = API_SKILL.parent
    paths = [API_SKILL, *sorted((skill_root / "api").glob("*.md"))]
    return "\n".join(path.read_text(encoding="utf-8") for path in paths)


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


def test_cleanup_confirmation_stays_in_authenticated_management_workflow() -> None:
    """人工确认清理只关闭历史提示，不应作为 Agent 已验证外部清理的业务工具。"""
    classify = _load_generator()["_classify"]
    disposition, owner, _, operations = classify(
        method="POST",
        path="/api/v1/history/transfer/{history_id}/cleanup-resolved",
        tags=["history"],
        gateway_routes={},
    )
    assert (disposition, owner, operations) == ("ui_presentation", "host-ui", [])


def test_recent_business_routes_are_exposed_as_stable_gateway_operations() -> None:
    """近期高层业务端点必须保持在 moviepilot_api，而不是落入边界归属。"""
    audit = json.loads(AUDIT_JSON.read_text(encoding="utf-8"))
    routes = {(item["method"], item["path"]): item for item in audit["operations"]}
    expected = {
        ("POST", "/api/v1/download/artist-collection"): "download.artist_collection",
        ("GET", "/api/v1/tmdb/cache"): "media.cache.get",
        ("DELETE", "/api/v1/tmdb/cache/{cache_key}"): "media.cache.delete",
        ("DELETE", "/api/v1/tmdb/cache"): "media.cache.clear",
        ("GET", "/api/v1/subscribe/execution/batches"): "subscription.execution.list",
        ("GET", "/api/v1/subscribe/execution/batches/{batch_id}"): "subscription.execution.get",
        (
            "PUT",
            "/api/v1/subscribe/execution/batches/{batch_id}/cancel",
        ): "subscription.execution.cancel",
    }

    for route, operation_id in expected.items():
        item = routes[route]
        assert item["disposition"] == "gateway"
        assert item["operation_ids"] == [operation_id]


def test_moviepilot_api_skill_routes_contracts_to_category_files() -> None:
    """The API Skill entrypoint stays concise and every operation has a namespace file."""
    entrypoint = API_SKILL.read_text(encoding="utf-8")
    category_files = {
        path.stem: path.read_text(encoding="utf-8")
        for path in sorted((API_SKILL.parent / "api").glob("*.md"))
    }

    assert len(entrypoint.splitlines()) < 300
    assert "### `" not in entrypoint
    assert len(category_files) == 21
    assert "models" not in category_files
    assert all(
        f"### `{operation_id}`" in category_files[operation_id.split(".", 1)[0]]
        for operation_id in API_OPERATION_ROUTES
    )
    assert all("## Body Models" in content for content in category_files.values())


def test_every_gateway_operation_has_one_exact_english_skill_and_mcp_contract() -> None:
    """Every approved operation must be discoverable with matching exact English contracts."""
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
