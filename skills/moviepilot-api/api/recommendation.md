# Recommendation APIs

Recommendation listing.

## Operations

### `recommendation.list`
`GET /api/v1/recommend/agent`; policy effect: `safe_read`.
Purpose: Read personalized media or music recommendations.
- `response`: `data` remains a list and `collection.result_count` reports the returned items. `collection.total_count` is omitted because this endpoint or its upstream source does not expose a total.
- `path_params`: none
- `query`: `days` (integer; default `14`): Recommendation time window in days.; `fresh_sort` (string; default `release_date`): Freshness ordering used by the recommendation source.; `future` (boolean; default `True`): Include future recommendation periods when supported.; `media_type` (string; default `all`): MoviePilot media type used to filter recommendations or rule groups.; `min_listen_count` (integer; default `0`): Minimum listen count required for a music recommendation.; `music_type` (string|null): Music identity level: recording, album, or artist where supported.; `page` (integer; default `1`): One-based result page number.; `past` (boolean; default `True`): Include past recommendation periods when supported.; `range_name` (string; default `this_month`): Named recommendation time range.; `sort_by` (string; default `listen_count.desc`): Recommendation field used for ordering results.; `source` (string; default `tmdb_trending`): Exact metadata or recommendation source selected by the operation.; `with_cover` (boolean; default `False`): Require recommendation results to include cover artwork.
- `body`: none
