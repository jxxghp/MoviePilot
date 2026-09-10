# Search APIs

Title and torrent search result discovery and recommendation search.

## Operations

### `search.recommend`
`POST /api/v1/search/recommend`; policy effect: `external_side_effect`.
Purpose: Use the configured recommendation model to rank or recommend torrent search results.
- `path_params`: none
- `query`: none
- `body`: `check_only` (boolean; default `False`): Validate or preview the recommendation without applying search-result filtering.; `filtered_indices` (array<integer>|null): Zero-based search-result indices selected by the recommendation model.; `force` (boolean; default `False`): Force a marketplace refresh or plugin installation when true.

### `search.results`
`GET /api/v1/search/last/context`; policy effect: `safe_read`.
Purpose: Read the most recent torrent-search context and result set.
- `path_params`: none
- `query`: none
- `body`: none

### `search.title`
`GET /api/v1/search/title`; policy effect: `external_side_effect`.
Purpose: Search torrent sites directly from a free-form title and optional media filters.
- `response`: `data` remains a list and `collection.result_count` reports the returned items. `collection.total_count` is omitted because this endpoint or its upstream source does not expose a total.
- `path_params`: none
- `query`: `keyword` (string|null): Case-insensitive substring used to discover settings or filter storage entries.; `mtype` (string|null): MoviePilot media type or subscription-history category required by the operation.; `page` (integer|null; default `0`): One-based result page number.; `sites` (string|null): Exact site IDs included in the search or subscription scope.
- `body`: none

### `search.torrents`
`GET /api/v1/search/media/{media_id}`; policy effect: `safe_read`.
Purpose: Search torrent sites for one canonical media identity.
- `response`: `data` remains a list; omitting both `page` and `count` keeps the complete legacy result. `collection.result_count` reports the returned items and `collection.total_count` reports the exact pre-pagination total. For counts or summaries, send `page=1,count=1`, read `collection.total_count`, and do not fall back to a database query because the item preview was truncated.
- `path_params`: `media_id*` (string): Source-native media ID. Always pair it with the exact media_source returned by search.
- `query`: `area` (string|null; default `title`): Optional region filter applied by the torrent search workflow.; `count` (integer|null): Optional page size for a legacy full-list endpoint. Supplying page or count activates pagination; an omitted count then uses 50.; `include_candidates` (boolean; default `False`): Include unconfirmed music resources and related albums for manual review. Defaults to false; candidates have no target media identity and must not be used for automatic download.; `media_source*` (MediaSource): Metadata source identifier. Preserve the exact value returned with media_id.; `mtype` (string|null): MoviePilot media type or subscription-history category required by the operation.; `music_type` (string|null): Music identity level: recording, album, or artist where supported.; `page` (integer|null): Optional one-based page for a legacy full-list endpoint. Omit both page and count to keep the original unpaginated full result.; `season` (string|null): Season number used by the media, search, subscription, or transfer operation.; `sites` (string|null): Exact site IDs included in the search or subscription scope.
- `body`: none
