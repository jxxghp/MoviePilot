# Scheduler APIs

Scheduler listing, progress inspection, and confirmed task execution.

## Operations

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
