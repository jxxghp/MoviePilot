# Workflow APIs

Workflow definitions, actions, event types, execution, sharing, and lifecycle control.

## Operations

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
