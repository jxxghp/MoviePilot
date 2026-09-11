---
name: moviepilot-api
version: 31
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
  config.system.get config.system.update slash.run music.recognize music.explore music.album.get
  music.album.related music.artist.get music.artist.albums music.artist.related music.cache.get
  music.cache.delete music.cache.clear system.versions system.update.status system.update.check
  system.update.download system.restart system.update.install system.upgrade.dev
  dashboard.media.statistics dashboard.storage dashboard.processes dashboard.system
  dashboard.downloader scheduler.progress dashboard.transfer.statistics dashboard.cpu
  dashboard.memory dashboard.network media.sources media.recognize_file media.cache.get
  media.cache.delete media.cache.clear media.classification.fields media.classification.policy.get
  media.classification.policy.validate media.classification.policy.preview
  media.classification.policy.impact media.classification.policy.history
  media.classification.policy.update media.classification.policy.rollback media.episode_groups
  media.episode_group.seasons media.seasons search.title search.recommend subtitle.search.title
  subtitle.search.media site.add site.delete site.auth.options site.authenticate
  site.cookiecloud.sync site.reset site.priorities.update site.userdata.refresh
  site.userdata.latest site.category site.resource site.searchable site.rss site.statistics
  site.statistic site.mapping site.supporting subscription.get subscription.find
  subscription.delete_by_media subscription.status.update subscription.reset
  subscription.search_all subscription.refresh subscription.metadata.refresh
  subscription.history.delete subscription.user.list subscription.files
  subscription.execution.list subscription.execution.get subscription.execution.cancel
  subscription.share subscription.share.delete subscription.fork subscription.follow.list
  subscription.follow.add subscription.follow.delete subscription.share.statistics storage.manage
  storage.mkdir storage.rename storage.delete transfer.queue transfer.queue.delete transfer.name
  transfer.target_path transfer.manual_history transfer.episode_format.recommend
  transfer.manual_reviews transfer.manual_review transfer.manual_review.resolve
  transfer.history.redo transfer.history.redo_batch transfer.history.clear workflow.create
  workflow.get workflow.update workflow.delete workflow.actions workflow.event_types
  workflow.plugin.actions workflow.start workflow.pause workflow.reset workflow.shares
  workflow.share workflow.share.delete workflow.fork torrent.cache.get torrent.cache.delete
  torrent.cache.clear torrent.cache.refresh torrent.cache.reidentify database.backups.list
  database.backups.create database.backups.verify database.backups.delete filter.test
  system.network.targets system.network.test system.module.list system.module.catalog
  system.module.settings system.module.test plugin.market.sync_wiki plugin.runtime.status
  plugin.history plugin.releases plugin.ratings plugin.rating plugin.rating.submit
  plugin.statistics plugin.reset plugin.clone config.user.get config.public.get
  system.usage.statistics plugin.folders.get plugin.folders.update plugin.folder.create
  plugin.folder.update plugin.folder.delete plugin.folder.plugins.update
  plugin.folder.plugin.assign plugin.folder.plugin.remove
---

# MoviePilot API

Use `moviepilot_api` for normal MoviePilot business operations. The tool accepts
only `operation_id`, `path_params`, `query`, and `body`. The host chooses the
fixed HTTP method and path, creates the current user's authentication token,
applies authorization and confirmation policy, and returns the API response.

This file is intentionally kept as the routing and execution guide. Detailed
operation contracts live in the linked category files under `api/`; load only
the one category file needed for the selected operation. Do not load every
category file by default.

Never provide a URL, method, authentication header, API key, or access token.
Never fall back to a retired tool name or `moviepilot tool` MCP command. If an
operation is not listed in this skill, do not simulate it through arbitrary HTTP;
use a more specific skill or explain that the structured operation is unavailable.

## Overall Workflow

1. Select the exact `operation_id` from the category index below.
2. Call `read_skill` again with `name="moviepilot-api"` and
   `file="api/<category>.md"` to load the complete standalone category
   contract. Do not use `read_file` for Skill documents.
3. The selected category file already includes the shared body Models needed to
   construct its calls; do not load a second Models document.
4. Build one gateway call with only declared fields. Preserve source-native
   identifiers and use the documented pagination fields.
5. Obtain confirmation for confirmation-protected or side-effecting operations,
   then execute the gateway call once.
6. Inspect `success`, `execution_outcome`, errors, empty results, and collection
   metadata before reporting or taking a dependent action.
7. Verify writes with the category's read-back operation when the contract
   requires it; do not repeat a write whose outcome is `unknown`.

## API Category Index

Each category file contains the complete operation contracts for its namespace.
The counts are a maintenance aid for the 220 currently exposed operations.

| Category | Detail file | Operation namespace | Count | Use for |
| --- | --- | --- | ---: | --- |
| Configuration | [api/config.md](api/config.md) | `config.*` | 6 | identifiers, public/user settings, system setting discovery and updates |
| Dashboard | [api/dashboard.md](api/dashboard.md) | `dashboard.*` | 9 | media, storage, process, system, downloader, CPU, memory, network, and transfer summaries |
| Database | [api/database.md](api/database.md) | `database.backups.*` | 4 | administrator backup lifecycle |
| Download | [api/download.md](api/download.md) | `download.*` | 7 | download submission, clients, paths, active tasks, and history |
| Filter | [api/filter.md](api/filter.md) | `filter.*` | 10 | built-in/custom rules, groups, and testing |
| Library | [api/library.md](api/library.md) | `library.*` | 2 | existence and latest-media checks |
| Media | [api/media.md](api/media.md) | `media.*` | 23 | media search/detail, recognition, scraping, schedules, sources, people, seasons, and classification |
| Music | [api/music.md](api/music.md) | `music.*` | 10 | recognition, exploration, albums, artists, and cache administration |
| Plugin | [api/plugin.md](api/plugin.md) | `plugin.*` | 30 | plugin market, install/runtime, configuration, source, folders, ratings, releases, and statistics |
| Recommendation | [api/recommendation.md](api/recommendation.md) | `recommendation.*` | 1 | recommendation listings |
| Scheduler | [api/scheduler.md](api/scheduler.md) | `scheduler.*` | 3 | scheduler listing, progress, and execution |
| Search | [api/search.md](api/search.md) | `search.*` | 4 | title, torrent, result, and recommendation search |
| Site | [api/site.md](api/site.md) | `site.*` | 22 | site discovery, authentication, cookies, user data, resources, RSS, priorities, and statistics |
| Slash | [api/slash.md](api/slash.md) | `slash.*` | 2 | slash-command discovery and execution |
| Storage | [api/storage.md](api/storage.md) | `storage.*` | 6 | storage settings, browsing, directories, rename, and delete |
| Subscription | [api/subscription.md](api/subscription.md) | `subscription.*` | 29 | subscription CRUD, search/refresh, history, files, sharing, following, and status |
| Subtitle | [api/subtitle.md](api/subtitle.md) | `subtitle.search.*` | 2 | subtitle title and media search |
| System | [api/system.md](api/system.md) | `system.*` | 12 | versions, update, restart, modules, network, and usage |
| Torrent cache | [api/torrent.md](api/torrent.md) | `torrent.cache.*` | 5 | torrent-cache inspection, refresh, re-identification, and deletion |
| Transfer | [api/transfer.md](api/transfer.md) | `transfer.*` | 15 | transfer queue/history, file, naming, manual review, retry, and target path |
| Workflow | [api/workflow.md](api/workflow.md) | `workflow.*` | 16 | workflow definitions, actions, execution, sharing, and lifecycle |

Each category file is a standalone contract: it contains the operation details
and the shared request/response body Models needed by that category. If an
operation is added or moved, update its category file, this index, the
frontmatter allowlist, and the matching gateway contract together.

## API Surface Scope

This Skill is the complete callable MoviePilot business API surface for the
Agent. Every operation in `allowed-api-operations` has one exact parameter
contract in a category file and one matching MCP `tools/list` branch. There is
no hidden fallback to an arbitrary REST route.

MoviePilot's underlying OpenAPI document is larger because it also serves the
web UI, authentication, account lifecycle, binary and streaming responses,
callbacks, compatibility endpoints, and source-specific presentation routes.
Those routes are deliberately not copied into this Skill. A non-listed route
must be one of the following before the Agent may use its capability:

- represented by one stable aggregate operation in this Skill;
- owned by `downloader-operation`, `mediaserver-operation`, or another domain
  Skill with its own exact action contract;
- reserved for host transport, identity, UI, streaming, binary, or diagnostic
  behavior and therefore unavailable as an Agent business action; or
- explicitly unapproved until a role, effect, confirmation, recovery, result,
  and English parameter contract is added.

The maintained route-by-route inventory is
`docs/refactor/agent-api-surface-audit.md`. Its generated drift test fails
when OpenAPI changes without an explicit ownership decision.

The management recovery route `POST /api/v1/history/transfer/{history_id}/discard-corrupt`
is reserved for direct authenticated management clients and is not a callable
Agent operation. It clears corrupt task state while retaining the history record.

## Calling Contract

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

Select the operation for the task first, then send only fields declared by that operation. Common verification contracts are:

| operation_id | `path_params` | `query` |
| --- | --- | --- |
| `subscription.find` | `media_id` | `media_source`; optional `season`, `music_type` |
| `subscription.list` | none | optional `page`, `count` |
| `download.tasks.active` | none | optional `page`, `count`, `name` |
| `site.list` | none | optional `page`, `count`, `name`, `status=all\|active\|inactive` |

The `download.add` body must contain `torrent_in` (at least `title` and `enclosure`) plus sibling `media_source` and `media_id`; do not put a magnet URI in `url`, or move media identity and filters into `query`. When a write returns `unknown`, never retry it; verify the actual state with a supported read operation first.

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
- Respect an explicit `execution_outcome`: `pending` is accepted but unfinished,
  while `unknown` means a write may have happened. Do not repeat an unknown write
  or change defaults merely to evade duplicate protection. In the built-in Agent,
  use `get_tool_execution` with the returned invocation ID; the host can reconcile
  supported non-sensitive setting replacements through a read-only check.
- When a built-in Agent preview contains `result_id` and `next_offset`, use
  `read_tool_result` for the next page instead of repeating the operation. These
  receipt and result tools are internal to the Agent, not external MCP tools.

## Collection Counts And Pagination

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

## Operation Order And Failure Handling

1. Select the operation first, then place each value in its documented bucket. Never move query fields into path_params or send undeclared fields.
2. Reuse the exact `media_source` + `media_id` pair returned by search. For music, also preserve `music_type`.
3. Downloads, transfers, configuration/rule/plugin writes, scheduler/workflow runs, and deletions have side effects; obtain confirmation and inspect the result.
4. `success=false`, HTTP errors, validation errors, and empty results are real outcomes. Never report them as success.
5. Use `database-operation`, `downloader-operation`, or `mediaserver-operation` for their native capabilities. Never bypass the gateway with an arbitrary URL.
