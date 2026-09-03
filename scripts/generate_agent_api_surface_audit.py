#!/usr/bin/env python3
"""Generate the complete OpenAPI-to-Agent surface audit."""

from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from fastapi import FastAPI

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.agent.policy.api import API_OPERATION_ROUTES  # noqa: E402
from app.api.apiv1 import api_router  # noqa: E402

JSON_OUTPUT = PROJECT_ROOT / "docs/architecture/agent-api-surface-audit.json"
MARKDOWN_OUTPUT = PROJECT_ROOT / "docs/architecture/agent-api-surface-audit.md"
HTTP_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE"})

TRANSPORT_TAGS = frozenset(
    {
        "agent",
        "anthropic",
        "auth",
        "llm",
        "login",
        "mcp",
        "message",
        "mfa",
        "notification",
        "openai",
        "user",
        "webhook",
    }
)
CONSOLIDATED_TAGS = frozenset(
    {
        "anilist",
        "bangumi",
        "discover",
        "douban",
        "recommend",
        "tmdb",
    }
)
PROVIDER_PATH_PREFIXES = (
    "/api/v1/download/",
    "/api/v1/mediaserver/",
)
CONSOLIDATED_ROUTE_OWNERS: dict[tuple[str, str], str] = {
    ("POST", "/api/v1/download/"): "download.add",
    ("GET", "/api/v1/plugin/installed"): "plugin.installed",
    ("GET", "/api/v1/plugin/source/{plugin_id}/options"): "plugin.source.options",
    ("GET", "/api/v1/plugin/{plugin_id}"): "plugin.config.get",
    ("GET", "/api/v1/search/last"): "search.results",
    ("GET", "/api/v1/search/media/{media_id}/stream"): "search.torrents",
    ("GET", "/api/v1/search/subtitle/media/{media_id}/stream"): "subtitle.search.media",
    ("GET", "/api/v1/search/subtitle/title/stream"): "subtitle.search.title",
    ("GET", "/api/v1/search/title/stream"): "search.title",
    ("GET", "/api/v1/site/"): "site.list",
    ("GET", "/api/v1/site/cookie/{site_id}"): "site.cookie.update",
    ("GET", "/api/v1/site/domain/{site_url}"): "site.list",
    ("GET", "/api/v1/site/{site_id}"): "site.list",
    ("POST", "/api/v1/storage/list"): "storage.list",
    ("GET", "/api/v1/subscribe/list"): "subscription.list",
    ("GET", "/api/v1/system/env"): "config.system.get",
    ("POST", "/api/v1/system/env"): "config.system.update",
    ("GET", "/api/v1/system/global"): "config.system.get",
    ("GET", "/api/v1/system/setting/{key}"): "config.system.get",
    ("POST", "/api/v1/system/setting/{key}"): "config.system.update",
    ("GET", "/api/v1/transfer/now"): "scheduler.run",
    ("GET", "/api/v1/workflow/"): "workflow.list",
}
STREAM_OR_BINARY_PATHS = frozenset(
    {
        "/api/v1/plugin/file/{plugin_id}/{filepath}",
        "/api/v1/site/icon/{site_id}",
        "/api/v1/storage/download",
        "/api/v1/storage/image",
        "/api/v1/system/cache/image",
        "/api/v1/system/img/{proxy}",
        "/api/v1/system/logging",
        "/api/v1/system/logging/download/{name}",
        "/api/v1/system/message",
        "/api/v1/system/progress/{process_type}",
    }
)
UI_PRESENTATION_PATHS = frozenset(
    {
        "/api/v1/plugin/dashboard/meta",
        "/api/v1/plugin/dashboard/{plugin_id}",
        "/api/v1/plugin/dashboard/{plugin_id}/{key}",
        "/api/v1/plugin/page/{plugin_id}",
        "/api/v1/plugin/sidebar_nav",
    }
)
EXPLICIT_TRANSPORT_PATHS = frozenset(
    {
        "/api/v1/plugin/remotes",
        "/api/v1/subscribe/seerr",
        "/api/v1/system/ping",
    }
)
SUBSCRIPTION_EXECUTION_UI_PREFIX = "/api/v1/subscribe/execution/"
CLASSIFICATION_POLICY_UI_PREFIX = "/api/v1/media/classification/"


def _gateway_routes() -> dict[tuple[str, str], list[str]]:
    """Return exact HTTP routes and every stable gateway operation using them."""
    results: dict[tuple[str, str], list[str]] = defaultdict(list)
    for operation_id, route in API_OPERATION_ROUTES.items():
        results[(route.method.upper(), route.path)].append(operation_id)
    return {key: sorted(values) for key, values in results.items()}


def _classify(
    *,
    method: str,
    path: str,
    tags: list[str],
    gateway_routes: dict[tuple[str, str], list[str]],
) -> tuple[str, str, str, list[str]]:
    """Classify one OpenAPI operation into one reviewed Agent ownership boundary."""
    operations = gateway_routes.get((method, path), [])
    if operations:
        return (
            "gateway",
            "moviepilot-api",
            "Executable through moviepilot_api; exact inputs are generated into MCP tools/list and SKILL.md.",
            operations,
        )
    primary_tag = tags[0] if tags else "untagged"
    if primary_tag in TRANSPORT_TAGS:
        return (
            "transport_or_identity",
            "host-runtime",
            "Authentication, protocol compatibility, conversation transport, callback, or account lifecycle endpoint; never recursively exposed as an Agent business action.",
            [],
        )
    consolidated_owner = CONSOLIDATED_ROUTE_OWNERS.get((method, path))
    if consolidated_owner:
        return (
            "consolidated",
            "moviepilot-api",
            f"This compatibility, broader-response, or UI route is represented by the safer stable operation {consolidated_owner}.",
            [consolidated_owner],
        )
    if path in STREAM_OR_BINARY_PATHS:
        return (
            "stream_or_binary",
            "host-transport",
            "Streaming, image, archive, or file response consumed by a direct client; the structured JSON Agent gateway does not proxy binary or unbounded streams.",
            [],
        )
    if path in UI_PRESENTATION_PATHS:
        return (
            "ui_presentation",
            "host-ui",
            "Plugin-rendered page, dashboard, or navigation metadata owned by the frontend presentation contract rather than an Agent business action.",
            [],
        )
    if path.startswith(SUBSCRIPTION_EXECUTION_UI_PREFIX):
        return (
            "ui_presentation",
            "host-ui",
            "Background subscription execution status and cancellation are owned by the authenticated frontend workflow; they are not yet a stable Agent gateway contract.",
            [],
        )
    if path.startswith(CLASSIFICATION_POLICY_UI_PREFIX):
        return (
            "ui_presentation",
            "host-ui",
            "Classification policy authoring, validation, preview, impact analysis, and publication are owned by the authenticated frontend editor until a stable Agent governance contract is approved.",
            [],
        )
    if path in EXPLICIT_TRANSPORT_PATHS:
        return (
            "transport_or_identity",
            "host-runtime",
            "Health, bootstrap, federation, or external webhook transport endpoint; it is not recursively callable as an Agent business action.",
            [],
        )
    if path.startswith(PROVIDER_PATH_PREFIXES):
        return (
            "provider-skill",
            "downloader-operation" if "/download/" in path else "mediaserver-operation",
            "Low-level provider behavior is exposed by the self-describing provider Skill; high-level MoviePilot operations remain in moviepilot-api.",
            [],
        )
    if primary_tag in CONSOLIDATED_TAGS:
        return (
            "consolidated",
            "moviepilot-api",
            "Source-specific or presentation-oriented route is represented by a stable aggregate search, detail, person, recommendation, or music operation instead of duplicating every frontend route.",
            [],
        )
    if path.endswith("2") or "/schedule2" in path or "/recognize2" in path or "/recognize_file2" in path:
        return (
            "alternate-auth-duplicate",
            "moviepilot-api",
            "API-token compatibility duplicate; the Agent uses the corresponding bearer-authenticated operation with its persisted user identity.",
            [],
        )
    raise ValueError(f"Unclassified OpenAPI operation: {method} {path}")


def generate_audit() -> dict[str, Any]:
    """Build a deterministic entry for every v1 OpenAPI HTTP operation."""
    app = FastAPI()
    app.include_router(api_router, prefix="/api/v1")
    openapi = app.openapi()
    gateway_routes = _gateway_routes()
    entries = []
    for path, path_item in sorted(openapi.get("paths", {}).items()):
        for raw_method, operation in sorted(path_item.items()):
            method = raw_method.upper()
            if method not in HTTP_METHODS or not isinstance(operation, dict):
                continue
            tags = [str(tag) for tag in operation.get("tags") or []]
            disposition, owner, reason, operation_ids = _classify(
                method=method,
                path=path,
                tags=tags,
                gateway_routes=gateway_routes,
            )
            entries.append(
                {
                    "method": method,
                    "path": path,
                    "tags": tags,
                    "summary": str(operation.get("summary") or ""),
                    "disposition": disposition,
                    "owner": owner,
                    "operation_ids": operation_ids,
                    "reason": reason,
                }
            )
    counts = Counter(entry["disposition"] for entry in entries)
    matched_gateway_routes = {
        (entry["method"], entry["path"])
        for entry in entries
        if entry["disposition"] == "gateway"
    }
    dynamic_gateway_routes = [
        {
            "method": method,
            "path": path,
            "operation_ids": operation_ids,
            "reason": (
                "The executor validates and expands this bounded source placeholder to one of "
                "tmdb, douban, bangumi, or anilist before calling the corresponding concrete OpenAPI route."
            ),
        }
        for (method, path), operation_ids in sorted(gateway_routes.items())
        if (method, path) not in matched_gateway_routes
    ]
    return {
        "openapi_operation_count": len(entries),
        "gateway_operation_count": len(API_OPERATION_ROUTES),
        "gateway_http_route_count": len(gateway_routes),
        "matched_gateway_http_route_count": len(matched_gateway_routes),
        "dynamic_gateway_routes": dynamic_gateway_routes,
        "disposition_counts": dict(sorted(counts.items())),
        "operations": entries,
    }


def render_markdown(audit: dict[str, Any]) -> str:
    """Render the complete audit as a reviewable architecture document."""
    lines = [
        "# MoviePilot Agent API Surface Audit",
        "",
        "> Generated from the v1 FastAPI OpenAPI document and the fixed Agent API registry.",
        "> Do not edit route rows manually; run `scripts/generate_agent_api_surface_audit.py`.",
        "",
        "## Result",
        "",
        f"- OpenAPI HTTP operations: **{audit['openapi_operation_count']}**",
        f"- Stable `moviepilot_api` operations: **{audit['gateway_operation_count']}**",
        f"- Exact HTTP routes used by the gateway: **{audit['gateway_http_route_count']}**",
        f"- OpenAPI routes matched directly by the gateway: **{audit['matched_gateway_http_route_count']}**",
        f"- Bounded dynamic gateway routes: **{len(audit['dynamic_gateway_routes'])}**",
        "- Every gateway operation has a generated English oneOf input contract in MCP `tools/list` and `skills/moviepilot-api/SKILL.md`.",
        "- Every non-gateway OpenAPI operation is listed below with an explicit ownership boundary; it is not silently callable through arbitrary URL/method input.",
        "",
        "## Dispositions",
        "",
        "| disposition | count | meaning |",
        "| :--- | ---: | :--- |",
    ]
    meanings = {
        "gateway": "Approved structured MoviePilot Agent operation.",
        "provider-skill": "Low-level downloader or media-server capability owned by a provider Skill.",
        "consolidated": "Source/UI route represented by a stable aggregate Agent operation.",
        "alternate-auth-duplicate": "API-token compatibility duplicate of a bearer-authenticated capability.",
        "transport_or_identity": "Authentication, protocol, callback, account, or conversation transport boundary.",
        "stream_or_binary": "Streaming or binary response owned by a direct client transport.",
        "ui_presentation": "Frontend or plugin-rendered presentation contract.",
    }
    for disposition, count in audit["disposition_counts"].items():
        lines.append(f"| `{disposition}` | {count} | {meanings[disposition]} |")
    if audit["dynamic_gateway_routes"]:
        lines.extend(
            [
                "",
                "## Bounded Dynamic Routes",
                "",
                "| method | route template | operations | constraint |",
                "| :--- | :--- | :--- | :--- |",
            ]
        )
        for item in audit["dynamic_gateway_routes"]:
            lines.append(
                f"| `{item['method']}` | `{item['path']}` | {', '.join(item['operation_ids'])} | {item['reason']} |"
            )
    lines.extend(
        [
            "",
            "## Complete Route Inventory",
            "",
            "| method | path | tags | disposition | owner / operation | summary |",
            "| :--- | :--- | :--- | :--- | :--- | :--- |",
        ]
    )
    for item in audit["operations"]:
        tags = ", ".join(item["tags"]) or "-"
        owner = ", ".join(item["operation_ids"]) or item["owner"]
        summary = item["summary"].replace("|", "\\|")
        lines.append(
            f"| `{item['method']}` | `{item['path']}` | {tags} | `{item['disposition']}` | {owner} | {summary} |"
        )
    lines.extend(
        [
            "",
            "## Exposure Rule",
            "",
            "Every structured JSON business endpoint is either a stable gateway operation, a provider Skill capability, or an explicitly consolidated compatibility route. Authentication, webhook, stream, binary, and UI-presentation endpoints remain owned by their direct transport or frontend consumer and must not be made recursively callable by the Agent.",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    """Write deterministic JSON and Markdown audit artifacts."""
    audit = generate_audit()
    JSON_OUTPUT.write_text(
        json.dumps(audit, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    MARKDOWN_OUTPUT.write_text(render_markdown(audit), encoding="utf-8")
    print(
        "generated "
        f"{JSON_OUTPUT.relative_to(PROJECT_ROOT)} and {MARKDOWN_OUTPUT.relative_to(PROJECT_ROOT)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
