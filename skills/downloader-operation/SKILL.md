---
name: downloader-operation
version: 2
description: >-
  Use this skill when the user asks to inspect, diagnose, or directly control a
  configured qBittorrent, Transmission, or rTorrent instance. It exposes
  provider capabilities on demand without adding permanent Agent tools, and is
  suitable for task files and selection, trackers, peers, queue order, limits,
  tags, locations, rechecks, direct provider submissions, or batch task control.
allowed-tools: execute_command
---

# Downloader Operation

Use `scripts/mp-downloader.py`. The helper reads MoviePilot's local downloader
configuration and credentials itself. Never request, print, or pass a host,
username, password, API key, Cookie, or arbitrary URL.

## Boundary

- Prefer `moviepilot-api` for ordinary MoviePilot acquisition workflows,
  especially site search, `download.add`, subscriptions, transfer, history, and
  canonical library checks.
- Use this Skill for downloader-native inspection, diagnosis, advanced task
  properties, and an explicit request to operate the provider directly.
- `tasks.add.direct` bypasses MoviePilot download history, site Cookie handling,
  path selection, duplicate checks, and transfer orchestration. Use it only when
  the user explicitly wants direct provider submission.
- Paths passed to `tasks.location.set` and `tasks.add.direct` are downloader-side
  paths, not MoviePilot storage paths.

## Instance And Provider Discovery

### Fast path: call directly

Do not routinely call `instances` or `capabilities` before an operation. This
Skill already contains the full action contract, and the helper performs
instance resolution, provider support checks, complete argument validation, and
the action in one `call` invocation.

- If the user or prior context provides the exact client name, pass it with
  `--client` and call the action immediately.
- If no client name is known, omit `--client`. The helper automatically uses the
  single default downloader, or the only enabled downloader.
- If multiple clients remain ambiguous, the failed call lists every valid
  client name. Reuse that list for the next direct call; do not add a separate
  `instances` call unless the user explicitly asks to inspect instances.
- Do not probe an action with empty or guessed arguments. Compose the complete
  JSON object from the contract below before calling.

The helper rejects unknown fields and reports all detectable argument errors in
one response before connecting to the provider, so correct every reported field
together instead of retrying one field at a time.

### Optional discovery

List configured instances without secrets:

```bash
python skills/downloader-operation/scripts/mp-downloader.py instances
```

List the actions supported by all providers or one configured client:

```bash
python skills/downloader-operation/scripts/mp-downloader.py capabilities
python skills/downloader-operation/scripts/mp-downloader.py capabilities --client "main-qb"
```

The complete action and argument contract is documented below. Use
`capabilities` only to confirm which documented actions a configured provider
supports. For a compact machine-readable copy of one action's same contract:

```bash
python skills/downloader-operation/scripts/mp-downloader.py capabilities \
  --client "main-qb" \
  --action tasks.properties.set
```

Do not inspect the helper source to discover arguments and do not guess a
provider-specific action. `capabilities` is optional and should be used only
when the configured provider itself is unknown or support must be diagnosed.

## Call Shape

```bash
python skills/downloader-operation/scripts/mp-downloader.py call \
  --client "main-qb" \
  --action tasks.list \
  --arguments '{"status":"downloading","limit":20}'
```

The `--arguments` value must be one JSON object. Large reads are paged with
`offset` and `limit`; the default limit is 50 and the maximum is 200.

## External MCP Contract

External MCP clients do not receive this `SKILL.md` and cannot use the hidden
`execute_command` tool. MoviePilot therefore exposes a separate admin-only MCP
tool named `downloader_operation`. Its `tools/list` `inputSchema` contains one
`oneOf` branch for every action below, including the function description,
supported providers, effect, field types, required/default values, enums, and
cross-field rules. The external client should select the matching branch and
make one `tools/call`; it does not need to call a discovery tool first.

MCP call arguments use the same contract without shell quoting:

```json
{
  "client": "main-qb",
  "action": "tasks.properties.set",
  "arguments": {
    "task_id": "exact-provider-hash",
    "download_limit": 2048,
    "upload_limit": 512
  }
}
```

`client` may be omitted for the default or only enabled downloader. If multiple
instances remain ambiguous, the result lists the valid client names.

## Complete Action Contract

In the tables below, `*` means required. Every listed field belongs inside the
single `--arguments` JSON object. Do not send fields that are not listed.

Shared rules:

- Task batch actions require exactly one of `task_id:string` or
  `task_ids:string[]`.
- Paged reads accept `offset:integer=0` and `limit:integer=50`; `offset` must be
  non-negative and `limit` is clamped to `1..200`.
- Speed values are numbers in `KB/s`. A value of `0` means unlimited.
- Task IDs, file indexes, tags, tracker URLs, and provider paths must come from
  the selected downloader or the user's explicit input; never invent them.

### Task reads

| Action | Function and providers | `--arguments` fields |
|---|---|---|
| `tasks.list` | List/filter tasks; all | `task_id:string` or `task_ids:string[]`; `status:string`; `tags:string\|string[]`; `offset:integer=0`; `limit:integer=50` |
| `tasks.files` | List files and priorities for one task; all | `task_id*:string`; `offset:integer=0`; `limit:integer=50` |
| `tasks.trackers` | List tracker URLs; qBittorrent, Transmission | `task_id*:string` |
| `tasks.tags.get` | Read tags/labels for one task; all | `task_id*:string` |
| `tasks.peers` | Read peer synchronization data; qBittorrent | `task_id*:string` |

### Task control

| Action | Function and effect | `--arguments` fields |
|---|---|---|
| `tasks.start` | Start/resume tasks; reversible write | exactly one of `task_id:string`, `task_ids:string[]` |
| `tasks.stop` | Pause tasks; reversible write | exactly one of `task_id:string`, `task_ids:string[]` |
| `tasks.recheck` | Force data verification; external side effect | exactly one of `task_id:string`, `task_ids:string[]` |
| `tasks.reannounce` | Force tracker reannounce; qBittorrent/Transmission, external side effect | exactly one of `task_id:string`, `task_ids:string[]` |
| `tasks.queue.move` | Move queue position; qBittorrent/Transmission, reversible write | exactly one of `task_id:string`, `task_ids:string[]`; `position*:string` = `top\|up\|down\|bottom` |
| `tasks.force_start.set` | Toggle force-start; qBittorrent, reversible write | exactly one of `task_id:string`, `task_ids:string[]`; `enabled*:boolean` |
| `tasks.files.selection.set` | Select files within one task; reversible write | `task_id*:string`; `wanted_file_ids:integer[]`; `unwanted_file_ids:integer[]`; at least one list, with no overlapping index |
| `tasks.properties.set` | Set per-task limits; reversible write | `task_id*:string`; at least one of `upload_limit:number`, `download_limit:number`, `ratio_limit:number`, `seeding_time_limit:integer` minutes. rTorrent supports only speed fields |
| `tasks.location.set` | Move/retarget data to a downloader-side path; external side effect | `task_id*:string`; `location*:string` |
| `tasks.category.set` | Set a non-empty category; qBittorrent, reversible write | `task_id*:string`; `category*:string` |
| `tasks.tags.set` | Set/add tags or labels; reversible write | exactly one of `task_id:string`, `task_ids:string[]`; `tags*:string[]` |
| `tasks.trackers.update` | Add/replace trackers; qBittorrent/Transmission, reversible write | `task_id*:string`; `trackers*:string[]` of URLs |
| `tasks.delete` | Delete tasks and optionally data; destructive write | exactly one of `task_id:string`, `task_ids:string[]`; `delete_files:boolean=false` |
| `tasks.add.direct` | Submit directly to provider, bypassing MoviePilot orchestration; external side effect | `content*:string` magnet/URL/path; `torrent_file:boolean=false`; `paused:boolean=false`; `download_dir:string`; `tags:string[]`; `category:string` (qBittorrent only) |

### Session operations

| Action | Function and providers | `--arguments` fields |
|---|---|---|
| `session.stats` | Read transfer/session statistics; all | none (`{}`) |
| `session.speed_limits.get` | Read global download/upload limits; qBittorrent, Transmission | none (`{}`) |
| `session.speed_limits.set` | Set global limits; qBittorrent, Transmission | at least one of `download_limit:number`, `upload_limit:number`; use explicit `0` to clear a limit |
| `session.details` | Read Transmission session configuration/capacity; Transmission | none (`{}`) |
| `session.content_layout` | Read default torrent content layout; qBittorrent | none (`{}`) |

Examples:

```bash
# Read one task's files.
python skills/downloader-operation/scripts/mp-downloader.py call \
  --client "main-qb" \
  --action tasks.files \
  --arguments '{"task_id":"exact-provider-hash","offset":0,"limit":50}'

# Limit one task to 2048 KB/s download and 512 KB/s upload.
python skills/downloader-operation/scripts/mp-downloader.py call \
  --client "main-qb" \
  --action tasks.properties.set \
  --arguments '{"task_id":"exact-provider-hash","download_limit":2048,"upload_limit":512}'
```

Before deleting data, confirm the exact client, tasks, and `delete_files=true`.
Before a direct add, confirm the exact magnet/URL or local torrent file, client,
paused state, provider path, tags, and category.

For `tasks.files.selection.set`, pass provider file indexes from `tasks.files`
through `wanted_file_ids` and/or `unwanted_file_ids`; never infer indexes from
filenames alone. `session.details` is Transmission-only and
`session.content_layout` is qBittorrent-only.

## Verification

After a write, query the smallest relevant state: `tasks.list` for status and
properties, `tasks.files` for file priority, `tasks.trackers` for trackers, or
`session.speed_limits.get` for global limits. Report unsupported provider
capabilities explicitly instead of falling back to raw HTTP or arbitrary SDK
method calls.
