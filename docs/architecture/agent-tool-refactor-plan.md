# MoviePilot Agent 工具体系重构计划

> 状态：COMPLETE — L1-L10 全部 VERIFIED，正式方案已提交推送并通过远端 CI
>
> 建立日期：2026-08-31
>
> 当前基线：v3@91e33277f3a05dc10b8984a2e612d8a32441c987
>
> 关联目标：本线程已建立的 Agent 工具体系重构 Goal

## 1. 战略目标

在不削弱 MoviePilot Agent 智能化、权限安全、会话交互、后台任务和失败恢复能力的前提下，
重构当前过大的 Agent 工具目录：

- 用受控、结构化的 MoviePilot API 网关承载稳定业务能力
- 用按领域 Skill 提供调用流程、参数语义和 API 操作范围
- 用受控 Skill 脚本直接承载下载器、媒体服务器的低层第三方 API 操作
- 迁移可以由 REST API 完整表达的业务工具，删除重复的 Agent Tool Schema
- 补齐搜索会话、规则校验、安全设置和插件数据等 API parity
- 保留依赖当前会话、渠道、宿主或流式状态的原生能力
- 保持原有权限、确认、幂等、审计、结果裁剪和可恢复语义

最终目标不是建立一个可执行任意 URL 的万能 curl 工具，而是建立一个由操作白名单、身份透传、
策略分类和结果投影共同约束的 API 能力面。

下载器和媒体服务器采用混合边界：设备级查询、诊断和任务控制可以由 Skill 的受控脚本
读取本机配置后调用第三方自身 API；涉及 MoviePilot 媒体身份、路径映射、下载历史、订阅、
转移和跨服务器聚合的高层用例继续由 MoviePilot Application/API 承担。

## 2. 基线与当前实现事实

| 项目 | 初始基线 | 当前实现 |
|---|---|---|
| 工厂固定工具元组 | 82 个 | 12 个原生工具；默认再追加 send_local_file 与 moviepilot_api，共 14 个 |
| 工具目录默认行为 | LLM_MAX_TOOLS=0，默认将完整目录绑定到主模型 | 默认完整目录已收敛为 14 个；按钮选择和语音发送仅在渠道能力满足时条件注入 |
| 默认固定目录体积 | 工具名称、描述和 JSON Schema 合计约 113 KB | 实测 23,865 bytes，约下降 79% |
| API Skill | skills/moviepilot-api/SKILL.md 通过任意 method/path 调用 | moviepilot_api 只接受 203 个固定 operation ID；Skill 与 MCP oneOf 逐 operation 暴露精确英文参数合同 |
| API 身份 | API Token 映射为管理员级集成身份，不等于 Agent 当前用户/渠道身份 | Web/渠道身份绑定到真实用户；HTTP/MCP 管理入口绑定持久化超级用户，不接受模型注入 Token |
| 下载器/媒体服务器 | 依赖内置低层 Agent 工具或有限 REST operation | downloader-operation 与 mediaserver-operation Skill 通过固定脚本调用已配置 provider API |
| MCP/HTTP 工具管理 | 存在旧业务工具与同名 first-wins 选择空间 | 与主 Agent 共用严格唯一新目录；重名直接以 TOOL_IDENTITY_AMBIGUOUS 失败 |
| 退役代码 | 旧实现仍位于 app/agent/tools/impl | 77 个退役文件已直接删除，其中 72 个工具模块、5 个辅助模块 |
| 架构图 | 982 个宿主模块、8,430 条内部依赖边 | 919 个宿主模块、7,690 条内部依赖边，Application/Chain 具体 Adapter 直连仍为 0 |
| 工作区状态 | 基线提交 871632af，与 origin/v3 对齐，初始工作区干净 | L1-L10 已完成并通过远端 CI；2026-09-01 继续修复集合总数在大结果截断预览中不可见的问题 |

## 3. 目标工具分层

### 3.1 第一批迁移：现有 API 基本覆盖的 48 个工具

| 领域 | 工具 |
|---|---|
| 媒体 | search_media, search_person, search_person_credits, recognize_media, scrape_metadata, query_episode_schedule, query_media_detail |
| 订阅 | add_subscribe, update_subscribe, search_subscribe, query_subscribes, query_subscribe_shares, query_popular_subscribes, query_subscribe_history, delete_subscribe |
| 下载/历史 | add_download_tasks, query_download_tasks, delete_download_tasks, delete_download_history, delete_transfer_history, query_downloaders |
| 站点 | query_sites, update_site, query_site_userdata, test_site, update_site_cookie |
| 推荐/媒体库/存储 | get_recommendations, query_library_exists, query_library_latest, query_directory_settings, list_directory, query_transfer_history, transfer_file |
| 调度/工作流 | query_schedulers, run_scheduler, query_workflows, run_workflow |
| 插件 | query_installed_plugins, query_market_plugins, query_plugin_capabilities, query_plugin_config, update_plugin_config, reload_plugin, install_plugin, uninstall_plugin |
| 命令/配置 | list_slash_commands, query_custom_identifiers, update_custom_identifiers |

### 3.2 第二批迁移：补齐 API parity 后的 16 个工具

- 搜索会话：search_torrents, get_search_results
- 下载器高级修改：update_download_tasks
- 过滤规则和规则组：9 个查询、增加、更新、删除工具
- 插件持久化数据：query_plugin_data
- 安全系统设置：query_system_settings, update_system_settings
- 斜杠命令执行：run_slash_command

### 3.3 原生能力最终保留

- 外部能力：search_web, recognize_captcha, browse_webpage
- 当前渠道消息：send_message, send_local_file；ask_user_choice 与 send_voice_message 按渠道能力条件注入
- Agent 会话任务：agent_task 单工具 action=create/list/update/run/delete
- Agent 人格状态：persona 单工具 action=list/switch/update
- 宿主能力：execute_command, edit_file, apply_patch, write_file, read_file
- 诊断能力：query_doctor_report

此外，渠道工具、Skill 工具、活动日志、子 Agent 和插件/MCP 动态工具继续按运行时条件注入。

### 3.4 最终目录规模

- 工厂固定工具元组由 82 个降为 12 个原生工具。
- 默认运行目录再追加 send_local_file 与单一 moviepilot_api 网关，共 14 个固定工具。
- 其中 13 个是原生能力，1 个是 203-operation 结构化 API 网关。
- ask_user_choice、send_voice_message、Skill、渠道、子 Agent、插件和外部 MCP 工具仍按运行时条件注入，不计入 14 个默认固定工具。
- 77 个退役实现/辅助文件直接删除，不保留 Agent 或 MCP 兼容副本。

### 3.5 下载器与媒体服务器第三方 API Skill

评估结论：可行，纳入正式目标，但采用“低层直连、高层保留”的边界。

| 能力 | 最终承载方式 | 原因 |
|---|---|---|
| 下载器实例发现、连接诊断、任务列表和详情 | downloader-operation Skill + 受控脚本 | 属于 qBittorrent、Transmission、rTorrent 自身能力，适合按 provider 扩展 |
| 下载器暂停、恢复、限速、标签、分类、Tracker 和删除 | downloader-operation Skill + 受控脚本 | 参数差异可在脚本适配层显式归一；删除文件仍需明确确认 |
| 媒体服务器实例、用户、媒体库、条目、最近入库和扫描诊断 | mediaserver-operation Skill + 受控脚本 | 属于 Emby、Jellyfin、Plex 等服务自身查询面 |
| 媒体库刷新/扫描 | mediaserver-operation Skill + 受控脚本 | 允许按 provider 能力执行，属于外部副作用并要求确认 |
| 下载提交、种子与站点 Cookie、保存目录选择 | MoviePilot API | 必须保留站点、下载选择、路径映射、历史和失败恢复语义 |
| library.exists 精确去重 | MoviePilot API | 必须保留 canonical media_source/media_id、音乐匹配和多服务器聚合 |
| 下载/转移历史、订阅状态、自动转移 | MoviePilot API | 属于 MoviePilot 持久化与业务编排，不是第三方设备 API |

Skill 脚本必须满足：

- 从 MoviePilot 本机配置读取连接参数，模型参数中不得出现服务器 URL、用户名、密码或 Token
- 只允许脚本声明的 provider、实例名和 action，禁止任意 URL、任意 HTTP method 和任意请求头
- 优先复用现有 provider client/SDK 和配置解析，不复制 MoviePilot 高层 Chain 业务规则
- 输出统一 JSON、默认分页和字段投影，错误中不得回显凭据
- 只在新 Skill 和离线测试就绪后，从 moviepilot-api operation 目录移除重复低层操作
- 不删除供前端和其它宿主调用的 REST 端点；删除的是 Agent operation 暴露和旧 Agent Tool 代码

能力面不局限于替换现有 operation。每个脚本都提供 `capabilities` 动作，按当前已配置实例和
provider 动态返回 namespaced action、参数约束、副作用等级及是否支持批量调用；Agent 只在加载
对应 Skill 后看到这部分领域知识。首批扩展目标包括：

- 下载器：任务属性/文件/Tracker/Peer、队列与优先级、暂停/恢复、重校验、重新汇报、
  限速、分类、标签、保存位置、顺序下载、会话统计、剩余空间和批量任务控制
- 媒体服务器：服务器/用户/媒体库/条目查询、最近入库、搜索与详情、播放会话、观看状态、
  媒体库扫描、单项元数据刷新，以及 provider 已稳定实现的合集/播放列表等扩展能力
- provider 专属能力通过扩展注册表增加；禁止退化为任意对象方法调用、任意 URL 或任意 HTTP 请求

## 4. 阶段、叶子和依赖

执行规则：任何时刻只允许一个叶子处于 ACTIVE；每个叶子必须可独立验证、交付和回滚。

| 阶段/叶子 | 状态 | 依赖 | 退出条件 |
|---|---|---|---|
| L1 基线与契约设计 | VERIFIED | 无 | 工具替代矩阵、调用路径、权限事实、输出合同、回归指标和排除项冻结 |
| L2 受控 API 网关 | VERIFIED | L1 | moviepilot_api 结构化工具、固定 API 路由、身份/策略透传、结果裁剪和统一新目录完成；不设置运行时切换开关 |
| L3 API parity | VERIFIED | L2 | 搜索会话、下载高级修改、过滤规则、插件数据、系统设置和斜杠命令均有 typed API 与离线测试 |
| L4 第一批迁移 | VERIFIED | L2 | 第一批工具从 Agent、MCP 和 HTTP 工具管理目录移除，能力统一由 API 网关提供 |
| L5 第二批迁移 | VERIFIED | L3 | parity 工具迁移完成，确认、脱敏、幂等和恢复语义进入统一策略 |
| L6 原生工具收敛 | VERIFIED | L4,L5 | Agent 任务和人格工具合并为 action 工具；宿主/会话/渠道原生能力边界固定 |
| L7 第三方服务 Skill 化 | VERIFIED | L5 | 下载器和媒体服务器能力发现、受控脚本、Skill、策略和离线测试完成；重复低层 Agent API operation 已删除 |
| L8 收口与交付 | VERIFIED | L6,L7 | 旧代码、文档与架构基线已收口；静态检查和锁定全量测试已完成 |
| L9 全 API 面审计与最终交付 | VERIFIED | L8 | 375 个 OpenAPI 操作逐路由归属、203 个网关合同与 72 个退出工具映射均由测试锁定；全量测试、80% 覆盖率门禁、提交推送和远端 CI 终态均已完成 |
| L10 查询 API 显式分页与数据库下推 | VERIFIED | L9 | 列表端点显式声明分页输入；数据库筛选在 `LIMIT/OFFSET` 前完成并使用同条件精确 `COUNT`；省略新增参数保持旧返回语义；响应 `data` 列表结构不变；外部原生分页未提供总数时不伪造；OpenAPI、MCP、Skill、审计、80% 覆盖率和远端 CI 同步完成 |

## 5. L2 受控 API 网关约束

### 5.1 模型可见合同

模型只看到一个结构化工具：moviepilot_api(operation_id, path_params, query, body)。

operation_id 来自审核过的操作注册表，模型不能传入任意 host、URL、认证头或 API Token。

### 5.2 身份和权限

- 从当前 Agent 的 ToolPolicyContext 继承用户、会话、渠道、来源、后台/子 Agent 身份
- API 网关不能把全局 API_TOKEN 当作当前用户身份
- 策略按 operation_id 加参数分类，而不是只按工具名 moviepilot_api 分类
- 读取、敏感读取、可恢复写入、破坏性写入和外部副作用分别处理
- 保持现有确认、管理员门禁、敏感值脱敏、幂等和恢复模式

### 5.3 Skill 合同

Skill 负责判断何时使用某个领域能力、提供业务流程和参数示例、声明允许访问的
allowed-api-operations。宿主必须实际执行该操作范围；只在提示词里显示 allowed-tools 不算权限控制。

### 5.4 输出和失败合同

- 统一 JSON envelope、分页、字段投影和长度上限
- 结果进入模型前做敏感字段裁剪和错误归一化
- 写操作带幂等键或版本前置条件
- 超时和取消后明确外部状态未知，不能直接报告为已停止
- API 网关不得复制业务规则；业务 owner 仍是现有 Application/Chain/API 能力

第三方服务 Skill 的脚本不属于 API 网关，也不得成为任意网络出口。脚本只消费审核过的
action，并使用 MoviePilot 已配置的具体服务实例访问其自身 API。

## 6. 验收指标

### 功能

- 媒体搜索、识别、订阅、下载、站点管理、媒体库、插件和工作流任务成功率不低于基线
- 搜索结果短引用、分页、筛选项和下载任务关联不回退
- API 网关和 provider Skill 的替代路径保持原业务结果、错误、确认与恢复语义

### 安全

- 普通用户、频道管理员、系统管理员、后台任务和子 Agent 权限边界保持一致
- 未经确认不能执行敏感读取、删除、覆盖、安装和外部副作用
- API Token、Cookie、密码和插件私有数据不进入 prompt、日志或错误摘要
- 不允许模型通过参数绕过 operation allowlist、host 限制或身份绑定
- 第三方服务 Skill 不接受连接地址或凭据参数，删除文件和媒体库扫描按外部副作用处理

### 运行质量

- 主模型工具 Schema 体积显著下降
- 工具选择误选率、平均调用次数、上下文 token、端到端延迟不劣于基线，优先取得改善
- API 网关失败可观测、可重试、可对账；取消/超时场景可恢复
- 所有测试零真实外部网络；相关测试使用边界 mock 或录制回放

## 7. 明确排除项

- 不把 execute_command 当作 MoviePilot API 网关；仅允许领域 Skill 调用仓库内受控、可审计的固定脚本
- 不暴露任意 REST path、任意 HTTP method 或任意外部 host
- 不把浏览器、验证码、文件编辑、终端命令和渠道回调强行 API 化
- MCP、HTTP 工具管理器和主 Agent 全部使用新 API 工具目录，不保留旧业务工具兼容入口
- 不修改 app/plugins/** 运行时副本
- 旧工具不作为回滚机制；回滚依赖 Git 版本和提交边界，正式运行时不保留兼容实现
- 不用第三方直连替代下载提交、订阅、转移、历史和 canonical 媒体存在性等 MoviePilot 高层用例

## 8. 阶段更新日志

### 2026-08-31：建立计划

- 创建 Codex Goal，目标为 Agent 工具体系的 API 化重构和安全收口
- 基线同步到 v3@6ee47795629f
- 确认 82 个固定工具、48 个第一批迁移候选、16 个 parity 候选和 18 个原生保留工具
- 当前活动叶子为 L1；尚未修改生产代码

### 2026-08-31：L1 基线验证与 L2 启动

- 已核对最新 v3 基线、工具工厂、Agent 编排、Skill 中间件、MCP 入口、API Token 鉴权和策略注册表
- 已确认第一批 48 个 API 候选、第二批 16 个 parity 候选和原生保留边界；后续实测更正默认固定目录终态为 14 个
- 已冻结 API 网关必须透传 ToolPolicyContext、禁止任意 URL、保留确认/幂等/恢复和结果裁剪的设计约束
- 当前唯一活动叶子切换为 L2；下一步直接实现正式结构化 API 网关，不增加运行时切换

### 2026-08-31：L2.1 网关骨架完成

- 新增 app/agent/policy/api.py，冻结第一批 48 个和 parity 16 个 operation ID、固定 API 路由与策略元数据
- 新增 app/agent/tools/impl/api.py，提供 moviepilot_api(operation_id, path_params, query, body) 结构化合同
- 工具工厂已切换为统一新目录：默认移除替代业务工具并注入单一网关，不新增运行时开关
- 主 Agent、子 Agent、MCP 和 HTTP 工具管理器均走同一新目录；不保留旧业务工具对外或内部兼容入口
- 策略注册表已按 operation ID 分类未知、敏感、可恢复写入、破坏性写入和外部副作用
- 网关已从旧工具 owner 转发改为 operation ID 到固定 HTTP 方法/路径的 API 执行器，模型不能注入 URL、认证头或方法
- 已删除临时 app/agent/tools/legacy.py，并移除 MCP 同名工具 first-wins 兼容选择；重复名称由严格目录冲突机制处理
- Skill operation 范围改为默认拒绝：未加载声明 allowed-api-operations 的 Skill 时不能调用 moviepilot_api
- 敏感系统设置确认入口已迁移到 config.system.get operation，策略回执对 SECRET 结果整体脱敏
- 当前未完成：补齐 parity API 的真实业务端点、完成渠道用户到 API 用户身份映射、删除 64 个退出工具文件并迁移关联测试/Skill
- 验证：网关、Skill、策略、数据端口与 MCP 专项 63 passed；新增代码 compileall 通过

### 2026-08-31：最终目录决策确认

- 不增加 `AGENT_API_GATEWAY_ENABLED`、`AGENT_COMPACT_TOOL_CATALOG_ENABLED` 或其它运行时切换
- 不考虑 MCP 旧工具兼容；MCP、HTTP 工具管理器和 Agent 统一采用新 API 目录
- 旧业务工具代码的删除是本目标的必达项：先将行为迁移到 API operation handler，再删除对应 `app/agent/tools/impl/*` 文件、注册、测试和文档
- 网关不得实例化旧工具类；临时 legacy owner 文件已经删除，后续不再恢复

### 2026-08-31：L2 完成并进入 L3

- Web/WebAgent 数字用户 ID 才允许直接作为 API 身份；外部消息渠道即使用户 ID 为数字，也必须通过渠道绑定解析为有效 MoviePilot 用户
- API 执行器使用宿主签发的当前用户令牌，并通过模型不可控请求头透传会话、渠道和来源；不读取全局 API_TOKEN
- 系统设置元数据与脱敏、过滤规则校验与引用处理、插件管理投影已迁出 `app/agent/tools/impl`，进入对应 Application owner
- 新增 typed API：统一系统设置、自定义识别词、下载任务高级修改、过滤规则/规则组 CRUD、插件运行能力/持久化数据、斜杠命令执行
- parity operation 已切换到真实固定路由，不再使用 `system/ruletest`、插件页面或环境变量接口作为占位映射
- L2 状态更新为 VERIFIED，当前唯一活动叶子为 L3；下一步补齐推荐、目录配置等剩余语义差异并开始删除 64 个退出工具文件
- 验证：`uv run --locked --no-sync python -m compileall -q app/agent app/application app/api/endpoints app/schemas` 通过；Agent 网关/Skill/策略/数据端口/MCP 专项 `63 passed`

### 2026-08-31：纳入下载器与媒体服务器第三方 API Skill

- 对照 database-operation Skill、下载器 provider client、媒体服务器 provider client 和现有 API operation 完成边界评估
- 决定新增 downloader-operation 与 mediaserver-operation 两个领域 Skill，通过固定脚本读取本机配置并调用已配置第三方服务自身 API
- 两个 Skill 不仅替代既有低层 operation，还提供动态 `capabilities` 清单和 provider 扩展注册表，使 Agent 可按需使用任务文件/Tracker/Peer/队列、播放会话、观看状态、扫描和元数据刷新等高级能力
- 计划在 Skill 通过离线测试后移除 `download.list`、`download.update`、`download.delete`、`downloaders.list` 和 `library.latest` 等重复低层 Agent operation
- 保留 `download.add`、`library.exists`、下载/转移历史和订阅等 MoviePilot 高层 operation，避免削弱媒体身份、路径映射、持久化和恢复能力
- 不保留旧工具或 MCP 兼容入口；前端仍需使用的 REST 端点不在删除范围
- 当前活动叶子仍为 L6 原生工具收敛；L7 已加入 Goal，待 action 工具收口后实施

### 2026-08-31：L3 至 L6 完成

- 完成 59 个正式 operation：第一批 44 个、parity 15 个；模型不能注入 URL、method、认证头或 Token
- 将过滤规则、系统设置、插件管理/数据、音乐投影、下载任务和命令执行迁入对应 Application owner
- agent_task 合并 create/list/update/run/delete，persona 合并 list/switch/update；旧独立工具实现全部删除
- 主 Agent、子 Agent、CLI、MCP 和 HTTP 工具管理器统一消费同一严格目录，不再注册旧工具名
- 删除 77 个 app/agent/tools/impl 退役文件，其中 72 个工具模块和 5 个辅助模块
- 阶段验证：网关、策略、原生 action、流式活动和关联回归 218 passed

### 2026-08-31：L7 第三方服务 Skill 化完成

- 新增 downloader-operation Skill 与 mp-downloader.py 固定脚本，覆盖 qBittorrent、Transmission、rTorrent 的实例、能力发现、任务/文件/Tracker/Peer、队列、限速、标签、分类、位置和会话统计等动作
- 新增 mediaserver-operation Skill 与 mp-mediaserver.py 固定脚本，覆盖 Emby、Jellyfin、Plex、ZSpace、Ugreen、TrimeMedia、Navidrome 的实例、媒体库、条目、活动、播放会话、扫描和元数据刷新等动作
- 两个脚本只读取 MoviePilot 已配置实例，拒绝任意 URL/method/header/凭据参数，统一递归裁剪密码、Token、Cookie、API Key 和会话字段
- 从 Agent operation 目录删除 download.list、download.update、download.delete、downloaders.list 和 library.latest；保留前端 REST 端点与 MoviePilot 高层 download.add、library.exists、订阅和历史能力
- 阶段验证：provider Skill、网关移除项和内置 Skill 边界 33 passed

### 2026-08-31：L8 收口完成

- CLI scheduler list/run 已改用 scheduler.list 与 scheduler.run operation，不再调用已删除工具名
- 工具目录构造、Agent 图、HTTP/MCP direct 调用均强制唯一身份；同名插件/MCP 工具直接失败，不采用 first-wins
- docs/mcp-api.md、docs/cli.md、命令规则、内置 Skill 文档和 Schema 导出清单已同步新合同
- 宿主架构基线在 L8 首次更新为 917 个模块、7,671 条内部依赖边；L9 新增固定 API MCP 合同与外部服务工具 owner 后为 919 个模块、7,680 条内部依赖边，未新增 Application/Chain 到具体 Adapter 的直连
- 受影响 Python 文件通过 Ruff、compileall 和 Pylint `--errors-only`；Schema 导出、架构基线与 `git diff --check` 均通过
- 最终架构/OpenAPI/i18n/事件/package-root 回归 84 passed；Agent/MCP/CLI/Skill/架构大回归 1066 passed、插件与架构边界专项 124 passed、provider/gateway 专项 33 passed
- 完整锁定测试 `uv run --locked --no-sync python tests/run.py` 完成：7554 passed、9 skipped、2 failed
- 两个失败仅为 `test_release_supply_chain.py::test_release_promotes_latest_only_after_both_versioned_images` 与 `test_resource_v3.py::test_v3_release_workflows_use_main_wiki_and_isolated_images`；均已在未改动的 `v3@6ee47795629f` 干净 detached worktree 复现，属于本次重构前已有的发布工作流基线问题
- Agent 工具重构范围内没有遗留失败；L1-L8 全部退出，正式方案不保留旧工具、MCP 兼容或运行时切换开关
- 当前变更尚未提交或推送

### 2026-08-31：Skill/MCP 自描述合同与系统设置收口

- 下载器 24 个 provider action 加 `instances.list` / `capabilities.list` 两个发现 action、媒体服务器 17 个 provider action 加同样两个发现 action、MoviePilot API 203 个 operation 均从运行时合同生成完整英文参数说明；外部 MCP `tools/list` 保留相同 oneOf、必填、类型、默认值、枚举和跨字段约束
- 新增 admin-only `database_operation` MCP 工具，暴露 `tables`、`schema`、`query`、`write` 四个 action；内置 Agent 仍按需加载 `database-operation` Skill，不增加常驻业务工具
- 数据库 Skill 已覆盖 27 张 ORM 表、`alembic_version` 和全部字段基线；每张表均说明用途、适合查询的场景和写入边界，运行时仍以 `tables` / `schema` 实际结果为准
- Skill 文档生成器会对数据库说明与 ORM 元数据做双向缺项校验；新增、删除或改名表但未同步说明时生成直接失败，避免文档再次出现漏表或只有字段没有语义的问题
- 系统设置继续由 `config.system.get/update` 统一承载，不恢复旧配置工具；Skill 不再复制易漂移的 `Settings` / `SystemConfigKey` 全量清单，而是要求先动态发现 `definition`（声明类型、当前形状、敏感性、允许操作、列表匹配字段和持久化位置），再按精确键更新
- `Settings` 更新执行类型转换并持久化到 `app.env`；`SystemConfigKey` 通过配置服务写入数据库，保留插件 mutation 门禁、敏感值脱敏和配置变更事件
- `app/agent/policy/resources/api_mcp_schema.json` 明确为 OpenAPI + 固定 operation 合同生成的 MCP `tools/list` 制品，禁止手工维护；所有 operation、模型和字段必须具备具体英文说明，抽象占位文本或中文说明会由测试拒绝
- API 权限保持双层硬门禁：`moviepilot_api` 按 operation 的 `required_role` 在发起 HTTP 前拒绝非管理员，最终 FastAPI 端点继续用真实当前用户令牌执行 superuser/manage/user 级鉴权；下载器、媒体服务器、数据库外部工具整体为 admin-only
- 异步边界复核：内置 Skill 脚本经 asyncio subprocess 执行；外部服务/数据库 MCP 工具把同步脚本放入 downloader、mediaserver、db 分域线程池；`moviepilot_api` 使用异步 HTTP，不阻塞 Agent event loop
- 当前阶段进入最终验证：聚焦 Agent/Skill/MCP 回归、全量锁定测试、80% 固定覆盖率 CI、提交推送与远端终态确认

### 2026-08-31 至 2026-09-01：L9 全 API 面审计与真实调用验证

- 从当前 FastAPI OpenAPI 生成 375 个 HTTP 操作的完整清单；每个路由明确归类为 `gateway`、`provider-skill`、`consolidated`、`alternate-auth-duplicate`、`transport_or_identity`、`stream_or_binary` 或 `ui_presentation`，生成器对任何未归类端点直接失败
- `moviepilot_api` 扩展为 203 个稳定 operation，除 10 个音乐识别、探索、专辑、艺术家和缓存操作外，继续覆盖仪表盘、媒体发现与识别、站点全生命周期、订阅协作、存储维护、整理队列、工作流、种子缓存、数据库备份、规则与网络测试、插件运行管理、当前用户公开配置和使用统计；音乐支持艺术家到作品、作品到艺术家的双向浏览
- 新增 `tests/test_agent_api_surface_audit.py`，逐项校验 OpenAPI 清单、固定路由、生成审计文档、MCP `oneOf`、英文字段说明和 Skill operation 章节完全一致
- 将历史删除提交中的 72 个业务工具冻结为替代映射测试，逐项证明其 owner 是 203-operation API、下载器/媒体服务器 action 或统一 `agent_task` / `persona` 原生工具，并确认旧模块物理文件不存在
- 实际执行数据库脚本 `tables` / `schema` / `SELECT 1`、下载器与媒体服务器 `instances` / `capabilities`，并直接运行三个结构化 service tool；本机未配置 provider 实例时返回空实例而不是配置读取错误
- 通过临时本地 HTTP 服务实际执行 `MoviePilotApiTool -> MoviePilotApiExecutor -> GET /api/v1/site/agent` 完整链路，验证返回成功且普通用户投影不包含 cookie、API key、token 或 RSS 等认证字段
- 新增内置 Agent 私有 `read_skill` 工具，绕过普通工具 64 KiB 结果裁剪并单独限制 `SKILL.md` 主体为 512 KiB；工具同时返回全部辅助文件相对路径，超过上限时明确标记截断，真实内置 Skill 加载测试确认 203 个 operation 均可见且 `truncated=false`
- Skill 生成器同步 YAML `allowed-api-operations` 与正文目录，避免“文档有参数但运行时未授权”的双事实源漂移；MCP schema、Skill front matter、正文和注册表数量及集合完全一致
- 为站点优先级、插件目录和工作流路径 ID 等原先不精确的输入补充类型模型或端点约束；固定路由占位符与 path schema 名称、required 状态由测试逐项校验
- 受影响 Agent/Skill/MCP/OpenAPI/音乐/架构回归 247 passed；修复全量发现的工作流管理员门禁、插件分页默认值、模块命名治理、服务工具标签和 Schema 导出清单后，专项回归 40 passed
- 完整锁定测试最终通过：7,614 passed、9 skipped；覆盖率按 CI 相同的 8 分片采集并合并，Application 81.88%、Domain 81.01%，通过固定 80% 门禁
- 首次推送 `6d4d7331d` 的远端 Pylint 成功；Architecture Contract Gate 在 complexity v2 发现 `plugin.py` 从 999 行增长到 1007 行，合并三个短 import 后恢复到 999 行，本地两套复杂度门禁及 49 项相关回归均通过
- 第二次推送 `28298c485` 的远端 Pylint 成功；Architecture Contract Gate 识别出两处 Ruff 导入排序债务下降尚未固化，以及新增 service tool/Agent 投影端点未显式处理 mypy 既有基础设施告警
- 已对新增 Pydantic/ResponseAPIRouter 边界使用精确错误码抑制，补齐 service tool 的 ClassVar 与可选 selector 类型；未通过覆盖基线隐藏任何新增 mypy 错误
- Ruff 低水位由 576 降至 574，mypy 低水位由 9,606 降至 9,605；mypy、复杂度 v1/v2、并发、异步阻塞、TaskRegistry owner、服务定位和启动性能全部本地通过
- API/Skill/MCP/provider 专项 86 passed，架构与质量门禁专项 127 passed；生成器重建后工作区合同无额外差异
- 第三次推送 `e5722ed98` 的远端 Pylint 成功；Unit Tests 的功能分片与覆盖率分片均通过，但 Architecture Contract Gate 在文档合同测试中发现优化清单仍写着旧的 `9,606 / 576` 量化指标
- 已将 `docs/architecture/optimization-checklist.md` 同步为 `9,605 / 574`，本地 `test_architecture_contract_baseline.py` 与 `test_architecture_baseline_cli.py` 共 45 项通过，并确认 `baseline.py --check-host` 成功
- 当前活动叶子保持 L9；下一步提交文档指标修复，等待所有远端工作流终态成功后更新为 COMPLETE

### 2026-08-31：最终提交与远端门禁完成

- 文档合同修复提交 `3106984dfc586aa171bbe9bd52307d19c6ef14f3` 已推送到 `origin/v3`，本地 HEAD 与远端一致，工作区无未提交变更
- Unit Tests 工作流 `33421990027` 已终态成功：Architecture Contract Gate、4 个功能分片、8 个覆盖率分片和 Coverage Report 全部通过
- Pylint 工作流 `33421989865` 已终态成功；架构质量门禁、Ruff/mypy ratchet、复杂度、异步阻塞、Task owner、服务定位和启动性能检查均成功
- 全量 Agent API 面审计、Skill/MCP 参数合同、下载器/媒体服务器/数据库真实只读调用、音乐双向浏览能力和固定 80% 覆盖率要求均已完成
- 本次重构正式完成：旧 Agent 工具代码、旧 MCP 兼容路径和运行时切换开关均不保留；后续新增 API 必须同步注册表、Skill、MCP schema、审计清单和测试

### 2026-09-01：L10 查询 API 兼容分页与总数合同

- 重新开启父目标并进入 L10；当前基线 `91e33277f` 与 `origin/v3` 对齐，初始工作区干净
- 查询 API 按结果语义分为完整列表、原生分页或限量列表、结构化分页对象、统计或聚合对象；只有列表结果进入统一数量合同，统计、映射和时序对象不为形式统一而错误分页
- 原完整列表新增的 `page` / `count` 必须都是可选参数；两者都省略时不切片，继续返回端点原先的完整列表。显式传入任一参数才启用分页，缺失的 `page` 按 1、缺失的 `count` 按 50 解释
- REST 响应继续保持 `Response.data` 为原列表，禁止改成 `{items,total}` 等对象；总数和分页信息使用响应头及 Agent 网关附加元数据表达，避免破坏外部插件和既有客户端
- 对完整列表可报告切片前精确总数；对已经由第三方来源原生分页或限量、且上游没有提供总数的结果，只报告当前返回数量，不强制增加总数，也禁止把当前页数量伪装成全局总数
- 第一版由统一响应路由隐式注入分页参数并在序列化后切片；复核后确认这种实现虽然能生成查询参数，但端点签名不自描述，而且数据库列表仍会全量读取，因此不作为最终方案
- 当前正式方案由每个列表端点显式声明 `page` / `count`；框架 `Response` 仅用于写响应头，不会出现在 OpenAPI、MCP 或 Skill 输入中。已有第三方原生 `page`、`count`、`limit` 或 `max_results` 的接口保留其显式参数语义；当原生限量参数默认值为 `None` 时，省略所有窗口参数继续返回完整列表
- 用户、PassKey、活动订阅、Workflow、Site、站点用户数据和站点统计的筛选、稳定排序、`LIMIT/OFFSET` 与精确 `COUNT` 已下推到异步 SQLAlchemy 查询；状态、用户名、名称、触发类型、站点启用状态、站点 ID/域名和日期筛选均在分页前执行，避免空页和错误总数
- 纯内存、配置、缓存、文件系统或运行时目录仍可在响应边界按显式 `page/count` 切片；第三方原生分页且不返回总数的接口只报告当前页数量，不伪造总数
- REST 保持 `data` 原列表；`X-Result-Count` 报告本次返回数量，精确可知时增加 `X-Total-Count`，Agent 网关把这些响应头映射到附加 `collection` 对象。下载历史、订阅历史和插件目录已增加本地精确计数；外部媒体、音乐、推荐和搜索来源未提供总数时不输出 `total_count`
- OpenAPI 与 `api_mcp_schema.json` 已重新生成，英文 `skills/moviepilot-api/SKILL.md` 已同步显式参数；生成器会识别插件目录的既有 `max_results`，不会错误声称其默认返回全量
- 完整锁定测试已通过：4 个分片分别为 `1620 passed, 3 skipped`、`1860 passed, 4 skipped`、`1917 passed`、`2248 passed, 2 skipped`，合计 `7645 passed, 9 skipped`
- 覆盖率按 CI 相同的 8 分片采集并合并，Application `81.72%`、Domain `81.01%`，通过固定 80% 门禁；Ruff/mypy ratchet、严格 mypy、复杂度 v1/v2、并发、异步阻塞、TaskRegistry owner、服务定位、事件策略和启动性能门禁均通过
- 数据库 Oper 复用 `literal_contains` 新增 4 条内部依赖边，宿主快照从 7,684 更新为 7,688；没有新增 Application/Chain 到具体 Adapter 的直连或跨层依赖
- 生成器幂等校验通过，`api_mcp_schema.json` 与 `skills/moviepilot-api/SKILL.md` 重建前后哈希一致；受影响 Python 文件 Pylint `10.00/10`，关键回归 `207 passed`

### 2026-09-01：L10 提交推送与远端门禁完成

- 实现提交 `d512f528be351dabba0cf344c2e9d890bb93ea6f` 已推送到 `origin/v3`，本地与远端提交一致
- Unit Tests 工作流 `33457912749` 已终态成功：Architecture Contract Gate、4 个功能分片、8 个覆盖率分片和 Coverage Report 全部通过
- Pylint 工作流 `33457912735` 已终态成功；变更文件质量检查与完整建议报告均成功
- L10 正式完成：集合端点显式声明 `page/count`，数据库查询在 SQL 层完成筛选、稳定排序、分页与同条件精确计数；不传新增参数继续返回旧全量列表，外部来源没有总数时不伪造
- MoviePilot Agent 工具体系重构父目标至此全部完成，后续查询端点必须继续维护 REST、OpenAPI、MCP、Skill 和数据库查询合同的一致性

### 2026-09-01：集合总数截断可见性修复

- Web Agent 实际调用 `subscription.list` 时未传分页，完整订阅列表超过通用工具结果 64KB 上限；此前网关把 `collection` 追加在 `data` 之后，截断预览只保留开头条目，导致 Agent 看不到已经由 API 返回的精确 `collection.total_count`，继而错误回退到数据库统计
- 网关现将 `collection` 放在 `data` 前面，使 `X-Total-Count` 投影的精确总数在大结果预览中仍优先可见；列表响应和 REST `data` 结构均未改变
- 核心提示、MCP oneOf、`moviepilot-api` Skill 和文档统一要求：数量或摘要查询使用最小分页窗口；兼容分页传 `page=1,count=1` 并读取 `collection.total_count`，不得因为条目或工具预览截断而改查数据库
- `moviepilot-api` Skill 版本提升到 24；新增超大订阅结果回归，验证截断预览中总数早于 `data` 出现
- 完整锁定测试已通过：4 个分片分别为 `1620 passed, 3 skipped`、`1860 passed, 4 skipped`、`1917 passed`、`2250 passed, 2 skipped`，合计 `7647 passed, 9 skipped`；变更文件 Pylint 为 `10.00/10`，Ruff、生成物幂等与 `git diff --check` 均通过
- 当前未托管后端使用的 API 身份与工作区 CLI 配置不一致，真实 CLI 调用被既有 API key 校验拒绝；本轮未擅自重启正在运行的服务，运行进程需在代码部署并重载后再做在线会话复验

### 2026-09-01：啰嗦模式 API 主要参数展示修复

- 旧领域工具各自通过 `get_tool_message()` 展示目标和主要参数；收敛为 `moviepilot_api` 后，该方法只展示 `operation_id`，导致 Web Agent 与通知渠道在啰嗦模式下失去 `path_params`、`query` 和 `body` 的调用上下文
- `moviepilot_api` 现按路径、查询、请求体顺序生成最多 320 字符的主要参数摘要；空参数桶不展示，工具执行与 API 输入结构保持不变
- 参数摘要统一复用宿主递归脱敏器，设置凭据、密码、Cookie、Token 与 API key 等仅显示 `***`，避免为了恢复可观察性而泄露敏感输入
- 复用脱敏器新增 1 条 Agent 同域内部依赖边，宿主架构快照由 7,688 更新为 7,689；模块总数和跨层边界均未变化
- 后续将 Skill、子 Agent 和活动记录等中间件统一接入运行时啰嗦模式判断，新增 `app.agent.callback -> app.runtime.settings` 依赖，宿主架构快照由 7,689 更新为 7,690；该方向符合 Agent 到 Runtime 的既定边界
- 中间件 import 排序修复同时消除了 2 条 Ruff `I001` 历史诊断，低水位由 571 降至 569，并已同步质量指标文档
- 新增 Web Agent 实际流式回调回归和设置凭据脱敏回归；扩展 Agent/API/流式专项 `487 passed`，完整锁定测试 4 个分片分别为 `1620 passed, 3 skipped`、`1860 passed, 4 skipped`、`1917 passed`、`2253 passed, 2 skipped`，合计 `7650 passed, 9 skipped`；Ruff、Pylint `10.00/10` 和全量 mypy ratchet 均通过

本文件作为本次重构的持续记录，保留阶段状态、实际变更、验证结果、提交状态与已知基线边界。
