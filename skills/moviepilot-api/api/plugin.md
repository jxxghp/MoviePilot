# Plugin APIs

Installed and marketplace plugins, configuration, lifecycle, sources, folders, ratings, releases, and statistics.

## Operations

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

### `plugin.folder.plugin.assign`
`PUT /api/v1/plugin/folders/{folder_name}/plugins/{plugin_id}`; policy effect: `reversible_write`.
Purpose: Move one installed plugin into one named folder and remove its other folder assignments.
- `path_params`: `folder_name*` (string): Exact plugin folder name returned by plugin.folders.get.; `plugin_id*` (string): Exact installed or marketplace plugin ID.
- `query`: none
- `body`: none

### `plugin.folder.plugin.remove`
`DELETE /api/v1/plugin/folders/{folder_name}/plugins/{plugin_id}`; policy effect: `reversible_write`.
Purpose: Remove one installed plugin from one named folder without uninstalling it.
- `path_params`: `folder_name*` (string): Exact plugin folder name returned by plugin.folders.get.; `plugin_id*` (string): Exact installed or marketplace plugin ID.
- `query`: none
- `body`: none

### `plugin.folder.plugins.update`
`PUT /api/v1/plugin/folders/{folder_name}/plugins`; policy effect: `reversible_write`.
Purpose: Replace the ordered plugin IDs assigned to one named plugin folder.
- `path_params`: `folder_name*` (string): Exact plugin folder name returned by plugin.folders.get.
- `query`: none
- `body*` (array<string>|PluginFolderPluginsUpdateRequest): Request value for plugin.folder.plugins.update. Replace the ordered plugin IDs assigned to one named plugin folder. Use the exact type and fields below.

### `plugin.folder.update`
`PATCH /api/v1/plugin/folders/{folder_name}`; policy effect: `reversible_write`.
Purpose: Incrementally rename one plugin folder or update its presentation settings.
- `path_params`: `folder_name*` (string): Exact plugin folder name returned by plugin.folders.get.
- `query`: none
- `body`: `background` (string|null): Optional folder background color or style.; `color` (string|null): Optional folder foreground color.; `gradient` (string|null): Optional folder gradient definition.; `icon` (string|null): Optional folder icon name.; `new_name` (string|null): Optional replacement folder name.; `showIcon` (boolean|null): Whether the frontend should display the folder icon.

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
`POST /api/v1/plugin/reload/{plugin_id}`; policy effect: `external_side_effect`.
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
- `body`: `expected_revision*` (integer; minimum `1.0`): Current revision from a preceding read, used to reject concurrent state changes.; `release_version` (string|null): Exact plugin release version to install when one is required.; `repo_url*` (string; minimum length `1`): Approved plugin repository URL used to resolve the installation source.

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

## Body Models

This category document is self-contained: the shared models below are included so the Agent does not need a second Skill document before calling the API.

### `ClassificationFacts`
Normalized media facts evaluated by the automatic classification policy.
- `extensions` (object): Additional normalized classification facts supplied by extensions.
- `field_sources` (object): Source provenance for normalized classification facts.
- `identity*` (ClassificationIdentityFacts): Stable source-native media identity used by classification facts.
- `media*` (ClassificationMediaFacts): Media metadata input used for a classification preview.
- `music` (ClassificationMusicFacts|null): Music-specific normalized facts used by classification rules.

### `ClassificationFactsPreviewInput`
Normalized facts supplied directly for a classification preview.
- `facts*` (ClassificationFacts): Normalized media facts to evaluate during a classification preview.
- `kind` (string=facts; default `facts`): Classification rule or preview-input kind selected by the request.

### `ClassificationIdentityFacts`
Stable source-native identity used by classification evaluation.
- `media_id*` (string): Source-native media ID. Always pair it with the exact media_source returned by search.
- `media_source*` (string): Metadata source identifier. Preserve the exact value returned with media_id.

### `ClassificationMediaFacts`
Normalized movie, TV, or shared media facts used by classification rules.
- `adult` (boolean|null): Whether the media is marked as adult content.
- `companies` (array<string>|null): Production companies or studios associated with the media.
- `content_rating` (string|null): Content rating assigned to the media.
- `countries` (array<string>|null): Normalized country or region codes used by classification rules.
- `genre_keys` (array<string>|null): Normalized MoviePilot genre keys used by classification rules.
- `genre_names` (array<string>|null): Source-provided genre names used as classification facts.
- `language` (string|null): Normalized media language code used by classification rules.
- `networks` (array<string>|null): Television networks or streaming platforms associated with the media.
- `runtime` (integer|null): Persisted workflow runtime metadata used for safe resume.
- `title` (string|null): Media, torrent, subscription, or history title used by the operation.
- `type*` (string): MoviePilot media or storage item type required by the selected operation.
- `year` (integer|null): Release or premiere year used to disambiguate the media title.

### `ClassificationMediaPreviewInput`
A selected media search result supplied for classification preview.
- `kind` (string=media; default `media`): Classification rule or preview-input kind selected by the request.
- `media*` (object): Media metadata input used for a classification preview.

### `ClassificationMusicFacts`
Music-specific facts used by automatic classification rules.
- `album_type` (string|null): Music album or release-group type used by classification rules.
- `artist_country` (string|null): Country or region associated with the music artist.
- `artists` (array<string>|null): Music artist names associated with the classified entity.
- `entity_type` (string|null): Music entity type used by classification rules.
- `genres` (array<string>|null): Normalized music genre values used by classification rules.
- `release_status` (string|null): Music release status used by classification rules.
- `secondary_types` (array<string>|null): Secondary music release-group types used by classification rules.
- `tags` (array<string>|null): Comma-separated Douban Music category tags; use only with a Douban Music exploration source.

### `ClassificationPolicy-Input`
Complete versioned automatic media-classification policy.
- `categories` (array<ClassificationCategory>): Complete ordered media-category definitions in the classification policy.
- `enrichment_mode` (string(primary_only,enrich_missing); default `primary_only`): Metadata enrichment mode used to populate classification facts.
- `fallbacks` (object): Fallback category or label actions used when no classification rule matches.
- `field_aliases` (object): Optional aliases mapping source-specific fields to normalized classification fields.
- `mode` (string=first_match; default `first_match`): Operation mode; music.explore accepts chart or fresh, while transfer history records move, copy, link, or softlink.
- `revision` (integer; default `0`; minimum `0.0`): Published classification policy revision or expected revision number.
- `rules` (array<ClassificationRule-Input>): Ordered classification rules evaluated from highest priority to lowest.
- `schema_version` (integer=2; default `2`): Classification policy schema version expected by the server.
- `updated_at` (string|null): Timestamp when the persisted object or execution state was last updated.

### `FileItem-Input`
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

### `JsonData-Input`
Arbitrary JSON-compatible auxiliary data.
This runtime model has no directly writable fields.

### `MediaSource`
Canonical metadata source identifier paired with a source-native media ID.
This runtime model has no directly writable fields.

### `MediaType`
MoviePilot media type.
This runtime model has no directly writable fields.

### `SubscriptionExecutionStatus`
Subscription refresh execution status and progress summary.
- `batch_id` (string|null): Stable subscription search batch identifier.
- `can_cancel` (boolean; default `False`): Whether the current subscription execution can be cancelled.
- `current_site_id` (integer|null): Configured site ID currently handling the subscription execution.
- `error` (string|null): Human-readable workflow, provider, or execution error message.
- `next_run_at` (string|null): Next scheduled subscription search time. Null when no future execution is planned.
- `phase*` (string): Current phase of a subscription execution.
- `source` (string|null): Exact metadata or recommendation source selected by the operation.
- `state*` (string): Current site, subscription, marketplace, or transfer state filter.
- `task_id` (string|null): Stable durable transfer task ID returned by transfer.manual_reviews.
- `updated_at*` (string): Timestamp when the persisted object or execution state was last updated.

### `TorrentInfo`
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
- `labels` (array<string>|null): Labels attached to the media, classification result, or torrent result.
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

### `WorkflowExecutionConfig`
Workflow concurrency, join, branch, and failure policies.
- `max_workers` (integer|null): Maximum concurrent workflow actions allowed by the execution configuration.

### `WorkflowExecutionState-Input`
Persisted resumable workflow execution state.
- `errors` (object): Workflow execution errors keyed or ordered by action identity.
- `nodes` (object): Persisted workflow node runtime states keyed by action identity.
- `outputs` (object): Named output mappings produced by this workflow action.
- `runtime` (WorkflowRuntimeState): Persisted workflow runtime metadata used for safe resume.
- `version` (integer; default `1`): Plugin release or schema version selected by the operation.

### `WorkflowRuntimeState`
Complete persisted workflow runtime and progress state.
- `attempts` (object): Attempt counters keyed by workflow node or operation identity.
- `errors` (object): Workflow execution errors keyed or ordered by action identity.
- `finished_actions` (integer; default `0`): Workflow action IDs already completed in the persisted execution state.
- `node_states` (object): Persisted runtime states keyed by workflow node identity.
- `progress` (integer; default `0`): Current numeric or structured workflow execution progress.
- `running_tasks` (integer; default `0`): Workflow task IDs currently executing.
