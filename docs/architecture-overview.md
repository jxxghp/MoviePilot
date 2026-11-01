# MoviePilot v3 后端架构总览

> 本文面向开发人员，以图文结合的方式说明 MoviePilot v3 的整体架构：分层结构、各核心包的职责、
> 模块间调用关系、典型业务流程与启动生命周期。
>
> 规范性约束以 [`docs/rules/05-architecture.md`](rules/05-architecture.md) 与
> [`docs/rules/04-design-patterns.md`](rules/04-design-patterns.md) 为准，本文与其保持一致；
> 如出现差异，以规则文档为准。
>
> *最后更新：2026-10-19*

---

## 一、系统概述

MoviePilot 覆盖影视与音乐媒体的自动化管理，实现**订阅、搜索、下载、文件整理、元数据刮削、
媒体库刷新与消息通知**的全流程自动化。后端基于 FastAPI，前端基于 Vue 3（独立仓库），
同时对外提供 REST API、MCP 工具调用端点（`/api/v1/mcp`）与 OpenAI / Anthropic 兼容协议端点，
内置 AI Agent 支持自然语言操控系统。

```mermaid
flowchart LR
    subgraph 外部["外部入口"]
        Web["Vue 3 前端"]
        Msg["消息渠道<br/>Telegram / WeChat / Slack ..."]
        ExtAgent["外部智能体<br/>MCP / Skills"]
    end

    subgraph 后端["MoviePilot 后端（FastAPI）"]
        API["REST API / MCP / 兼容协议"]
        Core["核心引擎<br/>Chain / Application / Module / Plugin / Agent"]
        Persist["持久化端口 / Oper"]
    end

    subgraph 外部服务["外部生态"]
        Site["PT 站点 / 索引器"]
        DL["下载器<br/>qBittorrent / Transmission ..."]
        MS["媒体服务器<br/>Emby / Plex / Jellyfin ..."]
        Meta["元数据源<br/>TMDB / Douban / Bangumi ..."]
        LLM["LLM 提供商"]
    end

    DB[("PostgreSQL / SQLite<br/>+ Alembic 迁移")]

    Web -->|HTTP| API
    Msg -->|Webhook / 轮询| Core
    ExtAgent -->|JSON-RPC| API
    API --> Core
    Core -->|应用端口 / DB Adapter| Persist
    Persist --> DB
    Core <--> Site
    Core <--> DL
    Core <--> MS
    Core <--> Meta
    Core <--> LLM
```

---

## 二、整体分层架构

MoviePilot v3 以单向依赖的模块化单体为目标。历史包 `app/core`、`app/helper`、`app/utils`
已不再存在物理目录，仅作为旧插件的**虚拟兼容导入根**保留。核心边界已有机器门禁，
完整宿主强连通分量（SCC）、Adapter 直连、网络出口和事件合同均有事实快照与独立语义门禁，
具体检查入口见第十章。

下图同时表达职责关系与运行时调用方向，不等同于 Python 静态 import 图；通过 Port 注入的调用
会与具体 Adapter 的静态依赖方向相反。

```mermaid
flowchart TB
    subgraph 入口层["入口层 Entrypoints"]
        ApiPkg["app/api<br/>REST / MCP 端点"]
        AgentPkg["app/agent<br/>AI Agent 运行时"]
        Monitor["app/monitor<br/>目录监控"]
        Workflow["app/workflow<br/>工作流"]
        Scheduler["app/scheduler/<br/>定时任务职责包"]
        CLI["app/cli.py<br/>命令行"]
        PluginPkg["插件运行时目录<br/>app/plugins/*（副本/覆盖层）"]
    end

    subgraph 编排层["编排层"]
        Chain["app/chain<br/>Chain 用例编排"]
        App["app/application<br/>聚焦应用服务"]
    end

    subgraph 能力层["能力层"]
        Modules["app/modules<br/>可插拔后端模块"]
        Db["app/db<br/>模型 + Oper 数据访问"]
        Schemas["app/schemas<br/>传输模型与枚举"]
    end

    subgraph 契约层["契约层"]
        Domain["app/domain<br/>纯业务语义"]
        Runtime["app/runtime<br/>进程级运行时契约"]
    end

    subgraph 底层["底层机制"]
        Foundation["app/foundation<br/>无状态原语"]
        Adapters["app/adapters<br/>技术 I/O 适配"]
    end

    subgraph 组合根["组合根 / 边界"]
        Startup["app/startup<br/>Composition Root"]
        Sdk["app/sdk<br/>插件稳定导入面"]
        Compat["app/runtime/compat<br/>旧导入路径与符号映射"]
    end

    ApiPkg --> Chain
    AgentPkg --> App
    Monitor --> Chain
    Workflow --> Chain
    Scheduler --> Chain
    CLI --> Chain
    PluginPkg --> Sdk

    Chain -->|run_module / async_run_module 分发| Modules
    Chain -->|Application service / 命名数据端口| App
    App -->|运行时调用 Protocol / 持久化端口| Db

    Modules --> Domain
    App --> Domain
    App --> Runtime
    Chain --> Runtime
    Domain --> Schemas
    Domain --> Foundation
    Runtime --> Foundation
    Adapters --> Domain
    Adapters --> Foundation
    Startup -->|构造并注入| Adapters

    Startup -.注入/装配.-> Runtime
    Startup -.注入/装配.-> App
    Startup -.注入/装配.-> Modules
    Sdk -.门面转发.-> App
    Compat -.惰性映射.-> Sdk
    Compat -.精确别名.-> App
    Compat -.精确别名.-> Db
    Compat -.精确别名.-> Foundation
    Compat -.精确别名.-> Adapters
```

图中的 `Chain → Application` 与 `Application → Db` 是运行时调用关系，表示通过应用端口和组合根
注入的实现完成持久化；静态 import 方向是 `db.adapters → application Protocol`。
具体 DB Adapter 再使用 Oper；这不是允许在用例代码中直接创建数据库引擎或拼接 SQL。`compat` 也不是只面向 SDK 的转发层，
它按 `app/runtime/compat/manifest.py` 的白名单把已经删除的旧模块/符号精确映射到各自的 canonical
归属。`app/application/subscribe.py` 与 `app/application/plugins.py` 都是 V3 重构过程中新增、
未形成插件 ABI 的宿主内部聚合文件，主题实现收口后直接删除，不在 manifest 中制造新的兼容债务。

当前正式目标和已完成参考切片的持久化调用路径可概括为：

```text
入口（API / Agent / CLI / Scheduler / Workflow）
        -> Chain 或 Application 用例
        -> 命名 Port / Protocol
        -> db/adapters 创建短生命周期 Session/UoW
        -> db/oper 与 db/models
```

Chain 的持久化能力由 `startup` 构造 `ChainRuntimeContext` 并在 `ChainBase` 实例化时显式注入；
Agent manager、memory、tool 和 scheduler 共享同一个 `AgentDataContext`。原
`app/application/chain/data.py`、`app/application/agentdata.py` 的进程级 getter 已删除，且不进入
SDK/Compat。领域查询与写入继续使用 Application 所属的冻结 DTO/Port，具体实现只存在于
`db/adapters`，宿主生产代码不得恢复 raw Oper、`Any` factory 或数据服务 locator。
需要跨进程恢复或 commit 后可靠执行的
业务副作用进入 `app/application/outbox.py` 定义的 Outbox 端口，由
`app/db/adapters/outbox.py` 实现；`app/runtime/tasks.py` 的 TaskRegistry 只负责进程内任务所有权、
取消和有限等待，不承担 durable queue 语义。

**依赖方向**如下。宿主 Application/Chain 到具体 Adapter 的静态直连已清零；
持久化 Port 归用例所有，具体 DB Adapter 通过组合根注入。表中的规范、语义测试与生成快照
分别承担边界定义、违规检测和事实漂移检测，不能相互替代：

| 方向 | 状态 |
|---|---|
| 入口层 → Chain / Application / 注入 Port | 允许（按工作流复杂度选择；不得直接构造 Oper） |
| Chain → Module | 仅允许通过 `run_module` 方法名分发，禁止直接 import 模块内部 |
| Chain → Agent 实现 | 禁止；只能经 `app/application/agent.py` 门面 |
| API / Chain → Workflow 实现 | 禁止；通过 `app/application/workflow.py` 用例门面 |
| Application → Domain / Runtime / 注入的 Port | 允许；不得直接依赖具体 DB/Oper/Adapter |
| DB Adapter → Application 持久化 Protocol / Oper / UoW | 允许；这是依赖倒置的实现方向 |
| Module → Module / Chain | 禁止（跨模块编排一律进 Chain） |
| Adapter → Application / runtime.extensions / sdk / compat | 禁止 |
| Domain → Runtime / Adapter / Application / DB | 禁止 |
| Foundation → 任何其他 app 包 | 禁止 |
| 规范实现包 → sdk / compat | 禁止 |
| 任何形成模块级循环依赖的 import | 禁止（延迟导入也不被接受） |

---

## 三、核心包职责速查

| 包 | 职责 | 代表性文件 |
|---|---|---|
| `app/foundation/` | 无状态、无配置、无 I/O 的底层原语：反射/动态导入、加密、DOM、单例、文本、URL、版本比较 | `reflection.py`、`crypto.py`、`singleton.py` |
| `app/domain/` | 纯 MoviePilot 业务语义：媒体上下文、识别解析、站点状态解释、磁力语义、NFO 刮削、图片内容与格式归一 | `context.py`、`metainfo.py`、`meta/`、`scraper.py`、`artwork.py` |
| `app/runtime/` | 进程级运行机制：配置、进程拓扑、事件、完整日志、缓存契约与内存后端、运行依赖 profile 与原生载荷激活检测、任务所有权、执行/关联上下文、并发、调度、限流、本地化、GC、重启状态 | `config.py`、`events.py`、`event/`、`dependencies/`、`tasks.py`、`execution.py`、`correlation.py`、`log.py`、`cache.py` |
| `app/runtime/extensions/` | 模块 / 插件 / 配置化服务 / 托管资源的发现、注册与生命周期适配；管理器归入对应主题包，旧插件路径只由 Compat 精确映射 | `module/manager.py`、`plugin/manager.py`、`service.py` |
| `app/runtime/compat/` | 仅标准库的精确旧模块、包与符号导入路由；不是业务实现，也不是通用 re-export 层 | `manifest.py`、`imports.py` |
| `app/adapters/network/` | 通用 HTTP、浏览器、DNS、Cloudflare、IP 传输机制 | `http.py`、`browser.py` |
| `app/adapters/cache/` | Redis 与文件缓存的具体实现 | `backends.py`、`redis.py` |
| `app/adapters/system/` | OS/文件/进程/stdio/显示/包安装/Rust 加速适配 | `host.py`、`resource.py`、`fsproxy.py` |
| `app/adapters/external/` | 命名外部生态：插件市场、CookieCloud、OCR、IP 归属、MP Server、微信加密 | `market.py`、`server.py`、`wechat.py` |
| `app/adapters/web/` | Web 技术适配：动态插件路由注册、认证依赖和 OpenAPI 重建；不承载插件路由用例 | `plugin/routes.py` |
| `app/adapters/observability/` | 可选观测技术适配；核心层只依赖 `runtime/observability` 定义的窄端口 | `otel.py` |
| `app/application/` | 读取配置/持久化状态的聚焦应用服务：识别、过滤、通知、RSS、站点、下载器、媒体服务器、存储、整理规则、可靠副作用等；同一主题拆成子包 | `recognition.py`、`rules.py`、`rss.py`、`outbox.py`、`site/`、`subscription/`、`plugin/` |
| `app/application/chain/` | Chain 运行时上下文、跨领域数据端口和 durable event 命令；由组合根显式注入 Chain | `context.py`、`events.py` |
| `app/application/subscription/` | 订阅深度冻结 DTO、typed query/write/staging Repository，以及新增、查询、变更、删除、媒体身份与搜索用例 | `contract.py`、`write.py`、`mutation.py`、`delete.py`、`identity.py`、`search.py` |
| `app/application/plugin/` | 插件市场、安装、运行时端口、文件夹操作和动态路由用例；具体 FastAPI 路由适配器在 adapters 层 | `catalog.py`、`install.py`、`runtime.py`、`folders.py`、`routes.py` |
| `app/application/messaging/` | 渠道回环入口、消息渲染/路由、命令交互会话、插件按钮回调、Agent 消息桥接 | `ingress.py`、`message.py`、`router.py`、`agent.py` |
| `app/application/security/` | 认证、授权、Cookie、Passkey、OTP/二次认证、SSRF 与 URL/路径安全 | `auth.py`、`url.py`、`twofactor.py` |
| `app/chain/` | 跨入口复用的用例编排：订阅、搜索、下载、整理、媒体、消息等 Chain | `subscribe/`、`search/`、`download/`、`transfer/` |
| `app/modules/` | 可插拔后端：下载器、媒体服务器、元数据源、消息渠道、索引器、存储 | `qbittorrent/`、`emby/`、`telegram/`、`themoviedb/` |
| `app/db/` | SQLAlchemy 模型、表级 Oper、会话/UoW 与 Application 持久化适配器；Model 只接受显式 Session，不拥有事务提交 | `models/`、`oper/`、`adapters/`、`uow.py` |
| `app/schemas/` | Pydantic 传输模型、枚举（`ModuleType`、`EventType`、`SystemConfigKey` 等） | `types.py`、`context.py` |
| `app/api/` | FastAPI 主端点、鉴权依赖、统一 `Response` 响应封装；动态插件端点不走此统一包装 | `apiv1.py`、`endpoints/`、`response.py` |
| `app/agent/` | AI Agent：稳定 Manager 门面、会话/生命周期/后台任务 owner、单 Agent 编排器、运行时、工具、中间件、LLM、记忆、技能、策略 | `manager.py`、`session.py`、`lifecycle.py`、`tasks.py`、`orchestrator.py` |
| `app/startup/` | 唯一组合根：跨层装配、领域初始化、声明式生命周期排序与重启策略 | `composition/`、`initializers/`、`lifecycle/` |
| `app/sdk/` | 面向新插件的稳定导入面（网络、缓存、日志、浏览器等）；`_legacy/` 只承载旧插件行为适配薄门面 | `network.py`、`browser.py`、`cache.py`、`_legacy/` |
| `app/monitor/` | 源目录监控 → 触发整理 | `watcher.py`、`dispatcher.py` |
| `app/workflow/` | 工作流执行引擎、动作与运行状态；跨入口消费者通过 Application 门面访问 | `__init__.py`、`actions/` |
| `app/scheduler/` | 定时任务目录、动态协调、执行和生命周期；业务 callable 由启动组合根注入 | `catalog.py`、`registry.py`、`execution.py`、`lifecycle.py` |
| `app/cli.py` | 本地命令入口：服务管理、安装更新、诊断和工具调用 | `cli.py` |
| `app/plugins/` | 插件运行时副本/覆盖目录，由插件管理器加载；不是官方插件源码或宿主架构实现，架构审计以插件仓库与宿主边界为准 | — |

`app/runtime/dependencies/` 是一个同名能力包：`profile.py` 只负责解释器 ABI 对应的依赖组，`native.py` 只负责已加载原生发行包的快照与变更检测。包根不重复导出实现，宿主直接导入职责子模块；旧的平级 `dependencies.py` 与 `native_dependencies.py` 已退出规范实现。

---

## 四、启动与生命周期

`app/startup/` 是全应用唯一的组合根：负责向低层注入依赖、编排初始化/关停顺序、决定重启策略。
低层模块不允许反向 import `startup`。

```mermaid
sequenceDiagram
    participant Main as app/main.py
    participant Factory as app/factory.py
    participant Life as startup/lifecycle/__init__.py
    participant Init as 各 initializer
    participant FastAPI as FastAPI 主循环

    Main->>Factory: 导入并创建 FastAPI 实例
    Factory->>Factory: create_app()：异常处理器 / CORS / 本地化中间件
    Factory->>Init: register_api_app(app) 注入插件路由服务
    Factory-->>Life: lifespan 绑定到 app
    Main->>FastAPI: Server.run() 触发 lifespan 启动

    Life->>Life: configure_cache_dependencies()<br/>（必须先于业务模块导入）
    Life->>Life: 校验进程拓扑，登记主事件循环
    Life->>Life: 创建文件日志 writer 与 TaskRegistry
    Life->>Init: prepare_database() + revision/head 校验
    Life->>Init: configure_default_user_agent（注入 UA）
    Life->>Init: configure_domain_dependencies（领域层依赖注入）
    Life->>Init: get_engine() / get_global_async_engine() 预热 + fail-fast
    Life->>Init: check_connection_budget() 连接预算核算
    Life->>Init: init_routers(app) 注册 API 路由
    Life->>Init: init_modules()（app/startup/initializers/modules.py）发现并初始化模块，返回 HostRuntime
    Life->>FastAPI: app.state.host_runtime = HostRuntime
    Life->>Life: 登记社区勋章后台维护，装配插件服务
    Life->>Life: 启动共享消息队列，恢复插件安装与备份
    Life->>Init: init_plugins() / init_scheduler() / init_monitor()
    Life->>Init: replay_pending_transfers()（登记整理恢复调度器）
    Life->>Init: init_command() / init_workflow()
    Life->>Life: 登记插件同步与启动收尾任务
    Note over Life,FastAPI: database_ready 在数据库准备后发布
    Life->>Life: 发布 lifecycle_ready
    Life->>FastAPI: yield，交还控制权
    Note over Life,FastAPI: 运行期……
    FastAPI->>Life: 收到停止信号
    Life->>Init: 按 stop_order 关停（与启动顺序独立）
    Note over Life,Init: 先封口停止信号、Scheduler 与 TaskRegistry
    Life->>Init: 收敛工作流/命令/监控、Agent/整理、事件与插件服务
    Life->>Init: 事件屏障→插件→消息队列→模块→撤销注入端口
    Life->>Life: 关闭共享异步 HTTP 连接池
    Life->>Life: 关闭并释放当前 lifespan 的日志 writer
```

关键设计点：

- **缓存装配先于业务导入**：缓存装饰器会在业务模块 import 时创建后端，
  因此 `configure_cache_dependencies()` 在 `startup/lifecycle/__init__.py` 的业务导入前执行。
- **Uvicorn 入口分流**：生产单 worker 使用带协作停止语义的 `MoviePilotServer`；开发 reload
  和安全模式多 worker 使用 `app.factory:create_app` import string/factory，由 supervisor
  创建应用实例。`app.factory:app` 继续保留给既有 ASGI supervisor 和测试使用。
- **数据库准备唯一入口**：建表、迁移、迁移前备份和 Alembic head 校验统一由 lifespan
  最早的“数据库准备”组件执行，`app.main` 不再主动迁移。主程序、外部 supervisor、factory
  和 TestClient 因而共享同一 fail-fast 语义。
- **引擎预热 fail-fast**：同步/异步数据库引擎在单线程期完成首次创建，
  避免调度器放出大量线程后再创建引擎导致连接锁竞争。
- **类型化请求装配**：`startup/composition/context.py` 的 frozen slots `HostRuntime` 是 lifespan 内唯一宿主
  上下文，`api/context.py` 从 `app.state` 收窄到具体领域能力。认证、消息、历史、媒体服务器、站点、
  订阅、工作流和请求事务均使用命名 runtime 字段，不再通过字符串仓储键定位；API、Scheduler、Chain
  从 `HostRuntime.configuration` 获取 frozen 配置快照。系统设置管理 API 通过
  `HostRuntime.settings` 的窄服务读写可变部署设置，业务域不接触 Settings 实例；生产与测试组合根统一
  复用 `startup/composition/configuration.py` 的映射、快照加载与发布。`startup/composition/database.py`
  持有数据库 worker、兼容事务 runner、查询与插件持久化；`startup/composition/runtime.py` 是
  `HostRuntime`、全部命名领域 Runtime 和旧 `ApiDataPorts` 投影的唯一构造/发布 owner。它先构造
  一份 frozen `RuntimeDependencies`，Agent、Chain 与 HostRuntime 复用同一仓储、transfer execution
  ledger 和消息对象，导入阶段不启动数据库 worker 或消息队列；initializer 只保留顺序调用。
  `startup/composition/chain.py` 集中构造无参 Chain 兼容上下文及其持久化、事件、
  Outbox、消息和模块分发依赖，并延迟绑定旧 Transfer command 与壁纸 Chain，避免组合根导入环。
  `startup/composition/security.py` 统一装配认证、用户查询、PassKey 与 Web 访问
  provider，并把持久化工厂交给 runtime owner 投影为 `AuthenticationRuntime`；
  `startup/composition/network.py` 统一装配网络应用端口；`startup/composition/agent.py` 构造并发布
  唯一的 Agent 数据、会话持久化和自主任务对象，由 runtime owner 投影 `AgentChatRuntime`，
  `HostRuntime`、工具管理器与 Scheduler 复用同一对象身份。`ApiDataPorts` 严格从同一 HostRuntime
  字段投影，仅保留旧导入 ABI，
  不参与正式请求链路。
- **安全模式**：`MOVIEPILOT_SAFE_MODE` 会跳过插件、定时器、监控器、命令与工作流，用于故障自救。
- **Scheduler 同名职责包**：旧 `app/scheduler.py` 单体已退役；`catalog.py` 负责作业目录和计划投影，
  `execution.py`、`bridge.py`、`progress.py` 分别负责执行、跨循环句柄和进度终态，`registry.py`
  唯一持有 generation、active generation、reservation 与 handle，`reconcile.py` 和 `lifecycle.py`
  分别负责动态任务协调与启动/重载/关闭，`oncejob.py` 承接插件一次性任务：插件经
  `app.sdk.scheduler.add_plugin_once_job` 在宿主调度器追加延迟单次执行，不再自建常驻
  `BackgroundScheduler`；追加不重建周期服务，服务重建只丢弃已被重载替换实例登记的任务。
- **Scheduler 显式装配**：`startup/initializers/scheduler.py` 构造业务 Chain 一次，将绑定 callable
  组成 frozen `SchedulerServices` 后注入 Scheduler；Scheduler 包内不再构造业务 Chain。
  `app.scheduler` 包根只惰性保留 `Scheduler`/`SchedulerChain` 旧 ABI，新插件经 `app.sdk.scheduler`
  使用窄调度服务，内部 owner 不重复导出。
- **进程拓扑**：全功能 V3 强制 `API_WORKERS=1`，避免每个 worker 重复启动插件和后台控制面；安全模式可临时使用多 worker 诊断，但不是正式扩容方案。
- **健康语义**：`/health/live` 只确认进程和事件循环可响应；`/health/ready` 仅在数据库
  到达当前 head 且生命周期完成后返回 200，启动失败或关停阶段返回 503。两者不公开路径、
  revision、插件和异常详情，深入诊断继续使用 Doctor。
- **声明式生命周期**：`lifecycle/components.py` 定义组件合同，`lifecycle/__init__.py` 的
  `build_lifecycle_components()` 装配回调、依赖、启用模式及独立的 `start_order` / `stop_order`。
  启动失败只清理已触达和部分启动的资源；关停按 owner 的预算等待，关键 `FAIL_FAST` owner
  未收敛时停止释放后续依赖，保留句柄供重试，避免任务仍在运行而资源已被销毁。

---

## 五、核心设计模式

### 5.1 Module 模式：可插拔后端

`app/modules/` 下每个目录是一个可插拔后端（下载器、媒体服务器、消息渠道、元数据源、索引器、
存储等），均继承 `_ModuleBase` 并实现统一契约：

```mermaid
flowchart LR
    subgraph 契约["_ModuleBase 契约"]
        A["get_name / get_type / get_subtype"]
        B["init_setting()：返回控制开关的配置项"]
        C["init_module()：初始化"]
        D["test()：连通性测试"]
        E["stop()：停止"]
    end

    MM["ModuleManager<br/>（发现 + 注册 + 生命周期）"]
    MM -->|扫描 app/modules/* 一级子包| M1["qbittorrent"]
    MM --> M2["transmission"]
    MM --> M3["emby / plex / jellyfin"]
    MM --> M4["telegram / wechat / slack ..."]
    MM --> M5["themoviedb / douban / bangumi ..."]
    MM --> M6["indexer / subtitle / filter ..."]
```

- 模块由 `runtime/extensions/module/manager.py` 发现并管理生命周期；
  `app/modules/_base/` 承载各模块族的共享模板基类（下载器、媒体服务器、消息渠道）。
- 模块开关由 `init_setting()` 声明的配置项决定（如 `DOWNLOADER = "qbittorrent"`）。
- **模块之间、模块到 Chain 的直接依赖被禁止**，跨模块编排一律由 Chain 完成。
- 渠道/存储的管理操作遵循统一契约：`channel_manage(channel, action, **params)` /
  `storage_manage(storage, action, **params)`，结果统一为 `{"success", "message", "data"}` 形态，
  Chain 与端点只做透传。

### 5.2 Chain 模式：用例编排

`app/chain/` 承载被 API、CLI、Agent、调度器、Webhook 等多入口共享的业务用例。
所有 Chain 继承 `ChainBase`，Chain 访问模块**只能通过方法名分发**：

```mermaid
flowchart TB
    Entry["API / CLI / Agent / 调度器 / Webhook"]
    C["SearchChain / SubscribeChain /<br/>DownloadChain / TransferChain ..."]
    Base["ChainBase<br/>run_module / async_run_module"]
    MM["ModuleManager 分发"]
    P["插件模块（同名方法优先）"]
    M1["模块 A"]
    M2["模块 B"]

    Entry --> C --> Base
    Base -->|先查询插件实现| P
    Base --> MM
    MM --> M1
    MM --> M2
```

- `run_module("method_name", **kwargs)` 与异步入口按契约分发：首个非空、列表/映射合并、
  管道传递或广播等规则由具体方法决定；插件同名实现优先，保留既有短路与返回语义。
- `runtime/extensions/module/contracts.py` 为宿主已观察到的方法提供显式参数与返回合同；兼容期只诊断
  旧插件签名差异，不改变插件优先级、短路和自由返回语义。
- 大型稳定 Chain 使用同名目录包治理：包根只保留稳定公开类，Facade 只组合职责 owner；
  `TransferChain`、`SubscribeChain`、`DownloadChain` 与 `SearchChain` 的旧单体文件均不得复活或以
  `source.py` 留存。`SearchChain` 的 provider 列表和流式入口共享同一批次事实，搜索状态仍由
  `application/search/state.py` 唯一拥有。
- `app/chain/` 中下划线前缀文件（`_recognition.py`、`_messaging.py`、`_interaction.py`、
  `_music.py`、`_transfer.py`）是 `ChainBase` 的功能域 Mixin，不是独立 Chain。
- 需要斜杠命令交互的 Chain 继承 `InteractionChainMixin`，只实现 `_interaction_handler`。

### 5.3 Event 模式：跨切面事件

`EventManager`（`app/runtime/events.py`）提供进程级事件总线，用于解耦跨切面反应
（整理完成后刷新媒体库、配置变更后重载模块、消息分发等）：

宿主内建 `EventType` / `ChainEventType` 均在事件注册表中绑定 typed payload。开放插件事件允许
额外字段，校验只生成诊断；分发给既有插件的仍是原始 dict/model 对象，不改变事件 ABI。
插件可通过 `app.sdk.events` 调用 `event.snapshot()` 或
`snapshot_event_data(event_type, event_data)` 获取独立的类型化快照。返回结果中的 `raw` 保留
原始对象，`payload` 是组合契约，链式事件还提供 `input` / `output` 快照；`known`、`valid` 和
`errors` 分别用于处理未知自定义事件和兼容期校验失败。快照修改不会回写原事件，如需拦截、取消
或替换链式结果，插件仍应修改原始 `event.event_data`。

架构基线中的 consumer 表示宿主源码中可静态证明的注册点，不是运行时 listener 实例数。
collector 只接受 canonical `eventmanager`、`EventManager()` 及其有限别名，忽略其他对象的同名
`register`/`add_event_listener`；当前宿主有 16 个静态注册点，另保留 1 个由工作流配置驱动的
真实动态注册。`app/plugins/**` 插件副本不进入宿主事实。

生产者与消费者共用 `scripts/architecture/event_facts.py` 这一份逐调用事实源。当前宿主有 86 个
生产调用，其中 85 个静态解析为 87 个事件引用，只有 `Command.send_plugin_event` 的插件事件类型
保持动态；17 个消费注册中 16 个静态、1 个动态。生成的
`runtime-contract-baseline.json` 保存 line-free 事实、数量和枚举索引；人工维护的
`runtime-contract-policy.json` 只批准 consumer 的精确 fingerprint、owner 和理由，任何新增、替换、
重复或陈旧项都会失败，`--write-host` 不会改写该 policy。

```python
from app.sdk.events import snapshot_event_data

snapshot = snapshot_event_data(event.event_type, event.event_data)
if snapshot.valid and snapshot.payload.context.media_info.type == "音乐":
    music_type = snapshot.payload.context.media_info.music_type
```

```mermaid
sequenceDiagram
    participant T as TransferChain
    participant EM as EventManager
    participant MS as MediaServerChain
    participant N as NotificationChain
    participant PL as 插件（注册了同一事件）

    T->>EM: send_event(EventType.TransferComplete, data)
    EM->>MS: on_transfer_complete(event)
    EM->>N: on_transfer_complete(event)
    EM->>PL: on_transfer_complete(event)
    Note over EM: 处理器通过 @eventmanager.register(EventType.X) 注册<br/>事件类型集中在 schemas.types.EventType
```

### 5.4 Oper 模式：数据访问层

数据库读写一律通过 `app/db/oper/` 下的 Oper 类，禁止在 Chain / Module / 端点中直接写
SQLAlchemy 查询。`models/` 与 `oper/` 按文件一一镜像（站点族聚合于 `oper/site.py` 等少数例外）。

```mermaid
flowchart LR
    Entry["API / Scheduler / Agent"] --> Command["Application Command / Chain"]
    Command -->|调用注入的 Port| Port["Application Protocol / DTO"]
    Port -->|运行时实现| Adapter["db/adapters<br/>Session / UoW 作用域"]
    Adapter --> Oper["db/oper<br/>查询 / stage / flush"]
    Oper --> Models["db/models<br/>显式 Session"]
    Models --> DB[("PostgreSQL / SQLite")]
    Command -.决定提交边界.-> Adapter
    Adapter --> UoW["db/uow.py<br/>commit / rollback"]
    UoW --> DB
    Command -.提交后副作用.-> Effects["Event / Scheduler / Report"]
```

图中 Port 到 Adapter 表示运行时调用；静态依赖仍为 `db.adapters → application Protocol`。

- Oper 只接收和返回持久化值；`MediaInfo` / `MetaBase` 与数据库行之间的转换属于业务逻辑，
  归 `app/application/`（见 `application/subscription/write.py`、`application/history/`）。
  订阅新增、查询、变更、删除、身份和搜索契约已经统一收口在 `application/subscription/`，
  不再保留主题包之外的第二个写入入口。
- 规范写入口中的 Oper 只 stage mutation，不创建独立 Session、不提交；Application Command
  通过请求或任务入口注入的 UnitOfWork 统一 `commit/rollback`，事件、刷新和上报只在 commit
  成功后执行。订阅新增 Port 位于 `application/subscription/write.py`，由
  `db/adapters/subscription.py` 创建独占 Session，`startup/composition/subscription.py` 只装配回调，
  `application/subscription/write.py` 决定事务与 post-commit 边界，`SubscribeOper.stage_add()`
  只查重、`add` 和 `flush`。旧 SDK 显式构造的无会话 Oper 暂留兼容自动短会话，不得被新代码复用。
  `transaction-debt-baseline.json` 要求 Model 上的查询/写装饰器持续保持为 0。Model 与 Base
  已不再导入数据库装饰器，所有 `db` 参数都要求显式 Session；这些方法只查询或 stage，不能
  创建、提交、回滚或关闭事务。无会话入口只存在于 Oper，由 `_execute_*` 经组合根事务执行器
  承接；内置插件必须调用 Oper，不得直接导入宿主 Model。AST 门禁同时约束装饰器、可选 Session
  和插件到 Model 的依赖，保证提交权不会被底层抢走。
- **Subscription typed boundary**：`application/subscription/contract.py` 是 Subscription 与
  SubscriptionHistory 完整快照、媒体身份、写 Patch 和 Repository Protocol 的唯一 owner；JSON 列在
  Session 内复制并深度冻结。Chain/Agent 的独立操作使用短事务 adapter，请求/API/Application Command
  使用绑定当前 Session 的 adapter；两者都不向调用方泄漏 ORM。旧订阅 Oper 模块和包根符号只经同一
  `sdk/_legacy/subscribe.py` + Compat 门面保留插件 ABI，不进入 canonical `__all__`。
- **Outbox 可靠副作用**：业务行与 durable intent 在同一 Session/UoW 中提交；提交后由
  Outbox dispatcher 依据 topic、claim/lease、有限重试和 dead-letter 执行。完成通知、事件和统计
  的 post-commit 逻辑必须保持幂等，不能用普通线程或 TaskRegistry 代替持久 intent。终态历史随统一
  数据维护任务分批清理，默认成功记录保留 30 天、dead letter 保留 90 天；总开关和两项保留期由
  高级设置维护，待投递和 lease 中记录不参与清理。
- **统一历史保留期**：所有可安全按时间回收的追加型数据均受 `DATA_CLEANUP_ENABLE` 控制，包括消息、
  下载及孤儿文件、站点快照、整理历史、下载失败冷却、订阅历史、Agent 会话、Agent 任务运行和 Outbox
  终态。Agent 会话会保护任务引用，Agent 运行会保护运行中与最后一次运行；`transferpending` 和
  `plugininstallation` 承担恢复语义，禁止按年龄删除。
- 站点、历史、工作流、Agent 会话删除和插件数据重置已经形成同构事务切片；对应 Application
  Command/Service 持有 UoW，Oper 的 `stage_*` 方法只修改当前会话。插件数据重置从
  `startup/initializers/plugins.py` 注入事务能力，插件直接使用 `PluginDataOper` 的旧 ABI 仅作兼容。
- 每次表结构变更必须新增 `database/versions/` 下的 Alembic 迁移。
- 运行期业务配置使用 `SystemConfigKey` 枚举 + `SystemConfigOper`，禁止裸字符串键；
  用户级配置使用 `UserConfigOper`。

### 5.5 其他横切模式

| 模式 | 说明 |
|---|---|
| **Config Reload** | 继承 `ConfigReloadMixin` 并声明 `CONFIG_WATCH`，配置变更时自动重建长生命周期对象（如下载器客户端重连） |
| **Singleton** | `EventManager`、`ModuleManager`、`PluginManager` 等全局共享管理器继承 `foundation/singleton.py` 的 `Singleton` |
| **Managed Resource** | 可选进程级技术资源（浏览器、虚拟显示等）以 data-only `capability.toml` 声明，`runtime/extensions` 解释生命周期，`startup` 构建 Runtime，消费者经 `runtime/resources.py` 显式获取；插件使用浏览器走 `app.sdk.browser` |
| **Observability** | `runtime/observability` 定义低基数指标和默认 no-op 端口，Startup 可选装配 OTel；HTTP、DB、Event、Module、Scheduler、插件生命周期和 Agent 只提交白名单标签 |

---

## 六、典型业务调用流程

### 6.1 搜索 → 过滤 → 下载

```mermaid
sequenceDiagram
    participant E as 入口（API / 消息命令 / Agent / 订阅）
    participant SC as SearchChain
    participant MC as MediaChain
    participant TC as TorrentsChain（索引）
    participant FC as 过滤（application/rules.py）
    participant DC as DownloadChain
    participant MOD as run_module 分发

    E->>SC: 按标题/媒体身份搜索
    SC->>MC: 识别与媒体信息补全（TMDB/Douban 模块）
    SC->>TC: 检索站点资源（indexer 模块 / 站点插件）
    TC->>MOD: search_torrents(...)
    SC->>FC: 按规则组过滤与排序
    SC-->>E: 返回候选（普通搜索到此结束）
    E->>DC: 用户或自动化用例提交选定资源
    DC->>MOD: download_torrent(...)（下载器模块）
    DC-->>E: 返回下载结果 / 发送通知事件
```

### 6.2 订阅全流程

```mermaid
flowchart TB
    A["用户创建订阅<br/>API / 消息 / Agent"] --> B["SubscribeChain<br/>写入订阅（application/subscription/write.py）"]
    B --> C{"调度器周期触发<br/>SubscribeChain.process"}
    C --> D["搜索缺失集数<br/>（复用搜索流程）"]
    D --> E{"命中资源？"}
    E -->|是| F["DownloadChain 下载"]
    F --> G["TransferChain 整理入库"]
    G --> H["刷新媒体库 + 发送通知"]
    H --> I{"订阅是否完结？"}
    I -->|否| C
    I -->|是| J["标记完成 / 发送完成通知"]
```

### 6.3 文件整理（Transfer）与监控

```mermaid
sequenceDiagram
    participant W as app/monitor 目录监控
    participant T as TransferChain
    participant R as 识别（domain/metainfo + application/recognition）
    participant FS as run_module 存储模块（具体 I/O）
    participant SC as ScrapingChain（刮削）
    participant EM as EventManager

    W->>T: 发现新文件，触发整理
    T->>R: 解析名称/路径得到 MetaBase
    T->>R: 匹配媒体（MediaChain）
    T->>T: 持久准入、计划与执行检查点
    T->>FS: 按目录配置转移/重命名
    T->>SC: 生成 NFO / 图片刮削（domain/scraper）
    T->>EM: send_event(TransferComplete)
    Note over EM: 媒体服务器刷新、通知、<br/>插件等订阅方各自响应
```

`application/transfer/` 定义准入、计划、稳定操作身份、执行检查点及结算端口，
`chain/transfer/` 负责识别、队列、存储分发、刮削与通知编排，`db/adapters/transfer/` 持有短事务实现。
启动时 `replay_pending_transfers()` 登记唯一恢复调度器，统一处理启动回放、同进程补偿与过期租约接管；
`retry_wait` 与 `manual_review` 分别表达可重试和需要人工核实的执行状态，不能把入队当作整理完成。

### 6.4 消息命令交互

```mermaid
flowchart LR
    CH["消息渠道模块<br/>telegram / wechat / slack ..."] --> MP["MessageProcessingMixin<br/>（ChainBase）"]
    MP --> RT["application/messaging/router.py<br/>统一优先级与回调分发"]
    RT --> CMD["Command 命令注册表<br/>（application/commands.py 门面）"]
    RT --> IT["InteractionChainMixin<br/>斜杠命令 / 按钮回调"]
    RT --> PLG["插件输入接管<br/>（messaging/plugin.py）"]
    RT --> AG["Agent 会话<br/>（messaging/agent.py → application/agent.py）"]
    CMD --> Chain["对应业务 Chain"]
    IT --> Chain
```

- `app/application/messaging/` 负责渠道回环入口（`ingress.py`）、消息渲染、模板、队列（`message.py`）、交互会话与视图；
  业务工作流仍由对应 Chain 执行（如媒体交互的业务部分在 `MediaInteractionChain`）。
- 该包不作为推荐给插件直接使用的公开 SDK。

### 6.5 搜索分页与音乐识别

- **分页搜索**：`chain/search/` 的 `facade.py` 保留公开入口，`manual.py`、`scan.py` 与
  `pagination.py` 协调分页；`application/search/state.py` 拥有搜索状态，`session.py` 定义
  检查点端口，`domain/search.py` 表达收集与来源游标规则。页面候选和游标一起保存，
  检查点仅保存允许的资源字段，凭据与临时下载票据不进入快照。
- **页诊断与最近上下文**：`manual.py` 的每次请求持有站点观察、过滤计数与规则适用性，
  `schemas/search.py` 定义可选诊断合同；不保存原始错误或凭据。`state.py` 经注入缓存端口
  按认证用户一次写入完整参数、结果和诊断，API 不拼接旧全局散键；普通搜索以空诊断
  发布自己的完整记录，避免显示其它请求的过滤说明。
- **音乐识别**：`application/audio.py` 读取本地标签与 CUE，`domain/meta/` 和音乐领域规则解释
  文件名、标签、目录、艺人、发行与曲目身份；`application/music/recognition.py` 经注入的
  来源 Port 有界回退，`observation.py` 记录本次来源结果和共享预算。Domain 不持有来源客户端。
  `chain/transfer/music.py` 负责批次准入与目录共识，`chain/subscribe/music.py` 使用下载/整理历史
  对账专辑入库事实；匹配不到唯一身份时保留歧义保护。

---

## 七、AI Agent 子系统

复杂任务的计划、工具发现和中断恢复由 `app/agent/` 的实现决定，本文只说明其在宿主中的位置与依赖边界。

Agent 采用**门面 + 惰性物化**设计，Chain 经应用门面访问，
启动组合根注册轻量 Provider，避免未启用时加载完整 Agent 运行时：

```mermaid
flowchart TB
    Entry["消息渠道 / API / MCP"] --> Facade["app/application/agent.py<br/>编排门面（get_agent_manager 等）"]
    Reg["app/startup/initializers/agent.py<br/>生命周期显式注册/重置 Provider"]
    Reg --> Facade
    Facade -.能力启用或首次使用时物化.-> RT["app/agent/loader.py<br/>能力发现与服务物化"]
    RT --> Manager["app/agent/manager.py<br/>稳定 AgentManager 门面"]
    Manager --> Session["session.py / lifecycle.py<br/>会话队列与有界生命周期"]
    Manager --> Tasks["tasks.py<br/>后台、调度与心跳任务"]
    Session --> ORC["orchestrator.py<br/>MoviePilotAgent 图与工具编排"]
    Tasks --> Session
    ORC --> Tools["app/agent/tools<br/>系统工具（经 application 门面）"]
    ORC --> LLM["app/agent/llm<br/>LLM 提供商管理"]
    ORC --> MW["middleware / policy / memory / skills"]

    style Facade fill:#eef
```

约束要点：

- Chain 访问 Agent 运行时只能经 `app/application/agent.py`；
  `app/chain/agent.py` 的 `AgentChain` 是链层入口，Agent 实现保持在 `app/agent/`。
- `app.agent.AgentManager` 是插件稳定路径，精确解析到
  `app/agent/manager.py`；宿主直接导入 owner，包根不得通配转发
  `orchestrator.py` 的任意内部名称。
- `app.agent.llm` 包根不承载实现或动态通配导出；宿主直接导入
  `llm/helper.py`、`llm/capability.py`、`llm/auth.py`、`llm/provider.py` 等 owner。
  旧 `app.agent.llm.LLMHelper` 由 Compat 精确叠加，`app.helper.llm` 直接映射到
  canonical `llm/helper.py`，三条插件路径保持同一类身份且不公开 Provider 内部 owner。
- 官方插件使用的 `MoviePilotTool` 与 `moviepilot_tool_manager` 继续由
  `agent/tools/base.py`、`agent/tools/manager.py` 原位拥有，现有 `_load_tools()` 调用保持兼容。
- Agent 工具不直接 import API / 调度器 / 命令：插件动态路由与文件夹操作使用
  `application/plugin/routes.py`、`application/plugin/folders.py`，调度和命令分别使用
  `application/scheduling.py`、`application/commands.py`。FastAPI 具体实现位于
  `adapters/web/plugin/`，入口层不承载路由实现。
- 对外暴露 MCP 端点 `/api/v1/mcp` 与 OpenAI / Anthropic 兼容端点，
  错误响应在 `app/factory.py` 中按协议原生格式单独处理（不走统一 `Response` 包装）。

Agent 内部状态按责任分开保存：

| 能力 | 所有者与边界 |
|---|---|
| 计划、工具发现与执行纠偏 | `middleware/plan.py`、`selection.py`、`guardrails.py`；计划和历史只提供上下文，实际调用仍须经过权限与执行合同 |
| 业务工具 | `policy/api.py` 固定 `moviepilot_api` 的 operation 注册表，工具经 Application 门面访问业务；外部 MCP 工具目录按请求投影 Schema |
| 写入回执 | `middleware/invocation.py` 经 `application/invocation.py` Port 认领与结算，`db/adapters/invocation.py` 提供持久化；回执未知不等于成功，也不能盲目重试 |
| 历史回忆 | `application/messaging/recall.py` 定义 DTO/Port 和维护服务，`agent/history/` 持有按用户隔离的独立 SQLite/FTS5 消息库；不共享主库 Session，旧快照仅由 `db/adapters/recall.py` 只读导出 |
| 个人技能与学习 | `agent/learning/`、`middleware/learning.py` 管理按用户隔离的技能、稳定记忆与复盘；后台快照不能授予新执行权限 |
| 终端与 Python 会话 | `agent/terminal/`、`agent/code/` 持有会话身份和授权；通用 Python 子进程 I/O 在 `adapters/system/code/`，不拥有用户和业务 API 权限 |

`middleware/recall.py` 在压缩或截断前采集脱敏证据，历史库的索引、旧数据回填与学习任务由
TaskRegistry/会话 owner 有界调度，导入阶段不打开消息库。Agent 主库会话/任务数据、独立历史库、
个人技能文件与临时工具输出各有生命周期，不能混用主库 Alembic 或把临时输出当作长期记忆。

---

## 八、插件系统与 SDK / 兼容边界

```mermaid
flowchart TB
    subgraph 插件侧
        P["app/plugins/*<br/>（运行时副本/覆盖层）"]
    end

    subgraph 宿主边界
        SDK["app/sdk<br/>新插件稳定导入面<br/>network / cache / logging / browser ..."]
        Compat["app/runtime/compat<br/>manifest 精确模块/符号映射<br/>app.core / app.helper / app.utils / app.log 及已删除旧路径"]
        PM["PluginManager<br/>发现 / 生命周期 / 事件桥接"]
    end

    subgraph 规范实现
        Canonical["foundation / domain / runtime /<br/>application / adapters / chain ..."]
    end

    P -->|新插件推荐| SDK
    P -->|旧插件（DEBUG 下告警）| Compat
    SDK -->|门面转发| Canonical
    Compat -.惰性解析.-> SDK
    Compat -.惰性解析.-> Canonical
    PM -->|run_plugin 方法分发 / 事件广播| P
    Canonical -.-|禁止 import| Compat
    Canonical -.-|禁止 import| SDK
```

- `app/plugins/` 是运行时插件副本，不是官方插件仓库的源码副本；它不纳入宿主架构拆分的源代码审计。
  宿主代码只使用 canonical 路径；只有运行时插件与兼容性测试可用旧路径。
- `compat` 只存字符串映射并惰性解析，不得在模块导入期急切 import canonical 实现；
  canonical 包也不得为兼容而反向 import `compat` / `sdk`。
- 插件 API 的动态注册/移除及端口协议统一位于 `app/application/plugin/routes.py`，
  FastAPI 技术实现位于
  `app/adapters/web/plugin/routes.py`；FastAPI 实例由组合根（`app/factory.py`）在创建后注入，
  端点层禁止直接依赖 `factory`。
- 动态插件路由使用原生 `APIRoute`，插件自行决定返回结构；主程序的统一 `Response` 封装只适用于
  `app/api/` 的宿主端点。插件若已经自行返回 `Response`、字典、列表或其它可序列化值，宿主不再二次包裹。
- `app/runtime/extensions/plugin/manager.py` 是 canonical 管理器 owner，发现、加载、生命周期、
  目录、同步等实现共同归入 `app/runtime/extensions/plugin/`。旧插件仍从 `app.core.plugin` 或
  `app.sdk.plugins` 进入，并由 Compat 精确路由到同一个 `PluginManager` 身份；新入口是
  `app.sdk.plugin.manager`。
- `app/sdk/plugin/base.py` 是插件契约基类 `_PluginBase` 与 `PluginChain` 的 owner。
  `app/plugins/` 只是插件安装命名空间，包根不再导出符号，`app.plugins._PluginBase` 与历史拼写
  `app.plugins.PluginChian` 由 Compat 精确叠加承接。
- 插件可参与 `run_module` 方法分发（同名方法优先响应）并注册事件处理器。

---

## 九、运行时基础设施

### 9.1 runtime 与 adapters 的分工

```mermaid
flowchart LR
    subgraph runtime["app/runtime（契约 + 内存行为）"]
        RC["config.py<br/>Settings / 部署配置"]
        RE["events.py<br/>事件总线"]
        RL["log.py<br/>日志运行时（依赖叶子）"]
        RCA["cache.py<br/>缓存协议 / 内存后端 / 装饰器"]
        MR["resources.py<br/>托管资源门面"]
    end

    subgraph adapters["app/adapters（具体 I/O）"]
        AC["cache/backends.py<br/>Redis / 文件缓存"]
        AN["network/http.py<br/>同步 + 异步 HTTP 客户端"]
        AB["network/browser.py<br/>浏览器会话"]
        AS["system/*<br/>OS / 文件 / 进程 / Rust"]
        AE["external/*<br/>市场 / OCR / MP Server"]
    end

    Startup["app/startup<br/>启动期注册具体缓存工厂"]
    Startup --> RCA
    Startup --> AC

    Caller["Application / Chain"] -->|装饰器/协议| RCA
    RCA -.运行时调用注入后端.-> AC
    Caller --> Port["注入的网络 / 浏览器 Port"]
    Port -.运行时调用.-> AN
    Port -.运行时调用.-> AB
    Startup -->|构造并注入| Port
```

- **普通 HTTP 统一通过 `RequestUtils`**（`app/adapters/network/http.py`）；
  网络 SDK、流协议、vendor 和诊断/控制面仅允许规则 09 与人工 policy 中的精确例外；
  这不授权 Application/Chain 直接导入具体 Adapter，插件使用 `app.sdk.network`。
- `app/runtime/log.py` 是依赖叶子（无任何 `app.*` 导入）；`foundation` 不输出运行日志，
  由上层所有者决定是否记录。
- 缓存契约与内存后端在 `runtime/cache.py`，Redis/文件实现在 `adapters/cache/backends.py`，
  由 startup 在被装饰的业务模块导入前完成工厂注册；Redis 实现仅在选用该后端时加载。
- **消息协议与结构化通知**：Schema 的 `NotificationContent` / 段落结构表达通用语义，
  `foundation/text.py` 提供无状态格式转换；渠道依据 `ChannelCapabilities` 选择格式与分段预算，
  插件同时保留纯文本回退。Telegram Bot API、飞书 OpenAPI 和 Web Push 的宿主实现复用
  统一 HTTP 传输，飞书长连接等协议入口按精确出口 policy 管理。
- **安全策略与技术 I/O**：URL/路径/SSRF 与图片域名白名单策略归 `application/security/`，
  DNS、HTTP 和文件操作归 Adapter。白名单快照不替代每次实际访问的安全判定。
- **社区勋章缓存**：`application/community/medals.py` 经注入的 GitHub Port 维护实例快照；
  生命周期主动预热，TaskRegistry 定期刷新，请求只读本地快照，不等待远端下载。

### 9.2 数据与配置总览

| 类别 | 载体 | 说明 |
|---|---|---|
| 持久化业务数据 | `app/db/models` + Alembic | 每次 schema 变更必须配套迁移 |
| 运行期业务配置 | `SystemConfigKey` + `SystemConfigOper` | 用户可编辑、跨重启持久 |
| 用户级配置 | `UserConfigOper` | 按 `user_id` 隔离 |
| 部署配置 | `runtime/config.py` 的 `Settings` | 环境变量 / `app.env` |
| 缓存 | `runtime/cache.py` 装饰器 | Redis 或文件后端可切换 |
| 站点资源 | `app/application/site/sites.*` + `user.sites.v3.bin` | Build 仓生成、Resources 仓发布，宿主安装/加载；不写入虚拟兼容目录 |
| Agent 历史与学习 | `agent/history/`、`agent/learning/` | 独立消息库及个人文件按用户隔离，与主库会话表分工 |
| 进程内任务 | `runtime/tasks.py` + 领域生命周期 | TaskRegistry 管取消和等待；跨重启恢复使用持久执行记录/Outbox |

---

## 十、架构治理

### 10.1 事实、策略与自动检查

宿主架构统计排除 `app/plugins/**`。生成快照记录源码事实，人工 policy 决定精确准入边界；
快照一致不等于架构合理，也不能用刷新基线来接受新债务。

| 检查对象 | 事实与约束 |
|---|---|
| 依赖方向与循环 | `test_architecture_dependencies.py` 构建完整模块图；`dependency-baseline.json` 保存模块、边和 SCC，`dependency-policy.json` 仅容纳精确的 TMDB vendor SCC |
| Adapter 与网络出口 | `test_architecture_adapter_imports.py`、`test_architecture_egress.py` 分别约束具体 Adapter 直连与 raw transport/SDK/协议操作；出口的 source、target、用途与 fingerprint 均须匹配 policy |
| 事件合同 | `scripts/architecture/event_facts.py` 是生产/消费事实的唯一收集器；`runtime-contract-policy.json` 独立准入消费者身份，禁止靠生成快照绕过语义检查 |
| 事务和配置 | `transaction-debt-baseline.json`、`configuration-debt-baseline.json` 检查底层事务所有权和配置边界；Model/Oper 不得重新创建或提交独立 Session |
| SDK / Compat | manifest、SDK 导出和官方插件快照共同核对旧导入路径、符号身份与行为边界；迁移时同步 canonical 导入、公开契约及兼容测试 |
| 类型与复杂度 | `mypy.ini` 默认严格检查全部宿主，类型错误预算为零；`mypy_policy.py` 锁定兼容例外、检查配置与本地类型桩，禁止新增豁免；复杂度约束 C901/PLR1702，行数只作观察 |
| 后台与启动 | 并发所有权、异步阻塞、TaskRegistry/生命周期测试和启动性能检查；结构计数与耗时预算分开维护 |
| 行为与覆盖率 | pytest 离线隔离执行；CI 按全部宿主文件的覆盖率低水位检查，新文件至少 80%，本机覆盖率不能回写 canonical baseline |

所有权迁移须同步 canonical 导入、Compat manifest、公开 SDK（若涉及）、规则 05 和对应测试。
延迟导入不能用来隐藏循环；`--write-host` 不会改写人工 policy，也不能替代语义审查。

### 10.2 当前架构快照

以下是 2026-11-01 核对的 `tests/fixtures/architecture/` 数据；这是源码合同快照，
不代表某个已发布镜像或正在运行的实例已经验证。

| 指标 | 当前值 |
|---|---:|
| Python 模块 | 1146 |
| 内部导入边 | 9,913 |
| 非平凡 SCC | 1（29 个模块的 TMDB vendor 精确 containment） |
| Application / Chain 具体 Adapter 直连 | 0 / 0 |
| Direct egress | 51（精确 policy 准入，Application / Chain 均为 0） |
| Module Contract V2 spec | 224（其中 220 个方法进入 `run_module` 观察面） |
| Event Contract | 53 |
| Event producer / consumer | 86（85 静态、1 动态）/ 17（16 静态、1 动态） |
| Model/Oper 自动事务与自建 Session | 0 |
| 组合根外 `SystemConfigOper()` | 0 |

保留的 SCC 是移植的 TMDB 包内部环，不得扩大或吸收包外模块；精确出口准入也不授予同包其他模块
网络权限。配置驱动的工作流事件注册是当前唯一动态 consumer，插件自定义事件类型可保持动态。

### 10.3 检查入口

使用目标项目的锁定虚拟环境；共享工作区的解释器映射见[开发环境设置](development-setup.md)。
以下是文档与架构的核对入口，修改生产行为仍须按[测试规范](testing.md)扩展验证范围：

```bash
# 文档结构与架构快照合同
uv run --locked --no-sync pytest tests/test_architecture_documentation.py \
  tests/test_architecture_contract_baseline.py -q

# 当前宿主事实与快照是否一致；此命令不写基线
uv run --locked --no-sync python scripts/architecture/baseline.py --check-host
```

CI 的 `Architecture Contract Gate` 先检查依赖、Adapter、出口与事件语义，再核对宿主快照；
`Architecture Ratchet Gate` 执行类型、复杂度与并发等治理检查，行为测试、覆盖率、启动性能和
Pylint 分别提供对应证据。验证结论应绑定实际源码提交，不能引用旧一轮成功记录推断当前状态。

---

## 附录：相关文档索引

| 文档 | 内容 |
|---|---|
| [项目概述](rules/01-project-overview.md) | 系统目标与业务范围 |
| [技术栈](rules/02-tech-stack.md) | 技术栈与第三方集成 |
| [设计模式](rules/04-design-patterns.md) | Module / Chain / Event / Oper 模式细则 |
| [架构规则](rules/05-architecture.md) | 层边界、依赖方向与关键文件位置（权威） |
| [外部接口约定](rules/09-external-response.md) | HTTP、协议适配与响应格式 |
| [数据与持久化](rules/10-data-and-persistent.md) | 数据模型、事务、迁移与缓存 |
| [开发环境设置](development-setup.md) | 锁定依赖、环境映射与治理检查 |
| [测试规范](testing.md) | 隔离、测试范围与 CI 证据 |
| [MCP 接入](mcp-api.md) | 外部 Agent 连接、认证、工具发现和调用 |
| [CLI](cli.md) / [Doctor](doctor.md) | 本地管理与故障诊断 |
