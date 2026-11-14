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

Run the bundled copy from the MoviePilot program directory with its project
runtime, for example:

```bash
cd <MOVIEPILOT_ROOT>
python skills/downloader-operation/scripts/mp-downloader.py instances
```

The Agent command environment routes `python` to the project-specific `moviepilot-python`
entry when available and falls back to the project or Docker `VENV_PATH` Python otherwise.
Downloader connections may depend on macOS local-network permissions, so use the provided
`python` command for every operation.

The runtime sets `MOVIEPILOT_ROOT` for copied skills. If you run a copied
script directly from `<CONFIG_PATH>/agent/skills/`, set that variable to the
program directory first; otherwise the helper returns a clear relocation
error. Do not use a system `python3` when it does not meet MoviePilot's
required Python version.

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
- `tasks.location.set` is a raw provider operation and does not evaluate
  MoviePilot media categories. For an existing MoviePilot task, use the Web UI
  resource-category preview and confirmation flow when the target should come
  from MoviePilot's download history and resource-directory rules.

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

The default schema mode is `full`. Clients explicitly sending
`X-MCP-Schema-Mode: compact` receive a best-effort projection for oversized
schemas, without action-specific branches. Use this contract or the full
`GET /api/v1/mcp/tools/{tool_name}/schema` response for exact arguments;
call-time validation and authorization are unchanged.

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
| `capabilities.list` | 列出支持的下载器动作及完整参数契约。; arguments: `action_name` |
| `instances.list` | 列出已配置的下载器实例，不返回连接凭据。; no arguments |
| `session.content_layout` | 读取 qBittorrent 默认的种子内容布局。; no arguments |
| `session.details` | 读取 Transmission 会话配置与容量详情。; no arguments |
| `session.speed_limits.get` | 读取全局速率限制。; no arguments |
| `session.speed_limits.set` | 设置全局速率限制，单位为 KB/s。; arguments: `download_limit`, `upload_limit` |
| `session.stats` | 读取下载器传输或会话统计。; no arguments |
| `tasks.add.direct` | 直接向下载器提交磁力链接、URL 或本地种子文件。; arguments: `content*`, `torrent_file`, `paused`, `download_dir`, `tags`, `category` |
| `tasks.category.set` | 设置 qBittorrent 分类。; arguments: `task_id*`, `category*` |
| `tasks.delete` | 删除任务，并可选删除对应数据。; arguments: `task_id`, `task_ids`, `delete_files` |
| `tasks.files` | 列出单个任务的文件及下载优先级。; arguments: `task_id*`, `offset`, `limit` |
| `tasks.files.selection.set` | 设置单个任务中需要下载或跳过的文件。; arguments: `task_id*`, `wanted_file_ids`, `unwanted_file_ids` |
| `tasks.force_start.set` | 启用或关闭 qBittorrent 任务的强制启动。; arguments: `task_id`, `task_ids`, `enabled*` |
| `tasks.list` | 查询并筛选下载器任务。; arguments: `task_id`, `task_ids`, `status`, `tags`, `offset`, `limit` |
| `tasks.location.set` | 移动任务数据或将任务保存位置指向下载器端路径。; arguments: `task_id*`, `location*` |
| `tasks.peers` | 读取 qBittorrent 的连接节点同步数据。; arguments: `task_id*` |
| `tasks.properties.set` | 设置任务速率、分享率或做种时长限制。; arguments: `task_id*`, `upload_limit`, `download_limit`, `ratio_limit`, `seeding_time_limit` |
| `tasks.queue.move` | 将任务移至队首、上移、下移或移至队尾。; arguments: `task_id`, `task_ids`, `position*` |
| `tasks.reannounce` | 强制向 Tracker 重新汇报。; arguments: `task_id`, `task_ids` |
| `tasks.recheck` | 强制校验任务数据。; arguments: `task_id`, `task_ids` |
| `tasks.start` | 启动或恢复一个或多个任务。; arguments: `task_id`, `task_ids` |
| `tasks.stop` | 暂停一个或多个任务。; arguments: `task_id`, `task_ids` |
| `tasks.tags.get` | 读取任务标签。; arguments: `task_id*` |
| `tasks.tags.set` | 设置或添加任务标签。; arguments: `task_id`, `task_ids`, `tags*` |
| `tasks.trackers` | 列出单个任务的 Tracker。; arguments: `task_id*` |
| `tasks.trackers.update` | 添加或替换任务 Tracker。; arguments: `task_id*`, `trackers*` |

### `capabilities.list`
列出支持的下载器动作及完整参数契约。 Effect: `safe_read`. Providers: `qbittorrent, transmission, rtorrent`.
- `action_name` (string): 可选的精确动作名称，填写时只返回该动作的能力契约。

### `instances.list`
列出已配置的下载器实例，不返回连接凭据。 Effect: `safe_read`. Providers: `qbittorrent, transmission, rtorrent`.
- `arguments`: `{}`

### `session.content_layout`
读取 qBittorrent 默认的种子内容布局。 Effect: `safe_read`. Providers: `qbittorrent`.
- `arguments`: `{}`

### `session.details`
读取 Transmission 会话配置与容量详情。 Effect: `safe_read`. Providers: `transmission`.
- `arguments`: `{}`

### `session.speed_limits.get`
读取全局速率限制。 Effect: `safe_read`. Providers: `qbittorrent, transmission`.
- `arguments`: `{}`

### `session.speed_limits.set`
设置全局速率限制，单位为 KB/s。 Effect: `reversible_write`. Providers: `qbittorrent, transmission`.
- `download_limit` (number): 全局下载上限，单位为 KB/s；0 或省略表示不限速。
- `upload_limit` (number): 全局上传上限，单位为 KB/s；0 或省略表示不限速。

### `session.stats`
读取下载器传输或会话统计。 Effect: `safe_read`. Providers: `qbittorrent, transmission, rtorrent`.
- `arguments`: `{}`

### `tasks.add.direct`
直接向下载器提交磁力链接、URL 或本地种子文件。 Effect: `external_side_effect`. Providers: `qbittorrent, transmission, rtorrent`.
- `content*` (string): 磁力链接或种子 URL；torrent_file=true 时为本地种子路径。
- `torrent_file` (boolean; default `False`): 是否将 content 解释为本地种子文件路径。
- `paused` (boolean; default `False`): 以暂停状态添加任务。
- `download_dir` (string): 下载器端的保存路径。
- `tags` (string[]): 为新任务设置的标签。
- `category` (string): qBittorrent 分类；其他下载器忽略此参数。

### `tasks.category.set`
设置 qBittorrent 分类。 Effect: `reversible_write`. Providers: `qbittorrent`.
- `task_id*` (string): 下载器自身返回的单个任务哈希或标识。
- `category*` (string): 非空的 qBittorrent 分类名称。

### `tasks.delete`
删除任务，并可选删除对应数据。 Effect: `destructive_write`. Providers: `qbittorrent, transmission, rtorrent`.
- `task_id` (string): 下载器自身返回的单个任务哈希或标识。
- `task_ids` (string[]): 下载器自身返回的多个任务哈希或标识，与 task_id 互斥。
- `delete_files` (boolean; default `False`): 同时永久删除任务的数据文件。
- Rule: task_id 和 task_ids 必须且只能提供一项。

### `tasks.files`
列出单个任务的文件及下载优先级。 Effect: `safe_read`. Providers: `qbittorrent, transmission, rtorrent`.
- `task_id*` (string): 下载器自身返回的单个任务哈希或标识。
- `offset` (integer; default `0`): 列表偏移量，从 0 开始。
- `limit` (integer; default `50`): 返回条目数量，范围为 1 至 200。

### `tasks.files.selection.set`
设置单个任务中需要下载或跳过的文件。 Effect: `reversible_write`. Providers: `qbittorrent, transmission, rtorrent`.
- `task_id*` (string): 下载器自身返回的单个任务哈希或标识。
- `wanted_file_ids` (integer[]): 需要下载的文件在下载器中的索引；须提供本字段或 unwanted_file_ids。
- `unwanted_file_ids` (integer[]): 需要跳过的文件在下载器中的索引；须提供本字段或 wanted_file_ids。
- Rule: 须提供 wanted_file_ids 或 unwanted_file_ids，同一文件索引不得同时出现在两个列表中。

### `tasks.force_start.set`
启用或关闭 qBittorrent 任务的强制启动。 Effect: `reversible_write`. Providers: `qbittorrent`.
- `task_id` (string): 下载器自身返回的单个任务哈希或标识。
- `task_ids` (string[]): 下载器自身返回的多个任务哈希或标识，与 task_id 互斥。
- `enabled*` (boolean): 是否启用强制启动。
- Rule: task_id 和 task_ids 必须且只能提供一项。

### `tasks.list`
查询并筛选下载器任务。 Effect: `safe_read`. Providers: `qbittorrent, transmission, rtorrent`.
- `task_id` (string): 下载器自身返回的单个任务哈希或标识。
- `task_ids` (string[]): 下载器自身返回的多个任务哈希或标识，与 task_id 互斥。
- `status` (string): 按下载器原生任务状态筛选。
- `tags` (string|string[]): 只返回包含全部指定标签的任务。
- `offset` (integer; default `0`): 列表偏移量，从 0 开始。
- `limit` (integer; default `50`): 返回条目数量，范围为 1 至 200。

### `tasks.location.set`
移动任务数据或将任务保存位置指向下载器端路径。 Effect: `external_side_effect`. Providers: `qbittorrent, transmission, rtorrent`.
- `task_id*` (string): 下载器自身返回的单个任务哈希或标识。
- `location*` (string): 下载器端的新保存路径。

### `tasks.peers`
读取 qBittorrent 的连接节点同步数据。 Effect: `safe_read`. Providers: `qbittorrent`.
- `task_id*` (string): 下载器自身返回的单个任务哈希或标识。

### `tasks.properties.set`
设置任务速率、分享率或做种时长限制。 Effect: `reversible_write`. Providers: `qbittorrent, transmission, rtorrent`.
- `task_id*` (string): 下载器自身返回的单个任务哈希或标识。
- `upload_limit` (number): 上传上限，单位为 KB/s；0 表示不限速。
- `download_limit` (number): 下载上限，单位为 KB/s；0 表示不限速。
- `ratio_limit` (number): 分享率上限；rTorrent 不支持。
- `seeding_time_limit` (integer): 做种时长上限，单位为分钟；rTorrent 不支持。

### `tasks.queue.move`
将任务移至队首、上移、下移或移至队尾。 Effect: `reversible_write`. Providers: `qbittorrent, transmission`.
- `task_id` (string): 下载器自身返回的单个任务哈希或标识。
- `task_ids` (string[]): 下载器自身返回的多个任务哈希或标识，与 task_id 互斥。
- `position*` (string; allowed values `top,up,down,bottom`): 目标队列位置。
- Rule: task_id 和 task_ids 必须且只能提供一项。

### `tasks.reannounce`
强制向 Tracker 重新汇报。 Effect: `external_side_effect`. Providers: `qbittorrent, transmission`.
- `task_id` (string): 下载器自身返回的单个任务哈希或标识。
- `task_ids` (string[]): 下载器自身返回的多个任务哈希或标识，与 task_id 互斥。
- Rule: task_id 和 task_ids 必须且只能提供一项。

### `tasks.recheck`
强制校验任务数据。 Effect: `external_side_effect`. Providers: `qbittorrent, transmission, rtorrent`.
- `task_id` (string): 下载器自身返回的单个任务哈希或标识。
- `task_ids` (string[]): 下载器自身返回的多个任务哈希或标识，与 task_id 互斥。
- Rule: task_id 和 task_ids 必须且只能提供一项。

### `tasks.start`
启动或恢复一个或多个任务。 Effect: `reversible_write`. Providers: `qbittorrent, transmission, rtorrent`.
- `task_id` (string): 下载器自身返回的单个任务哈希或标识。
- `task_ids` (string[]): 下载器自身返回的多个任务哈希或标识，与 task_id 互斥。
- Rule: task_id 和 task_ids 必须且只能提供一项。

### `tasks.stop`
暂停一个或多个任务。 Effect: `reversible_write`. Providers: `qbittorrent, transmission, rtorrent`.
- `task_id` (string): 下载器自身返回的单个任务哈希或标识。
- `task_ids` (string[]): 下载器自身返回的多个任务哈希或标识，与 task_id 互斥。
- Rule: task_id 和 task_ids 必须且只能提供一项。

### `tasks.tags.get`
读取任务标签。 Effect: `safe_read`. Providers: `qbittorrent, transmission, rtorrent`.
- `task_id*` (string): 下载器自身返回的单个任务哈希或标识。

### `tasks.tags.set`
设置或添加任务标签。 Effect: `reversible_write`. Providers: `qbittorrent, transmission, rtorrent`.
- `task_id` (string): 下载器自身返回的单个任务哈希或标识。
- `task_ids` (string[]): 下载器自身返回的多个任务哈希或标识，与 task_id 互斥。
- `tags*` (string[]): 需要设置或添加的标签。
- Rule: task_id 和 task_ids 必须且只能提供一项。

### `tasks.trackers`
列出单个任务的 Tracker。 Effect: `safe_read`. Providers: `qbittorrent, transmission`.
- `task_id*` (string): 下载器自身返回的单个任务哈希或标识。

### `tasks.trackers.update`
添加或替换任务 Tracker。 Effect: `reversible_write`. Providers: `qbittorrent, transmission`.
- `task_id*` (string): 下载器自身返回的单个任务哈希或标识。
- `trackers*` (string[]): Tracker URL 列表。

## Verification

After a write, query the smallest relevant state: `tasks.list` for status and
properties, `tasks.files` for file priority, `tasks.trackers` for trackers, or
`session.speed_limits.get` for global limits. Report unsupported provider
capabilities explicitly instead of falling back to raw HTTP or arbitrary SDK
method calls.
