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
