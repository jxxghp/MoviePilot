# Subscription APIs

Subscription creation, search, refresh, sharing, following, history, files, and status management.

## Operations

### `subscription.add`
`POST /api/v1/subscribe/`; policy effect: `reversible_write`.
Purpose: 创建一个电影、电视剧或音乐订阅。
Identity scope: `bound_user`; required role: `user`.
- `path_params`: none
- `query`: none
- `body`: `audio_format` (string|null): 音频格式正则，如 FLAC|ALAC; `audio_quality` (string|null): 音乐音质等级，可用 | 组合 hires/lossless/lossy; `backdrop` (string|null): 背景图; `best_version` (integer|null): 是否洗版; `best_version_full` (integer|null): 是否只洗全集整包; `classification_policy_revision` (integer|null): 历史执行时分类策略版本，活动订阅中为空; `classification_rule_id` (string|null): 历史命中的分类规则标识，活动订阅中为空; `classification_source` (string|null): 历史最终分类来源，活动订阅中为空; `completed_episode` (integer|null): 已完成集数; `completed_tracks` (integer|null): 已累计下载曲目数，仅由后端根据专辑音轨事实派生; `current_audio_format` (string|null): 当前音乐版本格式; `current_bit_depth` (integer|null): 当前音乐版本位深（bit）; `current_bitrate` (integer|null): 当前音乐版本码率（bps）; `current_priority` (integer|null): 当前优先级; `current_sample_rate` (integer|null): 当前音乐版本采样率（Hz）; `custom_words` (string|null): 自定义识别词; `date` (string|null): 时间; `description` (string|null): 描述; `downloader` (string|null): 下载器; `effect` (string|null): 特效; `episode_group` (string|null): 剧集组; `episode_priority` (object|null): 洗版时已下载剧集的优先级状态; `exclude` (string|null): 排除; `execution_status` (SubscriptionExecutionStatus|null): 当前搜索或下载执行状态，只用于响应投影; `filter` (string|null): 过滤规则; `filter_groups` (array<string>|null): 过滤规则组; `id` (integer|null): 记录标识; `include` (string|null): 包含; `keyword` (string|null): 搜索关键字; `lack_episode` (integer|null; default `0`): 缺失集数; `last_search` (string|null): 最近一次主动搜索开始时间，仅供读取; `last_update` (string|null): 最后更新时间; `media_category` (string|null): 自定义媒体类别兼容路径快照; `media_category_id` (string|null): 自定义媒体类别稳定标识; `media_id` (string|null): 数据源原生 ID; `media_source` (MediaSource|null): 媒体数据源; `min_bit_depth` (integer|null): 最低位深（bit）; `min_bitrate` (integer|null): 最低码率（bps）; `min_sample_rate` (integer|null): 最低采样率（Hz）; `music_type` (string|null): 音乐实体类型：recording 单曲、album 专辑; `name` (string|null): 订阅名称; `note` (array<integer>|null): 附加信息; `poster` (string|null): 海报; `quality` (string|null): 质量; `resolution` (string|null): 分辨率; `save_path` (string|null): 保存路径; `search_imdbid` (integer|null; default `0`): 是否使用 imdbid 搜索; `search_interval` (integer|null): 定时搜索间隔（小时）；空值跟随系统设置; `season` (integer|null): 季号; `sites` (array<integer>|null): 订阅站点; `start_episode` (integer|null; default `0`): 开始集数; `state` (string|null): 状态：N-新建， R-订阅中; `total_episode` (integer|null; default `0`): 总集数; `total_tracks` (integer|null): 专辑预期总曲目数; `type` (string|null): 订阅类型 电影/电视剧; `username` (string|null): 订阅用户; `vote` (number|null; default `0.0`): 评分; `year` (string|null): 媒体发行年份

### `subscription.delete`
`DELETE /api/v1/subscribe/{subscribe_id}`; policy effect: `destructive_write`.
Purpose: 删除一个当前订阅。
Identity scope: `bound_user`; required role: `user`.
- `path_params`: `subscribe_id*` (integer): subscription.list 返回的持久化订阅标识。
- `query`: none
- `body`: none

### `subscription.delete_by_media`
`DELETE /api/v1/subscribe/media/{media_id}`; policy effect: `destructive_write`.
Purpose: 删除当前用户可访问且匹配规范媒体身份的订阅。
Identity scope: `bound_user`; required role: `user`.
- `path_params`: `media_id*` (string): 来源原生媒体标识，始终与搜索返回的精确 media_source 配对。
- `query`: `media_source*` (MediaSource): 元数据来源标识，保留与 media_id 一同返回的精确值。; `music_type` (string|null): 音乐身份层级：按操作支持范围选择 recording、album 或 artist。; `season` (integer|null): 媒体、搜索、订阅或整理操作使用的季号。
- `body`: none

### `subscription.execution.cancel`
`PUT /api/v1/subscribe/execution/batches/{batch_id}/cancel`; policy effect: `external_side_effect`.
Purpose: 请求取消一个尚未越过下载副作用边界的持久化订阅搜索批次。
Identity scope: `bound_user`; required role: `user`.
- `path_params`: `batch_id*` (string): 稳定的订阅搜索批次标识。
- `query`: none
- `body`: none

### `subscription.execution.get`
`GET /api/v1/subscribe/execution/batches/{batch_id}`; policy effect: `safe_read`.
Purpose: 读取当前用户可见的一个持久化订阅搜索批次。
Identity scope: `bound_user`; required role: `user`.
- `path_params`: `batch_id*` (string): 稳定的订阅搜索批次标识。
- `query`: none
- `body`: none

### `subscription.execution.list`
`GET /api/v1/subscribe/execution/batches`; policy effect: `safe_read`.
Purpose: 列出当前用户可见的近期持久化订阅搜索批次。
Identity scope: `bound_user`; required role: `user`.
- `response`: `data` remains a list and `collection.result_count` reports the returned items. `collection.total_count` is omitted because this endpoint or its upstream source does not expose a total.
- `path_params`: none
- `query`: `limit` (integer; default `10`): 最多返回的近期订阅搜索批次数。
- `body`: none

### `subscription.files`
`GET /api/v1/subscribe/files/{subscribe_id}`; policy effect: `safe_read`.
Purpose: 读取可访问订阅的本地媒体库及整理文件覆盖情况。
Identity scope: `bound_user`; required role: `user`.
- `path_params`: `subscribe_id*` (integer): subscription.list 返回的持久化订阅标识。
- `query`: none
- `body`: none

### `subscription.find`
`GET /api/v1/subscribe/media/{media_id}`; policy effect: `safe_read`.
Purpose: 按规范媒体身份和可选季查找一个可访问订阅。 For movies and TV, if exact identity misses and mtype/title are supplied, match across sources by type, normalized title, optional season, and year. An exact year ranks first; a subscription with no year may match when the card has a year, while a card with no year only matches a subscription with no year. Music uses exact identity only.
Identity scope: `bound_user`; required role: `user`.
- `path_params`: `media_id*` (string): 来源原生媒体标识，始终与搜索返回的精确 media_source 配对。
- `query`: `media_source*` (MediaSource): 元数据来源标识，保留与 media_id 一同返回的精确值。; `mtype` (MediaType|null): 操作要求的媒体类型或订阅历史分类。; `music_type` (string|null): 音乐身份层级：按操作支持范围选择 recording、album 或 artist。; `season` (integer|null): 媒体、搜索、订阅或整理操作使用的季号。; `title` (string|null): 操作使用的媒体、种子、订阅或历史标题。; `year` (string|null): 用于区分同名媒体的发行或首映年份。
- `body`: none

### `subscription.follow.add`
`POST /api/v1/subscribe/follow`; policy effect: `reversible_write`.
Purpose: 按精确分享用户标识关注一个订阅分享用户。
Identity scope: `bound_user`; required role: `user`.
- `path_params`: none
- `query`: `share_uid` (string|null): 需要关注或取消关注的精确 MoviePilot Server 分享用户标识。
- `body`: none

### `subscription.follow.delete`
`DELETE /api/v1/subscribe/follow`; policy effect: `reversible_write`.
Purpose: 按精确分享用户标识取消关注一个订阅分享用户。
Identity scope: `bound_user`; required role: `user`.
- `path_params`: none
- `query`: `share_uid` (string|null): 需要关注或取消关注的精确 MoviePilot Server 分享用户标识。
- `body`: none

### `subscription.follow.list`
`GET /api/v1/subscribe/follow`; policy effect: `safe_read`.
Purpose: 列出当前用户关注的订阅分享用户标识。
Identity scope: `bound_user`; required role: `user`.
- `response`: `data` remains a list; omitting both `page` and `count` keeps the complete legacy result. `collection.result_count` reports the returned items and `collection.total_count` reports the exact pre-pagination total. For counts or summaries, send `page=1,count=1`, read `collection.total_count`, and do not fall back to a database query because the item preview was truncated.
- `path_params`: none
- `query`: `count` (integer|null): 旧版完整列表接口的可选每页条数；提供 page 或 count 即启用分页，此时省略 count 使用 50 条。; `page` (integer|null): 旧版完整列表接口的可选页码，从 1 开始；同时省略 page 和 count 时保留原有完整结果。
- `body`: none

### `subscription.fork`
`POST /api/v1/subscribe/fork`; policy effect: `external_side_effect`.
Purpose: 根据共享订阅定义创建一个本地订阅。
Identity scope: `bound_user`; required role: `user`.
- `path_params`: none
- `query`: none
- `body`: `audio_format` (string|null): 音频格式; `audio_quality` (string|null): 音乐音质等级; `backdrop` (string|null): 背景图; `count` (integer|null; default `0`): 复用人次; `custom_words` (string|null): 自定义识别词; `date` (string|null): 时间; `description` (string|null): 描述; `effect` (string|null): 特效; `episode_group` (string|null): 自定义剧集组; `exclude` (string|null): 排除; `id` (integer|null): 分享ID; `include` (string|null): 包含; `keyword` (string|null): 搜索关键字; `media_category` (string|null): 自定义媒体类别兼容路径快照; `media_category_id` (string|null): 自定义媒体类别稳定标识; `media_id` (string|null): 数据源原生 ID; `media_source` (MediaSource|null): 媒体数据源; `min_bit_depth` (integer|null): 最低位深（bit）; `min_bitrate` (integer|null): 最低码率（bps）; `min_sample_rate` (integer|null): 最低采样率（Hz）; `music_type` (string|null): 音乐实体类型：recording 单曲、album 专辑; `name` (string|null): 订阅名称; `poster` (string|null): 海报; `quality` (string|null): 质量; `resolution` (string|null): 分辨率; `season` (integer|null): 季号; `share_comment` (string|null): 分享说明; `share_title` (string|null): 分享标题; `share_uid` (string|null): 分享人唯一ID; `share_user` (string|null): 分享人; `subscribe_id` (integer|null): 订阅ID; `total_episode` (integer|null; default `0`): 总集数; `total_tracks` (integer|null): 专辑预期总曲目数; `type` (string|null): 订阅类型 电影/电视剧; `vote` (number|null; default `0.0`): 评分; `year` (string|null): 媒体发行年份

### `subscription.get`
`GET /api/v1/subscribe/{subscribe_id}`; policy effect: `safe_read`.
Purpose: 按持久化订阅标识读取一个可访问订阅。
Identity scope: `bound_user`; required role: `user`.
- `path_params`: `subscribe_id*` (integer): subscription.list 返回的持久化订阅标识。
- `query`: none
- `body`: none

### `subscription.history`
`GET /api/v1/subscribe/history/{mtype}`; policy effect: `safe_read`.
Purpose: 列出已完成或已归档的订阅记录。
Identity scope: `bound_user`; required role: `user`.
- `response`: `data` remains a list and the endpoint's documented pagination or limit defaults remain in effect. `collection.result_count` reports the returned items and `collection.total_count` reports the exact total. For a count-only request, use the smallest valid page and read that metadata instead of querying the database after item truncation.
- `path_params`: `mtype*` (string): 操作要求的媒体类型或订阅历史分类。
- `query`: `count` (integer|null; default `30`): 当前页最多返回的记录数。; `page` (integer|null; default `1`): 从 1 开始的结果页码。
- `body`: none

### `subscription.history.delete`
`DELETE /api/v1/subscribe/history/{history_id}`; policy effect: `destructive_write`.
Purpose: 删除一条可访问的订阅历史记录。
Identity scope: `bound_user`; required role: `user`.
- `path_params`: `history_id*` (integer): 历史查询操作返回的持久化整理或订阅历史标识。
- `query`: none
- `body`: none

### `subscription.list`
`GET /api/v1/subscribe/`; policy effect: `safe_read`.
Purpose: 列出当前订阅。
Identity scope: `bound_user`; required role: `user`.
- `response`: `data` remains a list; omitting both `page` and `count` keeps the complete legacy result. `collection.result_count` reports the returned items and `collection.total_count` reports the exact pre-pagination total. For counts or summaries, send `page=1,count=1`, read `collection.total_count`, and do not fall back to a database query because the item preview was truncated.
- `path_params`: none
- `query`: `count` (integer|null): 旧版完整列表接口的可选每页条数；提供 page 或 count 即启用分页，此时省略 count 使用 50 条。; `page` (integer|null): 旧版完整列表接口的可选页码，从 1 开始；同时省略 page 和 count 时保留原有完整结果。
- `body`: none

### `subscription.metadata.refresh`
`POST /api/v1/subscribe/check`; policy effect: `external_side_effect`.
Purpose: 启动全系统订阅 TMDB 元数据刷新。
Identity scope: `admin`; required role: `system_admin`.
- `path_params`: none
- `query`: `type` (MediaType|null): 仅处理指定媒体类型的订阅；省略时处理全部类型
- `body`: none

### `subscription.popular`
`GET /api/v1/subscribe/popular`; policy effect: `safe_read`.
Purpose: 按条件和分页读取全站热门订阅。
Identity scope: `public`; required role: `user`.
- `response`: `data` remains a list and `collection.result_count` reports the returned items. `collection.total_count` is omitted because this endpoint or its upstream source does not expose a total.
- `path_params`: none
- `query`: `count` (integer|null; default `30`): 当前页最多返回的记录数。; `genre_id` (integer|null): 筛选共享或热门订阅的题材标识。; `max_rating` (number|null): 筛选共享或热门订阅的最高评分。; `min_rating` (number|null): 筛选共享或热门订阅的最低评分。; `min_sub` (integer|null): 筛选热门订阅的最低订阅人数。; `page` (integer|null; default `1`): 从 1 开始的结果页码。; `sort_type` (string|null): 共享或热门订阅使用的升序或降序方式。; `stype*` (string): 接口请求的热门订阅分类。
- `body`: none

### `subscription.refresh`
`POST /api/v1/subscribe/refresh`; policy effect: `external_side_effect`.
Purpose: 启动已配置的全系统订阅刷新任务。
Identity scope: `admin`; required role: `system_admin`.
- `path_params`: none
- `query`: `type` (MediaType|null): 仅处理指定媒体类型的订阅；省略时处理全部类型
- `body`: none

### `subscription.reset`
`POST /api/v1/subscribe/reset/{subid}`; policy effect: `reversible_write`.
Purpose: 重置一个可访问订阅，使其可以再次处理。
Identity scope: `bound_user`; required role: `user`.
- `path_params`: `subid*` (integer): 需要改变状态或处理进度的持久化订阅标识。
- `query`: none
- `body`: none

### `subscription.search`
`POST /api/v1/subscribe/search/{subscribe_id}`; policy effect: `external_side_effect`.
Purpose: 立即搜索一个已有订阅。
Identity scope: `bound_user`; required role: `user`.
An explicit search can search and download once for a paused subscription without enabling it.
State `S` stops automatic searches, including tasks already queued or waiting for site recovery;
cancel the execution batch to stop an explicit search. Already submitted downloads remain active.
- `path_params`: `subscribe_id*` (integer): subscription.list 返回的持久化订阅标识。
- `query`: `type` (MediaType|null): 仅处理指定媒体类型的订阅；省略时处理全部类型
- `body`: none

### `subscription.search_all`
`POST /api/v1/subscribe/search`; policy effect: `external_side_effect`.
Purpose: 立即搜索当前用户可访问的全部订阅。
Identity scope: `bound_user`; required role: `user`.
- `path_params`: none
- `query`: `type` (MediaType|null): 仅处理指定媒体类型的订阅；省略时处理全部类型
- `body`: none

### `subscription.share`
`POST /api/v1/subscribe/share`; policy effect: `external_side_effect`.
Purpose: 把一个可访问订阅发布到 MoviePilot 分享服务。
Identity scope: `bound_user`; required role: `user`.
- `path_params`: none
- `query`: none
- `body`: `audio_format` (string|null): 音频格式; `audio_quality` (string|null): 音乐音质等级; `backdrop` (string|null): 背景图; `count` (integer|null; default `0`): 复用人次; `custom_words` (string|null): 自定义识别词; `date` (string|null): 时间; `description` (string|null): 描述; `effect` (string|null): 特效; `episode_group` (string|null): 自定义剧集组; `exclude` (string|null): 排除; `id` (integer|null): 分享ID; `include` (string|null): 包含; `keyword` (string|null): 搜索关键字; `media_category` (string|null): 自定义媒体类别兼容路径快照; `media_category_id` (string|null): 自定义媒体类别稳定标识; `media_id` (string|null): 数据源原生 ID; `media_source` (MediaSource|null): 媒体数据源; `min_bit_depth` (integer|null): 最低位深（bit）; `min_bitrate` (integer|null): 最低码率（bps）; `min_sample_rate` (integer|null): 最低采样率（Hz）; `music_type` (string|null): 音乐实体类型：recording 单曲、album 专辑; `name` (string|null): 订阅名称; `poster` (string|null): 海报; `quality` (string|null): 质量; `resolution` (string|null): 分辨率; `season` (integer|null): 季号; `share_comment` (string|null): 分享说明; `share_title` (string|null): 分享标题; `share_uid` (string|null): 分享人唯一ID; `share_user` (string|null): 分享人; `subscribe_id` (integer|null): 订阅ID; `total_episode` (integer|null; default `0`): 总集数; `total_tracks` (integer|null): 专辑预期总曲目数; `type` (string|null): 订阅类型 电影/电视剧; `vote` (number|null; default `0.0`): 评分; `year` (string|null): 媒体发行年份

### `subscription.share.delete`
`DELETE /api/v1/subscribe/share/{share_id}`; policy effect: `external_side_effect`.
Purpose: 按分享标识删除一条共享订阅发布记录。
Identity scope: `bound_user`; required role: `user`.
- `path_params`: `share_id*` (integer): 分享列表操作返回的持久化 MoviePilot Server 分享标识。
- `query`: none
- `body`: none

### `subscription.share.statistics`
`GET /api/v1/subscribe/share/statistics`; policy effect: `safe_read`.
Purpose: 读取订阅分享者的贡献与复用次数汇总。
Identity scope: `public`; required role: `user`.
- `response`: `data` remains a list; omitting both `page` and `count` keeps the complete legacy result. `collection.result_count` reports the returned items and `collection.total_count` reports the exact pre-pagination total. For counts or summaries, send `page=1,count=1`, read `collection.total_count`, and do not fall back to a database query because the item preview was truncated.
- `path_params`: none
- `query`: `count` (integer|null): 旧版完整列表接口的可选每页条数；提供 page 或 count 即启用分页，此时省略 count 使用 50 条。; `page` (integer|null): 旧版完整列表接口的可选页码，从 1 开始；同时省略 page 和 count 时保留原有完整结果。
- `body`: none

### `subscription.shares`
`GET /api/v1/subscribe/shares`; policy effect: `safe_read`.
Purpose: 按条件和分页读取共享订阅。
Identity scope: `public`; required role: `user`.
- `response`: `data` remains a list and `collection.result_count` reports the returned items. `collection.total_count` is omitted because this endpoint or its upstream source does not expose a total.
- `path_params`: none
- `query`: `count` (integer|null; default `30`): 当前页最多返回的记录数。; `genre_id` (integer|null): 筛选共享或热门订阅的题材标识。; `max_rating` (number|null): 筛选共享或热门订阅的最高评分。; `min_rating` (number|null): 筛选共享或热门订阅的最低评分。; `name` (string|null): 站点、存储项目、订阅或规则组的可读名称。; `page` (integer|null; default `1`): 从 1 开始的结果页码。; `sort_type` (string|null): 共享或热门订阅使用的升序或降序方式。
- `body`: none

### `subscription.status.update`
`PUT /api/v1/subscribe/status/{subid}`; policy effect: `reversible_write`.
Purpose: 把一个可访问订阅设为运行、暂停或停止。
Identity scope: `bound_user`; required role: `user`.
- `path_params`: `subid*` (integer): 需要改变状态或处理进度的持久化订阅标识。
- `query`: `state*` (string): 站点、订阅、市场或整理的当前状态筛选条件。
- `body`: none

### `subscription.update`
`PUT /api/v1/subscribe/`; policy effect: `reversible_write`.
Purpose: 更新一个已有电影、电视剧或音乐订阅。
Identity scope: `bound_user`; required role: `user`.
- Omitted fields keep their stored values. Send JSON `null` to clear `custom_words`, `keyword`, `save_path`, `episode_group`, `downloader`, `min_bitrate`, `min_bit_depth`, `min_sample_rate`, `filter`, `include`, `exclude`, `quality`, `resolution`, `effect`, `audio_quality`, or `audio_format`. Form submissions using `""` for these fields are also accepted as explicit clearing. Nonempty strings are preserved without trimming, and numeric `0` remains a value. Name, type, season, and episode counts retain their existing normalization; submit `media_source` and `media_id` together when changing or clearing media identity.
- `path_params`: none
- `query`: none
- `body`: `audio_format` (string|null): 音频格式正则，如 FLAC|ALAC; `audio_quality` (string|null): 音乐音质等级，可用 | 组合 hires/lossless/lossy; `backdrop` (string|null): 背景图; `best_version` (integer|null): 是否洗版; `best_version_full` (integer|null): 是否只洗全集整包; `classification_policy_revision` (integer|null): 历史执行时分类策略版本，活动订阅中为空; `classification_rule_id` (string|null): 历史命中的分类规则标识，活动订阅中为空; `classification_source` (string|null): 历史最终分类来源，活动订阅中为空; `completed_episode` (integer|null): 已完成集数; `completed_tracks` (integer|null): 已累计下载曲目数，仅由后端根据专辑音轨事实派生; `current_audio_format` (string|null): 当前音乐版本格式; `current_bit_depth` (integer|null): 当前音乐版本位深（bit）; `current_bitrate` (integer|null): 当前音乐版本码率（bps）; `current_priority` (integer|null): 当前优先级; `current_sample_rate` (integer|null): 当前音乐版本采样率（Hz）; `custom_words` (string|null): 自定义识别词; `date` (string|null): 时间; `description` (string|null): 描述; `downloader` (string|null): 下载器; `effect` (string|null): 特效; `episode_group` (string|null): 剧集组; `episode_priority` (object|null): 洗版时已下载剧集的优先级状态; `exclude` (string|null): 排除; `execution_status` (SubscriptionExecutionStatus|null): 当前搜索或下载执行状态，只用于响应投影; `filter` (string|null): 过滤规则; `filter_groups` (array<string>|null): 过滤规则组; `id` (integer|null): 记录标识; `include` (string|null): 包含; `keyword` (string|null): 搜索关键字; `lack_episode` (integer|null; default `0`): 缺失集数; `last_search` (string|null): 最近一次主动搜索开始时间，仅供读取; `last_update` (string|null): 最后更新时间; `media_category` (string|null): 自定义媒体类别兼容路径快照; `media_category_id` (string|null): 自定义媒体类别稳定标识; `media_id` (string|null): 数据源原生 ID; `media_source` (MediaSource|null): 媒体数据源; `min_bit_depth` (integer|null): 最低位深（bit）; `min_bitrate` (integer|null): 最低码率（bps）; `min_sample_rate` (integer|null): 最低采样率（Hz）; `music_type` (string|null): 音乐实体类型：recording 单曲、album 专辑; `name` (string|null): 订阅名称; `note` (array<integer>|null): 附加信息; `poster` (string|null): 海报; `quality` (string|null): 质量; `resolution` (string|null): 分辨率; `save_path` (string|null): 保存路径; `search_imdbid` (integer|null; default `0`): 是否使用 imdbid 搜索; `search_interval` (integer|null): 定时搜索间隔（小时）；空值跟随系统设置; `season` (integer|null): 季号; `sites` (array<integer>|null): 订阅站点; `start_episode` (integer|null; default `0`): 开始集数; `state` (string|null): 状态：N-新建， R-订阅中; `total_episode` (integer|null; default `0`): 总集数; `total_tracks` (integer|null): 专辑预期总曲目数; `type` (string|null): 订阅类型 电影/电视剧; `username` (string|null): 订阅用户; `vote` (number|null; default `0.0`): 评分; `year` (string|null): 媒体发行年份

### `subscription.user.list`
`GET /api/v1/subscribe/user/{username}`; policy effect: `safe_read`.
Purpose: 列出指定可访问 MoviePilot 用户名下的公开订阅。
Identity scope: `bound_user`; required role: `user`.
- `response`: `data` remains a list; omitting both `page` and `count` keeps the complete legacy result. `collection.result_count` reports the returned items and `collection.total_count` reports the exact pre-pagination total. For counts or summaries, send `page=1,count=1`, read `collection.total_count`, and do not fall back to a database query because the item preview was truncated.
- `path_params`: `username*` (string): 所选操作要求的 MoviePilot 或站点用户名。
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
