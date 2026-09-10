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
