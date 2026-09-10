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
- `path_params`: `cache_key*` (string): Exact recognition-cache key returned by the corresponding recognition-cache get operation.
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
