---
name: mediaserver-operation
version: 3
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

This is the complete Media Server Operation action contract. It comes directly from the script `ACTIONS` registry and matches the external MCP `tools/list` oneOf branches.
A field name ending in `*` is required. Put every action parameter in the `arguments` object.

| action | Purpose and argument summary |
| :--- | :--- |
| `activity.backdrops` | Read recent provider backdrop images.; arguments: `limit`, `remote` |
| `activity.latest` | Read recently added provider items.; arguments: `limit`, `username` |
| `activity.resume` | Read in-progress/resumable provider items.; arguments: `limit`, `username` |
| `capabilities.list` | List supported media-server actions and their complete argument contracts.; arguments: `action_name` |
| `instances.list` | List configured media-server instances without connection secrets.; no arguments |
| `items.count` | Count items below one library or parent.; arguments: `parent` |
| `items.detail` | Read one provider item by native ID.; arguments: `item_id*` |
| `items.list` | Page items below one library or parent.; arguments: `parent`, `offset`, `limit` |
| `items.movies.search` | Search provider-native movie items by title and optional year.; arguments: `title*`, `year` |
| `items.music.search` | Search provider-native music by title, artist, or album.; arguments: `title`, `artist`, `album` |
| `items.season_episodes` | Read native episode coverage for one series and optional season.; arguments: `item_id`, `title`, `year`, `season` |
| `libraries.list` | List visible provider libraries.; arguments: `hidden`, `username` |
| `library.scan` | Trigger a provider library scan.; arguments: `scan_mode` |
| `metadata.refresh` | Refresh provider metadata for mapped items.; arguments: `items*` |
| `playback.sessions` | Read active playback sessions.; no arguments |
| `playback.url` | Build the provider play URL for one item.; arguments: `item_id*` |
| `server.statistics` | Read media counts and provider statistics.; no arguments |
| `server.user.library_folders` | Read the current user's visible library folders.; no arguments |
| `server.users.count` | Read provider user count.; no arguments |

### `activity.backdrops`
Read recent provider backdrop images. Effect: `safe_read`. Providers: `ugreen, trimemedia`.
- `limit` (integer; default `50`): Number of items to return, from 1 to 200.
- `remote` (boolean; default `False`): Return provider URLs that are remotely accessible.

### `activity.latest`
Read recently added provider items. Effect: `safe_read`. Providers: `emby, jellyfin, plex, zspace, ugreen, trimemedia, navidrome`.
- `limit` (integer; default `50`): Number of items to return, from 1 to 200.
- `username` (string): Read for this username; supported by Emby, Jellyfin, and ZSpace.

### `activity.resume`
Read in-progress/resumable provider items. Effect: `safe_read`. Providers: `emby, jellyfin, plex, zspace, ugreen, trimemedia, navidrome`.
- `limit` (integer; default `50`): Number of items to return, from 1 to 200.
- `username` (string): Read for this username; supported by Emby, Jellyfin, and ZSpace.

### `capabilities.list`
List supported media-server actions and their complete argument contracts. Effect: `safe_read`. Providers: `emby, jellyfin, plex, zspace, ugreen, trimemedia, navidrome`.
- `action_name` (string): Optional exact action name used to return one capability contract.

### `instances.list`
List configured media-server instances without connection secrets. Effect: `safe_read`. Providers: `emby, jellyfin, plex, zspace, ugreen, trimemedia, navidrome`.
- `arguments`: `{}`

### `items.count`
Count items below one library or parent. Effect: `safe_read`. Providers: `emby, jellyfin, plex, zspace, ugreen, trimemedia, navidrome`.
- `parent` (string|integer): Library or parent item ID; Navidrome may omit it and use music.
- Rule: parent is required except for Navidrome, which defaults to music.

### `items.detail`
Read one provider item by native ID. Effect: `safe_read`. Providers: `emby, jellyfin, plex, zspace, ugreen, trimemedia, navidrome`.
- `item_id*` (string): Provider-native item ID returned by the selected media server.

### `items.list`
Page items below one library or parent. Effect: `safe_read`. Providers: `emby, jellyfin, plex, zspace, ugreen, trimemedia, navidrome`.
- `parent` (string|integer): Library or parent item ID; Navidrome may omit it and use music.
- `offset` (integer; default `0`): Zero-based list offset.
- `limit` (integer; default `50`): Number of items to return, from 1 to 200.
- Rule: parent is required except for Navidrome, which ignores it.

### `items.movies.search`
Search provider-native movie items by title and optional year. Effect: `safe_read`. Providers: `emby, jellyfin, plex, zspace, ugreen, trimemedia`.
- `title*` (string): Movie title.
- `year` (string|integer): Optional release year.

### `items.music.search`
Search provider-native music by title, artist, or album. Effect: `safe_read`. Providers: `emby, jellyfin, plex, zspace, ugreen, navidrome`.
- `title` (string): Track, album, or music-item title.
- `artist` (string): Artist name.
- `album` (string): Album name; provide title, artist, or album.
- Rule: Provide at least one of title, artist, and album.

### `items.season_episodes`
Read native episode coverage for one series and optional season. Effect: `safe_read`. Providers: `emby, jellyfin, plex, zspace, ugreen, trimemedia`.
- `item_id` (string): Provider-native item ID returned by the selected media server.
- `title` (string): Series title; provide it or item_id.
- `year` (string|integer): Optional premiere year.
- `season` (integer): Optional season number.
- Rule: Provide at least one of item_id and title.

### `libraries.list`
List visible provider libraries. Effect: `safe_read`. Providers: `emby, jellyfin, plex, zspace, ugreen, trimemedia, navidrome`.
- `hidden` (boolean; default `False`): Return only libraries configured for synchronization.
- `username` (string): Read libraries visible to this username; supported by Emby, Jellyfin, and ZSpace.

### `library.scan`
Trigger a provider library scan. Effect: `external_side_effect`. Providers: `emby, jellyfin, plex, zspace, ugreen, trimemedia, navidrome`.
- `scan_mode` (string|integer): UGREEN-native scan mode; omit it for every other provider.

### `metadata.refresh`
Refresh provider metadata for mapped items. Effect: `external_side_effect`. Providers: `emby, plex, zspace, ugreen, trimemedia`.
- `items*` (object[]): Items to refresh. Each item supports title:string, year:string|integer, type using the exact MoviePilot media-type value, category:string, and target_path:string.

### `playback.sessions`
Read active playback sessions. Effect: `safe_read`. Providers: `emby, jellyfin, plex`.
- `arguments`: `{}`

### `playback.url`
Build the provider play URL for one item. Effect: `safe_read`. Providers: `emby, jellyfin, plex, zspace, ugreen, trimemedia, navidrome`.
- `item_id*` (string): Provider-native item ID returned by the selected media server.

### `server.statistics`
Read media counts and provider statistics. Effect: `safe_read`. Providers: `emby, jellyfin, plex, zspace, ugreen, trimemedia, navidrome`.
- `arguments`: `{}`

### `server.user.library_folders`
Read the current user's visible library folders. Effect: `safe_read`. Providers: `emby, jellyfin, zspace`.
- `arguments`: `{}`

### `server.users.count`
Read provider user count. Effect: `safe_read`. Providers: `emby, jellyfin, zspace, ugreen, trimemedia, navidrome`.
- `arguments`: `{}`

## Safety And Verification

- Before `library.scan`, confirm the exact server and scan scope/mode.
- Before `metadata.refresh`, confirm the exact item list because providers may
  perform a broad library refresh when a precise item cannot be mapped.
- After a scan or refresh, verify with `activity.latest`, `items.detail`, or the
  smallest relevant library query.
- If a provider does not advertise an action, report it as unsupported. Never
  fall back to raw HTTP, arbitrary SDK methods, or credentials copied from
  MoviePilot settings.
