# Download APIs

Download submission, configured clients, paths, active tasks, and download history.

## Operations

### `download.add`
`POST /api/v1/download/add`; policy effect: `external_side_effect`.
Purpose: Submit one torrent to MoviePilot's normal download workflow.
- `path_params`: none
- `query`: none
- `body`: `allow_unrecognized` (boolean; default `False`): Allow a download when MoviePilot cannot resolve a canonical media identity.; `downloader` (string|null): Configured downloader instance name.; `media_id` (string|null): Source-native media ID. Always pair it with the exact media_source returned by search.; `media_source` (MediaSource|null): Metadata source identifier. Preserve the exact value returned with media_id.; `music_type` (string(recording,album)|null): Music identity level: recording, album, or artist where supported.; `save_path` (string|null): Configured downloader-side save path for the download or subscription.; `torrent_in*` (TorrentInfo): Complete torrent candidate returned by search.results or search.torrents.

### `download.clients`
`GET /api/v1/download/clients`; policy effect: `safe_read`.
Purpose: List enabled downloader instance names and provider types without credentials.
- `response`: `data` remains a list; omitting both `page` and `count` keeps the complete legacy result. `collection.result_count` reports the returned items and `collection.total_count` reports the exact pre-pagination total. For counts or summaries, send `page=1,count=1`, read `collection.total_count`, and do not fall back to a database query because the item preview was truncated.
- `path_params`: none
- `query`: `count` (integer|null): Optional page size for a legacy full-list endpoint. Supplying page or count activates pagination; an omitted count then uses 50.; `page` (integer|null): Optional one-based page for a legacy full-list endpoint. Omit both page and count to keep the original unpaginated full result.
- `body`: none

### `download.history.delete`
`DELETE /api/v1/history/download`; policy effect: `destructive_write`.
Purpose: Delete one MoviePilot download-history record.
- `path_params`: none
- `query`: none
- `body`: `channel` (string|null): Message channel that originally submitted the download.; `classification_policy_revision` (integer|null): Policy revision that produced the persisted classification snapshot.; `classification_rule_id` (string|null): Stable rule ID that selected the persisted classification category.; `classification_source` (string|null): Selection source recorded with the persisted classification snapshot.; `date` (string|null): Record creation or completion timestamp used by the history item.; `download_hash` (string|null): Provider-native torrent hash associated with the record.; `episode_group` (string|null): TMDB episode-group identifier used for alternate episode ordering.; `episodes` (string|null): Episode-number expression recorded in history, such as E01-E03.; `id*` (integer): Persistent database identifier of the supplied record.; `image` (string|null): Image URL stored with the history record.; `media_category` (string|null): MoviePilot library category assigned to the media.; `media_category_id` (string|null): Stable classification category ID; preserve it separately from the current category path snapshot.; `media_id` (string|null): Source-native media ID. Always pair it with the exact media_source returned by search.; `media_source` (MediaSource|null): Metadata source identifier. Preserve the exact value returned with media_id.; `music_type` (string|null): Music identity level: recording, album, or artist where supported.; `note` (JsonData-Input|null): Structured auxiliary metadata stored with the record.; `path` (string|null): Storage or history path represented by this record.; `poster` (string|null): Poster image URL stored with the media or subscription.; `seasons` (string|null): Season-number expression recorded in history.; `title` (string|null): Media, torrent, subscription, or history title used by the operation.; `torrent_description` (string|null): Torrent release description recorded in download history.; `torrent_name` (string|null): Torrent release name recorded in download history.; `torrent_site` (string|null): Source site name recorded in download history.; `type` (string|null): MoviePilot media or storage item type required by the selected operation.; `userid` (string|null): Message-channel user ID recorded with download history.; `username` (string|null): MoviePilot or site username required by the selected operation.; `year` (string|null): Release or premiere year used to disambiguate the media title.

### `download.history.list`
`GET /api/v1/history/download`; policy effect: `safe_read`.
Purpose: Page MoviePilot download-history records in reverse chronological order.
- `response`: `data` remains a list and the endpoint's documented pagination or limit defaults remain in effect. `collection.result_count` reports the returned items and `collection.total_count` reports the exact total. For a count-only request, use the smallest valid page and read that metadata instead of querying the database after item truncation.
- `path_params`: none
- `query`: `count` (integer|null; default `30`): Maximum number of records to return on the requested page.; `page` (integer|null; default `1`): One-based result page number.
- `body`: none

### `download.paths`
`GET /api/v1/download/paths`; policy effect: `safe_read`.
Purpose: List configured downloader save-path URIs that may be passed to download.add.
- `response`: `data` remains a list; omitting both `page` and `count` keeps the complete legacy result. `collection.result_count` reports the returned items and `collection.total_count` reports the exact pre-pagination total. For counts or summaries, send `page=1,count=1`, read `collection.total_count`, and do not fall back to a database query because the item preview was truncated.
- `path_params`: none
- `query`: `count` (integer|null): Optional page size for a legacy full-list endpoint. Supplying page or count activates pagination; an omitted count then uses 50.; `page` (integer|null): Optional one-based page for a legacy full-list endpoint. Omit both page and count to keep the original unpaginated full result.
- `body`: none

### `download.tasks.active`
`GET /api/v1/download/`; policy effect: `safe_read`.
Purpose: List currently downloading MoviePilot tasks with their canonical media context.
- `response`: `data` remains a list; omitting both `page` and `count` keeps the complete legacy result. `collection.result_count` reports the returned items and `collection.total_count` reports the exact pre-pagination total. For counts or summaries, send `page=1,count=1`, read `collection.total_count`, and do not fall back to a database query because the item preview was truncated.
- `path_params`: none
- `query`: `count` (integer|null): Optional page size for a legacy full-list endpoint. Supplying page or count activates pagination; an omitted count then uses 50.; `name` (string|null): Human-readable name of the site, storage item, subscription, or rule group.; `page` (integer|null): Optional one-based page for a legacy full-list endpoint. Omit both page and count to keep the original unpaginated full result.
- `body`: none
