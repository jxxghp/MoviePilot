---
name: moviepilot-api
version: 25
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
  subscription.delete download.add download.tasks.active download.clients download.paths
  download.history.list download.history.delete transfer.history.delete site.update site.list
  site.userdata site.test site.cookie.update recommendation.list library.exists library.latest
  storage.settings storage.list transfer.history transfer.file scheduler.list scheduler.run
  workflow.list workflow.run plugin.installed plugin.market plugin.capabilities plugin.config.get
  plugin.config.update plugin.source.options plugin.source.install plugin.source.change
  plugin.reload plugin.install plugin.uninstall slash.list config.identifiers.get
  config.identifiers.update search.torrents search.results filter.builtin filter.custom
  filter.groups filter.custom.add filter.custom.update filter.custom.delete filter.group.add
  filter.group.update filter.group.delete plugin.data config.system.get config.system.update
  slash.run music.recognize music.explore music.album.get music.album.related music.artist.get
  music.artist.albums music.artist.related music.cache.get music.cache.delete music.cache.clear
  system.versions system.update.status system.update.check system.update.download system.restart
  system.update.install system.upgrade.dev dashboard.media.statistics dashboard.storage
  dashboard.processes dashboard.system dashboard.downloader scheduler.progress
  dashboard.transfer.statistics dashboard.cpu dashboard.memory dashboard.network media.sources
  media.recognize_file media.category.config.get media.category.config.update media.categories
  media.episode_groups media.episode_group.seasons media.seasons search.title search.recommend
  subtitle.search.title subtitle.search.media site.add site.delete site.auth.options
  site.authenticate site.cookiecloud.sync site.reset site.priorities.update site.userdata.refresh
  site.userdata.latest site.category site.resource site.searchable site.rss site.statistics
  site.statistic site.mapping site.supporting subscription.get subscription.find
  subscription.delete_by_media subscription.status.update subscription.reset
  subscription.search_all subscription.refresh subscription.metadata.refresh
  subscription.history.delete subscription.user.list subscription.files subscription.share
  subscription.share.delete subscription.fork subscription.follow.list subscription.follow.add
  subscription.follow.delete subscription.share.statistics storage.manage storage.mkdir
  storage.rename storage.delete transfer.queue transfer.queue.delete transfer.name
  transfer.target_path transfer.manual_history transfer.episode_format.recommend
  transfer.manual_reviews transfer.manual_review transfer.manual_review.resolve
  transfer.history.redo transfer.history.redo_batch transfer.history.clear workflow.create
  workflow.get workflow.update workflow.delete workflow.actions workflow.event_types
  workflow.plugin.actions workflow.start workflow.pause workflow.reset workflow.shares
  workflow.share workflow.share.delete workflow.fork torrent.cache.get torrent.cache.delete
  torrent.cache.clear torrent.cache.refresh torrent.cache.reidentify database.backups.list
  database.backups.create database.backups.verify database.backups.delete filter.test
  system.network.targets system.network.test system.module.list system.module.test
  plugin.market.sync_wiki plugin.runtime.status plugin.history plugin.releases plugin.ratings
  plugin.rating plugin.rating.submit plugin.statistics plugin.reset plugin.clone config.user.get
  config.public.get system.usage.statistics plugin.folders.get plugin.folders.update
  plugin.folder.create plugin.folder.delete plugin.folder.plugins.update
---

# MoviePilot API

Use `moviepilot_api` for normal MoviePilot business operations. The tool accepts
only `operation_id`, `path_params`, `query`, and `body`. The host chooses the
fixed HTTP method and path, creates the current user's authentication token,
applies authorization and confirmation policy, and returns the API response.
When the gateway is called directly, the host automatically loads this Skill's
operation scope before enforcing the allowlist; explicitly loading the Skill is
still preferred so the model receives the full parameter and failure-handling
contract before it calls the gateway.

Never provide a URL, method, authentication header, API key, or access token.
Never fall back to a retired tool name or `moviepilot tool` MCP command. If an
operation is not listed in this skill, do not simulate it through arbitrary HTTP;
use a more specific skill or explain that the structured operation is unavailable.
Use `downloader-operation` for downloader instances, task inspection and native
task control. Use `mediaserver-operation` for libraries, items, playback sessions,
scans, refreshes and other native media-server capabilities.

## API Surface Scope

This Skill is the complete callable MoviePilot business API surface for the
Agent. Every operation in `allowed-api-operations` has one exact parameter
contract below and one matching MCP `tools/list` branch. There is no hidden
fallback to an arbitrary REST route.

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
`docs/architecture/agent-api-surface-audit.md`. Its generated drift test fails
when OpenAPI changes without an explicit ownership decision.

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

## Music Navigation

- Search titles, albums, or artists with `media.search` using `type=music`. Preserve
  every returned `media_source`, `media_id`, `music_type`, `album_id`, and
  `artist_ids` value instead of matching by display name.
- Use `music.artist.albums` to browse an artist's works. Its `album_type` filter
  distinguishes albums, singles, EPs, compilations, soundtracks, live releases,
  remixes, and the other documented MusicBrainz release-group types.
- Use `music.album.get` to browse from a work back to its artists. The response
  includes aligned `artists` and `artist_ids`, plus tracks and releases; pass one
  returned artist ID to `music.artist.get`, `music.artist.albums`, or
  `music.artist.related` with the same `media_source`.
- Use `music.album.related` for related works and `music.artist.related` for
  related artists. Use `music.explore` for MusicBrainz charts/fresh releases or
  Douban Music tag browsing.
- `music.recognize` resolves only a recording or album. Artist identities are
  browse-only. Music recognition-cache operations are administrator-only; call
  `music.cache.get` before deleting one exact key, and clear all entries only
  after explicit confirmation.

## Operation Catalog

The operations, HTTP methods, routes, and path/query/body fields below exactly match external MCP `tools/list`.
A field name ending in `*` is required. Omit an empty bucket or send `{}`. Referenced body models are expanded below.
For collection operations, `data` keeps its existing list or page-object shape. The gateway may add a sibling `collection` object with `result_count`, optional exact `total_count`, `page`, and `count`; it never replaces the list body with a new wrapper.
When a collection contract exposes an exact total, answer count or summary requests from that API metadata. For optional legacy pagination, send `page=1,count=1` and read `collection.total_count`; never query the database merely because item data or a tool preview was truncated.
If an endpoint or external source does not expose a total, `collection.total_count` is omitted instead of being guessed from the current page.

### `config.identifiers.get`
`GET /api/v1/system/identifiers`; policy effect: `safe_read`.
Purpose: Read the complete custom media-recognition identifier list.
- `path_params`: none
- `query`: none
- `body`: none

### `config.identifiers.update`
`POST /api/v1/system/identifiers`; policy effect: `reversible_write`.
Purpose: Replace the complete custom media-recognition identifier list.
- `path_params`: none
- `query`: none
- `body`: `identifiers` (array<string>): Complete ordered list of custom recognition identifier rules.

### `config.public.get`
`GET /api/v1/system/setting/public/{key}`; policy effect: `safe_read`.
Purpose: Read one explicitly public system setting by exact key.
- `path_params`: `key*` (string): Optional exact plugin data key used to narrow the returned preview.
- `query`: none
- `body`: none

### `config.system.get`
`GET /api/v1/system/settings`; policy effect: `safe_read`.
Purpose: Discover registered system settings or read one exact setting.
- `path_params`: none
- `query`: `group` (string|null; default `all`): Discovery group used when setting_key is omitted. Supported groups are all, settings, systemconfig, downloaders, media_servers, notifications, notification_switches, storages, directories, search_sites, subscribe_sites, site_auth, ai_agent, filter_rules, subscribe_defaults, plugins, customization, transfer, scraping, and misc.; `include_values` (boolean|null): Return full values. Defaults to true for one exact key and false for discovery results.; `keyword` (string|null): Case-insensitive substring used to discover matching keys, groups, or labels.; `setting_key` (string|null): Exact setting key. Accepts Settings field names such as APP_DOMAIN or LLM_MODEL, SystemConfigKey values or enum names such as Downloaders or MediaServers, and aliases that resolve to one unique setting. Omit it to discover settings.; `show_secrets` (boolean; default `False`): Return unredacted secret values. Defaults to false and remains confirmation-protected.
- `body`: none

### `config.system.update`
`POST /api/v1/system/settings`; policy effect: `reversible_write`.
Purpose: Update one exact registered system setting.
- `path_params`: none
- `query`: none
- `body`: `match_field` (string|null): Object field used to match a list item. Downloaders, MediaServers, Notifications, Directories, and Storages default to name; NotificationSwitchs defaults to type. Supply it for other object lists.; `match_value` (value): Value compared against match_field. If omitted, use value[match_field]; scalar lists use value directly.; `operation` (string(replace,merge_dict,upsert_list_item,remove_list_item); default `replace`): replace overwrites the complete value; merge_dict shallow-merges an object; upsert_list_item inserts or replaces one matched list item; remove_list_item removes one matched list item.; `remove_keys` (array<string>): Object keys to remove after merge_dict applies the supplied value.; `setting_key*` (string): Exact setting key. Accepts a Settings field name, a SystemConfigKey value or enum name, or an alias that resolves to one unique setting. Call config.system.get with group or keyword first when the key is unknown.; `value` (value): New value or list item. For replace, send the complete value. For merge_dict, send the object fragment to merge. For upsert_list_item or remove_list_item, send one object or scalar item.

### `config.user.get`
`GET /api/v1/system/global/user`; policy effect: `safe_read`.
Purpose: Read current-user feature flags, runtime capabilities, and effective permissions.
- `path_params`: none
- `query`: none
- `body`: none

### `dashboard.cpu`
`GET /api/v1/dashboard/cpu`; policy effect: `safe_read`.
Purpose: Read the current host CPU utilization percentage.
- `path_params`: none
- `query`: none
- `body`: none

### `dashboard.downloader`
`GET /api/v1/dashboard/downloader`; policy effect: `safe_read`.
Purpose: Read aggregate downloader task counts, speeds, and free-space information.
- `path_params`: none
- `query`: `name` (string|null): Human-readable name of the site, storage item, subscription, or rule group.
- `body`: none

### `dashboard.media.statistics`
`GET /api/v1/dashboard/statistic`; policy effect: `safe_read`.
Purpose: Read aggregate movie, TV, episode, and music library counts.
- `path_params`: none
- `query`: `name` (string|null): Human-readable name of the site, storage item, subscription, or rule group.
- `body`: none

### `dashboard.memory`
`GET /api/v1/dashboard/memory`; policy effect: `safe_read`.
Purpose: Read current MoviePilot process and host memory utilization.
- `path_params`: none
- `query`: none
- `body`: none

### `dashboard.network`
`GET /api/v1/dashboard/network`; policy effect: `safe_read`.
Purpose: Read the current host network receive and transmit counters.
- `response`: `data` remains a list; omitting both `page` and `count` keeps the complete legacy result. `collection.result_count` reports the returned items and `collection.total_count` reports the exact pre-pagination total. For counts or summaries, send `page=1,count=1`, read `collection.total_count`, and do not fall back to a database query because the item preview was truncated.
- `path_params`: none
- `query`: `count` (integer|null): Optional page size for a legacy full-list endpoint. Supplying page or count activates pagination; an omitted count then uses 50.; `page` (integer|null): Optional one-based page for a legacy full-list endpoint. Omit both page and count to keep the original unpaginated full result.
- `body`: none

### `dashboard.processes`
`GET /api/v1/dashboard/processes`; policy effect: `safe_read`.
Purpose: List host processes visible to the MoviePilot runtime.
- `response`: `data` remains a list; omitting both `page` and `count` keeps the complete legacy result. `collection.result_count` reports the returned items and `collection.total_count` reports the exact pre-pagination total. For counts or summaries, send `page=1,count=1`, read `collection.total_count`, and do not fall back to a database query because the item preview was truncated.
- `path_params`: none
- `query`: `count` (integer|null): Optional page size for a legacy full-list endpoint. Supplying page or count activates pagination; an omitted count then uses 50.; `page` (integer|null): Optional one-based page for a legacy full-list endpoint. Omit both page and count to keep the original unpaginated full result.
- `body`: none

### `dashboard.storage`
`GET /api/v1/dashboard/storage`; policy effect: `safe_read`.
Purpose: Read local filesystem capacity and free-space information.
- `path_params`: none
- `query`: none
- `body`: none

### `dashboard.system`
`GET /api/v1/dashboard/system`; policy effect: `safe_read`.
Purpose: Read MoviePilot host, runtime, platform, and uptime summary information.
- `path_params`: none
- `query`: none
- `body`: none

### `dashboard.transfer.statistics`
`GET /api/v1/dashboard/transfer`; policy effect: `safe_read`.
Purpose: Read aggregate file-transfer counts grouped by time period.
- `response`: `data` remains a list; omitting both `page` and `count` keeps the complete legacy result. `collection.result_count` reports the returned items and `collection.total_count` reports the exact pre-pagination total. For counts or summaries, send `page=1,count=1`, read `collection.total_count`, and do not fall back to a database query because the item preview was truncated.
- `path_params`: none
- `query`: `count` (integer|null): Optional page size for a legacy full-list endpoint. Supplying page or count activates pagination; an omitted count then uses 50.; `days` (integer|null; default `7`): Recommendation time window in days.; `page` (integer|null): Optional one-based page for a legacy full-list endpoint. Omit both page and count to keep the original unpaginated full result.
- `body`: none

### `database.backups.create`
`POST /api/v1/system/database/backups`; policy effect: `external_side_effect`.
Purpose: Create, verify, and atomically publish a managed database backup.
- `path_params`: none
- `query`: none
- `body`: none

### `database.backups.delete`
`DELETE /api/v1/system/database/backups/{name}`; policy effect: `destructive_write`.
Purpose: Delete one exact managed database backup artifact.
- `path_params`: `name*` (string): Human-readable name of the site, storage item, subscription, or rule group.
- `query`: none
- `body`: none

### `database.backups.list`
`GET /api/v1/system/database/backups`; policy effect: `safe_read`.
Purpose: List managed database backup artifacts without exposing host paths.
- `response`: `data` remains a list; omitting both `page` and `count` keeps the complete legacy result. `collection.result_count` reports the returned items and `collection.total_count` reports the exact pre-pagination total. For counts or summaries, send `page=1,count=1`, read `collection.total_count`, and do not fall back to a database query because the item preview was truncated.
- `path_params`: none
- `query`: `count` (integer|null): Optional page size for a legacy full-list endpoint. Supplying page or count activates pagination; an omitted count then uses 50.; `page` (integer|null): Optional one-based page for a legacy full-list endpoint. Omit both page and count to keep the original unpaginated full result.
- `body`: none

### `database.backups.verify`
`POST /api/v1/system/database/backups/{name}/verify`; policy effect: `safe_read`.
Purpose: Verify the integrity of one exact managed database backup artifact.
- `path_params`: `name*` (string): Human-readable name of the site, storage item, subscription, or rule group.
- `query`: none
- `body`: none

### `download.add`
`POST /api/v1/download/add`; policy effect: `external_side_effect`.
Purpose: Submit one torrent to MoviePilot's normal download workflow.
- `path_params`: none
- `query`: none
- `body`: `allow_unrecognized` (boolean; default `False`): Allow a download when MoviePilot cannot resolve a canonical media identity.; `downloader` (string|null): Configured downloader instance name.; `media_id` (string|null): Source-native media ID. Always pair it with the exact media_source returned by search.; `media_source` (MediaSource|null): Metadata source identifier. Preserve the exact value returned with media_id.; `music_type` (string(recording,album)|null): Music identity level: recording, album, or artist where supported.; `save_path` (string|null): Configured downloader-side save path for the download or subscription.; `torrent_in*` (TorrentInfo): Complete torrent candidate returned by search.results or search.torrents.

### `download.clients`
`GET /api/v1/download/clients`; policy effect: `safe_read`.
Purpose: List enabled downloader instance names and provider types without credentials.
- `response`: `data` remains a list; omitting both `page` and `count` keeps the complete legacy result. `collection.result_count` reports the returned items and `collection.total_count` reports the exact pre-pagination total. For counts or summaries, send `page=1,count=1`, read `collection.total_count`, and do not fall back to a database query because the item preview was truncated.
- `path_params`: none
- `query`: `count` (integer|null): Optional page size for a legacy full-list endpoint. Supplying page or count activates pagination; an omitted count then uses 50.; `page` (integer|null): Optional one-based page for a legacy full-list endpoint. Omit both page and count to keep the original unpaginated full result.
- `body`: none

### `download.history.delete`
`DELETE /api/v1/history/download`; policy effect: `destructive_write`.
Purpose: Delete one MoviePilot download-history record.
- `path_params`: none
- `query`: none
- `body`: `channel` (string|null): Message channel that originally submitted the download.; `date` (string|null): Record creation or completion timestamp used by the history item.; `download_hash` (string|null): Provider-native torrent hash associated with the record.; `episode_group` (string|null): TMDB episode-group identifier used for alternate episode ordering.; `episodes` (string|null): Episode-number expression recorded in history, such as E01-E03.; `id*` (integer): Persistent database identifier of the supplied record.; `image` (string|null): Image URL stored with the history record.; `media_category` (string|null): MoviePilot library category assigned to the media.; `media_id` (string|null): Source-native media ID. Always pair it with the exact media_source returned by search.; `media_source` (MediaSource|null): Metadata source identifier. Preserve the exact value returned with media_id.; `music_type` (string|null): Music identity level: recording, album, or artist where supported.; `note` (JsonData-Input|null): Structured auxiliary metadata stored with the record.; `path` (string|null): Storage or history path represented by this record.; `poster` (string|null): Poster image URL stored with the media or subscription.; `seasons` (string|null): Season-number expression recorded in history.; `title` (string|null): Media, torrent, subscription, or history title used by the operation.; `torrent_description` (string|null): Torrent release description recorded in download history.; `torrent_name` (string|null): Torrent release name recorded in download history.; `torrent_site` (string|null): Source site name recorded in download history.; `type` (string|null): MoviePilot media or storage item type required by the selected operation.; `userid` (string|null): Message-channel user ID recorded with download history.; `username` (string|null): MoviePilot or site username required by the selected operation.; `year` (string|null): Release or premiere year used to disambiguate the media title.

### `download.history.list`
`GET /api/v1/history/download`; policy effect: `safe_read`.
Purpose: Page MoviePilot download-history records in reverse chronological order.
- `response`: `data` remains a list and the endpoint's documented pagination or limit defaults remain in effect. `collection.result_count` reports the returned items and `collection.total_count` reports the exact total. For a count-only request, use the smallest valid page and read that metadata instead of querying the database after item truncation.
- `path_params`: none
- `query`: `count` (integer|null; default `30`): Maximum number of records to return on the requested page.; `page` (integer|null; default `1`): One-based result page number.
- `body`: none

### `download.paths`
`GET /api/v1/download/paths`; policy effect: `safe_read`.
Purpose: List configured downloader save-path URIs that may be passed to download.add.
- `response`: `data` remains a list; omitting both `page` and `count` keeps the complete legacy result. `collection.result_count` reports the returned items and `collection.total_count` reports the exact pre-pagination total. For counts or summaries, send `page=1,count=1`, read `collection.total_count`, and do not fall back to a database query because the item preview was truncated.
- `path_params`: none
- `query`: `count` (integer|null): Optional page size for a legacy full-list endpoint. Supplying page or count activates pagination; an omitted count then uses 50.; `page` (integer|null): Optional one-based page for a legacy full-list endpoint. Omit both page and count to keep the original unpaginated full result.
- `body`: none

### `download.tasks.active`
`GET /api/v1/download/`; policy effect: `safe_read`.
Purpose: List currently downloading MoviePilot tasks with their canonical media context.
- `response`: `data` remains a list; omitting both `page` and `count` keeps the complete legacy result. `collection.result_count` reports the returned items and `collection.total_count` reports the exact pre-pagination total. For counts or summaries, send `page=1,count=1`, read `collection.total_count`, and do not fall back to a database query because the item preview was truncated.
- `path_params`: none
- `query`: `count` (integer|null): Optional page size for a legacy full-list endpoint. Supplying page or count activates pagination; an omitted count then uses 50.; `name` (string|null): Human-readable name of the site, storage item, subscription, or rule group.; `page` (integer|null): Optional one-based page for a legacy full-list endpoint. Omit both page and count to keep the original unpaginated full result.
- `body`: none

### `filter.builtin`
`GET /api/v1/rule/builtin`; policy effect: `safe_read`.
Purpose: List built-in torrent filter rules.
- `path_params`: none
- `query`: `rule_ids` (array<string>|null): Exact built-in rule IDs to return. Repeat rule_ids in the query string; omit it to list every built-in rule.
- `body`: none

### `filter.custom`
`GET /api/v1/rule/custom`; policy effect: `safe_read`.
Purpose: List user-defined torrent filter rules.
- `path_params`: none
- `query`: `include_group_refs` (boolean; default `True`): Include custom rules referenced only through rule groups.; `rule_ids` (array<string>|null): Exact custom rule IDs to return. Repeat rule_ids in the query string; omit it to list every custom rule.
- `body`: none

### `filter.custom.add`
`POST /api/v1/rule/custom`; policy effect: `reversible_write`.
Purpose: Create one user-defined torrent filter rule.
- `path_params`: none
- `query`: none
- `body`: `exclude` (string|null): Regular expression or filter expression that rejects matching releases.; `include` (string|null): Regular expression or filter expression that a release must match.; `name*` (string): Human-readable name of the site, storage item, subscription, or rule group.; `publish_time` (string|null): Release-age filter expression for a custom filter rule.; `rule_id*` (string): Stable custom filter-rule ID.; `seeders` (string|null): Minimum seeder expression for a filter rule, or the torrent's seeder count.; `size_range` (string|null): Accepted torrent size range expression for a custom filter rule.

### `filter.custom.delete`
`DELETE /api/v1/rule/custom/{rule_id}`; policy effect: `destructive_write`.
Purpose: Delete one user-defined torrent filter rule.
- `path_params`: `rule_id*` (string): Stable custom filter-rule ID.
- `query`: none
- `body`: none

### `filter.custom.update`
`PUT /api/v1/rule/custom/{rule_id}`; policy effect: `reversible_write`.
Purpose: Update one user-defined torrent filter rule.
- `path_params`: `rule_id*` (string): Stable custom filter-rule ID.
- `query`: none
- `body`: `exclude` (string|null): Regular expression or filter expression that rejects matching releases.; `include` (string|null): Regular expression or filter expression that a release must match.; `name` (string|null): Human-readable name of the site, storage item, subscription, or rule group.; `new_rule_id` (string|null): Replacement stable ID for the existing custom filter rule.; `publish_time` (string|null): Release-age filter expression for a custom filter rule.; `seeders` (string|null): Minimum seeder expression for a filter rule, or the torrent's seeder count.; `size_range` (string|null): Accepted torrent size range expression for a custom filter rule.

### `filter.group.add`
`POST /api/v1/rule/groups`; policy effect: `reversible_write`.
Purpose: Create one named filter-rule group.
- `path_params`: none
- `query`: none
- `body`: `category` (string|null): MoviePilot media category or filter-group category, depending on the operation.; `media_type` (string|null): MoviePilot media type used to filter recommendations or rule groups.; `name*` (string): Human-readable name of the site, storage item, subscription, or rule group.; `rule_string*` (string): Ordered filter-rule expression stored in the group.

### `filter.group.delete`
`DELETE /api/v1/rule/groups/{name}`; policy effect: `destructive_write`.
Purpose: Delete one named filter-rule group.
- `path_params`: `name*` (string): Human-readable name of the site, storage item, subscription, or rule group.
- `query`: none
- `body`: none

### `filter.group.update`
`PUT /api/v1/rule/groups/{name}`; policy effect: `reversible_write`.
Purpose: Update or rename one named filter-rule group.
- `path_params`: `name*` (string): Human-readable name of the site, storage item, subscription, or rule group.
- `query`: none
- `body`: `category` (string|null): MoviePilot media category or filter-group category, depending on the operation.; `media_type` (string|null): MoviePilot media type used to filter recommendations or rule groups.; `new_name` (string|null): Replacement name for the existing filter-rule group.; `rule_string` (string|null): Ordered filter-rule expression stored in the group.

### `filter.groups`
`GET /api/v1/rule/groups`; policy effect: `safe_read`.
Purpose: List named filter-rule groups.
- `path_params`: none
- `query`: `group_names` (array<string>|null): Exact rule-group names to return. Repeat group_names in the query string; omit it to list every group.; `include_usage` (boolean; default `True`): Include the subscriptions or defaults that reference each rule group.
- `body`: none

### `filter.test`
`GET /api/v1/system/ruletest`; policy effect: `external_side_effect`.
Purpose: Test one title and optional subtitle against an exact named filter-rule group.
- `path_params`: none
- `query`: `rulegroup_name*` (string): Exact filter-rule group name returned by filter.groups.; `subtitle` (string|null): Optional subtitle text used together with title during media recognition.; `title*` (string): Media, torrent, subscription, or history title used by the operation.
- `body`: none

### `library.exists`
`GET /api/v1/mediaserver/exists`; policy effect: `safe_read`.
Purpose: Check configured media servers for one canonical media identity.
- `path_params`: none
- `query`: `media_id` (string|null): Source-native media ID. Always pair it with the exact media_source returned by search.; `media_source` (MediaSource|null): Metadata source identifier. Preserve the exact value returned with media_id.; `mtype` (string|null): MoviePilot media type or subscription-history category required by the operation.; `season` (integer|null): Season number used by the media, search, subscription, or transfer operation.; `title` (string|null): Media, torrent, subscription, or history title used by the operation.; `year` (string|null): Release or premiere year used to disambiguate the media title.
- `body`: none

### `library.latest`
`GET /api/v1/mediaserver/latest`; policy effect: `safe_read`.
Purpose: List recently added items from one configured media-server instance for the current user.
- `response`: `data` remains a list and `collection.result_count` reports the returned items. `collection.total_count` is omitted because this endpoint or its upstream source does not expose a total.
- `path_params`: none
- `query`: `count` (integer|null; default `20`): Maximum number of records to return on the requested page.; `server*` (string): Exact configured media-server instance name returned by the media-server instance list.
- `body`: none

### `media.categories`
`GET /api/v1/media/category`; policy effect: `safe_read`.
Purpose: Read the resolved automatic media-category mapping.
- `path_params`: none
- `query`: none
- `body`: none

### `media.category.config.get`
`GET /api/v1/media/category/config`; policy effect: `safe_read`.
Purpose: Read the complete automatic media-category strategy configuration.
- `path_params`: none
- `query`: none
- `body`: none

### `media.category.config.update`
`POST /api/v1/media/category/config`; policy effect: `reversible_write`.
Purpose: Replace the complete automatic media-category strategy configuration.
- `path_params`: none
- `query`: none
- `body`: `movie` (object|null; default `{}`): Automatic movie-category rules evaluated in order.; `tv` (object|null; default `{}`): Automatic TV-category rules evaluated in order.

### `media.detail`
`GET /api/v1/media/{media_id}`; policy effect: `safe_read`.
Purpose: Read canonical media details from one selected metadata source.
- `path_params`: `media_id*` (string): Source-native media ID. Always pair it with the exact media_source returned by search.
- `query`: `media_source*` (MediaSource): Metadata source identifier. Preserve the exact value returned with media_id.; `type_name*` (string): Explicit media type name used when source IDs alone are ambiguous.
- `body`: none

### `media.episode_group.seasons`
`GET /api/v1/media/group/seasons/{episode_group}`; policy effect: `safe_read`.
Purpose: List seasons defined by one exact TMDB episode-group identity.
- `response`: `data` remains a list; omitting both `page` and `count` keeps the complete legacy result. `collection.result_count` reports the returned items and `collection.total_count` reports the exact pre-pagination total. For counts or summaries, send `page=1,count=1`, read `collection.total_count`, and do not fall back to a database query because the item preview was truncated.
- `path_params`: `episode_group*` (string): TMDB episode-group identifier used for alternate episode ordering.
- `query`: `count` (integer|null): Optional page size for a legacy full-list endpoint. Supplying page or count activates pagination; an omitted count then uses 50.; `page` (integer|null): Optional one-based page for a legacy full-list endpoint. Omit both page and count to keep the original unpaginated full result.
- `body`: none

### `media.episode_groups`
`GET /api/v1/media/groups/{tmdbid}`; policy effect: `safe_read`.
Purpose: List alternate TMDB episode groups available for one TV media identity.
- `response`: `data` remains a list; omitting both `page` and `count` keeps the complete legacy result. `collection.result_count` reports the returned items and `collection.total_count` reports the exact pre-pagination total. For counts or summaries, send `page=1,count=1`, read `collection.total_count`, and do not fall back to a database query because the item preview was truncated.
- `path_params`: `tmdbid*` (integer): TMDB media ID returned by media search or detail.
- `query`: `count` (integer|null): Optional page size for a legacy full-list endpoint. Supplying page or count activates pagination; an omitted count then uses 50.; `page` (integer|null): Optional one-based page for a legacy full-list endpoint. Omit both page and count to keep the original unpaginated full result.
- `body`: none

### `media.episode_schedule`
`GET /api/v1/tmdb/{tmdbid}/{season}`; policy effect: `safe_read`.
Purpose: Read TMDB episode release information for one season.
- `response`: `data` remains a list; omitting both `page` and `count` keeps the complete legacy result. `collection.result_count` reports the returned items and `collection.total_count` reports the exact pre-pagination total. For counts or summaries, send `page=1,count=1`, read `collection.total_count`, and do not fall back to a database query because the item preview was truncated.
- `path_params`: `season*` (integer): Season number used by the media, search, subscription, or transfer operation.; `tmdbid*` (integer): TMDB media ID returned by media search or detail.
- `query`: `count` (integer|null): Optional page size for a legacy full-list endpoint. Supplying page or count activates pagination; an omitted count then uses 50.; `episode_group` (string|null): TMDB episode-group identifier used for alternate episode ordering.; `page` (integer|null): Optional one-based page for a legacy full-list endpoint. Omit both page and count to keep the original unpaginated full result.
- `body`: none

### `media.person.credits`
`GET /api/v1/{source}/person/credits/{person_id}`; policy effect: `safe_read`.
Purpose: Read one person's credits from the selected metadata source.
- `response`: `data` remains a list and `collection.result_count` reports the returned items. `collection.total_count` is omitted because this endpoint or its upstream source does not expose a total.
- `path_params`: `person_id*` (integer): Source-native person ID.; `source*` (string(douban,tmdb,bangumi,anilist)): Metadata source that owns the person ID.
- `query`: `count` (integer; default `20`; minimum `1`; maximum `50`): Page size used by Bangumi and AniList; other sources ignore it.; `page` (integer; default `1`; minimum `1`): One-based result page number.
- `body`: none

### `media.person.search`
`GET /api/v1/media/search`; policy effect: `safe_read`.
Purpose: Search people across selected metadata sources.
- `response`: `data` remains a list and `collection.result_count` reports the returned items. `collection.total_count` is omitted because this endpoint or its upstream source does not expose a total.
- `path_params`: none
- `query`: `count` (integer; default `8`): Maximum number of records to return on the requested page.; `media_source` (array<MediaSource>; default `[]`): Metadata source identifier. Preserve the exact value returned with media_id.; `page` (integer; default `1`): One-based result page number.; `title*` (string): Media, torrent, subscription, or history title used by the operation.; `type*` (string=person): Literal person, selecting person search instead of media search.
- `body`: none

### `media.recognize`
`GET /api/v1/media/recognize`; policy effect: `safe_read`.
Purpose: Recognize media identity from a title, subtitle, or custom rule context.
- `path_params`: none
- `query`: `custom_words` (string|null): Custom recognition or rename words applied to this media workflow.; `media_source` (MediaSource|null): Metadata source identifier. Preserve the exact value returned with media_id.; `subtitle` (string|null): Optional subtitle text used together with title during media recognition.; `title*` (string): Media, torrent, subscription, or history title used by the operation.
- `body`: none

### `media.recognize_file`
`GET /api/v1/media/recognize_file`; policy effect: `safe_read`.
Purpose: Recognize canonical media identity from one exact filename and optional path context.
- `path_params`: none
- `query`: `media_source` (MediaSource|null): Metadata source identifier. Preserve the exact value returned with media_id.; `path*` (string): Storage or history path represented by this record.
- `body`: none

### `media.scrape`
`POST /api/v1/media/scrape/{storage}`; policy effect: `external_side_effect`.
Purpose: Generate or refresh metadata for one storage item.
- `path_params`: `storage*` (string|null): Configured storage name or storage type used by the operation.
- `query`: `media_id` (string|null): Source-native media ID. Always pair it with the exact media_source returned by search.; `media_source` (MediaSource|null): Metadata source identifier. Preserve the exact value returned with media_id.; `music_type` (string|null): Music identity level: recording, album, or artist where supported.; `type_name` (MediaType|null): Explicit media type name used when source IDs alone are ambiguous.
- `body`: `basename` (string|null): Base filename without its parent path.; `children` (array<FileItem-Input>|null): Child storage items nested below this item.; `drive_id` (string|null): Provider-native storage drive identifier.; `extension` (string|null): Filename extension, including or excluding the leading dot as returned by storage.; `fileid` (string|null): Provider-native storage item identifier.; `modify_time` (number|null): Storage item modification timestamp.; `name` (string|null): Human-readable name of the site, storage item, subscription, or rule group.; `parent_fileid` (string|null): Provider-native identifier of the parent storage directory.; `path` (string|null; default `/`): Storage or history path represented by this record.; `pickcode` (string|null): 115 storage pickcode associated with the item.; `size` (integer|null): File or torrent size in bytes.; `storage` (string|null; default `local`): Configured storage name or storage type used by the operation.; `thumbnail` (string|null): Thumbnail URL returned by the storage provider.; `type` (string|null): MoviePilot media or storage item type required by the selected operation.; `url` (string|null): Site, storage, or torrent URL represented by this field.

### `media.search`
`GET /api/v1/media/search`; policy effect: `safe_read`.
Purpose: Search canonical media across selected metadata sources.
- `response`: `data` remains a list and `collection.result_count` reports the returned items. `collection.total_count` is omitted because this endpoint or its upstream source does not expose a total.
- `path_params`: none
- `query`: `count` (integer; default `8`): Maximum number of records to return on the requested page.; `media_source` (array<MediaSource>; default `[]`): Metadata source identifier. Preserve the exact value returned with media_id.; `page` (integer; default `1`): One-based result page number.; `title*` (string): Media, torrent, subscription, or history title used by the operation.; `type` (string|null; default `media`): MoviePilot media or storage item type required by the selected operation.
- `body`: none

### `media.seasons`
`GET /api/v1/media/seasons`; policy effect: `safe_read`.
Purpose: List seasons for one exact media identity or a title-and-year fallback.
- `response`: `data` remains a list; omitting both `page` and `count` keeps the complete legacy result. `collection.result_count` reports the returned items and `collection.total_count` reports the exact pre-pagination total. For counts or summaries, send `page=1,count=1`, read `collection.total_count`, and do not fall back to a database query because the item preview was truncated.
- `path_params`: none
- `query`: `count` (integer|null): Optional page size for a legacy full-list endpoint. Supplying page or count activates pagination; an omitted count then uses 50.; `media_id` (string|null): Source-native media ID. Always pair it with the exact media_source returned by search.; `media_source` (MediaSource|null): Metadata source identifier. Preserve the exact value returned with media_id.; `page` (integer|null): Optional one-based page for a legacy full-list endpoint. Omit both page and count to keep the original unpaginated full result.; `season` (integer): Season number used by the media, search, subscription, or transfer operation.; `title` (string|null): Media, torrent, subscription, or history title used by the operation.; `year` (string): Release or premiere year used to disambiguate the media title.
- `body`: none

### `media.sources`
`GET /api/v1/media/source`; policy effect: `safe_read`.
Purpose: List metadata sources currently registered for MoviePilot media operations.
- `response`: `data` remains a list; omitting both `page` and `count` keeps the complete legacy result. `collection.result_count` reports the returned items and `collection.total_count` reports the exact pre-pagination total. For counts or summaries, send `page=1,count=1`, read `collection.total_count`, and do not fall back to a database query because the item preview was truncated.
- `path_params`: none
- `query`: `count` (integer|null): Optional page size for a legacy full-list endpoint. Supplying page or count activates pagination; an omitted count then uses 50.; `page` (integer|null): Optional one-based page for a legacy full-list endpoint. Omit both page and count to keep the original unpaginated full result.
- `body`: none

### `music.album.get`
`GET /api/v1/music/album/{album_id}`; policy effect: `safe_read`.
Purpose: Read one album's details, tracks, releases, and aligned artist names and IDs.
- `path_params`: `album_id*` (string): Source-native album ID returned by music search, exploration, or artist-album browsing.
- `query`: `media_source` (MediaSource): Metadata source identifier. Preserve the exact value returned with media_id.
- `body`: none

### `music.album.related`
`GET /api/v1/music/album/{album_id}/related`; policy effect: `safe_read`.
Purpose: Browse albums related to one source-native album identity.
- `response`: `data` remains a list and `collection.result_count` reports the returned items. `collection.total_count` is omitted because this endpoint or its upstream source does not expose a total.
- `path_params`: `album_id*` (string): Source-native album ID returned by music search, exploration, or artist-album browsing.
- `query`: `count` (integer; default `24`; minimum `1`; maximum `100`): Maximum number of records to return on the requested page.; `media_source` (MediaSource): Metadata source identifier. Preserve the exact value returned with media_id.
- `body`: none

### `music.artist.albums`
`GET /api/v1/music/artist/{artist_id}/albums`; policy effect: `safe_read`.
Purpose: Browse one artist's albums, singles, EPs, or another exact release-group type.
- `response`: `data` remains a list and `collection.result_count` reports the returned items. `collection.total_count` is omitted because this endpoint or its upstream source does not expose a total.
- `path_params`: `artist_id*` (string): Source-native artist ID returned by music search or an album detail response.
- `query`: `album_type` (string|null): MusicBrainz release-group type filter: album, single, ep, broadcast, other, compilation, soundtrack, live, or remix.; `count` (integer; default `30`; minimum `1`; maximum `100`): Maximum number of records to return on the requested page.; `media_source` (MediaSource): Metadata source identifier. Preserve the exact value returned with media_id.; `page` (integer; default `1`; minimum `1`): One-based result page number.
- `body`: none

### `music.artist.get`
`GET /api/v1/music/artist/{artist_id}`; policy effect: `safe_read`.
Purpose: Read one artist's canonical details from the selected music metadata source.
- `path_params`: `artist_id*` (string): Source-native artist ID returned by music search or an album detail response.
- `query`: `media_source` (MediaSource): Metadata source identifier. Preserve the exact value returned with media_id.
- `body`: none

### `music.artist.related`
`GET /api/v1/music/artist/{artist_id}/related`; policy effect: `safe_read`.
Purpose: Browse artists related to one source-native artist identity.
- `response`: `data` remains a list and `collection.result_count` reports the returned items. `collection.total_count` is omitted because this endpoint or its upstream source does not expose a total.
- `path_params`: `artist_id*` (string): Source-native artist ID returned by music search or an album detail response.
- `query`: `count` (integer; default `24`; minimum `1`; maximum `100`): Maximum number of records to return on the requested page.; `media_source` (MediaSource): Metadata source identifier. Preserve the exact value returned with media_id.
- `body`: none

### `music.cache.clear`
`DELETE /api/v1/music/cache`; policy effect: `destructive_write`.
Purpose: Clear the complete administrator-only MusicBrainz recognition cache.
- `path_params`: none
- `query`: none
- `body`: none

### `music.cache.delete`
`DELETE /api/v1/music/cache/{cache_key}`; policy effect: `destructive_write`.
Purpose: Delete one administrator-only MusicBrainz recognition-cache entry by exact key.
- `path_params`: `cache_key*` (string): Exact recognition-cache key returned by music.cache.get.
- `query`: none
- `body`: none

### `music.cache.get`
`GET /api/v1/music/cache`; policy effect: `safe_read`.
Purpose: Inspect the administrator-only MusicBrainz recognition cache and summary counts.
- `path_params`: none
- `query`: none
- `body`: none

### `music.explore`
`GET /api/v1/music/explore`; policy effect: `safe_read`.
Purpose: Browse MusicBrainz charts or fresh releases, or Douban Music tag categories.
- `response`: `data` remains a list and `collection.result_count` reports the returned items. `collection.total_count` is omitted because this endpoint or its upstream source does not expose a total.
- `path_params`: none
- `query`: `count` (integer; default `30`; minimum `1`; maximum `100`): Maximum number of records to return on the requested page.; `days` (integer; default `14`; minimum `1`; maximum `90`): Fresh-release lookback/lookahead window, from 1 through the endpoint maximum.; `douban_sort` (string; default `U`): Douban Music order: U comprehensive, S rating, R newest, or O hottest.; `entity` (string; default `recording`): Chart entity: recording for tracks or album for release groups. Fresh results are albums.; `future` (boolean; default `True`): Include releases after today in fresh mode.; `media_source` (MediaSource): Music exploration source. Use musicbrainz for chart/fresh modes or doubanmusic for tag browsing.; `min_listen_count` (integer; default `0`; minimum `0`): Minimum ListenBrainz listen count in chart mode.; `mode` (string; default `chart`): MusicBrainz mode: chart reads listening charts; fresh reads new album releases.; `page` (integer; default `1`; minimum `1`): One-based result page number.; `past` (boolean; default `True`): Include releases before today in fresh mode.; `range_name` (string; default `this_month`): ListenBrainz chart range: this_week, this_month, this_year, week, month, or year.; `sort` (string; default `release_date`): Fresh-release order accepted by the current ListenBrainz implementation.; `sort_by` (string; default `listen_count.desc`): ListenBrainz chart order: listen_count.desc or listen_count.asc.; `tags` (string; default ``): Comma-separated Douban Music tags used only when media_source is doubanmusic.; `with_cover` (boolean; default `False`): Keep only results with cover artwork when true.
- `body`: none

### `music.recognize`
`POST /api/v1/music/recognize`; policy effect: `safe_read`.
Purpose: Resolve one recording or album from an exact music source and source-native ID.
- `path_params`: none
- `query`: none
- `body`: `media_id*` (string): Source-native media ID. Always pair it with the exact media_source returned by search.; `media_source*` (MediaSource): Metadata source identifier. Preserve the exact value returned with media_id.; `music_type` (string(recording,album)|null): Music identity level: recording, album, or artist where supported.

### `plugin.capabilities`
`GET /api/v1/plugin/runtime/capabilities`; policy effect: `safe_read`.
Purpose: Inspect the runtime capabilities exposed by installed plugins.
- `path_params`: none
- `query`: `plugin_id` (string|null): Exact installed or marketplace plugin ID.
- `body`: none

### `plugin.clone`
`POST /api/v1/plugin/clone/{plugin_id}`; policy effect: `external_side_effect`.
Purpose: Create a configurable clone of one installed plugin.
- `path_params`: `plugin_id*` (string): Exact installed or marketplace plugin ID.
- `query`: none
- `body`: `description` (string; default ``): Human-readable media, torrent, or subscription description.; `icon` (string|null): Icon name or URL used by a workflow, network target, plugin, or category.; `name` (string; default ``): Human-readable name of the site, storage item, subscription, or rule group.; `suffix*` (string; minimum length `1`): File suffix or extension matched by an automatic category rule.; `version` (string|null): Plugin release or schema version selected by the operation.

### `plugin.config.get`
`GET /api/v1/plugin/form/{plugin_id}`; policy effect: `safe_read`.
Purpose: Read one loaded plugin's configuration form and its defaults merged with saved values.
- `path_params`: `plugin_id*` (string): Exact installed or marketplace plugin ID.
- `query`: none
- `body`: none

### `plugin.config.update`
`PUT /api/v1/plugin/{plugin_id}`; policy effect: `reversible_write`.
Purpose: Replace one installed plugin's complete configuration and apply it immediately.
- `path_params`: `plugin_id*` (string): Exact installed or marketplace plugin ID.
- `query`: none
- `body*` (object): Complete plugin configuration object. First call plugin.config.get, copy its returned model, change only the intended keys, and submit the full resulting object. Omit a key only when it must be removed. A successful update reinitializes the plugin and refreshes commands, jobs, and routes.

### `plugin.data`
`GET /api/v1/plugin/runtime/{plugin_id}/data`; policy effect: `safe_read`.
Purpose: Read a bounded preview of one plugin's persisted data.
- `path_params`: `plugin_id*` (string): Exact installed or marketplace plugin ID.
- `query`: `key` (string|null): Optional exact plugin data key used to narrow the returned preview.; `max_chars` (integer|null): Maximum number of serialized plugin-data characters to return.
- `body`: none

### `plugin.folder.create`
`POST /api/v1/plugin/folders/{folder_name}`; policy effect: `reversible_write`.
Purpose: Create one named plugin folder.
- `path_params`: `folder_name*` (string): Exact plugin folder name returned by plugin.folders.get.
- `query`: none
- `body`: none

### `plugin.folder.delete`
`DELETE /api/v1/plugin/folders/{folder_name}`; policy effect: `destructive_write`.
Purpose: Delete one named plugin folder without uninstalling its plugins.
- `path_params`: `folder_name*` (string): Exact plugin folder name returned by plugin.folders.get.
- `query`: none
- `body`: none

### `plugin.folder.plugins.update`
`PUT /api/v1/plugin/folders/{folder_name}/plugins`; policy effect: `reversible_write`.
Purpose: Replace the ordered plugin IDs assigned to one named plugin folder.
- `path_params`: `folder_name*` (string): Exact plugin folder name returned by plugin.folders.get.
- `query`: none
- `body*` (array<string>): Request value for plugin.folder.plugins.update. Replace the ordered plugin IDs assigned to one named plugin folder. Use the exact type and fields below.

### `plugin.folders.get`
`GET /api/v1/plugin/folders`; policy effect: `safe_read`.
Purpose: Read the complete administrator plugin-folder grouping configuration.
- `path_params`: none
- `query`: none
- `body`: none

### `plugin.folders.update`
`POST /api/v1/plugin/folders`; policy effect: `reversible_write`.
Purpose: Replace the complete administrator plugin-folder grouping configuration.
- `path_params`: none
- `query`: none
- `body`: `PluginFoldersData` with no direct fields

### `plugin.history`
`GET /api/v1/plugin/history/{plugin_id}`; policy effect: `safe_read`.
Purpose: Read marketplace update notes and history for one plugin.
- `path_params`: `plugin_id*` (string): Exact installed or marketplace plugin ID.
- `query`: `force` (boolean; default `True`): Force a marketplace refresh or plugin installation when true.
- `body`: none

### `plugin.install`
`GET /api/v1/plugin/install/{plugin_id}`; policy effect: `external_side_effect`.
Purpose: Install or update one plugin from an approved source.
- `path_params`: `plugin_id*` (string): Exact installed or marketplace plugin ID.
- `query`: `force` (boolean|null; default `False`): Force a marketplace refresh or plugin installation when true.; `release_version` (string|null): Exact plugin release version to install when one is required.; `repo_url` (string|null; default ``): Approved plugin repository URL used to resolve the installation source.
- `body`: none

### `plugin.installed`
`GET /api/v1/plugin/`; policy effect: `safe_read`.
Purpose: List installed plugins and their runtime status.
- `response`: `data` remains a list; omitting both `page` and `count` keeps the complete legacy result. `collection.result_count` reports the returned items and `collection.total_count` reports the exact pre-pagination total. For counts or summaries, send `page=1,count=1`, read `collection.total_count`, and do not fall back to a database query because the item preview was truncated.
- `path_params`: none
- `query`: `count` (integer|null): Optional page size for a legacy full-list endpoint. Supplying page or count activates pagination; an omitted count then uses 50.; `force` (boolean; default `False`): Force a marketplace refresh or plugin installation when true.; `max_results` (integer|null): Optional upper bound on plugin catalog results, from 1 to 200; omit it for the complete catalog.; `page` (integer|null): Optional one-based page for a legacy full-list endpoint. Omit both page and count to keep the original unpaginated full result.; `query` (string|null): Optional case-insensitive keyword matched against plugin ID, name, description, and author.; `state*` (string=installed): Literal installed, selecting only installed plugin catalog entries.
- `body`: none

### `plugin.market`
`GET /api/v1/plugin/`; policy effect: `safe_read`.
Purpose: List plugins available from configured marketplaces.
- `response`: `data` remains a list; omitting both `page` and `count` keeps the complete legacy result. `collection.result_count` reports the returned items and `collection.total_count` reports the exact pre-pagination total. For counts or summaries, send `page=1,count=1`, read `collection.total_count`, and do not fall back to a database query because the item preview was truncated.
- `path_params`: none
- `query`: `count` (integer|null): Optional page size for a legacy full-list endpoint. Supplying page or count activates pagination; an omitted count then uses 50.; `force` (boolean; default `False`): Force a marketplace refresh or plugin installation when true.; `max_results` (integer|null): Optional upper bound on plugin catalog results, from 1 to 200; omit it for the complete catalog.; `page` (integer|null): Optional one-based page for a legacy full-list endpoint. Omit both page and count to keep the original unpaginated full result.; `query` (string|null): Optional case-insensitive keyword matched against plugin ID, name, description, and author.; `state*` (string=market): Literal market, selecting only market plugin catalog entries.
- `body`: none

### `plugin.market.sync_wiki`
`POST /api/v1/system/setting/PLUGIN_MARKET/sync-wiki`; policy effect: `external_side_effect`.
Purpose: Refresh the configured plugin marketplace repositories from the MoviePilot Wiki.
- `path_params`: none
- `query`: none
- `body` (PluginMarketSyncRequest|null): Request value for plugin.market.sync_wiki. Refresh the configured plugin marketplace repositories from the MoviePilot Wiki. Use the exact type and fields below.

### `plugin.rating`
`GET /api/v1/plugin/rating/{plugin_id}`; policy effect: `safe_read`.
Purpose: Read the current aggregate rating for one plugin.
- `path_params`: `plugin_id*` (string): Exact installed or marketplace plugin ID.
- `query`: none
- `body`: none

### `plugin.rating.submit`
`POST /api/v1/plugin/rating/{plugin_id}`; policy effect: `external_side_effect`.
Purpose: Submit or replace the current user's rating for one plugin.
- `path_params`: `plugin_id*` (string): Exact installed or marketplace plugin ID.
- `query`: none
- `body`: `rating*` (number; minimum `0.1`; maximum `5.0`): Numeric plugin rating accepted by the endpoint's declared bounds.

### `plugin.ratings`
`GET /api/v1/plugin/rating`; policy effect: `safe_read`.
Purpose: Read aggregate ratings for a requested plugin set.
- `path_params`: none
- `query`: `plugin_ids` (string|null): Exact plugin IDs whose aggregate ratings should be returned.
- `body`: none

### `plugin.releases`
`GET /api/v1/plugin/releases/{plugin_id}`; policy effect: `safe_read`.
Purpose: List available release versions for one plugin source.
- `path_params`: `plugin_id*` (string): Exact installed or marketplace plugin ID.
- `query`: `force` (boolean; default `False`): Force a marketplace refresh or plugin installation when true.; `repo_url` (string|null; default ``): Approved plugin repository URL used to resolve the installation source.
- `body`: none

### `plugin.reload`
`GET /api/v1/plugin/reload/{plugin_id}`; policy effect: `external_side_effect`.
Purpose: Reload one installed plugin into the running process.
- `path_params`: `plugin_id*` (string): Exact installed or marketplace plugin ID.
- `query`: none
- `body`: none

### `plugin.reset`
`GET /api/v1/plugin/reset/{plugin_id}`; policy effect: `destructive_write`.
Purpose: Delete one plugin's saved configuration and data, then restore its default runtime state.
- `path_params`: `plugin_id*` (string): Exact installed or marketplace plugin ID.
- `query`: none
- `body`: none

### `plugin.runtime.status`
`GET /api/v1/plugin/runtime`; policy effect: `safe_read`.
Purpose: Read plugin runtime convergence, loading, and failure state.
- `path_params`: none
- `query`: none
- `body`: none

### `plugin.source.change`
`POST /api/v1/plugin/source/{plugin_id}`; policy effect: `external_side_effect`.
Purpose: Switch an installed plugin to one explicitly selected online source revision.
- `path_params`: `plugin_id*` (string): Exact installed or marketplace plugin ID.
- `query`: none
- `body`: `expected_revision*` (integer; minimum `1.0`): Exact current plugin source-identity revision returned by plugin.source.options.; `release_version` (string|null): Exact plugin release version to install when one is required.; `repo_url*` (string; minimum length `1`): Approved plugin repository URL used to resolve the installation source.

### `plugin.source.install`
`POST /api/v1/plugin/source/{plugin_id}/install`; policy effect: `external_side_effect`.
Purpose: Install an unbound plugin from one explicitly selected online source.
- `path_params`: `plugin_id*` (string): Exact installed or marketplace plugin ID.
- `query`: none
- `body`: `force` (boolean; default `False`): Force a marketplace refresh or plugin installation when true.; `release_version` (string|null): Exact plugin release version to install when one is required.; `repo_url*` (string; minimum length `1`): Approved plugin repository URL used to resolve the installation source.

### `plugin.source.options`
`GET /api/v1/plugin/source/{plugin_id}`; policy effect: `safe_read`.
Purpose: Inspect source candidates and the current immutable source identity before installation or source change.
- `path_params`: `plugin_id*` (string): Exact installed or marketplace plugin ID.
- `query`: none
- `body`: none

### `plugin.statistics`
`GET /api/v1/plugin/statistic`; policy effect: `safe_read`.
Purpose: Read public installation statistics for plugins.
- `path_params`: none
- `query`: none
- `body`: none

### `plugin.uninstall`
`DELETE /api/v1/plugin/{plugin_id}`; policy effect: `destructive_write`.
Purpose: Uninstall one plugin and remove it from the installed set.
- `path_params`: `plugin_id*` (string): Exact installed or marketplace plugin ID.
- `query`: none
- `body`: none

### `recommendation.list`
`GET /api/v1/recommend/agent`; policy effect: `safe_read`.
Purpose: Read personalized media or music recommendations.
- `response`: `data` remains a list and `collection.result_count` reports the returned items. `collection.total_count` is omitted because this endpoint or its upstream source does not expose a total.
- `path_params`: none
- `query`: `days` (integer; default `14`): Recommendation time window in days.; `fresh_sort` (string; default `release_date`): Freshness ordering used by the recommendation source.; `future` (boolean; default `True`): Include future recommendation periods when supported.; `media_type` (string; default `all`): MoviePilot media type used to filter recommendations or rule groups.; `min_listen_count` (integer; default `0`): Minimum listen count required for a music recommendation.; `music_type` (string|null): Music identity level: recording, album, or artist where supported.; `page` (integer; default `1`): One-based result page number.; `past` (boolean; default `True`): Include past recommendation periods when supported.; `range_name` (string; default `this_month`): Named recommendation time range.; `sort_by` (string; default `listen_count.desc`): Recommendation field used for ordering results.; `source` (string; default `tmdb_trending`): Exact metadata or recommendation source selected by the operation.; `with_cover` (boolean; default `False`): Require recommendation results to include cover artwork.
- `body`: none

### `scheduler.list`
`GET /api/v1/dashboard/schedule`; policy effect: `safe_read`.
Purpose: List registered scheduler jobs and their current state.
- `response`: `data` remains a list; omitting both `page` and `count` keeps the complete legacy result. `collection.result_count` reports the returned items and `collection.total_count` reports the exact pre-pagination total. For counts or summaries, send `page=1,count=1`, read `collection.total_count`, and do not fall back to a database query because the item preview was truncated.
- `path_params`: none
- `query`: `count` (integer|null): Optional page size for a legacy full-list endpoint. Supplying page or count activates pagination; an omitted count then uses 50.; `page` (integer|null): Optional one-based page for a legacy full-list endpoint. Omit both page and count to keep the original unpaginated full result.
- `body`: none

### `scheduler.progress`
`GET /api/v1/dashboard/schedule/{job_id}/progress`; policy effect: `safe_read`.
Purpose: Read current progress for one exact scheduler job.
- `path_params`: `job_id*` (string): Exact scheduler job ID returned by scheduler.list.
- `query`: none
- `body`: none

### `scheduler.run`
`GET /api/v1/system/runscheduler`; policy effect: `external_side_effect`.
Purpose: Run one registered scheduler job immediately.
- `path_params`: none
- `query`: `jobid*` (string): Exact scheduler job ID returned by scheduler.list.
- `body`: none

### `search.recommend`
`POST /api/v1/search/recommend`; policy effect: `external_side_effect`.
Purpose: Use the configured recommendation model to rank or recommend torrent search results.
- `path_params`: none
- `query`: none
- `body`: `check_only` (boolean; default `False`): Validate or preview the recommendation without applying search-result filtering.; `filtered_indices` (array<integer>|null): Zero-based search-result indices selected by the recommendation model.; `force` (boolean; default `False`): Force a marketplace refresh or plugin installation when true.

### `search.results`
`GET /api/v1/search/last/context`; policy effect: `safe_read`.
Purpose: Read the most recent torrent-search context and result set.
- `path_params`: none
- `query`: none
- `body`: none

### `search.title`
`GET /api/v1/search/title`; policy effect: `external_side_effect`.
Purpose: Search torrent sites directly from a free-form title and optional media filters.
- `response`: `data` remains a list and `collection.result_count` reports the returned items. `collection.total_count` is omitted because this endpoint or its upstream source does not expose a total.
- `path_params`: none
- `query`: `keyword` (string|null): Case-insensitive substring used to discover settings or filter storage entries.; `mtype` (string|null): MoviePilot media type or subscription-history category required by the operation.; `page` (integer|null; default `0`): One-based result page number.; `sites` (string|null): Exact site IDs included in the search or subscription scope.
- `body`: none

### `search.torrents`
`GET /api/v1/search/media/{media_id}`; policy effect: `safe_read`.
Purpose: Search torrent sites for one canonical media identity.
- `response`: `data` remains a list; omitting both `page` and `count` keeps the complete legacy result. `collection.result_count` reports the returned items and `collection.total_count` reports the exact pre-pagination total. For counts or summaries, send `page=1,count=1`, read `collection.total_count`, and do not fall back to a database query because the item preview was truncated.
- `path_params`: `media_id*` (string): Source-native media ID. Always pair it with the exact media_source returned by search.
- `query`: `area` (string|null; default `title`): Optional region filter applied by the torrent search workflow.; `count` (integer|null): Optional page size for a legacy full-list endpoint. Supplying page or count activates pagination; an omitted count then uses 50.; `media_source*` (MediaSource): Metadata source identifier. Preserve the exact value returned with media_id.; `mtype` (string|null): MoviePilot media type or subscription-history category required by the operation.; `music_type` (string|null): Music identity level: recording, album, or artist where supported.; `page` (integer|null): Optional one-based page for a legacy full-list endpoint. Omit both page and count to keep the original unpaginated full result.; `season` (string|null): Season number used by the media, search, subscription, or transfer operation.; `sites` (string|null): Exact site IDs included in the search or subscription scope.
- `body`: none

### `site.add`
`POST /api/v1/site/`; policy effect: `reversible_write`.
Purpose: Create one configured site with its complete authentication and search settings.
- `path_params`: none
- `query`: none
- `body`: `apikey` (string|null): Site API key used by sites that support API-key authentication.; `cookie` (string|null): Site authentication cookie. Treat this value as a secret.; `domain` (string|null): Site hostname or domain used for matching and requests.; `downloader` (string|null): Configured downloader instance name.; `filter` (string|null): Named filter rule or rule expression applied to this site or subscription.; `id` (integer|null): Persistent database identifier of the supplied record.; `is_active` (boolean|null; default `True`): Whether the configured site is enabled.; `limit_count` (integer|null): Maximum number of site requests allowed in one rate-limit interval.; `limit_interval` (integer|null): Number of requests in the site's rate-limit window.; `limit_seconds` (integer|null): Site rate-limit window length in seconds.; `name` (string|null): Human-readable name of the site, storage item, subscription, or rule group.; `note` (JsonData-Input|null): Structured auxiliary metadata stored with the record.; `pri` (integer|null; default `0`): Site search priority; lower or higher ordering follows the existing site API convention.; `proxy` (integer|null; default `0`): Whether the site uses MoviePilot's configured proxy.; `public` (integer|null; default `0`): Whether the site is treated as a public indexer.; `render` (integer|null; default `0`): Whether site requests require browser rendering.; `rss` (string|null): Site RSS feed URL.; `timeout` (integer|null; default `15`): Per-request site timeout in seconds.; `token` (string|null): Site authentication token. Treat this value as a secret.; `ua` (string|null): Site User-Agent string used for authenticated requests.; `url` (string|null): Site, storage, or torrent URL represented by this field.

### `site.auth.options`
`GET /api/v1/site/auth`; policy effect: `safe_read`.
Purpose: List site-account authentication providers and their required input definitions.
- `path_params`: none
- `query`: none
- `body`: none

### `site.authenticate`
`POST /api/v1/site/auth`; policy effect: `external_side_effect`.
Purpose: Authenticate a supported site account and persist the resulting site authorization state.
- `path_params`: none
- `query`: none
- `body`: `params` (object|null): Provider-defined JSON parameters for the selected authentication or storage action.; `site` (string|null): Source site identifier associated with the torrent result.

### `site.category`
`GET /api/v1/site/category/{site_id}`; policy effect: `safe_read`.
Purpose: List torrent categories supported by one configured site.
- `response`: `data` remains a list; omitting both `page` and `count` keeps the complete legacy result. `collection.result_count` reports the returned items and `collection.total_count` reports the exact pre-pagination total. For counts or summaries, send `page=1,count=1`, read `collection.total_count`, and do not fall back to a database query because the item preview was truncated.
- `path_params`: `site_id*` (integer): Persistent site ID returned by site.list.
- `query`: `count` (integer|null): Optional page size for a legacy full-list endpoint. Supplying page or count activates pagination; an omitted count then uses 50.; `page` (integer|null): Optional one-based page for a legacy full-list endpoint. Omit both page and count to keep the original unpaginated full result.
- `body`: none

### `site.cookie.update`
`POST /api/v1/site/cookie/{site_id}`; policy effect: `reversible_write`.
Purpose: Log in to one site and refresh its stored authentication cookie.
- `path_params`: `site_id*` (integer): Persistent site ID returned by site.list.
- `query`: none
- `body`: `code` (string|null): Two-factor verification code or site-specific authentication secret.; `password*` (string): Site login password. Treat this value as a secret.; `username*` (string): MoviePilot or site username required by the selected operation.

### `site.cookiecloud.sync`
`GET /api/v1/site/cookiecloud`; policy effect: `external_side_effect`.
Purpose: Start a CookieCloud synchronization of configured sites.
- `path_params`: none
- `query`: none
- `body`: none

### `site.delete`
`DELETE /api/v1/site/{site_id}`; policy effect: `destructive_write`.
Purpose: Delete one configured site by persistent site ID.
- `path_params`: `site_id*` (integer): Persistent site ID returned by site.list.
- `query`: none
- `body`: none

### `site.list`
`GET /api/v1/site/agent`; policy effect: `safe_read`.
Purpose: List configured sites with status/name filters; authentication fields are returned only to a superuser.
- `response`: `data` remains a list; omitting both `page` and `count` keeps the complete legacy result. `collection.result_count` reports the returned items and `collection.total_count` reports the exact pre-pagination total. For counts or summaries, send `page=1,count=1`, read `collection.total_count`, and do not fall back to a database query because the item preview was truncated.
- `path_params`: none
- `query`: `count` (integer|null): Optional page size for a legacy full-list endpoint. Supplying page or count activates pagination; an omitted count then uses 50.; `name` (string|null): Human-readable name of the site, storage item, subscription, or rule group.; `page` (integer|null): Optional one-based page for a legacy full-list endpoint. Omit both page and count to keep the original unpaginated full result.; `status` (string(active,inactive,all); default `all`): Transfer success status used to filter history or describe a record.
- `body`: none

### `site.mapping`
`GET /api/v1/site/mapping`; policy effect: `safe_read`.
Purpose: Read the configured site-domain to site-name mapping.
- `path_params`: none
- `query`: none
- `body`: none

### `site.priorities.update`
`POST /api/v1/site/priorities`; policy effect: `reversible_write`.
Purpose: Replace priorities for the supplied configured site IDs.
- `path_params`: none
- `query`: none
- `body*` (array<SitePriorityUpdate>): Request value for site.priorities.update. Replace priorities for the supplied configured site IDs. Use the exact type and fields below.

### `site.reset`
`GET /api/v1/site/reset`; policy effect: `destructive_write`.
Purpose: Delete all configured sites and start a fresh CookieCloud synchronization.
- `path_params`: none
- `query`: none
- `body`: none

### `site.resource`
`GET /api/v1/site/resource/{site_id}`; policy effect: `external_side_effect`.
Purpose: Browse torrent resources from one configured site with category and keyword filters.
- `response`: `data` remains a list and `collection.result_count` reports the returned items. `collection.total_count` is omitted because this endpoint or its upstream source does not expose a total.
- `path_params`: `site_id*` (integer): Persistent site ID returned by site.list.
- `query`: `cat` (string|null): Exact site category identifier returned by site.category.; `keyword` (string|null): Case-insensitive substring used to discover settings or filter storage entries.; `mtype` (string|null): MoviePilot media type or subscription-history category required by the operation.; `page` (integer|null; default `0`): One-based result page number.
- `body`: none

### `site.rss`
`GET /api/v1/site/rss`; policy effect: `safe_read`.
Purpose: List configured sites selected for RSS subscription processing.
- `response`: `data` remains a list; omitting both `page` and `count` keeps the complete legacy result. `collection.result_count` reports the returned items and `collection.total_count` reports the exact pre-pagination total. For counts or summaries, send `page=1,count=1`, read `collection.total_count`, and do not fall back to a database query because the item preview was truncated.
- `path_params`: none
- `query`: `count` (integer|null): Optional page size for a legacy full-list endpoint. Supplying page or count activates pagination; an omitted count then uses 50.; `page` (integer|null): Optional one-based page for a legacy full-list endpoint. Omit both page and count to keep the original unpaginated full result.
- `body`: none

### `site.searchable`
`GET /api/v1/site/media/{media_type}`; policy effect: `safe_read`.
Purpose: List active configured sites supporting one exact media type.
- `response`: `data` remains a list; omitting both `page` and `count` keeps the complete legacy result. `collection.result_count` reports the returned items and `collection.total_count` reports the exact pre-pagination total. For counts or summaries, send `page=1,count=1`, read `collection.total_count`, and do not fall back to a database query because the item preview was truncated.
- `path_params`: `media_type*` (string): MoviePilot media type used to filter recommendations or rule groups.
- `query`: `count` (integer|null): Optional page size for a legacy full-list endpoint. Supplying page or count activates pagination; an omitted count then uses 50.; `page` (integer|null): Optional one-based page for a legacy full-list endpoint. Omit both page and count to keep the original unpaginated full result.
- `body`: none

### `site.statistic`
`GET /api/v1/site/statistic/{site_url}`; policy effect: `safe_read`.
Purpose: Read account and traffic statistics for one exact configured site domain.
- `path_params`: `site_url*` (string): Configured site URL or hostname used to select one site's statistics.
- `query`: none
- `body`: none

### `site.statistics`
`GET /api/v1/site/statistic`; policy effect: `safe_read`.
Purpose: Read the latest account and traffic statistics for all configured sites.
- `response`: `data` remains a list; omitting both `page` and `count` keeps the complete legacy result. `collection.result_count` reports the returned items and `collection.total_count` reports the exact pre-pagination total. For counts or summaries, send `page=1,count=1`, read `collection.total_count`, and do not fall back to a database query because the item preview was truncated.
- `path_params`: none
- `query`: `count` (integer|null): Optional page size for a legacy full-list endpoint. Supplying page or count activates pagination; an omitted count then uses 50.; `page` (integer|null): Optional one-based page for a legacy full-list endpoint. Omit both page and count to keep the original unpaginated full result.
- `body`: none

### `site.supporting`
`GET /api/v1/site/supporting`; policy effect: `safe_read`.
Purpose: List indexer definitions supported by the installed MoviePilot resources.
- `path_params`: none
- `query`: none
- `body`: none

### `site.test`
`GET /api/v1/site/test/{site_id}`; policy effect: `safe_read`.
Purpose: Test connectivity and authentication for one configured site.
- `path_params`: `site_id*` (integer): Persistent site ID returned by site.list.
- `query`: none
- `body`: none

### `site.update`
`PUT /api/v1/site/`; policy effect: `reversible_write`.
Purpose: Update one configured site's complete settings.
- `path_params`: none
- `query`: none
- `body`: `apikey` (string|null): Site API key used by sites that support API-key authentication.; `cookie` (string|null): Site authentication cookie. Treat this value as a secret.; `domain` (string|null): Site hostname or domain used for matching and requests.; `downloader` (string|null): Configured downloader instance name.; `filter` (string|null): Named filter rule or rule expression applied to this site or subscription.; `id` (integer|null): Persistent database identifier of the supplied record.; `is_active` (boolean|null; default `True`): Whether the configured site is enabled.; `limit_count` (integer|null): Maximum number of site requests allowed in one rate-limit interval.; `limit_interval` (integer|null): Number of requests in the site's rate-limit window.; `limit_seconds` (integer|null): Site rate-limit window length in seconds.; `name` (string|null): Human-readable name of the site, storage item, subscription, or rule group.; `note` (JsonData-Input|null): Structured auxiliary metadata stored with the record.; `pri` (integer|null; default `0`): Site search priority; lower or higher ordering follows the existing site API convention.; `proxy` (integer|null; default `0`): Whether the site uses MoviePilot's configured proxy.; `public` (integer|null; default `0`): Whether the site is treated as a public indexer.; `render` (integer|null; default `0`): Whether site requests require browser rendering.; `rss` (string|null): Site RSS feed URL.; `timeout` (integer|null; default `15`): Per-request site timeout in seconds.; `token` (string|null): Site authentication token. Treat this value as a secret.; `ua` (string|null): Site User-Agent string used for authenticated requests.; `url` (string|null): Site, storage, or torrent URL represented by this field.

### `site.userdata`
`GET /api/v1/site/userdata/{site_id}`; policy effect: `safe_read`.
Purpose: Read the latest account statistics collected from one site.
- `response`: `data` remains a list; omitting both `page` and `count` keeps the complete legacy result. `collection.result_count` reports the returned items and `collection.total_count` reports the exact pre-pagination total. For counts or summaries, send `page=1,count=1`, read `collection.total_count`, and do not fall back to a database query because the item preview was truncated.
- `path_params`: `site_id*` (integer): Persistent site ID returned by site.list.
- `query`: `count` (integer|null): Optional page size for a legacy full-list endpoint. Supplying page or count activates pagination; an omitted count then uses 50.; `page` (integer|null): Optional one-based page for a legacy full-list endpoint. Omit both page and count to keep the original unpaginated full result.; `workdate` (string|null): Date used when retrieving one site's historical user statistics.
- `body`: none

### `site.userdata.latest`
`GET /api/v1/site/userdata/latest`; policy effect: `safe_read`.
Purpose: Read the latest collected account statistics for every configured site.
- `response`: `data` remains a list; omitting both `page` and `count` keeps the complete legacy result. `collection.result_count` reports the returned items and `collection.total_count` reports the exact pre-pagination total. For counts or summaries, send `page=1,count=1`, read `collection.total_count`, and do not fall back to a database query because the item preview was truncated.
- `path_params`: none
- `query`: `count` (integer|null): Optional page size for a legacy full-list endpoint. Supplying page or count activates pagination; an omitted count then uses 50.; `page` (integer|null): Optional one-based page for a legacy full-list endpoint. Omit both page and count to keep the original unpaginated full result.
- `body`: none

### `site.userdata.refresh`
`POST /api/v1/site/userdata/{site_id}`; policy effect: `external_side_effect`.
Purpose: Refresh and return account statistics for one configured site.
- `path_params`: `site_id*` (integer): Persistent site ID returned by site.list.
- `query`: none
- `body`: none

### `slash.list`
`GET /api/v1/message/agent/commands`; policy effect: `safe_read`.
Purpose: List slash commands that the Agent may dispatch.
- `response`: `data` remains a list; omitting both `page` and `count` keeps the complete legacy result. `collection.result_count` reports the returned items and `collection.total_count` reports the exact pre-pagination total. For counts or summaries, send `page=1,count=1`, read `collection.total_count`, and do not fall back to a database query because the item preview was truncated.
- `path_params`: none
- `query`: `count` (integer|null): Optional page size for a legacy full-list endpoint. Supplying page or count activates pagination; an omitted count then uses 50.; `page` (integer|null): Optional one-based page for a legacy full-list endpoint. Omit both page and count to keep the original unpaginated full result.
- `body`: none

### `slash.run`
`POST /api/v1/message/agent/commands/run`; policy effect: `external_side_effect`.
Purpose: Execute one complete slash command through MoviePilot messaging.
- `path_params`: none
- `query`: none
- `body`: `command*` (string): Complete slash command, including the leading slash and all arguments.

### `storage.delete`
`POST /api/v1/storage/delete`; policy effect: `destructive_write`.
Purpose: Delete one exact file or directory from a configured storage provider.
- `path_params`: none
- `query`: none
- `body`: `basename` (string|null): Base filename without its parent path.; `children` (array<FileItem-Input>|null): Child storage items nested below this item.; `drive_id` (string|null): Provider-native storage drive identifier.; `extension` (string|null): Filename extension, including or excluding the leading dot as returned by storage.; `fileid` (string|null): Provider-native storage item identifier.; `modify_time` (number|null): Storage item modification timestamp.; `name` (string|null): Human-readable name of the site, storage item, subscription, or rule group.; `parent_fileid` (string|null): Provider-native identifier of the parent storage directory.; `path` (string|null; default `/`): Storage or history path represented by this record.; `pickcode` (string|null): 115 storage pickcode associated with the item.; `size` (integer|null): File or torrent size in bytes.; `storage` (string|null; default `local`): Configured storage name or storage type used by the operation.; `thumbnail` (string|null): Thumbnail URL returned by the storage provider.; `type` (string|null): MoviePilot media or storage item type required by the selected operation.; `url` (string|null): Site, storage, or torrent URL represented by this field.

### `storage.list`
`POST /api/v1/storage/agent/list`; policy effect: `safe_read`.
Purpose: List files or directories from one configured storage location.
- `response`: `data` remains a list; omitting both `page` and `count` keeps the complete legacy result. `collection.result_count` reports the returned items and `collection.total_count` reports the exact pre-pagination total. For counts or summaries, send `page=1,count=1`, read `collection.total_count`, and do not fall back to a database query because the item preview was truncated.
- `path_params`: none
- `query`: `count` (integer|null): Optional page size for a legacy full-list endpoint. Supplying page or count activates pagination; an omitted count then uses 50.; `keyword` (string|null): Case-insensitive substring used to discover settings or filter storage entries.; `page` (integer|null): Optional one-based page for a legacy full-list endpoint. Omit both page and count to keep the original unpaginated full result.; `sort` (string|null; default `updated_at`): Storage-list sort field or ordering expression.
- `body`: `basename` (string|null): Base filename without its parent path.; `children` (array<FileItem-Input>|null): Child storage items nested below this item.; `drive_id` (string|null): Provider-native storage drive identifier.; `extension` (string|null): Filename extension, including or excluding the leading dot as returned by storage.; `fileid` (string|null): Provider-native storage item identifier.; `modify_time` (number|null): Storage item modification timestamp.; `name` (string|null): Human-readable name of the site, storage item, subscription, or rule group.; `parent_fileid` (string|null): Provider-native identifier of the parent storage directory.; `path` (string|null; default `/`): Storage or history path represented by this record.; `pickcode` (string|null): 115 storage pickcode associated with the item.; `size` (integer|null): File or torrent size in bytes.; `storage` (string|null; default `local`): Configured storage name or storage type used by the operation.; `thumbnail` (string|null): Thumbnail URL returned by the storage provider.; `type` (string|null): MoviePilot media or storage item type required by the selected operation.; `url` (string|null): Site, storage, or torrent URL represented by this field.

### `storage.manage`
`POST /api/v1/storage/manage`; policy effect: `external_side_effect`.
Purpose: Run one provider-defined management action against an exact configured storage target.
- `path_params`: none
- `query`: none
- `body`: `action*` (string): Exact provider or workflow action identifier required by the selected operation.; `params` (object): Provider-defined JSON parameters for the selected authentication or storage action.; `target*` (string): Exact configured storage target name accepted by storage.manage.

### `storage.mkdir`
`POST /api/v1/storage/mkdir`; policy effect: `reversible_write`.
Purpose: Create a named child directory below one exact storage directory item.
- `path_params`: none
- `query`: `name*` (string): Human-readable name of the site, storage item, subscription, or rule group.
- `body`: `basename` (string|null): Base filename without its parent path.; `children` (array<FileItem-Input>|null): Child storage items nested below this item.; `drive_id` (string|null): Provider-native storage drive identifier.; `extension` (string|null): Filename extension, including or excluding the leading dot as returned by storage.; `fileid` (string|null): Provider-native storage item identifier.; `modify_time` (number|null): Storage item modification timestamp.; `name` (string|null): Human-readable name of the site, storage item, subscription, or rule group.; `parent_fileid` (string|null): Provider-native identifier of the parent storage directory.; `path` (string|null; default `/`): Storage or history path represented by this record.; `pickcode` (string|null): 115 storage pickcode associated with the item.; `size` (integer|null): File or torrent size in bytes.; `storage` (string|null; default `local`): Configured storage name or storage type used by the operation.; `thumbnail` (string|null): Thumbnail URL returned by the storage provider.; `type` (string|null): MoviePilot media or storage item type required by the selected operation.; `url` (string|null): Site, storage, or torrent URL represented by this field.

### `storage.rename`
`POST /api/v1/storage/rename`; policy effect: `reversible_write`.
Purpose: Rename one exact storage item, optionally applying media-aware recursive renaming.
- `path_params`: none
- `query`: `new_name*` (string): Replacement name for the existing filter-rule group.; `recursive` (boolean|null; default `False`): Apply media-aware renaming recursively to child files when true.
- `body`: `basename` (string|null): Base filename without its parent path.; `children` (array<FileItem-Input>|null): Child storage items nested below this item.; `drive_id` (string|null): Provider-native storage drive identifier.; `extension` (string|null): Filename extension, including or excluding the leading dot as returned by storage.; `fileid` (string|null): Provider-native storage item identifier.; `modify_time` (number|null): Storage item modification timestamp.; `name` (string|null): Human-readable name of the site, storage item, subscription, or rule group.; `parent_fileid` (string|null): Provider-native identifier of the parent storage directory.; `path` (string|null; default `/`): Storage or history path represented by this record.; `pickcode` (string|null): 115 storage pickcode associated with the item.; `size` (integer|null): File or torrent size in bytes.; `storage` (string|null; default `local`): Configured storage name or storage type used by the operation.; `thumbnail` (string|null): Thumbnail URL returned by the storage provider.; `type` (string|null): MoviePilot media or storage item type required by the selected operation.; `url` (string|null): Site, storage, or torrent URL represented by this field.

### `storage.settings`
`GET /api/v1/storage/directories`; policy effect: `safe_read`.
Purpose: Read configured directory or storage settings.
- `response`: `data` remains a list; omitting both `page` and `count` keeps the complete legacy result. `collection.result_count` reports the returned items and `collection.total_count` reports the exact pre-pagination total. For counts or summaries, send `page=1,count=1`, read `collection.total_count`, and do not fall back to a database query because the item preview was truncated.
- `path_params`: none
- `query`: `count` (integer|null): Optional page size for a legacy full-list endpoint. Supplying page or count activates pagination; an omitted count then uses 50.; `directory_type` (string; default `all`): Directory configuration subtype to return.; `name` (string|null): Human-readable name of the site, storage item, subscription, or rule group.; `page` (integer|null): Optional one-based page for a legacy full-list endpoint. Omit both page and count to keep the original unpaginated full result.; `storage_type` (string; default `all`): Configured storage provider type to return.
- `body`: none

### `subscription.add`
`POST /api/v1/subscribe/`; policy effect: `reversible_write`.
Purpose: Create one movie, TV, or music subscription.
- `path_params`: none
- `query`: none
- `body`: `audio_format` (string|null): Requested or recorded audio container or codec, such as FLAC or MP3.; `audio_quality` (string|null): Subscription audio-quality rule, such as hires, lossless, or lossy.; `backdrop` (string|null): Backdrop image URL stored with the media or subscription.; `best_version` (integer|null): Enable normal best-version upgrading when set to 1.; `best_version_full` (integer|null): Enable full best-version upgrading when set to 1.; `completed_episode` (integer|null): Highest episode number already completed for the subscription.; `current_audio_format` (string|null): Audio format of the best version currently held.; `current_bit_depth` (integer|null): Bit depth of the best version currently held.; `current_bitrate` (integer|null): Bitrate of the best version currently held.; `current_priority` (integer|null): Calculated priority of the best version currently held.; `current_sample_rate` (integer|null): Sample rate of the best version currently held.; `custom_words` (string|null): Custom recognition or rename words applied to this media workflow.; `date` (string|null): Record creation or completion timestamp used by the history item.; `description` (string|null): Human-readable media, torrent, or subscription description.; `downloader` (string|null): Configured downloader instance name.; `effect` (string|null): Video or release-effect filter expression used by the subscription.; `episode_group` (string|null): TMDB episode-group identifier used for alternate episode ordering.; `episode_priority` (object|null): Per-episode best-version priority state.; `exclude` (string|null): Regular expression or filter expression that rejects matching releases.; `execution_status` (SubscriptionExecutionStatus|null): Current subscription execution status returned with the subscription snapshot.; `filter` (string|null): Named filter rule or rule expression applied to this site or subscription.; `filter_groups` (array<string>|null): Ordered filter-rule group names applied to the subscription.; `id` (integer|null): Persistent database identifier of the supplied record.; `include` (string|null): Regular expression or filter expression that a release must match.; `keyword` (string|null): Case-insensitive substring used to discover settings or filter storage entries.; `lack_episode` (integer|null; default `0`): Number of episodes still missing from the subscription.; `last_update` (string|null): Timestamp of the subscription's most recent update.; `media_category` (string|null): MoviePilot library category assigned to the media.; `media_id` (string|null): Source-native media ID. Always pair it with the exact media_source returned by search.; `media_source` (MediaSource|null): Metadata source identifier. Preserve the exact value returned with media_id.; `min_bit_depth` (integer|null): Minimum acceptable audio bit depth in bits.; `min_bitrate` (integer|null): Minimum acceptable audio bitrate in bits per second.; `min_sample_rate` (integer|null): Minimum acceptable audio sample rate in hertz.; `music_type` (string|null): Music identity level: recording, album, or artist where supported.; `name` (string|null): Human-readable name of the site, storage item, subscription, or rule group.; `note` (array<integer>|null): Structured auxiliary metadata stored with the record.; `poster` (string|null): Poster image URL stored with the media or subscription.; `quality` (string|null): Video or release quality filter expression.; `resolution` (string|null): Video resolution filter expression, such as 1080p or 2160p.; `save_path` (string|null): Configured downloader-side save path for the download or subscription.; `search_imdbid` (integer|null; default `0`): Use IMDb identity during subscription search when set to 1.; `season` (integer|null): Season number used by the media, search, subscription, or transfer operation.; `sites` (array<integer>|null): Exact site IDs included in the search or subscription scope.; `start_episode` (integer|null; default `0`): First episode number requested by the subscription.; `state` (string|null): Current site, subscription, marketplace, or transfer state filter.; `total_episode` (integer|null; default `0`): Expected total episode count for the subscription.; `total_tracks` (integer|null): Expected or recorded track count for a music item.; `type` (string|null): MoviePilot media or storage item type required by the selected operation.; `username` (string|null): MoviePilot or site username required by the selected operation.; `vote` (number|null; default `0.0`): Media vote average stored with the subscription.; `year` (string|null): Release or premiere year used to disambiguate the media title.

### `subscription.delete`
`DELETE /api/v1/subscribe/{subscribe_id}`; policy effect: `destructive_write`.
Purpose: Delete one active subscription.
- `path_params`: `subscribe_id*` (integer): Persistent subscription ID returned by subscription.list.
- `query`: none
- `body`: none

### `subscription.delete_by_media`
`DELETE /api/v1/subscribe/media/{media_id}`; policy effect: `destructive_write`.
Purpose: Delete accessible subscriptions matching one canonical media identity.
- `path_params`: `media_id*` (string): Source-native media ID. Always pair it with the exact media_source returned by search.
- `query`: `media_source*` (MediaSource): Metadata source identifier. Preserve the exact value returned with media_id.; `music_type` (string|null): Music identity level: recording, album, or artist where supported.; `season` (integer|null): Season number used by the media, search, subscription, or transfer operation.
- `body`: none

### `subscription.files`
`GET /api/v1/subscribe/files/{subscribe_id}`; policy effect: `safe_read`.
Purpose: Read local library and transfer-file coverage for one accessible subscription.
- `path_params`: `subscribe_id*` (integer): Persistent subscription ID returned by subscription.list.
- `query`: none
- `body`: none

### `subscription.find`
`GET /api/v1/subscribe/media/{media_id}`; policy effect: `safe_read`.
Purpose: Find one accessible subscription by canonical media identity and optional season.
- `path_params`: `media_id*` (string): Source-native media ID. Always pair it with the exact media_source returned by search.
- `query`: `media_source*` (MediaSource): Metadata source identifier. Preserve the exact value returned with media_id.; `music_type` (string|null): Music identity level: recording, album, or artist where supported.; `season` (integer|null): Season number used by the media, search, subscription, or transfer operation.; `title` (string|null): Media, torrent, subscription, or history title used by the operation.
- `body`: none

### `subscription.follow.add`
`POST /api/v1/subscribe/follow`; policy effect: `reversible_write`.
Purpose: Follow one subscription-sharing user by exact share user ID.
- `path_params`: none
- `query`: `share_uid` (string|null): Exact MoviePilot Server sharing-user ID to follow or unfollow.
- `body`: none

### `subscription.follow.delete`
`DELETE /api/v1/subscribe/follow`; policy effect: `reversible_write`.
Purpose: Stop following one subscription-sharing user by exact share user ID.
- `path_params`: none
- `query`: `share_uid` (string|null): Exact MoviePilot Server sharing-user ID to follow or unfollow.
- `body`: none

### `subscription.follow.list`
`GET /api/v1/subscribe/follow`; policy effect: `safe_read`.
Purpose: List subscription-sharing user IDs followed by the current user.
- `response`: `data` remains a list; omitting both `page` and `count` keeps the complete legacy result. `collection.result_count` reports the returned items and `collection.total_count` reports the exact pre-pagination total. For counts or summaries, send `page=1,count=1`, read `collection.total_count`, and do not fall back to a database query because the item preview was truncated.
- `path_params`: none
- `query`: `count` (integer|null): Optional page size for a legacy full-list endpoint. Supplying page or count activates pagination; an omitted count then uses 50.; `page` (integer|null): Optional one-based page for a legacy full-list endpoint. Omit both page and count to keep the original unpaginated full result.
- `body`: none

### `subscription.fork`
`POST /api/v1/subscribe/fork`; policy effect: `external_side_effect`.
Purpose: Create a local subscription from one shared subscription definition.
- `path_params`: none
- `query`: none
- `body`: `audio_format` (string|null): Requested or recorded audio container or codec, such as FLAC or MP3.; `audio_quality` (string|null): Subscription audio-quality rule, such as hires, lossless, or lossy.; `backdrop` (string|null): Backdrop image URL stored with the media or subscription.; `count` (integer|null; default `0`): Maximum number of records to return on the requested page.; `custom_words` (string|null): Custom recognition or rename words applied to this media workflow.; `date` (string|null): Record creation or completion timestamp used by the history item.; `description` (string|null): Human-readable media, torrent, or subscription description.; `effect` (string|null): Video or release-effect filter expression used by the subscription.; `episode_group` (string|null): TMDB episode-group identifier used for alternate episode ordering.; `exclude` (string|null): Regular expression or filter expression that rejects matching releases.; `id` (integer|null): Persistent database identifier of the supplied record.; `include` (string|null): Regular expression or filter expression that a release must match.; `keyword` (string|null): Case-insensitive substring used to discover settings or filter storage entries.; `media_category` (string|null): MoviePilot library category assigned to the media.; `media_id` (string|null): Source-native media ID. Always pair it with the exact media_source returned by search.; `media_source` (MediaSource|null): Metadata source identifier. Preserve the exact value returned with media_id.; `min_bit_depth` (integer|null): Minimum acceptable audio bit depth in bits.; `min_bitrate` (integer|null): Minimum acceptable audio bitrate in bits per second.; `min_sample_rate` (integer|null): Minimum acceptable audio sample rate in hertz.; `music_type` (string|null): Music identity level: recording, album, or artist where supported.; `name` (string|null): Human-readable name of the site, storage item, subscription, or rule group.; `poster` (string|null): Poster image URL stored with the media or subscription.; `quality` (string|null): Video or release quality filter expression.; `resolution` (string|null): Video resolution filter expression, such as 1080p or 2160p.; `season` (integer|null): Season number used by the media, search, subscription, or transfer operation.; `share_comment` (string|null): Optional explanatory comment published with a shared item.; `share_title` (string|null): Public title used when publishing a subscription or workflow.; `share_uid` (string|null): Exact MoviePilot Server sharing-user ID to follow or unfollow.; `share_user` (string|null): Public contributor name used when publishing a subscription or workflow.; `subscribe_id` (integer|null): Persistent subscription ID returned by subscription.list.; `total_episode` (integer|null; default `0`): Expected total episode count for the subscription.; `total_tracks` (integer|null): Expected or recorded track count for a music item.; `type` (string|null): MoviePilot media or storage item type required by the selected operation.; `vote` (number|null; default `0.0`): Media vote average stored with the subscription.; `year` (string|null): Release or premiere year used to disambiguate the media title.

### `subscription.get`
`GET /api/v1/subscribe/{subscribe_id}`; policy effect: `safe_read`.
Purpose: Read one accessible subscription by persistent subscription ID.
- `path_params`: `subscribe_id*` (integer): Persistent subscription ID returned by subscription.list.
- `query`: none
- `body`: none

### `subscription.history`
`GET /api/v1/subscribe/history/{mtype}`; policy effect: `safe_read`.
Purpose: List completed or archived subscription records.
- `response`: `data` remains a list and the endpoint's documented pagination or limit defaults remain in effect. `collection.result_count` reports the returned items and `collection.total_count` reports the exact total. For a count-only request, use the smallest valid page and read that metadata instead of querying the database after item truncation.
- `path_params`: `mtype*` (string): MoviePilot media type or subscription-history category required by the operation.
- `query`: `count` (integer|null; default `30`): Maximum number of records to return on the requested page.; `page` (integer|null; default `1`): One-based result page number.
- `body`: none

### `subscription.history.delete`
`DELETE /api/v1/subscribe/history/{history_id}`; policy effect: `destructive_write`.
Purpose: Delete one accessible subscription-history record.
- `path_params`: `history_id*` (integer): Persistent transfer- or subscription-history ID returned by a history operation.
- `query`: none
- `body`: none

### `subscription.list`
`GET /api/v1/subscribe/`; policy effect: `safe_read`.
Purpose: List active subscriptions.
- `response`: `data` remains a list; omitting both `page` and `count` keeps the complete legacy result. `collection.result_count` reports the returned items and `collection.total_count` reports the exact pre-pagination total. For counts or summaries, send `page=1,count=1`, read `collection.total_count`, and do not fall back to a database query because the item preview was truncated.
- `path_params`: none
- `query`: `count` (integer|null): Optional page size for a legacy full-list endpoint. Supplying page or count activates pagination; an omitted count then uses 50.; `page` (integer|null): Optional one-based page for a legacy full-list endpoint. Omit both page and count to keep the original unpaginated full result.
- `body`: none

### `subscription.metadata.refresh`
`GET /api/v1/subscribe/check`; policy effect: `external_side_effect`.
Purpose: Start a system-wide refresh of subscription TMDB metadata.
- `path_params`: none
- `query`: none
- `body`: none

### `subscription.popular`
`GET /api/v1/subscribe/popular`; policy effect: `safe_read`.
Purpose: List globally popular subscriptions with filters and pagination.
- `response`: `data` remains a list and `collection.result_count` reports the returned items. `collection.total_count` is omitted because this endpoint or its upstream source does not expose a total.
- `path_params`: none
- `query`: `count` (integer|null; default `30`): Maximum number of records to return on the requested page.; `genre_id` (integer|null): Genre identifier used to filter shared or popular subscriptions.; `max_rating` (number|null): Maximum rating used to filter shared or popular subscriptions.; `min_rating` (number|null): Minimum rating used to filter shared or popular subscriptions.; `min_sub` (integer|null): Minimum subscriber count used to filter popular subscriptions.; `page` (integer|null; default `1`): One-based result page number.; `sort_type` (string|null): Ascending or descending order used by shared or popular subscriptions.; `stype*` (string): Popular-subscription category requested by the endpoint.
- `body`: none

### `subscription.refresh`
`GET /api/v1/subscribe/refresh`; policy effect: `external_side_effect`.
Purpose: Start the configured system-wide subscription refresh job.
- `path_params`: none
- `query`: none
- `body`: none

### `subscription.reset`
`GET /api/v1/subscribe/reset/{subid}`; policy effect: `reversible_write`.
Purpose: Reset one accessible subscription so it can be processed again.
- `path_params`: `subid*` (integer): Persistent subscription ID whose status or processing state will change.
- `query`: none
- `body`: none

### `subscription.search`
`GET /api/v1/subscribe/search/{subscribe_id}`; policy effect: `safe_read`.
Purpose: Run an immediate search for one existing subscription.
- `path_params`: `subscribe_id*` (integer): Persistent subscription ID returned by subscription.list.
- `query`: none
- `body`: none

### `subscription.search_all`
`GET /api/v1/subscribe/search`; policy effect: `external_side_effect`.
Purpose: Start immediate searches for all subscriptions accessible to the current user.
- `path_params`: none
- `query`: none
- `body`: none

### `subscription.share`
`POST /api/v1/subscribe/share`; policy effect: `external_side_effect`.
Purpose: Publish one accessible subscription to the MoviePilot sharing service.
- `path_params`: none
- `query`: none
- `body`: `audio_format` (string|null): Requested or recorded audio container or codec, such as FLAC or MP3.; `audio_quality` (string|null): Subscription audio-quality rule, such as hires, lossless, or lossy.; `backdrop` (string|null): Backdrop image URL stored with the media or subscription.; `count` (integer|null; default `0`): Maximum number of records to return on the requested page.; `custom_words` (string|null): Custom recognition or rename words applied to this media workflow.; `date` (string|null): Record creation or completion timestamp used by the history item.; `description` (string|null): Human-readable media, torrent, or subscription description.; `effect` (string|null): Video or release-effect filter expression used by the subscription.; `episode_group` (string|null): TMDB episode-group identifier used for alternate episode ordering.; `exclude` (string|null): Regular expression or filter expression that rejects matching releases.; `id` (integer|null): Persistent database identifier of the supplied record.; `include` (string|null): Regular expression or filter expression that a release must match.; `keyword` (string|null): Case-insensitive substring used to discover settings or filter storage entries.; `media_category` (string|null): MoviePilot library category assigned to the media.; `media_id` (string|null): Source-native media ID. Always pair it with the exact media_source returned by search.; `media_source` (MediaSource|null): Metadata source identifier. Preserve the exact value returned with media_id.; `min_bit_depth` (integer|null): Minimum acceptable audio bit depth in bits.; `min_bitrate` (integer|null): Minimum acceptable audio bitrate in bits per second.; `min_sample_rate` (integer|null): Minimum acceptable audio sample rate in hertz.; `music_type` (string|null): Music identity level: recording, album, or artist where supported.; `name` (string|null): Human-readable name of the site, storage item, subscription, or rule group.; `poster` (string|null): Poster image URL stored with the media or subscription.; `quality` (string|null): Video or release quality filter expression.; `resolution` (string|null): Video resolution filter expression, such as 1080p or 2160p.; `season` (integer|null): Season number used by the media, search, subscription, or transfer operation.; `share_comment` (string|null): Optional explanatory comment published with a shared item.; `share_title` (string|null): Public title used when publishing a subscription or workflow.; `share_uid` (string|null): Exact MoviePilot Server sharing-user ID to follow or unfollow.; `share_user` (string|null): Public contributor name used when publishing a subscription or workflow.; `subscribe_id` (integer|null): Persistent subscription ID returned by subscription.list.; `total_episode` (integer|null; default `0`): Expected total episode count for the subscription.; `total_tracks` (integer|null): Expected or recorded track count for a music item.; `type` (string|null): MoviePilot media or storage item type required by the selected operation.; `vote` (number|null; default `0.0`): Media vote average stored with the subscription.; `year` (string|null): Release or premiere year used to disambiguate the media title.

### `subscription.share.delete`
`DELETE /api/v1/subscribe/share/{share_id}`; policy effect: `external_side_effect`.
Purpose: Delete one shared-subscription publication by share ID.
- `path_params`: `share_id*` (integer): Persistent MoviePilot Server share ID returned by a share-list operation.
- `query`: none
- `body`: none

### `subscription.share.statistics`
`GET /api/v1/subscribe/share/statistics`; policy effect: `safe_read`.
Purpose: Read aggregate contribution and reuse counts for subscription sharers.
- `response`: `data` remains a list; omitting both `page` and `count` keeps the complete legacy result. `collection.result_count` reports the returned items and `collection.total_count` reports the exact pre-pagination total. For counts or summaries, send `page=1,count=1`, read `collection.total_count`, and do not fall back to a database query because the item preview was truncated.
- `path_params`: none
- `query`: `count` (integer|null): Optional page size for a legacy full-list endpoint. Supplying page or count activates pagination; an omitted count then uses 50.; `page` (integer|null): Optional one-based page for a legacy full-list endpoint. Omit both page and count to keep the original unpaginated full result.
- `body`: none

### `subscription.shares`
`GET /api/v1/subscribe/shares`; policy effect: `safe_read`.
Purpose: List shared subscriptions with filters and pagination.
- `response`: `data` remains a list and `collection.result_count` reports the returned items. `collection.total_count` is omitted because this endpoint or its upstream source does not expose a total.
- `path_params`: none
- `query`: `count` (integer|null; default `30`): Maximum number of records to return on the requested page.; `genre_id` (integer|null): Genre identifier used to filter shared or popular subscriptions.; `max_rating` (number|null): Maximum rating used to filter shared or popular subscriptions.; `min_rating` (number|null): Minimum rating used to filter shared or popular subscriptions.; `name` (string|null): Human-readable name of the site, storage item, subscription, or rule group.; `page` (integer|null; default `1`): One-based result page number.; `sort_type` (string|null): Ascending or descending order used by shared or popular subscriptions.
- `body`: none

### `subscription.status.update`
`PUT /api/v1/subscribe/status/{subid}`; policy effect: `reversible_write`.
Purpose: Set one accessible subscription to running, paused, or stopped state.
- `path_params`: `subid*` (integer): Persistent subscription ID whose status or processing state will change.
- `query`: `state*` (string): Current site, subscription, marketplace, or transfer state filter.
- `body`: none

### `subscription.update`
`PUT /api/v1/subscribe/`; policy effect: `reversible_write`.
Purpose: Update one existing movie, TV, or music subscription.
- `path_params`: none
- `query`: none
- `body`: `audio_format` (string|null): Requested or recorded audio container or codec, such as FLAC or MP3.; `audio_quality` (string|null): Subscription audio-quality rule, such as hires, lossless, or lossy.; `backdrop` (string|null): Backdrop image URL stored with the media or subscription.; `best_version` (integer|null): Enable normal best-version upgrading when set to 1.; `best_version_full` (integer|null): Enable full best-version upgrading when set to 1.; `completed_episode` (integer|null): Highest episode number already completed for the subscription.; `current_audio_format` (string|null): Audio format of the best version currently held.; `current_bit_depth` (integer|null): Bit depth of the best version currently held.; `current_bitrate` (integer|null): Bitrate of the best version currently held.; `current_priority` (integer|null): Calculated priority of the best version currently held.; `current_sample_rate` (integer|null): Sample rate of the best version currently held.; `custom_words` (string|null): Custom recognition or rename words applied to this media workflow.; `date` (string|null): Record creation or completion timestamp used by the history item.; `description` (string|null): Human-readable media, torrent, or subscription description.; `downloader` (string|null): Configured downloader instance name.; `effect` (string|null): Video or release-effect filter expression used by the subscription.; `episode_group` (string|null): TMDB episode-group identifier used for alternate episode ordering.; `episode_priority` (object|null): Per-episode best-version priority state.; `exclude` (string|null): Regular expression or filter expression that rejects matching releases.; `execution_status` (SubscriptionExecutionStatus|null): Current subscription execution status returned with the subscription snapshot.; `filter` (string|null): Named filter rule or rule expression applied to this site or subscription.; `filter_groups` (array<string>|null): Ordered filter-rule group names applied to the subscription.; `id` (integer|null): Persistent database identifier of the supplied record.; `include` (string|null): Regular expression or filter expression that a release must match.; `keyword` (string|null): Case-insensitive substring used to discover settings or filter storage entries.; `lack_episode` (integer|null; default `0`): Number of episodes still missing from the subscription.; `last_update` (string|null): Timestamp of the subscription's most recent update.; `media_category` (string|null): MoviePilot library category assigned to the media.; `media_id` (string|null): Source-native media ID. Always pair it with the exact media_source returned by search.; `media_source` (MediaSource|null): Metadata source identifier. Preserve the exact value returned with media_id.; `min_bit_depth` (integer|null): Minimum acceptable audio bit depth in bits.; `min_bitrate` (integer|null): Minimum acceptable audio bitrate in bits per second.; `min_sample_rate` (integer|null): Minimum acceptable audio sample rate in hertz.; `music_type` (string|null): Music identity level: recording, album, or artist where supported.; `name` (string|null): Human-readable name of the site, storage item, subscription, or rule group.; `note` (array<integer>|null): Structured auxiliary metadata stored with the record.; `poster` (string|null): Poster image URL stored with the media or subscription.; `quality` (string|null): Video or release quality filter expression.; `resolution` (string|null): Video resolution filter expression, such as 1080p or 2160p.; `save_path` (string|null): Configured downloader-side save path for the download or subscription.; `search_imdbid` (integer|null; default `0`): Use IMDb identity during subscription search when set to 1.; `season` (integer|null): Season number used by the media, search, subscription, or transfer operation.; `sites` (array<integer>|null): Exact site IDs included in the search or subscription scope.; `start_episode` (integer|null; default `0`): First episode number requested by the subscription.; `state` (string|null): Current site, subscription, marketplace, or transfer state filter.; `total_episode` (integer|null; default `0`): Expected total episode count for the subscription.; `total_tracks` (integer|null): Expected or recorded track count for a music item.; `type` (string|null): MoviePilot media or storage item type required by the selected operation.; `username` (string|null): MoviePilot or site username required by the selected operation.; `vote` (number|null; default `0.0`): Media vote average stored with the subscription.; `year` (string|null): Release or premiere year used to disambiguate the media title.

### `subscription.user.list`
`GET /api/v1/subscribe/user/{username}`; policy effect: `safe_read`.
Purpose: List public subscriptions owned by one accessible MoviePilot username.
- `response`: `data` remains a list; omitting both `page` and `count` keeps the complete legacy result. `collection.result_count` reports the returned items and `collection.total_count` reports the exact pre-pagination total. For counts or summaries, send `page=1,count=1`, read `collection.total_count`, and do not fall back to a database query because the item preview was truncated.
- `path_params`: `username*` (string): MoviePilot or site username required by the selected operation.
- `query`: `count` (integer|null): Optional page size for a legacy full-list endpoint. Supplying page or count activates pagination; an omitted count then uses 50.; `page` (integer|null): Optional one-based page for a legacy full-list endpoint. Omit both page and count to keep the original unpaginated full result.
- `body`: none

### `subtitle.search.media`
`GET /api/v1/search/subtitle/media/{media_id}`; policy effect: `external_side_effect`.
Purpose: Search subtitle providers for one canonical media identity and optional season or episode.
- `response`: `data` remains a list; omitting both `page` and `count` keeps the complete legacy result. `collection.result_count` reports the returned items and `collection.total_count` reports the exact pre-pagination total. For counts or summaries, send `page=1,count=1`, read `collection.total_count`, and do not fall back to a database query because the item preview was truncated.
- `path_params`: `media_id*` (string): Source-native media ID. Always pair it with the exact media_source returned by search.
- `query`: `count` (integer|null): Optional page size for a legacy full-list endpoint. Supplying page or count activates pagination; an omitted count then uses 50.; `episode` (string|null): Episode number used to narrow a subtitle or media search.; `media_source*` (MediaSource): Metadata source identifier. Preserve the exact value returned with media_id.; `mtype` (string|null): MoviePilot media type or subscription-history category required by the operation.; `page` (integer|null): Optional one-based page for a legacy full-list endpoint. Omit both page and count to keep the original unpaginated full result.; `season` (string|null): Season number used by the media, search, subscription, or transfer operation.; `sites` (string|null): Exact site IDs included in the search or subscription scope.
- `body`: none

### `subtitle.search.title`
`GET /api/v1/search/subtitle/title`; policy effect: `external_side_effect`.
Purpose: Search subtitle providers from a free-form title and optional media filters.
- `response`: `data` remains a list and `collection.result_count` reports the returned items. `collection.total_count` is omitted because this endpoint or its upstream source does not expose a total.
- `path_params`: none
- `query`: `keyword` (string|null): Case-insensitive substring used to discover settings or filter storage entries.; `page` (integer|null; default `0`): One-based result page number.; `sites` (string|null): Exact site IDs included in the search or subscription scope.
- `body`: none

### `system.module.list`
`GET /api/v1/system/modulelist`; policy effect: `safe_read`.
Purpose: List loaded MoviePilot module IDs and localized names.
- `path_params`: none
- `query`: none
- `body`: none

### `system.module.test`
`GET /api/v1/system/moduletest/{moduleid}`; policy effect: `external_side_effect`.
Purpose: Run the built-in availability test for one loaded MoviePilot module.
- `path_params`: `moduleid*` (string): Exact loaded module ID returned by system.module.list.
- `query`: none
- `body`: none

### `system.network.targets`
`GET /api/v1/system/nettest/targets`; policy effect: `safe_read`.
Purpose: List approved built-in network-test targets without exposing their request URLs.
- `response`: `data` remains a list; omitting both `page` and `count` keeps the complete legacy result. `collection.result_count` reports the returned items and `collection.total_count` reports the exact pre-pagination total. For counts or summaries, send `page=1,count=1`, read `collection.total_count`, and do not fall back to a database query because the item preview was truncated.
- `path_params`: none
- `query`: `count` (integer|null): Optional page size for a legacy full-list endpoint. Supplying page or count activates pagination; an omitted count then uses 50.; `page` (integer|null): Optional one-based page for a legacy full-list endpoint. Omit both page and count to keep the original unpaginated full result.
- `body`: none

### `system.network.test`
`GET /api/v1/system/nettest`; policy effect: `external_side_effect`.
Purpose: Test connectivity to one approved target or the legacy constrained URL input.
- `path_params`: none
- `query`: `include` (string|null): Regular expression or filter expression that a release must match.; `target_id` (string|null): Approved built-in network-test target ID returned by system.network.targets.; `url` (string|null): Site, storage, or torrent URL represented by this field.
- `body`: none

### `system.restart`
`GET /api/v1/system/restart`; policy effect: `external_side_effect`.
Purpose: Restart the running MoviePilot process.
- `path_params`: none
- `query`: none
- `body`: none

### `system.update.check`
`POST /api/v1/system/update/check`; policy effect: `external_side_effect`.
Purpose: Check GitHub for the latest stable MoviePilot v3 release.
- `path_params`: none
- `query`: none
- `body`: none

### `system.update.download`
`POST /api/v1/system/update/download`; policy effect: `external_side_effect`.
Purpose: Start downloading and verifying the available stable release in the background.
- `path_params`: none
- `query`: none
- `body`: none

### `system.update.install`
`POST /api/v1/system/update/install`; policy effect: `external_side_effect`.
Purpose: Install the already downloaded and verified stable release, then restart MoviePilot.
- `path_params`: none
- `query`: none
- `body`: none

### `system.update.status`
`GET /api/v1/system/update/status`; policy effect: `safe_read`.
Purpose: Read the current stable-release check, download, verification, or install state.
- `path_params`: none
- `query`: none
- `body`: none

### `system.upgrade.dev`
`POST /api/v1/system/upgrade`; policy effect: `external_side_effect`.
Purpose: Update to the current v3 development branch and restart MoviePilot.
- `path_params`: none
- `query`: none
- `body*` (string=dev): Literal dev. Release updates must use the separate check, download, and install operations.

### `system.usage.statistics`
`GET /api/v1/system/usage/statistic`; policy effect: `safe_read`.
Purpose: Read the installation version and runtime usage report available to the current user.
- `path_params`: none
- `query`: none
- `body`: none

### `system.versions`
`GET /api/v1/system/versions`; policy effect: `safe_read`.
Purpose: List available MoviePilot GitHub releases.
- `response`: `data` remains a list; omitting both `page` and `count` keeps the complete legacy result. `collection.result_count` reports the returned items and `collection.total_count` reports the exact pre-pagination total. For counts or summaries, send `page=1,count=1`, read `collection.total_count`, and do not fall back to a database query because the item preview was truncated.
- `path_params`: none
- `query`: `count` (integer|null): Optional page size for a legacy full-list endpoint. Supplying page or count activates pagination; an omitted count then uses 50.; `page` (integer|null): Optional one-based page for a legacy full-list endpoint. Omit both page and count to keep the original unpaginated full result.
- `body`: none

### `torrent.cache.clear`
`DELETE /api/v1/torrent/cache`; policy effect: `destructive_write`.
Purpose: Delete every cached torrent context.
- `path_params`: none
- `query`: none
- `body`: none

### `torrent.cache.delete`
`DELETE /api/v1/torrent/cache/{domain}/{torrent_hash}`; policy effect: `destructive_write`.
Purpose: Delete one cached torrent context by site domain and cache hash.
- `path_params`: `domain*` (string): Site hostname or domain used for matching and requests.; `torrent_hash*` (string): Cache hash returned by torrent.cache.get for one exact site-domain entry.
- `query`: none
- `body`: none

### `torrent.cache.get`
`GET /api/v1/torrent/cache`; policy effect: `safe_read`.
Purpose: Inspect cached torrent contexts and their recognized media identities.
- `path_params`: none
- `query`: none
- `body`: none

### `torrent.cache.refresh`
`POST /api/v1/torrent/cache/refresh`; policy effect: `external_side_effect`.
Purpose: Refresh torrent caches from configured RSS or spider sources.
- `path_params`: none
- `query`: none
- `body`: none

### `torrent.cache.reidentify`
`POST /api/v1/torrent/cache/reidentify/{domain}/{torrent_hash}`; policy effect: `reversible_write`.
Purpose: Replace or recompute the media identity for one cached torrent context.
- `path_params`: `domain*` (string): Site hostname or domain used for matching and requests.; `torrent_hash*` (string): Cache hash returned by torrent.cache.get for one exact site-domain entry.
- `query`: `media_id` (string|null): Source-native media ID. Always pair it with the exact media_source returned by search.; `media_source` (MediaSource|null): Metadata source identifier. Preserve the exact value returned with media_id.; `music_type` (string(recording,album)|null): Music identity level: recording, album, or artist where supported.
- `body`: none

### `transfer.episode_format.recommend`
`POST /api/v1/transfer/episode-format/recommend`; policy effect: `safe_read`.
Purpose: Recommend an episode-number extraction template from supplied file samples.
- `path_params`: none
- `query`: none
- `body`: `fileitem` (FileItem-Input|null): One complete source storage item returned by storage.list.; `fileitems` (array<FileItem-Input>|null): Additional source storage items included in the same manual transfer.

### `transfer.file`
`POST /api/v1/transfer/manual`; policy effect: `external_side_effect`.
Purpose: Run MoviePilot's manual file-transfer and organization workflow.
- `path_params`: none
- `query`: `background` (boolean|null; default `False`): Run the transfer asynchronously and return before completion.
- `body`: `episode_detail` (string|null): Episode mapping details used by manual transfer.; `episode_format` (string|null): Episode-number formatting rule used by manual transfer.; `episode_group` (string|null): TMDB episode-group identifier used for alternate episode ordering.; `episode_offset` (string|null): Integer offset added to detected episode numbers.; `episode_part` (string|null): Episode part number used when one episode is split across files.; `fileitem` (FileItem-Input): One complete source storage item returned by storage.list.; `fileitems` (array<FileItem-Input>|null): Additional source storage items included in the same manual transfer.; `from_history` (boolean|null; default `False`): Treat the transfer input as originating from an existing history record.; `library_category_folder` (boolean|null): Create or use a category-level folder in the target library.; `library_type_folder` (boolean|null): Create or use a media-type folder in the target library.; `logid` (integer|null): One download-history or transfer-log identifier used by manual transfer.; `logids` (array<integer>|null): Multiple download-history or transfer-log identifiers included in manual transfer.; `media_id` (string|null): Source-native media ID. Always pair it with the exact media_source returned by search.; `media_source` (MediaSource|null): Metadata source identifier. Preserve the exact value returned with media_id.; `min_filesize` (integer|null; default `0`): Minimum source file size accepted by manual transfer, in bytes.; `music_type` (string(recording,album)|null): Music identity level: recording, album, or artist where supported.; `preview` (boolean|null; default `False`): Validate and preview manual-transfer output without committing file changes.; `reorganize` (boolean|null; default `False`): Allow manual transfer to organize an item that was already processed.; `scrape` (boolean|null; default `False`): Generate metadata and images after manual transfer.; `season` (integer|null): Season number used by the media, search, subscription, or transfer operation.; `target_path` (string|null): Destination path used by manual transfer.; `target_storage` (string|null): Configured storage name receiving the manual transfer.; `transfer_type` (string|null): Manual-transfer mode, such as move, copy, link, or softlink.; `type_name` (string|null): Explicit media type name used when source IDs alone are ambiguous.

### `transfer.history`
`GET /api/v1/history/transfer`; policy effect: `safe_read`.
Purpose: List file-transfer history with filters and pagination.
- `response`: structured page object; items stay in `data.list` and the exact total stays in `data.total`.
- `path_params`: none
- `query`: `count` (integer|null; default `30`): Maximum number of records to return on the requested page.; `page` (integer|null; default `1`): One-based result page number.; `status` (boolean|null): Transfer success status used to filter history or describe a record.; `title` (string|null): Media, torrent, subscription, or history title used by the operation.
- `body`: none

### `transfer.history.clear`
`GET /api/v1/history/empty/transfer`; policy effect: `destructive_write`.
Purpose: Delete every transfer-history record while leaving transferred files untouched.
- `path_params`: none
- `query`: none
- `body`: none

### `transfer.history.delete`
`DELETE /api/v1/history/transfer`; policy effect: `destructive_write`.
Purpose: Delete one transfer-history record and optionally remove files.
- `path_params`: none
- `query`: `deletedest` (boolean|null; default `False`): Also delete the organized destination files when deleting transfer history.; `deletesrc` (boolean|null; default `False`): Also delete the recorded source files when deleting transfer history.
- `body`: `audio_format` (string|null): Requested or recorded audio container or codec, such as FLAC or MP3.; `audio_lossless` (boolean|null): Whether the recorded audio result is lossless.; `bit_depth` (integer|null): Recorded audio bit depth in bits.; `bitrate` (integer|null): Recorded audio bitrate in bits per second.; `category` (string|null): MoviePilot media category or filter-group category, depending on the operation.; `date` (string|null): Record creation or completion timestamp used by the history item.; `dest` (string|null): Organized destination path recorded in transfer history.; `dest_fileitem` (JsonData-Input|null): Serialized destination storage item recorded by the transfer.; `dest_storage` (string|null): Configured storage name containing the organized destination.; `download_hash` (string|null): Provider-native torrent hash associated with the record.; `episode_group` (string|null): TMDB episode-group identifier used for alternate episode ordering.; `episodes` (string|null): Episode-number expression recorded in history, such as E01-E03.; `errmsg` (string|null): Error message recorded for a failed transfer.; `files` (JsonData-Input|null): Serialized list of files recorded by the history item.; `id*` (integer): Persistent database identifier of the supplied record.; `image` (string|null): Image URL stored with the history record.; `media_id` (string|null): Source-native media ID. Always pair it with the exact media_source returned by search.; `media_source` (MediaSource|null): Metadata source identifier. Preserve the exact value returned with media_id.; `mode` (string|null): Operation mode; music.explore accepts chart or fresh, while transfer history records move, copy, link, or softlink.; `music_type` (string|null): Music identity level: recording, album, or artist where supported.; `sample_rate` (integer|null): Recorded audio sample rate in hertz.; `seasons` (string|null): Season-number expression recorded in history.; `src` (string|null): Source path recorded in transfer history.; `src_fileitem` (JsonData-Input|null): Serialized source storage item recorded by the transfer.; `src_storage` (string|null): Configured storage name containing the transfer source.; `status` (boolean; default `True`): Transfer success status used to filter history or describe a record.; `title` (string|null): Media, torrent, subscription, or history title used by the operation.; `total_tracks` (integer|null): Expected or recorded track count for a music item.; `transfer_task_id` (string|null): Stable durable transfer-task ID associated with the history record.; `type` (string|null): MoviePilot media or storage item type required by the selected operation.; `year` (string|null): Release or premiere year used to disambiguate the media title.

### `transfer.history.redo`
`POST /api/v1/history/transfer/{history_id}/ai-redo`; policy effect: `external_side_effect`.
Purpose: Start AI-assisted reorganization for one transfer-history record.
- `path_params`: `history_id*` (integer): Persistent transfer- or subscription-history ID returned by a history operation.
- `query`: none
- `body`: none

### `transfer.history.redo_batch`
`POST /api/v1/history/transfer/ai-redo`; policy effect: `external_side_effect`.
Purpose: Start AI-assisted reorganization for an explicit list of transfer-history records.
- `path_params`: none
- `query`: none
- `body`: `history_ids` (array<integer>): Explicit persistent transfer-history IDs included in one batch redo request.

### `transfer.manual_history`
`POST /api/v1/transfer/manual/history`; policy effect: `safe_read`.
Purpose: Check whether supplied storage items already have successful transfer history.
- `path_params`: none
- `query`: none
- `body`: `episode_detail` (string|null): Episode mapping details used by manual transfer.; `episode_format` (string|null): Episode-number formatting rule used by manual transfer.; `episode_group` (string|null): TMDB episode-group identifier used for alternate episode ordering.; `episode_offset` (string|null): Integer offset added to detected episode numbers.; `episode_part` (string|null): Episode part number used when one episode is split across files.; `fileitem` (FileItem-Input): One complete source storage item returned by storage.list.; `fileitems` (array<FileItem-Input>|null): Additional source storage items included in the same manual transfer.; `from_history` (boolean|null; default `False`): Treat the transfer input as originating from an existing history record.; `library_category_folder` (boolean|null): Create or use a category-level folder in the target library.; `library_type_folder` (boolean|null): Create or use a media-type folder in the target library.; `logid` (integer|null): One download-history or transfer-log identifier used by manual transfer.; `logids` (array<integer>|null): Multiple download-history or transfer-log identifiers included in manual transfer.; `media_id` (string|null): Source-native media ID. Always pair it with the exact media_source returned by search.; `media_source` (MediaSource|null): Metadata source identifier. Preserve the exact value returned with media_id.; `min_filesize` (integer|null; default `0`): Minimum source file size accepted by manual transfer, in bytes.; `music_type` (string(recording,album)|null): Music identity level: recording, album, or artist where supported.; `preview` (boolean|null; default `False`): Validate and preview manual-transfer output without committing file changes.; `reorganize` (boolean|null; default `False`): Allow manual transfer to organize an item that was already processed.; `scrape` (boolean|null; default `False`): Generate metadata and images after manual transfer.; `season` (integer|null): Season number used by the media, search, subscription, or transfer operation.; `target_path` (string|null): Destination path used by manual transfer.; `target_storage` (string|null): Configured storage name receiving the manual transfer.; `transfer_type` (string|null): Manual-transfer mode, such as move, copy, link, or softlink.; `type_name` (string|null): Explicit media type name used when source IDs alone are ambiguous.

### `transfer.manual_review`
`GET /api/v1/transfer/tasks/{task_id}/manual-review`; policy effect: `safe_read`.
Purpose: Read one durable transfer task awaiting manual review.
- `path_params`: `task_id*` (string): Stable durable transfer task ID returned by transfer.manual_reviews.
- `query`: none
- `body`: none

### `transfer.manual_review.resolve`
`POST /api/v1/transfer/tasks/{task_id}/manual-review`; policy effect: `reversible_write`.
Purpose: Record the authorized decision for one durable transfer manual-review operation.
- `path_params`: `task_id*` (string): Stable durable transfer task ID returned by transfer.manual_reviews.
- `query`: none
- `body`: `decision*` (string(not_applied,applied)): Manual-review decision selected from the endpoint's declared enum.; `operation_id*` (string; minimum length `1`): Exact allowlisted MoviePilot operation ID selecting this oneOf branch.; `reason*` (string; minimum length `1`): Human-readable justification recorded with a manual-review decision.; `result_payload` (object|null): Structured external-operation result recorded with manual review.

### `transfer.manual_reviews`
`GET /api/v1/transfer/tasks/manual-reviews`; policy effect: `safe_read`.
Purpose: Page durable transfer tasks awaiting manual review or retry recovery.
- `response`: structured page object; items stay in `data.items` and the exact total stays in `data.total`.
- `path_params`: none
- `query`: `page` (integer; default `1`; minimum `1`): One-based result page number.; `page_size` (integer; default `30`; minimum `1`; maximum `100`): Maximum records returned on one page.; `state` (string(manual_review,retry_wait); default `manual_review`): Current site, subscription, marketplace, or transfer state filter.
- `body`: none

### `transfer.name`
`GET /api/v1/transfer/name`; policy effect: `safe_read`.
Purpose: Preview the organized destination name for one source path and media identity.
- `path_params`: none
- `query`: `filetype*` (string): Media file type used to preview the organized destination name.; `path*` (string): Storage or history path represented by this record.
- `body`: none

### `transfer.queue`
`GET /api/v1/transfer/queue`; policy effect: `safe_read`.
Purpose: List items waiting in the file-transfer queue.
- `response`: `data` remains a list; omitting both `page` and `count` keeps the complete legacy result. `collection.result_count` reports the returned items and `collection.total_count` reports the exact pre-pagination total. For counts or summaries, send `page=1,count=1`, read `collection.total_count`, and do not fall back to a database query because the item preview was truncated.
- `path_params`: none
- `query`: `count` (integer|null): Optional page size for a legacy full-list endpoint. Supplying page or count activates pagination; an omitted count then uses 50.; `page` (integer|null): Optional one-based page for a legacy full-list endpoint. Omit both page and count to keep the original unpaginated full result.
- `body`: none

### `transfer.queue.delete`
`DELETE /api/v1/transfer/queue`; policy effect: `destructive_write`.
Purpose: Remove one exact storage item from the file-transfer queue and stop its transfer.
- `path_params`: none
- `query`: none
- `body`: `basename` (string|null): Base filename without its parent path.; `children` (array<FileItem-Input>|null): Child storage items nested below this item.; `drive_id` (string|null): Provider-native storage drive identifier.; `extension` (string|null): Filename extension, including or excluding the leading dot as returned by storage.; `fileid` (string|null): Provider-native storage item identifier.; `modify_time` (number|null): Storage item modification timestamp.; `name` (string|null): Human-readable name of the site, storage item, subscription, or rule group.; `parent_fileid` (string|null): Provider-native identifier of the parent storage directory.; `path` (string|null; default `/`): Storage or history path represented by this record.; `pickcode` (string|null): 115 storage pickcode associated with the item.; `size` (integer|null): File or torrent size in bytes.; `storage` (string|null; default `local`): Configured storage name or storage type used by the operation.; `thumbnail` (string|null): Thumbnail URL returned by the storage provider.; `type` (string|null): MoviePilot media or storage item type required by the selected operation.; `url` (string|null): Site, storage, or torrent URL represented by this field.

### `transfer.target_path`
`POST /api/v1/transfer/manual/target-path`; policy effect: `safe_read`.
Purpose: Resolve the configured transfer destination for supplied source storage items.
- `path_params`: none
- `query`: none
- `body`: `episode_detail` (string|null): Episode mapping details used by manual transfer.; `episode_format` (string|null): Episode-number formatting rule used by manual transfer.; `episode_group` (string|null): TMDB episode-group identifier used for alternate episode ordering.; `episode_offset` (string|null): Integer offset added to detected episode numbers.; `episode_part` (string|null): Episode part number used when one episode is split across files.; `fileitem` (FileItem-Input): One complete source storage item returned by storage.list.; `fileitems` (array<FileItem-Input>|null): Additional source storage items included in the same manual transfer.; `from_history` (boolean|null; default `False`): Treat the transfer input as originating from an existing history record.; `library_category_folder` (boolean|null): Create or use a category-level folder in the target library.; `library_type_folder` (boolean|null): Create or use a media-type folder in the target library.; `logid` (integer|null): One download-history or transfer-log identifier used by manual transfer.; `logids` (array<integer>|null): Multiple download-history or transfer-log identifiers included in manual transfer.; `media_id` (string|null): Source-native media ID. Always pair it with the exact media_source returned by search.; `media_source` (MediaSource|null): Metadata source identifier. Preserve the exact value returned with media_id.; `min_filesize` (integer|null; default `0`): Minimum source file size accepted by manual transfer, in bytes.; `music_type` (string(recording,album)|null): Music identity level: recording, album, or artist where supported.; `preview` (boolean|null; default `False`): Validate and preview manual-transfer output without committing file changes.; `reorganize` (boolean|null; default `False`): Allow manual transfer to organize an item that was already processed.; `scrape` (boolean|null; default `False`): Generate metadata and images after manual transfer.; `season` (integer|null): Season number used by the media, search, subscription, or transfer operation.; `target_path` (string|null): Destination path used by manual transfer.; `target_storage` (string|null): Configured storage name receiving the manual transfer.; `transfer_type` (string|null): Manual-transfer mode, such as move, copy, link, or softlink.; `type_name` (string|null): Explicit media type name used when source IDs alone are ambiguous.

### `workflow.actions`
`GET /api/v1/workflow/actions`; policy effect: `safe_read`.
Purpose: List built-in workflow action definitions and their parameter contracts.
- `response`: `data` remains a list; omitting both `page` and `count` keeps the complete legacy result. `collection.result_count` reports the returned items and `collection.total_count` reports the exact pre-pagination total. For counts or summaries, send `page=1,count=1`, read `collection.total_count`, and do not fall back to a database query because the item preview was truncated.
- `path_params`: none
- `query`: `count` (integer|null): Optional page size for a legacy full-list endpoint. Supplying page or count activates pagination; an omitted count then uses 50.; `page` (integer|null): Optional one-based page for a legacy full-list endpoint. Omit both page and count to keep the original unpaginated full result.
- `body`: none

### `workflow.create`
`POST /api/v1/workflow/`; policy effect: `reversible_write`.
Purpose: Create one workflow from a complete workflow definition.
- `path_params`: none
- `query`: none
- `body`: `actions` (array<Action-Input>|null): Ordered workflow action definitions executed by this workflow or flow.; `add_time` (string|null): Timestamp when the workflow definition was created.; `current_action` (string|null): Identifier of the workflow action currently selected or executing.; `description` (string|null): Human-readable media, torrent, or subscription description.; `event_conditions` (object|null): Additional workflow event-filter conditions.; `event_type` (string|null): Exact event type returned by workflow.event_types.; `execution_config` (WorkflowExecutionConfig|null): Workflow runtime limits, concurrency, and failure-policy configuration.; `execution_state` (WorkflowExecutionState-Input|null): Persisted resumable workflow execution state.; `flows` (array<ActionFlow-Input>|null): Workflow connection definitions linking action nodes.; `id` (integer|null): Persistent database identifier of the supplied record.; `last_time` (string|null): Timestamp of the workflow's most recent execution.; `name` (string|null): Human-readable name of the site, storage item, subscription, or rule group.; `result` (string|null): Persisted workflow action result value.; `run_count` (integer|null; default `0`): Number of times the workflow has been executed.; `state` (string|null): Current site, subscription, marketplace, or transfer state filter.; `timer` (string|null): Workflow timer or cron expression used for scheduled execution.; `trigger_type` (string|null; default `timer`): Workflow trigger filter: timer, event, manual, or all.

### `workflow.delete`
`DELETE /api/v1/workflow/{workflow_id}`; policy effect: `destructive_write`.
Purpose: Delete one configured workflow by persistent workflow ID.
- `path_params`: `workflow_id*` (integer): Persistent workflow ID returned by workflow.list.
- `query`: none
- `body`: none

### `workflow.event_types`
`GET /api/v1/workflow/event_types`; policy effect: `safe_read`.
Purpose: List event types that can trigger workflows.
- `response`: `data` remains a list; omitting both `page` and `count` keeps the complete legacy result. `collection.result_count` reports the returned items and `collection.total_count` reports the exact pre-pagination total. For counts or summaries, send `page=1,count=1`, read `collection.total_count`, and do not fall back to a database query because the item preview was truncated.
- `path_params`: none
- `query`: `count` (integer|null): Optional page size for a legacy full-list endpoint. Supplying page or count activates pagination; an omitted count then uses 50.; `page` (integer|null): Optional one-based page for a legacy full-list endpoint. Omit both page and count to keep the original unpaginated full result.
- `body`: none

### `workflow.fork`
`POST /api/v1/workflow/fork`; policy effect: `external_side_effect`.
Purpose: Create a local workflow from one shared workflow definition.
- `path_params`: none
- `query`: none
- `body`: `actions` (string|null): Ordered workflow action definitions executed by this workflow or flow.; `context` (string|null): Persisted workflow execution context available to later actions.; `count` (integer|null; default `0`): Maximum number of records to return on the requested page.; `date` (string|null): Record creation or completion timestamp used by the history item.; `description` (string|null): Human-readable media, torrent, or subscription description.; `event_conditions` (string|null): Additional workflow event-filter conditions.; `event_type` (string|null): Exact event type returned by workflow.event_types.; `flows` (string|null): Workflow connection definitions linking action nodes.; `id` (integer|null): Persistent database identifier of the supplied record.; `name` (string|null): Human-readable name of the site, storage item, subscription, or rule group.; `share_comment` (string|null): Optional explanatory comment published with a shared item.; `share_title` (string|null): Public title used when publishing a subscription or workflow.; `share_uid` (string|null): Exact MoviePilot Server sharing-user ID to follow or unfollow.; `share_user` (string|null): Public contributor name used when publishing a subscription or workflow.; `timer` (string|null): Workflow timer or cron expression used for scheduled execution.; `trigger_type` (string|null): Workflow trigger filter: timer, event, manual, or all.

### `workflow.get`
`GET /api/v1/workflow/{workflow_id}`; policy effect: `safe_read`.
Purpose: Read one complete configured workflow definition.
- `path_params`: `workflow_id*` (integer): Persistent workflow ID returned by workflow.list.
- `query`: none
- `body`: none

### `workflow.list`
`GET /api/v1/workflow/agent`; policy effect: `safe_read`.
Purpose: List configured workflows and their execution state.
- `response`: `data` remains a list; omitting both `page` and `count` keeps the complete legacy result. `collection.result_count` reports the returned items and `collection.total_count` reports the exact pre-pagination total. For counts or summaries, send `page=1,count=1`, read `collection.total_count`, and do not fall back to a database query because the item preview was truncated.
- `path_params`: none
- `query`: `count` (integer|null): Optional page size for a legacy full-list endpoint. Supplying page or count activates pagination; an omitted count then uses 50.; `name` (string|null): Human-readable name of the site, storage item, subscription, or rule group.; `page` (integer|null): Optional one-based page for a legacy full-list endpoint. Omit both page and count to keep the original unpaginated full result.; `state` (string(W,R,P,S,F,all); default `all`): Current site, subscription, marketplace, or transfer state filter.; `trigger_type` (string(timer,event,manual,all); default `all`): Workflow trigger filter: timer, event, manual, or all.
- `body`: none

### `workflow.pause`
`POST /api/v1/workflow/{workflow_id}/pause`; policy effect: `reversible_write`.
Purpose: Disable automatic execution of one configured workflow.
- `path_params`: `workflow_id*` (integer): Persistent workflow ID returned by workflow.list.
- `query`: none
- `body`: none

### `workflow.plugin.actions`
`GET /api/v1/workflow/plugin/actions`; policy effect: `safe_read`.
Purpose: List workflow actions contributed by installed plugins, optionally filtered by plugin ID.
- `response`: `data` remains a list; omitting both `page` and `count` keeps the complete legacy result. `collection.result_count` reports the returned items and `collection.total_count` reports the exact pre-pagination total. For counts or summaries, send `page=1,count=1`, read `collection.total_count`, and do not fall back to a database query because the item preview was truncated.
- `path_params`: none
- `query`: `count` (integer|null): Optional page size for a legacy full-list endpoint. Supplying page or count activates pagination; an omitted count then uses 50.; `page` (integer|null): Optional one-based page for a legacy full-list endpoint. Omit both page and count to keep the original unpaginated full result.; `plugin_id` (string): Exact installed or marketplace plugin ID.
- `body`: none

### `workflow.reset`
`POST /api/v1/workflow/{workflow_id}/reset`; policy effect: `reversible_write`.
Purpose: Reset one configured workflow definition and execution state.
- `path_params`: `workflow_id*` (integer): Persistent workflow ID returned by workflow.list.
- `query`: none
- `body`: none

### `workflow.run`
`POST /api/v1/workflow/{workflow_id}/run`; policy effect: `external_side_effect`.
Purpose: Run one configured workflow from the beginning or resume point.
- `path_params`: `workflow_id*` (integer): Persistent workflow ID returned by workflow.list.
- `query`: `from_begin` (boolean|null; default `True`): Restart the workflow from its first action instead of resuming progress.
- `body`: none

### `workflow.share`
`POST /api/v1/workflow/share`; policy effect: `external_side_effect`.
Purpose: Publish one configured workflow to the MoviePilot sharing service.
- `path_params`: none
- `query`: none
- `body`: `actions` (string|null): Ordered workflow action definitions executed by this workflow or flow.; `context` (string|null): Persisted workflow execution context available to later actions.; `count` (integer|null; default `0`): Maximum number of records to return on the requested page.; `date` (string|null): Record creation or completion timestamp used by the history item.; `description` (string|null): Human-readable media, torrent, or subscription description.; `event_conditions` (string|null): Additional workflow event-filter conditions.; `event_type` (string|null): Exact event type returned by workflow.event_types.; `flows` (string|null): Workflow connection definitions linking action nodes.; `id` (integer|null): Persistent database identifier of the supplied record.; `name` (string|null): Human-readable name of the site, storage item, subscription, or rule group.; `share_comment` (string|null): Optional explanatory comment published with a shared item.; `share_title` (string|null): Public title used when publishing a subscription or workflow.; `share_uid` (string|null): Exact MoviePilot Server sharing-user ID to follow or unfollow.; `share_user` (string|null): Public contributor name used when publishing a subscription or workflow.; `timer` (string|null): Workflow timer or cron expression used for scheduled execution.; `trigger_type` (string|null): Workflow trigger filter: timer, event, manual, or all.

### `workflow.share.delete`
`DELETE /api/v1/workflow/share/{share_id}`; policy effect: `external_side_effect`.
Purpose: Delete one shared-workflow publication by share ID.
- `path_params`: `share_id*` (integer): Persistent MoviePilot Server share ID returned by a share-list operation.
- `query`: none
- `body`: none

### `workflow.shares`
`GET /api/v1/workflow/shares`; policy effect: `safe_read`.
Purpose: List shared workflows with name and pagination filters.
- `response`: `data` remains a list and `collection.result_count` reports the returned items. `collection.total_count` is omitted because this endpoint or its upstream source does not expose a total.
- `path_params`: none
- `query`: `count` (integer|null; default `30`): Maximum number of records to return on the requested page.; `name` (string|null): Human-readable name of the site, storage item, subscription, or rule group.; `page` (integer|null; default `1`): One-based result page number.
- `body`: none

### `workflow.start`
`POST /api/v1/workflow/{workflow_id}/start`; policy effect: `reversible_write`.
Purpose: Enable automatic execution of one configured workflow.
- `path_params`: `workflow_id*` (integer): Persistent workflow ID returned by workflow.list.
- `query`: none
- `body`: none

### `workflow.update`
`PUT /api/v1/workflow/{workflow_id}`; policy effect: `reversible_write`.
Purpose: Replace one configured workflow definition.
- `path_params`: `workflow_id*` (integer): Persistent workflow ID returned by workflow.list.
- `query`: none
- `body`: `actions` (array<Action-Input>|null): Ordered workflow action definitions executed by this workflow or flow.; `add_time` (string|null): Timestamp when the workflow definition was created.; `current_action` (string|null): Identifier of the workflow action currently selected or executing.; `description` (string|null): Human-readable media, torrent, or subscription description.; `event_conditions` (object|null): Additional workflow event-filter conditions.; `event_type` (string|null): Exact event type returned by workflow.event_types.; `execution_config` (WorkflowExecutionConfig|null): Workflow runtime limits, concurrency, and failure-policy configuration.; `execution_state` (WorkflowExecutionState-Input|null): Persisted resumable workflow execution state.; `flows` (array<ActionFlow-Input>|null): Workflow connection definitions linking action nodes.; `id` (integer|null): Persistent database identifier of the supplied record.; `last_time` (string|null): Timestamp of the workflow's most recent execution.; `name` (string|null): Human-readable name of the site, storage item, subscription, or rule group.; `result` (string|null): Persisted workflow action result value.; `run_count` (integer|null; default `0`): Number of times the workflow has been executed.; `state` (string|null): Current site, subscription, marketplace, or transfer state filter.; `timer` (string|null): Workflow timer or cron expression used for scheduled execution.; `trigger_type` (string|null; default `timer`): Workflow trigger filter: timer, event, manual, or all.

### Referenced Body Models

#### `FileItem-Input`
One file or directory returned by a configured storage provider.
- `basename` (string|null): Base filename without its parent path.
- `children` (array<FileItem-Input>|null): Child storage items nested below this item.
- `drive_id` (string|null): Provider-native storage drive identifier.
- `extension` (string|null): Filename extension, including or excluding the leading dot as returned by storage.
- `fileid` (string|null): Provider-native storage item identifier.
- `modify_time` (number|null): Storage item modification timestamp.
- `name` (string|null): Human-readable name of the site, storage item, subscription, or rule group.
- `parent_fileid` (string|null): Provider-native identifier of the parent storage directory.
- `path` (string|null; default `/`): Storage or history path represented by this record.
- `pickcode` (string|null): 115 storage pickcode associated with the item.
- `size` (integer|null): File or torrent size in bytes.
- `storage` (string|null; default `local`): Configured storage name or storage type used by the operation.
- `thumbnail` (string|null): Thumbnail URL returned by the storage provider.
- `type` (string|null): MoviePilot media or storage item type required by the selected operation.
- `url` (string|null): Site, storage, or torrent URL represented by this field.

#### `JsonData-Input`
Arbitrary JSON-compatible auxiliary data.
This runtime model has no directly writable fields.

#### `MediaSource`
Canonical metadata source identifier paired with a source-native media ID.
This runtime model has no directly writable fields.

#### `MediaType`
MoviePilot media type.
This runtime model has no directly writable fields.

#### `SubscriptionExecutionStatus`
Subscription refresh execution status and progress summary.
- `batch_id` (string|null): Stable subscription search batch identifier.
- `can_cancel` (boolean; default `False`): Whether the current subscription execution can be cancelled.
- `current_site_id` (integer|null): Configured site ID currently handling the subscription execution.
- `error` (string|null): Human-readable workflow, provider, or execution error message.
- `phase*` (string): Current phase of a subscription execution.
- `source` (string|null): Exact metadata or recommendation source selected by the operation.
- `state*` (string): Current site, subscription, marketplace, or transfer state filter.
- `task_id` (string|null): Stable durable transfer task ID returned by transfer.manual_reviews.
- `updated_at*` (string): Timestamp when the subscription execution status was last updated.

#### `TorrentInfo`
One torrent candidate returned by MoviePilot search.
- `category` (string|null): MoviePilot media category or filter-group category, depending on the operation.
- `date_elapsed` (string|null): Human-readable age of the torrent publication date.
- `description` (string|null): Human-readable media, torrent, or subscription description.
- `downloadvolumefactor` (number|null): Torrent download-volume multiplier reported by the site.
- `enclosure` (string|null): Torrent download URL or enclosure supplied by the indexer result.
- `freedate` (string|null): Torrent freeleech expiration timestamp reported by the site.
- `freedate_diff` (string|null): Seconds remaining until the torrent freeleech period ends.
- `grabs` (integer|null; default `0`): Number of completed downloads reported for the torrent.
- `hit_and_run` (boolean|null; default `False`): Whether the torrent is subject to hit-and-run requirements.
- `labels` (array<string>|null): Torrent labels supplied by the site result.
- `media_id` (string|null): Source-native media ID. Always pair it with the exact media_source returned by search.
- `media_source` (MediaSource|null): Metadata source identifier. Preserve the exact value returned with media_id.
- `page_url` (string|null): Public details page for the torrent result.
- `peers` (integer|null; default `0`): Number of downloading peers reported for the torrent.
- `pri_order` (integer|null; default `0`): Indexer priority order assigned to the torrent result.
- `pubdate` (string|null): Torrent publication timestamp.
- `seeders` (integer|null; default `0`): Minimum seeder expression for a filter rule, or the torrent's seeder count.
- `site` (integer|null): Source site identifier associated with the torrent result.
- `site_cookie` (string|null): Site cookie bundled with the torrent result. Treat this value as a secret.
- `site_downloader` (string|null): Downloader instance selected by the source site.
- `site_name` (string|null): Human-readable source site name.
- `site_order` (integer|null; default `0`): Source site's configured search order.
- `site_proxy` (boolean|null; default `False`): Whether the torrent's source site uses the configured proxy.
- `site_ua` (string|null): User-Agent associated with the source site.
- `size` (number|null; default `0.0`): File or torrent size in bytes.
- `title` (string|null): Media, torrent, subscription, or history title used by the operation.
- `uploadvolumefactor` (number|null): Torrent upload-volume multiplier reported by the site.
- `volume_factor` (string|null): Combined upload/download volume-factor label shown for the torrent.

#### `WorkflowExecutionConfig`
Workflow concurrency, join, branch, and failure policies.
- `max_workers` (integer|null): Maximum concurrent workflow actions allowed by the execution configuration.

#### `WorkflowExecutionState-Input`
Persisted resumable workflow execution state.
- `errors` (object): Workflow execution errors keyed or ordered by action identity.
- `nodes` (object): Persisted workflow node runtime states keyed by action identity.
- `outputs` (object): Named output mappings produced by this workflow action.
- `runtime` (WorkflowRuntimeState): Persisted workflow runtime metadata used for safe resume.
- `version` (integer; default `1`): Plugin release or schema version selected by the operation.

#### `WorkflowRuntimeState`
Complete persisted workflow runtime and progress state.
- `attempts` (object): Attempt counters keyed by workflow node or operation identity.
- `errors` (object): Workflow execution errors keyed or ordered by action identity.
- `finished_actions` (integer; default `0`): Workflow action IDs already completed in the persisted execution state.
- `node_states` (object): Persisted runtime states keyed by workflow node identity.
- `progress` (integer; default `0`): Current numeric or structured workflow execution progress.
- `running_tasks` (integer; default `0`): Workflow task IDs currently executing.

## System Settings Contract

Do not enumerate setting keys in this Skill. Settings change as MoviePilot evolves, so use `config.system.get` as the runtime discovery operation before updating an unfamiliar key.

| `source` | Contents | Persistence |
| :--- | :--- | :--- |
| `settings` | Runtime `Settings` fields such as APP_DOMAIN or LLM_MODEL | Type-converted and persisted to `app.env`, then applied to the current process |
| `systemconfig` | Database-backed business configuration such as downloaders, media servers, directories, and notifications | Written through the configuration service with plugin admission and change events |

The `systemconfig` database table is only the physical store for the second source. Use `config.system.get/update` for normal reads and writes. Direct SQL is reserved for an explicitly authorized repair when the managed API cannot complete the operation.

### Discover definitions

1. Call `config.system.get` with `query={"group":"settings","keyword":"LLM"}` or another group/keyword. Discovery defaults to summaries instead of full values.
2. Each returned setting includes `setting_key`, `source`, `group`, `label`, and a `definition` object with `declared_type`, current `value_shape`, `nullable`, `sensitive`, allowed `update_operations`, `default_match_field`, and `persistence`.
3. Read one exact value with `query={"setting_key":"LLM_MODEL"}`. Exact-key reads include the value by default.
4. Use `show_secrets=true` only when an administrator explicitly requests the plaintext value; secret reads remain confirmation-protected.

### Update settings

Choose an operation listed in the discovered setting definition, then send it in `body`:

| operation | Fields | Meaning |
| :--- | :--- | :--- |
| `replace` | `setting_key*`, `value` | Replace the complete scalar, list, or object value |
| `merge_dict` | `setting_key*`, `value`; optional `remove_keys` | Shallow-merge an object and optionally remove keys |
| `upsert_list_item` | `setting_key*`, `value`; optional `match_field`, `match_value` | Replace a matched list item or append it when absent |
| `remove_list_item` | `setting_key*`, `value` or `match_value`; optional `match_field` | Remove one matched list item without replacing the list |

After every update, call `config.system.get` again with the exact setting_key and verify the saved value. Do not guess a key, value shape, list match field, or update operation when discovery can return it.

## Operation Order And Failure Handling

1. Select the operation first, then place each value in its documented bucket. Never move query fields into path_params or send undeclared fields.
2. Reuse the exact `media_source` + `media_id` pair returned by search. For music, also preserve `music_type`.
3. Downloads, transfers, configuration/rule/plugin writes, scheduler/workflow runs, and deletions have side effects; obtain confirmation and inspect the result.
4. `success=false`, HTTP errors, validation errors, and empty results are real outcomes. Never report them as success.
5. Use `database-operation`, `downloader-operation`, or `mediaserver-operation` for their native capabilities. Never bypass the gateway with an arbitrary URL.
