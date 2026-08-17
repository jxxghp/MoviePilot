# MoviePilot MCP (Model Context Protocol) API 文档

MoviePilot 实现了标准的 **Model Context Protocol (MCP)**，允许 AI 智能体（如 Claude, GPT 等）直接调用 MoviePilot 的功能进行媒体管理、搜索、订阅和下载。

## 1. 基础信息

*   **基础路径**: `/api/v1/mcp`
*   **协议版本**: `2025-11-25, 2025-06-18, 2024-11-05`
*   **传输协议**: HTTP (JSON-RPC 2.0)
*   **认证方式**: 
    *   Header: `X-API-KEY: <你的API_KEY>`
    *   Query: `?apikey=<你的API_KEY>`

### 安全提示

MCP 使用系统配置中的 `API_TOKEN` 作为认证密钥，文档中的 API KEY 是请求字段名。该密钥应按管理员级 secret 保管，持有者可作为受信第三方集成调用暴露的 MoviePilot 工具。

- 优先使用 `X-API-KEY` 请求头；查询参数更容易出现在代理、浏览器或客户端日志中。
- 不要在缺少 HTTPS、访问控制和网络隔离的情况下，将 MCP、OpenAI 或 Anthropic 兼容接口直接暴露到公网。
- MCP 隐藏工具列表只用于减少默认暴露面，不是 per-user 权限系统。

## 2. 标准 MCP 协议 (JSON-RPC 2.0)

### 端点
**POST** `/api/v1/mcp`

### 支持的方法
- `initialize`: 初始化会话，协商协议版本和能力。
- `notifications/initialized`: 客户端确认初始化完成。
- `tools/list`: 获取可用工具列表。
- `tools/call`: 调用特定工具。
- `ping`: 连接存活检测。

### 动态插件工具

`tools/list` 会同时返回 MoviePilot 内置工具和已启用插件通过 `get_agent_tools()` 声明的工具。插件启动、停止、重载或配置生效后，MCP 工具管理器会在下一次列出或调用工具时按注册表版本惰性刷新，避免继续暴露已移除的工具或遗漏新工具。

MCP 当前不会主动发送工具列表变更通知（`listChanged=false`）。如果客户端缓存了工具列表，插件状态变化后需要让客户端重新请求 `tools/list`；无法手动刷新的客户端应重新连接 MCP 服务或新建会话。

---

## 4. 客户端配置示例

### Claude Desktop (Anthropic)

在Claude Desktop的配置文件中添加MoviePilot的MCP服务器配置：

**macOS**: `~/Library/Application Support/Claude/claude_desktop_config.json`  
**Windows**: `%APPDATA%\Claude\claude_desktop_config.json`

使用请求头方式：
```json
{
  "mcpServers": {
    "moviepilot": {
      "url": "http://localhost:3001/api/v1/mcp",
      "headers": {
        "X-API-KEY": "your_api_key_here"
      }
    }
  }
}
```

或使用查询参数方式：
```json
{
  "mcpServers": {
    "moviepilot": {
      "url": "http://localhost:3001/api/v1/mcp?apikey=your_api_key_here"
    }
  }
}
```

## 4.1 Agent 外部 MCP Client 配置

MoviePilot 的内置 Agent 也可以作为 MCP Client 连接外部 MCP 服务器，将外部工具注入到智能助手工具列表中。当前支持：

- `stdio`：按配置的命令和参数启动本地 MCP 进程，通过标准输入输出交换 JSON-RPC 消息。
- `sse`：连接旧版 HTTP+SSE MCP 服务，先读取 `endpoint` 事件，再向返回的 endpoint POST JSON-RPC 消息。
- `http` / `streamable_http`：连接 Streamable HTTP MCP 服务，直接向配置 URL POST JSON-RPC 消息。

这些配置是管理员级 Agent 运行时配置，保存在 `SystemConfigKey.AIAgentMcpServers` 中。外部 MCP 工具默认要求管理员上下文调用，避免普通用户触发高权限外部工具。

### Agent MCP 配置接口

这些接口使用登录态鉴权，并要求当前用户为超级管理员。

| 方法 | 路径 | 说明 |
| :--- | :--- | :--- |
| GET | `/api/v1/message/agent/mcp/servers` | 查询已配置的外部 MCP 服务器列表 |
| POST | `/api/v1/message/agent/mcp/servers` | 保存外部 MCP 服务器列表 |
| POST | `/api/v1/message/agent/mcp/servers/test` | 测试单个外部 MCP 服务器，返回发现的工具列表 |

## 5. 错误码说明

| 错误码 | 消息 | 说明 |
| :--- | :--- | :--- |
| -32700 | Parse error | JSON 格式错误 |
| -32600 | Invalid Request | 无效的 JSON-RPC 请求 |
| -32601 | Method not found | 方法不存在 |
| -32602 | Invalid params | 参数验证失败 |
| -32002 | Session not found | 会话不存在或已过期 |
| -32003 | Not initialized | 会话未完成初始化流程 |
| -32603 | Internal error | 服务器内部错误 |

## 6. RESTful API
所有工具相关的API端点都在 `/api/v1/mcp` 路径下（保持向后兼容）。

### 相关 REST 端点

MoviePilot 也提供普通 REST API 给前端和自动化客户端使用。所有接口同样需要 API KEY 认证，在请求头中添加 `X-API-KEY: <api_key>` 或在查询参数中添加 `apikey=<api_key>`。

#### REST API 版本

- 普通 JSON REST 接口统一使用 `/api/v1`，不再提供 `/api/v2` 套壳版本。
- 成功和失败响应都只包含 `success`、`message`、`data` 三个顶层字段；各接口只有 `data` 的模型可以变化。
- 成功响应为 `{"success": true, "message": "", "data": <接口数据>}`。HTTP 错误保留原状态码，返回 `{"success": false, "message": <错误原因>, "data": null}`；请求参数校验错误会在 `data` 中附带结构化错误列表。
- 查询接口未命中但请求已正常完成时仍返回 `success=true`，存在性等业务状态通过 `data` 表达。例如 `/mediaserver/exists` 未命中时返回空的 `data.item`。
- 每个普通 JSON 端点都会在 OpenAPI 中声明具体的 `Response[DataModel]`，调用方可从 `/docs` 或 `/api/v1/openapi.json` 查询数据结构。
- SSE、文件、图片、HTML、空响应，以及 OAuth2 登录、OpenAI、Anthropic、MCP JSON-RPC 等标准协议端点保持协议原生响应体；它们会在 OpenAPI 中显式声明对应的流、文件或协议模型。
- 插件通过 `get_api()` 动态注册的 `/api/v1/plugin/...` 端点不属于主程序统一响应信封范围。插件自行声明响应模型、状态码和返回体，宿主只补充路径与鉴权依赖。

客户端可发送 `X-MoviePilot-Locale: zh-CN|zh-TW|en-US` 或 `Accept-Language`。后端会按当前请求语言直接翻译顶层 `message`；未提供语言头时使用简体中文，翻译缺失时回退原文本。SSE 和业务数据中原有的 `text_i18n`、`error_i18n` 等展示字段继续保留。

`GET /api/v1/login/wallpaper` 会将壁纸 URL 放在 `data` 字段中。`POST /api/v1/user/avatar/{user_id}` 会以 `data.filename` 返回原始文件名。上述接口的 `message` 均不承载业务数据。

FastAPI 的 HTTP 异常和参数校验异常统一使用 `message`，不再返回顶层 `detail` / `detail_i18n`。

交互式接口文档 `/docs` 读取 `/api/v1/openapi.json`，页面版本号直接使用 `version.py` 中的后端 `APP_VERSION`。

#### 媒体识别 / 整理

媒体识别、搜索和手动整理统一使用 `media_source` + `media_id` 表示媒体主身份。内置来源通过 `MediaSource` 提供 `themoviedb`、`douban`、`bangumi`、`anilist`、`imdb`、`tvdb`、`musicbrainz`、`theaudiodb`、`doubanmusic`、`bilibili`、`mangguodiscover`、`migu` 和 `tencentvideodiscover` 等常量；该列表不是插件来源白名单，插件可以注册符合 OpenAPI 格式约束的稳定扩展标识。`media_id` 是该来源的原生 ID，不添加 `tmdb:` 等前缀。需要精确身份时两个字段必须同时提供，不能只传其中一个。

影视自动识别在未指定来源时只使用 TMDB，未命中时不会继续查询其它影视源。音乐路径识别严格按 AcoustID 音频指纹、文件标签、文件名三级依次执行；指纹或标签直接提供 MusicBrainz Recording ID 时，会直接查询 MusicBrainz 详情，标签和文件名标题识别也只使用 MusicBrainz。其它元数据源仅在手动操作通过请求级 `media_source`，或通过完整的 `media_source` + `media_id` 精确指定时使用，不修改系统默认值，也不会跨来源兜底。`MediaInfo` 响应仍可能包含 `tmdb_id`、`douban_id`、`bangumi_id`、`anilist_id` 等跨源映射辅助字段，但这些字段不是通用请求入口。明确归属 `/tmdb`、`/douban`、`/bangumi`、`/anilist` 的接口，以及固定使用 TMDB 的剧集组和排期接口，仍可按其单数据源契约接收原生 ID。

| 方法 | 路径 | 说明 |
| :--- | :--- | :--- |
| GET | `/api/v1/media/search` | 按标题搜索媒体、合集、人物或音乐，参数：`title`、`type`、`page`、`count`，可重复传入可选 `media_source`；内置模块只处理自身支持的来源，插件模块可以处理其注册的扩展来源，旧客户端的逗号格式仅在输入边界兼容 |
| GET | `/api/v1/media/recognize` | 识别标题，参数：`title`、`subtitle`、`custom_words`，可选 `media_source`；当 `title` 为含目录的媒体文件路径时，会合并父目录中的名称、年份等信息 |
| GET | `/api/v1/media/recognize_file` | 识别文件路径，参数：`path`，可选 `media_source` |
| GET | `/api/v1/media/{media_id}` | 按原生 ID 查询媒体详情；必填参数：`media_source`、`type_name`，其中 `media_source` 与路径中的 `media_id` 组成统一媒体身份 |
| POST | `/api/v1/media/scrape/{storage}` | 刮削媒体元数据；请求体为 `FileItem`，可选查询参数 `media_source`、`media_id`、`type_name`（电影/电视剧/音乐）。音乐会按策略处理音频标签、封面和歌词 |
| POST | `/api/v1/transfer/manual/target-path` | 按源文件与目录配置匹配手动整理目标路径；请求体为 `ManualTransferItem`，该接口不执行媒体识别 |
| POST | `/api/v1/transfer/manual/history` | 查询文件、批量文件或目录命中的成功整理历史摘要，用于进入手动整理界面时显示重新整理状态 |
| POST | `/api/v1/transfer/manual` | 手动整理；请求体可用 `media_source` + `media_id` 指定本次识别与刮削数据源；命中失败历史时自动清理旧目标和记录后重试，`reorganize=true` 时清理命中的成功历史和非移动模式旧目标后重新整理 |

#### 站点

| 方法 | 路径 | 说明 |
| :--- | :--- | :--- |
| GET | `/api/v1/site/media/{media_type}` | 按媒体类型查询已配置且启用的可搜索站点；`media_type` 支持 `movie`、`tv`、`music` 或对应中文类型，音乐仅返回明确声明音乐能力的站点，影视不返回纯音乐站点 |

#### 搜索 / 种子 / 字幕

| 方法 | 路径 | 说明 |
| :--- | :--- | :--- |
| GET | `/api/v1/search/media/{media_id}` | 按统一媒体身份搜索站点种子资源；必填参数：`media_source`，其它参数：`mtype`、`area`、`season`、`sites`、`music_type` |
| GET | `/api/v1/search/media/{media_id}/stream` | 按统一媒体身份渐进式搜索站点种子资源，返回 SSE，参数同上 |
| GET | `/api/v1/search/title` | 按关键字模糊搜索站点种子资源，参数：`keyword`、`page`、`sites`，可选 `mtype=音乐` 仅搜索音乐分类 |
| GET | `/api/v1/search/title/stream` | 按关键字渐进式搜索站点种子资源，返回 SSE，参数：`keyword`、`page`、`sites`，可选 `mtype=音乐` |
| GET | `/api/v1/search/subtitle/title` | 按关键字搜索站点字幕资源，参数：`keyword`、`page`、`sites` |
| GET | `/api/v1/search/subtitle/title/stream` | 按关键字渐进式搜索站点字幕资源，返回 SSE，参数：`keyword`、`page`、`sites` |
| GET | `/api/v1/search/subtitle/media/{media_id}` | 按统一媒体身份精确搜索站点字幕资源；必填参数：`media_source`，其它参数：`mtype`、`season`、`episode`、`sites` |
| GET | `/api/v1/search/subtitle/media/{media_id}/stream` | 按统一媒体身份渐进式精确搜索站点字幕资源，返回 SSE，参数同上 |
| GET | `/api/v1/search/last` | 获取上一次种子搜索结果 |
| GET | `/api/v1/search/last/context` | 获取上一次搜索结果及可复用搜索参数，`params.result_type` 为 `torrent` 或 `subtitle` |
| POST | `/api/v1/search/recommend` | 获取 AI 推荐资源，请求体：`filtered_indices`、`check_only`、`force` |

渐进式搜索在无业务事件时每 15 秒发送 `{"type":"heartbeat"}`，客户端应将其仅用于连接保活。超过 48 条的最终 `replace` 会分批发送：首批 `type=replace`，后续批次 `type=append`，所有批次均带 `replace_batch=true`、从 0 开始的 `batch_index`、`batch_count` 和最终 `total_items`；客户端必须按顺序收齐后再原子替换结果。最终 `done` 在已发送 `replace` 后不重复携带 `items`。

#### AniList 榜单 / 探索

AniList 榜单、探索、详情、人物和推荐接口优先通过 `anilist-chinese` 代理查询。代理不可用时自动回退 AniList 官方 GraphQL，并合并 `anilist-chinese` 每日数据集；媒体标题优先使用项目提供的中文标题，未提供中文标题时回退 AniList 原语言标题。

| 方法 | 路径 | 说明 |
| :--- | :--- | :--- |
| GET | `/api/v1/anilist/trending` | 查询 TRENDING NOW 榜单，参数：`page`、`count` |
| GET | `/api/v1/anilist/popular-this-season` | 查询 POPULAR THIS SEASON 榜单，参数：`page`、`count` |
| GET | `/api/v1/anilist/discover` | 组合探索动画，参数：`search`、`genre`、`format`、`season`、`season_year`、`status`、`country`、`sort`、`page`、`count` |
| GET | `/api/v1/anilist/{anilist_id}` | 查询动画详情 |
| GET | `/api/v1/anilist/credits/{anilist_id}` | 查询日语配音演员，参数：`page`、`count` |
| GET | `/api/v1/anilist/recommend/{anilist_id}` | 查询相关推荐，参数：`page`、`count` |
| GET | `/api/v1/anilist/person/{person_id}` | 查询人物详情 |
| GET | `/api/v1/anilist/person/credits/{person_id}` | 查询人物参与的动画作品，参数：`page`、`count` |

#### 音乐元数据 / 推荐 / 探索

音乐元数据使用 `MusicMeta` / `MusicInfo` 独立模型。`music_type=recording` 表示单曲，`album` 表示包含多首曲目的完整专辑，`artist` 仅用于浏览；稳定身份分别使用对应的 `musicbrainz:<mbid>`。单曲和专辑可进入搜索、订阅、下载、整理、刮削和已配置音乐媒体服务器的入库检查，艺术家不能作为订阅或下载目标。

音乐识别结果同时提供 `audio_format`、`audio_lossless`、`audio_quality`、`bit_depth`、`sample_rate`、`bitrate`、`audio_specs` 和 `audio_quality_score`。本地文件识别读取实际音频流参数，并使用 Chromaprint 的 `fpcalc` 在本地生成指纹后查询 AcoustID；音频文件本身不会上传。站点资源识别从标题和描述提取声明参数；码率、采样率的存储单位分别为 bps 和 Hz。

| 方法 | 路径 | 说明 |
| :--- | :--- | :--- |
| GET | `/api/v1/media/search` | 当 `type=music` 或指定音乐 `media_source` 时按歌曲、专辑或歌手关键词搜索音乐元数据，参数：`title`、`type`、`count`、可重复的 `media_source` 枚举 |
| POST | `/api/v1/music/recognize` | 按 `media_source` + `media_id` 识别音乐详情，请求体：`MusicRecognizeRequest` |
| GET | `/api/v1/music/explore` | 按来源浏览音乐；`media_source=musicbrainz` 支持 `mode=chart|fresh` 榜单与新发行，`media_source=doubanmusic` 固定按官方标签分类浏览，使用 `tags` 和 `douban_sort=U|S|R|O` 筛选。其它参数：`entity=recording|album`、`range_name`、`sort_by`、`sort`、`days`、`past`、`future`、`min_listen_count`、`with_cover`、`page`、`count` |
| GET | `/api/v1/music/album/{album_id}` | 按来源专辑 ID 查询专辑详情、完整曲目和发行版本，参数：`media_source` |
| GET | `/api/v1/music/album/{album_id}/related` | 按来源查询关联专辑，参数：`media_source`、`count` |
| GET | `/api/v1/music/artist/{artist_id}` | 查询艺术家详情；艺术家为只读浏览实体，参数：`media_source` |
| GET | `/api/v1/music/artist/{artist_id}/albums` | 分页查询艺术家的专辑、EP 和单曲，参数：`media_source`、`page`、`count`、`album_type` |
| GET | `/api/v1/music/artist/{artist_id}/related` | 查询关联艺术家，参数：`media_source`、`count` |
| GET | `/api/v1/recommend/music_weekly` | 浏览本周热门音乐，参数：`page`、`count` |
| GET | `/api/v1/recommend/music_douban` | 浏览豆瓣音乐新碟榜，参数：`page`、`count` |

专辑下载与订阅按“整包”处理：下载层会读取种子文件清单并以专辑 `total_tracks` 校验独立音频文件数量；未确认完整覆盖时不会把专辑订阅销订，也不会把部分曲目报告为完整专辑已入库。音乐刮削遵循 `music` 的标签、封面和歌词策略，歌词通过带有界 TTL/LRU 缓存的 LRCLIB 模块保存为同名 `.lrc` 或 `.txt` 旁挂文件。

音乐订阅可使用 `audio_quality=hires|lossless|lossy`（支持正则组合）、`audio_format`、`min_bitrate`、`min_bit_depth`、`min_sample_rate` 过滤资源。`best_version=1` 开启音质洗版，系统按格式、无损属性、位深、采样率和码率换算 0-100 优先级，只下载高于 `current_priority` 的候选；DSD 或 24-bit/192 kHz 无损资源达到终态 100。内置规则 `HIRES`、`LOSSLESS`、`FLAC`、`ALAC`、`APE`、`WAV`、`DSD`、`MP3`、`AAC`、`OPUS`、`BITRATE320`、`BITRATE256`、`BITRATE192` 可用于自定义过滤规则组。

#### 下载

| 方法 | 路径 | 说明 |
| :--- | :--- | :--- |
| GET | `/api/v1/download/` | 查询正在下载的任务，参数：`name`；关联下载历史时返回媒体类型、来源站点 `site_name`，以及 `media.poster` 海报和 `media.backdrop` 背景图；兼容字段 `media.image` 与 `media.poster` 相同 |
| POST | `/api/v1/download/` | 添加含媒体信息的下载任务，请求体包含媒体信息和种子信息 |
| POST | `/api/v1/download/add` | 添加不含媒体信息的下载任务，请求体包含 `torrent_in`，可选且必须成对提供 `media_source` + `media_id`，并支持 `music_type`、`downloader`、`save_path` |
| POST | `/api/v1/download/subtitle` | 下载字幕到识别出的媒体下载目录，请求体包含 `subtitle_in`，并必须提供 `media_source` + `media_id`；可选 `save_path` |
| GET | `/api/v1/download/start/{hashString}` | 恢复下载任务，参数：`name` |
| GET | `/api/v1/download/stop/{hashString}` | 暂停下载任务，参数：`name` |
| GET | `/api/v1/download/clients` | 查询可用下载器 |
| GET | `/api/v1/download/paths` | 查询可用于下载接口 `save_path` 参数的下载路径 |
| DELETE | `/api/v1/download/{hashString}` | 删除下载任务，参数：`name` |

#### 历史

| 方法 | 路径 | 说明 |
| :--- | :--- | :--- |
| GET | `/api/v1/history/download` | 按下载时间倒序查询下载历史，参数：`page`、`count`；`poster` 为海报，兼容字段 `image` 为背景图 |
| DELETE | `/api/v1/history/download` | 删除下载历史，请求体为下载历史记录 |

#### 系统

| 方法 | 路径 | 说明 |
| :--- | :--- | :--- |
| GET | `/api/v1/system/ping` | 登录用户服务存活检测，用于前端重启后轮询恢复状态 |
| GET | `/api/v1/dashboard/system` | 查询仪表板系统摘要，包括主机名称、操作系统、MoviePilot 运行时间和后端版本 |
| GET | `/api/v1/dashboard/schedule` | 查询所有后台定时服务，包含当前完成百分比、进度文本和执行状态 |
| GET | `/api/v1/dashboard/schedule/{job_id}/progress` | 查询指定后台定时服务的实时进度详情 |
| GET | `/api/v1/dashboard/schedule2/{job_id}/progress` | 使用 API_TOKEN 查询指定后台定时服务的实时进度详情 |
| GET | `/api/v1/system/setting/public/{key}` | 登录用户读取白名单内非敏感系统设置，仅支持目录、存储、站点范围、默认订阅规则、Follow 订阅者和插件市场地址等前端必需配置 |
| POST | `/api/v1/system/setting/PLUGIN_MARKET/sync-wiki` | 管理员从 MoviePilot Wiki 的插件文档同步公开插件仓库清单，和本地 `PLUGIN_MARKET` 合并去重后写入配置 |
| GET | `/api/v1/system/modulelist` | 查询已加载模块，保留 `name` 原始中文字段，并提供 `name_i18n` 和 `name_key` 给多语言前端展示 |
| GET | `/api/v1/system/moduletest/{moduleid}` | 测试指定模块可用性，标准响应的 `message` 会按请求语言直接返回翻译文本 |
| GET | `/api/v1/message/agent/mcp/servers` | 管理员查询 Agent 外部 MCP 服务器配置 |
| POST | `/api/v1/message/agent/mcp/servers` | 管理员保存 Agent 外部 MCP 服务器配置 |
| POST | `/api/v1/message/agent/mcp/servers/test` | 管理员测试单个 Agent 外部 MCP 服务器并读取工具列表 |

#### 缓存管理

以下接口使用登录态鉴权，并要求当前用户为超级管理员。

| 方法 | 路径 | 说明 |
| :--- | :--- | :--- |
| GET | `/api/v1/tmdb/cache` | 查询 TheMovieDb 识别缓存统计、共享识别累计成功命中次数及开关状态 |
| DELETE | `/api/v1/tmdb/cache/{cache_key}` | 按缓存键删除单条 TheMovieDb 识别缓存，缓存键需要进行 URL 编码 |
| DELETE | `/api/v1/tmdb/cache` | 清空全部 TheMovieDb 识别缓存 |
| GET | `/api/v1/music/cache` | 查询 MusicBrainz 音乐识别缓存统计及条目列表 |
| DELETE | `/api/v1/music/cache/{cache_key}` | 按缓存键删除单条音乐识别缓存，缓存键需要进行 URL 编码 |
| DELETE | `/api/v1/music/cache` | 清空全部音乐识别缓存 |

TMDB 缓存查询响应的 `data` 包含 `count`、`recognized`、`unrecognized`、`data`，以及共享识别统计字段
`shared_recognized` 和开关字段 `shared_recognize_enabled`。共享命中次数仅在共享结果驱动的二次媒体识别成功后累计。

音乐识别缓存查询响应的 `data` 包含 `count`、`recognized`、`unrecognized` 和 `data`；条目字段包括缓存键、`media_id`、`title`、`artists`、`album`、`year`、`music_type` 和 `cover_url`。未携带远端身份的兜底负缓存仅保留在内存，不参与持久化。

### 插件补充接口

**GET** `/api/v1/plugin/history/{plugin_id}`

按需读取指定已安装插件的最新远端更新说明。该接口用于前端在用户点击“查看更新说明”时再实时访问插件仓库，避免加载已安装插件列表时批量请求网络。

**GET** `/api/v1/plugin/rating?plugin_ids={plugin_id,...}`

批量查询插件平均分、评分人数和当前安装实例评分。`plugin_ids` 省略时查询中心端已有的全部插件评分。

**GET** `/api/v1/plugin/rating/{plugin_id}`

查询单个插件平均分、评分人数和当前安装实例评分。中心端暂不可用时返回该插件的零评分结果。

**POST** `/api/v1/plugin/rating/{plugin_id}`

为已安装插件提交当前安装实例评分，请求体为 `{"rating": 4.5}`。评分范围为 `0.1` 至 `5.0`，精确到 `0.1`；同一安装实例再次提交会更新原评分。

### 1. 列出所有工具

**GET** `/api/v1/mcp/tools`

获取所有可用的MCP工具列表。

内置工具的 `inputSchema` 只包含实际执行业务所需的参数，不包含用于解释调用原因的通用 `explanation` 参数，以减少 Agent 上下文消耗。插件工具的参数结构由插件自身声明。

内置 Agent 的本地文件与命令工具 `read_file`、`write_file`、`edit_file`、
`apply_patch`、`execute_command` 不通过 MCP 暴露。这些工具在 Agent 运行时执行独立的
用户权限与路径边界检查；MCP 隐藏列表只负责收敛接口暴露面，不替代权限控制。
其中 `read_file` 单次最多返回 50KB 文件内容；超出时会截断并提示 Agent 使用
`start_line`、`end_line` 指定更小的行号范围继续读取。

媒体相关 MCP 工具以 `media_source` + 来源原生 `media_id` 传递精确身份；内置来源使用 `MediaSource` 常量，插件来源使用注册的稳定扩展标识。`query_media_detail`、`search_torrents`、`query_library_exists` 必须提供完整字段对；`add_subscribe`、`transfer_file`、`scrape_metadata` 在显式指定身份时也必须成对提供。`search_media` 和 `recognize_media` 是按标题或路径发现身份的入口，其结果中的字段对可直接用于后续工具。音乐调用还使用 `media_type=music` 与 `music_type=recording|album|artist`；其中艺术家只允许搜索和详情浏览。工具响应中的专用 ID 仅是跨源映射辅助输出，不应再作为上述通用工具的输入。TMDB 专用的 `query_episode_schedule` 仍使用 `tmdb_id`，因为它直接调用单一 TMDB 剧集接口。

Agent 音乐流程与影视共用同一采集管线，但实体边界不同：单曲通过 `music_type=recording` 按一个文件处理；专辑通过 `music_type=album` 类似电视剧整季包，按一个目录/资源处理并校验总曲目数；艺术家不是采集目标。`add_subscribe` / `update_subscribe` 支持音乐音质筛选字段和 `best_version` 音质洗版；`query_subscribes` 会返回筛选条件及当前音质快照。`scrape_metadata(media_type="music")` 会按策略写音频标签、封面和歌词，并返回歌词新增、已存在、未匹配和失败数量。

`get_search_results` 可使用 `title_pattern` 对种子标题执行正则筛选，也可使用 `content_pattern` 联合匹配种子标题、简介和标签。`title_pattern` 保持仅匹配标题的兼容语义；需要在结果中查看种子简介时，传入 `include_description=true`；需要查看种子标签时，传入 `include_labels=true`。两种正则参数与站点、分辨率等结构化筛选条件同时传入时按 AND 关系组合。

#### Agent 自主定时任务工具

以下工具用于管理会在指定时间重新唤醒 Agent 的持久化任务，均为管理员级工具：

| 工具 | 说明 |
| :--- | :--- |
| `create_agent_task` | 创建单次或周期任务，并保存任务内容及当前用户、会话上下文 |
| `query_agent_tasks` | 查询任务配置、启用状态、下次执行时间及最近执行结果 |
| `update_agent_task` | 修改任务内容或触发器，也可通过 `enabled` 暂停、恢复任务 |
| `run_agent_task` | 使用整数 `task_id` 将当前用户已启用的任务提交为立即执行 |
| `delete_agent_task` | 永久删除任务并立即移除运行时调度 |

`trigger_type=date` 表示单次执行：“30 分钟后检查”这类相对时间传 `delay_minutes=30`，由后端计算精确时间；固定时间则传 ISO 8601 `trigger`，支持精确到秒。`trigger_type=cron` 使用标准五段 cron（分、时、日、月、周），适合周期检查。未显式携带时区的时间按 MoviePilot 的 `TZ` 配置解释。任务由内存调度器精确触发，配置持久化到数据库，服务重启后会自动恢复；触发后 Agent 在原会话中执行 `content`，执行过程及最终结果均不绑定创建任务时的消息渠道，而是通过 MoviePilot 已配置的通知渠道广播。如果 Agent 在执行过程中已通过消息工具发送完整结果，任务结束时不会再次发送相同的最终回复。

服务重启时仍处于运行中的任务会显示为 `interrupted`，表示上次结果未知且可能已有部分操作。中断的一次任务不会自动补跑，暂停后恢复也仍保留中断状态；需要先核对实际结果，再用 `run_agent_task` 明确立即重跑，或通过 `update_agent_task` 提供新的 `trigger_type` 与未来触发时间重新安排。

Agent 自主任务工具使用数据库中的整数 `task_id`。`query_schedulers` 与 `run_scheduler` 仅面向系统、插件和工作流注册的运行时定时服务，使用字符串 `job_id`，不会返回或执行 `agent-task-*`。两类 ID 不可混用；需要立即执行自主任务时，应先通过 `query_agent_tasks` 确认归属和状态，再调用 `run_agent_task`。立即执行只提交任务，不在当前工具调用内等待结果，从而避免同一 Agent 会话互相等待；执行结果仍按上述通知规则广播。

上述过滤只约束 Agent 工具，避免模型混用两类任务。前端系统设置和仪表盘使用的 `/api/v1/dashboard/schedule` 仍返回完整运行时列表，其中包含 `provider=[Agent]` 的自主任务；前端通过 `/api/v1/system/runscheduler` 立即执行这类列表项的行为也保持不变。

创建单次任务的参数示例：

```json
{
  "tool_name": "create_agent_task",
  "arguments": {
    "name": "检查电影资源",
    "content": "搜索电影《示例电影》是否已有可下载资源，并报告站点、版本和大小；不要自动下载。",
    "trigger_type": "date",
    "delay_minutes": 30
  }
}
```

创建每天 20:30 执行的周期任务时，使用 `trigger_type=cron` 和 `trigger="30 20 * * *"`。

**认证**: 需要API KEY，在请求头中添加 `X-API-KEY: <api_key>` 或在查询参数中添加 `apikey=<api_key>`

**响应示例**:
```json
{
  "success": true,
  "message": "",
  "data": [
    {
      "name": "add_subscribe",
      "description": "Add media subscription to create automated download rules...",
      "inputSchema": {
        "type": "object",
        "properties": {
          "title": {
            "type": "string",
            "description": "The title of the media to subscribe to"
          }
        },
        "required": ["title", "media_type"]
      }
    }
  ]
}
```

#### 系统诊断工具

`query_doctor_report` 以只读方式返回 MoviePilot Doctor 诊断报告，可通过 `deep` 启用深度检查，并通过 `include_details` 控制是否返回完整详情。每条诊断项的 `affects_report_status` 表示其是否参与整体状态聚合；插件日志异常会保留为 `warn/degraded` 线索，但该字段为 `false`，不会单独把系统整体状态降为 `degraded`。

### 2. 调用工具

**POST** `/api/v1/mcp/tools/call`

调用指定的MCP工具。

**认证**: 需要API KEY，在请求头中添加 `X-API-KEY: <api_key>` 或在查询参数中添加 `apikey=<api_key>`

**请求体**:
```json
{
  "tool_name": "add_subscribe",
  "arguments": {
    "title": "流浪地球",
    "year": "2019",
    "media_type": "movie"
  }
}
```

**响应示例**:
```json
{
  "success": true,
  "message": "",
  "data": {
    "result": "成功添加订阅：流浪地球 (2019)"
  }
}
```

**错误响应示例**:
```json
{
  "success": false,
  "message": "调用工具失败: 参数验证失败",
  "data": null
}
```

### 3. 获取工具详情

**GET** `/api/v1/mcp/tools/{tool_name}`

获取指定工具的详细信息。

**认证**: 需要API KEY，在请求头中添加 `X-API-KEY: <api_key>` 或在查询参数中添加 `apikey=<api_key>`

**路径参数**:
- `tool_name`: 工具名称

**响应示例**:
```json
{
  "success": true,
  "message": "",
  "data": {
    "name": "add_subscribe",
    "description": "Add media subscription to create automated download rules...",
    "inputSchema": {
      "type": "object",
      "properties": {
        "title": {
          "type": "string",
          "description": "The title of the media to subscribe to"
        }
      },
      "required": ["title", "media_type"]
    }
  }
}
```

### 4. 获取工具参数Schema

**GET** `/api/v1/mcp/tools/{tool_name}/schema`

获取指定工具的参数Schema（JSON Schema格式）。

**认证**: 需要API KEY，在请求头中添加 `X-API-KEY: <api_key>` 或在查询参数中添加 `apikey=<api_key>`

**路径参数**:
- `tool_name`: 工具名称

**响应示例**:
```json
{
  "success": true,
  "message": "",
  "data": {
    "type": "object",
    "properties": {
      "title": {
        "type": "string",
        "description": "The title of the media to subscribe to"
      },
      "year": {
        "type": "string",
        "description": "Release year of the media"
      }
    },
    "required": ["title", "year", "media_type"]
  }
}
```
