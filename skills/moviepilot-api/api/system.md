# System APIs

Version, update, restart, module, network, and usage operations.

## Operations

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
Purpose: Check for the latest stable MoviePilot v3 application release and current-platform site resources.
- `path_params`: none
- `query`: none
- `body`: none

### `system.update.download`
`POST /api/v1/system/update/download`; policy effect: `external_side_effect`.
Purpose: Start downloading and verifying one selected application or site-resource update in the background.
- `path_params`: none
- `query`: none
- `body` (SystemUpdateRequest|null): Request value for system.update.download. Start downloading and verifying one selected application or site-resource update in the background. Use the exact type and fields below.

### `system.update.install`
`POST /api/v1/system/update/install`; policy effect: `external_side_effect`.
Purpose: Install one selected already downloaded and verified application or site-resource update, then restart MoviePilot.
- `path_params`: none
- `query`: none
- `body` (SystemUpdateRequest|null): Request value for system.update.install. Install one selected already downloaded and verified application or site-resource update, then restart MoviePilot. Use the exact type and fields below.

### `system.update.status`
`GET /api/v1/system/update/status`; policy effect: `safe_read`.
Purpose: Read application and site-resource update checks, downloads, verification, or install state.
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
