# Library APIs

Library existence checks and latest-media inspection.

## Operations

### `library.exists`
`GET /api/v1/mediaserver/exists`; policy effect: `safe_read`.
Purpose: Check configured media servers for one canonical media identity.
- `path_params`: none
- `query`: `media_id` (string|null): Source-native media ID. Always pair it with the exact media_source returned by search.; `media_source` (MediaSource|null): Metadata source identifier. Preserve the exact value returned with media_id.; `mtype` (string|null): MoviePilot media type or subscription-history category required by the operation.; `season` (integer|null): Season number used by the media, search, subscription, or transfer operation.; `title` (string|null): Media, torrent, subscription, or history title used by the operation.; `year` (string|null): Release or premiere year used to disambiguate the media title.
- `body`: none

### `library.latest`
`GET /api/v1/mediaserver/latest`; policy effect: `safe_read`.
Purpose: List recently added items from one configured media-server instance for the current user.
- `response`: `data` remains a list and `collection.result_count` reports the returned items. `collection.total_count` is omitted because this endpoint or its upstream source does not expose a total.
- `path_params`: none
- `query`: `count` (integer|null; default `20`): Maximum number of records to return on the requested page.; `server*` (string): Exact configured media-server instance name returned by the media-server instance list.
- `body`: none
