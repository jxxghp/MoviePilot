# MoviePilot MCP 接入说明

本文面向通过 MCP 调用 MoviePilot 的外部 Agent 和工具开发者。接入后，客户端通过工具目录发现能力，按 JSON Schema 构造参数，并根据业务结果判断操作是否完成。

**最短调用路径：**配置连接与密钥 → `initialize` → `notifications/initialized` → `tools/list` → `tools/call`。已有 MCP 客户端通常会自动完成初始化和工具发现，只需配置下表中的连接信息。

### 插件安装前说明入口

插件目录的可选 `plugin_readme` 提供 HTTP(S) 文档外链，兼容索引中的 `homepage` 和 `project_url`。
详情界面通过管理员接口 `GET /api/v1/plugin/readme/{plugin_id}?repo_url=...` 查询当前来源，
返回 `{"url":"https://..."}`；链接未声明时按所选候选的实际代际探测 GitHub README，
缺失或超时返回 `{"url":null}`，不要求先安装插件。本地来源只返回显式声明的链接。
探测总时限为五秒，结果缓存五分钟；接口不返回文档正文，也不新增 Agent operation。

### 插件生命周期忙碌与超时

`plugin.runtime.status`（`GET /api/v1/plugin/runtime`）的可选 `active_operation` 返回当前
`operation`、`plugin_id`、`hook`、`elapsed_seconds` 与 `hook_elapsed_seconds`；空值表示没有
持有者。有在途变更或钩子时 `ready=false`，准备态数量仍由 `pending_count` 独立表达。
插件变更竞争共享执行权时在副作用前明确拒绝；安装的逐插件或启动准入最多等待五秒。
调用方超时、取消或停机超时不会终止第三方同步钩子，执行权、实例及数据库保留到真实结束。
遇到执行结果未知时先只读检查此摘要及插件实际状态，不能仅凭超时重复提交变更。

### 订阅下载的提交来源

新产生的订阅下载在订阅活动和日志中区分“首次订阅”“洗版升级”或“全集洗版”。
下载通知的标题和正文按通知模板渲染，不额外拼接来源标签。
该标记来自提交前订阅的已下载事实与最终选集，不改变过滤、优先级、选集和下载策略。
同一整包包含已下载集与缺集时计作一次洗版任务，分别保留 `initial_episodes` 和
`upgrade_episodes`；`best_version_full` 表示此次提交使用的全集洗版模式。

- `GET /api/v1/subscribe/{id}/activity` 的 `counts` 保留 `submitted` 总量，增加
  `initial_submitted`、`upgrade_submitted`、`full_upgrade_submitted`；最后一项是洗版计数的子集。
- 活动详情 `details.resources[].subscription_download` 保存 `subscription_id`、
  `submission_type`（`initial` / `upgrade`）、`best_version_full` 及两类集数。
  活动可从订阅历史继续查看，恢复重试按资源去重，不重复累计成功回执。
- `GET /api/v1/subscribe/execution/batches` 及其 `/{batch_id}` 详情返回 `download_counts`、
  `downloads` 和 `downloads_omitted`。分类数量按成功下载任务计数，与搜索任务完成数量独立；
  明细最多 100 条，裁剪不影响总量，访问权限沿用整批订阅归属校验。
- 下载历史的 `note.subscription_download` 保留同一来源，`note.source` 继续保留原入口信息。
  下载处理上下文在持久事件恢复后保留该来源，已冻结的通知保持模板渲染结果。

升级前历史没有来源标记，界面显示“未记录”，不会按当前订阅设置追溯推断。
普通订阅的首次提交与洗版订阅补缺在活动中都标记为首次订阅。

### WebAgent 会话状态

`POST /api/v1/message/agent/callback` 在返回前保存首次选择。`already_selected=true` 表示
该选择已领取执行，客户端仅恢复原反馈；已选但尚未执行时返回 `false`，可重试原选项。
后端按用户、会话和选择 ID 领取一次执行；入队前明确拒绝可重试，执行结果未知时不重放。
`echo_user=false` 仅隐藏用户回显，选择结果仍保存；完整快照替换保留隐藏选择收据，
即使旧快照不含卡片，也不能让后续快照把已选卡片改回 `pending`。普通消息仍按完整快照替换。

会话 `is_processing` 只表示实际执行或排队，空闲等待下一条消息的 worker 不计入。
工具事件沿用 `tool_id` 与 `assistant_message_id`，跨 steering 后仍更新原助手段。
全流结束收口所有助手段；缺少终态的结构化工具标为 `error`，旧文本提示保留完成兼容。

### 资源搜索诊断

Web 影视精确与标题 SSE 的 `manual_paging=true` 模式在最终 `done` 事件增加可选
`diagnostics`。V1 的 `scope=page` 仅覆盖当前页请求，续页沿用来源中的 `search_id`。
旧后端可缺省此字段，客户端应兼容 `null` 和未知原因代码。普通资源 API 的 `data` 数组
及 `GET /api/v1/search/last` 保持原合同。

- `searched_sites` 提供实际调度来源的 `site_id`、`site_name`、`attempted`、`outcome`
  和 `raw_count`。状态为 `success`、`empty`、`error`、`cooldown` 或 `unknown`；
  未发请求的冷却标记为 `attempted=false`。别名请求和摘要补取的数量按站点累加。
- `stage_counts` 包含候选数 `candidates` 和保留数 `retained`。`rejection_reasons`
  使用 `{code,count}`：`filter_params`、`filter_rules`、`title_missing`、`scope_mismatch`、
  `identity_mismatch`、`caller_filter`、`duplicate`。身份不符可能包含类型不符；
  索引器内部未报告的丢弃原因不作推测。
- `rule_groups` 包含 `name`、`status`、`reason`、`media_type` 和 `categories`。
  `effective` 表示规则适用，`skipped` 不增加拒绝数。标题搜索沿用过滤器的空媒体上下文。
- `warnings` 使用 `{code,rule_group?}`，代码包括 `rule_group_not_applicable`、
  `rule_group_missing`、`provider_error`、`provider_cooldown`、`provider_outcome_unknown`。
  原始异常、下载地址和凭据不进入诊断。

`GET /api/v1/search/last/context` 按认证用户返回完整的 `params`、`results` 和可选
`diagnostics`。同一用户并发搜索按完成顺序保存，异常或取消保留上次完整记录。普通
影视、标题、字幕 API 和旧 SSE 完成后也保存结果（含空结果），`diagnostics=null`；
字幕链接在读取时重新签名。旧全局缓存不迁移，升级后需重新搜索；首次搜索前返回
空参数、空结果和 `diagnostics=null`。

### 大集合查询窗口

`POST /api/v1/media/status` 接受 1 至 50 个媒体身份/标题/年份/季号查询，按输入顺序返回
`item_id`、`subscribed` 与同来源剧集的各季订阅模式 `seasons`。查询仅访问本地媒体库和
当前用户可见订阅，不调用外部媒体服务器，不返回完整订阅表。普通用户仍受 owner 限制，
影视保留标题、年份回退，音乐按实体类型隔离。该界面专用批量端点不新增 Agent 操作。

`search.recommend` 的 `check_only=true` 只读当前状态，不读取全量搜索结果缓存；
推荐禁用时也直接返回 `disabled`。`force=true` 保留优先级，确实启动任务时才读取候选。
前端轮询等待上次请求结束后再间隔五秒，后台标签暂停发包，离开资源页后停止轮询。

卡片搜索配置与 `system/env` 在前端共享最多三十秒的只读快照，并合并并发请求；
各表单持有独立副本。相关站点、系统设置和订阅写操作成功后，以及登录身份变化后，
快照立即失效，在途旧结果不能写回新会话；失败查询允许重试。

`GET /api/v1/site/userdata/{site_id}` 支持 `page` / `count`；趋势界面使用最近 90 条记录，
并传入 `latest_seeding_only=true`，仅窗口内最新记录携带 `seeding_info`，历史统计字段不变。
省略该选项时保留每条记录的完整明细；这不会删除或修改采集历史。

`GET /api/v1/site/category/{site_id}` 只返回分类项，不包含 `field`、`delimiter`、`param`
等搜索配置。分类 `id` 支持整数和文本，数字字符串沿用整数响应；客户端应保留返回值，
浏览资源时将所选分类用逗号拼接后传入 `site.resource` 的 `query.cat`，不要强制转为整数。

`GET /api/v1/torrent/cache` 支持可选 `page` / `count`，每页最多 200 条，省略两项时保留全量合同。
`title` 为不区分大小写的标题包含条件，`site` 为站点名称精确条件，均在分页前应用。
`brief=true` 将描述截至 200 字符，缓存 hash 仍由完整原文生成。
返回的 `count` / `sites` 是完整缓存统计，`total` 是筛选后数量，`site_names` 包含完整可选站点。
分页只限制明细构造和响应，不改变底层缓存读取语义。

`GET /api/v1/subscribe/calendar?start=2026-10-01&end=2026-11-01&page=1&count=50`
面向日历界面，按真实播出/上映日期过滤后排序、分页，返回 `items` / `total`。
`start` 包含当天，`end` 不包含当天，跨度最多 93 天，每页最多 200 条。
每项仅含订阅展示/入库摘要、日期、分集标题/集号及片长，不包含窗口外分集、简介和演职人员。
普通用户只能查询自己的订阅，管理员可查询全部；音乐订阅不进入影视日历。
详情查询最多四路并发，复用现有媒体预缓存及五分钟的有界精简缓存；冷缓存仍可能等待媒体源。
翻月或离开页面后客户端取消旧窗口，服务端探测断开后停止继续派发详情。
该界面专用聚合端点未增加 Agent 操作；Agent 的 `subscription.list` 合同不变。

### App 小组件只读接口

主 App 使用既有登录通过 `/api/v1/widget/context` 核对账号、`/api/v1/widget/registrations` 登记。
扩展使用独立的有限期只读 Bearer 通过 `/api/v1/widget/snapshot` 刷新。
该凭据不适用于普通 REST/MCP，小组件接口也不加入 Agent 工具目录；
详见[授权、权限与快照协议](widgets.md)。主登录凭据不得交给系统扩展。

### 通知配置与交互身份

`slash.run` 返回的是命令已触发，不代表后台命令或通知已完成。消息渠道发起的命令使用
宿主签名的渠道用户身份；请求头不能指定其他用户，渠道身份缺失或不匹配时会拒绝执行。
Web 会话沿用当前用户；无渠道调用不携带收件人，插件回执按既有管理员通知目标路由，
没有可用目标时不发送。

`GET /api/v1/message/notification` 的可空 `poster` 字段提供独立媒体海报。
新通知从已有媒体上下文提取海报；`image` 继续保留原有渠道图片语义。
旧通知及没有海报的通知返回 `poster: null`，客户端不应将背景图裁切成海报。

`POST /api/v1/notification/config` 保存通知渠道时要求名称和稳定 `id` 各自唯一。
改名应保留原 `id`；复制为新的渠道实例应使用新的 `id`，避免共享登录凭据。
通过消息渠道发起普通重启时，完成回执保留发起时的 `source` 配置。

`POST /api/v1/notification/app` 使用当前激活用户的既有 Bearer 登录态管理 AppPush，
请求包含 `action`（`status/register/unregister/pair/test`）、随机 `installation` 和实例 `origin`。
`register` 另需 APNs `token/topic/environment`；`source` 可选，用于选择启用的 AppPush 渠道。
账号通知身份由后台注入，客户端不能指定其他用户。注销同时撤销该安装配对的主屏幕通知网页。
前端仅在用户主动退出登录时请求 `unregister`；服务重启、认证失效及站点认证完成后的重新登录
只清理登录会话，保留通知绑定。`pair` 返回的绑定码十分钟有效且只能使用一次，完成绑定后无需保留或重新生成。
AppPush 向中心传递独立海报（旧消息回退到图片）；通知网页点开后显示本条通知的标题、正文和海报，
并提供 `moviepilot-app://notification` 链接。应先更新 Server，再更新 MoviePilot；图片由接收设备加载，系统通知中的图片展示以浏览器实际支持为准。
正文不再截取前1000个字符；Server 接受最多65536个字符，系统推送只携带预览和随机消息标识。
全文在 Server Redis 按设备绑定版本暂存，每个绑定最多20条、每条24小时；网页用自身绑定能力读取，
不在 URL 中传递正文或令牌。网页也短期暂存最近通知，用于冷启动、恢复和重载时恢复详情，解绑/换绑时清除；
超过暂存期限或读取失败会给出明确提示，永久历史仍在 MoviePilot 通知中心。
网页本地绑定记录缺失时，Server 可校验浏览器现存订阅的完整端点与密钥，恢复原设备能力；
这不需要生成新的绑定码，也不会更改 MoviePilot 登录态或建立另一份订阅。
AppPush 地址 `PUSH_SERVER_URL` 留空时使用 `MP_SERVER_HOST`；首次操作自动生成并先行持久保存256位实例凭据，再向中心幂等登记现有 `user_uid`。UID 只关联来源，不能据此获取、重置或认领已有凭据。内部 `AppPushCredentials` 按中心地址隔离，不进入通知渠道配置；中心只保存凭据摘要。已有 `PUSH_RELAY_KEY` 手工配置继续兼容。
`POST /api/v1/notification/app/instance` 仅容器超级管理员可调用，正文为 `{"action":"rotate","source":"已保存的渠道名称"}`。轮换保留设备，候选凭据先落库，响应丢失可在重启后接续；返回值不包含密钥。普通用户的 `/notification/app` 不接受该动作。
Apple/VAPID 私钥只放在中心 Server，不放入 App 或用户容器。
消息沿用现有类型开关和管理员/用户路由；“所有用户”范围会查询本实例所有启用账号的不可变通知身份，
逐账号投递到各自绑定的设备。停用账号和空身份不会进入接收集合，组合路由中已投递的账号不重复发送。
未经过该范围解析且缺少明确用户目标时不广播，其他渠道继续使用原有默认目标。部署与免费自签说明见
[MoviePilot-Server](https://github.com/jxxghp/MoviePilot-Server#私有-app-消息推送)。

Discord 仅指定 `userid` 的定向通知使用私聊；无法私聊时返回失败，不转发公共频道。
`/skills` 安装和删除按展示时的条目身份操作，刷新或翻页后的旧按钮会失效，应使用最新列表重新选择。

### 文件整理与恢复

自动选择目的目录时，整理请求应将 `target_path` 留空，由源存储、源目录、媒体类型和分类
匹配启用的存储目录配置；未匹配且没有显式目标路径时，整理会以“未找到有效的媒体库目录”拒绝。
`downloader` 为空不代表使用默认目标路径，也不会自动选择存储根目录。

显式传入 `target_path` 时，若未匹配目录配置，则保留自定义目标路径，并按请求的类型、分类
子目录开关拼接路径；例如 `/media` 可得到 `/media/电影/华语电影`，不会自动补入 `links`。
该分支在预览和实际规划时均输出 WARNING，包含源存储与路径、传入的目标根路径及计算后的目标路径。
需要使用已配置的库路径时，请选择自动匹配，并在执行前通过整理预览核对目标位置。

整理成功历史保存 `episode_group`；批量复用历史时仅恢复所有记录共同的剧集组，
不同剧集组应分别提交。目录整理的叶子文件同样遵守冻结的覆盖策略，
`overwrite_skipped` 表示策略拒绝，不能当作文件已导入。

`FileItem` 的可选 `modify_time_ns` 和 `change_time_ns` 保存本地纳秒版本证据，
JSON 使用十进制字符串避免浏览器数字精度丢失，客户端应原样保留查询返回值，不自行生成。
恢复清理发现本地源或目标版本变化时会保留副本并转人工复核；
云盘时间戳变化仍按大小与稳定文件标识核验。OpenList 中断或大小不符的下载不会作为移动成功结果删除源文件。
`storage.rename` 的 `recursive=true` 会识别并重命名所有子目录内媒体，随后重命名所选目录。

## 1. 连接与认证

| 配置项 | 值与含义 |
| --- | --- |
| 服务地址 | `https://<MoviePilot 后端地址>/api/v1/mcp`；使用实际可达的后端地址，反向代理有前缀时需一并保留 |
| 传输方式 | Streamable HTTP，向同一个地址 POST JSON-RPC 2.0 消息 |
| 认证请求头 | `X-API-KEY: <API_TOKEN>`，值为该 MoviePilot 实例配置的 `API_TOKEN` |
| 请求类型 | `Content-Type: application/json` |
| 接受类型 | 客户端可发送 `Accept: application/json, text/event-stream`；当前服务端以 JSON 返回调用结果 |
| Schema 模式 | 默认 `full`；客户端无法处理大型 Schema 时，增加 `X-MCP-Schema-Mode: compact`，详见[参数合同](#3-获取和使用参数合同) |

客户端必须能够配置远程 HTTP MCP URL 和自定义请求头。不同客户端的配置文件格式不同，将上述值填入其对应配置项即可。这里的服务地址是 HTTP 入口，不是本地进程启动命令。

整理失败的 AI 接管应先读取 `transfer.history` 并核实媒体身份；对持久化任务使用
`transfer.history.replan` 提交 `history_id`、接管时的 `task_id`/`settlement_revision`、`media_source`、`media_id`、`media_type`
以及可选的 `season`/`episode`。该操作只接受没有文件副作用的识别拒绝，保留原任务、失败证据、结算回执和重试预算；排队受理不代表文件已经整理完成。

认证补充：

- MCP 使用固定 API 密钥，不使用网页登录的 Bearer JWT，也不提供 OAuth 登录流程。
- 无法设置请求头的客户端可使用 `.../api/v1/mcp?apikey=<API_TOKEN>`。优先使用请求头，避免密钥进入 URL 日志与历史记录。
- 每个请求都需要认证，包括初始化、发现工具和终止会话。密钥校验还要求实例中存在有效的超级管理员账号。
- 这是**管理员级集成入口**，可以访问站点、配置、数据库及有副作用的操作。只向可信客户端提供密钥，并使用 HTTPS 或受信网络。
- 写操作的用户授权由调用方负责取得；不要假设 MCP 会弹出 MoviePilot 内置 Agent 的确认界面。隐藏工具也不构成权限隔离。

REST 兼容集成接口 `GET /api/v1/subscribe/list` 使用 `X-API-KEY: <API_TOKEN>`，
也接受 `?token=<API_TOKEN>`；缺失、错误凭据或绑定的管理员身份失效时返回 HTTP `401`。
认证成功后可读取全部用户的订阅及总量。Agent 的 `subscription.list` 使用当前用户身份访问
`GET /api/v1/subscribe/`，普通用户只能读取自己的订阅。

下文的 HTTP 示例使用两个占位环境变量，由接入方在自己的环境中设置。不要把真实密钥写进文档、模型提示词或代码仓库。

```bash
MP_MCP_URL='https://moviepilot.example.com/api/v1/mcp'
MP_API_KEY='<该实例的 API_TOKEN>'
```

## 2. 完成一次最小调用

### 2.1 初始化

```bash
curl -sS "$MP_MCP_URL" \
  -H "X-API-KEY: $MP_API_KEY" \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  --data '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-11-25","capabilities":{},"clientInfo":{"name":"example-agent","version":"1.0.0"}}}'
```

成功响应位于 `result`，其中包含 `protocolVersion`、`capabilities`、`serverInfo` 和 `instructions`。服务端支持 `2025-11-25`、`2025-06-18`、`2024-11-05`；请求版本不在支持列表时返回默认版本 `2025-11-25`，客户端应检查是否能继续使用该版本。

随后发送初始化完成通知，**不携带 `id`**：

```json
{
  "jsonrpc": "2.0",
  "method": "notifications/initialized",
  "params": {}
}
```

使用与初始化相同的地址和认证头 POST。当前实现返回 HTTP `204`，没有 JSON 正文。

### 2.2 发现工具

```bash
curl -sS "$MP_MCP_URL" \
  -H "X-API-KEY: $MP_API_KEY" \
  -H 'Content-Type: application/json' \
  --data '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}'
```

读取 `result.tools`。每项包含：

| 字段 | 客户端用途 |
| --- | --- |
| `name` | `tools/call.params.name` 必须使用的精确工具名 |
| `description` | 工具用途、边界与调用提示 |
| `inputSchema` | 本次部署实际提供的 JSON Schema，决定参数名、类型、必填项和可选操作 |

工具目录可能随插件安装、启停或卸载而改变。当前 `capabilities.tools.listChanged=false`，不会主动推送目录变更通知；重连、插件变更或遇到工具不存在时重新发现。不要将本文中的示例当成完整工具清单。

### 2.3 调用一个只读工具

下面查询第一页订阅，不创建或修改订阅：

```bash
curl -sS "$MP_MCP_URL" \
  -H "X-API-KEY: $MP_API_KEY" \
  -H 'Content-Type: application/json' \
  --data '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"moviepilot_api","arguments":{"operation_id":"subscription.list","query":{"page":1,"count":20}}}}'
```

MCP 响应的外层结构如下。`text` 中的内容为示意业务结果：

```json
{
  "jsonrpc": "2.0",
  "id": 3,
  "result": {
    "content": [
      {
        "type": "text",
        "text": "{\"collection\":{\"result_count\":0,\"total_count\":0,\"page\":1,\"count\":20},\"success\":true,\"message\":\"\",\"data\":[]}"
      }
    ]
  }
}
```

`result` 表示协议调用已返回，业务结果在 `content` 的文本块中。文本可解析为 JSON 时再读取其中的 `success`、`error`、`data` 和执行状态；不要仅凭 HTTP `200` 或存在 `result` 判断业务成功。完整处理规则见[结果与恢复](#6-处理结果分页与失败恢复)。

### 2.4 传输约定

| 方法 | 行为 |
| --- | --- |
| `POST /api/v1/mcp` | 每次发送一个 JSON-RPC 对象；不支持批量数组请求 |
| JSON-RPC `ping` | 返回空对象 `result: {}`，可用于检查协议连通性 |
| `DELETE /api/v1/mcp` | 认证后返回 `204`；当前为无状态模式，无需维护服务端 Session ID |

当前没有供旧式 HTTP+SSE 客户端使用的 GET 事件流或独立消息投递地址，也不提供 `resources/list`、`prompts/list`。客户端应选择向同一 URL POST 的 HTTP 传输模式。

## 3. 获取和使用参数合同

### 完整模式：按 operation / action 选择分支

默认 `tools/list` 返回完整 Schema。四个主要入口使用 `oneOf` 表达不同操作的独立参数合同：

1. 根据任务选择工具。
2. `moviepilot_api` 按 `properties.operation_id.const` 选择分支；其余三个入口按 `properties.action.const` 选择分支。
3. 根据该分支的 `required`、`properties`、`enum`、默认值和跨字段规则构造参数。分支中的 `$ref` 应在**整份 Schema** 的 `$defs` 中解析。
4. 只传该操作声明的字段；JSON 字符串、数字、布尔值、数组、对象和 `null` 必须保持正确类型。
5. 若已获得完整参数合同且目标明确，直接调用。无需为每次操作额外执行能力探测，也不要用缺参请求试探写操作。

外部 MCP 客户端不需要访问 MoviePilot 本地文件，也不会自动获得内置 Agent 的 Skill 内容。工具说明中提到的 Skill 可作为补充阅读；接入所需参数以当前部署返回的 Schema 为准。

### 紧凑模式：为受 Schema 体积限制的客户端使用

在 MCP 请求头中增加：

```text
X-MCP-Schema-Mode: compact
```

该模式只改变工具发现时的 Schema 展示：超过客户端压缩阈值的大型 Schema 会去掉组合分支和定义表，保留顶层对象、必填字段及 operation/action 枚举。发生投影时会出现 `x-moviepilot-schema-projection` 标记。

**紧凑模式不包含每个操作的完整参数，也不承诺 Schema 一定小于客户端体积限制。** 服务端实际参数校验、身份与权限规则不变。调用前通过以下 HTTP 辅助端点取得完整 Schema，或复用已缓存且与当前部署一致的合同：

```bash
curl -sS "$MP_MCP_URL/tools/moviepilot_api/schema" \
  -H "X-API-KEY: $MP_API_KEY"
```

这个辅助端点属于 REST，完整 Schema 位于响应的 **`data`**。如果客户端侧有 `jq`，可只保留所需分支及其依赖定义，减少交给模型的内容：

```bash
curl -sS "$MP_MCP_URL/tools/moviepilot_api/schema" \
  -H "X-API-KEY: $MP_API_KEY" \
  | jq --arg op 'media.detail' '.data | .oneOf |= map(select(.properties.operation_id.const == $op))'
```

上例假设 `MP_MCP_URL` 不包含查询参数；使用 URL 密钥认证时，需先构造完整路径，再附加 `?apikey=...`。不要向 `tools/call` 提交 Schema 或把 `$defs` 当成业务参数。

## 4. 选择工具

| 任务 | 主要入口 | 选择依据 |
| --- | --- | --- |
| 媒体搜索、识别、订阅、站点资源搜索、下载、整理、历史、站点、插件、配置、调度、工作流 | `moviepilot_api` | MoviePilot 自身业务流程的首选入口 |
| 下载器任务、文件选择、队列、标签、限速、Tracker、校验 | `downloader_operation` | 直接操作已配置下载器的原生能力 |
| 媒体库浏览、播放会话、近期入库、扫描和元数据刷新 | `mediaserver_operation` | 直接操作已配置媒体服务器的原生能力 |
| 数据库表、实时字段、SQL 统计与明确授权的数据修复 | `database_operation` | 业务 API 无法满足时使用；普通配置修改优先走业务 API |
| 插件或其它运行时扩展能力 | `tools/list` 中对应工具 | 按返回的精确名称、说明和 Schema 调用 |

不要由此推断任意 REST 路由、第三方 API 或本地命令都能通过 MCP 调用。目录未提供的能力应明确说明不可用。`execute_command`、文件读写、`read_skill` 等内部工具不向 MCP 开放。

### 4.1 `moviepilot_api`：产品业务操作

工具参数最多使用以下四个顶层字段，具体是否允许或必填由所选分支决定：

| 字段 | 含义 |
| --- | --- |
| `operation_id` | 精确的白名单操作标识，例如 `media.search`、`subscription.list` |
| `path_params` | 资源标识等路径参数 |
| `query` | 筛选、分页等查询参数 |
| `body` | 该操作声明的 JSON 请求体 |

以下示例均为 `tools/call` 的 **`params`**，发送时仍需外包 `jsonrpc`、`id`、`method`。示例标识必须替换为前序查询返回的真实标识。

搜索媒体：

```json
{
  "name": "moviepilot_api",
  "arguments": {
    "operation_id": "media.search",
    "query": {"title": "盗梦空间", "media_source": ["tmdb"], "page": 1, "count": 5}
  }
}
```

按来源和媒体标识读取详情：

```json
{
  "name": "moviepilot_api",
  "arguments": {
    "operation_id": "media.detail",
    "path_params": {"media_id": "27205"},
    "query": {"media_source": "tmdb", "type_name": "电影"}
  }
}
```

构造业务调用时遵守以下规则：

- 不传 `url`、`method`、请求头或认证凭据；固定 operation 由服务端映射到业务接口。
- 不将 `media_id` 等业务字段平铺到顶层，也不为无请求体的操作补 `body: {}`。
- `body` 使用原生 JSON 对象或数组；只有合同允许时才传 `null`。当前字符串请求体仅用于 `system.upgrade.dev` 的固定值 `"dev"`。
- 若模型把整个 `body` 编码成 JSON 字符串，各工具入口会在校验和发送前还原一层对象、数组或 `null`；嵌套字符串保持原值，非法请求体仍按 operation 合同拒绝。客户端应直接传原生 JSON 值。
- 媒体标识必须与返回它的 `media_source` 配对。不同操作的类型字段与枚举可能不同，不要把 `movie`、`电影` 或 `type`、`mtype`、`type_name` 相互套用。
- 下载、整理、订阅等操作优先复用前序查询返回的媒体上下文、种子上下文、存储项目和精确 ID，不根据标题猜测资源身份或文件路径。

业务操作的完整分类参考见 [MoviePilot API Skill](../skills/moviepilot-api/SKILL.md) 和其 [API 分类目录](../skills/moviepilot-api/api/)。运行时 Schema 才是目标实例的可调用范围。

### 4.2 `downloader_operation` / `mediaserver_operation`：原生服务操作

下载器示例：

```json
{
  "name": "downloader_operation",
  "arguments": {
    "client": "main-qb",
    "action": "tasks.list",
    "arguments": {"status": "downloading", "offset": 0, "limit": 20}
  }
}
```

媒体服务器示例：

```json
{
  "name": "mediaserver_operation",
  "arguments": {
    "server": "living-room",
    "action": "activity.latest",
    "arguments": {"limit": 20}
  }
}
```

注意这里有两层 `arguments`：外层是 MCP 工具输入，内层是所选 `action` 的参数。

- `client` / `server` 是 MoviePilot 中已配置实例的精确名称，不是地址、类型或凭据。
- 已知实例名称时直接传入；未知时可省略。下载器使用默认或唯一启用实例，媒体服务器使用唯一启用实例。存在歧义时结果会列出有效名称，按用户目标选择后再调用。
- 若需要主动查看实例，使用 `action: "instances.list"`；查看一个动作的能力与参数时使用 `action: "capabilities.list"`，内层参数为 `{"action_name":"<精确动作>"}`。
- 动作是否可用取决于实际提供方。下载器任务 ID、媒体服务器项目 ID 与 MoviePilot 业务记录 ID 不可互换。
- 原生服务操作的结果只证明该服务的状态；例如下载器提交成功不等于 MoviePilot 已完成整理或入库。

完整动作参考：[下载器](../skills/downloader-operation/SKILL.md)、[媒体服务器](../skills/mediaserver-operation/SKILL.md)。

Emby、Jellyfin、Plex、Navidrome 的目录、条目、搜索、季集和活动查询支持内层可选 `query`，例如
`{"parent":"movies","limit":50,"query":{"fields":["identity","path","year"]}}`。
`fields` 省略或为 null 时保留原内容；空数组只取当前操作必需的信息。输入 schema 从
`MediaServerQueryOptions` 推导，内容组与服务端能力边界见 [媒体查询内容契约](media-server-query.md)。
调用方不能据此假定每种私有协议都支持完整的远端字段裁剪。

### 4.3 `database_operation`：直接数据库访问

先发现真实表名和字段，再构造 SQL：

```json
{
  "name": "database_operation",
  "arguments": {"action": "tables", "arguments": {}}
}
```

| `action` | 内层 `arguments` |
| --- | --- |
| `tables` | 空对象，返回实际表清单 |
| `schema` | `table_name`，必须使用表清单中的精确名称 |
| `query` | `sql` 与 `file` 二选一；`limit` 默认 100，范围 1–200；默认只读，`write` 默认 `false`；`show_secrets` 默认 `false` |
| `write` | `sql` 与 `file` 二选一；执行单条写入或结构变更语句，必须已有对应用户授权；引用 `systemconfig` 时同样需要 `show_secrets=true` |

`file` 指 MoviePilot 运行环境中的 SQL 文件，不是客户端本地文件；外部接入通常直接传 `sql`。工具自行读取 MoviePilot 数据库配置，不接受任意连接串或数据库密码。

`systemconfig` 集中存放实例级 API Key、Cookie 与口令，语句只要引用该表（含引号标识符、模式限定名和 CTE 写法）就被拒绝，除非显式传 `show_secrets=true`。该门禁按表生效，因此计数或只取键名也需要开启；宿主会把这类读取判定为敏感读取并要求确认，回执中不保留结果内容。读取普通设置仍应使用 `config.system.*`。 SQLite 另在执行连接上校验真实表访问，单引号表名与视图转引也不能绕过；语句结束后撤销临时授权回调。

读取行时使用必要字段和有限窗口；写入前核对目标与条件，完成后只读核验。普通设置修改使用 `config.system.*`，避免绕过配置校验、事件通知和专用业务流程。更多约束见[数据库操作参考](../skills/database-operation/SKILL.md)。

## 5. 组合业务调用

以下是调用顺序指引。具体参数仍按各 operation 的 Schema 构造，不将顺序中的操作全部默认执行。

| 用户目标 | 调用路径与完成依据 |
| --- | --- |
| 查找影片并查看详情 | `media.search` → 用户目标对应的精确媒体身份 → `media.detail` |
| 检查库存后订阅 | 确认媒体身份 → `library.exists` / `subscription.find` → 按已授权意图调用 `subscription.add` → 查询订阅核验 |
| 搜索并下载站点资源 | `search.torrents` 或 `search.title` → 从结果选择种子上下文 → `download.add` → 查询下载任务；下载和整理状态另行核验 |
| 搜索已有订阅 | 读取订阅 → `subscription.search` → 按返回批次标识调用 `subscription.execution.get`；不要把触发搜索当成下载完成 |
| 修改系统设置 | `config.system.list` 发现精确键 → `config.system.describe` 读取合同、当前值与 `revision` → `config.system.update` → 重新读取核验 |
| 操作插件能力 | `plugin.installed` / `plugin.capabilities` 或当前工具目录 → 明确插件实例 → 对应业务 operation 或动态工具 → 查询结果 |

补充规则：

- 设置更新优先回传 `expected_revision`。发生冲突时重新读取并比较，不直接覆盖；`generic_write_allowed=false` 时使用返回的专用操作。
- 插件存在分身时，显式选择目标实例或使用已经配置的默认目标。遇到实例歧义、停用目标或插件门禁拒绝时，按错误处理，不猜测另一个实例。
- 参数省略、空字符串与 `null` 的含义由具体操作决定。例如更新订阅时，省略字段通常保留原值，允许清空的字段可使用空值；不要把整份读取结果不加筛选地回写。
- `agent_task` 更新的 `enabled:null` 保留原启用状态，仅重命名暂停任务不会恢复调度。MCP 工具发现和 Google 模型发现会读取全部分页；重复游标或超过分页上限时报告失败。
- `agent_task` 创建时固定真实执行账号；渠道入口须唯一绑定启用账号。后台查询与管理员操作沿用同一账号权限，身份失效记录为失败。升级前的任务须从原会话 `update` 确认绑定或重新创建，详见[自主任务执行账号](agent-execution.md#自主任务执行账号)。
- 传统消息会话按渠道、配置接入源与原始用户 ID 隔离，停止、清除、状态和确认操作使用同一范围。WebAgent 后台完成追加本轮展示历史，前端完整快照仍按原合同替换。
- Web 智能助手的临时附件按创建者账号隔离；下载、语音转写和附件事件只接受同一账号登记的引用，跨账号访问按不存在处理。
- Agent 文件工具的禁止覆盖创建使用排他提交；同目标并发版本修改只允许仍匹配原 SHA 的请求提交。冲突后重新读取当前文件再编辑。`view_image` 使用只读权限，普通用户可读取允许范围内的公共技能图片。
- 安装市场技能保留用户明确选择的 GitHub 分支，该分支不可用时不改装默认分支。Agent 运行时人格文件变化使缓存失效；关闭 Agent 配置会通过能力 owner 停止服务。
- 订阅身份包含用户名、媒体类型、来源、媒体 ID、季号、剧集组和音乐实体类型。同编号的电影与电视剧可分别订阅；按来源定位时保留剧集组。同用户并发创建同一目标会复用真实订阅 ID。
- 更新订阅的作品、季号、剧集组或音乐实体后，旧目标的下载与洗版进度在同一事务清除，缺失集数按新总集数重新开始；显式清空剧集组也属于目标切换。目标身份已存在时取消该次修改，原有两条订阅及进度保持不变。
- Servarr 重复添加已监控的剧集返回真实既有订阅 ID。搜索任务取消后若执行者丢失，租约到期会结算取消并释放活动占位，再次搜索可重新入队。
- 工作流恢复会重试失败节点及因失败跳过的节点，成功节点继续复用结果；文本 `content` 会随其他上下文完整恢复。编辑暂停工作流不会重新注册定时器，Cron 使用宿主调度时区；并行分支保存去重缓存时合并各自记录。
- 自定义过滤规则的新增、修改或删除遇到并发快照冲突时返回业务失败。先重新读取规则集合，再重新确认修改，不用旧集合覆盖已成功提交的配置。
- `search.torrents` 可能执行多轮站点搜索，工具等待上限为 300 秒；客户端应为此设置足够的超时。分页只限制返回结果，不会缩短搜索过程。

## 6. 处理结果、分页与失败恢复

### 分层判断成功

客户端依次检查三层结果：

1. **HTTP 与 JSON-RPC：**检查状态码及顶层 `error`。认证或协议失败时不进入业务结果解析；通知成功的 `204` 没有正文。
2. **MCP 工具：**检查 `result.isError`，读取 `result.content` 中的文本块。当前工具返回文本，不能假定存在 `structuredContent`。
3. **业务与执行状态：**文本为 JSON 时检查 `success: false`、`error`、`execution_outcome` 及具体业务状态。即使 `isError` 缺失，文本内仍可能是业务失败。

`execution_outcome: "pending"` 表示已提交但尚未确认最终完成；`"unknown"` 表示效果未知，先用只读操作核验实际状态。没有该字段时，依据具体业务响应判断，不能自动推断异步任务完成。

`media.scrape` 遇到不支持的媒体文件或 NFO 保存失败时返回 `success: false` 及原因，目录中的媒体保存失败也会向上传递。不要将这种响应报告为刮削完成。

### 分页与截断

- 按选定操作的合同使用分页字段。例如 `subscription.list` 使用 `page` / `count`，`subscription.execution.list` 使用 `limit`，原生服务列表常用 `offset` / `limit`。
- 支持兼容分页的列表，两项均省略时仍可能返回全量；显式传 `page` 或 `count` 才开启分页，缺失值分别按 1 和 50 处理，`count` 最大 200。
- `moviepilot_api` 的 `collection` 是附加元数据，不替换 `data`。`result_count` 是本次返回数量，`total_count` 仅在服务端知道精确总数时出现；缺失不等于零。
- 只求总数时，对提供精确总数的操作请求最小窗口，例如 `page=1,count=1`，读取 `collection.total_count`。不要将当前页长度当成总数。
- 工具结果可能被截断。看到 `tool_result_truncated: true` 时，`content_preview` 只是预览；按回执提示缩小查询、分页，或在提供结果引用与读取工具时继续读取。不要因预览不完整而重新执行写操作。

### 常见失败

| 信号 | 处理方式 |
| --- | --- |
| HTTP `401` | 检查地址对应实例、`API_TOKEN`、请求头是否被代理保留，以及超级管理员账号是否有效 |
| HTTP `503` | 认证等运行时服务可能尚未初始化；先检查实例就绪状态 |
| JSON-RPC `-32700` | 请求正文不是有效 JSON |
| JSON-RPC `-32600` | 请求不是 JSON-RPC 2.0 对象，或错误地给 initialized 通知添加了 `id` |
| JSON-RPC `-32601` | 方法未实现；检查是否误用旧 SSE、resources 或 prompts 接口 |
| JSON-RPC `-32602` | 请求参数不符合协议入口要求，按错误详情修正 |
| JSON-RPC `-32603` | 服务端内部错误，保留请求 ID 与脱敏错误信息排查 |
| 业务 `invalid_input` | 按返回的 `input_contract` 一次修正缺失、位置、类型或未声明字段；不要添加猜测参数 |
| 工具或 operation 不存在 | 重新读取工具目录和完整 Schema，选择当前部署提供的能力 |
| 权限拒绝或配置修订冲突 | 核对身份、授权范围或当前修订，不能通过 SQL 或另一个接口绕过 |
| 超时、连接中断、`pending` / `unknown` | 对有副作用的操作先查实际状态，避免重复下载、订阅、删除或提交任务 |

网关参数校验失败通常携带 `input_contract`；更外层的解析失败或其它工具不保证提供该字段，必要时回到完整 Schema。不要把全部失败归结为可直接重试的网络问题。

## 7. HTTP 辅助端点

不使用 MCP SDK 的程序也可以通过这些端点发现和调用同一套工具。所有端点使用相同 API Key 认证，响应遵循普通 REST 包装 **`{"success": ..., "message": ..., "data": ...}`**，与 `/mcp` 的 JSON-RPC 包装不同。

| 方法与路径 | 请求或返回内容 |
| --- | --- |
| `GET /api/v1/mcp/tools` | `data` 为工具数组，包含完整 `inputSchema`；可选 `page` / `count` |
| `GET /api/v1/mcp/tools/{tool_name}` | `data` 为单个工具的名称、说明与完整 `inputSchema` |
| `GET /api/v1/mcp/tools/{tool_name}/schema` | `data` 为完整输入 Schema，即使 MCP 使用 compact 也不受影响 |
| `POST /api/v1/mcp/tools/call` | 请求字段为 `tool_name`、`arguments`；成功分发时 `data.result` 为工具结果文本 |

REST 调用示例：

```json
{
  "tool_name": "moviepilot_api",
  "arguments": {"operation_id": "subscription.list", "query": {"page": 1, "count": 20}}
}
```

注意 REST 使用 `tool_name`，MCP `tools/call.params` 使用 `name`。REST 外层 `success: true` 同样不代表内层业务成功，仍需解析 `data.result`。

## 8. 参考与维护

- [业务 API 操作参考](../skills/moviepilot-api/SKILL.md)：按领域查阅业务操作与约束。
- [下载器操作参考](../skills/downloader-operation/SKILL.md)、[媒体服务器操作参考](../skills/mediaserver-operation/SKILL.md)、[数据库操作参考](../skills/database-operation/SKILL.md)：原生服务与 SQL 操作合同。

本文维护连接方式、发现流程、调用结构和结果处理。字段清单、操作全集、提供方差异由运行时 Schema 及对应参考维护；新增业务能力时不应继续向本文追加 REST 路由表、前端展示契约或版本变更记录。
