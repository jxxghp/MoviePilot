# Torrent Cache APIs

Torrent-cache inspection, refresh, re-identification, and deletion.

## Operations

### `torrent.cache.clear`
`DELETE /api/v1/torrent/cache`; policy effect: `destructive_write`.
Purpose: Delete every cached torrent context.
- `path_params`: none
- `query`: none
- `body`: none

### `torrent.cache.delete`
`DELETE /api/v1/torrent/cache/{domain}/{torrent_hash}`; policy effect: `destructive_write`.
Purpose: Delete one cached torrent context by site domain and cache hash.
- `path_params`: `domain*` (string): Site hostname or domain used for matching and requests.; `torrent_hash*` (string): Cache hash returned by torrent.cache.get for one exact site-domain entry.
- `query`: none
- `body`: none

### `torrent.cache.get`
`GET /api/v1/torrent/cache`; policy effect: `safe_read`.
Purpose: Inspect cached torrent contexts and their recognized media identities.
- `path_params`: none
- `query`: none
- `body`: none

### `torrent.cache.refresh`
`POST /api/v1/torrent/cache/refresh`; policy effect: `external_side_effect`.
Purpose: Refresh torrent caches from configured RSS or spider sources.
- `path_params`: none
- `query`: none
- `body`: none

### `torrent.cache.reidentify`
`POST /api/v1/torrent/cache/reidentify/{domain}/{torrent_hash}`; policy effect: `reversible_write`.
Purpose: Replace or recompute the media identity for one cached torrent context.
- `path_params`: `domain*` (string): Site hostname or domain used for matching and requests.; `torrent_hash*` (string): Cache hash returned by torrent.cache.get for one exact site-domain entry.
- `query`: `media_id` (string|null): Source-native media ID. Always pair it with the exact media_source returned by search.; `media_source` (MediaSource|null): Metadata source identifier. Preserve the exact value returned with media_id.; `music_type` (string(recording,album)|null): Music identity level: recording, album, or artist where supported.
- `body`: none
