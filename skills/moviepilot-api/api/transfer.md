# Transfer APIs

Transfer queue, history, file, naming, manual review, retry, and target-path operations.

## Operations

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
- `body`: `episode_detail` (string|null): Episode mapping details used by manual transfer.; `episode_format` (string|null): Episode-number formatting rule used by manual transfer.; `episode_group` (string|null): TMDB episode-group identifier used for alternate episode ordering.; `episode_offset` (string|null): Integer offset added to detected episode numbers.; `episode_part` (string|null): Episode part number used when one episode is split across files.; `fileitem` (FileItem-Input): One complete source storage item returned by storage.list.; `fileitems` (array<FileItem-Input>|null): Additional source storage items included in the same manual transfer.; `from_history` (boolean|null; default `False`): Treat the transfer input as originating from an existing history record.; `library_category_folder` (boolean|null): Create or use a category-level folder in the target library.; `library_type_folder` (boolean|null): Create or use a media-type folder in the target library.; `logid` (integer|null): One download-history or transfer-log identifier used by manual transfer.; `logids` (array<integer>|null): Multiple download-history or transfer-log identifiers included in manual transfer.; `media_id` (string|null): Source-native media ID. Always pair it with the exact media_source returned by search.; `media_source` (MediaSource|null): Metadata source identifier. Preserve the exact value returned with media_id.; `min_filesize` (integer|null; default `0`): Minimum source file size accepted by manual transfer, in bytes.; `music_release_regions` (array<string>|null): Optional ISO 3166-1 release-region priority for music organization.; `music_release_scripts` (array<string>|null): Optional ISO 15924 script priority for music organization.; `music_type` (string(recording,album)|null): Music identity level: recording, album, or artist where supported.; `preview` (boolean|null; default `False`): Validate and preview manual-transfer output without committing file changes.; `reorganize` (boolean|null; default `False`): Allow manual transfer to organize an item that was already processed.; `scrape` (boolean|null; default `False`): Generate metadata and images after manual transfer.; `season` (integer|null): Season number used by the media, search, subscription, or transfer operation.; `skip_success` (boolean; default `False`): Skip files already recorded as successfully organized.; `target_path` (string|null): Destination path used by manual transfer.; `target_storage` (string|null): Configured storage name receiving the manual transfer.; `transfer_type` (string|null): Manual-transfer mode, such as move, copy, link, or softlink.; `type_name` (string|null): Explicit media type name used when source IDs alone are ambiguous.

### `transfer.history`
`GET /api/v1/history/transfer`; policy effect: `safe_read`.
Purpose: List file-transfer history with filters and pagination.
- `response`: structured page object; items stay in `data.list` and the exact total stays in `data.total`.
- `path_params`: none
- `query`: `count` (integer|null; default `30`): Maximum number of records to return on the requested page.; `page` (integer|null; default `1`): One-based result page number.; `status` (boolean|null): Transfer success status used to filter history or describe a record.; `title` (string|null): Media, torrent, subscription, or history title used by the operation.
- `body`: none

### `transfer.history.clear`
`DELETE /api/v1/history/transfer/all`; policy effect: `destructive_write`.
Purpose: Delete legacy transfer-history records while leaving files and durable failed-task records untouched.
- `path_params`: none
- `query`: none
- `body`: none

### `transfer.history.delete`
`DELETE /api/v1/history/transfer`; policy effect: `destructive_write`.
Purpose: Delete one transfer-history record and optionally remove files.
- `path_params`: none
- `query`: `deletedest` (boolean|null; default `False`): Also delete the organized destination files when deleting transfer history.; `deletesrc` (boolean|null; default `False`): Also delete the recorded source files when deleting transfer history.
- `body`: `audio_format` (string|null): Requested or recorded audio container or codec, such as FLAC or MP3.; `audio_lossless` (boolean|null): Whether the recorded audio result is lossless.; `auto_paused` (boolean; default `False`): Whether a failed transfer automatically paused the related subscription.; `bit_depth` (integer|null): Recorded audio bit depth in bits.; `bitrate` (integer|null): Recorded audio bitrate in bits per second.; `category` (string|null): MoviePilot media category or filter-group category, depending on the operation.; `classification_policy_revision` (integer|null): Policy revision that produced the persisted classification snapshot.; `classification_rule_id` (string|null): Stable rule ID that selected the persisted classification category.; `classification_source` (string|null): Selection source recorded with the persisted classification snapshot.; `cleanup_error` (string|null): Error recorded while cleaning up a transfer source or destination.; `cleanup_status` (string|null): Current status of transfer-file cleanup after an operation.; `date` (string|null): Record creation or completion timestamp used by the history item.; `dest` (string|null): Organized destination path recorded in transfer history.; `dest_fileitem` (JsonData-Input|null): Serialized destination storage item recorded by the transfer.; `dest_storage` (string|null): Configured storage name containing the organized destination.; `download_hash` (string|null): Provider-native torrent hash associated with the record.; `episode_group` (string|null): TMDB episode-group identifier used for alternate episode ordering.; `episodes` (string|null): Episode-number expression recorded in history, such as E01-E03.; `errmsg` (string|null): Error message recorded for a failed transfer.; `failure_stage` (string|null): Transfer or workflow stage at which the operation failed.; `files` (JsonData-Input|null): Serialized list of files recorded by the history item.; `id*` (integer): Persistent database identifier of the supplied record.; `image` (string|null): Image URL stored with the history record.; `media_category_id` (string|null): Stable classification category ID; preserve it separately from the current category path snapshot.; `media_id` (string|null): Source-native media ID. Always pair it with the exact media_source returned by search.; `media_source` (MediaSource|null): Metadata source identifier. Preserve the exact value returned with media_id.; `mode` (string|null): Operation mode; music.explore accepts chart or fresh, while transfer history records move, copy, link, or softlink.; `music_type` (string|null): Music identity level: recording, album, or artist where supported.; `recovery_action` (string|null): Recovery action reported for a failed or partially completed transfer.; `retry_count` (integer|null): Number of retry attempts already used by the operation.; `retry_exhausted` (boolean; default `False`): Whether the operation has used all configured retry attempts.; `sample_rate` (integer|null): Recorded audio sample rate in hertz.; `seasons` (string|null): Season-number expression recorded in history.; `src` (string|null): Source path recorded in transfer history.; `src_fileitem` (JsonData-Input|null): Serialized source storage item recorded by the transfer.; `src_storage` (string|null): Configured storage name containing the transfer source.; `status` (boolean; default `True`): Transfer success status used to filter history or describe a record.; `title` (string|null): Media, torrent, subscription, or history title used by the operation.; `total_tracks` (integer|null): Expected or recorded track count for a music item.; `transfer_task_id` (string|null): Stable durable transfer-task ID associated with the history record.; `type` (string|null): MoviePilot media or storage item type required by the selected operation.; `year` (string|null): Release or premiere year used to disambiguate the media title.

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
- `body`: `episode_detail` (string|null): Episode mapping details used by manual transfer.; `episode_format` (string|null): Episode-number formatting rule used by manual transfer.; `episode_group` (string|null): TMDB episode-group identifier used for alternate episode ordering.; `episode_offset` (string|null): Integer offset added to detected episode numbers.; `episode_part` (string|null): Episode part number used when one episode is split across files.; `fileitem` (FileItem-Input): One complete source storage item returned by storage.list.; `fileitems` (array<FileItem-Input>|null): Additional source storage items included in the same manual transfer.; `from_history` (boolean|null; default `False`): Treat the transfer input as originating from an existing history record.; `library_category_folder` (boolean|null): Create or use a category-level folder in the target library.; `library_type_folder` (boolean|null): Create or use a media-type folder in the target library.; `logid` (integer|null): One download-history or transfer-log identifier used by manual transfer.; `logids` (array<integer>|null): Multiple download-history or transfer-log identifiers included in manual transfer.; `media_id` (string|null): Source-native media ID. Always pair it with the exact media_source returned by search.; `media_source` (MediaSource|null): Metadata source identifier. Preserve the exact value returned with media_id.; `min_filesize` (integer|null; default `0`): Minimum source file size accepted by manual transfer, in bytes.; `music_release_regions` (array<string>|null): Optional ISO 3166-1 release-region priority for music organization.; `music_release_scripts` (array<string>|null): Optional ISO 15924 script priority for music organization.; `music_type` (string(recording,album)|null): Music identity level: recording, album, or artist where supported.; `preview` (boolean|null; default `False`): Validate and preview manual-transfer output without committing file changes.; `reorganize` (boolean|null; default `False`): Allow manual transfer to organize an item that was already processed.; `scrape` (boolean|null; default `False`): Generate metadata and images after manual transfer.; `season` (integer|null): Season number used by the media, search, subscription, or transfer operation.; `skip_success` (boolean; default `False`): Skip files already recorded as successfully organized.; `target_path` (string|null): Destination path used by manual transfer.; `target_storage` (string|null): Configured storage name receiving the manual transfer.; `transfer_type` (string|null): Manual-transfer mode, such as move, copy, link, or softlink.; `type_name` (string|null): Explicit media type name used when source IDs alone are ambiguous.

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
- `body`: `episode_detail` (string|null): Episode mapping details used by manual transfer.; `episode_format` (string|null): Episode-number formatting rule used by manual transfer.; `episode_group` (string|null): TMDB episode-group identifier used for alternate episode ordering.; `episode_offset` (string|null): Integer offset added to detected episode numbers.; `episode_part` (string|null): Episode part number used when one episode is split across files.; `fileitem` (FileItem-Input): One complete source storage item returned by storage.list.; `fileitems` (array<FileItem-Input>|null): Additional source storage items included in the same manual transfer.; `from_history` (boolean|null; default `False`): Treat the transfer input as originating from an existing history record.; `library_category_folder` (boolean|null): Create or use a category-level folder in the target library.; `library_type_folder` (boolean|null): Create or use a media-type folder in the target library.; `logid` (integer|null): One download-history or transfer-log identifier used by manual transfer.; `logids` (array<integer>|null): Multiple download-history or transfer-log identifiers included in manual transfer.; `media_id` (string|null): Source-native media ID. Always pair it with the exact media_source returned by search.; `media_source` (MediaSource|null): Metadata source identifier. Preserve the exact value returned with media_id.; `min_filesize` (integer|null; default `0`): Minimum source file size accepted by manual transfer, in bytes.; `music_release_regions` (array<string>|null): Optional ISO 3166-1 release-region priority for music organization.; `music_release_scripts` (array<string>|null): Optional ISO 15924 script priority for music organization.; `music_type` (string(recording,album)|null): Music identity level: recording, album, or artist where supported.; `preview` (boolean|null; default `False`): Validate and preview manual-transfer output without committing file changes.; `reorganize` (boolean|null; default `False`): Allow manual transfer to organize an item that was already processed.; `scrape` (boolean|null; default `False`): Generate metadata and images after manual transfer.; `season` (integer|null): Season number used by the media, search, subscription, or transfer operation.; `skip_success` (boolean; default `False`): Skip files already recorded as successfully organized.; `target_path` (string|null): Destination path used by manual transfer.; `target_storage` (string|null): Configured storage name receiving the manual transfer.; `transfer_type` (string|null): Manual-transfer mode, such as move, copy, link, or softlink.; `type_name` (string|null): Explicit media type name used when source IDs alone are ambiguous.

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
