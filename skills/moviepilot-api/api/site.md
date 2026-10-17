# Site APIs

Site discovery, authentication, cookies, user data, resources, RSS, priorities, statistics, and lifecycle.

## Operations

### `site.add`
`POST /api/v1/site/`; policy effect: `reversible_write`.
Purpose: 使用完整认证与搜索设置创建一个站点。
Identity scope: `admin`; required role: `system_admin`.
- `path_params`: none
- `query`: none
- `body`: `apikey` (string|null): 站点接口访问密钥; `cookie` (string|null): 站点登录凭据; `domain` (string|null): 用于匹配站点的主域名键; `downloader` (string|null): 下载器; `filter` (string|null): 过滤规则; `id` (integer|null): 记录标识; `is_active` (boolean|null; default `True`): 是否启用; `limit_count` (integer|null): 流控统计窗口内允许的最大请求数；None 表示未配置; `limit_interval` (integer|null): 请求流控统计窗口长度，单位为秒；None 表示未配置; `limit_seconds` (integer|null): 相邻请求之间的最小间隔，单位为秒；None 表示未配置; `name` (string|null): 站点名称; `note` (JsonData-Input|null): 备注; `pri` (integer|null; default `0`): 站点搜索优先级，数值越小越靠前，默认 0; `proxy` (integer|null; default `0`): 是否使用代理; `public` (integer|null; default `0`): 是否公开站点; `render` (integer|null; default `0`): 是否使用浏览器渲染站点页面，0 表示关闭; `rss` (string|null): RSS地址; `timeout` (integer|null; default `15`): 单次站点请求的超时秒数，默认 15 秒; `token` (string|null): 认证令牌; `ua` (string|null): 站点请求使用的浏览器标识; `url` (string|null): 站点地址

### `site.auth.options`
`GET /api/v1/site/auth`; policy effect: `safe_read`.
Purpose: 列出站点账号认证提供方及所需输入定义。
Identity scope: `bound_user`; required role: `user`.
- `path_params`: none
- `query`: none
- `body`: none

### `site.authenticate`
`POST /api/v1/site/auth`; policy effect: `external_side_effect`.
Purpose: 认证受支持站点的账号，并保存取得的站点授权状态。
Identity scope: `admin`; required role: `system_admin`.
- `path_params`: none
- `query`: none
- `body`: `params` (object|null): 请求参数; `site` (string|null): 站点ID

### `site.category`
`GET /api/v1/site/category/{site_id}`; policy effect: `safe_read`.
Purpose: 列出指定站点支持的种子分类。
Identity scope: `bound_user`; required role: `user`.
- `response`: `data` remains a list; omitting both `page` and `count` keeps the complete legacy result. `collection.result_count` reports the returned items and `collection.total_count` reports the exact pre-pagination total. For counts or summaries, send `page=1,count=1`, read `collection.total_count`, and do not fall back to a database query because the item preview was truncated.
- `path_params`: `site_id*` (integer): site.list 返回的持久化站点标识。
- `query`: `count` (integer|null): 旧版完整列表接口的可选每页条数；提供 page 或 count 即启用分页，此时省略 count 使用 50 条。; `page` (integer|null): 旧版完整列表接口的可选页码，从 1 开始；同时省略 page 和 count 时保留原有完整结果。
- `body`: none

### `site.cookie.set`
`POST /api/v1/site/cookie/{site_id}/set`; policy effect: `reversible_write`.
Purpose: 为一个站点保存浏览器取得的认证 Cookie 和可选 User-Agent，保留其他设置。
Identity scope: `admin`; required role: `system_admin`.
- `path_params`: `site_id*` (integer): site.list 返回的持久化站点标识。
- `query`: none
- `body`: `cookie*` (string): 可信浏览器登录会话提供的站点 Cookie 请求头; `ua` (string|null): 与已认证浏览器会话对应的浏览器标识

### `site.cookie.update`
`POST /api/v1/site/cookie/{site_id}`; policy effect: `reversible_write`.
Purpose: 登录一个站点并刷新保存的认证 Cookie。
Identity scope: `admin`; required role: `system_admin`.
- `path_params`: `site_id*` (integer): site.list 返回的持久化站点标识。
- `query`: none
- `body`: `code` (string|null): 二步验证码或密钥; `password*` (string): 站点登录密码; `username*` (string): 站点登录用户名

### `site.cookiecloud.sync`
`POST /api/v1/site/cookiecloud`; policy effect: `external_side_effect`.
Purpose: 启动已配置站点的 CookieCloud 同步。
Identity scope: `admin`; required role: `system_admin`.
- `path_params`: none
- `query`: none
- `body`: none

### `site.delete`
`DELETE /api/v1/site/{site_id}`; policy effect: `destructive_write`.
Purpose: 按持久化站点标识删除一个站点。
Identity scope: `admin`; required role: `system_admin`.
- `path_params`: `site_id*` (integer): site.list 返回的持久化站点标识。
- `query`: none
- `body`: none

### `site.list`
`GET /api/v1/site/agent`; policy effect: `safe_read`.
Purpose: 按状态和名称筛选已配置站点；认证字段仅向超级管理员返回。
Identity scope: `bound_user`; required role: `user`.
- `response`: `data` remains a list; omitting both `page` and `count` keeps the complete legacy result. `collection.result_count` reports the returned items and `collection.total_count` reports the exact pre-pagination total. For counts or summaries, send `page=1,count=1`, read `collection.total_count`, and do not fall back to a database query because the item preview was truncated.
- `path_params`: none
- `query`: `count` (integer|null): 旧版完整列表接口的可选每页条数；提供 page 或 count 即启用分页，此时省略 count 使用 50 条。; `name` (string|null): 站点、存储项目、订阅或规则组的可读名称。; `page` (integer|null): 旧版完整列表接口的可选页码，从 1 开始；同时省略 page 和 count 时保留原有完整结果。; `status` (string(active,inactive,all); default `all`): 用于筛选历史或描述记录的整理成功状态。
- `body`: none

### `site.mapping`
`GET /api/v1/site/mapping`; policy effect: `safe_read`.
Purpose: 读取已配置站点域名到名称的映射。
Identity scope: `admin`; required role: `system_admin`.
- `path_params`: none
- `query`: none
- `body`: none

### `site.priorities.update`
`POST /api/v1/site/priorities`; policy effect: `reversible_write`.
Purpose: 替换指定站点标识对应的优先级。
Identity scope: `admin`; required role: `system_admin`.
- `path_params`: none
- `query`: none
- `body*` (array<SitePriorityUpdate>): site.priorities.update 的请求内容。替换指定站点标识对应的优先级。严格使用下列类型和字段。

### `site.reset`
`POST /api/v1/site/reset`; policy effect: `destructive_write`.
Purpose: 删除全部已配置站点，并重新启动 CookieCloud 同步。
Identity scope: `admin`; required role: `system_admin`.
- `path_params`: none
- `query`: none
- `body`: none

### `site.resource`
`GET /api/v1/site/resource/{site_id}`; policy effect: `external_side_effect`.
Purpose: 按分类和关键词浏览指定站点的种子资源。
Identity scope: `admin`; required role: `system_admin`.
- `response`: `data` remains a list and `collection.result_count` reports the returned items. `collection.total_count` is omitted because this endpoint or its upstream source does not expose a total.
- `path_params`: `site_id*` (integer): site.list 返回的持久化站点标识。
- `query`: `cat` (string|null): site.category 返回的精确站点分类标识。; `keyword` (string|null): 发现设置或筛选存储项目时使用的不区分大小写子串。; `mtype` (string(movie,tv,music)|null): 操作要求的媒体类型或订阅历史分类。 Agent-facing values: movie,tv,music. Use this exact field name in its documented bucket.; `page` (integer|null; default `0`): 从 1 开始的结果页码。
- `body`: none

### `site.rss`
`GET /api/v1/site/rss`; policy effect: `safe_read`.
Purpose: 列出已选入 RSS 订阅处理的站点。
Identity scope: `admin`; required role: `system_admin`.
- `response`: `data` remains a list; omitting both `page` and `count` keeps the complete legacy result. `collection.result_count` reports the returned items and `collection.total_count` reports the exact pre-pagination total. For counts or summaries, send `page=1,count=1`, read `collection.total_count`, and do not fall back to a database query because the item preview was truncated.
- `path_params`: none
- `query`: `count` (integer|null): 旧版完整列表接口的可选每页条数；提供 page 或 count 即启用分页，此时省略 count 使用 50 条。; `page` (integer|null): 旧版完整列表接口的可选页码，从 1 开始；同时省略 page 和 count 时保留原有完整结果。
- `body`: none

### `site.searchable`
`GET /api/v1/site/media/{media_type}`; policy effect: `safe_read`.
Purpose: 列出已启用且支持精确媒体类型的站点。
Identity scope: `admin`; required role: `system_admin`.
- `response`: `data` remains a list; omitting both `page` and `count` keeps the complete legacy result. `collection.result_count` reports the returned items and `collection.total_count` reports the exact pre-pagination total. For counts or summaries, send `page=1,count=1`, read `collection.total_count`, and do not fall back to a database query because the item preview was truncated.
- `path_params`: `media_type*` (string(movie,tv,music)): 筛选推荐或规则组使用的媒体类型。 Agent-facing values: movie,tv,music. Use this exact field name in its documented bucket.
- `query`: `count` (integer|null): 旧版完整列表接口的可选每页条数；提供 page 或 count 即启用分页，此时省略 count 使用 50 条。; `page` (integer|null): 旧版完整列表接口的可选页码，从 1 开始；同时省略 page 和 count 时保留原有完整结果。
- `body`: none
- **Agent note:** `media_type` is a **path** parameter and MUST be nested under `path_params`. Never flatten it to the top level or place it inside `query`, otherwise the gateway rejects with `invalid_input`.

### `site.statistic`
`GET /api/v1/site/statistic/{site_url}`; policy effect: `safe_read`.
Purpose: 读取精确站点域名对应的账号和流量统计。
Identity scope: `bound_user`; required role: `user`.
- `path_params`: `site_url*` (string): 选取站点统计使用的已配置站点地址或主机名。
- `query`: none
- `body`: none

### `site.statistics`
`GET /api/v1/site/statistic`; policy effect: `safe_read`.
Purpose: 读取所有已配置站点的最新账号和流量统计。
Identity scope: `bound_user`; required role: `user`.
- `response`: `data` remains a list; omitting both `page` and `count` keeps the complete legacy result. `collection.result_count` reports the returned items and `collection.total_count` reports the exact pre-pagination total. For counts or summaries, send `page=1,count=1`, read `collection.total_count`, and do not fall back to a database query because the item preview was truncated.
- `path_params`: none
- `query`: `count` (integer|null): 旧版完整列表接口的可选每页条数；提供 page 或 count 即启用分页，此时省略 count 使用 50 条。; `page` (integer|null): 旧版完整列表接口的可选页码，从 1 开始；同时省略 page 和 count 时保留原有完整结果。
- `body`: none

### `site.supporting`
`GET /api/v1/site/supporting`; policy effect: `safe_read`.
Purpose: 列出已安装资源支持的站点索引器定义。
Identity scope: `admin`; required role: `system_admin`.
- `path_params`: none
- `query`: none
- `body`: none

### `site.test`
`GET /api/v1/site/test/{site_id}`; policy effect: `safe_read`.
Purpose: 测试一个已配置站点的连通性和认证状态。
Identity scope: `bound_user`; required role: `user`.
- `path_params`: `site_id*` (integer): site.list 返回的持久化站点标识。
- `query`: none
- `body`: none

### `site.update`
`PUT /api/v1/site/`; policy effect: `reversible_write`.
Purpose: 更新一个已配置站点的完整设置。
Identity scope: `admin`; required role: `system_admin`.
- `path_params`: none
- `query`: none
- `body`: `apikey` (string|null): 站点接口访问密钥; `cookie` (string|null): 站点登录凭据; `domain` (string|null): 用于匹配站点的主域名键; `downloader` (string|null): 下载器; `filter` (string|null): 过滤规则; `id` (integer|null): 记录标识; `is_active` (boolean|null; default `True`): 是否启用; `limit_count` (integer|null): 流控统计窗口内允许的最大请求数；None 表示未配置; `limit_interval` (integer|null): 请求流控统计窗口长度，单位为秒；None 表示未配置; `limit_seconds` (integer|null): 相邻请求之间的最小间隔，单位为秒；None 表示未配置; `name` (string|null): 站点名称; `note` (JsonData-Input|null): 备注; `pri` (integer|null; default `0`): 站点搜索优先级，数值越小越靠前，默认 0; `proxy` (integer|null; default `0`): 是否使用代理; `public` (integer|null; default `0`): 是否公开站点; `render` (integer|null; default `0`): 是否使用浏览器渲染站点页面，0 表示关闭; `rss` (string|null): RSS地址; `timeout` (integer|null; default `15`): 单次站点请求的超时秒数，默认 15 秒; `token` (string|null): 认证令牌; `ua` (string|null): 站点请求使用的浏览器标识; `url` (string|null): 站点地址

### `site.userdata`
`GET /api/v1/site/userdata/{site_id}`; policy effect: `safe_read`.
Purpose: 读取最近采集的指定站点账号统计。
Identity scope: `admin`; required role: `system_admin`.
- `response`: `data` remains a list; omitting both `page` and `count` keeps the complete legacy result. `collection.result_count` reports the returned items and `collection.total_count` reports the exact pre-pagination total. For counts or summaries, send `page=1,count=1`, read `collection.total_count`, and do not fall back to a database query because the item preview was truncated.
- `path_params`: `site_id*` (integer): site.list 返回的持久化站点标识。
- `query`: `count` (integer|null): 旧版完整列表接口的可选每页条数；提供 page 或 count 即启用分页，此时省略 count 使用 50 条。; `page` (integer|null): 旧版完整列表接口的可选页码，从 1 开始；同时省略 page 和 count 时保留原有完整结果。; `workdate` (string|null): 读取站点历史用户统计时使用的日期。
- `body`: none

### `site.userdata.latest`
`GET /api/v1/site/userdata/latest`; policy effect: `safe_read`.
Purpose: 读取每个已配置站点最近采集的账号统计。
Identity scope: `admin`; required role: `system_admin`.
- `response`: `data` remains a list; omitting both `page` and `count` keeps the complete legacy result. `collection.result_count` reports the returned items and `collection.total_count` reports the exact pre-pagination total. For counts or summaries, send `page=1,count=1`, read `collection.total_count`, and do not fall back to a database query because the item preview was truncated.
- `path_params`: none
- `query`: `count` (integer|null): 旧版完整列表接口的可选每页条数；提供 page 或 count 即启用分页，此时省略 count 使用 50 条。; `page` (integer|null): 旧版完整列表接口的可选页码，从 1 开始；同时省略 page 和 count 时保留原有完整结果。
- `body`: none

### `site.userdata.refresh`
`POST /api/v1/site/userdata/{site_id}`; policy effect: `external_side_effect`.
Purpose: 刷新并返回指定站点的账号统计。
Identity scope: `admin`; required role: `system_admin`.
- `path_params`: `site_id*` (integer): site.list 返回的持久化站点标识。
- `query`: none
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
