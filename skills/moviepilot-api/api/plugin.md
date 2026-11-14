# Plugin APIs

Installed and marketplace plugins, configuration, lifecycle, sources, folders, ratings, releases, and statistics.

## Operations

### `plugin.capabilities`
`GET /api/v1/plugin/runtime/capabilities`; policy effect: `safe_read`.
Purpose: 查看已安装插件公开的运行时能力。
Identity scope: `admin`; required role: `system_admin`.
- `path_params`: none
- `query`: `plugin_id` (string|null): 精确的已安装插件或市场插件标识。
- `body`: none

### `plugin.clone`
`POST /api/v1/plugin/clone/{plugin_id}`; policy effect: `external_side_effect`.
Purpose: 为已安装插件创建可配置分身；suffix 留空时自动分配下一个空闲实例标识，复用已停用分身的后缀会连同保留配置重新启用该分身。
Identity scope: `admin`; required role: `system_admin`.
- `path_params`: `plugin_id*` (string): 精确的已安装插件或市场插件标识。
- `query`: none
- `body`: `description` (string; default ``): 分身展示描述; `icon` (string|null): 分身展示图标; `name` (string; default ``): 分身展示名称; `restore_previous` (boolean; default `True`): 该后缀名下留有一个已停用的分身时是否沿用它的业务参数，为假时按源插件模板重建; `suffix` (string|null): 追加到当前插件 ID 后的 ASCII 字母或数字后缀；留空时由服务端自动分配; `version` (string|null): 兼容旧客户端保留，虚拟分身始终跟随源插件版本

### `plugin.clone.restorable`
`GET /api/v1/plugin/clone/{plugin_id}/restorable`; policy effect: `safe_read`.
Purpose: 列出指定插件已停用且仍保留配置的分身，可通过同后缀创建操作恢复。
Identity scope: `admin`; required role: `system_admin`.
- `response`: `data` remains a list; omitting both `page` and `count` keeps the complete legacy result. `collection.result_count` reports the returned items and `collection.total_count` reports the exact pre-pagination total. For counts or summaries, send `page=1,count=1`, read `collection.total_count`, and do not fall back to a database query because the item preview was truncated.
- `path_params`: `plugin_id*` (string): 精确的已安装插件或市场插件标识。
- `query`: `count` (integer|null): 旧版完整列表接口的可选每页条数；提供 page 或 count 即启用分页，此时省略 count 使用 50 条。; `page` (integer|null): 旧版完整列表接口的可选页码，从 1 开始；同时省略 page 和 count 时保留原有完整结果。
- `body`: none

### `plugin.config.get`
`GET /api/v1/plugin/form/{plugin_id}`; policy effect: `safe_read`.
Purpose: 读取已加载插件的配置表单，以及合并已保存值后的默认配置。
Identity scope: `admin`; required role: `system_admin`.
- `path_params`: `plugin_id*` (string): 精确的已安装插件或市场插件标识。
- `query`: none
- `body`: none

### `plugin.config.update`
`PUT /api/v1/plugin/{plugin_id}`; policy effect: `reversible_write`.
Purpose: 替换已安装插件的完整配置并立即应用。
Identity scope: `admin`; required role: `system_admin`.
- `path_params`: `plugin_id*` (string): 精确的已安装插件或市场插件标识。
- `query`: none
- `body*` (object): 完整的插件配置对象。先调用 plugin.config.get，复制返回的配置模型，仅修改目标键后提交完整对象；只有需要删除某个键时才省略该键。更新成功后会重新初始化插件，并刷新命令、任务和路由。

### `plugin.data`
`GET /api/v1/plugin/runtime/{plugin_id}/data`; policy effect: `safe_read`.
Purpose: 读取插件持久化数据的有界预览。
Identity scope: `admin`; required role: `system_admin`.
- `path_params`: `plugin_id*` (string): 精确的已安装插件或市场插件标识。
- `query`: `key` (string|null): 用于缩小预览范围的可选精确插件数据键。; `max_chars` (integer|null): 最多返回的序列化插件数据字符数。
- `body`: none

### `plugin.default_target.clear`
`DELETE /api/v1/plugin/instances/{plugin_id}/{instance_id}/default_target`; policy effect: `reversible_write`.
Purpose: 仅当指定实例为当前默认目标时，清除其默认调用目标标记。
Identity scope: `admin`; required role: `system_admin`.
- `path_params`: `instance_id*` (string): plugin.loglevel.get 返回的精确插件实例标识。; `plugin_id*` (string): 精确的已安装插件或市场插件标识。
- `query`: none
- `body`: none

### `plugin.default_target.set`
`PUT /api/v1/plugin/instances/{plugin_id}/{instance_id}/default_target`; policy effect: `reversible_write`.
Purpose: 将一个插件实例设为默认调用目标，自动清除此前默认目标。
Identity scope: `admin`; required role: `system_admin`.
- `path_params`: `instance_id*` (string): plugin.loglevel.get 返回的精确插件实例标识。; `plugin_id*` (string): 精确的已安装插件或市场插件标识。
- `query`: none
- `body`: none

### `plugin.folder.create`
`POST /api/v1/plugin/folders/{folder_name}`; policy effect: `reversible_write`.
Purpose: 创建一个命名插件文件夹。
Identity scope: `admin`; required role: `system_admin`.
- `path_params`: `folder_name*` (string): plugin.folders.get 返回的精确插件文件夹名称。
- `query`: none
- `body`: none

### `plugin.folder.delete`
`DELETE /api/v1/plugin/folders/{folder_name}`; policy effect: `destructive_write`.
Purpose: 删除一个命名插件文件夹，保留其中已安装插件。
Identity scope: `admin`; required role: `system_admin`.
- `path_params`: `folder_name*` (string): plugin.folders.get 返回的精确插件文件夹名称。
- `query`: none
- `body`: none

### `plugin.folder.plugin.assign`
`PUT /api/v1/plugin/folders/{folder_name}/plugins/{plugin_id}`; policy effect: `reversible_write`.
Purpose: 把已安装插件移入指定文件夹，并移除其其他文件夹归属。
Identity scope: `admin`; required role: `system_admin`.
- `path_params`: `folder_name*` (string): plugin.folders.get 返回的精确插件文件夹名称。; `plugin_id*` (string): 精确的已安装插件或市场插件标识。
- `query`: none
- `body`: none

### `plugin.folder.plugin.remove`
`DELETE /api/v1/plugin/folders/{folder_name}/plugins/{plugin_id}`; policy effect: `reversible_write`.
Purpose: 从指定文件夹移除一个已安装插件，不卸载插件。
Identity scope: `admin`; required role: `system_admin`.
- `path_params`: `folder_name*` (string): plugin.folders.get 返回的精确插件文件夹名称。; `plugin_id*` (string): 精确的已安装插件或市场插件标识。
- `query`: none
- `body`: none

### `plugin.folder.plugins.update`
`PUT /api/v1/plugin/folders/{folder_name}/plugins`; policy effect: `reversible_write`.
Purpose: 替换指定插件文件夹中的有序插件标识列表。
Identity scope: `admin`; required role: `system_admin`.
- `path_params`: `folder_name*` (string): plugin.folders.get 返回的精确插件文件夹名称。
- `query`: none
- `body*` (array<string>|PluginFolderPluginsUpdateRequest): plugin.folder.plugins.update 的请求内容。替换指定插件文件夹中的有序插件标识列表。严格使用下列类型和字段。

### `plugin.folder.update`
`PATCH /api/v1/plugin/folders/{folder_name}`; policy effect: `reversible_write`.
Purpose: 增量重命名插件文件夹或更新其展示设置。
Identity scope: `admin`; required role: `system_admin`.
- `path_params`: `folder_name*` (string): plugin.folders.get 返回的精确插件文件夹名称。
- `query`: none
- `body`: `background` (string|null): 文件夹的背景样式; `color` (string|null): 文件夹的显示颜色; `gradient` (string|null): 文件夹的渐变背景配置; `icon` (string|null): 文件夹图标名称; `new_name` (string|null): 修改后的名称; `showIcon` (boolean|null): 是否显示文件夹图标

### `plugin.folders.get`
`GET /api/v1/plugin/folders`; policy effect: `safe_read`.
Purpose: 读取管理员完整插件文件夹分组配置。
Identity scope: `admin`; required role: `system_admin`.
- `path_params`: none
- `query`: none
- `body`: none

### `plugin.folders.update`
`POST /api/v1/plugin/folders`; policy effect: `reversible_write`.
Purpose: 替换管理员完整插件文件夹分组配置。
Identity scope: `admin`; required role: `system_admin`.
- `path_params`: none
- `query`: none
- `body`: `PluginFoldersData` with no direct fields

### `plugin.history`
`GET /api/v1/plugin/history/{plugin_id}`; policy effect: `safe_read`.
Purpose: 读取指定插件的市场更新说明与历史。
Identity scope: `admin`; required role: `system_admin`.
- `path_params`: `plugin_id*` (string): 精确的已安装插件或市场插件标识。
- `query`: `force` (boolean; default `True`): 为 true 时强制刷新市场或安装插件。
- `body`: none

### `plugin.install`
`GET /api/v1/plugin/install/{plugin_id}`; policy effect: `external_side_effect`.
Purpose: 从获准来源安装或更新一个插件。
Identity scope: `admin`; required role: `system_admin`.
- `path_params`: `plugin_id*` (string): 精确的已安装插件或市场插件标识。
- `query`: `force` (boolean|null; default `False`): 为 true 时强制刷新市场或安装插件。; `release_version` (string|null): 需要指定版本时使用的精确插件发布版本。; `repo_url` (string|null; default ``): 用于解析安装来源的获准插件仓库地址。
- `body`: none

### `plugin.installed`
`GET /api/v1/plugin/`; policy effect: `safe_read`.
Purpose: 列出已安装插件及其运行状态。
Identity scope: `admin`; required role: `system_admin`.
- `response`: `data` remains a list; omitting both `page` and `count` keeps the complete legacy result. `collection.result_count` reports the returned items and `collection.total_count` reports the exact pre-pagination total. For counts or summaries, send `page=1,count=1`, read `collection.total_count`, and do not fall back to a database query because the item preview was truncated.
- `path_params`: none
- `query`: `count` (integer|null): 旧版完整列表接口的可选每页条数；提供 page 或 count 即启用分页，此时省略 count 使用 50 条。; `force` (boolean; default `False`): 为 true 时强制刷新市场或安装插件。; `max_results` (integer|null): 插件目录结果数上限，范围 1 至 200；省略时返回完整目录。; `page` (integer|null): 旧版完整列表接口的可选页码，从 1 开始；同时省略 page 和 count 时保留原有完整结果。; `query` (string|null): 可选关键词，匹配插件标识、名称、说明和作者，不区分大小写。; `state*` (string=installed): 固定为 installed，仅选择该状态的插件目录条目。
- `body`: none

### `plugin.instance.purge`
`POST /api/v1/plugin/instance/{instance_id}/purge`; policy effect: `destructive_write`.
Purpose: 永久删除明确选中的实例数据：保存配置、插件数据行、独立数据库和数据目录；分身还会删除实例记录。未明确选择的项目不删除。
Identity scope: `admin`; required role: `system_admin`.
- `path_params`: `instance_id*` (string): plugin.loglevel.get 返回的精确插件实例标识。
- `query`: none
- `body`: `config` (boolean; default `False`): 是否删除该实例的业务参数; `data_directory` (boolean; default `False`): 是否删除该实例在插件数据目录下的整个目录；选中时自有数据库必然一并销毁; `own_database` (boolean; default `False`): 是否销毁该实例的自有数据库; `plugin_data` (boolean; default `False`): 是否删除该实例在插件数据表中的行
- **Historical invalid-ID recovery:** If the exact instance ID from a warning is rejected by normal validation, send only `config: true` (all other scope flags false). The host removes only an empty disabled host row with no display fields, default target, log override, or configuration; an empty configuration object still protects the row. It never stops a plugin or deletes plugin data, databases, or files. Rows with settings, clones, missing rows, or mixed scopes are rejected.

### `plugin.instance.set_enabled`
`POST /api/v1/plugin/instance/{instance_id}/enabled`; policy effect: `reversible_write`.
Purpose: 启用或停用插件本体或分身；停用只停止运行，保留配置以便以后重新启用。
Identity scope: `admin`; required role: `system_admin`.
- `path_params`: `instance_id*` (string): plugin.loglevel.get 返回的精确插件实例标识。
- `query`: none
- `body`: `enabled*` (boolean): 目标启用状态；置假即停用，业务参数与展示信息原样留存等待再次启用

### `plugin.loglevel.clear`
`DELETE /api/v1/plugin/loglevel/{plugin_id}/{instance_id}`; policy effect: `reversible_write`.
Purpose: 清除插件实例日志等级覆盖，立即恢复跟随全局等级。
Identity scope: `admin`; required role: `system_admin`.
- `path_params`: `instance_id*` (string): plugin.loglevel.get 返回的精确插件实例标识。; `plugin_id*` (string): 精确的已安装插件或市场插件标识。
- `query`: none
- `body`: none

### `plugin.loglevel.get`
`GET /api/v1/plugin/loglevel/{plugin_id}`; policy effect: `safe_read`.
Purpose: 列出指定插件的本体与分身，以及各实例配置和实际生效的日志等级。
Identity scope: `admin`; required role: `system_admin`.
- `path_params`: `plugin_id*` (string): 精确的已安装插件或市场插件标识。
- `query`: none
- `body`: none

### `plugin.loglevel.set`
`PUT /api/v1/plugin/loglevel/{plugin_id}/{instance_id}`; policy effect: `reversible_write`.
Purpose: 设置插件实例日志等级覆盖，立即生效且不再跟随全局等级。
Identity scope: `admin`; required role: `system_admin`.
- `path_params`: `instance_id*` (string): plugin.loglevel.get 返回的精确插件实例标识。; `plugin_id*` (string): 精确的已安装插件或市场插件标识。
- `query`: none
- `body`: `expires_at` (string|null): 覆盖失效时间，None 表示不过期；不带时区时按 UTC 解读; `level*` (string): 目标日志等级，如 DEBUG、INFO、WARNING、ERROR、CRITICAL

### `plugin.market`
`GET /api/v1/plugin/`; policy effect: `safe_read`.
Purpose: 列出已配置市场中可用的插件。
Identity scope: `admin`; required role: `system_admin`.
- `response`: `data` remains a list; omitting both `page` and `count` keeps the complete legacy result. `collection.result_count` reports the returned items and `collection.total_count` reports the exact pre-pagination total. For counts or summaries, send `page=1,count=1`, read `collection.total_count`, and do not fall back to a database query because the item preview was truncated.
- `path_params`: none
- `query`: `count` (integer|null): 旧版完整列表接口的可选每页条数；提供 page 或 count 即启用分页，此时省略 count 使用 50 条。; `force` (boolean; default `False`): 为 true 时强制刷新市场或安装插件。; `max_results` (integer|null): 插件目录结果数上限，范围 1 至 200；省略时返回完整目录。; `page` (integer|null): 旧版完整列表接口的可选页码，从 1 开始；同时省略 page 和 count 时保留原有完整结果。; `query` (string|null): 可选关键词，匹配插件标识、名称、说明和作者，不区分大小写。; `state*` (string=market): 固定为 market，仅选择该状态的插件目录条目。
- `body`: none

### `plugin.market.sync_wiki`
`POST /api/v1/system/setting/PLUGIN_MARKET/sync-wiki`; policy effect: `external_side_effect`.
Purpose: 从 MoviePilot Wiki 刷新已配置插件市场仓库。
Identity scope: `admin`; required role: `system_admin`.
- `path_params`: none
- `query`: none
- `body` (PluginMarketSyncRequest|null): plugin.market.sync_wiki 的请求内容。从 MoviePilot Wiki 刷新已配置插件市场仓库。严格使用下列类型和字段。

### `plugin.rating`
`GET /api/v1/plugin/rating/{plugin_id}`; policy effect: `safe_read`.
Purpose: 读取指定插件的当前评分汇总。
Identity scope: `admin`; required role: `system_admin`.
- `path_params`: `plugin_id*` (string): 精确的已安装插件或市场插件标识。
- `query`: none
- `body`: none

### `plugin.rating.submit`
`POST /api/v1/plugin/rating/{plugin_id}`; policy effect: `external_side_effect`.
Purpose: 提交或替换当前用户对指定插件的评分。
Identity scope: `admin`; required role: `system_admin`.
- `path_params`: `plugin_id*` (string): 精确的已安装插件或市场插件标识。
- `query`: none
- `body`: `rating*` (number; minimum `0.1`; maximum `5.0`): 评分，范围 0.1 至 5.0，精确到 0.1

### `plugin.ratings`
`GET /api/v1/plugin/rating`; policy effect: `safe_read`.
Purpose: 读取指定插件集合的评分汇总。
Identity scope: `admin`; required role: `system_admin`.
- `path_params`: none
- `query`: `plugin_ids` (string|null): 需要返回评分汇总的精确插件标识。
- `body`: none

### `plugin.releases`
`GET /api/v1/plugin/releases/{plugin_id}`; policy effect: `safe_read`.
Purpose: 列出指定插件来源的可用发布版本。
Identity scope: `admin`; required role: `system_admin`.
- `path_params`: `plugin_id*` (string): 精确的已安装插件或市场插件标识。
- `query`: `force` (boolean; default `False`): 为 true 时强制刷新市场或安装插件。; `repo_url` (string|null; default ``): 用于解析安装来源的获准插件仓库地址。
- `body`: none

### `plugin.reload`
`POST /api/v1/plugin/reload/{plugin_id}`; policy effect: `external_side_effect`.
Purpose: 将一个已安装插件重新加载到当前进程。
Identity scope: `admin`; required role: `system_admin`.
- `path_params`: `plugin_id*` (string): 精确的已安装插件或市场插件标识。
- `query`: none
- `body`: none

### `plugin.reset`
`GET /api/v1/plugin/reset/{plugin_id}`; policy effect: `destructive_write`.
Purpose: 删除插件已保存的配置与数据，并恢复默认运行状态。
Identity scope: `admin`; required role: `system_admin`.
- `path_params`: `plugin_id*` (string): 精确的已安装插件或市场插件标识。
- `query`: none
- `body`: none

### `plugin.runtime.status`
`GET /api/v1/plugin/runtime`; policy effect: `safe_read`.
Purpose: 读取插件运行时的收敛、加载和失败状态。
Identity scope: `admin`; required role: `system_admin`.
- `path_params`: none
- `query`: none
- `body`: none

### `plugin.source.change`
`POST /api/v1/plugin/source/{plugin_id}`; policy effect: `external_side_effect`.
Purpose: 把已安装插件切换到明确选定的在线来源修订。
Identity scope: `admin`; required role: `system_admin`.
- `path_params`: `plugin_id*` (string): 精确的已安装插件或市场插件标识。
- `query`: none
- `body`: `expected_revision*` (integer; minimum `1.0`): 提交换源时必须匹配的当前身份 revision; `release_version` (string|null): 指定安装的 Release 资产版本；为空时使用当前索引版本; `repo_url*` (string; minimum length `1`): 明确选择的目标插件仓库地址

### `plugin.source.install`
`POST /api/v1/plugin/source/{plugin_id}/install`; policy effect: `external_side_effect`.
Purpose: 从明确选定的在线来源安装一个尚未绑定来源的插件。
Identity scope: `admin`; required role: `system_admin`.
- `path_params`: `plugin_id*` (string): 精确的已安装插件或市场插件标识。
- `query`: none
- `body`: `force` (boolean; default `False`): 是否强制重新下载并安装所选来源载荷; `release_version` (string|null): 指定安装的 Release 资产版本；为空时使用当前索引版本; `repo_url*` (string; minimum length `1`): 明确选择的目标插件仓库地址

### `plugin.source.options`
`GET /api/v1/plugin/source/{plugin_id}`; policy effect: `safe_read`.
Purpose: 安装或切换来源前，查看候选来源与当前不可变来源身份。
Identity scope: `admin`; required role: `system_admin`.
- `path_params`: `plugin_id*` (string): 精确的已安装插件或市场插件标识。
- `query`: none
- `body`: none

### `plugin.statistics`
`GET /api/v1/plugin/statistic`; policy effect: `safe_read`.
Purpose: 读取公开插件安装统计。
Identity scope: `public`; required role: `user`.
- `path_params`: none
- `query`: none
- `body`: none

### `plugin.uninstall`
`DELETE /api/v1/plugin/{plugin_id}`; policy effect: `destructive_write`.
Purpose: 卸载一个插件并从已安装集合中移除。
Identity scope: `admin`; required role: `system_admin`.
- `path_params`: `plugin_id*` (string): 精确的已安装插件或市场插件标识。
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
