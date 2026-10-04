# Music APIs

Music recognition, exploration, album and artist navigation, and recognition-cache administration.

Music organization previews expose local tag/CUE, confirmed online, manual, ambiguous,
conflicting, and temporary failure states separately from file success. For the optional
`music` evidence, bounded candidates and actual release grouping contract, see
[Music preview evidence](transfer.md#music-preview-evidence). Tag IDs alone never imply
online confirmation; album corrections must preserve the returned source-file scope.

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
- Preserve `MusicMeta.music_type` when metadata carries a bound identity. Read
  `media_source`, `media_id`, and `music_type` together; an album ID is not a
  recording ID. The optional `musicbrainz_release_id`,
  `musicbrainz_release_group_id`, and `musicbrainz_release_track_id` refer to
  distinct MusicBrainz entities and never replace the primary identity.
- `MusicMeta.album_type` and `secondary_types` retain declared release types,
  including EP, Single, and Compilation. These differ from the recording/album/
  artist entity type; track count alone must not override a declared release type.
- Preserve independent `composers`, `conductors`, and `orchestras` lists and the
  `performers` instrument/voice-to-names mapping (`performer` for an unspecified
  role). They never become primary artists or artist aliases. Simplified cards
  omit empty roles. Native tags and explicit torrent subtitle roles can provide
  local evidence; complete tags still organize offline. MusicBrainz reads actual
  recording/work relationships and supplements at most three recording details
  per batch within the existing HTTP budget. Known performance conflicts reject
  automatic matches; a shared composer alone does not identify a recording.
  Explicit recording/release IDs can resolve missing relationships, never known
  conflicts. Album matching may verify roles through its actual tracks, without
  broadcasting one track's credits to a compilation. Native ID3, Vorbis/APEv2,
  and MP4 role tags are preserved; standalone ORCHESTRA and MP4 PERFORMER are
  custom compatibility fields, not universal player standards.
- `original_year` and `release_year` distinguish the original and current
  edition; `year` remains the display-compatible value. A release group's first
  release date does not prove the current edition. Preserve `total_discs` and
  `field_sources` in round trips. Sources such as `tag`, `album_tags`, `stream`,
  `filename`, `directory`, `torrent`, `remote`, and `manual` explain field origins;
  they do not independently prove that an online identity was verified.
- CUE-derived fields use the `cue` evidence source. `music_layout=image_cue`
  represents a whole album in one physical audio file; do not identify it as the
  first Recording or assume it has been split. Organization keeps the audio and
  companion `cue_filename` unchanged within the organized album directory.
  `cue_tracks` stores logical tracks and 75-frames-per-second indexes. Split
  `tracks_cue` metadata may supplement tracks, but the original multi-file CUE
  is not copied after track renaming. Treat `organization_error` as a blocking
  structural issue rather than overriding it with an unrelated media ID.
- Writing music tags or embedded covers to a local hardlink/symlink uses an
  independent copy and atomically replaces only the library entry after all
  writes succeed. Seed bytes and permissions remain unchanged. A changed target
  becomes a regular file and consumes independent disk space; unchanged tags
  without other writes retain the link. Skip tags and covers to keep linked
  audio. Failed writes or a concurrently changed target discard the temporary
  copy. LRC and Lyricsfile sidecars also replace directory entries atomically.
  Untagged files can be written, and incorrect extensions use the actual
  container. Native ID3 and MP4/freeform writers keep recording/release/group/
  release-track identities separate. An original year alone writes
  the original-year tag (`ORIGINALDATE` or its native equivalent), never a
  fabricated current-release date. Preserve more precise existing dates in the
  same year. APEv2 MusicBrainz keys use underscores, not ID3's spaced descriptions.
  WMA/ASF uses native Title/Author/WM and MusicBrainz properties, with legacy
  lowercase aliases accepted. WM/TrackNumber is one-based; legacy WM/Track is
  zero-based. WM/Lyrics supplies plain lyrics, and WM/Picture embeds covers under
  the same link-isolation and overwrite policy. Only the actual codec type can
  establish WMA Lossless; high bitrate and filename extensions cannot. Missing
  bit depth stays unknown. WM/Orchestra and WM/Performer are custom compatibility
  properties. Equivalent separate/combined track totals retain existing links.
  AIFF and DSDIFF use the same native ID3 path as WAV and DSF.
- Organization uses each file's `storage`: remote items use names and original
  torrent evidence, never local tags, duration, or CUE at an identical path.
  An `.m4a` suffix alone does not establish AAC/ALAC or lossless status.
  Explicit music transfers reject disc images and archives with an extraction
  message. Explicit movie/TV transfers do not inherit old music identities for
  accompanying audio tracks.
- Album track alignment requires unique identity, title, disc/track position,
  or duration evidence; file order never fills unresolved tracks. Numeric file
  names are weak evidence, while numeric titles in tags remain meaningful.
  Manual album selection may correct names using unique positions, but still
  rejects clear duration conflicts. An incomplete manual alignment stops before
  file operations and asks the caller to check the selected edition and tracks.
- A concrete `musicbrainz_release_id` uses direct release lookup; a release-group
  ID constrains release search. Generic inbox directory names are not album
  evidence. All local logical tracks must align, even for a partial download.
  Conflicting identities, current edition years, and performer tags reject a
  candidate. Near ties with different recording sequences remain unmatched;
  equivalent editions in one release group retain region/script preferences.
  Successful `raw_data.match_score` is a ranking score, not a probability, and
  `match_coverage=1` covers the supplied files, not necessarily the full album.
  CUE images contribute logical tracks/durations and keep an album identity;
  changing the CUE invalidates the directory match cache.
- Track-title discovery uses the Recording search index and its related
  releases. Release search has no `recording` field. At similar relevance,
  releases supported by multiple recordings precede editions of one single.
- Native AcoustID recognition verifies at most five Recording candidates using
  duration, meaningful titles, artists, and versions. A unique high-score match
  with consistent duration can identify untagged or numbered files. Placeholder
  artist tags are missing evidence, while explicit conflicting artists or live
  versions still reject a candidate. Real numeric song tags remain meaningful;
  zero-padded rip numbers and `Track 01` do not establish a trusted local title.
  Near ties or truncated candidates remain `ambiguous`. A native fingerprint
  result has `raw_data.recognition.method=fingerprint`,
  `identity_type=recording`, and `release_verified=false`; supplemental release
  IDs and `album_id` must come from actual tags, with `album_id` retaining only
  a release-group ID. Legacy single-ID plugins keep their text-validation
  contract and are not assigned an invented AcoustID score. Fingerprint work
  shares a 90-second, 16-HTTP-attempt budget, or the existing enclosing budget.
  Cached fingerprints retain candidates and expire after 3600 seconds for a
  hit, 300 seconds for no match, and 15 seconds for a transient failure.
- Unbound music recognition follows the built-in music sources selected in
  `SEARCH_SOURCE`, in configured order; no music selection keeps MusicBrainz as
  the default. Explicit sources, primary IDs and MusicBrainz release evidence
  stay pinned. Never relabel an ID as belonging to another source.
- Each source has eight HTTP attempts and 45 seconds, with at most three sources
  (24 attempts / 135 seconds total). Smaller enclosing budgets still apply;
  changing source cannot reset them. HTTP cache hits consume no request quota.
  A service failure allows the next source; ambiguity or conflict stops fallback.
- MusicBrainz and TheAudioDB can verify mismatched artist names through actual
  source Artist IDs when title, version and year remain compatible. At most three
  distinct Artist IDs are queried per candidate batch, within the existing HTTP
  budget and cache. Romanized names need a source-provided alias; do not infer
  identities from transliteration. Evidence is added to `artist_aliases` without
  changing `artists`, aligned `artist_ids` or primary identities. Album details
  inherit aliases only for the same source and Artist ID. Equal candidates and
  distinct recordings sharing an ISRC remain ambiguous.
- Secondary album catalogs require a complete track list and unique alignment
  covering all local files. They do not prove a release edition:
  `identity_type=album`, `release_verified=false`. Local disc/track numbers,
  totals, release year and original year remain intact. Source order is included
  in the directory cache key.
- Directory successes, misses and transient failures expire after 3600, 300 and
  15 seconds respectively. TheAudioDB and Douban Music HTTP misses expire after
  300 seconds; transport errors, malformed responses and exhausted budgets are
  not cached as misses. MusicBrainz identity-free misses also expire after 300 seconds.
- `MusicInfo.raw_data.recognition` may carry `status`, `message`, `requests`,
  `candidates`, and a `sources` array of per-source reports with a `source` field.
  `ambiguous`, `conflict`, `service_error` and `budget_exhausted` block automatic
  file actions and survive path fallback. Python directory results retain dict
  compatibility and expose the same diagnostics as their `recognition` attribute.
- Clearing/deleting music cache also invalidates HTTP responses and album
  directories. Audio/CUE evidence is reused only within a bounded read-only scan
  and invalidated by file changes or scope exit.
- Transient recognition failures before a file plan exists stay durably accepted
  and retry recognition after 30 seconds, within the existing transfer retry
  budget. Recovery keeps the original release file selection and preferences.
  Retrying a settled music planning rejection with no file/provider operations
  may create a new recognition task; the old history and settlement receipt stay
  available. Existing file-operation evidence always keeps its frozen plan.
  A retry-wait response is pending work, never proof that files were organized.

- Manual MusicBrainz album correction may set `musicbrainz_release_id` to an exact
  Release UUID while keeping `media_id` as the Release Group ID and `music_type=album`.
  The same parameter on `music.album.get` previews that edition's track list. The
  backend verifies group membership even when the embedded releases list is truncated;
  missing or mismatched editions never fall back to regional/script defaults.
  Keep the same edition and source file selection for preview and execution. Explicit
  edition organization currently requires local audio so its tracks can be aligned.

## Operations

### `music.album.get`
`GET /api/v1/music/album/{album_id}`; policy effect: `safe_read`.
Purpose: Read one album's details, tracks, releases, and aligned artist names and IDs.
- Album and track text follows `MUSIC_METADATA_TO_SIMPLIFIED`, matching recognition and final transfer naming. Original aliases, source data, lyrics, and IDs remain unchanged. Recording selection does not override local album-edition evidence, and multi-artist credits remain complete.
- `path_params`: `album_id*` (string): Source-native album ID returned by music search, exploration, or artist-album browsing.
- `query`: `media_source` (MediaSource): Metadata source identifier. Preserve the exact value returned with media_id.; `musicbrainz_release_id` (string|null): Optional exact MusicBrainz Release UUID belonging to the selected album Release Group; never use it as media_id.
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
