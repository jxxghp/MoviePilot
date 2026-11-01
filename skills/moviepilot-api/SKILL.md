---
name: moviepilot-api
version: 70
description: >-
  Use this skill for MoviePilot product operations such as media search, torrent
  search, downloads, subscriptions, library checks, sites, storage, workflows,
  schedulers, plugins, filter rules, and system settings. It authorizes the
  structured moviepilot_api gateway only; it does not authorize arbitrary HTTP,
  legacy Agent tools, MCP compatibility commands, authentication headers, or API
  tokens.
allowed-tools: moviepilot_api
allowed-api-operations: >-
  media.search media.person.search media.person.credits media.recognize media.scrape
  media.episode_schedule media.detail subscription.add subscription.update subscription.search
  subscription.list subscription.shares subscription.popular subscription.history
  subscription.delete download.add download.artist_collection download.tasks.active
  download.clients download.paths download.history.list download.history.delete
  transfer.history.delete site.update site.list site.userdata site.test site.cookie.update
  recommendation.list library.exists library.latest storage.settings storage.list transfer.history
  transfer.file scheduler.list scheduler.run workflow.list workflow.run plugin.installed
  plugin.market plugin.capabilities plugin.config.get plugin.config.update plugin.source.options
  plugin.source.install plugin.source.change plugin.reload plugin.install plugin.uninstall
  slash.list config.identifiers.get config.identifiers.update search.torrents search.results
  filter.builtin filter.custom filter.groups filter.custom.add filter.custom.update
  filter.custom.delete filter.group.add filter.group.update filter.group.delete plugin.data
  config.system.list config.system.describe config.system.get config.system.update slash.run
  music.recognize music.explore music.album.get music.album.related music.artist.get
  music.artist.albums music.artist.related music.cache.get music.cache.delete music.cache.clear
  system.versions system.update.status system.update.check system.update.download system.restart
  system.update.install system.upgrade.dev dashboard.media.statistics dashboard.storage
  dashboard.processes dashboard.system dashboard.downloader scheduler.progress
  dashboard.transfer.statistics dashboard.cpu dashboard.memory dashboard.network media.sources
  media.recognize_file media.cache.get media.cache.delete media.cache.clear
  media.classification.fields media.classification.policy.get media.classification.policy.validate
  media.classification.policy.preview media.classification.policy.impact
  media.classification.policy.history media.classification.policy.update
  media.classification.policy.rollback media.episode_groups media.episode_group.seasons
  media.seasons search.title search.recommend subtitle.search.title subtitle.search.media site.add
  site.delete site.auth.options site.authenticate site.cookiecloud.sync site.cookie.set site.reset
  site.priorities.update site.userdata.refresh site.userdata.latest site.category site.resource
  site.searchable site.rss site.statistics site.statistic site.mapping site.supporting
  subscription.get subscription.find subscription.delete_by_media subscription.status.update
  subscription.reset subscription.search_all subscription.refresh subscription.metadata.refresh
  subscription.history.delete subscription.user.list subscription.files
  subscription.execution.list subscription.execution.get subscription.execution.cancel
  subscription.share subscription.share.delete subscription.fork subscription.follow.list
  subscription.follow.add subscription.follow.delete subscription.share.statistics storage.manage
  storage.mkdir storage.rename storage.delete transfer.queue transfer.queue.delete transfer.name
  transfer.target_path transfer.manual_history transfer.episode_format.recommend
  transfer.manual_reviews transfer.manual_review transfer.manual_review.resolve
  transfer.history.redo transfer.history.redo_batch transfer.history.replan transfer.history.clear
  workflow.create workflow.get workflow.update workflow.delete workflow.actions
  workflow.event_types workflow.plugin.actions workflow.start workflow.pause workflow.reset
  workflow.shares workflow.share workflow.share.delete workflow.fork torrent.cache.get
  torrent.cache.delete torrent.cache.clear torrent.cache.refresh torrent.cache.reidentify
  database.backups.list database.backups.create database.backups.verify database.backups.delete
  filter.test system.network.targets system.network.test system.module.list system.module.catalog
  system.module.settings system.module.test plugin.market.sync_wiki plugin.runtime.status
  plugin.history plugin.releases plugin.ratings plugin.rating plugin.rating.submit
  plugin.statistics plugin.reset plugin.clone.restorable plugin.clone config.user.get
  config.public.get system.usage.statistics plugin.folders.get plugin.folders.update
  plugin.folder.create plugin.folder.update plugin.folder.delete plugin.folder.plugins.update
  plugin.folder.plugin.assign plugin.folder.plugin.remove plugin.loglevel.get plugin.loglevel.set
  plugin.loglevel.clear plugin.default_target.set plugin.default_target.clear
  plugin.instance.set_enabled plugin.instance.purge
---

# MoviePilot API

For a historical invalid plugin instance ID reported in logs, `plugin.instance.purge`
accepts the exact ID with only `config: true` (all other scope flags false). It deletes
only an empty disabled host record: no saved configuration, display overrides, default
target, or log overrides. A saved empty configuration object still protects the record.
This recovery path never stops plugins or deletes plugin data, databases, or files.
Records with settings, clones, missing records, and mixed scopes are rejected.

Unhandled exceptions outside the durable transfer step ledger stop automatic
replay and create an `unhandled_execution_review` diagnostic. Repair or disable
the failing plugin and inspect the files before resolving it as `not_applied`;
roll back untracked effects first when needed. This diagnostic rejects `applied`
and does not replace the evidence for any recorded file operations.

For relevant prior conversations or previous tool evidence, use the host-provided
`session_search` tool when available. Discover a session, then read its anchored
messages; past results are context, not proof of current state or permission to
repeat a write. Stable preferences use `search_memory`; its former `activity`
category no longer exists. Neither tool is an API operation or an external MCP tool.

Use `moviepilot_api` for normal MoviePilot business operations. The tool accepts
only `operation_id`, `path_params`, `query`, and `body`. The host chooses the
fixed HTTP method and path, creates the current user's authentication token,
applies authorization and confirmation policy, and returns the API response.

`slash.run` confirms that a command was triggered, not that execution finished.
Channel receipts use the host-verified sender; calls without a channel use the
existing administrator notification route and may have no delivery target.

The gateway connects directly to the local backend listener using `HOST` and
`PORT`, bypassing environment proxies. `APP_DOMAIN` is for public URLs such as
Passkey origins and is not the gateway base URL. Keep it configured even when
the public reverse proxy is unreachable from inside the container.

`subscription.add`, `subscription.update`, and `subscription.delete` require
authorization for the action and scope and use the active MoviePilot user bound to the channel account,
including for channel administrators. Creation belongs to that user; ordinary
users may update or delete only their own subscriptions. An unbound or inactive
channel user must bind an active account before retrying, not switch to an
administrator identity.

`subscription.list` uses the current user identity and subscription ownership filtering.
The separate global subscription integration endpoint requires a valid API_TOKEN
and administrator identity. It is outside this skill's allowed operations and must
never be used to bypass user scope.

For QQ, either `qq_userid` or `qq_openid` may match the current channel ID; for
Feishu, either `feishu_userid` or `feishu_openid` may match. A single matching
field is sufficient, but the match must identify exactly one active MoviePilot
user across all candidate fields. Do not copy an openid into the userid field
as a workaround or bypass an ambiguous binding.

For `subscription.update`, omit settings that should stay unchanged. Send JSON
`null` to clear a supported optional setting; see [Subscription APIs](api/subscription.md)
for the exact fields and the form empty-string compatibility rule.

Music downloads keep an existing media category and otherwise use the Chinese
music label as the downloader category, without changing library or metadata categories.
qBittorrent still follows MoviePilot's configured automatic-management toggle:
when enabled, its category rules control the save location; when disabled,
MoviePilot omits the category and explicitly disables automatic management so
the requested `save_path` applies. Existing tasks are not reclassified or moved.

This file is intentionally kept as the routing and execution guide. Detailed
operation contracts live in the linked category files under `api/`; load only
the one category file needed for the selected operation. Do not load every
category file by default.

Never provide a URL, method, authentication header, API key, or access token.
Never fall back to a retired tool name or `moviepilot tool` MCP command. If an
operation is not listed in this skill, do not simulate it through arbitrary HTTP;
use a more specific skill or explain that the structured operation is unavailable.

For SMB organization, keep the source `FileItem.storage` and share-relative
`path` together. Downloader task paths mapped to SMB retain the `smb:` prefix;
never interpret them as local filesystem paths. Same-storage SMB copy, move,
and hard-link operations execute on the server and fail without downloading
and re-uploading the media when the server cannot perform them. This does not
change separate content-processing operations such as embedded music tags.
With `shares: ["video", "downloads"]`, paths start with the share name, such as
`/downloads/Movie.mkv` and `/video/Movie.mkv`. Legacy `share` keeps share-relative
paths. Cross-share moves copy on the server before deleting the source and are
not atomic; cross-share hard links are unsupported. Update saved directory and
downloader mappings when switching path modes.

## Overall Workflow

1. Select the exact `operation_id` from the category index below.
2. Call `read_skill` again with `name="moviepilot-api"` and
   `file="api/<category>.md"` to load the complete standalone category
   contract. Do not use `read_file` for Skill documents.
3. The selected category file already includes the shared body Models needed to
   construct its calls; do not load a second Models document.
4. Build one gateway call with only declared fields. Preserve source-native
   identifiers and use the documented pagination fields. Pass object and array
   bodies as native JSON values, and pass null only when the selected operation
   allows it. The only string body is the literal `"dev"` for `system.upgrade.dev`.
   If a model JSON-encodes the whole body, every gateway entrypoint restores one
   layer of object, array, or null before validation and dispatch. Nested string
   values stay unchanged, and the selected operation's contract still applies.
   Never flatten operation fields next to `operation_id`: put each in its declared
   `path_params`, `query`, or `body` container. Do not copy pagination or filters
   from another operation. An `invalid_input` response names the failing field
   and includes `input_contract`; correct that field and all required fields
   before retrying. Invalid input is rejected before the API request is sent.
5. For a side effect, check authorization for the action, target, and scope.
   Reuse the user's explicit request or earlier authorization without asking
   again; ask only for missing material choices or expanded scope. Honor any
   confirmation required by the host, then execute the gateway call once.
6. Inspect `success`, `execution_outcome`, errors, empty results, and collection
   metadata before reporting or taking a dependent action. Never report a failure as success.
7. Verify writes with the category's read-back operation when the contract
   requires it; do not repeat a write whose outcome is `unknown`.

### Call Shape Examples

Replace example IDs with the exact identifiers returned by earlier calls. These
are separate operation contracts, not interchangeable parameter templates.

```json
{"operation_id":"media.detail","path_params":{"media_id":"27205"},"query":{"media_source":"tmdb","type_name":"\u7535\u5f71"}}
{"operation_id":"subscription.execution.list","query":{"limit":10}}
{"operation_id":"site.rss","query":{"page":1,"count":20}}
```

`media.detail` requires both the source-native ID and its source/type. Recent
subscription executions use `limit`, not `page` or `count`. `site.rss` lists
RSS-enabled sites; it does not accept a `site_id` filter.

`media.detail` and `transfer.file` accept `movie`, `tv`, or `music` in
`type_name`; `subscription.add` accepts the same values in `type`. The
corresponding Chinese labels remain supported.

## API Category Index

Each category file contains the complete operation contracts for its namespace.
Use the category contracts and frontmatter allowlist as the operation source of truth.

| Category | Detail file | Operation namespace | Use for |
| --- | --- | --- | --- |
| Configuration | [api/config.md](api/config.md) | `config.*` | identifiers, public/user settings, system setting discovery and updates |
| Dashboard | [api/dashboard.md](api/dashboard.md) | `dashboard.*` | media, storage, process, system, downloader, CPU, memory, network, and transfer summaries |
| Database | [api/database.md](api/database.md) | `database.backups.*` | administrator backup lifecycle |
| Download | [api/download.md](api/download.md) | `download.*` | download submission, clients, paths, active tasks, and history |
| Filter | [api/filter.md](api/filter.md) | `filter.*` | built-in/custom rules, groups, and testing |
| Library | [api/library.md](api/library.md) | `library.*` | existence and latest-media checks |
| Media | [api/media.md](api/media.md) | `media.*` | media search/detail, recognition, scraping, schedules, sources, people, seasons, and classification |
| Music | [api/music.md](api/music.md) | `music.*` | recognition, global and per-task CUE policy, exploration, albums, artists, text normalization, edition precedence, and cache administration |
| Plugin | [api/plugin.md](api/plugin.md) | `plugin.*` | plugin market, install/runtime, configuration, source, folders, ratings, releases, and statistics |
| Recommendation | [api/recommendation.md](api/recommendation.md) | `recommendation.*` | recommendation listings |
| Scheduler | [api/scheduler.md](api/scheduler.md) | `scheduler.*` | scheduler listing, progress, and execution |
| Search | [api/search.md](api/search.md) | `search.*` | title, torrent, result, and recommendation search |
| Site | [api/site.md](api/site.md) | `site.*` | site discovery, authentication, cookies, user data, resources, RSS, priorities, and statistics |
| Slash | [api/slash.md](api/slash.md) | `slash.*` | slash-command discovery and execution |
| Storage | [api/storage.md](api/storage.md) | `storage.*` | storage settings, browsing, directories, rename, and delete |
| Subscription | [api/subscription.md](api/subscription.md) | `subscription.*` | subscription CRUD, search/refresh, history, files, sharing, following, and status |
| Subtitle | [api/subtitle.md](api/subtitle.md) | `subtitle.search.*` | subtitle title and media search |
| System | [api/system.md](api/system.md) | `system.*` | versions, update, restart, modules, network, and usage |
| Torrent cache | [api/torrent.md](api/torrent.md) | `torrent.cache.*` | torrent-cache inspection, refresh, re-identification, and deletion |
| Transfer | [api/transfer.md](api/transfer.md) | `transfer.*` | transfer queue/history, file, naming, manual review, retry, and target path |
| Workflow | [api/workflow.md](api/workflow.md) | `workflow.*` | workflow definitions, actions, execution, sharing, and lifecycle |

Each category file is a standalone contract: it contains the operation details
and the shared request/response body Models needed by that category. If an
operation is added or moved, update its category file, this index, the
frontmatter allowlist, and the matching gateway contract together.

## API Surface Scope

System widget endpoints under `/api/v1/widget/*` are outside this skill and the Agent tool catalog.
Context, registration, and owner revocation use the existing main login. Snapshot reads and self
revocation use a separate, expiring, read-only Bearer; never use it as an ordinary API/MCP login
credential, or enable, rotate, or export widget grants on a user's behalf. Reading context must not
issue or rotate credentials. See `docs/widgets.md` for the protocol and user scope. System extensions
must not hold the main login token.

Plugin metadata may include `plugin_readme`, an optional HTTP(S) documentation link.
The UI resolves missing links through the admin REST endpoint
`GET /api/v1/plugin/readme/{plugin_id}?repo_url=...`, which returns `url` or null
without installing the plugin. This endpoint does not add an Agent operation.

For installed plugins with a bound source, `plugin.market` only lists an update
when that repository has a newer version. A higher version in another repository
does not trigger `has_update`. Use `plugin.source.options` to inspect alternative
sources; switching sources still requires an explicit source change. Installed
plugins without a bound source retain market candidates for source selection.

Operations declare an identity scope: `public` reads external public metadata without requiring a channel account binding; `bound_user` uses the active MoviePilot user bound to the stable channel sender ID (Web uses its authenticated user ID); `admin` requires administrator authority. Read-only does not mean public. Channel nicknames never establish identity. Personal records and subscription writes retain their existing ownership and permission checks.

When identity resolution fails, relay the gateway's actual remediation message and sender ID. Channel users bind their own MoviePilot account via avatar > Personal Information > Account Binding, fill the named channel field, and save. Do not invent a Settings > Users binding page, substitute nicknames/group/bot IDs, diagnose an ambiguous binding failure as a network issue, or ask Web users to bind a notification channel. Public operations do not require binding.

This Skill is the complete callable MoviePilot business API surface for the
Agent. Every operation in `allowed-api-operations` has one exact parameter
contract in a category file and one matching MCP `tools/list` branch in the
default `full` schema mode. Clients explicitly sending
`X-MCP-Schema-Mode: compact` receive a best-effort projection for oversized
schemas; exact operation contracts remain available here and through
`GET /api/v1/mcp/tools/moviepilot_api/schema`. There is
no hidden fallback to an arbitrary REST route.

MoviePilot's underlying OpenAPI document is larger because it also serves the
web UI, authentication, account lifecycle, binary and streaming responses,
callbacks, compatibility endpoints, and GitHub recognition/presentation routes.
Those routes are deliberately not copied into this Skill. A non-listed route
must be one of the following before the Agent may use its capability:

- represented by one stable aggregate operation in this Skill;
- owned by `downloader-operation`, `mediaserver-operation`, or another domain
  Skill with its own exact action contract;
- reserved for host transport, identity, UI, streaming, binary, or diagnostic
  behavior and therefore unavailable as an Agent business action; or
- explicitly unapproved until a role, effect, confirmation, recovery, result,
  and English parameter contract is added.

The maintained route-by-route inventory is the fixed registry in
`app/agent/policy/api.py`. The drift test fails when OpenAPI changes without an
explicit ownership decision.

The management recovery route `POST /api/v1/history/transfer/{history_id}/discard-corrupt`
is reserved for direct authenticated management clients and is not a callable
Agent operation. It clears corrupt task state while retaining the history record.

## Calling Contract

MCP operation and field descriptions are written in Chinese; operation IDs, field names,
enum values, and request shapes retain their exact documented spelling. The compact schema
rewrites descriptions that refer to omitted branches; use the full schema for precise inputs.

Setting a subscription to state `S` stops queued and running automatic searches at safe
boundaries, including new-subscription and site-retry tasks. An explicit subscription search
can still search and download once while paused; cancel its execution batch to stop that run.
Pausing or cancelling does not remove tasks already submitted to the downloader.

Call the gateway with this shape:

```json
{
  "operation_id": "media.search",
  "path_params": {},
  "query": {"title": "The Wandering Earth", "type": "media"},
  "body": {}
}
```

### Common read and download contracts

`search.recommend` with `check_only=true` reads current status without loading search results.
Disabled recommendations return `disabled` immediately. `force=true` takes precedence over
the read-only flag and loads candidates to start a new task.
The browser endpoints `/media/status` and `/subscribe/calendar` are outside the Agent
operation catalog; do not invent gateway calls for them.

Select the operation for the task first, then send only fields declared by that operation. Common verification contracts are:

| operation_id | `path_params` | `query` |
| --- | --- | --- |
| `subscription.find` | `media_id` | `media_source`; optional `season`, `music_type` |
| `subscription.list` | none | optional `page`, `count` |
| `download.tasks.active` | none | optional `page`, `count`, `name` |
| `site.list` | none | optional `page`, `count`, `name`, `status=all\|active\|inactive` |

`site.category` returns category entries without search settings. Category `id`
values may be integers or text; numeric strings retain the integer response.
Join selected IDs with commas for `site.resource`'s `query.cat`, preserving text IDs.

Subscription execution logs are browser resource endpoints:
`GET /api/v1/subscribe/{subscription_id}/logs` (SSE, or text with `length=-1`)
and `/logs/download` (ZIP). They require the existing resource Cookie and enforce
subscription ownership; they are not JSON gateway or MCP tools. Do not pass a
logfile path or try to call them through `call_api`. Read subscription status
through the supported subscription operations; completion of a search task does
not prove completion of the subscription.

The `download.add` body must contain `torrent_in` (at least `title` and `enclosure`) plus sibling `media_source` and `media_id`; do not put a magnet URI in `url`, or move media identity and filters into `query`. When a write returns `unknown`, never retry it; verify the actual state with a supported read operation first.

Preserve the candidate's `site` ID and download URL. For direct HTTP(S) torrents,
`site_cookie` may be omitted: the backend fills a missing Cookie and User-Agent
from the configured site only when the download URL matches its scheme, host,
and port. Without a site ID, this requires exactly one matching configured site.
Explicit credentials and proxy selection are preserved. Magnet and two-step
token exchange downloads retain their existing credential rules.

- Put route placeholders such as `subscribe_id`, `hashString`, `plugin_id`,
  `workflow_id`, `media_id`, `storage`, `rule_id`, and `name` in `path_params`.
- Put GET filters and control values in `query`. The gateway also accepts GET
  values in `body`, but use `query` consistently except for the protected secret
  flow below.
- Put POST, PUT, and PATCH request models in `body`.
- Preserve the exact source-native `media_source` + `media_id` returned by a
  search or detail response. For music, also preserve
  `music_type=recording|album|artist`; an artist is browse-only.
- Treat `success=false`, HTTP error data, empty results, and validation errors as
  real outcomes. Do not claim success without checking the response.
- `media.scrape` reports rejected media and failed NFO writes as business failures,
  including failures within a directory. For concurrent custom-filter changes,
  re-read the rules and confirm the intended change before retrying a conflict.
- Respect an explicit `execution_outcome`: `pending` is accepted but unfinished,
  while `unknown` means a write may have happened. Do not repeat an unknown write
  or change defaults merely to evade duplicate protection. In the built-in Agent,
  use `get_tool_execution` with the returned invocation ID; the host can reconcile
  supported non-sensitive setting replacements through a read-only check.
- When a built-in Agent preview contains `result_id` and `next_offset`, use
  `read_tool_result` for the next page instead of repeating the operation. These
  receipt and result tools are internal to the Agent, not external MCP tools.

## Agent Task And Conversation State

- For `agent_task` updates, `enabled=null` preserves the current enabled state. Renaming a paused task does not resume its schedule.
- `agent_task` pins a real execution account at creation. Channel senders must uniquely bind to an active MoviePilot user. Background API calls retain that account's permissions, and invalid identity fails the run. Confirm legacy tasks with `update` in their original conversation or recreate them; never substitute a same-name account or fall back to the superuser.
- Traditional message sessions are scoped by channel, configured source, and user ID. A stop, clear, status query, or confirmation targets the current source's session.
- Background WebAgent completion appends the current turn to display history. Frontend complete snapshots keep their existing replacement contract.
- Complete snapshot replacement retains hidden choice receipts so delayed snapshots cannot restore a selected card to pending; ordinary messages retain replacement behavior.
- WebAgent choice callbacks save the first selection before replying. `already_selected=true` means
  execution was claimed; restore its feedback without sending another message. Unclaimed selections
  can retry the same value. Idle waiting workers do not count toward `is_processing`.
- WebAgent temporary attachments are scoped to their creating account. Download, voice transcription, and attachment events accept only references registered to the current account; cross-account access is treated as missing.
- File creation with `overwrite=false` rejects a concurrent creator. Versioned edits reject a peer's intervening write; reread the file and use its current SHA before retrying.
- Market skill installation preserves an explicitly selected GitHub branch. If that branch is unavailable, treat installation as failed instead of silently switching branches.

## Subscription Identity And Workflow Recovery

- Preserve media type, source, media ID, season, episode group, and music entity type when finding or creating a subscription. Movies and TV series with the same numeric ID are distinct targets. Keep the episode group in source-based lookup.
- Concurrent creation for the same user and identity returns the existing subscription ID. An update to an existing target identity cancels that update; reread both subscriptions before deciding the next step.
- Changing the work, season, episode group, or music entity resets the previous target's download and upgrade progress. Missing episodes restart from the new total; clearing the episode group also changes the target.
- Servarr duplicate series additions return the real existing subscription ID. If a cancelled search loses its executor, lease expiry settles the cancellation and permits a new search.
- Workflow recovery retries failed nodes and nodes skipped because of failed dependencies while retaining successful node results. Editing a paused workflow keeps its timer disabled; Cron uses the host scheduler timezone.

## Collection Counts And Pagination

- Search SSE `manual_paging=true` is a Web-only page-by-page mode and adds no Agent
  operations; keep using the documented `search.*` operations. Web streams search
  sites concurrently and emit `append` previews before the final filtered page;
  only the completed result carries source state for advancing page numbers.
- Recent `search.results` snapshots belong to the authenticated user; search again after
  upgrading to populate them. Optional Web `done.diagnostics` uses `scope=page` for the current
  page. Skipped rule groups do not increase the resource rejection count.
- For list inspection, explicitly send the operation's documented pagination
  fields instead of requesting an unbounded legacy result. For optional legacy
  pagination, start with `query={"page":1,"count":20}`.
- For a count or summary request when the operation documents an exact total,
  send `query={"page":1,"count":1}` and read `collection.total_count`. This is
  the authoritative count after the endpoint's authorization scope and filters.
- A large item list or `tool_result_truncated=true` does not make the total
  unavailable. The gateway places `collection` before `data`, so its exact
  metadata remains visible in the bounded preview. Never query the MoviePilot
  database merely to recover a total already declared by the API contract.
- Use `database-operation` only for administrator diagnostics or aggregations
  that the business API cannot express. Do not use it as a fallback for an API
  list count. If an operation explicitly omits `collection.total_count`, do not
  infer a total from one page; continue its native pagination or state that the
  upstream total is unavailable.

## Cross-Skill Routing

Use `downloader-operation` for downloader instances, task inspection, and native
task control. Use `mediaserver-operation` for libraries, items, playback
sessions, scans, refreshes, and other native media-server capabilities.
Never bypass the gateway with an arbitrary URL.

For three or more read-only calls with paging or aggregation, load [Python aggregation](code-execution.md) and use `execute_code` when available.

## Reusable learning

When available, use `skills_list` and `skill_view` to read personal skills, and `skill_manage` only within the authorized authoring/review scope;
they never grant API operation scopes. Load the public domain skill with `read_skill` as usual.
Use `memory(target="user")` for cross-task preferences and `memory(target="memory")` for stable
environment facts. Procedures belong in the relevant skill, not duplicated in both stores.
A background proposal to replace/remove memory needs the user's explicit `/memory approve ID`;
never infer that confirmation from a tool result or a prior conversation.


### Download submission provenance

New records returned by `download.history.list` may include `note.subscription_download`:
`submission_type` (`initial` or `upgrade`), `best_version_full`, and the initial and upgraded episode lists.
Filling missing episodes remains an initial download even when quality upgrades are enabled.
A pack containing both missing and previously downloaded episodes counts as an upgrade and retains both lists.
Do not infer provenance from current subscription settings when an older history record has no marker.
