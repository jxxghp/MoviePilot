# Search APIs

Title and torrent search result discovery and recommendation search.

## Operations

### `search.recommend`
`POST /api/v1/search/recommend`; policy effect: `external_side_effect`.
Purpose: 使用已配置推荐模型对种子搜索结果排序或推荐。
Identity scope: `bound_user`; required role: `user`.
- `path_params`: none
- `query`: none
- `body`: `check_only` (boolean; default `False`): 仅检查状态，不启动新任务; `filtered_indices` (array<integer>|null): 筛选后的索引列表; `force` (boolean; default `False`): 强制重新推荐，清除旧结果

### `search.results`
`GET /api/v1/search/last/context`; policy effect: `safe_read`.
Purpose: 读取最近一次种子搜索的上下文和结果集。
Identity scope: `bound_user`; required role: `user`.
- `path_params`: none
- `query`: none
- `body`: none
- `params` and `results` are saved together for the authenticated user. Legacy global caches are not restored; search again after upgrading.
- Optional Web `manual_paging=true` diagnostics use `scope=page` for the current page. `searched_sites` records attempted requests and outcomes; `stage_counts` and `rejection_reasons` record observed filtering. Skipped rule groups do not increase the resource rejection count.
- Ordinary searches and legacy streams save `diagnostics=null`, including empty results. Errors and cancellation preserve the previous complete snapshot.

### `search.title`
`GET /api/v1/search/title`; policy effect: `external_side_effect`.
Purpose: 直接按自由文本标题和可选媒体条件搜索种子站点。
Identity scope: `bound_user`; required role: `user`.
- `response`: `data` remains a list and `collection.result_count` reports the returned items. `collection.total_count` is omitted because this endpoint or its upstream source does not expose a total.
- `path_params`: none
- `query`: `keyword` (string|null): 发现设置或筛选存储项目时使用的不区分大小写子串。; `mtype` (string(movie,tv,music)|null): 操作要求的媒体类型或订阅历史分类。 Agent-facing values: movie,tv,music. Use this exact field name in its documented bucket.; `page` (integer|null; default `0`): 从 1 开始的结果页码。; `sites` (string|null): 搜索或订阅范围包含的精确站点标识。
- `body`: none

### `search.torrents`
`GET /api/v1/search/media/{media_id}`; policy effect: `safe_read`.
Purpose: 按规范媒体身份搜索种子站点。
Identity scope: `bound_user`; required role: `user`.
- `response`: `data` remains a list; omitting both `page` and `count` keeps the complete legacy result. `collection.result_count` reports the returned items and `collection.total_count` reports the exact pre-pagination total. For counts or summaries, send `page=1,count=1`, read `collection.total_count`, and do not fall back to a database query because the item preview was truncated.
- `execution`: Searches may run multiple title/alias rounds across configured sites. The inner Agent API request may wait up to 290 seconds and the Agent tool call is capped at 300 seconds, leaving 10 seconds for timeout handling and returning the result; the V3 MCP wrapper uses its existing configured timeout (600 seconds by default). `page` / `count` paginate the completed result and do not shorten site-search execution; do not issue a duplicate call while one is pending.
- `path_params`: `media_id*` (string): 来源原生媒体标识，始终与搜索返回的精确 media_source 配对。
- `query`: `area` (string|null; default `title`): 种子搜索流程使用的可选地区条件。; `count` (integer|null): 旧版完整列表接口的可选每页条数；提供 page 或 count 即启用分页，此时省略 count 使用 50 条。; `include_candidates` (boolean; default `False`): 是否包含待人工复核的未确认音乐资源和相关专辑，默认 false；候选没有目标媒体身份，不能自动下载。; `media_source*` (MediaSource): 元数据来源标识，保留与 media_id 一同返回的精确值。; `mtype` (string(movie,tv,music)|null): 操作要求的媒体类型或订阅历史分类。 Agent-facing values: movie,tv,music. Use this exact field name in its documented bucket.; `music_type` (string|null): 音乐身份层级：按操作支持范围选择 recording、album 或 artist。; `page` (integer|null): 旧版完整列表接口的可选页码，从 1 开始；同时省略 page 和 count 时保留原有完整结果。; `season` (string|null): 媒体、搜索、订阅或整理操作使用的季号。; `sites` (string|null): 搜索或订阅范围包含的精确站点标识。
- `body`: none
- **Agent note:** `media_id` is a **path** parameter and MUST be nested under `path_params`. Never flatten it to the top level or place it inside `query`, otherwise the gateway rejects with `invalid_input`. The same rule applies to every `*`-marked path parameter across this document set (`media_type`, `site_id`, …).

#### Candidate Selection and Empty-Result Retries

- When `media.search` returns multiple candidates, select based on **title matching the user's request AND `type` (movie/series) matching**; do not mechanically pick the first or an arbitrary candidate without comparison. If the first candidate happens to exactly match and its type is correct, you may use it directly; when several are close, prefer the higher-ranked / more popular one.
- `search.torrents` must be called with `media_source` + `media_id`, and both must come from the **same** recognition result — do not mix `source`/`id` from different candidates.
- Do not repeat an unchanged empty or failed search without new evidence. On failure, inspect and report the error; an unavailable source does not prove there are no resources.
- After a successful empty search, try another credible title/type/year match or `search.title` with the original title keywords, preserving the user's site and media constraints. Report the actual search scope and outcome without claiming exhaustive coverage.

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
