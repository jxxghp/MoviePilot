# Subtitle APIs

Subtitle title and media search.

## Operations

### `subtitle.search.media`
`GET /api/v1/search/subtitle/media/{media_id}`; policy effect: `external_side_effect`.
Purpose: Search subtitle providers for one canonical media identity and optional season or episode.
- `response`: `data` remains a list; omitting both `page` and `count` keeps the complete legacy result. `collection.result_count` reports the returned items and `collection.total_count` reports the exact pre-pagination total. For counts or summaries, send `page=1,count=1`, read `collection.total_count`, and do not fall back to a database query because the item preview was truncated.
- `path_params`: `media_id*` (string): Source-native media ID. Always pair it with the exact media_source returned by search.
- `query`: `count` (integer|null): Optional page size for a legacy full-list endpoint. Supplying page or count activates pagination; an omitted count then uses 50.; `episode` (string|null): Episode number used to narrow a subtitle or media search.; `media_source*` (MediaSource): Metadata source identifier. Preserve the exact value returned with media_id.; `mtype` (string|null): MoviePilot media type or subscription-history category required by the operation.; `page` (integer|null): Optional one-based page for a legacy full-list endpoint. Omit both page and count to keep the original unpaginated full result.; `season` (string|null): Season number used by the media, search, subscription, or transfer operation.; `sites` (string|null): Exact site IDs included in the search or subscription scope.
- `body`: none

### `subtitle.search.title`
`GET /api/v1/search/subtitle/title`; policy effect: `external_side_effect`.
Purpose: Search subtitle providers from a free-form title and optional media filters.
- `response`: `data` remains a list and `collection.result_count` reports the returned items. `collection.total_count` is omitted because this endpoint or its upstream source does not expose a total.
- `path_params`: none
- `query`: `keyword` (string|null): Case-insensitive substring used to discover settings or filter storage entries.; `page` (integer|null; default `0`): One-based result page number.; `sites` (string|null): Exact site IDs included in the search or subscription scope.
- `body`: none
