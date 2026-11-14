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

Run the bundled copy from the MoviePilot program directory with its project
runtime, for example:

```bash
cd <MOVIEPILOT_ROOT>
python skills/mediaserver-operation/scripts/mp-mediaserver.py instances
```

The Agent command environment routes `python` to the project-specific `moviepilot-python`
entry when available and falls back to the project or Docker `VENV_PATH` Python otherwise.
Media-server connections may depend on macOS local-network permissions, so use the provided
`python` command for every operation.

The runtime sets `MOVIEPILOT_ROOT` for copied skills. If you run a copied
script directly from `<CONFIG_PATH>/agent/skills/`, set that variable to the
program directory first; otherwise the helper returns a clear relocation
error. Do not use a system `python3` when it does not meet MoviePilot's
required Python version.

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

The default schema mode is `full`. Clients explicitly sending
`X-MCP-Schema-Mode: compact` receive a best-effort projection for oversized
schemas, without action-specific branches. Use this contract or the full
`GET /api/v1/mcp/tools/{tool_name}/schema` response for exact arguments;
call-time validation and authorization are unchanged.

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
| `activity.backdrops` | 读取服务近期媒体的背景图。; arguments: `limit`, `remote` |
| `activity.latest` | 读取服务最近添加的媒体条目。; arguments: `limit`, `username`, `query` |
| `activity.resume` | 读取服务中正在观看或可继续播放的媒体条目。; arguments: `limit`, `username`, `query` |
| `capabilities.list` | 列出支持的媒体服务器动作及完整参数契约。; arguments: `action_name` |
| `instances.list` | 列出已配置的媒体服务器实例，不返回连接凭据。; no arguments |
| `items.count` | 统计指定媒体库或父条目下的内容数量。; arguments: `parent` |
| `items.detail` | 按服务原生标识读取单个媒体条目。; arguments: `item_id*`, `query` |
| `items.list` | 分页读取指定媒体库或父条目下的内容。; arguments: `parent`, `offset`, `limit`, `query` |
| `items.movies.search` | 按标题及可选年份搜索服务中的电影条目。; arguments: `title*`, `year`, `query` |
| `items.music.search` | 按标题、艺术家或专辑搜索服务中的音乐。; arguments: `title`, `artist`, `album`, `query` |
| `items.season_episodes` | 读取指定剧集及可选季的现有分集信息。; arguments: `item_id`, `title`, `year`, `season`, `query` |
| `libraries.list` | 列出可见媒体库。; arguments: `hidden`, `username`, `query` |
| `library.scan` | 触发媒体库扫描。; arguments: `scan_mode` |
| `metadata.refresh` | 刷新匹配条目的服务端元数据。; arguments: `items*` |
| `playback.sessions` | 读取正在进行的播放会话。; no arguments |
| `playback.url` | 生成单个条目的服务播放链接。; arguments: `item_id*` |
| `server.statistics` | 读取媒体数量及服务统计。; no arguments |
| `server.user.library_folders` | 读取当前用户可见的媒体库目录。; no arguments |
| `server.users.count` | 读取服务用户数量。; no arguments |

### `activity.backdrops`
读取服务近期媒体的背景图。 Effect: `safe_read`. Providers: `ugreen, trimemedia, mediavault`.
- `limit` (integer; default `50`): 返回条目数量，范围为 1 至 200。
- `remote` (boolean; default `False`): 是否返回可远程访问的服务 URL。

### `activity.latest`
读取服务最近添加的媒体条目。 Effect: `safe_read`. Providers: `emby, jellyfin, plex, zspace, ugreen, trimemedia, navidrome, mediavault`.
- `limit` (integer; default `50`): 返回条目数量，范围为 1 至 200。
- `username` (string): 按该用户名读取；Emby、Jellyfin 和 ZSpace 支持。
- `query` (object): 统一内容投影，如 {"fields":["identity","path"]}；省略或 fields=null 保留原内容，fields=[] 仅取基础信息。

### `activity.resume`
读取服务中正在观看或可继续播放的媒体条目。 Effect: `safe_read`. Providers: `emby, jellyfin, plex, zspace, ugreen, trimemedia, navidrome, mediavault`.
- `limit` (integer; default `50`): 返回条目数量，范围为 1 至 200。
- `username` (string): 按该用户名读取；Emby、Jellyfin 和 ZSpace 支持。
- `query` (object): 统一内容投影，如 {"fields":["identity","path"]}；省略或 fields=null 保留原内容，fields=[] 仅取基础信息。

### `capabilities.list`
列出支持的媒体服务器动作及完整参数契约。 Effect: `safe_read`. Providers: `emby, jellyfin, plex, zspace, ugreen, trimemedia, navidrome, mediavault`.
- `action_name` (string): 可选的精确动作名称，填写时只返回该动作的能力契约。

### `instances.list`
列出已配置的媒体服务器实例，不返回连接凭据。 Effect: `safe_read`. Providers: `emby, jellyfin, plex, zspace, ugreen, trimemedia, navidrome, mediavault`.
- `arguments`: `{}`

### `items.count`
统计指定媒体库或父条目下的内容数量。 Effect: `safe_read`. Providers: `emby, jellyfin, plex, zspace, ugreen, trimemedia, navidrome, mediavault`.
- `parent` (string|integer): 媒体库或父条目标识；Navidrome 可省略并使用 music。
- Rule: 除 Navidrome 外必须提供 parent；Navidrome 默认使用 music。

### `items.detail`
按服务原生标识读取单个媒体条目。 Effect: `safe_read`. Providers: `emby, jellyfin, plex, zspace, ugreen, trimemedia, navidrome, mediavault`.
- `item_id*` (string): 所选媒体服务器返回的原生条目标识。
- `query` (object): 统一内容投影，如 {"fields":["identity","path"]}；省略或 fields=null 保留原内容，fields=[] 仅取基础信息。

### `items.list`
分页读取指定媒体库或父条目下的内容。 Effect: `safe_read`. Providers: `emby, jellyfin, plex, zspace, ugreen, trimemedia, navidrome, mediavault`.
- `parent` (string|integer): 媒体库或父条目标识；Navidrome 可省略并使用 music。
- `offset` (integer; default `0`): 列表偏移量，从 0 开始。
- `limit` (integer; default `50`): 返回条目数量，范围为 1 至 200。
- `query` (object): 统一内容投影，如 {"fields":["identity","path"]}；省略或 fields=null 保留原内容，fields=[] 仅取基础信息。
- Rule: 除 Navidrome 外必须提供 parent；Navidrome 忽略该参数。

### `items.movies.search`
按标题及可选年份搜索服务中的电影条目。 Effect: `safe_read`. Providers: `emby, jellyfin, plex, zspace, ugreen, trimemedia, mediavault`.
- `title*` (string): 电影标题。
- `year` (string|integer): 可选的发行年份。
- `query` (object): 统一内容投影，如 {"fields":["identity","path"]}；省略或 fields=null 保留原内容，fields=[] 仅取基础信息。

### `items.music.search`
按标题、艺术家或专辑搜索服务中的音乐。 Effect: `safe_read`. Providers: `emby, jellyfin, plex, zspace, ugreen, navidrome`.
- `title` (string): 曲目、专辑或音乐条目标题。
- `artist` (string): 艺术家名称。
- `album` (string): 专辑名称；须提供 title、artist 或 album。
- `query` (object): 统一内容投影，如 {"fields":["identity","path"]}；省略或 fields=null 保留原内容，fields=[] 仅取基础信息。
- Rule: Provide at least one of title, artist, and album.

### `items.season_episodes`
读取指定剧集及可选季的现有分集信息。 Effect: `safe_read`. Providers: `emby, jellyfin, plex, zspace, ugreen, trimemedia, mediavault`.
- `item_id` (string): 所选媒体服务器返回的原生条目标识。
- `title` (string): 剧集标题；须提供本字段或 item_id。
- `year` (string|integer): 可选的首播年份。
- `season` (integer): 可选的季编号。
- `query` (object): 统一内容投影，如 {"fields":["identity","path"]}；省略或 fields=null 保留原内容，fields=[] 仅取基础信息。
- Rule: Provide at least one of item_id and title.

### `libraries.list`
列出可见媒体库。 Effect: `safe_read`. Providers: `emby, jellyfin, plex, zspace, ugreen, trimemedia, navidrome, mediavault`.
- `hidden` (boolean; default `False`): 只返回已配置为同步的媒体库。
- `username` (string): 读取该用户名可见的媒体库；Emby、Jellyfin 和 ZSpace 支持。
- `query` (object): 统一内容投影，如 {"fields":["identity","path"]}；省略或 fields=null 保留原内容，fields=[] 仅取基础信息。

### `library.scan`
触发媒体库扫描。 Effect: `external_side_effect`. Providers: `emby, jellyfin, plex, zspace, ugreen, trimemedia, navidrome, mediavault`.
- `scan_mode` (string|integer): UGREEN 原生扫描模式；其他服务必须省略。

### `metadata.refresh`
刷新匹配条目的服务端元数据。 Effect: `external_side_effect`. Providers: `emby, plex, zspace, ugreen, trimemedia`.
- `items*` (object[]): 待刷新的条目。每项支持 title 字符串、year 字符串或整数、type 精确 MoviePilot 媒体类型值、category 字符串及 target_path 字符串。

### `playback.sessions`
读取正在进行的播放会话。 Effect: `safe_read`. Providers: `emby, jellyfin, plex`.
- `arguments`: `{}`

### `playback.url`
生成单个条目的服务播放链接。 Effect: `safe_read`. Providers: `emby, jellyfin, plex, zspace, ugreen, trimemedia, navidrome, mediavault`.
- `item_id*` (string): 所选媒体服务器返回的原生条目标识。

### `server.statistics`
读取媒体数量及服务统计。 Effect: `safe_read`. Providers: `emby, jellyfin, plex, zspace, ugreen, trimemedia, navidrome, mediavault`.
- `arguments`: `{}`

### `server.user.library_folders`
读取当前用户可见的媒体库目录。 Effect: `safe_read`. Providers: `emby, jellyfin, zspace`.
- `arguments`: `{}`

### `server.users.count`
读取服务用户数量。 Effect: `safe_read`. Providers: `emby, jellyfin, zspace, ugreen, trimemedia, navidrome, mediavault`.
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

## Query Content Projection

Emby, Jellyfin, Plex and Navidrome read actions accept an optional `query` object.
Omit it for ZSpace, TrimeMedia, UGREEN and Vyo, whose adapters have not adopted it.
Omitting `query` or setting `fields` to null preserves the existing content;
`fields: []` requests only the information required by the operation.

```json
{"parent":"movies","limit":50,"query":{"fields":["identity","path","year"]}}
```

The field groups are `library`, `original_title`, `year`, `identity`, `path`,
`music`, `user_state`, `images`, `counts` and `link`. Matching and scope checks
retain their required dependencies, including music album completeness.
Adapters translate supported options into remote request parameters. Servers may
still return default attributes when their protocol cannot select those fields.
See `docs/media-server-query.md` for the mappings and compatibility limits.
