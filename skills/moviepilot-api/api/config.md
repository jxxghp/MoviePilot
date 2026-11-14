# Configuration APIs

Configuration discovery, identifiers, user settings, and system setting updates.

## Operations

### `config.identifiers.get`
`GET /api/v1/system/identifiers`; policy effect: `safe_read`.
Purpose: 读取完整的自定义媒体识别词列表。
Identity scope: `admin`; required role: `system_admin`.
- `path_params`: none
- `query`: none
- `body`: none

### `config.identifiers.update`
`POST /api/v1/system/identifiers`; policy effect: `reversible_write`.
Purpose: 替换完整的自定义媒体识别词列表。
Identity scope: `admin`; required role: `system_admin`.
- `path_params`: none
- `query`: none
- `body`: `expected_identifiers` (array<string>|null): 最近读取的完整有序列表；提供时若现有列表已变化则拒绝替换; `identifiers` (array<string>): 按顺序排列的完整自定义识别词规则列表

### `config.public.get`
`GET /api/v1/system/setting/public/{key}`; policy effect: `safe_read`.
Purpose: 按精确键读取一个明确公开的系统设置项。
Identity scope: `bound_user`; required role: `user`.
- `path_params`: `key*` (string): 用于缩小预览范围的可选精确插件数据键。
- `query`: none
- `body`: none

### `config.system.describe`
`GET /api/v1/system/settings/describe/{setting_key}`; policy effect: `safe_read`.
Purpose: 读取一个精确设置项的契约、当前值和修订号。
Identity scope: `admin`; required role: `system_admin`.
- `path_params`: `setting_key*` (string): config.system.get 发现结果返回的精确已登记设置键。
- `query`: `include_value` (boolean; default `True`): 是否在完整契约之外返回当前值。; `show_secrets` (boolean; default `False`): 是否返回未脱敏的秘密值，仅可在明确授权后使用。
- `body`: none

### `config.system.get`
`GET /api/v1/system/settings`; policy effect: `safe_read`.
Purpose: 发现已登记系统设置，或读取一个精确设置项。
Identity scope: `admin`; required role: `system_admin`.
- `path_params`: none
- `query`: `group` (string|null; default `all`): 省略 setting_key 时使用的发现分组。调用 config.system.list 可获取完整、机器可读的分组索引；此兼容端点接受全部已登记分组，包括 settings 和 systemconfig。; `include_values` (boolean|null): 是否返回完整值；查询单个精确设置键时默认为 True，发现设置列表时默认为 False。; `keyword` (string|null): 用于发现匹配设置键、分组或标签的子串，不区分大小写。; `setting_key` (string|null): 精确设置键，接受 APP_DOMAIN 或 LLM_MODEL 等 Settings 字段名、Downloaders 或 MediaServers 等 SystemConfigKey 值或枚举名，以及能唯一解析的别名；省略时按列表发现设置。; `show_secrets` (boolean; default `False`): 是否返回未脱敏的秘密值；默认为 False，启用时仍需通过确认保护。
- `body`: none

### `config.system.list`
`GET /api/v1/system/settings/catalog`; policy effect: `safe_read`.
Purpose: 列出已登记系统设置的精简机器可读契约。
Identity scope: `admin`; required role: `system_admin`.
- `path_params`: none
- `query`: `group` (string|null; default `all`): 设置契约分组；使用 all 可列出全部已登记设置。; `keyword` (string|null): 匹配设置键、分组、标签或说明的子串，不区分大小写。; `limit` (integer; default `100`; minimum `1`; maximum `500`): 最多返回的设置契约条数。; `offset` (integer; default `0`; minimum `0`): 结果偏移量，从 0 开始。; `source` (string|null): 可选的来源筛选：settings 或 systemconfig。
- `body`: none

### `config.system.update`
`POST /api/v1/system/settings`; policy effect: `reversible_write`.
Purpose: 更新一个精确的已登记系统设置项。
Identity scope: `admin`; required role: `system_admin`.
- `path_params`: none
- `query`: none
- `body`: `expected_revision` (string|null): 此设置最近读取的版本；提供时若期间有其他写入则拒绝更新，新 Agent 客户端应始终提供; `match_field` (string|null): 列表项匹配字段；下载器、媒体服务器、通知、目录和存储默认用 name，通知场景开关默认用 type，其他对象列表需显式指定; `match_value` (value): 与匹配字段比较的值；省略时使用 value 中对应字段，标量列表直接使用 value; `operation` (string(replace,merge_dict,upsert_list_item,remove_list_item); default `replace`): replace 整体替换；merge_dict 浅层合并对象；upsert_list_item 插入或替换匹配项；remove_list_item 删除匹配项; `remove_keys` (array<string>): 完成 merge_dict 合并后需要删除的对象键; `setting_key*` (string): 精确的设置键，可使用 Settings 字段名、SystemConfigKey 值或枚举名，以及能唯一解析的别名；未知时先调用 config.system.get 按组或关键词查询; `value` (value): 新值或列表项；replace 提交完整值，merge_dict 提交待合并的对象片段，列表增删操作提交单个对象或标量

### `config.user.get`
`GET /api/v1/system/global/user`; policy effect: `safe_read`.
Purpose: 读取当前用户功能开关、运行时能力和实际权限。
Identity scope: `bound_user`; required role: `user`.
- `path_params`: none
- `query`: none
- `body`: none

## System Settings Contract

Do not enumerate setting keys in this Skill. Settings change as MoviePilot evolves, so use `config.system.get` as the runtime discovery operation before updating an unfamiliar key.

| `source` | Contents | Persistence |
| :--- | :--- | :--- |
| `settings` | Runtime `Settings` fields such as APP_DOMAIN or LLM_MODEL | Type-converted and persisted to `app.env`, then applied to the current process |
| `systemconfig` | Database-backed business configuration such as downloaders, media servers, directories, and notifications | Written through the configuration service with plugin admission and change events |

The `systemconfig` database table is only the physical store for the second source. Use `config.system.get/update` for normal reads and writes. Direct SQL is reserved for an explicitly authorized repair when the managed API cannot complete the operation.

### Discover definitions

1. Call `config.system.get` with `query={"group":"settings","keyword":"LLM"}` or another group/keyword. Discovery defaults to summaries instead of full values.
2. Each returned setting includes `setting_key`, `source`, `group`, `label`, and a `definition` object with `declared_type`, current `value_shape`, `nullable`, `sensitive`, allowed `update_operations`, `default_match_field`, and `persistence`.
3. Read one exact value with `query={"setting_key":"LLM_MODEL"}`. Exact-key reads include the value by default.
4. Use `show_secrets=true` only when an administrator explicitly requests the plaintext value; secret reads remain confirmation-protected.

### Update settings

Choose an operation listed in the discovered setting definition, then send it in `body`:

| operation | Fields | Meaning |
| :--- | :--- | :--- |
| `replace` | `setting_key*`, `value` | Replace the complete scalar, list, or object value |
| `merge_dict` | `setting_key*`, `value`; optional `remove_keys` | Shallow-merge an object and optionally remove keys |
| `upsert_list_item` | `setting_key*`, `value`; optional `match_field`, `match_value` | Replace a matched list item or append it when absent |
| `remove_list_item` | `setting_key*`, `value` or `match_value`; optional `match_field` | Remove one matched list item without replacing the list |

After every update, call `config.system.get` again with the exact setting_key and verify the saved value. Do not guess a key, value shape, list match field, or update operation when discovery can return it.

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
