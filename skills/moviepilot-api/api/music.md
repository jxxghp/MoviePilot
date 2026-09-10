# Music APIs

Music recognition, exploration, album and artist navigation, and recognition-cache administration.

## Music Navigation

- Search titles, albums, or artists with `media.search` using `type=music`. Preserve
  every returned `media_source`, `media_id`, `music_type`, `album_id`, and
  `artist_ids` value instead of matching by display name.
- Use `music.artist.albums` to browse an artist's works. Its `album_type` filter
  distinguishes albums, singles, EPs, compilations, soundtracks, live releases,
  remixes, and the other documented MusicBrainz release-group types.
- Use `music.album.get` to browse from a work back to its artists. The response
  includes aligned `artists` and `artist_ids`, plus tracks and releases; pass one
  returned artist ID to `music.artist.get`, `music.artist.albums`, or
  `music.artist.related` with the same `media_source`.
- Use `music.album.related` for related works and `music.artist.related` for
  related artists. Use `music.explore` for MusicBrainz charts/fresh releases or
  Douban Music tag browsing.
- `music.recognize` resolves only a recording or album. Artist identities are
  browse-only. Music recognition-cache operations are administrator-only; call
  `music.cache.get` before deleting one exact key, and clear all entries only
  after explicit confirmation.
- Cache keys are opaque. Use the exact key returned by `music.cache.get`; do not
  construct one from a title or artist. Recognition caches separate recording,
  album, and unspecified requests, as well as version and ISRC evidence.
- Music resource metadata records applied recognition rules in `apply_words`.
  Explicit subtitle versions participate in matching. A track's `album` field
  does not prove whole-album coverage, even without a track number; keep
  `partial_album` candidates out of automatic downloads.
- TheAudioDB and Douban Music use the same name, artist, and version evidence
  rules. Album lookup uses the album credit without replacing the track's
  performer. Conflicting explicit recording dates are version mismatches;
  missing dates alone do not reject a candidate.

## Operations

### `music.album.get`
`GET /api/v1/music/album/{album_id}`; policy effect: `safe_read`.
Purpose: Read one album's details, tracks, releases, and aligned artist names and IDs.
- `path_params`: `album_id*` (string): Source-native album ID returned by music search, exploration, or artist-album browsing.
- `query`: `media_source` (MediaSource): Metadata source identifier. Preserve the exact value returned with media_id.
- `body`: none

### `music.album.related`
`GET /api/v1/music/album/{album_id}/related`; policy effect: `safe_read`.
Purpose: Browse albums related to one source-native album identity.
- `response`: `data` remains a list and `collection.result_count` reports the returned items. `collection.total_count` is omitted because this endpoint or its upstream source does not expose a total.
- `path_params`: `album_id*` (string): Source-native album ID returned by music search, exploration, or artist-album browsing.
- `query`: `count` (integer; default `24`; minimum `1`; maximum `100`): Maximum number of records to return on the requested page.; `media_source` (MediaSource): Metadata source identifier. Preserve the exact value returned with media_id.
- `body`: none

### `music.artist.albums`
`GET /api/v1/music/artist/{artist_id}/albums`; policy effect: `safe_read`.
Purpose: Browse one artist's albums, singles, EPs, or another exact release-group type.
- `response`: `data` remains a list and `collection.result_count` reports the returned items. `collection.total_count` is omitted because this endpoint or its upstream source does not expose a total.
- `path_params`: `artist_id*` (string): Source-native artist ID returned by music search or an album detail response.
- `query`: `album_type` (string|null): Music album or release-group type used by classification rules.; `count` (integer; default `30`; minimum `1`; maximum `100`): Maximum number of records to return on the requested page.; `media_source` (MediaSource): Metadata source identifier. Preserve the exact value returned with media_id.; `page` (integer; default `1`; minimum `1`): One-based result page number.
- `body`: none

### `music.artist.get`
`GET /api/v1/music/artist/{artist_id}`; policy effect: `safe_read`.
Purpose: Read one artist's canonical details from the selected music metadata source.
- `path_params`: `artist_id*` (string): Source-native artist ID returned by music search or an album detail response.
- `query`: `media_source` (MediaSource): Metadata source identifier. Preserve the exact value returned with media_id.
- `body`: none

### `music.artist.related`
`GET /api/v1/music/artist/{artist_id}/related`; policy effect: `safe_read`.
Purpose: Browse artists related to one source-native artist identity.
- `response`: `data` remains a list and `collection.result_count` reports the returned items. `collection.total_count` is omitted because this endpoint or its upstream source does not expose a total.
- `path_params`: `artist_id*` (string): Source-native artist ID returned by music search or an album detail response.
- `query`: `count` (integer; default `24`; minimum `1`; maximum `100`): Maximum number of records to return on the requested page.; `media_source` (MediaSource): Metadata source identifier. Preserve the exact value returned with media_id.
- `body`: none

### `music.cache.clear`
`DELETE /api/v1/music/cache`; policy effect: `destructive_write`.
Purpose: Clear the complete administrator-only MusicBrainz recognition cache.
- `path_params`: none
- `query`: none
- `body`: none

### `music.cache.delete`
`DELETE /api/v1/music/cache/{cache_key}`; policy effect: `destructive_write`.
Purpose: Delete one administrator-only MusicBrainz recognition-cache entry by exact key.
- `path_params`: `cache_key*` (string): Exact recognition-cache key returned by music.cache.get.
- `query`: none
- `body`: none

### `music.cache.get`
`GET /api/v1/music/cache`; policy effect: `safe_read`.
Purpose: Inspect the administrator-only MusicBrainz recognition cache and summary counts.
- `path_params`: none
- `query`: none
- `body`: none

### `music.explore`
`GET /api/v1/music/explore`; policy effect: `safe_read`.
Purpose: Browse MusicBrainz charts or fresh releases, or Douban Music tag categories.
- `response`: `data` remains a list and `collection.result_count` reports the returned items. `collection.total_count` is omitted because this endpoint or its upstream source does not expose a total.
- `path_params`: none
- `query`: `count` (integer; default `30`; minimum `1`; maximum `100`): Maximum number of records to return on the requested page.; `days` (integer; default `14`; minimum `1`; maximum `90`): Fresh-release lookback/lookahead window, from 1 through the endpoint maximum.; `douban_sort` (string; default `U`): Douban Music order: U comprehensive, S rating, R newest, or O hottest.; `entity` (string; default `recording`): Chart entity: recording for tracks or album for release groups. Fresh results are albums.; `future` (boolean; default `True`): Include releases after today in fresh mode.; `media_source` (MediaSource): Music exploration source. Use musicbrainz for chart/fresh modes or doubanmusic for tag browsing.; `min_listen_count` (integer; default `0`; minimum `0`): Minimum ListenBrainz listen count in chart mode.; `mode` (string; default `chart`): MusicBrainz mode: chart reads listening charts; fresh reads new album releases.; `page` (integer; default `1`; minimum `1`): One-based result page number.; `past` (boolean; default `True`): Include releases before today in fresh mode.; `range_name` (string; default `this_month`): ListenBrainz chart range: this_week, this_month, this_year, week, month, or year.; `sort` (string; default `release_date`): Fresh-release order accepted by the current ListenBrainz implementation.; `sort_by` (string; default `listen_count.desc`): ListenBrainz chart order: listen_count.desc or listen_count.asc.; `tags` (string; default ``): Comma-separated Douban Music tags used only when media_source is doubanmusic.; `with_cover` (boolean; default `False`): Keep only results with cover artwork when true.
- `body`: none

### `music.recognize`
`POST /api/v1/music/recognize`; policy effect: `safe_read`.
Purpose: Resolve one recording or album from an exact music source and source-native ID.
- `path_params`: none
- `query`: none
- `body`: `media_id*` (string): Source-native media ID. Always pair it with the exact media_source returned by search.; `media_source*` (MediaSource): Metadata source identifier. Preserve the exact value returned with media_id.; `music_type` (string(recording,album)|null): Music identity level: recording, album, or artist where supported.
