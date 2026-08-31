---
name: downloader-operation
version: 3
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

This is the complete Downloader Operation action contract. It comes directly from the script `ACTIONS` registry and matches the external MCP `tools/list` oneOf branches.
A field name ending in `*` is required. Put every action parameter in the `arguments` object.

| action | Purpose and argument summary |
| :--- | :--- |
| `capabilities.list` | List supported downloader actions and their complete argument contracts.; arguments: `action_name` |
| `instances.list` | List configured downloader instances without connection secrets.; no arguments |
| `session.content_layout` | Read qBittorrent's default torrent content layout.; no arguments |
| `session.details` | Read Transmission session configuration and capacity details.; no arguments |
| `session.speed_limits.get` | Read global speed limits.; no arguments |
| `session.speed_limits.set` | Set global speed limits in KB/s.; arguments: `download_limit`, `upload_limit` |
| `session.stats` | Read provider transfer/session statistics.; no arguments |
| `tasks.add.direct` | Submit a magnet, URL, or local torrent file directly to the provider.; arguments: `content*`, `torrent_file`, `paused`, `download_dir`, `tags`, `category` |
| `tasks.category.set` | Set qBittorrent category.; arguments: `task_id*`, `category*` |
| `tasks.delete` | Delete tasks and optionally their data.; arguments: `task_id`, `task_ids`, `delete_files` |
| `tasks.files` | List files and priorities for one task.; arguments: `task_id*`, `offset`, `limit` |
| `tasks.files.selection.set` | Select wanted and unwanted files within one task.; arguments: `task_id*`, `wanted_file_ids`, `unwanted_file_ids` |
| `tasks.force_start.set` | Enable or disable qBittorrent force-start for tasks.; arguments: `task_id`, `task_ids`, `enabled*` |
| `tasks.list` | List and filter downloader tasks.; arguments: `task_id`, `task_ids`, `status`, `tags`, `offset`, `limit` |
| `tasks.location.set` | Move or retarget one task to a provider-side path.; arguments: `task_id*`, `location*` |
| `tasks.peers` | Read qBittorrent peer synchronization data.; arguments: `task_id*` |
| `tasks.properties.set` | Set task speed, ratio, or seeding-time limits.; arguments: `task_id*`, `upload_limit`, `download_limit`, `ratio_limit`, `seeding_time_limit` |
| `tasks.queue.move` | Move tasks to top, up, down, or bottom of the queue.; arguments: `task_id`, `task_ids`, `position*` |
| `tasks.reannounce` | Force tracker reannounce.; arguments: `task_id`, `task_ids` |
| `tasks.recheck` | Force data verification for tasks.; arguments: `task_id`, `task_ids` |
| `tasks.start` | Start or resume one or more tasks.; arguments: `task_id`, `task_ids` |
| `tasks.stop` | Pause one or more tasks.; arguments: `task_id`, `task_ids` |
| `tasks.tags.get` | Read task tags or labels.; arguments: `task_id*` |
| `tasks.tags.set` | Set or add task tags/labels.; arguments: `task_id`, `task_ids`, `tags*` |
| `tasks.trackers` | List trackers for one task.; arguments: `task_id*` |
| `tasks.trackers.update` | Add or replace task trackers.; arguments: `task_id*`, `trackers*` |

### `capabilities.list`
List supported downloader actions and their complete argument contracts. Effect: `safe_read`. Providers: `qbittorrent, transmission, rtorrent`.
- `action_name` (string): Optional exact action name used to return one capability contract.

### `instances.list`
List configured downloader instances without connection secrets. Effect: `safe_read`. Providers: `qbittorrent, transmission, rtorrent`.
- `arguments`: `{}`

### `session.content_layout`
Read qBittorrent's default torrent content layout. Effect: `safe_read`. Providers: `qbittorrent`.
- `arguments`: `{}`

### `session.details`
Read Transmission session configuration and capacity details. Effect: `safe_read`. Providers: `transmission`.
- `arguments`: `{}`

### `session.speed_limits.get`
Read global speed limits. Effect: `safe_read`. Providers: `qbittorrent, transmission`.
- `arguments`: `{}`

### `session.speed_limits.set`
Set global speed limits in KB/s. Effect: `reversible_write`. Providers: `qbittorrent, transmission`.
- `download_limit` (number): Global download limit in KB/s; 0 or omission means unlimited.
- `upload_limit` (number): Global upload limit in KB/s; 0 or omission means unlimited.

### `session.stats`
Read provider transfer/session statistics. Effect: `safe_read`. Providers: `qbittorrent, transmission, rtorrent`.
- `arguments`: `{}`

### `tasks.add.direct`
Submit a magnet, URL, or local torrent file directly to the provider. Effect: `external_side_effect`. Providers: `qbittorrent, transmission, rtorrent`.
- `content*` (string): Magnet URI, torrent URL, or a local torrent path when torrent_file=true.
- `torrent_file` (boolean; default `False`): Interpret content as a local torrent-file path.
- `paused` (boolean; default `False`): Add the task in a paused state.
- `download_dir` (string): Provider-side save path.
- `tags` (string[]): Tags to assign to the new task.
- `category` (string): qBittorrent category; ignored by other providers.

### `tasks.category.set`
Set qBittorrent category. Effect: `reversible_write`. Providers: `qbittorrent`.
- `task_id*` (string): One provider-native task hash or ID.
- `category*` (string): Non-empty qBittorrent category name.

### `tasks.delete`
Delete tasks and optionally their data. Effect: `destructive_write`. Providers: `qbittorrent, transmission, rtorrent`.
- `task_id` (string): One provider-native task hash or ID.
- `task_ids` (string[]): Multiple provider-native task hashes or IDs; mutually exclusive with task_id.
- `delete_files` (boolean; default `False`): Also permanently delete the task data files.
- Rule: Provide exactly one of task_id and task_ids.

### `tasks.files`
List files and priorities for one task. Effect: `safe_read`. Providers: `qbittorrent, transmission, rtorrent`.
- `task_id*` (string): One provider-native task hash or ID.
- `offset` (integer; default `0`): Zero-based list offset.
- `limit` (integer; default `50`): Number of items to return, from 1 to 200.

### `tasks.files.selection.set`
Select wanted and unwanted files within one task. Effect: `reversible_write`. Providers: `qbittorrent, transmission, rtorrent`.
- `task_id*` (string): One provider-native task hash or ID.
- `wanted_file_ids` (integer[]): Provider file indexes to download; provide this or unwanted_file_ids.
- `unwanted_file_ids` (integer[]): Provider file indexes to skip; provide this or wanted_file_ids.
- Rule: Provide wanted_file_ids or unwanted_file_ids, and never place one index in both lists.

### `tasks.force_start.set`
Enable or disable qBittorrent force-start for tasks. Effect: `reversible_write`. Providers: `qbittorrent`.
- `task_id` (string): One provider-native task hash or ID.
- `task_ids` (string[]): Multiple provider-native task hashes or IDs; mutually exclusive with task_id.
- `enabled*` (boolean): Whether force-start is enabled.
- Rule: Provide exactly one of task_id and task_ids.

### `tasks.list`
List and filter downloader tasks. Effect: `safe_read`. Providers: `qbittorrent, transmission, rtorrent`.
- `task_id` (string): One provider-native task hash or ID.
- `task_ids` (string[]): Multiple provider-native task hashes or IDs; mutually exclusive with task_id.
- `status` (string): Filter by the provider-native task status.
- `tags` (string|string[]): Return only tasks that contain all specified tags.
- `offset` (integer; default `0`): Zero-based list offset.
- `limit` (integer; default `50`): Number of items to return, from 1 to 200.

### `tasks.location.set`
Move or retarget one task to a provider-side path. Effect: `external_side_effect`. Providers: `qbittorrent, transmission, rtorrent`.
- `task_id*` (string): One provider-native task hash or ID.
- `location*` (string): New provider-side save path.

### `tasks.peers`
Read qBittorrent peer synchronization data. Effect: `safe_read`. Providers: `qbittorrent`.
- `task_id*` (string): One provider-native task hash or ID.

### `tasks.properties.set`
Set task speed, ratio, or seeding-time limits. Effect: `reversible_write`. Providers: `qbittorrent, transmission, rtorrent`.
- `task_id*` (string): One provider-native task hash or ID.
- `upload_limit` (number): Upload limit in KB/s; 0 means unlimited.
- `download_limit` (number): Download limit in KB/s; 0 means unlimited.
- `ratio_limit` (number): Share-ratio limit; unsupported by rTorrent.
- `seeding_time_limit` (integer): Seeding-time limit in minutes; unsupported by rTorrent.

### `tasks.queue.move`
Move tasks to top, up, down, or bottom of the queue. Effect: `reversible_write`. Providers: `qbittorrent, transmission`.
- `task_id` (string): One provider-native task hash or ID.
- `task_ids` (string[]): Multiple provider-native task hashes or IDs; mutually exclusive with task_id.
- `position*` (string; allowed values `top,up,down,bottom`): Target queue position.
- Rule: Provide exactly one of task_id and task_ids.

### `tasks.reannounce`
Force tracker reannounce. Effect: `external_side_effect`. Providers: `qbittorrent, transmission`.
- `task_id` (string): One provider-native task hash or ID.
- `task_ids` (string[]): Multiple provider-native task hashes or IDs; mutually exclusive with task_id.
- Rule: Provide exactly one of task_id and task_ids.

### `tasks.recheck`
Force data verification for tasks. Effect: `external_side_effect`. Providers: `qbittorrent, transmission, rtorrent`.
- `task_id` (string): One provider-native task hash or ID.
- `task_ids` (string[]): Multiple provider-native task hashes or IDs; mutually exclusive with task_id.
- Rule: Provide exactly one of task_id and task_ids.

### `tasks.start`
Start or resume one or more tasks. Effect: `reversible_write`. Providers: `qbittorrent, transmission, rtorrent`.
- `task_id` (string): One provider-native task hash or ID.
- `task_ids` (string[]): Multiple provider-native task hashes or IDs; mutually exclusive with task_id.
- Rule: Provide exactly one of task_id and task_ids.

### `tasks.stop`
Pause one or more tasks. Effect: `reversible_write`. Providers: `qbittorrent, transmission, rtorrent`.
- `task_id` (string): One provider-native task hash or ID.
- `task_ids` (string[]): Multiple provider-native task hashes or IDs; mutually exclusive with task_id.
- Rule: Provide exactly one of task_id and task_ids.

### `tasks.tags.get`
Read task tags or labels. Effect: `safe_read`. Providers: `qbittorrent, transmission, rtorrent`.
- `task_id*` (string): One provider-native task hash or ID.

### `tasks.tags.set`
Set or add task tags/labels. Effect: `reversible_write`. Providers: `qbittorrent, transmission, rtorrent`.
- `task_id` (string): One provider-native task hash or ID.
- `task_ids` (string[]): Multiple provider-native task hashes or IDs; mutually exclusive with task_id.
- `tags*` (string[]): Tags or labels to set or add.
- Rule: Provide exactly one of task_id and task_ids.

### `tasks.trackers`
List trackers for one task. Effect: `safe_read`. Providers: `qbittorrent, transmission`.
- `task_id*` (string): One provider-native task hash or ID.

### `tasks.trackers.update`
Add or replace task trackers. Effect: `reversible_write`. Providers: `qbittorrent, transmission`.
- `task_id*` (string): One provider-native task hash or ID.
- `trackers*` (string[]): Tracker URL list.

## Verification

After a write, query the smallest relevant state: `tasks.list` for status and
properties, `tasks.files` for file priority, `tasks.trackers` for trackers, or
`session.speed_limits.get` for global limits. Report unsupported provider
capabilities explicitly instead of falling back to raw HTTP or arbitrary SDK
method calls.
