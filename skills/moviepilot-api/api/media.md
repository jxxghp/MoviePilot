# Media APIs

Media search, details, recognition, scraping, schedules, sources, seasons, people, and automatic classification.

## Automatic Media Classification

Use the versioned `media.classification.*` operations for the current automatic
media-classification policy. The legacy category operations are not Agent
operations; their REST read-only projections remain compatibility endpoints for
other clients.

1. Call `media.classification.fields` when you need the stable field IDs,
   operators, common `options`, source-specific `source_options`, or server limits.
   Save option `value`, not its display label or source annotation.
2. Call `media.classification.policy.get` before editing and preserve its active
   `revision`. A policy update replaces the complete policy, so retain categories,
   rules, fallbacks, and aliases that should remain unchanged.
3. Use `media.classification.policy.validate` for draft-only structural and
   semantic validation. Use `media.classification.policy.preview` with either a
   selected media result or normalized facts to inspect one classification result.
   For bounded recent-history or explicit-fact comparisons, use
   `media.classification.policy.impact`.
4. Publish with `media.classification.policy.update`, sending the complete policy
   and the revision read in step 2 as `expected_revision`. This is an administrator
   confirmation-protected write; a successful response creates the next revision.
5. Use `media.classification.policy.history` to inspect administrator-only
   historical revisions. `media.classification.policy.rollback` publishes one
   selected historical policy as a new revision and also requires the current
   `expected_revision`; it does not rewind the revision counter.

## Operations

### `media.classification.fields`
`GET /api/v1/media/classification/fields`; policy effect: `safe_read`.
Purpose: Read the media-classification field catalog and source capabilities.
- `path_params`: none
- `query`: none
- `body`: none

### `media.classification.policy.get`
`GET /api/v1/media/classification/policy`; policy effect: `safe_read`.
Purpose: Read the active automatic media-classification policy.
- `path_params`: none
- `query`: none
- `body`: none

### `media.classification.policy.history`
`GET /api/v1/media/classification/history`; policy effect: `safe_read`.
Purpose: Read the bounded history of published media-classification policies.
- `path_params`: none
- `query`: none
- `body`: none

### `media.classification.policy.impact`
`POST /api/v1/media/classification/impact`; policy effect: `safe_read`.
Purpose: Estimate how an unpublished media-classification policy changes bounded recent samples.
- `path_params`: none
- `query`: none
- `body`: `example_limit` (integer; default `20`; minimum `0.0`; maximum `50.0`): Maximum number of representative impact examples to return.; `expected_revision*` (integer; minimum `1.0`): Current revision from a preceding read, used to reject concurrent state changes.; `policy*` (ClassificationPolicy-Input): Complete or draft automatic media-classification policy.; `sample_limit` (integer; default `100`; minimum `1.0`; maximum `200.0`): Maximum number of recent records or samples to inspect for impact analysis.; `samples` (array<ClassificationFacts>): Explicit normalized fact samples used for classification impact analysis.

### `media.classification.policy.preview`
`POST /api/v1/media/classification/preview`; policy effect: `safe_read`.
Purpose: Preview automatic media classification for selected media or normalized facts.
- `path_params`: none
- `query`: none
- `body`: `input*` (ClassificationFactsPreviewInput|ClassificationMediaPreviewInput): Media or normalized-facts input used for a classification preview.; `policy` (ClassificationPolicy-Input|null): Complete or draft automatic media-classification policy.

### `media.classification.policy.rollback`
`POST /api/v1/media/classification/rollback/{revision}`; policy effect: `reversible_write`.
Purpose: Publish a selected historical media-classification policy as a new revision.
- `path_params`: `revision*` (integer): Published classification policy revision or expected revision number.
- `query`: none
- `body`: `expected_revision*` (integer; minimum `1.0`): Current revision from a preceding read, used to reject concurrent state changes.

### `media.classification.policy.update`
`PUT /api/v1/media/classification/policy`; policy effect: `reversible_write`.
Purpose: Validate and publish a complete automatic media-classification policy.
- `path_params`: none
- `query`: none
- `body`: `expected_revision*` (integer; minimum `0.0`): Current revision from a preceding read, used to reject concurrent state changes.; `policy*` (ClassificationPolicy-Input): Complete or draft automatic media-classification policy.

### `media.classification.policy.validate`
`POST /api/v1/media/classification/validate`; policy effect: `safe_read`.
Purpose: Validate an unpublished automatic media-classification policy without saving it.
- `path_params`: none
- `query`: none
- `body`: `policy*` (ClassificationPolicy-Input): Complete or draft automatic media-classification policy.

### `media.detail`
`GET /api/v1/media/{media_id}`; policy effect: `safe_read`.
Purpose: Read canonical media details from one selected metadata source.
- `path_params`: `media_id*` (string): Source-native media ID. Always pair it with the exact media_source returned by search.
- `query`: `media_source*` (MediaSource): Metadata source identifier. Preserve the exact value returned with media_id.; `type_name*` (string): Explicit media type name used when source IDs alone are ambiguous.
- `body`: none

### `media.episode_group.seasons`
`GET /api/v1/media/group/seasons/{episode_group}`; policy effect: `safe_read`.
Purpose: List seasons defined by one exact TMDB episode-group identity.
- `response`: `data` remains a list; omitting both `page` and `count` keeps the complete legacy result. `collection.result_count` reports the returned items and `collection.total_count` reports the exact pre-pagination total. For counts or summaries, send `page=1,count=1`, read `collection.total_count`, and do not fall back to a database query because the item preview was truncated.
- `path_params`: `episode_group*` (string): TMDB episode-group identifier used for alternate episode ordering.
- `query`: `count` (integer|null): Optional page size for a legacy full-list endpoint. Supplying page or count activates pagination; an omitted count then uses 50.; `page` (integer|null): Optional one-based page for a legacy full-list endpoint. Omit both page and count to keep the original unpaginated full result.
- `body`: none

### `media.episode_groups`
`GET /api/v1/media/groups/{tmdbid}`; policy effect: `safe_read`.
Purpose: List alternate TMDB episode groups available for one TV media identity.
- `response`: `data` remains a list; omitting both `page` and `count` keeps the complete legacy result. `collection.result_count` reports the returned items and `collection.total_count` reports the exact pre-pagination total. For counts or summaries, send `page=1,count=1`, read `collection.total_count`, and do not fall back to a database query because the item preview was truncated.
- `path_params`: `tmdbid*` (integer): TMDB media ID returned by media search or detail.
- `query`: `count` (integer|null): Optional page size for a legacy full-list endpoint. Supplying page or count activates pagination; an omitted count then uses 50.; `page` (integer|null): Optional one-based page for a legacy full-list endpoint. Omit both page and count to keep the original unpaginated full result.
- `body`: none

### `media.episode_schedule`
`GET /api/v1/tmdb/{tmdbid}/{season}`; policy effect: `safe_read`.
Purpose: Read TMDB episode release information for one season.
- `response`: `data` remains a list; omitting both `page` and `count` keeps the complete legacy result. `collection.result_count` reports the returned items and `collection.total_count` reports the exact pre-pagination total. For counts or summaries, send `page=1,count=1`, read `collection.total_count`, and do not fall back to a database query because the item preview was truncated.
- `path_params`: `season*` (integer): Season number used by the media, search, subscription, or transfer operation.; `tmdbid*` (integer): TMDB media ID returned by media search or detail.
- `query`: `count` (integer|null): Optional page size for a legacy full-list endpoint. Supplying page or count activates pagination; an omitted count then uses 50.; `episode_group` (string|null): TMDB episode-group identifier used for alternate episode ordering.; `page` (integer|null): Optional one-based page for a legacy full-list endpoint. Omit both page and count to keep the original unpaginated full result.
- `body`: none

### `media.person.credits`
`GET /api/v1/{source}/person/credits/{person_id}`; policy effect: `safe_read`.
Purpose: Read one person's credits from the selected metadata source.
- `response`: `data` remains a list and `collection.result_count` reports the returned items. `collection.total_count` is omitted because this endpoint or its upstream source does not expose a total.
- `path_params`: `person_id*` (integer): Source-native person ID.; `source*` (string(douban,tmdb,bangumi,anilist)): Metadata source that owns the person ID.
- `query`: `count` (integer; default `20`; minimum `1`; maximum `50`): Page size used by Bangumi and AniList; other sources ignore it.; `page` (integer; default `1`; minimum `1`): One-based result page number.
- `body`: none

### `media.person.search`
`GET /api/v1/media/search`; policy effect: `safe_read`.
Purpose: Search people and music artists across selected metadata sources.
- `response`: `data` remains a list and `collection.result_count` reports the returned items. `collection.total_count` is omitted because this endpoint or its upstream source does not expose a total.
- `path_params`: none
- `query`: `count` (integer; default `8`): Maximum number of records to return on the requested page.; `media_source` (array<MediaSource>; default `[]`): Metadata source identifier. Preserve the exact value returned with media_id.; `music_type` (string(recording,album,artist)|null): Music identity level: recording, album, or artist where supported.; `page` (integer; default `1`): One-based result page number.; `title*` (string): Media, torrent, subscription, or history title used by the operation.; `type*` (string=person): Literal person, selecting person search instead of media search.
- `body`: none

### `media.recognize`
`GET /api/v1/media/recognize`; policy effect: `safe_read`.
Purpose: Recognize media identity from a title, subtitle, or custom rule context.
- `path_params`: none
- `query`: `custom_words` (string|null): Custom recognition or rename words applied to this media workflow.; `media_source` (MediaSource|null): Metadata source identifier. Preserve the exact value returned with media_id.; `subtitle` (string|null): Optional subtitle text used together with title during media recognition.; `title*` (string): Media, torrent, subscription, or history title used by the operation.
- `body`: none

### `media.recognize_file`
`GET /api/v1/media/recognize_file`; policy effect: `safe_read`.
Purpose: Recognize canonical media identity from one exact filename and optional path context.
- `path_params`: none
- `query`: `media_source` (MediaSource|null): Metadata source identifier. Preserve the exact value returned with media_id.; `path*` (string): Storage or history path represented by this record.
- `body`: none

### `media.scrape`
`POST /api/v1/media/scrape/{storage}`; policy effect: `external_side_effect`.
Purpose: Generate or refresh metadata for one storage item.
- `path_params`: `storage*` (string|null): Configured storage name or storage type used by the operation.
- `query`: `media_id` (string|null): Source-native media ID. Always pair it with the exact media_source returned by search.; `media_source` (MediaSource|null): Metadata source identifier. Preserve the exact value returned with media_id.; `music_type` (string|null): Music identity level: recording, album, or artist where supported.; `type_name` (MediaType|null): Explicit media type name used when source IDs alone are ambiguous.
- `body`: `basename` (string|null): Base filename without its parent path.; `children` (array<FileItem-Input>|null): Child storage items nested below this item.; `drive_id` (string|null): Provider-native storage drive identifier.; `extension` (string|null): Filename extension, including or excluding the leading dot as returned by storage.; `fileid` (string|null): Provider-native storage item identifier.; `modify_time` (number|null): Storage item modification timestamp.; `name` (string|null): Human-readable name of the site, storage item, subscription, or rule group.; `parent_fileid` (string|null): Provider-native identifier of the parent storage directory.; `path` (string|null; default `/`): Storage or history path represented by this record.; `pickcode` (string|null): 115 storage pickcode associated with the item.; `size` (integer|null): File or torrent size in bytes.; `storage` (string|null; default `local`): Configured storage name or storage type used by the operation.; `thumbnail` (string|null): Thumbnail URL returned by the storage provider.; `type` (string|null): MoviePilot media or storage item type required by the selected operation.; `url` (string|null): Site, storage, or torrent URL represented by this field.

### `media.search`
`GET /api/v1/media/search`; policy effect: `safe_read`.
Purpose: Search canonical media across selected metadata sources.
- `response`: `data` remains a list and `collection.result_count` reports the returned items. `collection.total_count` is omitted because this endpoint or its upstream source does not expose a total.
- `path_params`: none
- `query`: `count` (integer; default `8`): Maximum number of records to return on the requested page.; `media_source` (array<MediaSource>; default `[]`): Metadata source identifier. Preserve the exact value returned with media_id.; `music_type` (string(recording,album,artist)|null): Music identity level: recording, album, or artist where supported.; `page` (integer; default `1`): One-based result page number.; `title*` (string): Media, torrent, subscription, or history title used by the operation.; `type` (string|null; default `media`): MoviePilot media or storage item type required by the selected operation.
- `body`: none

### `media.seasons`
`GET /api/v1/media/seasons`; policy effect: `safe_read`.
Purpose: List seasons for one exact media identity or a title-and-year fallback.
- `response`: `data` remains a list; omitting both `page` and `count` keeps the complete legacy result. `collection.result_count` reports the returned items and `collection.total_count` reports the exact pre-pagination total. For counts or summaries, send `page=1,count=1`, read `collection.total_count`, and do not fall back to a database query because the item preview was truncated.
- `path_params`: none
- `query`: `count` (integer|null): Optional page size for a legacy full-list endpoint. Supplying page or count activates pagination; an omitted count then uses 50.; `media_id` (string|null): Source-native media ID. Always pair it with the exact media_source returned by search.; `media_source` (MediaSource|null): Metadata source identifier. Preserve the exact value returned with media_id.; `page` (integer|null): Optional one-based page for a legacy full-list endpoint. Omit both page and count to keep the original unpaginated full result.; `season` (integer): Season number used by the media, search, subscription, or transfer operation.; `title` (string|null): Media, torrent, subscription, or history title used by the operation.; `year` (string): Release or premiere year used to disambiguate the media title.
- `body`: none

### `media.sources`
`GET /api/v1/media/source`; policy effect: `safe_read`.
Purpose: List metadata sources currently registered for MoviePilot media operations.
- `response`: `data` remains a list; omitting both `page` and `count` keeps the complete legacy result. `collection.result_count` reports the returned items and `collection.total_count` reports the exact pre-pagination total. For counts or summaries, send `page=1,count=1`, read `collection.total_count`, and do not fall back to a database query because the item preview was truncated.
- `path_params`: none
- `query`: `count` (integer|null): Optional page size for a legacy full-list endpoint. Supplying page or count activates pagination; an omitted count then uses 50.; `page` (integer|null): Optional one-based page for a legacy full-list endpoint. Omit both page and count to keep the original unpaginated full result.
- `body`: none
