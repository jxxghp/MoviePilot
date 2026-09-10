# Subscription APIs

Subscription creation, search, refresh, sharing, following, history, files, and status management.

## Operations

### `subscription.add`
`POST /api/v1/subscribe/`; policy effect: `reversible_write`.
Purpose: Create one movie, TV, or music subscription.
- `path_params`: none
- `query`: none
- `body`: `audio_format` (string|null): Requested or recorded audio container or codec, such as FLAC or MP3.; `audio_quality` (string|null): Subscription audio-quality rule, such as hires, lossless, or lossy.; `backdrop` (string|null): Backdrop image URL stored with the media or subscription.; `best_version` (integer|null): Enable normal best-version upgrading when set to 1.; `best_version_full` (integer|null): Enable full best-version upgrading when set to 1.; `classification_policy_revision` (integer|null): Policy revision that produced the persisted classification snapshot.; `classification_rule_id` (string|null): Stable rule ID that selected the persisted classification category.; `classification_source` (string|null): Selection source recorded with the persisted classification snapshot.; `completed_episode` (integer|null): Highest episode number already completed for the subscription.; `current_audio_format` (string|null): Audio format of the best version currently held.; `current_bit_depth` (integer|null): Bit depth of the best version currently held.; `current_bitrate` (integer|null): Bitrate of the best version currently held.; `current_priority` (integer|null): Calculated priority of the best version currently held.; `current_sample_rate` (integer|null): Sample rate of the best version currently held.; `custom_words` (string|null): Custom recognition or rename words applied to this media workflow.; `date` (string|null): Record creation or completion timestamp used by the history item.; `description` (string|null): Human-readable media, torrent, or subscription description.; `downloader` (string|null): Configured downloader instance name.; `effect` (string|null): Video or release-effect filter expression used by the subscription.; `episode_group` (string|null): TMDB episode-group identifier used for alternate episode ordering.; `episode_priority` (object|null): Per-episode best-version priority state.; `exclude` (string|null): Regular expression or filter expression that rejects matching releases.; `execution_status` (SubscriptionExecutionStatus|null): Current subscription execution status returned with the subscription snapshot.; `filter` (string|null): Named filter rule or rule expression applied to this site or subscription.; `filter_groups` (array<string>|null): Ordered filter-rule group names applied to the subscription.; `id` (integer|null): Persistent database identifier of the supplied record.; `include` (string|null): Regular expression or filter expression that a release must match.; `keyword` (string|null): Case-insensitive substring used to discover settings or filter storage entries.; `lack_episode` (integer|null; default `0`): Number of episodes still missing from the subscription.; `last_search` (string|null): Read-only UTC timestamp of the most recent subscription search attempt.; `last_update` (string|null): Timestamp of the subscription's most recent update.; `media_category` (string|null): MoviePilot library category assigned to the media.; `media_category_id` (string|null): Stable classification category ID; preserve it separately from the current category path snapshot.; `media_id` (string|null): Source-native media ID. Always pair it with the exact media_source returned by search.; `media_source` (MediaSource|null): Metadata source identifier. Preserve the exact value returned with media_id.; `min_bit_depth` (integer|null): Minimum acceptable audio bit depth in bits.; `min_bitrate` (integer|null): Minimum acceptable audio bitrate in bits per second.; `min_sample_rate` (integer|null): Minimum acceptable audio sample rate in hertz.; `music_type` (string|null): Music identity level: recording, album, or artist where supported.; `name` (string|null): Human-readable name of the site, storage item, subscription, or rule group.; `note` (array<integer>|null): Structured auxiliary metadata stored with the record.; `poster` (string|null): Poster image URL stored with the media or subscription.; `quality` (string|null): Video or release quality filter expression.; `resolution` (string|null): Video resolution filter expression, such as 1080p or 2160p.; `save_path` (string|null): Configured downloader-side save path for the download or subscription.; `search_imdbid` (integer|null; default `0`): Use IMDb identity during subscription search when set to 1.; `search_interval` (integer|null): Scheduled search interval in whole hours (1-8760); null uses the system interval.; `season` (integer|null): Season number used by the media, search, subscription, or transfer operation.; `sites` (array<integer>|null): Exact site IDs included in the search or subscription scope.; `start_episode` (integer|null; default `0`): First episode number requested by the subscription.; `state` (string|null): Current site, subscription, marketplace, or transfer state filter.; `total_episode` (integer|null; default `0`): Expected total episode count for the subscription.; `total_tracks` (integer|null): Expected or recorded track count for a music item.; `type` (string|null): MoviePilot media or storage item type required by the selected operation.; `username` (string|null): MoviePilot or site username required by the selected operation.; `vote` (number|null; default `0.0`): Media vote average stored with the subscription.; `year` (string|null): Release or premiere year used to disambiguate the media title.

### `subscription.delete`
`DELETE /api/v1/subscribe/{subscribe_id}`; policy effect: `destructive_write`.
Purpose: Delete one active subscription.
- `path_params`: `subscribe_id*` (integer): Persistent subscription ID returned by subscription.list.
- `query`: none
- `body`: none

### `subscription.delete_by_media`
`DELETE /api/v1/subscribe/media/{media_id}`; policy effect: `destructive_write`.
Purpose: Delete accessible subscriptions matching one canonical media identity.
- `path_params`: `media_id*` (string): Source-native media ID. Always pair it with the exact media_source returned by search.
- `query`: `media_source*` (MediaSource): Metadata source identifier. Preserve the exact value returned with media_id.; `music_type` (string|null): Music identity level: recording, album, or artist where supported.; `season` (integer|null): Season number used by the media, search, subscription, or transfer operation.
- `body`: none

### `subscription.execution.cancel`
`PUT /api/v1/subscribe/execution/batches/{batch_id}/cancel`; policy effect: `external_side_effect`.
Purpose: Request cancellation of one durable subscription-search batch before its download side-effect boundary.
- `path_params`: `batch_id*` (string): Stable subscription search batch identifier.
- `query`: none
- `body`: none

### `subscription.execution.get`
`GET /api/v1/subscribe/execution/batches/{batch_id}`; policy effect: `safe_read`.
Purpose: Read one durable subscription-search batch visible to the current user.
- `path_params`: `batch_id*` (string): Stable subscription search batch identifier.
- `query`: none
- `body`: none

### `subscription.execution.list`
`GET /api/v1/subscribe/execution/batches`; policy effect: `safe_read`.
Purpose: List recent durable subscription-search batches visible to the current user.
- `response`: `data` remains a list and `collection.result_count` reports the returned items. `collection.total_count` is omitted because this endpoint or its upstream source does not expose a total.
- `path_params`: none
- `query`: `limit` (integer; default `10`): Maximum number of recent subscription-search batches to return.
- `body`: none

### `subscription.files`
`GET /api/v1/subscribe/files/{subscribe_id}`; policy effect: `safe_read`.
Purpose: Read local library and transfer-file coverage for one accessible subscription.
- `path_params`: `subscribe_id*` (integer): Persistent subscription ID returned by subscription.list.
- `query`: none
- `body`: none

### `subscription.find`
`GET /api/v1/subscribe/media/{media_id}`; policy effect: `safe_read`.
Purpose: Find one accessible subscription by canonical media identity and optional season.
- `path_params`: `media_id*` (string): Source-native media ID. Always pair it with the exact media_source returned by search.
- `query`: `media_source*` (MediaSource): Metadata source identifier. Preserve the exact value returned with media_id.; `music_type` (string|null): Music identity level: recording, album, or artist where supported.; `season` (integer|null): Season number used by the media, search, subscription, or transfer operation.; `title` (string|null): Media, torrent, subscription, or history title used by the operation.
- `body`: none

### `subscription.follow.add`
`POST /api/v1/subscribe/follow`; policy effect: `reversible_write`.
Purpose: Follow one subscription-sharing user by exact share user ID.
- `path_params`: none
- `query`: `share_uid` (string|null): Exact MoviePilot Server sharing-user ID to follow or unfollow.
- `body`: none

### `subscription.follow.delete`
`DELETE /api/v1/subscribe/follow`; policy effect: `reversible_write`.
Purpose: Stop following one subscription-sharing user by exact share user ID.
- `path_params`: none
- `query`: `share_uid` (string|null): Exact MoviePilot Server sharing-user ID to follow or unfollow.
- `body`: none

### `subscription.follow.list`
`GET /api/v1/subscribe/follow`; policy effect: `safe_read`.
Purpose: List subscription-sharing user IDs followed by the current user.
- `response`: `data` remains a list; omitting both `page` and `count` keeps the complete legacy result. `collection.result_count` reports the returned items and `collection.total_count` reports the exact pre-pagination total. For counts or summaries, send `page=1,count=1`, read `collection.total_count`, and do not fall back to a database query because the item preview was truncated.
- `path_params`: none
- `query`: `count` (integer|null): Optional page size for a legacy full-list endpoint. Supplying page or count activates pagination; an omitted count then uses 50.; `page` (integer|null): Optional one-based page for a legacy full-list endpoint. Omit both page and count to keep the original unpaginated full result.
- `body`: none

### `subscription.fork`
`POST /api/v1/subscribe/fork`; policy effect: `external_side_effect`.
Purpose: Create a local subscription from one shared subscription definition.
- `path_params`: none
- `query`: none
- `body`: `audio_format` (string|null): Requested or recorded audio container or codec, such as FLAC or MP3.; `audio_quality` (string|null): Subscription audio-quality rule, such as hires, lossless, or lossy.; `backdrop` (string|null): Backdrop image URL stored with the media or subscription.; `count` (integer|null; default `0`): Maximum number of records to return on the requested page.; `custom_words` (string|null): Custom recognition or rename words applied to this media workflow.; `date` (string|null): Record creation or completion timestamp used by the history item.; `description` (string|null): Human-readable media, torrent, or subscription description.; `effect` (string|null): Video or release-effect filter expression used by the subscription.; `episode_group` (string|null): TMDB episode-group identifier used for alternate episode ordering.; `exclude` (string|null): Regular expression or filter expression that rejects matching releases.; `id` (integer|null): Persistent database identifier of the supplied record.; `include` (string|null): Regular expression or filter expression that a release must match.; `keyword` (string|null): Case-insensitive substring used to discover settings or filter storage entries.; `media_category` (string|null): MoviePilot library category assigned to the media.; `media_category_id` (string|null): Stable classification category ID; preserve it separately from the current category path snapshot.; `media_id` (string|null): Source-native media ID. Always pair it with the exact media_source returned by search.; `media_source` (MediaSource|null): Metadata source identifier. Preserve the exact value returned with media_id.; `min_bit_depth` (integer|null): Minimum acceptable audio bit depth in bits.; `min_bitrate` (integer|null): Minimum acceptable audio bitrate in bits per second.; `min_sample_rate` (integer|null): Minimum acceptable audio sample rate in hertz.; `music_type` (string|null): Music identity level: recording, album, or artist where supported.; `name` (string|null): Human-readable name of the site, storage item, subscription, or rule group.; `poster` (string|null): Poster image URL stored with the media or subscription.; `quality` (string|null): Video or release quality filter expression.; `resolution` (string|null): Video resolution filter expression, such as 1080p or 2160p.; `season` (integer|null): Season number used by the media, search, subscription, or transfer operation.; `share_comment` (string|null): Optional explanatory comment published with a shared item.; `share_title` (string|null): Public title used when publishing a subscription or workflow.; `share_uid` (string|null): Exact MoviePilot Server sharing-user ID to follow or unfollow.; `share_user` (string|null): Public contributor name used when publishing a subscription or workflow.; `subscribe_id` (integer|null): Persistent subscription ID returned by subscription.list.; `total_episode` (integer|null; default `0`): Expected total episode count for the subscription.; `total_tracks` (integer|null): Expected or recorded track count for a music item.; `type` (string|null): MoviePilot media or storage item type required by the selected operation.; `vote` (number|null; default `0.0`): Media vote average stored with the subscription.; `year` (string|null): Release or premiere year used to disambiguate the media title.

### `subscription.get`
`GET /api/v1/subscribe/{subscribe_id}`; policy effect: `safe_read`.
Purpose: Read one accessible subscription by persistent subscription ID.
- `path_params`: `subscribe_id*` (integer): Persistent subscription ID returned by subscription.list.
- `query`: none
- `body`: none

### `subscription.history`
`GET /api/v1/subscribe/history/{mtype}`; policy effect: `safe_read`.
Purpose: List completed or archived subscription records.
- `response`: `data` remains a list and the endpoint's documented pagination or limit defaults remain in effect. `collection.result_count` reports the returned items and `collection.total_count` reports the exact total. For a count-only request, use the smallest valid page and read that metadata instead of querying the database after item truncation.
- `path_params`: `mtype*` (string): MoviePilot media type or subscription-history category required by the operation.
- `query`: `count` (integer|null; default `30`): Maximum number of records to return on the requested page.; `page` (integer|null; default `1`): One-based result page number.
- `body`: none

### `subscription.history.delete`
`DELETE /api/v1/subscribe/history/{history_id}`; policy effect: `destructive_write`.
Purpose: Delete one accessible subscription-history record.
- `path_params`: `history_id*` (integer): Persistent transfer- or subscription-history ID returned by a history operation.
- `query`: none
- `body`: none

### `subscription.list`
`GET /api/v1/subscribe/`; policy effect: `safe_read`.
Purpose: List active subscriptions.
- `response`: `data` remains a list; omitting both `page` and `count` keeps the complete legacy result. `collection.result_count` reports the returned items and `collection.total_count` reports the exact pre-pagination total. For counts or summaries, send `page=1,count=1`, read `collection.total_count`, and do not fall back to a database query because the item preview was truncated.
- `path_params`: none
- `query`: `count` (integer|null): Optional page size for a legacy full-list endpoint. Supplying page or count activates pagination; an omitted count then uses 50.; `page` (integer|null): Optional one-based page for a legacy full-list endpoint. Omit both page and count to keep the original unpaginated full result.
- `body`: none

### `subscription.metadata.refresh`
`POST /api/v1/subscribe/check`; policy effect: `external_side_effect`.
Purpose: Start a system-wide refresh of subscription TMDB metadata.
- `path_params`: none
- `query`: none
- `body`: none

### `subscription.popular`
`GET /api/v1/subscribe/popular`; policy effect: `safe_read`.
Purpose: List globally popular subscriptions with filters and pagination.
- `response`: `data` remains a list and `collection.result_count` reports the returned items. `collection.total_count` is omitted because this endpoint or its upstream source does not expose a total.
- `path_params`: none
- `query`: `count` (integer|null; default `30`): Maximum number of records to return on the requested page.; `genre_id` (integer|null): Genre identifier used to filter shared or popular subscriptions.; `max_rating` (number|null): Maximum rating used to filter shared or popular subscriptions.; `min_rating` (number|null): Minimum rating used to filter shared or popular subscriptions.; `min_sub` (integer|null): Minimum subscriber count used to filter popular subscriptions.; `page` (integer|null; default `1`): One-based result page number.; `sort_type` (string|null): Ascending or descending order used by shared or popular subscriptions.; `stype*` (string): Popular-subscription category requested by the endpoint.
- `body`: none

### `subscription.refresh`
`POST /api/v1/subscribe/refresh`; policy effect: `external_side_effect`.
Purpose: Start the configured system-wide subscription refresh job.
- `path_params`: none
- `query`: none
- `body`: none

### `subscription.reset`
`POST /api/v1/subscribe/reset/{subid}`; policy effect: `reversible_write`.
Purpose: Reset one accessible subscription so it can be processed again.
- `path_params`: `subid*` (integer): Persistent subscription ID whose status or processing state will change.
- `query`: none
- `body`: none

### `subscription.search`
`POST /api/v1/subscribe/search/{subscribe_id}`; policy effect: `external_side_effect`.
Purpose: Run an immediate search for one existing subscription.
- `path_params`: `subscribe_id*` (integer): Persistent subscription ID returned by subscription.list.
- `query`: none
- `body`: none

### `subscription.search_all`
`POST /api/v1/subscribe/search`; policy effect: `external_side_effect`.
Purpose: Start immediate searches for all subscriptions accessible to the current user.
- `path_params`: none
- `query`: none
- `body`: none

### `subscription.share`
`POST /api/v1/subscribe/share`; policy effect: `external_side_effect`.
Purpose: Publish one accessible subscription to the MoviePilot sharing service.
- `path_params`: none
- `query`: none
- `body`: `audio_format` (string|null): Requested or recorded audio container or codec, such as FLAC or MP3.; `audio_quality` (string|null): Subscription audio-quality rule, such as hires, lossless, or lossy.; `backdrop` (string|null): Backdrop image URL stored with the media or subscription.; `count` (integer|null; default `0`): Maximum number of records to return on the requested page.; `custom_words` (string|null): Custom recognition or rename words applied to this media workflow.; `date` (string|null): Record creation or completion timestamp used by the history item.; `description` (string|null): Human-readable media, torrent, or subscription description.; `effect` (string|null): Video or release-effect filter expression used by the subscription.; `episode_group` (string|null): TMDB episode-group identifier used for alternate episode ordering.; `exclude` (string|null): Regular expression or filter expression that rejects matching releases.; `id` (integer|null): Persistent database identifier of the supplied record.; `include` (string|null): Regular expression or filter expression that a release must match.; `keyword` (string|null): Case-insensitive substring used to discover settings or filter storage entries.; `media_category` (string|null): MoviePilot library category assigned to the media.; `media_category_id` (string|null): Stable classification category ID; preserve it separately from the current category path snapshot.; `media_id` (string|null): Source-native media ID. Always pair it with the exact media_source returned by search.; `media_source` (MediaSource|null): Metadata source identifier. Preserve the exact value returned with media_id.; `min_bit_depth` (integer|null): Minimum acceptable audio bit depth in bits.; `min_bitrate` (integer|null): Minimum acceptable audio bitrate in bits per second.; `min_sample_rate` (integer|null): Minimum acceptable audio sample rate in hertz.; `music_type` (string|null): Music identity level: recording, album, or artist where supported.; `name` (string|null): Human-readable name of the site, storage item, subscription, or rule group.; `poster` (string|null): Poster image URL stored with the media or subscription.; `quality` (string|null): Video or release quality filter expression.; `resolution` (string|null): Video resolution filter expression, such as 1080p or 2160p.; `season` (integer|null): Season number used by the media, search, subscription, or transfer operation.; `share_comment` (string|null): Optional explanatory comment published with a shared item.; `share_title` (string|null): Public title used when publishing a subscription or workflow.; `share_uid` (string|null): Exact MoviePilot Server sharing-user ID to follow or unfollow.; `share_user` (string|null): Public contributor name used when publishing a subscription or workflow.; `subscribe_id` (integer|null): Persistent subscription ID returned by subscription.list.; `total_episode` (integer|null; default `0`): Expected total episode count for the subscription.; `total_tracks` (integer|null): Expected or recorded track count for a music item.; `type` (string|null): MoviePilot media or storage item type required by the selected operation.; `vote` (number|null; default `0.0`): Media vote average stored with the subscription.; `year` (string|null): Release or premiere year used to disambiguate the media title.

### `subscription.share.delete`
`DELETE /api/v1/subscribe/share/{share_id}`; policy effect: `external_side_effect`.
Purpose: Delete one shared-subscription publication by share ID.
- `path_params`: `share_id*` (integer): Persistent MoviePilot Server share ID returned by a share-list operation.
- `query`: none
- `body`: none

### `subscription.share.statistics`
`GET /api/v1/subscribe/share/statistics`; policy effect: `safe_read`.
Purpose: Read aggregate contribution and reuse counts for subscription sharers.
- `response`: `data` remains a list; omitting both `page` and `count` keeps the complete legacy result. `collection.result_count` reports the returned items and `collection.total_count` reports the exact pre-pagination total. For counts or summaries, send `page=1,count=1`, read `collection.total_count`, and do not fall back to a database query because the item preview was truncated.
- `path_params`: none
- `query`: `count` (integer|null): Optional page size for a legacy full-list endpoint. Supplying page or count activates pagination; an omitted count then uses 50.; `page` (integer|null): Optional one-based page for a legacy full-list endpoint. Omit both page and count to keep the original unpaginated full result.
- `body`: none

### `subscription.shares`
`GET /api/v1/subscribe/shares`; policy effect: `safe_read`.
Purpose: List shared subscriptions with filters and pagination.
- `response`: `data` remains a list and `collection.result_count` reports the returned items. `collection.total_count` is omitted because this endpoint or its upstream source does not expose a total.
- `path_params`: none
- `query`: `count` (integer|null; default `30`): Maximum number of records to return on the requested page.; `genre_id` (integer|null): Genre identifier used to filter shared or popular subscriptions.; `max_rating` (number|null): Maximum rating used to filter shared or popular subscriptions.; `min_rating` (number|null): Minimum rating used to filter shared or popular subscriptions.; `name` (string|null): Human-readable name of the site, storage item, subscription, or rule group.; `page` (integer|null; default `1`): One-based result page number.; `sort_type` (string|null): Ascending or descending order used by shared or popular subscriptions.
- `body`: none

### `subscription.status.update`
`PUT /api/v1/subscribe/status/{subid}`; policy effect: `reversible_write`.
Purpose: Set one accessible subscription to running, paused, or stopped state.
- `path_params`: `subid*` (integer): Persistent subscription ID whose status or processing state will change.
- `query`: `state*` (string): Current site, subscription, marketplace, or transfer state filter.
- `body`: none

### `subscription.update`
`PUT /api/v1/subscribe/`; policy effect: `reversible_write`.
Purpose: Update one existing movie, TV, or music subscription.
- `path_params`: none
- `query`: none
- `body`: `audio_format` (string|null): Requested or recorded audio container or codec, such as FLAC or MP3.; `audio_quality` (string|null): Subscription audio-quality rule, such as hires, lossless, or lossy.; `backdrop` (string|null): Backdrop image URL stored with the media or subscription.; `best_version` (integer|null): Enable normal best-version upgrading when set to 1.; `best_version_full` (integer|null): Enable full best-version upgrading when set to 1.; `classification_policy_revision` (integer|null): Policy revision that produced the persisted classification snapshot.; `classification_rule_id` (string|null): Stable rule ID that selected the persisted classification category.; `classification_source` (string|null): Selection source recorded with the persisted classification snapshot.; `completed_episode` (integer|null): Highest episode number already completed for the subscription.; `current_audio_format` (string|null): Audio format of the best version currently held.; `current_bit_depth` (integer|null): Bit depth of the best version currently held.; `current_bitrate` (integer|null): Bitrate of the best version currently held.; `current_priority` (integer|null): Calculated priority of the best version currently held.; `current_sample_rate` (integer|null): Sample rate of the best version currently held.; `custom_words` (string|null): Custom recognition or rename words applied to this media workflow.; `date` (string|null): Record creation or completion timestamp used by the history item.; `description` (string|null): Human-readable media, torrent, or subscription description.; `downloader` (string|null): Configured downloader instance name.; `effect` (string|null): Video or release-effect filter expression used by the subscription.; `episode_group` (string|null): TMDB episode-group identifier used for alternate episode ordering.; `episode_priority` (object|null): Per-episode best-version priority state.; `exclude` (string|null): Regular expression or filter expression that rejects matching releases.; `execution_status` (SubscriptionExecutionStatus|null): Current subscription execution status returned with the subscription snapshot.; `filter` (string|null): Named filter rule or rule expression applied to this site or subscription.; `filter_groups` (array<string>|null): Ordered filter-rule group names applied to the subscription.; `id` (integer|null): Persistent database identifier of the supplied record.; `include` (string|null): Regular expression or filter expression that a release must match.; `keyword` (string|null): Case-insensitive substring used to discover settings or filter storage entries.; `lack_episode` (integer|null; default `0`): Number of episodes still missing from the subscription.; `last_search` (string|null): Read-only UTC timestamp of the most recent subscription search attempt.; `last_update` (string|null): Timestamp of the subscription's most recent update.; `media_category` (string|null): MoviePilot library category assigned to the media.; `media_category_id` (string|null): Stable classification category ID; preserve it separately from the current category path snapshot.; `media_id` (string|null): Source-native media ID. Always pair it with the exact media_source returned by search.; `media_source` (MediaSource|null): Metadata source identifier. Preserve the exact value returned with media_id.; `min_bit_depth` (integer|null): Minimum acceptable audio bit depth in bits.; `min_bitrate` (integer|null): Minimum acceptable audio bitrate in bits per second.; `min_sample_rate` (integer|null): Minimum acceptable audio sample rate in hertz.; `music_type` (string|null): Music identity level: recording, album, or artist where supported.; `name` (string|null): Human-readable name of the site, storage item, subscription, or rule group.; `note` (array<integer>|null): Structured auxiliary metadata stored with the record.; `poster` (string|null): Poster image URL stored with the media or subscription.; `quality` (string|null): Video or release quality filter expression.; `resolution` (string|null): Video resolution filter expression, such as 1080p or 2160p.; `save_path` (string|null): Configured downloader-side save path for the download or subscription.; `search_imdbid` (integer|null; default `0`): Use IMDb identity during subscription search when set to 1.; `search_interval` (integer|null): Scheduled search interval in whole hours (1-8760); null uses the system interval.; `season` (integer|null): Season number used by the media, search, subscription, or transfer operation.; `sites` (array<integer>|null): Exact site IDs included in the search or subscription scope.; `start_episode` (integer|null; default `0`): First episode number requested by the subscription.; `state` (string|null): Current site, subscription, marketplace, or transfer state filter.; `total_episode` (integer|null; default `0`): Expected total episode count for the subscription.; `total_tracks` (integer|null): Expected or recorded track count for a music item.; `type` (string|null): MoviePilot media or storage item type required by the selected operation.; `username` (string|null): MoviePilot or site username required by the selected operation.; `vote` (number|null; default `0.0`): Media vote average stored with the subscription.; `year` (string|null): Release or premiere year used to disambiguate the media title.

### `subscription.user.list`
`GET /api/v1/subscribe/user/{username}`; policy effect: `safe_read`.
Purpose: List public subscriptions owned by one accessible MoviePilot username.
- `response`: `data` remains a list; omitting both `page` and `count` keeps the complete legacy result. `collection.result_count` reports the returned items and `collection.total_count` reports the exact pre-pagination total. For counts or summaries, send `page=1,count=1`, read `collection.total_count`, and do not fall back to a database query because the item preview was truncated.
- `path_params`: `username*` (string): MoviePilot or site username required by the selected operation.
- `query`: `count` (integer|null): Optional page size for a legacy full-list endpoint. Supplying page or count activates pagination; an omitted count then uses 50.; `page` (integer|null): Optional one-based page for a legacy full-list endpoint. Omit both page and count to keep the original unpaginated full result.
- `body`: none

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
