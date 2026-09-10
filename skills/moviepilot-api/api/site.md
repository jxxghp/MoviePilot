# Site APIs

Site discovery, authentication, cookies, user data, resources, RSS, priorities, statistics, and lifecycle.

## Operations

### `site.add`
`POST /api/v1/site/`; policy effect: `reversible_write`.
Purpose: Create one configured site with its complete authentication and search settings.
- `path_params`: none
- `query`: none
- `body`: `apikey` (string|null): Site API key used by sites that support API-key authentication.; `cookie` (string|null): Site authentication cookie. Treat this value as a secret.; `domain` (string|null): Site hostname or domain used for matching and requests.; `downloader` (string|null): Configured downloader instance name.; `filter` (string|null): Named filter rule or rule expression applied to this site or subscription.; `id` (integer|null): Persistent database identifier of the supplied record.; `is_active` (boolean|null; default `True`): Whether the configured site is enabled.; `limit_count` (integer|null): Maximum number of site requests allowed in one rate-limit interval.; `limit_interval` (integer|null): Number of requests in the site's rate-limit window.; `limit_seconds` (integer|null): Site rate-limit window length in seconds.; `name` (string|null): Human-readable name of the site, storage item, subscription, or rule group.; `note` (JsonData-Input|null): Structured auxiliary metadata stored with the record.; `pri` (integer|null; default `0`): Site search priority; lower or higher ordering follows the existing site API convention.; `proxy` (integer|null; default `0`): Whether the site uses MoviePilot's configured proxy.; `public` (integer|null; default `0`): Whether the site is treated as a public indexer.; `render` (integer|null; default `0`): Whether site requests require browser rendering.; `rss` (string|null): Site RSS feed URL.; `timeout` (integer|null; default `15`): Per-request site timeout in seconds.; `token` (string|null): Site authentication token. Treat this value as a secret.; `ua` (string|null): Site User-Agent string used for authenticated requests.; `url` (string|null): Site, storage, or torrent URL represented by this field.

### `site.auth.options`
`GET /api/v1/site/auth`; policy effect: `safe_read`.
Purpose: List site-account authentication providers and their required input definitions.
- `path_params`: none
- `query`: none
- `body`: none

### `site.authenticate`
`POST /api/v1/site/auth`; policy effect: `external_side_effect`.
Purpose: Authenticate a supported site account and persist the resulting site authorization state.
- `path_params`: none
- `query`: none
- `body`: `params` (object|null): Provider-defined JSON parameters for the selected authentication or storage action.; `site` (string|null): Source site identifier associated with the torrent result.

### `site.category`
`GET /api/v1/site/category/{site_id}`; policy effect: `safe_read`.
Purpose: List torrent categories supported by one configured site.
- `response`: `data` remains a list; omitting both `page` and `count` keeps the complete legacy result. `collection.result_count` reports the returned items and `collection.total_count` reports the exact pre-pagination total. For counts or summaries, send `page=1,count=1`, read `collection.total_count`, and do not fall back to a database query because the item preview was truncated.
- `path_params`: `site_id*` (integer): Persistent site ID returned by site.list.
- `query`: `count` (integer|null): Optional page size for a legacy full-list endpoint. Supplying page or count activates pagination; an omitted count then uses 50.; `page` (integer|null): Optional one-based page for a legacy full-list endpoint. Omit both page and count to keep the original unpaginated full result.
- `body`: none

### `site.cookie.update`
`POST /api/v1/site/cookie/{site_id}`; policy effect: `reversible_write`.
Purpose: Log in to one site and refresh its stored authentication cookie.
- `path_params`: `site_id*` (integer): Persistent site ID returned by site.list.
- `query`: none
- `body`: `code` (string|null): Two-factor verification code or site-specific authentication secret.; `password*` (string): Site login password. Treat this value as a secret.; `username*` (string): MoviePilot or site username required by the selected operation.

### `site.cookiecloud.sync`
`POST /api/v1/site/cookiecloud`; policy effect: `external_side_effect`.
Purpose: Start a CookieCloud synchronization of configured sites.
- `path_params`: none
- `query`: none
- `body`: none

### `site.delete`
`DELETE /api/v1/site/{site_id}`; policy effect: `destructive_write`.
Purpose: Delete one configured site by persistent site ID.
- `path_params`: `site_id*` (integer): Persistent site ID returned by site.list.
- `query`: none
- `body`: none

### `site.list`
`GET /api/v1/site/agent`; policy effect: `safe_read`.
Purpose: List configured sites with status/name filters; authentication fields are returned only to a superuser.
- `response`: `data` remains a list; omitting both `page` and `count` keeps the complete legacy result. `collection.result_count` reports the returned items and `collection.total_count` reports the exact pre-pagination total. For counts or summaries, send `page=1,count=1`, read `collection.total_count`, and do not fall back to a database query because the item preview was truncated.
- `path_params`: none
- `query`: `count` (integer|null): Optional page size for a legacy full-list endpoint. Supplying page or count activates pagination; an omitted count then uses 50.; `name` (string|null): Human-readable name of the site, storage item, subscription, or rule group.; `page` (integer|null): Optional one-based page for a legacy full-list endpoint. Omit both page and count to keep the original unpaginated full result.; `status` (string(active,inactive,all); default `all`): Transfer success status used to filter history or describe a record.
- `body`: none

### `site.mapping`
`GET /api/v1/site/mapping`; policy effect: `safe_read`.
Purpose: Read the configured site-domain to site-name mapping.
- `path_params`: none
- `query`: none
- `body`: none

### `site.priorities.update`
`POST /api/v1/site/priorities`; policy effect: `reversible_write`.
Purpose: Replace priorities for the supplied configured site IDs.
- `path_params`: none
- `query`: none
- `body*` (array<SitePriorityUpdate>): Request value for site.priorities.update. Replace priorities for the supplied configured site IDs. Use the exact type and fields below.

### `site.reset`
`POST /api/v1/site/reset`; policy effect: `destructive_write`.
Purpose: Delete all configured sites and start a fresh CookieCloud synchronization.
- `path_params`: none
- `query`: none
- `body`: none

### `site.resource`
`GET /api/v1/site/resource/{site_id}`; policy effect: `external_side_effect`.
Purpose: Browse torrent resources from one configured site with category and keyword filters.
- `response`: `data` remains a list and `collection.result_count` reports the returned items. `collection.total_count` is omitted because this endpoint or its upstream source does not expose a total.
- `path_params`: `site_id*` (integer): Persistent site ID returned by site.list.
- `query`: `cat` (string|null): Exact site category identifier returned by site.category.; `keyword` (string|null): Case-insensitive substring used to discover settings or filter storage entries.; `mtype` (string|null): MoviePilot media type or subscription-history category required by the operation.; `page` (integer|null; default `0`): One-based result page number.
- `body`: none

### `site.rss`
`GET /api/v1/site/rss`; policy effect: `safe_read`.
Purpose: List configured sites selected for RSS subscription processing.
- `response`: `data` remains a list; omitting both `page` and `count` keeps the complete legacy result. `collection.result_count` reports the returned items and `collection.total_count` reports the exact pre-pagination total. For counts or summaries, send `page=1,count=1`, read `collection.total_count`, and do not fall back to a database query because the item preview was truncated.
- `path_params`: none
- `query`: `count` (integer|null): Optional page size for a legacy full-list endpoint. Supplying page or count activates pagination; an omitted count then uses 50.; `page` (integer|null): Optional one-based page for a legacy full-list endpoint. Omit both page and count to keep the original unpaginated full result.
- `body`: none

### `site.searchable`
`GET /api/v1/site/media/{media_type}`; policy effect: `safe_read`.
Purpose: List active configured sites supporting one exact media type.
- `response`: `data` remains a list; omitting both `page` and `count` keeps the complete legacy result. `collection.result_count` reports the returned items and `collection.total_count` reports the exact pre-pagination total. For counts or summaries, send `page=1,count=1`, read `collection.total_count`, and do not fall back to a database query because the item preview was truncated.
- `path_params`: `media_type*` (string): MoviePilot media type used to filter recommendations or rule groups.
- `query`: `count` (integer|null): Optional page size for a legacy full-list endpoint. Supplying page or count activates pagination; an omitted count then uses 50.; `page` (integer|null): Optional one-based page for a legacy full-list endpoint. Omit both page and count to keep the original unpaginated full result.
- `body`: none

### `site.statistic`
`GET /api/v1/site/statistic/{site_url}`; policy effect: `safe_read`.
Purpose: Read account and traffic statistics for one exact configured site domain.
- `path_params`: `site_url*` (string): Configured site URL or hostname used to select one site's statistics.
- `query`: none
- `body`: none

### `site.statistics`
`GET /api/v1/site/statistic`; policy effect: `safe_read`.
Purpose: Read the latest account and traffic statistics for all configured sites.
- `response`: `data` remains a list; omitting both `page` and `count` keeps the complete legacy result. `collection.result_count` reports the returned items and `collection.total_count` reports the exact pre-pagination total. For counts or summaries, send `page=1,count=1`, read `collection.total_count`, and do not fall back to a database query because the item preview was truncated.
- `path_params`: none
- `query`: `count` (integer|null): Optional page size for a legacy full-list endpoint. Supplying page or count activates pagination; an omitted count then uses 50.; `page` (integer|null): Optional one-based page for a legacy full-list endpoint. Omit both page and count to keep the original unpaginated full result.
- `body`: none

### `site.supporting`
`GET /api/v1/site/supporting`; policy effect: `safe_read`.
Purpose: List indexer definitions supported by the installed MoviePilot resources.
- `path_params`: none
- `query`: none
- `body`: none

### `site.test`
`GET /api/v1/site/test/{site_id}`; policy effect: `safe_read`.
Purpose: Test connectivity and authentication for one configured site.
- `path_params`: `site_id*` (integer): Persistent site ID returned by site.list.
- `query`: none
- `body`: none

### `site.update`
`PUT /api/v1/site/`; policy effect: `reversible_write`.
Purpose: Update one configured site's complete settings.
- `path_params`: none
- `query`: none
- `body`: `apikey` (string|null): Site API key used by sites that support API-key authentication.; `cookie` (string|null): Site authentication cookie. Treat this value as a secret.; `domain` (string|null): Site hostname or domain used for matching and requests.; `downloader` (string|null): Configured downloader instance name.; `filter` (string|null): Named filter rule or rule expression applied to this site or subscription.; `id` (integer|null): Persistent database identifier of the supplied record.; `is_active` (boolean|null; default `True`): Whether the configured site is enabled.; `limit_count` (integer|null): Maximum number of site requests allowed in one rate-limit interval.; `limit_interval` (integer|null): Number of requests in the site's rate-limit window.; `limit_seconds` (integer|null): Site rate-limit window length in seconds.; `name` (string|null): Human-readable name of the site, storage item, subscription, or rule group.; `note` (JsonData-Input|null): Structured auxiliary metadata stored with the record.; `pri` (integer|null; default `0`): Site search priority; lower or higher ordering follows the existing site API convention.; `proxy` (integer|null; default `0`): Whether the site uses MoviePilot's configured proxy.; `public` (integer|null; default `0`): Whether the site is treated as a public indexer.; `render` (integer|null; default `0`): Whether site requests require browser rendering.; `rss` (string|null): Site RSS feed URL.; `timeout` (integer|null; default `15`): Per-request site timeout in seconds.; `token` (string|null): Site authentication token. Treat this value as a secret.; `ua` (string|null): Site User-Agent string used for authenticated requests.; `url` (string|null): Site, storage, or torrent URL represented by this field.

### `site.userdata`
`GET /api/v1/site/userdata/{site_id}`; policy effect: `safe_read`.
Purpose: Read the latest account statistics collected from one site.
- `response`: `data` remains a list; omitting both `page` and `count` keeps the complete legacy result. `collection.result_count` reports the returned items and `collection.total_count` reports the exact pre-pagination total. For counts or summaries, send `page=1,count=1`, read `collection.total_count`, and do not fall back to a database query because the item preview was truncated.
- `path_params`: `site_id*` (integer): Persistent site ID returned by site.list.
- `query`: `count` (integer|null): Optional page size for a legacy full-list endpoint. Supplying page or count activates pagination; an omitted count then uses 50.; `page` (integer|null): Optional one-based page for a legacy full-list endpoint. Omit both page and count to keep the original unpaginated full result.; `workdate` (string|null): Date used when retrieving one site's historical user statistics.
- `body`: none

### `site.userdata.latest`
`GET /api/v1/site/userdata/latest`; policy effect: `safe_read`.
Purpose: Read the latest collected account statistics for every configured site.
- `response`: `data` remains a list; omitting both `page` and `count` keeps the complete legacy result. `collection.result_count` reports the returned items and `collection.total_count` reports the exact pre-pagination total. For counts or summaries, send `page=1,count=1`, read `collection.total_count`, and do not fall back to a database query because the item preview was truncated.
- `path_params`: none
- `query`: `count` (integer|null): Optional page size for a legacy full-list endpoint. Supplying page or count activates pagination; an omitted count then uses 50.; `page` (integer|null): Optional one-based page for a legacy full-list endpoint. Omit both page and count to keep the original unpaginated full result.
- `body`: none

### `site.userdata.refresh`
`POST /api/v1/site/userdata/{site_id}`; policy effect: `external_side_effect`.
Purpose: Refresh and return account statistics for one configured site.
- `path_params`: `site_id*` (integer): Persistent site ID returned by site.list.
- `query`: none
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
