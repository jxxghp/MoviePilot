# Slash APIs

Slash-command discovery and execution.

## Operations

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
