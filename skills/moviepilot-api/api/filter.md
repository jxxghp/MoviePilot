# Filter APIs

Built-in and custom filter rules, groups, and rule testing.

## Operations

### `filter.builtin`
`GET /api/v1/rule/builtin`; policy effect: `safe_read`.
Purpose: 列出内置种子过滤规则。
Identity scope: `bound_user`; required role: `user`.
- `path_params`: none
- `query`: `rule_ids` (array<string>|null): 要返回的内置规则标识；可在查询字符串中重复传入 rule_ids，省略时列出全部内置规则。
- `body`: none

### `filter.custom`
`GET /api/v1/rule/custom`; policy effect: `safe_read`.
Purpose: 列出用户自定义的种子过滤规则。
Identity scope: `bound_user`; required role: `user`.
- `path_params`: none
- `query`: `include_group_refs` (boolean; default `True`): 是否包含仅被规则组引用的自定义规则。; `rule_ids` (array<string>|null): 要返回的自定义规则标识；可在查询字符串中重复传入 rule_ids，省略时列出全部自定义规则。
- `body`: none

### `filter.custom.add`
`POST /api/v1/rule/custom`; policy effect: `reversible_write`.
Purpose: 创建一条自定义种子过滤规则。
Identity scope: `admin`; required role: `system_admin`.
- `path_params`: none
- `query`: none
- `body`: `exclude` (string|null): 排除; `include` (string|null): 包含; `name*` (string): 显示名称; `publish_time` (string|null): 发布时间; `rule_id*` (string): 新建过滤规则的唯一标识; `seeders` (string|null): 允许的最少做种人数; `size_range` (string|null): 大小范围（MB）

### `filter.custom.delete`
`DELETE /api/v1/rule/custom/{rule_id}`; policy effect: `destructive_write`.
Purpose: 删除一条自定义种子过滤规则。
Identity scope: `admin`; required role: `system_admin`.
- `path_params`: `rule_id*` (string): 自定义过滤规则的稳定标识。
- `query`: none
- `body`: none

### `filter.custom.update`
`PUT /api/v1/rule/custom/{rule_id}`; policy effect: `reversible_write`.
Purpose: 更新一条自定义种子过滤规则。
Identity scope: `admin`; required role: `system_admin`.
- `path_params`: `rule_id*` (string): 自定义过滤规则的稳定标识。
- `query`: none
- `body`: `exclude` (string|null): 排除; `include` (string|null): 包含; `name` (string|null): 显示名称; `new_rule_id` (string|null): 修改后的规则标识; `publish_time` (string|null): 发布时间; `seeders` (string|null): 允许的最少做种人数; `size_range` (string|null): 大小范围（MB）

### `filter.group.add`
`POST /api/v1/rule/groups`; policy effect: `reversible_write`.
Purpose: 创建一个命名过滤规则组。
Identity scope: `admin`; required role: `system_admin`.
- `path_params`: none
- `query`: none
- `body`: `category` (string|null): 媒体分类; `media_type` (string(movie,tv,music)|null): 适用媒体类型 Agent-facing values: movie,tv,music. Use this exact field name in its documented bucket.; `name*` (string): 显示名称; `rule_string*` (string): 规则串

### `filter.group.delete`
`DELETE /api/v1/rule/groups/{name}`; policy effect: `destructive_write`.
Purpose: 删除一个命名过滤规则组。
Identity scope: `admin`; required role: `system_admin`.
- `path_params`: `name*` (string): 站点、存储项目、订阅或规则组的可读名称。
- `query`: none
- `body`: none

### `filter.group.update`
`PUT /api/v1/rule/groups/{name}`; policy effect: `reversible_write`.
Purpose: 更新或重命名一个过滤规则组。
Identity scope: `admin`; required role: `system_admin`.
- `path_params`: `name*` (string): 站点、存储项目、订阅或规则组的可读名称。
- `query`: none
- `body`: `category` (string|null): 媒体分类; `media_type` (string(movie,tv,music)|null): 适用媒体类型 Agent-facing values: movie,tv,music. Use this exact field name in its documented bucket.; `new_name` (string|null): 修改后的名称; `rule_string` (string|null): 规则串

### `filter.groups`
`GET /api/v1/rule/groups`; policy effect: `safe_read`.
Purpose: 列出命名过滤规则组。
Identity scope: `bound_user`; required role: `user`.
- `path_params`: none
- `query`: `group_names` (array<string>|null): 要返回的规则组名称；可在查询字符串中重复传入 group_names，省略时列出全部规则组。; `include_usage` (boolean; default `True`): 是否返回引用各规则组的订阅或默认设置。
- `body`: none

### `filter.test`
`GET /api/v1/system/ruletest`; policy effect: `external_side_effect`.
Purpose: 使用精确命名规则组测试标题及可选副标题。
Identity scope: `bound_user`; required role: `user`.
- `path_params`: none
- `query`: `rulegroup_name*` (string): filter.groups 返回的精确过滤规则组名称。; `subtitle` (string|null): 媒体识别时与标题配合使用的可选副标题。; `title*` (string): 操作使用的媒体、种子、订阅或历史标题。
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
