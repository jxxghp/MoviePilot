# Database APIs

Administrator database backup creation, verification, listing, and deletion.

## Operations

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
