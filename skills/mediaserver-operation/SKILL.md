---
name: mediaserver-operation
version: 2
description: >-
  Use this skill when the user asks to inspect, diagnose, or directly operate a
  configured Emby, Jellyfin, Plex, ZSpace, UGREEN, TrimeMedia, or Navidrome
  server. It discovers provider capabilities on demand and supports libraries,
  native movie/music search, episode coverage, recent media, resume state,
  playback sessions, statistics, scans, metadata refreshes, and provider play
  URLs without adding permanent tools.
allowed-tools: execute_command
---

# Media Server Operation

Use `scripts/mp-mediaserver.py`. The helper reads MoviePilot's local server
configuration and credentials itself. Never request, print, or pass a host,
username, password, API key, token, Cookie, or arbitrary URL.

## Boundary

- Keep `library.exists` in `moviepilot-api` for canonical duplicate checks. It
  aggregates configured servers and applies MoviePilot media identity and music
  matching rules.
- Use this Skill for server-native exploration, diagnostics, playback state,
  library browsing, scans, and metadata refreshes.
- A provider result is not automatically a MoviePilot transfer, subscription,
  or history fact. Use the appropriate MoviePilot API for those workflows.

## Instance And Provider Discovery

### Fast path: call directly

Do not routinely call `instances` or `capabilities` before an operation. This
Skill already contains the full action contract, and the helper performs
instance resolution, provider support checks, complete argument validation, and
the action in one `call` invocation.

- If the user or prior context provides the exact server name, pass it with
  `--server` and call the action immediately.
- If no server name is known, omit `--server`. The helper automatically uses the
  only enabled media server.
- If multiple servers remain ambiguous, the failed call lists every valid
  server name. Reuse that list for the next direct call; do not add a separate
  `instances` call unless the user explicitly asks to inspect instances.
- Do not probe an action with empty or guessed arguments. Compose the complete
  JSON object from the contract below before calling.

The helper rejects unknown fields and reports all detectable argument errors in
one response before connecting to the provider, so correct every reported field
together instead of retrying one field at a time.

### Optional discovery

```bash
python skills/mediaserver-operation/scripts/mp-mediaserver.py instances
python skills/mediaserver-operation/scripts/mp-mediaserver.py capabilities
python skills/mediaserver-operation/scripts/mp-mediaserver.py capabilities --server "living-room"
```

The complete action and argument contract is documented below. Use
`capabilities` only to confirm which documented actions a configured provider
supports. For a compact machine-readable copy of one action's same contract:

```bash
python skills/mediaserver-operation/scripts/mp-mediaserver.py capabilities \
  --server "living-room" \
  --action items.season_episodes
```

Do not inspect the helper source to discover arguments and do not guess a
provider-specific action. `capabilities` is optional and should be used only
when the configured provider itself is unknown or support must be diagnosed.

## Call Shape

```bash
python skills/mediaserver-operation/scripts/mp-mediaserver.py call \
  --server "living-room" \
  --action activity.latest \
  --arguments '{"limit":20}'
```

The `--arguments` value must be one JSON object. List reads default to 50 items
and cap at 200.

## External MCP Contract

External MCP clients do not receive this `SKILL.md` and cannot use the hidden
`execute_command` tool. MoviePilot therefore exposes a separate admin-only MCP
tool named `mediaserver_operation`. Its `tools/list` `inputSchema` contains one
`oneOf` branch for every action below, including the function description,
supported providers, effect, field types, required/default values, enums,
nested `metadata.refresh` item fields, and cross-field rules. The external
client should select the matching branch and make one `tools/call`; it does not
need to call a discovery tool first.

MCP call arguments use the same contract without shell quoting:

```json
{
  "server": "living-room",
  "action": "items.season_episodes",
  "arguments": {
    "item_id": "exact-series-id",
    "season": 2
  }
}
```

`server` may be omitted when only one media server is enabled. If multiple
instances remain ambiguous, the result lists the valid server names.

## Complete Action Contract

In the tables below, `*` means required. Every listed field belongs inside the
single `--arguments` JSON object. Do not send fields that are not listed.

Shared rules:

- Paged reads accept `offset:integer=0` where documented and
  `limit:integer=50`; `offset` must be non-negative and `limit` is clamped to
  `1..200`.
- `parent`, `item_id`, library IDs, and usernames are native to the selected
  server. Obtain them from that server's earlier response; never reuse IDs from
  another instance.
- All actions support only the providers shown by `capabilities`. The provider
  list below lets the Agent choose without inspecting source; query the selected
  instance only when provider support must be confirmed.

Provider abbreviations used below: all = Emby, Jellyfin, Plex, ZSpace, UGREEN,
TrimeMedia, and Navidrome.

### Server and library reads

| Action | Function and providers | `--arguments` fields |
|---|---|---|
| `server.statistics` | Read media counts/provider statistics; all | none (`{}`) |
| `server.users.count` | Read provider user count; Emby, Jellyfin, ZSpace, UGREEN, TrimeMedia, Navidrome | none (`{}`) |
| `server.user.library_folders` | Read current user's visible folders; Emby, Jellyfin, ZSpace | none (`{}`) |
| `libraries.list` | List visible libraries; all | `hidden:boolean=false` (true = configured sync scope only); `username:string` only for Emby/Jellyfin/ZSpace |
| `items.list` | Page items below a library/parent; all | `parent:string\|integer` required except Navidrome; `offset:integer=0`; `limit:integer=50` |
| `items.count` | Count items below a library/parent; all | `parent:string\|integer` required except Navidrome; omitted on Navidrome uses `music` |
| `items.detail` | Read one provider item; all | `item_id*:string` |

### Native search and activity

| Action | Function and providers | `--arguments` fields |
|---|---|---|
| `items.movies.search` | Search movies; Emby, Jellyfin, Plex, ZSpace, UGREEN, TrimeMedia | `title*:string`; `year:string\|integer` |
| `items.music.search` | Search music; Emby, Jellyfin, Plex, ZSpace, UGREEN, Navidrome | `title:string`; `artist:string`; `album:string`; at least one is required |
| `items.season_episodes` | Read existing episode coverage for a series; Emby, Jellyfin, Plex, ZSpace, UGREEN, TrimeMedia | `item_id:string`; `title:string`; at least one is required; optional `year:string\|integer`; `season:integer` |
| `activity.latest` | Read recently added items; all | `limit:integer=50`; `username:string` only for Emby/Jellyfin/ZSpace |
| `activity.resume` | Read in-progress/resumable items; all | `limit:integer=50`; `username:string` only for Emby/Jellyfin/ZSpace |
| `activity.backdrops` | Read recent backdrop URLs; UGREEN, TrimeMedia | `limit:integer=50`; `remote:boolean=false` |

### Playback and writes

| Action | Function, providers, and effect | `--arguments` fields |
|---|---|---|
| `playback.sessions` | Read active sessions; Emby, Jellyfin, Plex; safe read | none (`{}`) |
| `playback.url` | Build provider play URL; all; safe read | `item_id*:string` |
| `library.scan` | Trigger provider root-library scan; all; external side effect | `scan_mode:string\|integer` only for UGREEN; otherwise omit |
| `metadata.refresh` | Refresh metadata for mapped items; Emby, Plex, ZSpace, UGREEN, TrimeMedia; external side effect | `items*:object[]`; each object supports `title:string`, `year:string\|integer`, `type:string` (`电影\|电视剧\|音乐`), `category:string`, `target_path:string` |

Examples:

```bash
# List the first page below an exact library ID.
python skills/mediaserver-operation/scripts/mp-mediaserver.py call \
  --server "living-room" \
  --action items.list \
  --arguments '{"parent":"exact-library-id","offset":0,"limit":50}'

# Read season 2 coverage using an exact provider series ID.
python skills/mediaserver-operation/scripts/mp-mediaserver.py call \
  --server "living-room" \
  --action items.season_episodes \
  --arguments '{"item_id":"exact-series-id","season":2}'
```

Use the exact `server` and item/library IDs returned by earlier calls. Do not
invent IDs or reuse IDs across different server instances. `items.list` expects
`parent` for all video providers; Navidrome uses its single music library and
does not require a parent. `metadata.refresh` accepts an `items` array matching
MoviePilot's refresh item contract (`title`, `year`, `type`, `category`,
`target_path`).

Use `items.movies.search` for provider-native movie lookup,
`items.music.search` for a title/artist/album lookup, and
`items.season_episodes` when a direct server series ID or exact title is known.
These results describe one server only; use `library.exists` when the task needs
MoviePilot's canonical cross-server duplicate decision.

## Safety And Verification

- Before `library.scan`, confirm the exact server and scan scope/mode.
- Before `metadata.refresh`, confirm the exact item list because providers may
  perform a broad library refresh when a precise item cannot be mapped.
- After a scan or refresh, verify with `activity.latest`, `items.detail`, or the
  smallest relevant library query.
- If a provider does not advertise an action, report it as unsupported. Never
  fall back to raw HTTP, arbitrary SDK methods, or credentials copied from
  MoviePilot settings.
