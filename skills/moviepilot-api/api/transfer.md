# Transfer APIs

Transfer queue, history, file, naming, manual review, retry, and target-path operations.

## Music preview evidence

Manual previews optionally return `source_storage`, `source_item` and `music` on each item.
`source_item` preserves opaque storage IDs but omits download URLs, thumbnails and child trees.
Music recognition is independent of file `success`: `local_tags` and `local_cue` use local
evidence; `matched` means online confirmation; `manual` means an explicit selection;
`metadata` has existing information without confirmed online provenance. Other states are
`not_found`, `ambiguous`, `conflict`, `service_error`, `budget_exhausted`, and `unsupported`.
Only `matched` sets `online_confirmed=true`; a tag containing an MBID is insufficient.
`field_sources` preserves field provenance and `candidates` contains at most five identity
summaries, with Recording, Release Group and Release IDs kept separate and no confidence probability.
`read_status` is `tags`, `stream_only`, `unreadable`, `name_only`, `companion`, or `unknown`.
Use `group_id` to aggregate the actual scanned release members, including CUE/lyrics
companions (`file_role=companion`). `group_size` counts audio members. The identifier is
neither authorization nor a persistent media identity. Correct only the group's returned
source items and preview again; never expand `group_directory` or apply a selection to
other albums with the same display name. Unsupported packages appear as failed items.

- To select one MusicBrainz edition, pair the album Release Group `media_id` with
  `music_type=album` and `musicbrainz_release_id` (Release UUID). The backend checks
  membership and refuses unavailable or conflicting editions instead of choosing
  a default. Preview and execute with identical IDs and source items; this path
  requires local audio for track alignment. Do not place a Release ID in `media_id`.

## Durable AI recognition correction

With both `AI_AGENT_ENABLE` and `AI_AGENT_RETRY_TRANSFER` enabled, a newly committed
recognition/planning rejection may immediately start AI analysis; heartbeat jobs
are not required. Existing failed histories can use the AI button or single/batch
AI redo. Sources, identity and TV season/episode must be verified and previewed.

Use `transfer.history.replan` only for a failed movie/TV planning rejection with
no file effects. Submit the original task ID and settlement revision from the
analysis context; a changed revision requires fresh analysis. It retains the same task, source admission, original evidence,
settlement receipts, and consumed retry budget. Stop on rejection, manual review,
active execution, uncertain file effects, storage/configuration faults or exhausted
budget. Never delete durable task/history/target evidence or start a new direct
transfer to bypass these checks. A queued correction is not a completed transfer.

## Operations

### `transfer.episode_format.recommend`
`POST /api/v1/transfer/episode-format/recommend`; policy effect: `safe_read`.
Purpose: 根据文件样本推荐剧集编号提取模板。
Identity scope: `admin`; required role: `system_admin`.
- `path_params`: none
- `query`: none
- `body`: `fileitem` (FileItem-Input|null): 用于推荐集数格式的单个文件或目录; `fileitems` (array<FileItem-Input>|null): 文件项列表（前端多选时传入）

### `transfer.file`
`POST /api/v1/transfer/manual`; policy effect: `external_side_effect`.
Purpose: 执行手动文件转移与整理流程。
Identity scope: `admin`; required role: `system_admin`.
- `path_params`: none
- `query`: `background` (boolean|null; default `False`): 是否异步执行整理，并在完成前返回。
- `body`: `episode_detail` (string|null): 指定集数; `episode_format` (string|null): 自定义格式; `episode_group` (string|null): 剧集组; `episode_offset` (string|null): 集数偏移; `episode_part` (string|null): 指定PART; `fileitem` (FileItem-Input): storage.list 返回的一个完整源存储项目。; `fileitems` (array<FileItem-Input>|null): 文件项列表（前端多选时传入）; `from_history` (boolean|null; default `False`): 复用历史识别信息; `library_category_folder` (boolean|null): 媒体库类别子目录; `library_type_folder` (boolean|null): 媒体库类型子目录; `logid` (integer|null): 日志ID; `logids` (array<integer>|null): 日志ID列表（前端多选历史记录时传入）; `media_id` (string|null): 数据源原生ID; `media_source` (MediaSource|null): 媒体数据源; `min_filesize` (integer|null; default `0`): 最小文件大小; `music_cue_enable` (boolean|null): 本次手动整理是否识别 CUE；空值继承系统设置; `music_release_regions` (array<string>|null): 本次手动整理的 MusicBrainz 发行地区优先级；空值继承系统设置; `music_release_scripts` (array<string>|null): 本次手动整理的 MusicBrainz 文字字形优先级；空值继承系统设置; `music_type` (string(recording,album)|null): 音乐实体类型; `musicbrainz_release_id` (string|null): 显式选择具体发行版；media_id 仍为 Release Group，不能互换; `preview` (boolean|null; default `False`): 仅预览，不执行整理; `reorganize` (boolean|null; default `False`): 重新整理，清理命中的成功历史及其旧目标; `scrape` (boolean|null; default `False`): 刮削; `season` (integer|null): 季号; `skip_success` (boolean; default `False`): 跳过成功历史，优先于重新整理；预览与执行使用相同过滤范围; `target_path` (string|null): 目标路径; `target_storage` (string|null): 目标存储; `transfer_type` (string|null): 整理方式; `type_name` (string|null): 类型

### `transfer.history`
`GET /api/v1/history/transfer`; policy effect: `safe_read`.
Purpose: 按条件和分页读取文件整理历史。
Identity scope: `bound_user`; required role: `user`.
- `response`: structured page object; items stay in `data.list` and the exact total stays in `data.total`.
- `path_params`: none
- `query`: `count` (integer|null; default `30`): 当前页最多返回的记录数。; `page` (integer|null; default `1`): 从 1 开始的结果页码。; `status` (boolean|null): 用于筛选历史或描述记录的整理成功状态。; `title` (string|null): 操作使用的媒体、种子、订阅或历史标题。
- `body`: none

### `transfer.history.clear`
`DELETE /api/v1/history/transfer/all`; policy effect: `destructive_write`.
Purpose: 删除旧整理历史，保留文件和持久化失败任务记录。
Identity scope: `admin`; required role: `system_admin`.
- `path_params`: none
- `query`: none
- `body`: none

### `transfer.history.delete`
`DELETE /api/v1/history/transfer`; policy effect: `destructive_write`.
Purpose: 删除一条整理历史，并可选择同时删除文件。
Identity scope: `admin`; required role: `system_admin`.
- `path_params`: none
- `query`: `deletedest` (boolean|null; default `False`): 删除整理历史时是否同时删除整理后的目标文件。; `deletesrc` (boolean|null; default `False`): 删除整理历史时是否同时删除记录中的源文件。
- `body`: `audio_format` (string|null): 实际音频格式; `audio_lossless` (boolean|null): 是否无损音频; `auto_paused` (boolean; default `False`): 是否因反复失败而自动暂停; `bit_depth` (integer|null): 实际位深（bit）; `bitrate` (integer|null): 实际码率（bps）; `category` (string|null): 实际媒体类别兼容路径快照; `classification_policy_revision` (integer|null): 执行时分类策略版本; `classification_rule_id` (string|null): 命中的分类规则标识; `classification_source` (string|null): 最终分类来源; `cleanup_error` (string|null): 下载器清理失败的原因; `cleanup_status` (string|null): 下载器清理独立于媒体入库结果; `date` (string|null): 日期; `dest` (string|null): 目的目录; `dest_fileitem` (JsonData-Input|null): 目标文件项; `dest_storage` (string|null): 目标存储类型; `download_hash` (string|null): 下载器Hash; `episode_group` (string|null): 自定义剧集组; `episodes` (string|null): 集Exx; `errmsg` (string|null): 失败原因; `failure_stage` (string|null): 从失败原因和失败计数派生的用户可操作状态; `files` (JsonData-Input|null): 文件清单; `id*` (integer): 记录标识; `image` (string|null): 海报; `media_category_id` (string|null): 实际媒体类别稳定标识; `media_id` (string|null): 数据源原生ID; `media_source` (MediaSource|null): 媒体数据源; `mode` (string|null): 转移模式; `music_type` (string|null): 音乐实体类型：recording 单曲、album 专辑; `recovery_action` (string|null): 当前失败状态下可执行的恢复操作; `retry_count` (integer|null): 已执行的重试次数; `retry_exhausted` (boolean; default `False`): 是否已耗尽重试次数; `sample_rate` (integer|null): 实际采样率（Hz）; `seasons` (string|null): 季Sxx; `src` (string|null): 源目录; `src_fileitem` (JsonData-Input|null): 源文件项（含文件大小等运行时字段，与 to_dict 直出保持一致）; `src_storage` (string|null): 源存储类型; `status` (boolean; default `True`): 状态 1-成功，0-失败; `title` (string|null): 标题; `total_tracks` (integer|null): 专辑预期总曲目数; `transfer_settlement_revision` (integer|null): 宿主 AI 接管使用的结算版本，不属于公开历史响应; `transfer_task_id` (string|null): durable 整理任务标识仅供宿主入口选择重试协议，不属于公开历史响应; `type` (string|null): 类型：电影、电视剧、音乐; `year` (string|null): 年份

### `transfer.history.redo`
`POST /api/v1/history/transfer/{history_id}/ai-redo`; policy effect: `external_side_effect`.
Purpose: 对一条整理历史启动 AI 辅助重新整理。
Identity scope: `admin`; required role: `system_admin`.
- `path_params`: `history_id*` (integer): 历史查询操作返回的持久化整理或订阅历史标识。
- `query`: none
- `body`: none

### `transfer.history.redo_batch`
`POST /api/v1/history/transfer/ai-redo`; policy effect: `external_side_effect`.
Purpose: 对明确列出的整理历史记录启动 AI 辅助批量重新整理。
Identity scope: `admin`; required role: `system_admin`.
- `path_params`: none
- `query`: none
- `body`: `history_ids` (array<integer>): 历史记录 ID 列表

### `transfer.history.replan`
`POST /api/v1/history/transfer/{history_id}/replan`; policy effect: `external_side_effect`.
Purpose: 为没有文件副作用的识别失败历史提交已核实的电影或电视剧身份和季集修正，保留原任务及失败证据。
Identity scope: `admin`; required role: `system_admin`.
- `path_params`: `history_id*` (integer): 历史查询操作返回的持久化整理或订阅历史标识。
- `query`: none
- `body`: `episode` (integer|null): 修正后的单集集号；为空时保留原集数范围; `media_id*` (string; minimum length `1`): 经媒体查询核实的来源原生 ID; `media_source*` (MediaSource): 元数据来源标识，保留与 media_id 一同返回的精确值。; `media_type*` (string(movie,tv)): 经核实的电影或电视剧类型; `season` (integer|null): 修正后的季号，0 为特别篇；为空时保留原值; `settlement_revision*` (integer; minimum `1.0`): 接管上下文中的原始结算版本，拒绝分析期间已变化的任务; `task_id*` (string; minimum length `1`): 接管上下文中的原始持久任务标识，必须与历史绑定一致

### `transfer.manual_history`
`POST /api/v1/transfer/manual/history`; policy effect: `safe_read`.
Purpose: 检查给定存储项目是否已有成功整理历史。
Identity scope: `admin`; required role: `system_admin`.
- `path_params`: none
- `query`: none
- `body`: `episode_detail` (string|null): 指定集数; `episode_format` (string|null): 自定义格式; `episode_group` (string|null): 剧集组; `episode_offset` (string|null): 集数偏移; `episode_part` (string|null): 指定PART; `fileitem` (FileItem-Input): storage.list 返回的一个完整源存储项目。; `fileitems` (array<FileItem-Input>|null): 文件项列表（前端多选时传入）; `from_history` (boolean|null; default `False`): 复用历史识别信息; `library_category_folder` (boolean|null): 媒体库类别子目录; `library_type_folder` (boolean|null): 媒体库类型子目录; `logid` (integer|null): 日志ID; `logids` (array<integer>|null): 日志ID列表（前端多选历史记录时传入）; `media_id` (string|null): 数据源原生ID; `media_source` (MediaSource|null): 媒体数据源; `min_filesize` (integer|null; default `0`): 最小文件大小; `music_cue_enable` (boolean|null): 本次手动整理是否识别 CUE；空值继承系统设置; `music_release_regions` (array<string>|null): 本次手动整理的 MusicBrainz 发行地区优先级；空值继承系统设置; `music_release_scripts` (array<string>|null): 本次手动整理的 MusicBrainz 文字字形优先级；空值继承系统设置; `music_type` (string(recording,album)|null): 音乐实体类型; `musicbrainz_release_id` (string|null): 显式选择具体发行版；media_id 仍为 Release Group，不能互换; `preview` (boolean|null; default `False`): 仅预览，不执行整理; `reorganize` (boolean|null; default `False`): 重新整理，清理命中的成功历史及其旧目标; `scrape` (boolean|null; default `False`): 刮削; `season` (integer|null): 季号; `skip_success` (boolean; default `False`): 跳过成功历史，优先于重新整理；预览与执行使用相同过滤范围; `target_path` (string|null): 目标路径; `target_storage` (string|null): 目标存储; `transfer_type` (string|null): 整理方式; `type_name` (string|null): 类型

### `transfer.manual_review`
`GET /api/v1/transfer/tasks/{task_id}/manual-review`; policy effect: `safe_read`.
Purpose: 读取一个等待人工复核的持久化整理任务。
Identity scope: `admin`; required role: `system_admin`.
- `path_params`: `task_id*` (string): transfer.manual_reviews 返回的稳定持久化整理任务标识。
- `query`: none
- `body`: none

### `transfer.manual_review.resolve`
`POST /api/v1/transfer/tasks/{task_id}/manual-review`; policy effect: `reversible_write`.
Purpose: 记录一个持久化整理人工复核操作的已授权决定。
Identity scope: `admin`; required role: `system_admin`.
- `path_params`: `task_id*` (string): transfer.manual_reviews 返回的稳定持久化整理任务标识。
- `query`: none
- `body`: `decision*` (string(not_applied,applied)): 人工判定；不公开 failed，失败终态只能由 durable 结算写入; `operation_id*` (string; minimum length `1`): 待判定的稳定操作标识; `reason` (string; default ``): 人工判定理由，可选; `result_payload` (object|null): 判定为 applied 时必填的外部结果证据

### `transfer.manual_reviews`
`GET /api/v1/transfer/tasks/manual-reviews`; policy effect: `safe_read`.
Purpose: 分页读取等待人工复核或重试恢复的持久化整理任务。
Identity scope: `admin`; required role: `system_admin`.
- `response`: structured page object; items stay in `data.items` and the exact total stays in `data.total`.
- `path_params`: none
- `query`: `page` (integer; default `1`; minimum `1`): 从 1 开始的结果页码。; `page_size` (integer; default `30`; minimum `1`; maximum `100`): 每页最多返回的记录数。; `state` (string(manual_review,retry_wait); default `manual_review`): 站点、订阅、市场或整理的当前状态筛选条件。
- `body`: none

### `transfer.name`
`GET /api/v1/transfer/name`; policy effect: `safe_read`.
Purpose: 为源路径和媒体身份预览整理后的目标名称。
Identity scope: `admin`; required role: `system_admin`.
- `path_params`: none
- `query`: `filetype*` (string): 预览整理目标名称使用的媒体文件类型。; `path*` (string): 当前记录表示的存储路径或历史路径。
- `body`: none

### `transfer.queue`
`GET /api/v1/transfer/queue`; policy effect: `safe_read`.
Purpose: 列出文件整理队列中的待处理项目。
Identity scope: `bound_user`; required role: `user`.
- `response`: `data` remains a list; omitting both `page` and `count` keeps the complete legacy result. `collection.result_count` reports the returned items and `collection.total_count` reports the exact pre-pagination total. For counts or summaries, send `page=1,count=1`, read `collection.total_count`, and do not fall back to a database query because the item preview was truncated.
- `path_params`: none
- `query`: `count` (integer|null): 旧版完整列表接口的可选每页条数；提供 page 或 count 即启用分页，此时省略 count 使用 50 条。; `page` (integer|null): 旧版完整列表接口的可选页码，从 1 开始；同时省略 page 和 count 时保留原有完整结果。
- `body`: none

### `transfer.queue.delete`
`DELETE /api/v1/transfer/queue`; policy effect: `destructive_write`.
Purpose: 从整理队列移除一个精确存储项目，并停止其整理。
Identity scope: `bound_user`; required role: `user`.
- `path_params`: none
- `query`: none
- `body`: `basename` (string|null): 不含扩展名的文件基本名称; `children` (array<FileItem-Input>|null): 子节点; `drive_id` (string|null): 云盘驱动器标识; `extension` (string|null): 文件后缀; `fileid` (string|null): 存储服务分配的文件标识; `modify_time` (number|null): 文件修改时间的 Unix 时间戳; `name` (string|null): 文件名; `parent_fileid` (string|null): 父ID; `path` (string|null; default `/`): 文件路径; `pickcode` (string|null): 115 文件下载提取码; `size` (integer|null): 文件大小; `storage` (string|null; default `local`): 存储类型，默认 local 表示本地存储; `thumbnail` (string|null): 缩略图; `type` (string|null): 条目类型，dir 表示目录，file 表示文件; `url` (string|null): 访问地址

### `transfer.target_path`
`POST /api/v1/transfer/manual/target-path`; policy effect: `safe_read`.
Purpose: 根据给定源存储项目解析已配置的整理目标。
Identity scope: `admin`; required role: `system_admin`.
- `path_params`: none
- `query`: none
- `body`: `episode_detail` (string|null): 指定集数; `episode_format` (string|null): 自定义格式; `episode_group` (string|null): 剧集组; `episode_offset` (string|null): 集数偏移; `episode_part` (string|null): 指定PART; `fileitem` (FileItem-Input): storage.list 返回的一个完整源存储项目。; `fileitems` (array<FileItem-Input>|null): 文件项列表（前端多选时传入）; `from_history` (boolean|null; default `False`): 复用历史识别信息; `library_category_folder` (boolean|null): 媒体库类别子目录; `library_type_folder` (boolean|null): 媒体库类型子目录; `logid` (integer|null): 日志ID; `logids` (array<integer>|null): 日志ID列表（前端多选历史记录时传入）; `media_id` (string|null): 数据源原生ID; `media_source` (MediaSource|null): 媒体数据源; `min_filesize` (integer|null; default `0`): 最小文件大小; `music_cue_enable` (boolean|null): 本次手动整理是否识别 CUE；空值继承系统设置; `music_release_regions` (array<string>|null): 本次手动整理的 MusicBrainz 发行地区优先级；空值继承系统设置; `music_release_scripts` (array<string>|null): 本次手动整理的 MusicBrainz 文字字形优先级；空值继承系统设置; `music_type` (string(recording,album)|null): 音乐实体类型; `musicbrainz_release_id` (string|null): 显式选择具体发行版；media_id 仍为 Release Group，不能互换; `preview` (boolean|null; default `False`): 仅预览，不执行整理; `reorganize` (boolean|null; default `False`): 重新整理，清理命中的成功历史及其旧目标; `scrape` (boolean|null; default `False`): 刮削; `season` (integer|null): 季号; `skip_success` (boolean; default `False`): 跳过成功历史，优先于重新整理；预览与执行使用相同过滤范围; `target_path` (string|null): 目标路径; `target_storage` (string|null): 目标存储; `transfer_type` (string|null): 整理方式; `type_name` (string|null): 类型

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
