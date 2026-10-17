# System APIs

Version, update, restart, module, network, and usage operations.

## Operations

### `system.module.catalog`
`GET /api/v1/system/module-catalog`; policy effect: `safe_read`.
Purpose: 列出宿主模块与服务类型选项，供界面选择器使用。
Identity scope: `bound_user`; required role: `user`.
- `path_params`: none
- `query`: none
- `body`: none

### `system.module.list`
`GET /api/v1/system/modulelist`; policy effect: `safe_read`.
Purpose: 列出已启用模块标识及本地化名称。
Identity scope: `bound_user`; required role: `user`.
- `path_params`: none
- `query`: none
- `body`: none

### `system.module.settings`
`GET /api/v1/system/module-settings`; policy effect: `safe_read`.
Purpose: 列出管理员可启用或停用的内置模块。
Identity scope: `admin`; required role: `system_admin`.
- `path_params`: none
- `query`: none
- `body`: none

### `system.module.test`
`GET /api/v1/system/moduletest/{moduleid}`; policy effect: `external_side_effect`.
Purpose: 对指定已启用模块执行内置可用性测试。
Identity scope: `bound_user`; required role: `user`.
- `path_params`: `moduleid*` (string): system.module.list 返回的精确已加载模块标识。
- `query`: none
- `body`: none

### `system.network.targets`
`GET /api/v1/system/nettest/targets`; policy effect: `safe_read`.
Purpose: 列出获准网络测试目标及脱敏目标地址，不暴露请求路径、查询参数或凭据。
Identity scope: `bound_user`; required role: `user`.
- `response`: `data` remains a list; omitting both `page` and `count` keeps the complete legacy result. `collection.result_count` reports the returned items and `collection.total_count` reports the exact pre-pagination total. For counts or summaries, send `page=1,count=1`, read `collection.total_count`, and do not fall back to a database query because the item preview was truncated.
- `path_params`: none
- `query`: `count` (integer|null): 旧版完整列表接口的可选每页条数；提供 page 或 count 即启用分页，此时省略 count 使用 50 条。; `page` (integer|null): 旧版完整列表接口的可选页码，从 1 开始；同时省略 page 和 count 时保留原有完整结果。
- `body`: none

### `system.network.test`
`GET /api/v1/system/nettest`; policy effect: `external_side_effect`.
Purpose: 测试一个获准目标，或受约束的历史 URL 输入的连通性。
Identity scope: `bound_user`; required role: `user`.
- `path_params`: none
- `query`: `include` (string|null): 发布版本必须匹配的正则或过滤表达式。; `target_id` (string|null): system.network.targets 返回的获准内置网络测试目标标识。; `url` (string|null): 当前字段表示的站点、存储或种子地址。
- `body`: none

### `system.restart`
`GET /api/v1/system/restart`; policy effect: `external_side_effect`.
Purpose: 重启当前 MoviePilot 进程。
Identity scope: `admin`; required role: `system_admin`.
- `path_params`: none
- `query`: none
- `body`: none

### `system.update.check`
`POST /api/v1/system/update/check`; policy effect: `external_side_effect`.
Purpose: 检查 MoviePilot v3 最高稳定版本和站点资源，发现新版本时刷新较旧的已准备目标。
Identity scope: `admin`; required role: `system_admin`.
- `path_params`: none
- `query`: none
- `body`: none

### `system.update.download`
`POST /api/v1/system/update/download`; policy effect: `external_side_effect`.
Purpose: 后台下载并校验主程序或站点资源更新；target=all 表示一起准备所有最新更新。
Identity scope: `admin`; required role: `system_admin`.
- `path_params`: none
- `query`: none
- `body` (SystemUpdateRequest|null): system.update.download 的请求内容。后台下载并校验主程序或站点资源更新；target=all 表示一起准备所有最新更新。严格使用下列类型和字段。

### `system.update.install`
`POST /api/v1/system/update/install`; policy effect: `external_side_effect`.
Purpose: 安装已下载且通过校验的更新并重启；target=all 要求所有更新就绪后统一安装并只重启一次。
Identity scope: `admin`; required role: `system_admin`.
- `path_params`: none
- `query`: none
- `body` (SystemUpdateRequest|null): system.update.install 的请求内容。安装已下载且通过校验的更新并重启；target=all 要求所有更新就绪后统一安装并只重启一次。严格使用下列类型和字段。

### `system.update.status`
`GET /api/v1/system/update/status`; policy effect: `safe_read`.
Purpose: 读取主程序和站点资源的检查、下载、校验或安装状态。
Identity scope: `admin`; required role: `system_admin`.
- `path_params`: none
- `query`: none
- `body`: none

### `system.upgrade.dev`
`POST /api/v1/system/upgrade`; policy effect: `external_side_effect`.
Purpose: 更新到当前 v3 开发分支并重启。
Identity scope: `admin`; required role: `system_admin`.
- `path_params`: none
- `query`: none
- `body*` (string=dev): 固定为 dev；正式版本更新须分别调用检查、下载和安装操作。

### `system.usage.statistics`
`GET /api/v1/system/usage/statistic`; policy effect: `safe_read`.
Purpose: 读取当前用户可访问的安装版本和运行使用报告。
Identity scope: `bound_user`; required role: `user`.
- `path_params`: none
- `query`: none
- `body`: none

### `system.versions`
`GET /api/v1/system/versions`; policy effect: `safe_read`.
Purpose: 列出 GitHub 中可用的 MoviePilot 发布版本。
Identity scope: `admin`; required role: `system_admin`.
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
