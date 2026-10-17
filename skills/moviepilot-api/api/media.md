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
   complete media details or normalized facts to inspect one classification result.
   Fetch media details before previewing; search summaries may omit classification fields.
   Media inputs use the selected policy for asynchronous metadata enrichment.
   Explicit facts are evaluated as supplied. Recent-history impact analysis prepares
   facts separately under the active and candidate policies before comparing them.
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

### `media.cache.clear`
`DELETE /api/v1/tmdb/cache`; policy effect: `destructive_write`.
Purpose: 清空仅管理员可访问的 TMDB 识别缓存。
Identity scope: `admin`; required role: `system_admin`.
- `path_params`: none
- `query`: none
- `body`: none

### `media.cache.delete`
`DELETE /api/v1/tmdb/cache/{cache_key}`; policy effect: `destructive_write`.
Purpose: 按精确缓存键删除一条管理员可访问的 TMDB 识别缓存。
Identity scope: `admin`; required role: `system_admin`.
- `path_params`: `cache_key*` (string): 对应识别缓存查询操作返回的精确缓存键。
- `query`: none
- `body`: none

### `media.cache.get`
`GET /api/v1/tmdb/cache`; policy effect: `safe_read`.
Purpose: 查看仅管理员可访问的 TMDB 识别缓存及统计数量。
Identity scope: `admin`; required role: `system_admin`.
- `path_params`: none
- `query`: none
- `body`: none

### `media.classification.fields`
`GET /api/v1/media/classification/fields`; policy effect: `safe_read`.
Purpose: 读取媒体分类字段目录及来源能力。
Identity scope: `bound_user`; required role: `user`.
- `path_params`: none
- `query`: none
- `body`: none

### `media.classification.policy.get`
`GET /api/v1/media/classification/policy`; policy effect: `safe_read`.
Purpose: 读取当前生效的自动媒体分类策略。
Identity scope: `bound_user`; required role: `user`.
- `path_params`: none
- `query`: `template` (string|null): 读取内置分类策略模板时传入 default；省略则读取活动策略
- `body`: none

### `media.classification.policy.history`
`GET /api/v1/media/classification/history`; policy effect: `safe_read`.
Purpose: 读取已发布媒体分类策略的有界历史。
Identity scope: `admin`; required role: `system_admin`.
- `path_params`: none
- `query`: none
- `body`: none

### `media.classification.policy.impact`
`POST /api/v1/media/classification/impact`; policy effect: `safe_read`.
Purpose: 使用有界近期样本估算尚未发布的分类策略产生的变化。
Identity scope: `admin`; required role: `system_admin`.
- `path_params`: none
- `query`: none
- `body`: `example_limit` (integer; default `20`; minimum `0.0`; maximum `50.0`): 响应中最多返回的变化样本明细数量; `expected_revision*` (integer; minimum `1.0`): 本次影响分析使用的活动策略基线 revision; `policy*` (ClassificationPolicy-Input): 完整的自动媒体分类策略或策略草稿。; `sample_limit` (integer; default `100`; minimum `1.0`; maximum `200.0`): 近期历史或显式事实的最大比较数量; `samples` (array<ClassificationFacts>): 可选显式事实；为空时使用近期下载和整理历史样本

### `media.classification.policy.preview`
`POST /api/v1/media/classification/preview`; policy effect: `safe_read`.
Purpose: 为选定媒体或规范化事实预览自动分类结果。
Identity scope: `bound_user`; required role: `user`.
- `path_params`: none
- `query`: none
- `body`: `input*` (ClassificationFactsPreviewInput|ClassificationMediaPreviewInput): 可判别的预览输入; `policy` (ClassificationPolicy-Input|null): 可选未发布策略；省略时使用当前活动策略

### `media.classification.policy.rollback`
`POST /api/v1/media/classification/rollback/{revision}`; policy effect: `reversible_write`.
Purpose: 把选定历史媒体分类策略作为新修订发布。
Identity scope: `admin`; required role: `system_admin`.
- `path_params`: `revision*` (integer): 已发布分类策略修订号或预期修订号。
- `query`: none
- `body`: `expected_revision*` (integer; minimum `1.0`): 客户端读取策略时的活动 revision

### `media.classification.policy.update`
`PUT /api/v1/media/classification/policy`; policy effect: `reversible_write`.
Purpose: 校验并发布完整的自动媒体分类策略。
Identity scope: `admin`; required role: `system_admin`.
- `path_params`: none
- `query`: none
- `body`: `expected_revision*` (integer; minimum `0.0`): 客户端读取策略时的活动 revision; `policy*` (ClassificationPolicy-Input): 完整的自动媒体分类策略或策略草稿。

### `media.classification.policy.validate`
`POST /api/v1/media/classification/validate`; policy effect: `safe_read`.
Purpose: 校验尚未发布的自动媒体分类策略，不保存。
Identity scope: `admin`; required role: `system_admin`.
- `path_params`: none
- `query`: none
- `body`: `policy*` (ClassificationPolicy-Input): 完整的自动媒体分类策略或策略草稿。

### `media.detail`
`GET /api/v1/media/{media_id}`; policy effect: `safe_read`.
Purpose: 从选定元数据来源读取规范媒体详情。
Identity scope: `public`; required role: `user`.
- `path_params`: `media_id*` (string): 来源原生媒体标识，始终与搜索返回的精确 media_source 配对。
- `query`: `media_source*` (MediaSource): 元数据来源标识，保留与 media_id 一同返回的精确值。; `type_name*` (string): 仅凭来源标识无法区分时使用的显式媒体类型名。
- `body`: none

### `media.episode_group.seasons`
`GET /api/v1/media/group/seasons/{episode_group}`; policy effect: `safe_read`.
Purpose: 列出精确 TMDB 剧集组标识所定义的季。
Identity scope: `public`; required role: `user`.
- `response`: `data` remains a list; omitting both `page` and `count` keeps the complete legacy result. `collection.result_count` reports the returned items and `collection.total_count` reports the exact pre-pagination total. For counts or summaries, send `page=1,count=1`, read `collection.total_count`, and do not fall back to a database query because the item preview was truncated.
- `path_params`: `episode_group*` (string): 用于其他剧集排序方式的 TMDB 剧集组标识。
- `query`: `count` (integer|null): 旧版完整列表接口的可选每页条数；提供 page 或 count 即启用分页，此时省略 count 使用 50 条。; `page` (integer|null): 旧版完整列表接口的可选页码，从 1 开始；同时省略 page 和 count 时保留原有完整结果。
- `body`: none

### `media.episode_groups`
`GET /api/v1/media/groups/{tmdbid}`; policy effect: `safe_read`.
Purpose: 列出指定电视剧在 TMDB 中可用的其他剧集组。
Identity scope: `public`; required role: `user`.
- `response`: `data` remains a list; omitting both `page` and `count` keeps the complete legacy result. `collection.result_count` reports the returned items and `collection.total_count` reports the exact pre-pagination total. For counts or summaries, send `page=1,count=1`, read `collection.total_count`, and do not fall back to a database query because the item preview was truncated.
- `path_params`: `tmdbid*` (integer): 媒体搜索或详情返回的 TMDB 媒体标识。
- `query`: `count` (integer|null): 旧版完整列表接口的可选每页条数；提供 page 或 count 即启用分页，此时省略 count 使用 50 条。; `page` (integer|null): 旧版完整列表接口的可选页码，从 1 开始；同时省略 page 和 count 时保留原有完整结果。
- `body`: none

### `media.episode_schedule`
`GET /api/v1/tmdb/{tmdbid}/{season}`; policy effect: `safe_read`.
Purpose: 读取 TMDB 中指定季的剧集上映信息。
Identity scope: `public`; required role: `user`.
- `response`: `data` remains a list; omitting both `page` and `count` keeps the complete legacy result. `collection.result_count` reports the returned items and `collection.total_count` reports the exact pre-pagination total. For counts or summaries, send `page=1,count=1`, read `collection.total_count`, and do not fall back to a database query because the item preview was truncated.
- `path_params`: `season*` (integer): 媒体、搜索、订阅或整理操作使用的季号。; `tmdbid*` (integer): 媒体搜索或详情返回的 TMDB 媒体标识。
- `query`: `count` (integer|null): 旧版完整列表接口的可选每页条数；提供 page 或 count 即启用分页，此时省略 count 使用 50 条。; `episode_group` (string|null): 用于其他剧集排序方式的 TMDB 剧集组标识。; `page` (integer|null): 旧版完整列表接口的可选页码，从 1 开始；同时省略 page 和 count 时保留原有完整结果。
- `body`: none

### `media.person.credits`
`GET /api/v1/{source}/person/credits/{person_id}`; policy effect: `safe_read`.
Purpose: 读取选定元数据来源中指定人物的作品。
Identity scope: `public`; required role: `user`.
- `response`: `data` remains a list and `collection.result_count` reports the returned items. `collection.total_count` is omitted because this endpoint or its upstream source does not expose a total.
- `path_params`: `person_id*` (integer): 元数据来源分配的人物标识。; `source*` (string(douban,tmdb,bangumi,anilist)): 人物标识所属的元数据来源。
- `query`: `count` (integer; default `20`; minimum `1`; maximum `50`): Bangumi 和 AniList 使用的每页条数；其他来源忽略此值。; `page` (integer; default `1`; minimum `1`): 从 1 开始的结果页码。
- `body`: none

### `media.person.search`
`GET /api/v1/media/search`; policy effect: `safe_read`.
Purpose: 在选定元数据来源中搜索人物和音乐艺术家。
Identity scope: `public`; required role: `user`.
- `response`: `data` remains a list and `collection.result_count` reports the returned items. `collection.total_count` is omitted because this endpoint or its upstream source does not expose a total.
- `path_params`: none
- `query`: `count` (integer; default `8`): 当前页最多返回的记录数。; `media_source` (array<MediaSource>; default `[]`): 元数据来源标识，保留与 media_id 一同返回的精确值。; `music_type` (string(recording,album,artist)|null): 音乐身份层级：按操作支持范围选择 recording、album 或 artist。; `page` (integer; default `1`): 从 1 开始的结果页码。; `title*` (string): 操作使用的媒体、种子、订阅或历史标题。; `type*` (string=person): 固定为 person，表示搜索人物。
- `body`: none

### `media.recognize`
`GET /api/v1/media/recognize`; policy effect: `safe_read`.
Purpose: 根据标题、副标题或自定义规则上下文识别媒体身份。
Identity scope: `bound_user`; required role: `user`.
- `path_params`: none
- `query`: `custom_words` (string|null): 本次媒体处理使用的自定义识别或重命名词。; `media_source` (MediaSource|null): 元数据来源标识，保留与 media_id 一同返回的精确值。; `subtitle` (string|null): 媒体识别时与标题配合使用的可选副标题。; `title*` (string): 操作使用的媒体、种子、订阅或历史标题。
- `body`: none
- **Agent note:** Recognition returns a context containing `media_info`, not a candidate list. A failed match still returns parsed `meta_info` with the applied recognition words, so treat recognition as successful only when `media_info` is present. Check its title and media type against the user's request. If recognition is missing or ambiguous, use `media.search` and compare candidates by title, type, and year. Preserve the selected result's exact `media_source` + `media_id` for `search.torrents`.

### `media.recognize_file`
`GET /api/v1/media/recognize_file`; policy effect: `safe_read`.
Purpose: 根据精确文件名和可选路径上下文识别规范媒体身份。
Identity scope: `bound_user`; required role: `user`.
- `path_params`: none
- `query`: `media_source` (MediaSource|null): 元数据来源标识，保留与 media_id 一同返回的精确值。; `path*` (string): 当前记录表示的存储路径或历史路径。
- `body`: none

### `media.scrape`
`POST /api/v1/media/scrape/{storage}`; policy effect: `external_side_effect`.
Purpose: 为一个存储项目生成或刷新元数据。
Identity scope: `admin`; required role: `system_admin`.
- `path_params`: `storage*` (string|null): 操作使用的已配置存储名称或存储类型。
- `query`: `episode_group` (string|null): TMDB 剧集组编号，用于按指定季集顺序识别电视剧; `media_id` (string|null): 来源原生媒体标识，始终与搜索返回的精确 media_source 配对。; `media_source` (MediaSource|null): 元数据来源标识，保留与 media_id 一同返回的精确值。; `music_type` (string|null): 音乐身份层级：按操作支持范围选择 recording、album 或 artist。; `type_name` (MediaType|null): 仅凭来源标识无法区分时使用的显式媒体类型名。
- `body`: `basename` (string|null): 不含扩展名的文件基本名称; `children` (array<FileItem-Input>|null): 子节点; `drive_id` (string|null): 云盘驱动器标识; `extension` (string|null): 文件后缀; `fileid` (string|null): 存储服务分配的文件标识; `modify_time` (number|null): 文件修改时间的 Unix 时间戳; `name` (string|null): 文件名; `parent_fileid` (string|null): 父ID; `path` (string|null; default `/`): 文件路径; `pickcode` (string|null): 115 文件下载提取码; `size` (integer|null): 文件大小; `storage` (string|null; default `local`): 存储类型，默认 local 表示本地存储; `thumbnail` (string|null): 缩略图; `type` (string|null): 条目类型，dir 表示目录，file 表示文件; `url` (string|null): 访问地址

### `media.search`
`GET /api/v1/media/search`; policy effect: `safe_read`.
Purpose: 在选定元数据来源中搜索规范媒体。
Identity scope: `public`; required role: `user`.
- `response`: `data` remains a list and `collection.result_count` reports the returned items. `collection.total_count` is omitted because this endpoint or its upstream source does not expose a total.
- `path_params`: none
- `query`: `count` (integer; default `8`): 当前页最多返回的记录数。; `media_source` (array<MediaSource>; default `[]`): 元数据来源标识，保留与 media_id 一同返回的精确值。; `music_type` (string(recording,album,artist)|null): 音乐身份层级：按操作支持范围选择 recording、album 或 artist。; `page` (integer; default `1`): 从 1 开始的结果页码。; `title*` (string): 操作使用的媒体、种子、订阅或历史标题。; `type` (string|null; default `media`): 所选操作要求的媒体或存储项目类型。
- `body`: none

### `media.seasons`
`GET /api/v1/media/seasons`; policy effect: `safe_read`.
Purpose: 按精确媒体身份读取季列表，或回退到标题与年份查询。
Identity scope: `public`; required role: `user`.
- `response`: `data` remains a list; omitting both `page` and `count` keeps the complete legacy result. `collection.result_count` reports the returned items and `collection.total_count` reports the exact pre-pagination total. For counts or summaries, send `page=1,count=1`, read `collection.total_count`, and do not fall back to a database query because the item preview was truncated.
- `path_params`: none
- `query`: `count` (integer|null): 旧版完整列表接口的可选每页条数；提供 page 或 count 即启用分页，此时省略 count 使用 50 条。; `media_id` (string|null): 来源原生媒体标识，始终与搜索返回的精确 media_source 配对。; `media_source` (MediaSource|null): 元数据来源标识，保留与 media_id 一同返回的精确值。; `page` (integer|null): 旧版完整列表接口的可选页码，从 1 开始；同时省略 page 和 count 时保留原有完整结果。; `season` (integer): 媒体、搜索、订阅或整理操作使用的季号。; `title` (string|null): 操作使用的媒体、种子、订阅或历史标题。; `year` (string): 用于区分同名媒体的发行或首映年份。
- `body`: none

### `media.sources`
`GET /api/v1/media/source`; policy effect: `safe_read`.
Purpose: 列出当前已登记的媒体元数据来源。
Identity scope: `bound_user`; required role: `user`.
- `response`: `data` remains a list; omitting both `page` and `count` keeps the complete legacy result. `collection.result_count` reports the returned items and `collection.total_count` reports the exact pre-pagination total. For counts or summaries, send `page=1,count=1`, read `collection.total_count`, and do not fall back to a database query because the item preview was truncated.
- `path_params`: none
- `query`: `count` (integer|null): 旧版完整列表接口的可选每页条数；提供 page 或 count 即启用分页，此时省略 count 使用 50 条。; `page` (integer|null): 旧版完整列表接口的可选页码，从 1 开始；同时省略 page 和 count 时保留原有完整结果。
- `body`: none

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
