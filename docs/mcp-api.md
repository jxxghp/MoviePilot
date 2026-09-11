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

`POST /api/v1/history/transfer/{history_id}/discard-corrupt` 属于需要管理权限的整理恢复 REST 接口，不向 Agent gateway 暴露。成功响应的 `data.history_id` 为保留的整理历史 ID；任务已清理时同样返回该结构，历史不存在时返回业务失败。

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

内置 Agent 的 `update_plan`、`search_tools`、`read_tool_result`、`get_tool_execution` 为会话中间件工具，不通过外部 `tools/list` 发布；它们维护计划、发现工具、续读结果或查询执行回执，不能授予业务操作权限。使用与恢复语义见 [Agent 复杂任务执行与恢复](agent.md)。

`tools/list` 会同时返回 MoviePilot 内置工具和已启用插件通过 `get_agent_tools()` 声明的工具。插件启动、停止、重载或配置生效后，MCP 工具管理器会在下一次列出或调用工具时按注册表版本惰性刷新，避免继续暴露已移除的工具或遗漏新工具。

MCP 当前不会主动发送工具列表变更通知（`listChanged=false`）。如果客户端缓存了工具列表，插件状态变化后需要让客户端重新请求 `tools/list`；无法手动刷新的客户端应重新连接 MCP 服务或新建会话。

## 3.1 结构化 Agent 工具与完整参数合同

`tools/list` 会为以下四个正式入口返回可直接校验的 JSON Schema。每个入口都按 operation/action 生成 `oneOf` 分支，分支中包含必填字段、类型、默认值、枚举、嵌套对象和互斥/至少一项等跨字段规则；外部 MCP 客户端可以在一次 `tools/call` 中完成参数构造，不需要猜测 URL、HTTP 方法或第三方 SDK 参数。

| MCP 工具 | 用途 | 参数合同来源 |
| :--- | :--- | :--- |
| `moviepilot_api` | MoviePilot 产品业务 API：媒体、搜索、订阅、下载、整理、站点、存储、调度、工作流、插件、过滤规则和系统配置 | `skills/moviepilot-api/SKILL.md` 及其 `api/<category>.md` 独立分类合同；运行时 schema 为 `app/agent/policy/resources/api_mcp_schema.json` |
| `downloader_operation` | qBittorrent、Transmission、rTorrent 原生任务、队列、文件、限速、标签和会话操作 | `skills/downloader-operation/SKILL.md` 与 `skills/downloader-operation/scripts/mp-downloader.py` 的 `ACTIONS` |
| `mediaserver_operation` | Emby、Jellyfin、Plex、ZSpace、UGREEN、TrimeMedia、Navidrome、MediaVault 原生媒体库、搜索、播放、扫描和刷新操作 | `skills/mediaserver-operation/SKILL.md` 与 `skills/mediaserver-operation/scripts/mp-mediaserver.py` 的 `ACTIONS` |
| `database_operation` | MoviePilot 配置数据库表清单、实时 schema、只读 SQL 和明确授权写入 | `skills/database-operation/SKILL.md` 与 `skills/database-operation/scripts/mp-db.py` 的 `ACTIONS` |

这四个工具都要求管理员级 MCP 集成身份；`tools/list` 的可见性不等于绕过业务权限或写操作确认。下载器和媒体服务器工具会在一次调用内自动选择默认/唯一实例；实例不明确时，错误结果会列出可复用的精确实例名。数据库工具不接受任意连接串或凭据，脚本从 MoviePilot 运行时配置读取数据库连接。

`app/agent/policy/resources/api_mcp_schema.json` 是 `moviepilot_api` 的生成制品，不是设置项或 API 参数的手工事实源。`scripts/generate_agent_api_mcp_schema.py` 从当前 FastAPI OpenAPI、固定 operation 路由和 Agent 专用英文参数说明生成该文件；运行时直接读取它响应外部 MCP `tools/list`，测试会校验生成结果没有漂移。修改 API、请求模型或 operation 后应重新生成并提交该文件，不应直接编辑 JSON。

当前完整 FastAPI OpenAPI 包含 400 个 HTTP 操作，其中 220 个稳定业务操作进入
`moviepilot_api`，使用 218 个固定路由模板：217 条 OpenAPI 路由直接匹配，另有 1 条只允许
`tmdb`、`douban`、`bangumi`、`anilist` 四个来源的受限人物作品动态路由。每个 operation
均同时具备固定 method/path、角色权限、副作用等级、确认与恢复策略、结果敏感性、英文用途说明，
以及可直接提交的 path/query/body JSON Schema；Skill front matter、正文 operation 章节、运行时
注册表和 MCP `tools/list` 的 220 个 `oneOf` 分支必须完全一致。

数量不相等是明确的安全与语义边界，而不是漏生成。当前 400 条路由均被审计并锁定为以下一种
归属，审计生成器不再提供“未归类”兜底：

| 归属 | 数量 | Agent 使用方式 |
| :--- | ---: | :--- |
| `gateway` | 217 | 通过 `moviepilot_api` 的稳定 operation 和精确参数合同调用 |
| `consolidated` | 71 | 通过同领域聚合 operation 调用，不复制数据源或前端专用路由 |
| `provider-skill` | 12 | 通过下载器或媒体服务器 Skill 调用第三方 provider API |
| `alternate-auth-duplicate` | 11 | 使用对应 bearer-authenticated gateway operation，不暴露 API_TOKEN 兼容副本 |
| `transport_or_identity` | 66 | 由登录、令牌、MCP、会话、回调、健康检查等宿主传输/身份边界拥有 |
| `stream_or_binary` | 10 | 由直接客户端处理流式日志、消息、文件、图片等非结构化响应 |
| `ui_presentation` | 13 | 由前端或插件渲染面拥有，不作为业务 Agent operation |

逐路由归属见 `docs/refactor/agent-api-surface-audit.md`，并由
`tests/test_agent_api_surface_audit.py` 对当前 OpenAPI、固定注册表、MCP schema、英文 Skill
合同及全部非网关归属做漂移检查。任何新增端点必须先明确归属；对 Agent 开放时还必须补齐
operation ID、权限、副作用、确认、恢复、结果敏感性及精确参数说明。

### `moviepilot_api` 调用形状

```json
{
  "name": "moviepilot_api",
  "arguments": {
    "operation_id": "subscription.list",
    "path_params": {},
    "query": {"page": 1, "count": 20},
    "body": {}
  }
}
```

只允许传 `tools/list` 对应 operation 分支中声明的 `path_params`、`query` 和 `body` 字段。不得传 URL、认证头、API Token 或任意 HTTP 方法。

查询结果的兼容分页合同如下：

- 原先返回完整列表、没有分页参数的接口会在端点签名、OpenAPI、Skill 和 MCP `oneOf` 中显式声明可选 `page` / `count`；`page` 必须不小于 1，`count` 范围为 1 到 200。两者都省略时仍返回原来的完整列表，不启用分页；显式传入任一参数时才分页，缺失的 `page` 按 1、缺失的 `count` 按 50 处理。FastAPI 的 `response` 注入对象不是业务输入，不会出现在 REST、Skill 或 MCP 参数中。
- 数据库列表在查询层先应用授权范围和业务筛选，再执行稳定排序、`LIMIT/OFFSET` 和同条件精确 `COUNT`；不得先全表加载、响应后切片。纯内存、配置、缓存、文件系统或运行时列表可以在序列化边界切片。已有 `max_results` 等原生限量参数的接口继续支持显式限量；原生限量参数默认值为 `None` 时，省略所有分页和限量参数仍返回完整列表，显式 `page/count` 的优先级由端点合同说明。
- REST 响应的 `data` 保持原列表结构，不改成 `{items,total}`。`X-Result-Count` 报告本次实际返回数量；仅当 MoviePilot 已经取得完整筛选结果时，才增加精确的 `X-Total-Count`。原有结构化分页接口继续在既有 `data.total` 与 `data.items` / `data.list` 中返回总数。
- `moviepilot_api` 把这些响应头投影为响应中的附加 `collection` 对象：`result_count` 为本次返回数量，`total_count` 仅在精确可知时出现，`page` / `count` 在可用时出现。`collection` 是附加元数据，不替换或改写 `data`。
- Agent 仅查询数量或摘要时，应对支持精确总数的列表发送最小窗口；兼容分页接口使用 `page=1,count=1`，然后直接读取 `collection.total_count`。即使列表内容触发 64KB 工具预览截断，网关也会把 `collection` 放在 `data` 前面，确保总数仍可见；不得因为条目被截断就回退到数据库统计。
- 已经由第三方接口原生分页或限量、但上游没有返回总数的查询不会伪造 `total_count`；Agent 应以 `result_count` 判断当前页是否为空，并按原接口的分页参数继续读取。

### `downloader_operation` / `mediaserver_operation` 调用形状

```json
{
  "name": "downloader_operation",
  "arguments": {
    "client": "main-qb",
    "action": "tasks.list",
    "arguments": {
      "status": "downloading",
      "offset": 0,
      "limit": 20
    }
  }
}
```

媒体服务器将顶层实例字段改为 `server`，其余结构相同。`client`/`server` 可省略；具体 action 的 `arguments` 必须严格匹配对应 `oneOf` 分支。

### `database_operation` 调用形状

```json
{
  "name": "database_operation",
  "arguments": {
    "action": "query",
    "arguments": {
      "sql": "SELECT title, year FROM downloadhistory ORDER BY id DESC",
      "limit": 20,
      "write": false
    }
  }
}
```

数据库 action 参数为：

- `tables`：无参数，列出当前数据库实际表。
- `schema`：必填 `table_name`，必须使用 `tables` 返回的精确名称。
- `query`：`sql` 与 `file` 二选一；可选 `limit`（默认 100）和 `write`（默认 false）。默认只允许 `SELECT`、`WITH`、`EXPLAIN`。
- `write`：`sql` 与 `file` 二选一，只允许单条写入或结构变更语句；必须已有明确授权。

数据库 ORM 当前维护的完整表清单与字段基线见 `skills/database-operation/SKILL.md` 的 `Core Tables`；运行时仍应先调用 `schema`，因为实际部署可能存在迁移差异或插件表。

### 系统设置、配置变量与数据库配置

系统设置统一使用 `moviepilot_api`，不需要恢复旧的 `query_system_settings` / `update_system_settings` 工具：

- `config.system.get` 同时查询 `Settings` 运行配置变量和 `SystemConfigKey` 数据库配置。可用 `setting_key` 精确读取，或用 `group` + `keyword` 发现键；单项默认返回完整值，多项默认只返回摘要。
- 每个发现结果都返回动态 `definition`：声明类型、当前值形状、是否可空/敏感、允许的更新操作、列表默认匹配字段和持久化位置。Agent 应先发现定义，再按返回的精确键和形状调用更新。
- `config.system.update` 支持 `replace`、`merge_dict`、`upsert_list_item`、`remove_list_item`。`Settings` 字段会执行类型转换并持久化到 `app.env`；`SystemConfigKey` 会经配置服务写入数据库并发布配置变更事件。
- 敏感值默认脱敏；只有管理员明确要求时才传 `query.show_secrets=true`，并继续受宿主确认和保护输出策略约束。
- `database_operation` 直接修改 `systemconfig` 只用于受控数据修复。普通配置修改不得绕过键注册、类型转换、插件 mutation 门禁和事件通知。

`skills/moviepilot-api/SKILL.md` 只维护稳定的发现与更新流程，不复制当前版本全部 `Settings` / `SystemConfigKey` 清单。真实键、类型和值形状由 `config.system.get` 运行时发现；MCP `tools/list` 的 `config.system.get/update` 分支负责说明发现参数和更新请求结构。

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
HTTP 工具管理端点统一位于 `/api/v1/mcp`，并与标准 MCP JSON-RPC 端点共享同一最终工具目录。

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

#### 系统更新

系统 Release 更新采用“检查、后台下载、确认安装”三阶段流程，以下接口均要求超级管理员登录态。后台每 6 小时按独立开关检查更新：`MOVIEPILOT_AUTO_UPDATE=true` 检查稳定版 v3 GitHub Release，`AUTO_UPDATE_RESOURCE=true` 检查站点资源包，并分别提示升级。任一开关开启即启用定时服务；两者均关闭时移除定时服务并隐藏版本提醒。手动检查、下载和安装仍可用。独立布尔配置 `MOVIEPILOT_UPDATE_DEV` 控制启动时跟踪 Dev 分支；升级类型只有 `application`（主程序，前端版本由后端 Release 中的 `version.py` 决定）与 `resources`（认证资源和索引资源）。下载完成前不重启服务，安装接口只消费已下载并校验的完整制品，启动器会先应用主程序包，再应用资源包，之后才启动进程；启动后的初始化不会再次下载或触发资源重启。原 Dev 更新入口继续保留，但 `/system/upgrade` 只接受请求体 `"dev"`，不再处理 Release 更新。

| 方法 | 路径 | 说明 |
| :--- | :--- | :--- |
| GET | `/api/v1/system/update/status` | 查询聚合状态、实时提醒开关 `auto_update` / `auto_update_resource` 及 `updates` 中两类升级明细的 `idle`、`available`、`downloading`、`ready`、`installing` 或 `failed` 状态，以及版本、字节数和进度 |
| POST | `/api/v1/system/update/check` | 立即检查最新稳定版 v3 Release 和当前平台站点资源包 |
| POST | `/api/v1/system/update/download` | 请求体可传 `{"target":"application"}` 或 `{"target":"resources"}`；后台下载并校验对应制品 |
| POST | `/api/v1/system/update/install` | 请求体可传 `{"target":"application"}` 或 `{"target":"resources"}`；再次校验对应制品，写入安装意图并重启 |
| POST | `/api/v1/system/upgrade` | 保留 Dev 更新并重启，请求体只能为 `"dev"` |

#### 媒体识别 / 整理

媒体识别、搜索和手动整理统一使用 `media_source` + `media_id` 表示媒体主身份。内置来源通过 `MediaSource` 提供 `themoviedb`、`douban`、`bangumi`、`anilist`、`imdb`、`tvdb`、`musicbrainz`、`theaudiodb`、`doubanmusic` 九个具有宿主模块实现的常量；该列表不是插件来源白名单，插件可以注册符合 OpenAPI 格式约束的稳定扩展标识。`media_id` 是该来源的原生 ID，不添加 `tmdb:` 等前缀。需要精确身份时两个字段必须同时提供，不能只传其中一个。

媒体来源列表 `/api/v1/media/source` 仅预置上述九个来源，其余来源由启用插件注册后提供。哔哩哔哩、芒果 TV、咪咕视频、腾讯视频、爱奇艺不再占用内置来源标识，宿主也不再转换这些插件来源的旧别名；调用方应使用插件声明的准确来源 ID。

影视自动识别在未指定来源时只使用 TMDB，未命中时不会继续查询其它影视源。音乐路径识别严格按 AcoustID 音频指纹、文件标签、文件名三级依次执行；指纹或标签直接提供 MusicBrainz Recording ID 时，会直接查询 MusicBrainz 详情，标签和文件名标题识别也只使用 MusicBrainz。其它元数据源仅在手动操作通过请求级 `media_source`，或通过完整的 `media_source` + `media_id` 精确指定时使用，不修改系统默认值，也不会跨来源兜底。`MediaInfo` 响应仍可能包含 `tmdb_id`、`douban_id`、`bangumi_id`、`anilist_id` 等跨源映射辅助字段，但这些字段不是通用请求入口。明确归属 `/tmdb`、`/douban`、`/bangumi`、`/anilist` 的接口，以及固定使用 TMDB 的剧集组和排期接口，仍可按其单数据源契约接收原生 ID。

| 方法 | 路径 | 说明 |
| :--- | :--- | :--- |
| GET | `/api/v1/media/search` | 按标题搜索媒体、合集、影视人物或音乐；人物结果可同时包含 MusicBrainz 艺术家，参数：`title`、`type`、`page`、`count`，可重复传入可选 `media_source`；未指定 `music_type` 时音乐搜索只返回单曲和专辑，显式 `music_type` 仍可用于专用实体选择；内置模块只处理自身支持的来源，插件模块可以处理其注册的扩展来源，旧客户端的逗号格式仅在输入边界兼容 |
| GET | `/api/v1/media/recognize` | 识别标题，参数：`title`、`subtitle`、`custom_words`，可选 `media_source`；当 `title` 为含目录的媒体文件路径时，会合并父目录中的名称、年份等信息 |
| GET | `/api/v1/media/recognize_file` | 识别文件路径，参数：`path`，可选 `media_source` |
| GET | `/api/v1/media/{media_id}` | 按原生 ID 查询影视或音乐详情；必填参数：`media_source`、`type_name`，其中 `media_source` 与路径中的 `media_id` 组成统一媒体身份，`type_name` 支持电影、电视剧和音乐 |
| POST | `/api/v1/media/scrape/{storage}` | 刮削媒体元数据；请求体为 `FileItem`，可选查询参数 `media_source`、`media_id`、`type_name`（电影/电视剧/音乐）。音乐会按策略处理音频标签、封面和歌词 |
| POST | `/api/v1/transfer/manual/target-path` | 按源文件与目录配置匹配手动整理目标路径；请求体为 `ManualTransferItem`，该接口不执行媒体识别 |
| POST | `/api/v1/transfer/manual/history` | 查询文件、批量文件或目录命中的成功整理历史摘要，用于进入手动整理界面时显示重新整理状态 |
| POST | `/api/v1/transfer/manual` | 手动整理；请求体可用 `media_source` + `media_id` 指定本次识别与刮削数据源；`logids` 会先还原为同一个显式文件批次，多首音乐因而共享专辑识别上下文；音乐请求未传 `music_type` 时，目录按 `album`、文件按 `recording` 解释；可用最多三项的 `music_release_regions`（ISO 3166-1）和 `music_release_scripts`（ISO 15924）仅覆盖本次 MusicBrainz 发行版本排序，省略时继承系统设置；命中持久失败历史，且未指定媒体身份、未开启 `reorganize` 时，由调度器重试原计划（包括 `logid` 历史入口）；显式重整先校验并放弃确定失败任务，再清理旧目标和记录；旧版失败历史仍清理后重试；`reorganize=true` 时清理命中的成功历史和非移动模式旧目标后重新整理 |
| GET | `/api/v1/transfer/tasks/manual-reviews` | 管理员分页查询 durable 人工复核任务；`state` 仅允许 `manual_review`（默认）或已经人工判定、等待调度恢复的 `retry_wait`，支持 `page` 与 `page_size`。响应只公开任务、源文件、状态、步骤意图/证据/错误和复核修订号，不返回 lease 或 attempt 身份 |
| GET | `/api/v1/transfer/tasks/{task_id}/manual-review` | 管理员查询单个 durable 人工复核任务详情；仅可读取 `manual_review` 或已经人工判定的 `retry_wait` 任务，其余状态按不存在处理 |
| POST | `/api/v1/transfer/tasks/{task_id}/manual-review` | 管理员判定处于 `manual_review` 的 durable 整理步骤；请求包含 `operation_id`、`decision=not_applied|applied`、`reason`，`applied` 还必须提供 `result_payload`。`failed` 不属于公开决策，失败终态只能由持租约的 durable 结算写入；响应仅返回任务、操作、决策、后续状态和复核修订号 |

#### 媒体自动分类

媒体自动分类使用完整、可版本化的策略作为唯一写入合同。先读取当前策略的
`revision`，再用字段目录中的稳定字段 ID 和操作符构造规则；发布和回滚均使用
`expected_revision` 做并发校验，成功后产生新的 revision。旧 `/media/category` 与
`/media/category/config` 只读投影仅为兼容客户端保留，不属于 Agent 的 operation。

| 方法 | 路径 | 说明 |
| :--- | :--- | :--- |
| GET | `/api/v1/media/classification/fields` | 登录用户读取标准字段、操作符、通用选项、来源候选与策略限制 |
| GET | `/api/v1/media/classification/policy` | 登录用户读取当前活动策略和 revision |
| POST | `/api/v1/media/classification/validate` | 超级管理员校验完整草稿，不保存 |
| POST | `/api/v1/media/classification/preview` | 登录用户对媒体搜索结果或标准化事实执行单次只读预览，可选草稿策略 |
| POST | `/api/v1/media/classification/impact` | 超级管理员比较活动策略与草稿对近期历史或显式样本的有界影响 |
| GET | `/api/v1/media/classification/history` | 超级管理员读取可回滚的有限历史版本 |
| PUT | `/api/v1/media/classification/policy` | 超级管理员在 `expected_revision` 匹配时校验并发布完整策略，需要写操作确认 |
| POST | `/api/v1/media/classification/rollback/{revision}` | 超级管理员将指定历史策略作为新版本发布，需要当前 `expected_revision` 和写操作确认 |

`transfer/manual` 在 `preview=true` 时保留预览的 `summary/items/message`，不返回执行状态。
请求可传入 `skip_success=true`，在预览与执行中跳过同存储、同源路径已成功整理的文件，
也识别成功移动后的目标现址。该选项优先于 `reorganize` 和历史入口的强制整理，
不清理被跳过文件的历史和旧目标；失败记录及未处理文件继续原有流程，默认 `false` 保持现有行为。
被跳过文件不进入预览列表；全部跳过时返回空列表、零计数和跳过数量提示。
实际提交返回独立的 `data.items` 回执，即使批次 `success=false` 也保留其他文件的结果。
每项包含 `source/target/target_dir/success/message/failure_stage/recovery_action/overwrite_skipped/state`；
`state=accepted` 仅表示已接收，`retry_wait` 表示原计划已交给后台恢复，均不代表入库。
仅 `completed` 表示执行和终态原子结算已确认；`failed` 表示本次失败，`skipped` 表示历史、模板或覆盖策略跳过。
`manual_review` 表示等待人工复核，应先在整理队列中确认执行结果，不能自动重提。
`success` 表示本次操作被接收或完成，不能代替 `state` 判断入库；客户端不得在部分接收后原样重提整个批次。

#### 站点

| 方法 | 路径 | 说明 |
| :--- | :--- | :--- |
| GET | `/api/v1/site/media/{media_type}` | 按媒体类型查询已配置且启用的可搜索站点；`media_type` 支持 `movie`、`tv`、`music` 或对应中文类型，音乐仅返回明确声明音乐能力的站点，影视不返回纯音乐站点 |

#### 搜索 / 种子 / 字幕

| 方法 | 路径 | 说明 |
| :--- | :--- | :--- |
| GET | `/api/v1/search/media/{media_id}` | 按统一媒体身份搜索站点种子资源；必填参数：`media_source`，其它参数：`mtype`、`area`、`season`、`sites`、`music_type`、`include_candidates` |
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

音乐与影视共用媒体搜索、资源查询、过滤、匹配和订阅搜索编排。资源 `meta_info` 来自
标题、副标题的实际解析，不用目标媒体回填证据；`title_aliases`、`album_aliases`、
`artist_aliases` 分别保留同一实体的可信别名及展示转简体前的原文。
音乐资源解析同样应用全局或订阅自定义识别词，`MusicMeta.apply_words` 返回实际应用记录，
旧结果缺少该字段时按空列表处理。副标题的明确录音版本参与匹配；单曲所属专辑字段
不能证明资源覆盖整张专辑，无曲序的单曲也会标记为 `partial_album` 待确认项。
这些名称、艺名和版本规则也适用于显式选择的 TheAudioDB 与豆瓣音乐；查询专辑或确认
单曲所属专辑时优先使用专辑艺人，不能覆盖单曲的表演者。版本字段中双方明确且唯一的
年份、录制日期冲突会使资源进入 `version_mismatch` 待确认项，缺少日期不单独造成淘汰。

音乐资源搜索及对应 SSE 接口默认只返回精确匹配。手动调用可传 `include_candidates=true`，
额外返回待确认资源及关联专辑：`match_status=candidate`、`match_reason` 描述原因，
且 `media_info` 为空、不绑定目标 ID；精确结果为 `match_status=exact`。自动订阅和批量下载
不采用待确认项。单曲的关联专辑不代表已经确认包含该单曲，专辑下载仍需检查曲目覆盖。
SSE 的 `candidate_items` 是站点原始返回数量，`match_counts` 记录身份、分类及规则淘汰原因。
只有完整过滤后还有精确结果时才会按多名称设置提前停止；音乐元数据多来源结果先各自去重再公平合并。

音乐识别结果同时提供 `audio_format`、`audio_lossless`、`audio_quality`、`bit_depth`、`sample_rate`、`bitrate`、`audio_specs` 和 `audio_quality_score`。本地文件识别读取实际音频流参数，并使用 Chromaprint 的 `fpcalc` 在本地生成指纹后查询 AcoustID；音频文件本身不会上传。站点资源识别从标题和描述提取声明参数；码率、采样率的存储单位分别为 bps 和 Hz。

| 方法 | 路径 | 说明 |
| :--- | :--- | :--- |
| GET | `/api/v1/media/search` | 当 `type=music` 或指定音乐 `media_source` 时按歌曲或专辑关键词搜索音乐元数据；艺术家统一由 `type=person` 搜索，支持 TMDB 与 MusicBrainz 来源。参数：`title`、`type`、`count`、可重复的 `media_source` 枚举，以及可选的 `music_type` 实体过滤 |
| POST | `/api/v1/music/recognize` | 按 `media_source` + `media_id` 识别音乐详情，请求体：`MusicRecognizeRequest` |
| GET | `/api/v1/music/explore` | 按来源浏览音乐；`media_source=musicbrainz` 支持 `mode=chart|fresh` 榜单与新发行，`media_source=doubanmusic` 固定按官方标签分类浏览，使用 `tags` 和 `douban_sort=U|S|R|O` 筛选。其它参数：`entity=recording|album`、`range_name`、`sort_by`、`sort`、`days`、`past`、`future`、`min_listen_count`、`with_cover`、`page`、`count` |
| POST | `/api/v1/music/library/status` | 按 MusicBrainz、TheAudioDB 或豆瓣音乐的稳定专辑 ID 批量查询媒体库是否已存在；请求体为 `items` 专辑列表，返回对应的 `exists` 状态，供艺人作品资源矩阵默认排除已入库项目 |
| GET | `/api/v1/music/album/{album_id}` | 按来源专辑 ID 查询专辑详情、完整曲目和发行版本，参数：`media_source` |
| GET | `/api/v1/music/album/{album_id}/related` | 按来源查询关联专辑，参数：`media_source`、`count` |
| GET | `/api/v1/music/artist/{artist_id}` | 查询艺术家详情；艺术家为只读浏览实体，参数：`media_source` |
| GET | `/api/v1/music/artist/{artist_id}/albums` | 分页查询艺术家的专辑、EP 和单曲，参数：`media_source`、`page`、`count`、`album_type` |
| GET | `/api/v1/music/artist/{artist_id}/related` | 查询关联艺术家，参数：`media_source`、`count` |
| GET | `/api/v1/recommend/music_weekly` | 浏览本周热门音乐，参数：`page`、`count` |
| GET | `/api/v1/recommend/music_douban` | 浏览豆瓣音乐新碟榜，参数：`page`、`count` |

专辑下载与订阅按“整包”处理：下载层会读取种子文件清单并以专辑 `total_tracks` 校验独立音频文件数量；未确认完整覆盖时不会把专辑订阅销订，也不会把部分曲目报告为完整专辑已入库。音乐整理会迁移与音轨同目录、同主干名的 `.lrc`、`.txt` 和 `.lyricsfile.yaml` 旁挂歌词。音乐刮削默认使用“质量升级”策略：先读取已有旁挂和 MP3/FLAC/Ogg/MP4 内嵌歌词，再聚合插件、LRCLIB、AMLL TTML 和 TheAudioDB 纯文本候选；逐字 Lyricsfile、逐行同步 LRC、纯文本依次降级，任何覆盖入口都不会用低质量结果替换高质量歌词。Lyricsfile 会保留为 `.lyricsfile.yaml`，同时生成播放器兼容的 `.lrc`。

AMLL 使用无需鉴权的原生搜索与获取接口，先尝试 ISRC，再核对完整曲名、艺术家和已有专辑；搜索最多读取 20 项并下载 3 个匹配候选。TTML 只转换主唱内容，排除翻译、音译和背景人声；可靠的逐词或逐行时轴会保留，缺乏完整行时轴时降为纯文本。`AMLL_BASE_URL` 可配置兼容实例地址，网络超时为 10 秒，限流时进入最多 300 秒的有界冷却。动态搜索和 ISRC 查询缓存 1 小时，固定 ID 歌词缓存 7 天，未命中缓存 5 分钟。接口与格式依据见 [AMLL HTTP API](https://amll.dev/reference/http-api/overview)。

插件可通过 `get_module()` 注册 `music_lyrics_candidates(music)`，负责匹配并下载歌词内容，返回 `list[MusicLyrics]` 参与宿主择优；`MetaMusic`、`MusicInfo` 和 `MusicLyrics` 均可从 `app.sdk.media` 导入。该接口不需要注册 HTTP 路由，歌词文件仍由刮削链统一写入。完整契约和示例见[歌词插件开发说明](https://github.com/jxxghp/MoviePilot-Plugins/blob/main/docs/faq/21-register-lyrics-provider.md)。

音乐订阅可使用 `audio_quality=hires|lossless|lossy`（支持正则组合）、`audio_format`、`min_bitrate`、`min_bit_depth`、`min_sample_rate` 过滤资源。`best_version=1` 开启音质洗版，系统按格式、无损属性、位深、采样率和码率换算 0-100 优先级，只下载高于 `current_priority` 的候选；DSD 或 24-bit/192 kHz 无损资源达到终态 100。内置规则 `HIRES`、`LOSSLESS`、`FLAC`、`ALAC`、`APE`、`WAV`、`DSD`、`MP3`、`AAC`、`OPUS`、`BITRATE320`、`BITRATE256`、`BITRATE192` 可用于自定义过滤规则组。

#### 下载

| 方法 | 路径 | 说明 |
| :--- | :--- | :--- |
| GET | `/api/v1/download/` | 查询正在下载的任务，参数：`name`；关联下载历史时返回媒体类型、来源站点 `site_name`，以及 `media.poster` 海报和 `media.backdrop` 背景图；兼容字段 `media.image` 与 `media.poster` 相同 |
| POST | `/api/v1/download/` | 添加含媒体信息的下载任务，请求体包含媒体信息和种子信息 |
| POST | `/api/v1/download/add` | 添加不含媒体信息的下载任务，请求体包含 `torrent_in`，可选且必须成对提供 `media_source` + `media_id`，并支持 `music_type`、`downloader`、`save_path`；影视或音乐识别失败时统一响应 `data.requires_confirmation=true`，用户确认后可用 `allow_unrecognized=true` 重试本次下载 |
| POST | `/api/v1/download/subtitle` | 下载字幕到识别出的媒体下载目录，请求体包含 `subtitle_in`，并必须提供 `media_source` + `media_id`；可选 `save_path` |
| GET | `/api/v1/download/start/{hashString}` | 恢复下载任务，参数：`name` |
| GET | `/api/v1/download/stop/{hashString}` | 暂停下载任务，参数：`name` |
| PATCH | `/api/v1/download/{hashString}` | 高级更新下载任务，可修改限速、标签、Tracker、保存目录和下载器分类 |
| POST | `/api/v1/download/{hashString}/classify-source` | `recognize` 模式重新识别媒体并按当前生效分类计算保存位置，`manual` 模式使用明确目标目录；`execute=false` 只预览，`execute=true` 由下载器移动任务数据；可传当前策略中已启用的 `media_category` 路径覆盖自动分类 |
| GET | `/api/v1/download/clients` | 查询可用下载器 |
| GET | `/api/v1/download/paths` | 查询可用于下载接口 `save_path` 参数的下载路径 |
| DELETE | `/api/v1/download/{hashString}` | 删除下载任务，参数：`name` |

资源目录重新分类只接受仍存在于下载器且具有可恢复媒体类型的下载历史任务；识别模式可复用历史中的媒体来源和同来源媒体 ID，也可在请求中指定来源、媒体 ID 或当前策略中已启用且媒体类型匹配的 `media_category`。
目标路径必须落在已配置的资源根目录内，并且目录需开启“资源目录按类别分类”或绑定固定分类。
识别模式会优先使用媒体识别链产生的当前生效分类路径；例如 MusicBrainz 返回 `Album` 主类型和 `Compilation` 副类型并命中默认精选集规则时，目标分类为 `Album/Compilation`。识别结果尚无可用分类路径时，音乐兼容退回主类型目录。
执行时 MoviePilot 调用下载器的位置更新能力，不直接移动或改写 PT 数据文件。

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
| GET | `/api/v1/system/module-catalog` | 查询宿主模块及其服务类型目录，供前端选择器构造选项 |
| GET | `/api/v1/system/modulelist` | 查询已启用模块，保留 `name` 原始中文字段，并提供 `name_i18n` 和 `name_key` 给多语言前端展示 |
| GET | `/api/v1/system/module-settings` | 管理员查询没有其它激活配置、可由用户统一开关的内置模块 |
| GET | `/api/v1/system/moduletest/{moduleid}` | 测试指定模块可用性，标准响应的 `message` 会按请求语言直接返回翻译文本 |
| GET | `/api/v1/message/agent/mcp/servers` | 管理员查询 Agent 外部 MCP 服务器配置 |
| POST | `/api/v1/message/agent/mcp/servers` | 管理员保存 Agent 外部 MCP 服务器配置 |
| POST | `/api/v1/message/agent/mcp/servers/test` | 管理员测试单个 Agent 外部 MCP 服务器并读取工具列表 |

#### 缓存管理

以下接口使用登录态鉴权，并要求当前用户为超级管理员。

| 方法 | 路径 | 说明 |
| :--- | :--- | :--- |
| GET | `/api/v1/tmdb/cache` | `media.cache.get`：查询 TheMovieDb 识别缓存统计、共享识别累计成功命中次数及开关状态 |
| DELETE | `/api/v1/tmdb/cache/{cache_key}` | `media.cache.delete`：按缓存键删除单条 TheMovieDb 识别缓存，缓存键需要进行 URL 编码 |
| DELETE | `/api/v1/tmdb/cache` | `media.cache.clear`：清空全部 TheMovieDb 识别缓存 |
| GET | `/api/v1/music/cache` | `music.cache.get`：查询 MusicBrainz 音乐识别缓存统计及条目列表 |
| DELETE | `/api/v1/music/cache/{cache_key}` | `music.cache.delete`：按缓存键删除单条音乐识别缓存，缓存键需要进行 URL 编码 |
| DELETE | `/api/v1/music/cache` | `music.cache.clear`：清空全部音乐识别缓存 |

TMDB 缓存查询响应的 `data` 包含 `count`、`recognized`、`unrecognized`、`data`，以及共享识别统计字段
`shared_recognized` 和开关字段 `shared_recognize_enabled`。共享命中次数仅在共享结果驱动的二次媒体识别成功后累计。

音乐识别缓存查询响应的 `data` 包含 `count`、`recognized`、`unrecognized` 和 `data`；条目字段包括缓存键、`media_id`、`title`、`artists`、`album`、`year`、`music_type` 和 `cover_url`。未携带远端身份的兜底负缓存仅保留在内存，不参与持久化。
缓存键为不透明值，管理调用必须使用查询返回的原始键，不自行拼接。识别缓存按请求的
单曲、专辑或未限定实体范围隔离，版本及 ISRC 不同的文本识别请求也不会共用结果；
旧版未包含这些证据的派生缓存在升级后重新建立，不影响下载历史或订阅数据。
名称确认规则更新时同样重建旧派生缓存，避免艺术家前后缀误截断的旧结果继续命中。

### 订阅搜索执行批次

订阅搜索会持久化为可恢复的执行批次，Agent 可以使用以下 `moviepilot_api` operation 查询或停止当前用户可见的批次：

| Operation | 方法 | 路径 | 说明 |
| :--- | :--- | :--- | :--- |
| `subscription.execution.list` | GET | `/api/v1/subscribe/execution/batches` | 查询最近的订阅搜索批次，`limit` 默认 10 |
| `subscription.execution.get` | GET | `/api/v1/subscribe/execution/batches/{batch_id}` | 查询一个批次的状态、进度、恢复信息和最终结果 |
| `subscription.execution.cancel` | PUT | `/api/v1/subscribe/execution/batches/{batch_id}/cancel` | 在下载副作用边界前请求取消一个批次 |

取消是幂等的状态请求，不会撤销已经提交到下载器的任务；批次详情中的状态和任务结果才是最终事实。

### 单条订阅搜索周期

`POST /api/v1/subscribe/` 和 `PUT /api/v1/subscribe/` 支持 `search_interval`：
取值为 1–8760 的整数小时数，`null` 表示跟随系统的 `SUBSCRIBE_SEARCH_INTERVAL`。
更新时省略该字段保留原值，显式传入 `null` 恢复系统周期。默认订阅规则同样可保存此字段。

仅在启用 `SUBSCRIBE_SEARCH` 时执行定时搜索，每五分钟检查到期订阅，再通过原有搜索队列和站点限流执行；
实际开始时间可能因站点忙而延后。`last_search` 为系统维护的 UTC 搜索尝试开始时间，重启后仍有效，公共写接口忽略它。
旧订阅尚无搜索时间时，以添加时间计算到期。新订阅首次搜索、手动搜索和 RSS 刷新不受周期过滤影响；
手动主动搜索会更新最近搜索时间。电影、电视剧和音乐均支持独立周期。

自动批次只在启动时随机错峰 0–60 秒，不再按订阅数量累加分钟级等待；同站点访问间隔、唯一在途租约和错误冷却继续生效。
站点暂不可用时，任务保存未完成站点并按 `next_run_at` 恢复，不重复查询已完成站点或插件源；队列和站点游标可跨重启恢复。
`waiting_site_budget` 表示可恢复等待，`error` 中的“等待站点”或“站点冷却中”是原因提示，不表示搜索失败；重新执行时清除旧提示。
卡片应按 `state` / `phase` 展示简短标签，原因放入详情提示。恢复调度每 10 秒检查空闲消费者，长搜索由调度器持续托管。
实际吞吐仍受站点访问限制和网络响应时间影响，配置周期不是全部站点必须完成的截止时间。


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

MCP、HTTP 工具管理接口、本地 CLI 和内置 Agent 都从同一严格目录生成工具列表。
旧业务工具名不再注册，也没有 MCP 别名或兼容目录；例如
`search_media`、`add_subscribe`、`query_download_tasks`、`query_schedulers`
会直接返回工具不存在。插件工具仍由插件的 `get_agent_tools()` 动态声明，重名会让目录
构造失败，不采用 first-wins 覆盖。

固定目录按职责收敛为：

| 工具 | 说明 |
| :--- | :--- |
| `moviepilot_api` | 通过固定 `operation_id` 调用受控 MoviePilot 业务 API；不接受 URL、HTTP method、认证头或 Token |
| `agent_task` | 通过 `action=create|list|update|run|delete` 管理自主任务 |
| `persona` | 通过 `action=list|switch|update` 管理 Agent 人格 |
| `send_message`、`send_local_file` | 当前目录中可用的消息和文件发送能力；实际渠道能力仍由运行时校验 |
| `browse_webpage`、`recognize_captcha` | 浏览和验证码等非 MoviePilot 业务 API 能力 |
| `query_doctor_report` | 只读系统诊断 |

`read_skill`、`read_file`、`write_file`、`edit_file`、`apply_patch`、`execute_command`、
`search_web` 和 `view_image` 不通过 MCP 暴露。隐藏列表只负责收敛接口暴露面，不替代各工具自身的
权限、路径和网络边界。

`browse_webpage(action="screenshot")` 的外部直调仍返回 JSON 字符串，保留
`url/title/screenshot_base64/format/note` 并用 `success/execution_outcome` 明确状态。
内置 Agent 在专用格式化路径把成功截图转换为图像输入，外部 HTTP/MCP 客户端仍按
原 JSON 合同消费；本次不宣称外部 MCP 已提供原生 image content block。

`view_image` 只供内置 Agent 使用：它会在 Agent 专用格式化路径把 URL、本地图片或图片内容转换为
原生图像输入，HTTP/MCP 直调不提供该工具，避免把只能由视觉中间件消费的图像块误当作普通 JSON。

内置命令工具 `execute_command(action="run")` 的返回值为 JSON 字符串，包含
`success`、`execution_outcome`、`status`、`exit_code`、`timed_out`、`timeout`、
`output_truncated`、`output_file`、`output` 和 `message`。正常退出码 0 才是成功，
非零退出及已停止的超时执行为失败，无法确认进程结束时为 unknown。输出预览与状态分开，
不再通过中文完成提示判断成功。后台 `start/read/wait/write/interrupt/kill` 保持会话状态与游标协议；
`env` 同时适用于 `start` 和 `run`，此工具仍不通过 MCP 暴露。

`run`、pipe 和 PTY 共享 `cwd/shell/login`：默认及相对 `cwd` 使用 MoviePilot 根目录，
POSIX 默认非登录；Windows 未指定时保留已有解释器/UTF-8 策略。回包包含实际 `shell/login`。
`write(close_stdin=true)` 仅在 pipe 模式支持末段输入后 EOF，回包包含 `stdin_closed`；输出可继续读取。
PTY 会在写入之前拒绝 half-close，空 `write` 不代表 EOF，控制字节在 pipe 中也不等于信号。
新增 `interrupt` 只发送一次平台支持的中断并返回 `signal/signal_sent`，不会升级强杀；
`kill` 保留终止语义但提前拒绝无效信号。上述能力仍为内置管理员 Agent 工具，不扩大外部 MCP 目录。

后台句柄按宿主用户和真实任务作用域隔离；相同对话的不同定时 `run_id` 也不会共享终端。
正常对话可跨轮继续，清空/更换用户或定时运行收尾只回收自身进程。子任务仅在父任务明确提供
`terminal_sessions` 时获得指定 `read/wait` 授权，不能凭句柄或任务文字访问其他终端。
缺失、封闭或无权限均返回 `terminal_access_denied`，不会回显其他任务状态；宿主重启不恢复旧句柄。

后台命令的 `start` 新增 `yield_time_ms`（默认 250、上限 10000，0 不等待）。
`read/wait/write/interrupt/kill` 接受 `since_seq` 和 `since_offset`：一起传回上次响应的
`output_until_seq/output_until_offset`，后者表示下一分片内的 UTF-8 字节位置；
首次显式传 offset=0 开启部分分片读取。不传 offset 的旧调用只返回完整分片，
页预算过小时返回 `read_limit_too_small` 和 `minimum_read_bytes`。
所有后台动作都返回实际交付的输出游标，`last_seq` 不代表已读位置。
`wait` 可由新增输出提前唤醒，0ms 表示非阻塞读取；`output_complete` 表示读取器已收尾，
`output_lost` 表示输出存在不可恢复缺口。动作已执行后的分页错误放在 `output_error` 中，
保留会话 ID，调用方应仅重试读取；纯读错误返回 `execution_outcome=failed`。

下载器和媒体服务器的第三方原生高级能力不注册成永久 MCP 工具。内置 Agent 按需
加载 `downloader-operation` 或 `mediaserver-operation` Skill，通过固定脚本读取本机
配置、发现 provider 能力并调用受控 action；脚本不接受任意 URL、认证信息或任意
SDK method。普通 MCP 客户端如需这些 provider 原生能力，应使用对应第三方服务的
正式 API，而不是依赖已删除的 MoviePilot 旧工具名。

`send_message` 新增可选的 `rich_message` 字符串参数，用于传入一份完整的 GitHub 风格 Markdown 正文。Telegram 渠道会把它转换为 Bot API Rich Message，支持标题、列表、表格、引用、代码块和链接，并按 Rich Message 限制自动分段；没有使用该参数时继续走原有普通消息链路。广播到其它通知渠道时，同一正文会作为普通 `text` 回退。`rich_message` 是完整正文，不应再同时传 `message`、`title` 或 `image_url` 表达同一份内容。内置 Agent 在 Telegram 会话中的普通回复、流式首发和后续流式编辑都会优先使用该富文本链路。

`moviepilot_api` 的模型可见输入固定为：

```json
{
  "operation_id": "media.search",
  "path_params": {},
  "query": {"title": "流浪地球", "type": "media"},
  "body": {}
}
```

宿主按 `operation_id` 决定固定 method 与 path，使用真实持久化管理员身份为
API KEY 集成签发短期本机令牌，并按 operation 执行权限、确认、结果脱敏和恢复策略。
调用方不能注入 host、URL、认证头或 API Token。
Web Agent 直接调用 `moviepilot_api` 时，宿主会自动加载 `moviepilot-api` Skill
的 operation 白名单后再执行；这只是授权兜底，不会放宽固定 operation、身份、权限
或确认策略。

当前业务 operation 分组如下；主流程与分类索引见 `skills/moviepilot-api/SKILL.md`，
完整参数合同见其 `skills/moviepilot-api/api/*.md` 分类文档和各 REST 请求模型：

| 领域 | Operation ID |
| :--- | :--- |
| 媒体/搜索 | `media.search`、`media.person.search`、`media.person.credits`、`media.recognize`、`media.scrape`、`media.episode_schedule`、`media.detail`、`search.torrents`、`search.results`、`recommendation.list` |
| 媒体自动分类 | `media.classification.fields`、`media.classification.policy.get`、`media.classification.policy.validate`、`media.classification.policy.preview`、`media.classification.policy.impact`、`media.classification.policy.history`、`media.classification.policy.update`、`media.classification.policy.rollback` |
| 订阅 | `subscription.add`、`subscription.update`、`subscription.search`、`subscription.list`、`subscription.shares`、`subscription.popular`、`subscription.history`、`subscription.delete`、`subscription.execution.list`、`subscription.execution.get`、`subscription.execution.cancel` |
| 下载/历史 | `download.add`、`download.artist_collection`、`download.history.delete`、`transfer.history.delete` |
| 媒体缓存 | `media.cache.get`、`media.cache.delete`、`media.cache.clear`、`music.cache.get`、`music.cache.delete`、`music.cache.clear` |
| 媒体库/存储/转移 | `library.exists`、`storage.settings`、`storage.list`、`transfer.history`、`transfer.file` |
| 站点 | `site.list`、`site.update`、`site.userdata`、`site.test`、`site.cookie.update` |
| 调度/工作流 | `scheduler.list`、`scheduler.run`、`workflow.list`、`workflow.run` |
| 插件 | `plugin.installed`、`plugin.market`、`plugin.capabilities`、`plugin.config.get`、`plugin.config.update`、`plugin.reload`、`plugin.install`、`plugin.uninstall`、`plugin.data` |
| 规则/配置/命令 | `filter.builtin`、`filter.custom`、`filter.groups`、`filter.custom.add`、`filter.custom.update`、`filter.custom.delete`、`filter.group.add`、`filter.group.update`、`filter.group.delete`、`config.identifiers.get`、`config.identifiers.update`、`config.system.get`、`config.system.update`、`slash.list`、`slash.run` |

`download.list`、`download.update`、`download.delete`、`downloaders.list` 和
`library.latest` 已从 Agent operation 目录删除，避免与 provider Skill 重复。供前端和
其它宿主使用的普通 REST 端点仍然保留。

#### Agent 自主定时任务与人格

`agent_task` 是唯一的自主任务工具，要求管理员权限：

| Action | 说明 |
| :--- | :--- |
| `create` | 创建单次或周期任务，并保存任务内容及当前用户、会话上下文 |
| `list` | 查询任务配置、启用状态、下次执行时间及最近执行结果 |
| `update` | 修改任务内容或触发器，也可通过 `enabled` 暂停、恢复任务 |
| `run` | 使用整数 `task_id` 将当前用户已启用的任务提交为立即执行 |
| `delete` | 永久删除任务并立即移除运行时调度 |

`trigger_type=date` 表示单次执行：“30 分钟后检查”这类相对时间传 `delay_minutes=30`，由后端计算精确时间；固定时间则传 ISO 8601 `trigger`，支持精确到秒。`trigger_type=cron` 使用标准五段 cron（分、时、日、月、周），适合周期检查。未显式携带时区的时间按 MoviePilot 的 `TZ` 配置解释。任务由内存调度器精确触发，配置持久化到数据库，服务重启后会自动恢复；触发后 Agent 在原会话中执行 `content`，执行过程及最终结果均不绑定创建任务时的消息渠道，而是通过 MoviePilot 已配置的通知渠道广播。如果 Agent 在执行过程中已通过消息工具发送完整结果，任务结束时不会再次发送相同的最终回复。

服务重启时仍处于运行中的任务会显示为 `interrupted`，表示上次结果未知且可能已有部分操作。中断的一次任务不会自动补跑，暂停后恢复也仍保留中断状态；需要先核对实际结果，再用 `agent_task(action="run")` 明确立即重跑，或通过 `agent_task(action="update")` 提供新的 `trigger_type` 与未来触发时间重新安排。

Agent 自主任务使用数据库中的整数 `task_id`。`scheduler.list` 与 `scheduler.run` operation 仅面向系统、插件和工作流注册的运行时定时服务，使用字符串 `job_id`，不会返回或执行 `agent-task-*`。两类 ID 不可混用；需要立即执行自主任务时，应先通过 `agent_task(action="list")` 确认归属和状态，再调用 `agent_task(action="run")`。立即执行只提交任务，不在当前工具调用内等待结果，从而避免同一 Agent 会话互相等待；执行结果仍按上述通知规则广播。

上述过滤只约束 Agent 工具，避免模型混用两类任务。前端系统设置和仪表盘使用的 `/api/v1/dashboard/schedule` 仍返回完整运行时列表，其中包含 `provider=[Agent]` 的自主任务；前端通过 `/api/v1/system/runscheduler` 立即执行这类列表项的行为也保持不变。

创建单次任务的参数示例：

```json
{
  "tool_name": "agent_task",
  "arguments": {
    "action": "create",
    "name": "检查电影资源",
    "content": "搜索电影《示例电影》是否已有可下载资源，并报告站点、版本和大小；不要自动下载。",
    "trigger_type": "date",
    "delay_minutes": 30
  }
}
```

创建每天 20:30 执行的周期任务时，使用 `trigger_type=cron` 和 `trigger="30 20 * * *"`。

`persona` 使用 `action=list|switch|update`。`list` 可按 `query` 过滤；
`switch` 必须提供 `persona_id`；`update` 只有管理员可用，支持替换 label、
description、aliases、instructions，或通过 `append_instructions` 追加规则。旧的
`query_personas`、`switch_persona` 和 `update_persona_definition` 不再注册。

**认证**: 需要API KEY，在请求头中添加 `X-API-KEY: <api_key>` 或在查询参数中添加 `apikey=<api_key>`

**响应示例**:
```json
{
  "success": true,
  "message": "",
  "data": [
    {
      "name": "moviepilot_api",
      "description": "调用经过白名单审核的 MoviePilot 业务 API...",
      "inputSchema": {
        "type": "object",
        "properties": {
          "operation_id": {
            "type": "string",
            "description": "稳定的 MoviePilot API operation ID"
          }
        },
        "required": ["operation_id"]
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
  "tool_name": "moviepilot_api",
  "arguments": {
    "operation_id": "media.search",
    "query": {
      "title": "流浪地球",
      "type": "media"
    }
  }
}
```

**响应示例**:
```json
{
  "success": true,
  "message": "",
  "data": {
    "result": "{\"success\":true,\"message\":\"\",\"data\":[...]}"
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
    "name": "moviepilot_api",
    "description": "调用经过白名单审核的 MoviePilot 业务 API...",
    "inputSchema": {
      "type": "object",
      "properties": {
        "operation_id": {
          "type": "string",
          "description": "稳定的 MoviePilot API operation ID"
        }
      },
      "required": ["operation_id"]
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
      "operation_id": {
        "type": "string",
        "description": "稳定的 MoviePilot API operation ID"
      },
      "query": {
        "type": "object",
        "description": "固定 operation 的查询参数"
      }
    },
    "required": ["operation_id"]
  }
}
```


### 分类条件字段字典

`GET /api/v1/media/classification/fields` 的 `fields` 与 `retired_fields` 使用同一字段目录 schema：
`options` 提供来源无关的 `{value, label}`，`source_options` 按数据源 ID 提供开放候选。国家与语言显示中文名称，规则保存标准代码；风格保存与分类事实归一化共用的稳定键。来源风格和音乐枚举保留原始大小写。

客户端合并通用选项和所选来源的候选；未限制来源时展示全部候选并标注来源。`allow_custom_values` 为真时允许输入其他值，切换来源不得清空已有条件。`source_options` 缺失等价于空目录；候选是录入辅助，不改变来源支持等级或规则校验范围。公司、平台和用户标签等开放字段应使用媒体预览中的原值。
