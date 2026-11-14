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
- `MUSIC_CUE_ENABLE` defaults to true. For audio already split into tracks with
  invalid companion CUE files, users can disable Music CUE recognition under
  Settings > System > Advanced > Media and preview or submit organization again.
  Disabled mode uses audio tags and filenames without reading, validating, or
  automatically archiving CUE files. The manual organization dialog also provides
  a per-task CUE switch initialized from the system setting. For
  `POST /api/v1/transfer/manual`, `music_cue_enable: false/true` overrides this
  task only; omission or null inherits the system default. Use the same value
  for preview and execution. Album caches separate effective modes, and admitted
  tasks retain their mode across queue execution and restart recovery. Existing
  transfer plans remain unchanged.
  Keep it enabled for whole-album audio images.
- When enabled, CUE-derived fields use the `cue` evidence source. `music_layout=image_cue`
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
  `MUSIC_LYRICS_TO_SIMPLIFIED` defaults to false and independently converts
  generated LRC/TXT lyric text to Simplified Chinese for local and remote writes.
  Timestamps, LRC tags, candidates, and source Lyricsfile remain unchanged.
  Existing lyrics still follow quality and overwrite policies; use Always Scrape
  to regenerate them with this setting.
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
Purpose: 读取专辑详情、曲目、发行版本，以及按位置对应的艺术家名称和标识。
Identity scope: `public`; required role: `user`.
- Album and track text follows `MUSIC_METADATA_TO_SIMPLIFIED`, matching recognition and final transfer naming. Original aliases, source data, lyrics, and IDs remain unchanged. Recording selection does not override local album-edition evidence, and multi-artist credits remain complete.
- `path_params`: `album_id*` (string): 音乐搜索、发现或艺术家专辑列表返回的来源原生专辑标识。
- `query`: `media_source` (MediaSource): 元数据来源标识，保留与 media_id 一同返回的精确值。; `musicbrainz_release_id` (string|null): 选定专辑发行组下的精确 MusicBrainz Release UUID，可选；不得作为 media_id 使用。
- `body`: none

### `music.album.related`
`GET /api/v1/music/album/{album_id}/related`; policy effect: `safe_read`.
Purpose: 按来源原生专辑标识浏览相关专辑。
Identity scope: `public`; required role: `user`.
- `response`: `data` remains a list and `collection.result_count` reports the returned items. `collection.total_count` is omitted because this endpoint or its upstream source does not expose a total.
- `path_params`: `album_id*` (string): 音乐搜索、发现或艺术家专辑列表返回的来源原生专辑标识。
- `query`: `count` (integer; default `24`; minimum `1`; maximum `100`): 当前页最多返回的记录数。; `media_source` (MediaSource): 元数据来源标识，保留与 media_id 一同返回的精确值。
- `body`: none

### `music.artist.albums`
`GET /api/v1/music/artist/{artist_id}/albums`; policy effect: `safe_read`.
Purpose: 浏览艺术家的专辑、单曲、EP 或其他精确发行组类型。
Identity scope: `public`; required role: `user`.
- `response`: `data` remains a list and `collection.result_count` reports the returned items. `collection.total_count` is omitted because this endpoint or its upstream source does not expose a total.
- `path_params`: `artist_id*` (string): 音乐搜索或专辑详情返回的来源原生艺术家标识。
- `query`: `album_type` (string|null): 音乐专辑或发行组类型；音乐发现可用 album、single、ep、broadcast、other、compilation、soundtrack、live、remix 筛选。; `count` (integer; default `30`; minimum `1`; maximum `100`): 当前页最多返回的记录数。; `media_source` (MediaSource): 元数据来源标识，保留与 media_id 一同返回的精确值。; `page` (integer; default `1`; minimum `1`): 从 1 开始的结果页码。
- `body`: none

### `music.artist.get`
`GET /api/v1/music/artist/{artist_id}`; policy effect: `safe_read`.
Purpose: 从选定音乐元数据来源读取艺术家的规范详情。
Identity scope: `public`; required role: `user`.
- `path_params`: `artist_id*` (string): 音乐搜索或专辑详情返回的来源原生艺术家标识。
- `query`: `media_source` (MediaSource): 元数据来源标识，保留与 media_id 一同返回的精确值。
- `body`: none

### `music.artist.related`
`GET /api/v1/music/artist/{artist_id}/related`; policy effect: `safe_read`.
Purpose: 按来源原生艺术家标识浏览相关艺术家。
Identity scope: `public`; required role: `user`.
- `response`: `data` remains a list and `collection.result_count` reports the returned items. `collection.total_count` is omitted because this endpoint or its upstream source does not expose a total.
- `path_params`: `artist_id*` (string): 音乐搜索或专辑详情返回的来源原生艺术家标识。
- `query`: `count` (integer; default `24`; minimum `1`; maximum `100`): 当前页最多返回的记录数。; `media_source` (MediaSource): 元数据来源标识，保留与 media_id 一同返回的精确值。
- `body`: none

### `music.cache.clear`
`DELETE /api/v1/music/cache`; policy effect: `destructive_write`.
Purpose: 清空仅管理员可访问的 MusicBrainz 识别缓存。
Identity scope: `admin`; required role: `system_admin`.
- `path_params`: none
- `query`: none
- `body`: none

### `music.cache.delete`
`DELETE /api/v1/music/cache/{cache_key}`; policy effect: `destructive_write`.
Purpose: 按精确键删除一条管理员可访问的 MusicBrainz 识别缓存。
Identity scope: `admin`; required role: `system_admin`.
- `path_params`: `cache_key*` (string): 对应识别缓存查询操作返回的精确缓存键。
- `query`: none
- `body`: none

### `music.cache.get`
`GET /api/v1/music/cache`; policy effect: `safe_read`.
Purpose: 查看仅管理员可访问的 MusicBrainz 识别缓存及统计数量。
Identity scope: `admin`; required role: `system_admin`.
- `path_params`: none
- `query`: none
- `body`: none

### `music.explore`
`GET /api/v1/music/explore`; policy effect: `safe_read`.
Purpose: 浏览 MusicBrainz 榜单和新发行，或豆瓣音乐标签分类。
Identity scope: `public`; required role: `user`.
- `response`: `data` remains a list and `collection.result_count` reports the returned items. `collection.total_count` is omitted because this endpoint or its upstream source does not expose a total.
- `path_params`: none
- `query`: `count` (integer; default `30`; minimum `1`; maximum `100`): 当前页最多返回的记录数。; `days` (integer; default `14`; minimum `1`; maximum `90`): 新发行专辑的回溯或前瞻天数，最小为 1，最大值以端点限制为准。; `douban_sort` (string; default `U`): 豆瓣音乐排序：U 综合、S 评分、R 最新、O 最热。; `entity` (string; default `recording`): 榜单对象：recording 表示曲目，album 表示发行组；fresh 模式返回专辑。; `future` (boolean; default `True`): fresh 模式是否包含今天之后发行的专辑。; `media_source` (MediaSource): 音乐探索来源：musicbrainz 支持 chart 和 fresh 模式；doubanmusic 按标签浏览。; `min_listen_count` (integer; default `0`; minimum `0`): chart 模式的最低 ListenBrainz 收听次数。; `mode` (string; default `chart`): MusicBrainz 模式：chart 查询收听榜单，fresh 查询新发行专辑。; `page` (integer; default `1`; minimum `1`): 从 1 开始的结果页码。; `past` (boolean; default `True`): fresh 模式是否包含今天之前发行的专辑。; `range_name` (string; default `this_month`): ListenBrainz 榜单时间范围：this_week、this_month、this_year、week、month 或 year。; `sort` (string; default `release_date`): 当前 ListenBrainz 实现接受的新发行专辑排序方式。; `sort_by` (string; default `listen_count.desc`): ListenBrainz 榜单排序：listen_count.desc 按收听次数降序，listen_count.asc 按升序。; `tags` (string; default ``): 逗号分隔的豆瓣音乐标签，仅在 media_source 为 doubanmusic 时使用。; `with_cover` (boolean; default `False`): 为 True 时仅保留有封面图片的结果。
- `body`: none

### `music.recognize`
`POST /api/v1/music/recognize`; policy effect: `safe_read`.
Purpose: 按精确音乐来源及其原生标识解析一首录音或一张专辑。
Identity scope: `public`; required role: `user`.
- `path_params`: none
- `query`: none
- `body`: `media_id*` (string): 数据源原生 ID; `media_source*` (MediaSource): 元数据来源标识，保留与 media_id 一同返回的精确值。; `music_type` (string(recording,album)|null): 音乐实体类型：recording 单曲、album 专辑

## Body Models

This category document is self-contained: the shared models below are included so the Agent does not need a second Skill document before calling the API.

### `ClassificationFacts`
自动分类策略求值的规范化媒体事实。
- `extensions` (object): 按数据源命名空间隔离的插件扩展事实
- `field_sources` (object): 跨来源补充事实到实际 provider 的映射
- `identity*` (ClassificationIdentityFacts): 分类事实使用的稳定来源原生媒体身份。
- `media*` (ClassificationMediaFacts): 分类预览使用的媒体元数据输入。
- `music` (ClassificationMusicFacts|null): 音乐专用事实

### `ClassificationFactsPreviewInput`
直接提供给分类预览的规范化事实。
- `facts*` (ClassificationFacts): 分类预览时求值的规范化媒体事实。
- `kind` (string=facts; default `facts`): 预览输入类型

### `ClassificationIdentityFacts`
分类求值使用的稳定来源原生身份。
- `media_id*` (string): 当前数据源原生媒体 ID
- `media_source*` (string): 当前主身份数据源

### `ClassificationMediaFacts`
分类规则使用的规范化电影、电视剧或共用媒体事实。
- `adult` (boolean|null): 是否为成人内容
- `companies` (array<string>|null): 出品公司或工作室名称
- `content_rating` (string|null): 内容分级
- `countries` (array<string>|null): 标准国家或地区代码
- `genre_keys` (array<string>|null): MoviePilot 规范化类型键
- `genre_names` (array<string>|null): 数据源提供的类型名称
- `language` (string|null): 标准语言代码
- `networks` (array<string>|null): 电视台或播出平台名称
- `runtime` (integer|null): 影视时长，单位为分钟
- `title` (string|null): 标准媒体标题
- `type*` (string): 标准媒体类型
- `year` (integer|null): 标准发行或首播年份

### `ClassificationMediaPreviewInput`
提供给分类预览的选定媒体搜索结果。
- `kind` (string=media; default `media`): 预览输入类型
- `media*` (object): 从媒体搜索结果选择的媒体信息

### `ClassificationMusicFacts`
自动分类规则使用的音乐专用事实。
- `album_type` (string|null): 专辑主类型
- `artist_country` (string|null): 艺术家国家或地区
- `artists` (array<string>|null): 艺术家名称
- `entity_type` (string|null): 音乐实体类型
- `genres` (array<string>|null): 标准化音乐流派
- `release_status` (string|null): 发行状态
- `secondary_types` (array<string>|null): 专辑副类型
- `tags` (array<string>|null): 数据源音乐标签

### `ClassificationPolicy-Input`
完整且带版本的自动媒体分类策略。
- `categories` (array<ClassificationCategory>): 稳定分类定义列表
- `enrichment_mode` (string(primary_only,enrich_missing); default `primary_only`): 缺失标准事实的跨来源补充模式
- `fallbacks` (object): 各媒体类型的兜底分类 ID
- `field_aliases` (object): 字段值别名到规范值的映射
- `mode` (string=first_match; default `first_match`): 主分类规则求值模式
- `revision` (integer; default `0`; minimum `0.0`): 已发布策略修订号
- `rules` (array<ClassificationRule-Input>): 全局有序规则列表
- `schema_version` (integer=2; default `2`): 分类策略结构版本
- `updated_at` (string|null): 策略最后发布时间

### `FileItem-Input`
已配置存储提供方返回的一个文件或目录。
- `basename` (string|null): 不含扩展名的文件基本名称
- `children` (array<FileItem-Input>|null): 子节点
- `drive_id` (string|null): 云盘驱动器标识
- `extension` (string|null): 文件后缀
- `fileid` (string|null): 存储服务分配的文件标识
- `modify_time` (number|null): 文件修改时间的 Unix 时间戳
- `name` (string|null): 文件名
- `parent_fileid` (string|null): 父ID
- `path` (string|null; default `/`): 文件路径
- `pickcode` (string|null): 115 文件下载提取码
- `size` (integer|null): 文件大小
- `storage` (string|null; default `local`): 存储类型，默认 local 表示本地存储
- `thumbnail` (string|null): 缩略图
- `type` (string|null): 条目类型，dir 表示目录，file 表示文件
- `url` (string|null): 访问地址

### `JsonData-Input`
兼容 JSON 的任意辅助数据。
This runtime model has no directly writable fields.

### `MediaSource`
与来源原生媒体标识配对的规范元数据来源标识。
This runtime model has no directly writable fields.

### `MediaType`
MoviePilot 媒体类型。
This runtime model has no directly writable fields.

### `SubscriptionExecutionStatus`
订阅刷新执行状态和进度摘要。
- `batch_id` (string|null): 所属批次标识
- `can_cancel` (boolean; default `False`): 当前任务是否允许取消
- `current_site_id` (integer|null): 正在处理的站点标识
- `error` (string|null): 错误摘要
- `next_run_at` (string|null): 下次计划执行时间
- `phase*` (string): 当前执行阶段
- `source` (string|null): 触发本轮订阅执行的入口或来源
- `state*` (string): 当前业务状态
- `task_id` (string|null): 任务标识
- `updated_at*` (string): 更新时间

### `TorrentInfo`
搜索返回的一个候选种子。
- `category` (string|null): 种子分类 电影/电视剧/音乐
- `date_elapsed` (string|null): 已过时间
- `description` (string|null): 种子副标题
- `downloadvolumefactor` (number|null): 下载因子
- `enclosure` (string|null): 种子链接
- `freedate` (string|null): 免费截止时间
- `freedate_diff` (string|null): 剩余免费时间
- `grabs` (integer|null; default `0`): 完成者
- `hit_and_run` (boolean|null; default `False`): 种子是否存在下载后做种义务
- `labels` (array<string>|null): 种子标签
- `media_id` (string|null): 数据源原生 ID
- `media_source` (MediaSource|null): 种子页面声明的媒体身份
- `page_url` (string|null): 详情页面
- `peers` (integer|null; default `0`): 下载者
- `pri_order` (integer|null; default `0`): 种子优先级
- `pubdate` (string|null): 发布时间
- `seeders` (integer|null; default `0`): 做种者
- `site` (integer|null): 站点ID
- `site_cookie` (string|null): 站点Cookie
- `site_downloader` (string|null): 站点下载器
- `site_name` (string|null): 站点名称
- `site_order` (integer|null; default `0`): 站点优先级
- `site_proxy` (boolean|null; default `False`): 站点是否使用代理
- `site_ua` (string|null): 站点UA
- `size` (number|null; default `0.0`): 种子大小
- `title` (string|null): 种子名称
- `uploadvolumefactor` (number|null): 上传因子
- `volume_factor` (string|null): 促销

### `WorkflowExecutionConfig`
工作流并发、汇合、分支和失败策略。
- `max_workers` (integer|null): 最大并发动作数

### `WorkflowExecutionState-Input`
可恢复的持久化工作流执行状态。
- `errors` (object): 按动作标识保存的执行错误
- `nodes` (object): 按动作标识组织的持久化执行快照
- `outputs` (object): 按动作标识保存的输出数据
- `runtime` (WorkflowRuntimeState): 用于安全恢复的持久化工作流运行时元数据。
- `version` (integer; default `1`): 执行快照的数据结构版本

### `WorkflowRuntimeState`
完整的持久化工作流运行和进度状态。
- `attempts` (object): 按动作标识记录的执行尝试次数
- `errors` (object): 按动作标识组织的执行错误
- `finished_actions` (integer; default `0`): 已完成的工作流动作数量
- `node_states` (object): 按动作标识组织的运行状态
- `progress` (integer; default `0`): 当前完成百分比
- `running_tasks` (integer; default `0`): 当前运行中的工作流任务数量
