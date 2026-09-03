#!/usr/bin/env python3
"""Generate English Agent Skill contracts from runtime metadata."""

from __future__ import annotations

import json
import runpy
import sys
import textwrap
from pathlib import Path
from typing import Any, Mapping

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


DATABASE_TABLE_GUIDES: dict[str, tuple[str, str, str]] = {
    "alembic_version": (
        "Records the Alembic migration revision currently applied to the database.",
        "Diagnosing startup migration failures or a database/code revision mismatch.",
        "Never edit it directly; advance or roll back revisions only through Alembic.",
    ),
    "agentchat": (
        "Stores Web Agent and messaging-channel session indexes, titles, previews, and message snapshots.",
        "Tracing Agent history or context restoration by user, session, or update time.",
        "Owned by the Agent conversation service; do not rewrite message JSON, counters, or ownership.",
    ),
    "agenttask": (
        "Stores one-shot or recurring Agent task definitions, triggers, and the latest execution summary.",
        "Inspecting task ownership, enablement, cron/run_at settings, and the latest result.",
        "Create, update, enable, disable, or delete tasks through the Agent task API.",
    ),
    "agenttaskrun": (
        "Stores the input snapshot, status, timestamps, and result of each Agent task execution.",
        "Auditing one run or correlating a failure with task_id, run_id, and trigger source.",
        "Execution evidence owned by the task runner; never fabricate rows or edit run status.",
    ),
    "downloadfailure": (
        "Stores stable fingerprints, media/torrent context, errors, and retry scheduling for failed downloads.",
        "Analyzing failure causes, retry counts, next retry time, and affected media or sites.",
        "Owned by download-failure compensation; retry or clean records through its business API.",
    ),
    "downloadfiles": (
        "Maps downloader task hashes to full paths, save directories, relative files, and active state.",
        "Finding task files by downloader/download_hash or diagnosing savepath associations.",
        "Maintained by download and transfer flows; do not manually change state or path mappings.",
    ),
    "downloadhistory": (
        "Stores media identity, torrent, downloader, user, and recognition context for submitted downloads.",
        "Reviewing download history or tracing a media identity or hash back to its source.",
        "Written by the download use case; delete or correct records through the download-history API.",
    ),
    "mediaserveritem": (
        "Stores the local index and canonical media identity projected from media-server libraries.",
        "Checking library presence, server/library/path placement, and season information.",
        "This is a rebuildable projection; writes and cleanup belong to media-server synchronization.",
    ),
    "message": (
        "Stores inbound and outbound messages, channels, content, attachments, users, and timestamps.",
        "Paging notification history, distinguishing direction, or tracing duplicates by source.",
        "Written by messaging and notification services; clean it through the message API or retention job.",
    ),
    "outboxmessage": (
        "Stores externally visible side-effect intents committed atomically with business transactions.",
        "Diagnosing pending/processing/failed state, leases, attempts, and the last error.",
        "Owned by the Outbox Dispatcher state machine; never mark completion or delete undelivered events manually.",
    ),
    "passkey": (
        "Stores WebAuthn/PassKey credentials, public keys, signature counters, and activation state.",
        "Authorized authentication diagnostics such as ownership, activation, and last use.",
        "Security-sensitive; manage it only through the PassKey API and never disclose credential material.",
    ),
    "plugindata": (
        "Stores plugin-owned JSON values isolated by plugin_id and key.",
        "Diagnosing persistence or migration issues for one explicitly identified plugin and key.",
        "The plugin owns these values; prefer plugin capabilities or the plugin-data API.",
    ),
    "pluginidentity": (
        "Stores trusted source, payload source, version, receipt, and CAS revision for a physical plugin package.",
        "Auditing source binding, package generation, payload application, or identity conflicts.",
        "Plugin supply-chain state owned exclusively by installation and update transactions.",
    ),
    "plugininstallation": (
        "Stores plugin installation phase, membership target, identity revisions, and backup state.",
        "Diagnosing interrupted installations, rollback conditions, and package or backup presence.",
        "Owned by the plugin installation state machine; never advance phase or overwrite evidence manually.",
    ),
    "site": (
        "Stores private-tracker URLs, RSS, credentials, rate limits, proxy state, and downloader binding.",
        "Inspecting enablement, domain, rate limits, or downloader binding with minimal credential exposure.",
        "Contains cookies, API keys, and tokens; manage it through the site API.",
    ),
    "siteicon": (
        "Caches site names, domains, icon URLs, and Base64 icon content.",
        "Diagnosing missing icons, incorrect domain mapping, or cache generation.",
        "Rebuildable cache owned by site-icon synchronization; direct writes are not recommended.",
    ),
    "sitestatistic": (
        "Aggregates site request successes, failures, durations, latest state, and diagnostic notes.",
        "Comparing site availability, failure rate, and the most recent access state.",
        "Accumulated by site access statistics; never edit counters to conceal runtime behavior.",
    ),
    "siteuserdata": (
        "Stores tracker account level, traffic, ratio, seeding, and unread-message data.",
        "Inspecting account state, traffic trends, seeding volume, and the latest collection error.",
        "A site-scraping projection refreshed by synchronization; do not edit it directly.",
    ),
    "subscribe": (
        "Stores active movie, TV, or music subscriptions, filters, progress, and download targets.",
        "Inspecting state, missing episodes/tracks, quality rules, site scope, and match progress.",
        "Create, update, search, or delete through the subscription API to preserve state-machine consistency.",
    ),
    "subscribehistory": (
        "Stores snapshots of completed or archived subscriptions and their final filter state.",
        "Auditing historical subscriptions, media identity, completion criteria, and filter configuration.",
        "Generated by subscription completion and archival; restore or delete through its business API.",
    ),
    "subscriptionsearchbatch": (
        "Stores durable subscription search batches, source, aggregate state, counts, and cancellation requests.",
        "Inspecting user-visible search progress, recovery state, cancellation, and terminal outcomes.",
        "Owned by subscription search orchestration; create and cancel batches through the subscription API.",
    ),
    "subscriptionsearchtask": (
        "Stores one durable subscription search task per batch and subscription with leases and execution phases.",
        "Diagnosing queued, running, failed, cancelled, or recovered work and its current site.",
        "Advanced only by the search queue lease state machine; never rewrite leases or terminal states manually.",
    ),
    "subscriptionsitebudget": (
        "Stores per-site subscription search concurrency, cooldown, health, and fairness state.",
        "Diagnosing site pressure, cooldown deferrals, recent failures, and active search ownership.",
        "Owned by the subscription site-budget coordinator; do not clear cooldowns or counters by direct SQL.",
    ),
    "systemconfig": (
        "Stores JSON business configuration values keyed by SystemConfigKey.",
        "Verifying the physical value only when the managed settings API behaves unexpectedly.",
        "Use config.system.get/update first; direct writes bypass validation, events, and plugin admission.",
    ),
    "transferexecutionstep": (
        "Stores intent, attempt identity, state, and result evidence for each durable transfer operation.",
        "Diagnosing stuck, failed, or repeated steps by task_id or operation_id.",
        "Owned by the transfer execution state machine and lease CAS; never force state transitions manually.",
    ),
    "transferhistory": (
        "Stores transfer source, destination, mode, media identity, download linkage, and outcome.",
        "Reviewing success/failure history, destination paths, media classification, and download linkage.",
        "Written by transfer settlement; delete or retry through transfer-history business APIs.",
    ),
    "transferpending": (
        "Durably stores pending transfer input, plans, checkpoints, leases, retries, and manual review state.",
        "Diagnosing restart recovery, expired leases, retry_wait, terminal failures, or manual review.",
        "Core durable state machine advanced only by planning, execution, retry, and review services.",
    ),
    "transfersettlementreceipt": (
        "Stores immutable terminal settlement receipts with contiguous revisions per transfer task.",
        "Verifying that history, pending deletion, and execution fingerprints were settled reliably.",
        "Idempotency and audit evidence; append revisions only and never overwrite or delete old receipts.",
    ),
    "user": (
        "Stores user accounts, password hashes, administrator state, OTP, permissions, and preferences.",
        "Authorized diagnostics of account state, permissions, or authentication configuration.",
        "Security-sensitive; manage through user, permission, password, and two-factor APIs.",
    ),
    "userconfig": (
        "Stores per-user JSON configuration isolated by username and key.",
        "Inspecting UI preferences, message clear cursors, or other personalized state.",
        "Modify through the owning user or messaging API to preserve key semantics.",
    ),
    "workflow": (
        "Stores workflow definitions, triggers, action graphs, execution context, and runtime state.",
        "Inspecting scheduled/event workflows, pause state, current action, run count, and failures.",
        "Create, modify, run, pause, or reset through the workflow API.",
    ),
}


def _load_json_schema() -> dict[str, Any]:
    """Load the generated moviepilot_api MCP schema."""
    path = PROJECT_ROOT / "app/agent/policy/resources/api_mcp_schema.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("The API MCP schema must be a JSON object")
    return payload


def _schema_type(schema: Mapping[str, Any], definitions: Mapping[str, Any]) -> str:
    """Compress JSON Schema into a readable Skill type expression."""
    reference = schema.get("$ref")
    if isinstance(reference, str):
        return reference.rsplit("/", 1)[-1]
    if isinstance(schema.get("const"), (str, int, float, bool)):
        return f"{schema.get('type', 'value')}={schema['const']}"
    enum = schema.get("enum")
    if isinstance(enum, list):
        return f"{schema.get('type', 'value')}({','.join(map(str, enum))})"
    schema_type = schema.get("type")
    if schema_type == "array":
        items = schema.get("items")
        item_type = _schema_type(items, definitions) if isinstance(items, Mapping) else "value"
        return f"array<{item_type}>"
    if schema_type:
        return str(schema_type)
    any_of = schema.get("anyOf")
    if isinstance(any_of, list):
        return "|".join(
            _schema_type(item, definitions)
            for item in any_of
            if isinstance(item, Mapping)
        )
    if "properties" in schema or schema.get("additionalProperties") is not None:
        return "object"
    return "value"


def _field_suffix(schema: Mapping[str, Any]) -> str:
    """Format defaults, ranges, and collection constraints."""
    suffix: list[str] = []
    if "default" in schema:
        suffix.append(f"default `{schema['default']}`")
    if "minimum" in schema:
        suffix.append(f"minimum `{schema['minimum']}`")
    if "maximum" in schema:
        suffix.append(f"maximum `{schema['maximum']}`")
    if "minLength" in schema:
        suffix.append(f"minimum length `{schema['minLength']}`")
    if "minItems" in schema:
        suffix.append(f"minimum items `{schema['minItems']}`")
    return f"; {'; '.join(suffix)}" if suffix else ""


def _schema_fields(
    schema: Mapping[str, Any],
    definitions: Mapping[str, Any],
) -> list[tuple[str, str, str, bool]]:
    """Extract fields, types, descriptions, and required markers."""
    reference = schema.get("$ref")
    if isinstance(reference, str):
        schema = definitions.get(reference.rsplit("/", 1)[-1], {})
    properties = schema.get("properties")
    if not isinstance(properties, Mapping):
        return []
    required = set(schema.get("required") or [])
    fields = []
    for name, raw_schema in properties.items():
        if not isinstance(raw_schema, Mapping):
            continue
        fields.append(
            (
                str(name),
                _schema_type(raw_schema, definitions),
                str(raw_schema.get("description") or ""),
                str(name) in required,
            )
        )
    return fields


def _resolve_schema(
    schema: Mapping[str, Any],
    definitions: Mapping[str, Any],
) -> Mapping[str, Any]:
    """Resolve a local JSON Schema reference into an enumerable object."""
    reference = schema.get("$ref")
    if isinstance(reference, str):
        resolved = definitions.get(reference.rsplit("/", 1)[-1])
        if isinstance(resolved, Mapping):
            return resolved
    return schema


def _render_api_docs() -> str:
    """Render the complete MoviePilot API operation and field contract."""
    schema = _load_json_schema()
    definitions = schema.get("$defs") if isinstance(schema.get("$defs"), Mapping) else {}
    from app.agent.policy.api import API_OPERATION_ROUTES, API_OPERATION_SPECS

    specs = {spec.operation_id: spec for spec in API_OPERATION_SPECS}
    lines = [
        "## Operation Catalog",
        "",
        "The operations, HTTP methods, routes, and path/query/body fields below exactly match external MCP `tools/list`.",
        "A field name ending in `*` is required. Omit an empty bucket or send `{}`. Referenced body models are expanded below.",
        "For collection operations, `data` keeps its existing list or page-object shape. The gateway may add a sibling `collection` object with `result_count`, optional exact `total_count`, `page`, and `count`; it never replaces the list body with a new wrapper.",
        "When a collection contract exposes an exact total, answer count or summary requests from that API metadata. For optional legacy pagination, send `page=1,count=1` and read `collection.total_count`; never query the database merely because item data or a tool preview was truncated.",
        "If an endpoint or external source does not expose a total, `collection.total_count` is omitted instead of being guessed from the current page.",
        "",
    ]
    for operation_id in sorted(API_OPERATION_ROUTES):
        route = API_OPERATION_ROUTES[operation_id]
        branch = next(
            item
            for item in schema["oneOf"]
            if item["properties"]["operation_id"].get("const") == operation_id
        )
        description = str(branch.get("description") or "")
        effect = specs[operation_id].effect.value
        lines.extend(
            [
                f"### `{operation_id}`",
                f"`{route.method} {route.path}`; policy effect: `{effect}`.",
                f"Purpose: {description.split(' Method:', 1)[0].strip()}",
            ]
        )
        collection_contract = branch.get("x-moviepilot-collection")
        if isinstance(collection_contract, Mapping):
            if collection_contract.get("body_shape") == "page_object":
                lines.append(
                    "- `response`: structured page object; items stay in "
                    f"`{collection_contract['items_field']}` and the exact total stays in "
                    f"`{collection_contract['total_count_field']}`."
                )
            elif collection_contract.get("total_count_field"):
                if collection_contract.get("default_pagination") == "unpaginated":
                    lines.append(
                        "- `response`: `data` remains a list; omitting both `page` and `count` "
                        "keeps the complete legacy result. `collection.result_count` reports the "
                        "returned items and `collection.total_count` reports the exact pre-pagination "
                        "total. For counts or summaries, send `page=1,count=1`, read "
                        "`collection.total_count`, and do not fall back to a database query because "
                        "the item preview was truncated."
                    )
                else:
                    lines.append(
                        "- `response`: `data` remains a list and the endpoint's documented pagination "
                        "or limit defaults remain in effect. `collection.result_count` reports the "
                        "returned items and `collection.total_count` reports the exact total. For a "
                        "count-only request, use the smallest valid page and read that metadata instead "
                        "of querying the database after item truncation."
                    )
            else:
                lines.append(
                    "- `response`: `data` remains a list and `collection.result_count` reports "
                    "the returned items. `collection.total_count` is omitted because this endpoint "
                    "or its upstream source does not expose a total."
                )
        for bucket in ("path_params", "query", "body"):
            bucket_schema = branch["properties"].get(bucket)
            if not isinstance(bucket_schema, Mapping):
                lines.append(f"- `{bucket}`: none")
                continue
            resolved_bucket = _resolve_schema(bucket_schema, definitions)
            fields = _schema_fields(resolved_bucket, definitions)
            if not fields:
                if resolved_bucket.get("type") != "object":
                    required_mark = "*" if bucket in set(branch.get("required") or []) else ""
                    description_text = str(resolved_bucket.get("description") or "")
                    if not description_text:
                        raise ValueError(
                            f"API Skill scalar guidance is missing: {operation_id}.{bucket}"
                        )
                    lines.append(
                        f"- `{bucket}{required_mark}` ({_schema_type(resolved_bucket, definitions)}): "
                        f"{description_text}"
                    )
                    continue
                dynamic_description = str(resolved_bucket.get("description") or "")
                if resolved_bucket.get("additionalProperties") is True and dynamic_description:
                    required_mark = "*" if bucket in set(branch.get("required") or []) else ""
                    lines.append(f"- `{bucket}{required_mark}` (object): {dynamic_description}")
                    continue
                body_ref = bucket_schema.get("$ref")
                label = (
                    body_ref.rsplit("/", 1)[-1]
                    if isinstance(body_ref, str)
                    else "empty object"
                )
                lines.append(f"- `{bucket}`: `{label}` with no direct fields")
                continue
            rendered = []
            for name, field_type, description_text, required in fields:
                required_mark = "*" if required else ""
                if not description_text:
                    raise ValueError(
                        f"API Skill field guidance is missing: {operation_id}.{bucket}.{name}"
                    )
                raw_schema = resolved_bucket["properties"][name]
                rendered.append(
                    f"`{name}{required_mark}` ({field_type}{_field_suffix(raw_schema)}): {description_text}"
                )
            lines.append(f"- `{bucket}`: " + "; ".join(rendered))
        lines.append("")

    lines.extend(["### Referenced Body Models", ""])
    referenced: set[str] = set()
    for branch in schema["oneOf"]:
        for bucket in ("path_params", "query", "body"):
            bucket_schema = branch["properties"].get(bucket)
            if not isinstance(bucket_schema, Mapping):
                continue
            resolved_bucket = _resolve_schema(bucket_schema, definitions)
            for raw_schema in (resolved_bucket.get("properties") or {}).values():
                if not isinstance(raw_schema, Mapping):
                    continue
                refs = [raw_schema.get("$ref")]
                refs.extend(
                    item.get("$ref")
                    for item in raw_schema.get("anyOf", [])
                    if isinstance(item, Mapping)
                )
                for reference in refs:
                    if isinstance(reference, str) and reference.startswith("#/$defs/"):
                        referenced.add(reference.rsplit("/", 1)[-1])
    pending = list(referenced)
    while pending:
        name = pending.pop()
        model = definitions.get(name)
        if not isinstance(model, Mapping):
            continue
        for raw_schema in (model.get("properties") or {}).values():
            if not isinstance(raw_schema, Mapping):
                continue
            references = [raw_schema.get("$ref")]
            references.extend(
                item.get("$ref")
                for item in raw_schema.get("anyOf", [])
                if isinstance(item, Mapping)
            )
            for reference in references:
                if isinstance(reference, str) and reference.startswith("#/$defs/"):
                    child = reference.rsplit("/", 1)[-1]
                    if child not in referenced:
                        referenced.add(child)
                        pending.append(child)

    for name in sorted(referenced):
        model = definitions.get(name)
        if not isinstance(model, Mapping):
            continue
        lines.append(f"#### `{name}`")
        model_description = str(model.get("description") or "")
        if not model_description:
            raise ValueError(f"API Skill model guidance is missing: {name}")
        lines.append(model_description)
        fields = _schema_fields(model, definitions)
        if not fields:
            lines.append("This runtime model has no directly writable fields.")
        else:
            for field_name, field_type, description_text, required in fields:
                required_mark = "*" if required else ""
                raw_schema = model["properties"][field_name]
                if not description_text:
                    raise ValueError(f"API Skill model field guidance is missing: {name}.{field_name}")
                lines.append(
                    f"- `{field_name}{required_mark}` ({field_type}{_field_suffix(raw_schema)}): {description_text}"
                )
        lines.append("")

    lines.append(_render_system_settings_docs().rstrip())
    lines.append("")
    lines.extend(
        [
            "## Operation Order And Failure Handling",
            "",
            "1. Select the operation first, then place each value in its documented bucket. Never move query fields into path_params or send undeclared fields.",
            "2. Reuse the exact `media_source` + `media_id` pair returned by search. For music, also preserve `music_type`.",
            "3. Downloads, transfers, configuration/rule/plugin writes, scheduler/workflow runs, and deletions have side effects; obtain confirmation and inspect the result.",
            "4. `success=false`, HTTP errors, validation errors, and empty results are real outcomes. Never report them as success.",
            "5. Use `database-operation`, `downloader-operation`, or `mediaserver-operation` for their native capabilities. Never bypass the gateway with an arbitrary URL.",
            "",
        ]
    )
    return "\n".join(lines).rstrip() + "\n"


def _render_service_docs(script_path: Path, title: str) -> str:
    """Render one downloader or media-server action contract."""
    namespace = runpy.run_path(str(script_path))
    actions = namespace.get("ACTIONS")
    if not isinstance(actions, Mapping):
        raise ValueError(f"{script_path} does not expose ACTIONS")
    lines = [
        "## Complete Action Contract",
        "",
        f"This is the complete {title} action contract. It comes directly from the script `ACTIONS` registry and matches the external MCP `tools/list` oneOf branches.",
        "A field name ending in `*` is required. Put every action parameter in the `arguments` object.",
        "",
        "| action | Purpose and argument summary |",
        "| :--- | :--- |",
    ]
    for name, spec in sorted(actions.items()):
        contract = spec.to_dict(name)
        argument_names = [
            f"`{argument['name']}{'*' if argument['required'] else ''}`"
            for argument in contract["arguments"]
        ]
        summary = contract["description"]
        if argument_names:
            summary += "; arguments: " + ", ".join(argument_names)
        else:
            summary += "; no arguments"
        lines.append(f"| `{name}` | {summary} |")
    lines.append("")
    for name, spec in sorted(actions.items()):
        contract = spec.to_dict(name)
        lines.extend(
            [
                f"### `{name}`",
                f"{contract['description']} Effect: `{contract['effect']}`. Providers: `{', '.join(contract['providers'])}`.",
            ]
        )
        if not contract["arguments"]:
            lines.append("- `arguments`: `{}`")
        else:
            for argument in contract["arguments"]:
                required_mark = "*" if argument["required"] else ""
                extras = []
                if "default" in argument:
                    extras.append(f"default `{argument['default']}`")
                if argument.get("enum"):
                    extras.append(f"allowed values `{','.join(map(str, argument['enum']))}`")
                suffix = f"; {'; '.join(extras)}" if extras else ""
                lines.append(
                    f"- `{argument['name']}{required_mark}` ({argument['type']}{suffix}): {argument['description']}"
                )
        for rule in contract.get("argument_rules") or []:
            lines.append(f"- Rule: {rule}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _render_database_tables() -> str:
    """Render all ORM tables, purposes, and access boundaries."""
    from app.db.base import Base
    from app.db.models import load_all_models

    load_all_models()
    table_names = {"alembic_version", *Base.metadata.tables}
    missing_guides = table_names - DATABASE_TABLE_GUIDES.keys()
    stale_guides = DATABASE_TABLE_GUIDES.keys() - table_names
    if missing_guides or stale_guides:
        raise ValueError(
            "Database table guidance does not match ORM metadata: "
            f"missing={sorted(missing_guides)}, stale={sorted(stale_guides)}"
        )
    lines = [
        "## Core Tables",
        "",
        "`tables` returns the tables that exist in the current instance. The catalog below covers every MoviePilot ORM table plus Alembic metadata. Always treat the live `schema <table>` result as authoritative.",
        "",
    ]
    for table_name in sorted(table_names):
        purpose, query_usage, write_boundary = DATABASE_TABLE_GUIDES[table_name]
        if table_name == "alembic_version":
            columns = "`version_num`"
        else:
            table = Base.metadata.tables[table_name]
            columns = ", ".join(f"`{column.name}`" for column in table.columns)
        lines.extend(
            [
                f"### `{table_name}`",
                f"- Purpose: {purpose}",
                f"- Useful queries: {query_usage}",
                f"- Write boundary: {write_boundary}",
                f"- Columns: {columns}",
                "",
            ]
        )
    lines.extend(
        [
            "## Database Action Contract",
            "",
            "- `tables`: `arguments={}` lists current database tables.",
            "- `schema`: `arguments={\"table_name\":\"downloadhistory\"}`; table_name must come from `tables`.",
            "- `query`: `arguments={\"sql\":\"SELECT ...\",\"limit\":100,\"write\":false}`; provide exactly one of sql and file. SELECT/WITH/EXPLAIN are allowed by default.",
            "- `write`: `arguments={\"sql\":\"UPDATE ... WHERE ...\"}`; provide exactly one of sql and file and only one statement.",
            "- `file` is a local SQL path readable by the MoviePilot process. MCP clients normally send `sql` directly.",
            "",
            "Use the live `schema` result instead of guessing columns from older documentation. Treat `media_source` and `media_id` as one atomic identity pair.",
            "",
        ]
    )
    return "\n".join(lines).rstrip() + "\n"


def _render_system_settings_docs() -> str:
    """Render dynamic system-setting discovery and update guidance."""
    return "\n".join([
        "## System Settings Contract",
        "",
        "Do not enumerate setting keys in this Skill. Settings change as MoviePilot evolves, so use `config.system.get` as the runtime discovery operation before updating an unfamiliar key.",
        "",
        "| `source` | Contents | Persistence |",
        "| :--- | :--- | :--- |",
        "| `settings` | Runtime `Settings` fields such as APP_DOMAIN or LLM_MODEL | Type-converted and persisted to `app.env`, then applied to the current process |",
        "| `systemconfig` | Database-backed business configuration such as downloaders, media servers, directories, and notifications | Written through the configuration service with plugin admission and change events |",
        "",
        "The `systemconfig` database table is only the physical store for the second source. Use `config.system.get/update` for normal reads and writes. Direct SQL is reserved for an explicitly authorized repair when the managed API cannot complete the operation.",
        "",
        "### Discover definitions",
        "",
        "1. Call `config.system.get` with `query={\"group\":\"settings\",\"keyword\":\"LLM\"}` or another group/keyword. Discovery defaults to summaries instead of full values.",
        "2. Each returned setting includes `setting_key`, `source`, `group`, `label`, and a `definition` object with `declared_type`, current `value_shape`, `nullable`, `sensitive`, allowed `update_operations`, `default_match_field`, and `persistence`.",
        "3. Read one exact value with `query={\"setting_key\":\"LLM_MODEL\"}`. Exact-key reads include the value by default.",
        "4. Use `show_secrets=true` only when an administrator explicitly requests the plaintext value; secret reads remain confirmation-protected.",
        "",
        "### Update settings",
        "",
        "Choose an operation listed in the discovered setting definition, then send it in `body`:",
        "",
        "| operation | Fields | Meaning |",
        "| :--- | :--- | :--- |",
        "| `replace` | `setting_key*`, `value` | Replace the complete scalar, list, or object value |",
        "| `merge_dict` | `setting_key*`, `value`; optional `remove_keys` | Shallow-merge an object and optionally remove keys |",
        "| `upsert_list_item` | `setting_key*`, `value`; optional `match_field`, `match_value` | Replace a matched list item or append it when absent |",
        "| `remove_list_item` | `setting_key*`, `value` or `match_value`; optional `match_field` | Remove one matched list item without replacing the list |",
        "",
        "After every update, call `config.system.get` again with the exact setting_key and verify the saved value. Do not guess a key, value shape, list match field, or update operation when discovery can return it.",
        "",
    ]).rstrip() + "\n"


def _sync_api_frontmatter(text: str) -> str:
    """Synchronize the API Skill authorization list with the fixed operation registry."""
    from app.agent.policy.api import list_api_operation_ids

    operation_text = " ".join(list_api_operation_ids())
    wrapped = textwrap.wrap(
        operation_text,
        width=96,
        break_long_words=False,
        break_on_hyphens=False,
    )
    replacement = "allowed-api-operations: >-\n" + "\n".join(f"  {line}" for line in wrapped) + "\n"
    lines = text.splitlines(keepends=True)
    frontmatter_end = next(
        (index for index, line in enumerate(lines[1:], start=1) if line.strip() == "---"),
        None,
    )
    if frontmatter_end is None:
        raise ValueError("moviepilot-api Skill frontmatter closing marker is missing")
    field_index = next(
        (
            index
            for index, line in enumerate(lines[:frontmatter_end])
            if line.startswith("allowed-api-operations:")
        ),
        None,
    )
    if field_index is None:
        raise ValueError("moviepilot-api Skill frontmatter operation list is missing")
    field_end = field_index + 1
    while field_end < frontmatter_end and lines[field_end].startswith((" ", "\t")):
        field_end += 1
    lines[field_index:field_end] = [replacement]
    return "".join(lines)


def _replace_section(text: str, heading: str, replacement: str, next_heading: str | None = None) -> str:
    """Replace one Markdown section from a heading to the next heading."""
    start = text.index(heading)
    if next_heading is None:
        return text[:start].rstrip() + "\n\n" + replacement
    end = text.index(next_heading, start)
    return text[:start].rstrip() + "\n\n" + replacement + "\n" + text[end:]


def main() -> int:
    """Generate complete contracts for four built-in Skills."""
    api_path = PROJECT_ROOT / "skills/moviepilot-api/SKILL.md"
    api_text = api_path.read_text(encoding="utf-8")
    api_text = _sync_api_frontmatter(api_text)
    api_path.write_text(
        _replace_section(api_text, "## Operation Catalog", _render_api_docs()),
        encoding="utf-8",
    )

    downloader_path = PROJECT_ROOT / "skills/downloader-operation/SKILL.md"
    downloader_text = downloader_path.read_text(encoding="utf-8")
    downloader_path.write_text(
        _replace_section(
            downloader_text,
            "## Complete Action Contract",
            _render_service_docs(
                PROJECT_ROOT / "skills/downloader-operation/scripts/mp-downloader.py",
                "Downloader Operation",
            ),
            "## Verification",
        ),
        encoding="utf-8",
    )

    mediaserver_path = PROJECT_ROOT / "skills/mediaserver-operation/SKILL.md"
    mediaserver_text = mediaserver_path.read_text(encoding="utf-8")
    mediaserver_path.write_text(
        _replace_section(
            mediaserver_text,
            "## Complete Action Contract",
            _render_service_docs(
                PROJECT_ROOT / "skills/mediaserver-operation/scripts/mp-mediaserver.py",
                "Media Server Operation",
            ),
            "## Safety And Verification",
        ),
        encoding="utf-8",
    )

    database_path = PROJECT_ROOT / "skills/database-operation/SKILL.md"
    database_text = database_path.read_text(encoding="utf-8")
    database_path.write_text(
        _replace_section(
            database_text,
            "## Core Tables",
            _render_database_tables(),
            "## Common Queries",
        ),
        encoding="utf-8",
    )
    print("generated four Agent Skill contracts")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
