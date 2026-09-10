# Dashboard APIs

Dashboard statistics and runtime overview projections.

## Operations

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
