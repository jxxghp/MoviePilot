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

标准 REST 响应包含 `success`、`message`、`message_i18n`、`data` 字段。为兼容 App 和第三方客户端，`message` 继续保留原中文或原始后端文本；新版前端可发送 `X-MoviePilot-Locale: zh-CN|zh-TW|en-US` 或 `Accept-Language`，并优先展示 `message_i18n`。未提供语言头或翻译缺失时，`message_i18n` 会回退为原文本。

FastAPI 异常响应保留 `detail` 字段，并在错误详情为文本时返回 `detail_i18n`；新版前端优先展示 `detail_i18n`，缺失时回退 `detail`。

#### 媒体识别 / 整理

媒体识别、搜索和手动整理内置支持 `themoviedb`、`douban`、`bangumi`、`anilist` 四种数据源，也允许插件处理自定义来源。自动识别仍使用系统默认来源；手动操作可通过请求级 `source` 或 `media_source` + `media_id` 临时指定来源，不修改系统默认值。

涉及媒体身份的请求统一以 `media_source` + `media_id` 表示本次选定的主身份，同时保留 `tmdbid`、`doubanid`、`bangumiid`、`anilistid` 作为跨数据源映射和旧客户端兼容字段。两者并非两套独立数据流：显式通用主身份优先，专用 ID 用于补全映射和兼容回退。

| 方法 | 路径 | 说明 |
| :--- | :--- | :--- |
| GET | `/api/v1/media/search` | 按标题搜索媒体、合集或人物，参数：`title`、`type`、`page`、`count`，可选 `source`；`media` 支持 `themoviedb`、`douban`、`bangumi`、`anilist`，`collection` 支持 `themoviedb`，`person` 支持 `themoviedb`、`douban` |
| GET | `/api/v1/media/recognize` | 识别标题，参数：`title`、`subtitle`、`custom_words`，可选 `source` |
| GET | `/api/v1/media/recognize_file` | 识别文件路径，参数：`path`，可选 `source` |
| GET | `/api/v1/media/{mediaid}` | 查询媒体详情，`mediaid` 支持 `tmdb:`、`douban:`、`bangumi:`、`anilist:` 及插件自定义来源前缀 |
| POST | `/api/v1/media/scrape/{storage}` | 刮削媒体元数据；请求体为 `FileItem`，可选查询参数 `media_source`、`media_id`、`type_name`（电影/电视剧）可指定本次刮削媒体 |
| POST | `/api/v1/transfer/manual/target-path` | 匹配手动整理目标路径；请求体可用 `media_source` + `media_id` 指定数据源原生ID |
| POST | `/api/v1/transfer/manual` | 手动整理；请求体可用 `media_source` + `media_id` 指定本次识别与刮削数据源，同时兼容 `tmdbid`、`doubanid`、`bangumiid`、`anilistid` |

#### 搜索 / 种子 / 字幕

| 方法 | 路径 | 说明 |
| :--- | :--- | :--- |
| GET | `/api/v1/search/media/{mediaid}` | 按媒体 ID 搜索站点种子资源，`mediaid` 支持 `tmdb:123`、`douban:123`、`bangumi:123`、`anilist:123` 及插件来源前缀，参数：`mtype`、`area`、`title`、`year`、`season`、`sites` |
| GET | `/api/v1/search/media/{mediaid}/stream` | 按媒体 ID 渐进式搜索站点种子资源，返回 SSE，参数同上 |
| GET | `/api/v1/search/title` | 按关键字模糊搜索站点种子资源，参数：`keyword`、`page`、`sites` |
| GET | `/api/v1/search/title/stream` | 按关键字渐进式搜索站点种子资源，返回 SSE，参数：`keyword`、`page`、`sites` |
| GET | `/api/v1/search/subtitle/title` | 按关键字搜索站点字幕资源，参数：`keyword`、`page`、`sites` |
| GET | `/api/v1/search/subtitle/title/stream` | 按关键字渐进式搜索站点字幕资源，返回 SSE，参数：`keyword`、`page`、`sites` |
| GET | `/api/v1/search/subtitle/media/{mediaid}` | 按媒体 ID 精确搜索站点字幕资源，`mediaid` 支持四种内置来源及插件来源前缀，参数：`mtype`、`title`、`year`、`season`、`episode`、`sites` |
| GET | `/api/v1/search/subtitle/media/{mediaid}/stream` | 按媒体 ID 渐进式精确搜索站点字幕资源，返回 SSE，参数同上 |
| GET | `/api/v1/search/last` | 获取上一次种子搜索结果 |
| GET | `/api/v1/search/last/context` | 获取上一次搜索结果及可复用搜索参数，`params.result_type` 为 `torrent` 或 `subtitle` |
| POST | `/api/v1/search/recommend` | 获取 AI 推荐资源，请求体：`filtered_indices`、`check_only`、`force` |

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

#### 下载

| 方法 | 路径 | 说明 |
| :--- | :--- | :--- |
| GET | `/api/v1/download/` | 查询正在下载的任务，参数：`name` |
| POST | `/api/v1/download/` | 添加含媒体信息的下载任务，请求体包含媒体信息和种子信息 |
| POST | `/api/v1/download/add` | 添加不含媒体信息的下载任务，请求体包含 `torrent_in`，可选 `media_source` + `media_id`；继续兼容四种专用 ID，并支持 `downloader`、`save_path` |
| POST | `/api/v1/download/subtitle` | 下载字幕到识别出的媒体下载目录，请求体包含 `subtitle_in`，可选 `media_source` + `media_id`；继续兼容四种专用 ID，并支持 `save_path` |
| GET | `/api/v1/download/start/{hashString}` | 恢复下载任务，参数：`name` |
| GET | `/api/v1/download/stop/{hashString}` | 暂停下载任务，参数：`name` |
| GET | `/api/v1/download/clients` | 查询可用下载器 |
| GET | `/api/v1/download/paths` | 查询可用于下载接口 `save_path` 参数的下载路径 |
| DELETE | `/api/v1/download/{hashString}` | 删除下载任务，参数：`name` |

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
| GET | `/api/v1/system/moduletest/{moduleid}` | 测试指定模块可用性，保留原 `message`，并在标准响应顶层返回 `message_i18n` |
| GET | `/api/v1/message/agent/mcp/servers` | 管理员查询 Agent 外部 MCP 服务器配置 |
| POST | `/api/v1/message/agent/mcp/servers` | 管理员保存 Agent 外部 MCP 服务器配置 |
| POST | `/api/v1/message/agent/mcp/servers/test` | 管理员测试单个 Agent 外部 MCP 服务器并读取工具列表 |

#### 缓存管理

以下接口使用登录态鉴权，并要求当前用户为超级管理员。

| 方法 | 路径 | 说明 |
| :--- | :--- | :--- |
| GET | `/api/v1/tmdb/cache` | 查询 TheMovieDb 识别缓存及识别成功、失败条目统计 |
| DELETE | `/api/v1/tmdb/cache/{cache_key}` | 按缓存键删除单条 TheMovieDb 识别缓存，缓存键需要进行 URL 编码 |
| DELETE | `/api/v1/tmdb/cache` | 清空全部 TheMovieDb 识别缓存 |

### 插件补充接口

**GET** `/api/v1/plugin/history/{plugin_id}`

按需读取指定已安装插件的最新远端更新说明。该接口用于前端在用户点击“查看更新说明”时再实时访问插件仓库，避免加载已安装插件列表时批量请求网络。

### 1. 列出所有工具

**GET** `/api/v1/mcp/tools`

获取所有可用的MCP工具列表。

工具的 `inputSchema` 只包含实际执行业务所需的参数，不包含用于解释调用原因的通用 `explanation` 参数，以减少 Agent 上下文消耗。

媒体相关 MCP 工具（如 `query_media_detail`、`search_torrents`、`query_library_exists`、`add_subscribe`、`transfer_file`）接受 `tmdb_id`/`tmdbid`、`douban_id`/`doubanid`、`bangumi_id`/`bangumiid`、`anilist_id`/`anilistid`，也接受 `media_source` + `media_id`。工具返回的媒体、订阅、下载和整理记录会同步带回可用的四种专用 ID 及通用主身份。

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
[
  {
    "name": "add_subscribe",
    "description": "Add media subscription to create automated download rules...",
    "inputSchema": {
      "type": "object",
      "properties": {
        "title": {
          "type": "string",
          "description": "The title of the media to subscribe to"
        },
        "year": {
          "type": "string",
          "description": "Release year of the media"
        },
        ...
      },
      "required": ["title", "year", "media_type"]
    }
  },
  ...
]
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
  "result": "成功添加订阅：流浪地球 (2019)",
  "error": null
}
```

**错误响应示例**:
```json
{
  "success": false,
  "result": null,
  "error": "调用工具失败: 参数验证失败"
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
  "name": "add_subscribe",
  "description": "Add media subscription to create automated download rules...",
  "inputSchema": {
    "type": "object",
    "properties": {
      "title": {
        "type": "string",
        "description": "The title of the media to subscribe to"
      },
      ...
    },
    "required": ["title", "year", "media_type"]
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
  "type": "object",
  "properties": {
    "title": {
      "type": "string",
      "description": "The title of the media to subscribe to"
    },
    "year": {
      "type": "string",
      "description": "Release year of the media"
    },
    ...
  },
  "required": ["title", "year", "media_type"]
}
```
