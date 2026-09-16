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
