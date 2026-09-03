# 05 - Architecture and Modules

## Directory Model

MoviePilot keeps the established product packages such as `app/chain`,
`app/agent`, `app/modules`, `app/db`, `app/api`, `app/startup` and
`app/workflow` in their original locations. The historical `app/core`,
`app/helper` and `app/utils` roots are virtual compatibility packages only;
physical Python sources must not be recreated there.

The legacy roots have no physical directories in the source tree. Current
images and update flows write site resources only to `app/application/site/`;
plugin imports under `app.helper.*` are resolved exclusively by the exact
runtime compatibility manifest.

Capabilities migrated out of those legacy roots are organized by technical
responsibility:

```text
Entrypoints / Plugins
        |
        v
API / Agent / CLI / Scheduler / Workflow
        |
        v
Chain orchestration ---------> Application services
        |                              |
        +----------> Modules / DB <----+
                       |
                       v
             Domain / Runtime contracts
                       |
                       v
              Foundation / Adapters

Startup remains the composition root. SDK and compatibility are boundaries,
not dependencies of canonical implementation modules.
```

Directory grouping does not override dependency direction. The architecture
gate builds the complete Python module graph and rejects cycles even when a
cycle passes through an established package that was not moved.

## Canonical Migrated Packages

| Package | Ownership |
|---|---|
| `app/foundation/` | Stateless, config-free and I/O-free primitives: reflection and dynamic import, crypto, DOM parsing, identity, collections, singleton, text conversion/segmentation, URL and version helpers |
| `app/domain/` | Pure MoviePilot business semantics for media, recognition, sites and torrents; `projection/` owns immutable external-source-to-`MediaInfo` field mapping; live configuration, persistence, transport and acceleration are injected |
| `app/application/` | Focused stateful application services, configured capability selection and service-bound rules |
| `app/runtime/` | Process-wide config, events, complete logging runtime, cache contracts/in-memory policy, execution, background-task ownership, localization, scheduling, restart state, concurrency, GC and rate limits |
| `app/adapters/` | Concrete technical I/O and named external ecosystems, split by cache, network, system and external boundaries |
| `app/sdk/` | Stable, deliberately curated imports for plugin authors |

The packages above are the only top-level roots created by the legacy-module
refactor. Existing product roots remain unchanged rather than being moved only
to make the directory tree look symmetrical.

### Application boundaries

| Path | Ownership |
|---|---|
| `app/application/*.py` | Established single-module application services and compatibility facades |
| `app/application/subscription/` | Subscription use cases: `write.py` owns media-to-row translation and the write port; `contract.py` owns shared metadata/media-key projection; query, mutation, deletion, identity and search stay in their single-word modules |
| `app/application/search/` | Search state and later search-plan use cases |
| `app/application/download/` | Download task querying/control and selection use cases; `failures.py` owns the frozen failure-cooldown write/query DTOs and persistence Port |
| `app/application/history.py` | History use cases and persistence contracts; DownloadHistory and TransferHistory own deeply frozen DTOs plus typed query/write/staging ports |
| `app/application/music/` | Multi-source music catalog orchestration |
| `app/application/chain/` | Injectable Chain runtime capabilities: `context.py` owns the typed runtime and persistence dependency aggregate, and `events.py` owns durable event write contracts plus replayable payload conversion |
| `app/application/agent.py` | Agent orchestration facade and typed `AgentDataContext`; startup injects one explicit data context into the manager, memory, tool and scheduler owners without a process-wide persistence locator |
| `app/application/network.py` | System network-test target catalog, immutable public/private projections, URL and redirect admission, response validation and the injected transport Port; startup owns concrete HTTP Adapter assembly |
| `app/application/outbox.py` | Durable intent, transaction-only stager, short-transaction dispatch store, claim fencing and structured post-commit result contracts |
| `app/application/transfer/` | Durable transfer use cases: `workflow.py` owns admission/planning/queue behavior; `execution.py` owns stable operation identity, step/checkpoint state, retry/manual-review commands and terminal-settlement DTOs |
| `app/application/plugin/` | Plugin market catalog, installation command, installed-plugin identity contract and startup migration, runtime port, folder operations and dynamic-route use cases; filenames remain single words (`catalog.py`, `identity.py`, `migration.py`, `install.py`, `runtime.py`, `folders.py`, `routes.py`) |
| `app/application/server/` | MoviePilot Server reporting and sharing use cases; local data readers and transport callbacks are injected by startup |
| `app/application/site/` | Configured site catalog, authentication level and index-resource capability; the generated extension and its data bundle stay together here |
| `app/application/messaging/` | Message rendering/routing, interactions and the Agent-to-message bridge: `ingress.py` owns the single channel-to-host loopback boundary; `interaction.py` shared interaction contracts and view helpers; `router.py` unified interaction priority and callback dispatch; `site.py`/`subscribe.py`/`skill.py` per-command sessions, input parsing and views; `media.py` media interaction state while the business workflow stays in `MediaInteractionChain`; `plugin.py` plugin input capture and plugin button callbacks; `agent.py` owns Agent choice state, callback protocol, bounded WebAgent event publication, display projection, session identity/persistence coordination, temporary attachment registry and audio preparation/transcription; `message.py` notification rendering, templates and queue. Not a public SDK recommended for direct plugin use |
| `app/application/security/` | Authentication, authorization, frozen user/auth projections, atomic user aggregate commands, per-user configuration publication, cookies, passkeys, OTP/two-factor, path/URL safety, SSRF and signing policy |

### Agent runtime boundaries

| Path | Ownership |
|---|---|
| `app/agent/manager.py` | Stable `AgentManager` facade and process singleton identity; no session, lifecycle or scheduled-task implementation |
| `app/agent/session.py` | Per-session queues, workers, active Agent instances, status projection, cancellation and deferred cleanup |
| `app/agent/lifecycle.py` | Runtime generation admission, initialization, idle-session collection and bounded manager shutdown |
| `app/agent/tasks.py` | Background prompts, durable scheduled-task execution and heartbeat wakeups |
| `app/agent/orchestrator.py` | One `MoviePilotAgent` execution instance: prompt/tool/middleware assembly, model invocation, streaming and per-agent state |
| `app/agent/shell.py` | Agent command-shell selection and subprocess text-encoding policy; Windows prefers Git Bash, then PowerShell 7, while POSIX keeps native shell/PTY behavior |
| `app/agent/policy/api.py` | Fixed `moviepilot_api` operation registry, HTTP route templates and per-operation authorization/effect policy; no arbitrary URL or method input |
| `app/agent/policy/mcp.py` | Generated external MCP input-contract builder for the fixed API registry; owns exact English oneOf parameter projection, not runtime authorization |
| `app/agent/tools/impl/service.py` | Admin-only external MCP wrappers for downloader, media-server and database Skill scripts; synchronous scripts run only through the Agent blocking executor |

`app.agent.AgentManager` remains the stable plugin-facing import and resolves lazily to
`app.agent.manager.AgentManager`. Historical symbols formerly imported from
`app.agent.orchestrator` are preserved only by exact Compat aliases; the physical
orchestrator must not re-export manager/session implementations. The package root
uses a reviewed symbol whitelist and must not restore an unbounded `__getattr__`
forwarder.

The policy and service modules above are host-internal owners. They are not SDK or
Compat surfaces, and retired multiword module paths must not be restored as aliases.

Application services may use domain rules and runtime contracts. They own the
persistence Protocol needed by a use case, but must not import `app.db`,
SQLAlchemy, Session, Oper classes or concrete adapters. `app/db/adapters/`
implements those Protocols and startup injects the implementation. Multi-domain
workflows still belong in the existing `app/chain/` package. `Chain`, `Service`
and `Manager` remain class patterns; they do not create additional top-level
directory categories.

`app/api/endpoints/agent.py` must not recreate WebAgent file registries, audio
conversion/transcription policy, traditional-message dispatch, Agent execution
lifecycle, event-to-display projection or AgentChat snapshot writes. Those reusable
state transitions and the transport-neutral event-stream use case belong to
`app/application/messaging/agent.py`; the endpoint maps FastAPI principals and DTOs,
translates upload/domain failures to HTTP responses, and frames Application events
as SSE.

### Runtime boundaries

| Path | Ownership |
|---|---|
| `app/runtime/config.py` | Deployment configuration and resolved runtime settings |
| `app/runtime/topology.py` | Process topology policy shared by startup and offline diagnostics |
| `app/runtime/events.py` | Event contracts, dispatch and resolver registration |
| `app/runtime/event/` | Event registry, explicit handler binding, dispatch barrier/concurrency and isolated error handling |
| `app/runtime/observability/` | Low-cardinality metric contracts and no-op-capable observation facade |
| `app/runtime/log.py` | Complete console/plugin/file logging runtime and shutdown |
| `app/runtime/cache.py` | Cache protocols, memory implementations, decorators and proxies |
| `app/runtime/resources.py` | Provider-neutral acquisition, observation and shutdown facade for process-owned optional resources |
| `app/runtime/tasks.py` | Lifespan-scoped ownership, cancellation and bounded shutdown waiting for in-process background tasks |
| `app/runtime/execution.py` | Shared sync/async execution and cross-thread submission boundary with correlation propagation |
| `app/runtime/correlation.py` | Request/cross-thread correlation context and safe propagation into logs and child work |
| `app/runtime/state.py` | Process restart and update state |
| `app/runtime/dependencies/` | Runtime dependency profile selection in `profile.py` and loaded native payload activation detection in `native.py`; the package root contains no implementation or host re-exports |
| `app/runtime/extensions/` | Module, plugin, configured-service and managed-resource discovery/registration/lifecycle adapters |
| `app/runtime/compat/` | Standard-library-only exact legacy import routing, resource preflight scanning and DEBUG diagnostics |

`app/startup/` remains the established composition root and is not nested under
runtime. Its root contains only `composition/`, `initializers/` and `lifecycle/`:
composition constructs and injects cross-layer dependencies, initializers expose
domain-scoped startup/shutdown hooks, and lifecycle orders those hooks and decides
restart policy. Reusable persistence implementations belong in `app/db/adapters/`,
not startup. Lower-level runtime modules must not import startup.
`startup/composition/configuration.py` owns configuration snapshot loading, typed
runtime/settings construction and publication. `startup/composition/database.py`
owns the process database worker, compatibility transaction runner, query services,
plugin persistence and workflow query wiring. Initializers call these owners in
startup order and must not recreate their concrete construction.
`startup/composition/runtime.py` is the sole constructor of `HostRuntime`, its named
domain Runtime projections and the legacy `ApiDataPorts` projection. It first creates
one frozen `RuntimeDependencies` aggregate; Agent, Chain and HostRuntime must reuse the
same repositories, transfer-execution ledger and message owners rather than constructing
parallel instances. Importing this owner remains cold: concrete DB adapters are resolved
only when composition functions run, and FastAPI `app.state` publication remains owned by
the lifecycle layer.
`startup/composition/chain.py` owns the no-argument Chain compatibility context,
typed persistence/dispatch object graph, lazy legacy Transfer command binding and
wallpaper-provider publication. It also owns MoviePilot Server share and filesystem
Adapter wiring required by Chain ports. `initializers/chain.py` is only the lifecycle
hook; `initializers/modules.py` passes the already constructed `RuntimeDependencies`
in startup order and must not recreate those dependencies.
`startup/composition/security.py` owns authentication, user lookup, PassKey and Web
access provider assembly; it returns the persistence factories needed by the runtime
owner, which alone constructs `AuthenticationRuntime`.
`startup/composition/network.py` owns concrete network-test, image, internal-address,
message-ingress, DoH and Chain synchronous network/system Adapter wiring while
retaining lazy RuntimeSettings reads. Initializers call these owners in startup order
and must not recreate their concrete construction.
`startup/composition/domain.py` owns DNS/System/Rust Adapter assembly together
with the single `RecognitionRuleService` used to publish media-recognition rule
providers. `initializers/domain.py` is only the lifecycle hook and must not
construct a parallel rule service or import concrete adapters.
`startup/composition/site.py` owns RSS, site login, captcha, torrent and SiteChain
network/browser/OCR Adapter assembly. `initializers/site.py` is only the lifecycle
hook and must not import or construct those concrete adapters.
`startup/composition/cache.py` owns concrete platform cache registration.
`initializers/cache.py` is only the startup hook and must not import cache adapters.
`startup/composition/resource.py` owns managed-resource Adapter construction and site
resource installation. `initializers/resources.py` only exposes lifecycle hooks; the
modules initializer retains only the decision to restart after an actual installation.
`startup/composition/database.py` also owns the concrete transactional Workflow
execution service; the modules initializer only invokes its configure/reset lifecycle.
`startup/composition/outbox.py`
owns durable handler validation, lazy event/notification replay binding and the
short-transaction dispatcher factory. Initializers call this owner in
startup order and must not recreate its concrete construction.
`startup/composition/server.py` owns MoviePilot Server report/share service
construction and registration, including lazy local-data readers and external
transport callbacks; `initializers/modules.py` retains only its startup-order call
and the later explicit network warmup. Database runtime
`startup/composition/agent.py` owns the single `AgentDataContext`, chat persistence and
autonomous-task repository/execution service set. The runtime owner projects those same
objects into `AgentChatRuntime`. Agent composition may accept
the Agent initializer's data-context registrar as a callback, but must not import
`startup.initializers`; tool-manager, scheduler and Agent activation order remains in
the initializer. Database runtime ownership rejects overlapping lifespans. After a
successful worker shutdown, `initializers/modules.py` invokes the explicit reverse-order
reset manifest: event/resource/security/Chain/managed-resource/Scheduler/tool/Agent,
then transfer/outbox/workflow/server/runtime/database/network/configuration providers,
and only afterward releases database connections. Startup rollback uses the same reset
manifest and never materializes an owner merely to stop it. A failed database shutdown
retains the runtime owner, every provider and the connection boundary for diagnosis and
retry instead of publishing a second owner or silently entering a partial lifespan.
Startup publishes its frozen, slotted `HostRuntime` through FastAPI `app.state`.
API dependencies must narrow that object to a domain runtime (for example,
`AgentChatRuntime`) instead of adding a string key to a global service map.
Legacy registries may delegate the same object while domains migrate, but they
must not construct a second set of service instances.
The retired flat `app/runtime/dependencies.py` and
`app/runtime/native_dependencies.py` modules must not return. Canonical host
callers import `app.runtime.dependencies.profile` or
`app.runtime.dependencies.native` directly; the package root is not an export
facade, and plugin compatibility is added only through an exact SDK/Compat route
when a verified plugin contract requires it.
Canonical host consumers of the process-wide module, plugin, scheduler and
system-configuration runtimes must call `get_module_manager()`,
`get_plugin_manager()`, `get_scheduler()` and `get_configured_system_config()`
explicitly. The class-shaped `ModuleManager` and `Scheduler` application facades,
the concrete plugin manager class paths and DB `SystemConfigOper` remain
compatibility or composition boundaries; host code must not import those facades
or alias a getter back to a manager/Oper class name.
API, Scheduler and Chain deployment values are exposed as frozen snapshots from
`HostRuntime.configuration`; canonical callers must not add a fresh direct
`settings` import when the required field belongs to an existing snapshot.

The historical `app/scheduler.py` monolith must not be recreated. Scheduler
implementation lives in the same-named `app/scheduler/` package: `catalog.py`
owns built-in job declarations and APScheduler projection, `execution.py`,
`bridge.py` and `progress.py` own execution and completion mechanics,
`registry.py` is the sole owner of generations, active ownership, reservations
and handles, `reconcile.py` owns dynamic Agent/Workflow/plugin job projection,
and `lifecycle.py` owns init, reload and shutdown. Business Chain instances are
constructed only by `app/startup/initializers/scheduler.py` and injected as the
frozen callable-only `SchedulerServices`; files under `app/scheduler/` must not
construct a business Chain. The `app.scheduler` package root lazily preserves
only the historical `Scheduler` and `SchedulerChain` identities. New plugins use
the narrow `app.sdk.scheduler` facade; internal Scheduler owners must not be
re-exported from the package root, SDK or Compat.

Complexity and concurrency governance covers the complete canonical execution
surface rather than only public methods. `scripts/architecture/complexity.py --v2`
uses complete AST child traversal to ratchet private/dunder/nested methods,
class/file hotspots, and the `app/scheduler/` package, including owners nested
under `Match`, `TryStar`, and other control-flow nodes. Its generated baseline
is an evidence ledger, not permission to add another oversized owner.
`scripts/architecture/concurrency.py` resolves canonical imports and aliases for
native Thread, Timer, thread/process executors, Process, TaskGroup, event-loop
task submission, asyncio task/thread helpers, and executor hand-off calls. It
scans the complete `app/**` host tree and aggregates each canonical target by
stable lexical owner and call count, so physical line movement is not semantic
drift and multiple same-line calls cannot collapse. New owners/count growth and
stale baselines after count reduction all block CI. `app/plugins/**`, `app/sdk/**`, and
`app/runtime/compat/**` are excluded because their compatibility/runtime
contracts are governed by their own exact manifests.

`app.schemas` and the `app.db` package root are compatibility facades, not
implementation dependency hubs. Host code imports concrete schema submodules; the schema root
resolves its generated export manifest lazily for plugins and legacy callers.
DB internals import `base`, `decorators`, `engine`, `session`, concrete models
and Oper modules directly. `app.db.models.load_all_models()` is the explicit
composition entry used before metadata creation or migration; importing one
model must not import every table.

`app/db/oper/` owns table-oriented SQLAlchemy access and receives a caller-owned
Session. `app/db/adapters/` is the concrete persistence-adapter layer: it may
depend on Application-owned Protocols, UoW/Session and Oper implementations.
This deliberate dependency inversion is the only `DB implementation ->
Application contract` direction; Application must remain free of DB imports.
User, interaction, messaging, music, site, media-server, download, subscribe and transfer
Chain consumers receive typed repositories through `ChainRuntimeContext`; `ChainBase` copies the
context fields to instance-owned capabilities during construction. The deleted process-wide
`chain/data.py` locator, migration-time `*PortProxy` classes and dynamic `__getattr__` forwarding
must not be recreated; they had no host, SDK or plugin consumers. Workflow execution uses its
owning `app.application.workflow` service directly and is not duplicated in the Chain context.
Download-failure cooldown and media-server cache consumers use their typed Application DTO/Port
contracts. Their DB adapters project ORM values before Session close and commit each local write
in a separate short UoW, so remote media enumeration never holds a database transaction.
Agent orchestration, memory, tools and scheduler receive the single typed `AgentDataContext`
declared in `app/application/agent.py`. The deleted `agentdata.py` locator and its getter surface
were host-internal migration scaffolding, never plugin ABI, and must not gain SDK/Compat aliases.
Legacy public Agent imports remain exact SDK/Compat boundaries and must not be reintroduced as
Oper aliases in canonical Agent modules.
Monitor history checks use `get_transfer_history_repository()` from
`app/application/history.py`; old constructible Oper-style facades are available
only through the exact SDK Legacy/Compat mapping.

Durable transfer execution follows one explicit boundary. The Chain freezes each
external file operation into the Application-owned contract in
`app/application/transfer/execution.py`; `app/db/adapters/transfer/execution.py`
uses short transactions to persist the task ledger and fences every state change
with the current lease and attempt token. `app/db/oper/transferexecutionstep.py`
remains table-oriented and never owns retry or recovery policy. External file I/O
runs outside those transactions. A legacy or remote operation whose result cannot
be proven as applied or not applied enters `manual_review` and must not be replayed
automatically. Terminal history, pending state, execution-step cleanup and the
optional outbox intent are committed only by the task-aware implementation in
`app/db/adapters/chain.py`; canonical callers must not add a second settlement or
direct pending-deletion path. Task-aware settlement never performs synchronous
event publication inside the worker callback; the committed outbox owns delivery.
History mutation and maintenance paths may delete or replace only legacy rows with
no `transfer_task_id`, because durable receipts are recovery evidence rather than
ordinary user-maintained history.
Canonical Chain, API, Scheduler and Agent consumers read notification and media
server configuration through the named helpers in `app/application/notification.py`
and `app/application/mediaserver.py`. `ServiceConfigHelper` remains the parser at
the startup/runtime module boundary and a plugin SDK compatibility export; it is
not a second application-facing service directory.

### Adapter boundaries

| Path | Ownership |
|---|---|
| `app/adapters/cache/` | Redis and filesystem cache implementations and Redis clients |
| `app/adapters/network/` | Generic HTTP, browser, DNS, Cloudflare and IP transport mechanisms |
| `app/adapters/system/` | OS/filesystem/process facilities, stdio, display, packages, resources and optional Rust acceleration |
| `app/adapters/external/` | CookieCloud, plugin market, OCR, IP-location providers and MoviePilot Server |
| `app/adapters/web/` | FastAPI-specific technical adapters, including raw dynamic plugin routes |
| `app/adapters/observability/` | Optional telemetry exporters; core code depends only on runtime observation ports |
| `app/adapters/external/plugin/client.py` | Plugin-market transport, index/release queries and local-repository candidate resolution; `PluginHelper` is only the stable compatibility facade that delegates here |
| `app/adapters/system/plugin/` | Plugin package, dependency and health I/O (`package.py`, `dependency.py`, `health.py`); `PluginPackageManager` is the unique physical package mutation owner |
| `app/db/adapters/` | SQLAlchemy implementations of Application-owned persistence Protocols |

Generic protocol transport belongs in `adapters/network`; a named product or
ecosystem workflow belongs in `adapters/external`. An adapter may depend on
foundation, domain models, schemas and narrowly required runtime contracts, but
must not import application services, `runtime/extensions`, `runtime/compat` or
the plugin SDK.

RSS is not classified as a transport adapter merely because it uses HTTP. The
current `RssHelper` combines feed parsing, torrent item semantics, configured
site-specific URL discovery and browser fallback, so it belongs to
`app/application/rss.py`. The target design gives it ownership of the required
technical Ports, and startup injects the network/system Adapter implementations;
no concrete Adapter import remains in the Application module. Likewise, the generated
site extension owns the configured catalog/authentication/index capability and
lives in `app/application/site/`; only its download and file installation
mechanism remains in `app/adapters/system/resource.py`.

可选的进程级技术资源使用 Managed Resource 合同：实现及其 data-only
`capability.toml` 与适配器同目录，`runtime/extensions` 只解释通用的同步/异步
`start`、`stop` 生命周期，`startup` 负责构建 Capability Runtime。声明必须使用
`on_first_use`，普通启动只发现声明；消费者通过 `app/runtime/resources.py`
显式获取资源。关闭路径先释放消费者，再关闭已初始化 Runtime，未使用的资源不得因关闭而物化。
应用级启动顺序使用 `app/startup/lifecycle/components.py` 的组件描述声明依赖、
normal/safe-mode 范围、start/stop 顺序、超时预算和失败策略。新增进程级资源不得只在
`lifespan()` 中追加过程代码，必须先进入可导出的生命周期清单并补顺序快照测试。
Host Module 的 `stop()` 可以显式返回 `False` 表示资源 owner 尚未收敛；
`HostModuleAdapter` 必须将它视为 stop 失败，Capability Runtime 保留原 owner 供后续重试，
ModuleManager 与 startup 组合根继续关闭其余资源但必须向上返回未收敛，不得把记录日志等同于成功。
同步和异步 Capability Runtime 的 `shutdown` 必须使用同一布尔收敛合同；Agent、Managed Resource
等领域关闭入口必须直接传播 Runtime 的整体结果，不得以单个能力快照或无返回包装器覆盖失败。
消息渠道模块必须通过 `_MessageChannelModuleBase._stop_service_instances()` 聚合多实例关闭结果；
长连接、轮询或 Socket 服务只有在真实终止后才能返回成功，超时 owner 不得清空句柄。
应用消息队列的监控线程遵守同一收敛语义：停止必须有限等待，回调阻塞导致线程仍存活时保留 owner
并向 startup 返回 `False`，不得用无界 `join()` 阻塞生命周期或把日志当作成功。
共享 `ThreadHelper` 必须追踪通过宿主 `submit()` 和旧兼容 `.pool.submit()` 接受的全部 Future；关闭时
先封口新任务，再有限等待且保留未终止 owner，结果由 startup 聚合，不得恢复无界 executor shutdown。
`app.runtime.execution.OwnedThreadPoolExecutor` 是进程级同步执行器有界收敛的唯一事实源；新的专用
线程池不得复制 Future 追踪、worker join 或重试关闭实现。DoH 查询线程池也必须复用该 owner：恢复系统
DNS 后有限等待，超时保留原 executor 并向 startup 返回 `False`，真实收敛前不得创建替代线程池或回填缓存。
工作流节点线程池同样复用该 executor；所有 `WorkflowExecutor` 必须在 concrete `WorkflowManager` 登记，
manager 停机先封口新执行并向活动 owner 发送本地取消，再有限等待执行线程和节点 worker。未收敛时必须
保留动作注册表和执行 owner，并让工作流生命周期 fail-fast，禁止继续释放仍被动作使用的插件或模块依赖。
工作流读取统一使用 `app.application.workflow.WorkflowQueryService` 和冻结的 `WorkflowSnapshot`；
`app.db.adapters.workflow.TransactionalWorkflowQueryRepository` 必须在自有短 Session 内完成 ORM 投影与
嵌套 JSON 深拷贝。API、Agent、Chain、Scheduler、`WorkflowManager` 和中心服务分享不得读取 raw
`WorkflowOper` 或把 ORM 带出 Session。旧 `WorkFlowManager` 拼写只由 Compat 符号覆盖承接，不进入
canonical 模块定义或 `__all__`。
工作流执行状态写入统一依赖 `app.application.workflow.WorkflowExecutionPort`；Chain 在一次执行中只获取
一个事务端口，并由 `TransactionalWorkflowExecutionService` 为每次状态写入持有短 Session/UoW。
canonical `app.db.oper.workflow.WorkflowOper` 只提供显式 Session 的 query/stage 方法；旧无 Session
`start/success/fail/step/reset` 仅由 `app.sdk._legacy.workflow` 和精确 Compat 映射承接，且不进入
`app.db.oper.__all__`。
协程环境文件日志属于有界 E1 观测能力，只允许单一队列 writer；队列满时不得再以无界 executor
形成第二条异步写入路径。日志关闭必须有限等待 writer 与文件处理器，未收敛时 `LoggerManager`
保留原 owner 并让 lifespan 以关闭失败结束，不得先清空引用或用无界 `join()` 掩盖失败。
API 中允许丢失或可重建的进程内任务必须登记到 `app/runtime/tasks.py`；登记器先于其他
运行资源启动，并在资源释放前停止接收、取消和有限等待。需要崩溃恢复的 E2/E3 副作用仍应
进入 Outbox 或持久任务表，不能把 TaskRegistry 当成 durable queue。
Runtime 关闭后不可逆；完整应用生命周期的再次启动必须由新进程承载，不能在同一解释器中重建局部资源域。
插件需要浏览器时使用 `app.sdk.browser`，由宿主浏览器适配器协调资源，不直接依赖资源实现。
旧插件若直接导入有资源前置条件的第三方包，compat 在插件 import 前递归扫描源码并保守准备资源；
无法精确解析的文件按全部已登记资源降级，最终可导入性仍由 Python loader 判断。

`app/foundation/crypto.py` stays in foundation because it contains only generic
RSA, digest and CryptoJS-compatible AES primitives and has no settings, policy,
I/O or logging. Authentication, token, passkey, signing and two-factor policy
still belongs in `app/application/security/`; callers decide how cryptographic
failures are reported.

### Domain subdomains

`app/domain/` is a business package, not a synonym for every file whose name
mentions media, site or torrent:

| Subdomain | Modules and ownership |
|---|---|
| Media | `context.py` owns canonical `Context`, `MediaInfo` and `TorrentInfo` construction; `projection/` owns the pure, input-immutable TMDB/Douban/Bangumi/AniList field mappings and TMDB image-builder port; `media.py` owns source selection policy; identity representation/normalization stays in `schemas/media.py`; `title.py` owns title-candidate and search-keyword rules; `episode.py` owns episode-range display; `scraper.py` owns Kodi-style NFO reading and metadata document generation |
| Classification | `classification/fields.py` owns the standard field and operator catalog; `classification/sources.py` owns fixture-verified built-in source capability levels; `classification/facts.py` owns canonical fact construction and cross-source country/genre normalization; `classification/evaluator.py` owns deterministic condition and policy evaluation; `classification/validation.py` owns publish-time structural and semantic validation. The package consumes only normalized classification schemas and facts, never concrete media-source modules or persisted configuration |
| Recognition | `metainfo.py`, `meta/` and `tokens.py` parse names, paths, release groups, streaming platforms, anime, video and music metadata |
| Site | `site.py` owns site-domain exceptions and interprets HTML into business states such as logged-in and checked-in; configured catalog/auth/index resources stay in `app/application/site/`, generic URL/DOM parsing stays in foundation and network access stays in adapters |
| Torrent | `domain/torrent.py` owns magnet-link semantics; configured download/file behavior stays in `application/torrent/download.py`, while cache recognition stays in `application/torrent/cache.py` |

`app/domain` may depend only on schemas and foundation. It must not read global
settings, access DB/network/filesystem adapters, import Rust, discover services
or initialize process runtime state.

External media detail projection is one domain capability under
`app/domain/projection/`. Its package root contains documentation only;
`mapping.py`, `tmdb.py`, `douban.py`, `bangumi.py` and `anilist.py` are the only
owners. Projection accepts read-only mappings and returns field changes without
mutating either input. `MediaInfo` retains its canonical constructor, attributes
and four setter ABI as thin delegates; host code imports source owners rather
than calling its compatibility helpers. These internal owners do not create new
SDK or Compat paths.

Automatic media classification is a separate pure domain capability under
`app/domain/classification/`. Its package root contains documentation only;
callers import its focused child modules directly. `sources.py` declares only
fixture-verified built-in field coverage, while `facts.py` converts canonical
media objects into normalized `ClassificationFacts`. The evaluator therefore
never reads raw source payloads; plugin fact admission, policy persistence and
recognition lifecycle remain in their own Application, Runtime and Chain owners.

Configured media-classification use cases belong to the same-named
`app/application/classification/` package. Its package root contains
documentation only; `catalog.py` merges pure standard fields with the currently
registered source set, while undeclared dynamic-source capabilities remain
absent instead of being misreported as unavailable. `contract.py` owns the
atomic policy-state Port and stable conflict/corruption errors;
`configuration.py` owns default initialization, publish validation, monotonic
revision, bounded history, rollback and isolated process snapshots;
`legacy.py` owns the pure, deterministic conversion between the read-only
`category.yaml` contract and the versioned policy, including controlled TMDB
extension facts; `runtime.py` owns legacy API projection and CAS publication
without writing YAML; `execution.py` owns the input-immutable finalization of
complete recognition results, current-revision evaluation, explicit manual
overrides and invalid-policy legacy fallback; `analysis.py` owns the typed field catalog, explicit-fact
preview and bounded, read-only impact comparison over partial recent-history
facts. It must neither import concrete media-source modules nor write history,
subscriptions or files, and CPU-bound batch evaluation runs outside the API
event loop. The startup composition root injects the same execution port into
the host runtime and every Chain context; source modules only construct source
metadata and must not assign library categories. `app/api/endpoints/classification.py` owns the HTTP permission and
structured `409`/`422` response mapping only; it composes read ports without
moving classification semantics into the endpoint. `app/startup/composition/classification.py` is the only
owner allowed to decide whether the one-time YAML migration runs: an existing
`MediaClassificationPolicy` always wins, while invalid legacy input leaves the
new runtime unavailable with structured diagnostics instead of publishing a
partial policy. The
`app/db/adapters/classification.py` implementation stores `active + history` in
the single `SystemConfigKey.MediaClassificationPolicy` value, verifies revision
inside a short row-lock transaction and publishes the shared SystemConfig
snapshot only after commit. The same adapter also owns directory-configuration
mutation: policy and directory rows are locked in one fixed order, directory IDs
are resolved against the transaction-local active policy, and policy CAS repeats
directory-reference validation before commit. `DirectoryAwareSystemConfigService`
routes every `Directories` write through that shared mutation boundary, so API,
Agent, internal and offline configuration callers publish the exact normalized
snapshot that was committed. `systemconfig.key` is non-null and uniquely indexed,
so first-create races are rejected by the database as well as existing-row CAS.
Plugin declaration discovery and validation remain owned by the plugin runtime
and are supplied to this Application boundary rather than imported from the pure
Domain package. `app/runtime/extensions/plugin/classification.py` owns the enabled-plugin
source registry, namespace/type validation and fact filtering; lifecycle replacement and
removal use the runtime instance ID so virtual instances remain isolated. The Application
classification service reads immutable field and fact snapshots through composition-root
providers, while `app/sdk/classification.py` is the stable public capability/version surface.

`StringUtils` is not a canonical implementation type. Generic text, capacity,
time, URL, DOM, hash and version functions live under `app.foundation`; media
title, episode, site and torrent rules live in their owning domain modules. Host
code must import those implementations directly. `app.sdk.string.StringUtils`
only composes the complete historical static-method surface for plugins, and
both `app.utils.string` and the retired `app.domain.string` resolve to that same
SDK module through the compatibility manifest.

## Established Packages That Stay in Place

The following roots predate this migration and must not be moved or renamed as
part of migrated-capability cleanup:

- `app/agent/`
- `app/api/`
- `app/chain/`
- `app/db/`
- `app/doctor/`
- `app/modules/`
- `app/monitor/`
- `app/plugins/`
- `app/schemas/`
- `app/startup/`
- `app/testing/`
- `app/workflow/`

Necessary canonical import updates are allowed; changing their physical layout
or product responsibilities requires a separate architectural decision.

## Placement Decision Order

Use these questions in order before creating or moving a migrated capability:

1. Is it generic, stateless, independent of MoviePilot state and free of I/O?
   Put it in `app/foundation`.
2. Is it a pure MoviePilot business rule/model? Put it in `app/domain`.
3. Does it read persisted configuration or coordinate one focused configured
   capability? Put it in `app/application`.
4. Is it authentication, authorization, signing, SSRF, URL/path safety, OTP,
   passkey or two-factor policy? Put it in `app/application/security`.
5. Is it message rendering, routing or interaction behavior? Put it in
   `app/application/messaging`.
6. Is it process-wide configuration, events, logging, cache policy, execution,
   scheduling, concurrency, GC or restart state? Put it in `app/runtime`.
7. Does it discover/manage modules, plugins or configured service providers?
   Put it in `app/runtime/extensions`.
8. Does it perform concrete cache, network, OS/process, filesystem, stdio,
   package/resource or Rust I/O? Put it under the matching `app/adapters`
   technical boundary.
9. Does it implement a named external product/ecosystem? Put it in
   `app/adapters/external`.
10. Is it public to plugins or only preserving an old path? Curate it in
    `app/sdk` or map it in `app/runtime/compat`; never move implementation there.

Do not create generic `common`, `helper` or `utils` buckets. Reuse does not erase
ownership.

New production Python module filenames use one lowercase word. When one topic
needs multiple modules, create a topic package and keep each child filename to
one word, for example `runtime/event/{registry,binding,dispatch,errors}.py` or
`application/subscription/{contract,delete,identity}.py`. Multiword production
paths may remain only when the machine policy verifies a stable discovery
identifier or records a path-specific reason and consumer evidence; an
unstructured grandfathered filename list is forbidden. The same gate scans
every package root: new roots are documentation-only or precise lazy facades
unless a reviewed owner reason is recorded. Test filenames continue to follow
pytest's descriptive `test_<behavior>.py` convention.

Legacy module paths belong in `app/runtime/compat/manifest.py`. New
implementation modules must not re-export old managers, helpers or Oper classes
just to preserve imports or tests. A public runtime object whose path or identity
is itself part of the plugin ABI stays at its established path as a thin facade;
new plugin-facing symbols are exported deliberately through `app/sdk` and its
architecture snapshot, not through incidental module globals.

## Existing Chain, Module and DB Layers

### Chain layer

运行时停止信号统一由 `app/runtime/stop.py` 的 `StopState` 持有。业务代码应注入或读取
`runtime_stop_state`，`app/runtime/config.py` 中的 `global_vars` 停止属性只作为旧插件和
兼容测试的门面，不得新增依赖。Chain mixin 通过 `app/chain/_contracts.py` 声明最小宿主
能力，并优先使用宿主提供的可替换工厂；具体 mixin 的反向导入按批次收敛。

`app/chain/` implements use cases shared by API, CLI, Agent, scheduler and other
entrypoints. Chains may coordinate modules, application services, injected
persistence Ports, events and caches. New chain-to-chain dependencies are allowed only while the
static graph remains acyclic. Backend protocol details and HTTP request objects
do not belong here. Chains interact with modules exclusively through
`run_module` dispatch on method-name contracts; direct imports of module
internals (classes, exceptions, constants) are forbidden, so every module stays
pluggable and a chain never names a concrete module implementation.
The dispatch algorithm belongs to
`app/runtime/extensions/module/dispatcher.py`; `ChainBase` remains the
compatibility facade. New chains and tests inject the minimal
`ChainRuntimeContext` from `app/application/chain/context.py`. No-argument
`Chain()` remains supported through the startup-configured compatibility
provider. High-frequency string methods are classified in
`module/contracts.py`; unknown third-party plugin methods retain the frozen
legacy aggregation contract, while the architecture baseline records every
literal method and call site.

Underscore-prefixed files in `app/chain/` are feature-domain mixins for
`ChainBase` and concrete chains, not chains themselves: `_recognition.py`
(`RecognitionMixin`), `_messaging.py` (`MessageProcessingMixin` /
`NotificationMixin`), `_interaction.py` (`InteractionChainMixin`, the shared
slash-command delegation for `remote_list` / `parse_callback` /
`handle_callback_interaction` / `handle_text_interaction`) and `_music.py`
(`MusicSubscribeMixin`, the music single/album subscribe domain mixed into
`SubscribeChain`).

Transfer orchestration is a same-named package. Its root exposes only the stable
`TransferChain` and the plugin-used `task_lock` compatibility identity; `facade.py`
composes the owner classes, while queue/recovery, planning, execution, settlement,
history/notification and request construction stay in focused single-word child
modules. The retired `app/chain/transfer.py` and `_transfer.py` monoliths must not
return. Internal owner classes, `JobManager` and the durable runner are imported
from their canonical child owners instead of being re-exported by the package root.

Shared subscription metadata and media-key construction belongs to
`app.application.subscription.contract`; `app.chain.subscribe` is a same-named package
whose root exposes only the stable `SubscribeChain`. `facade.py` owns the class
identity and event entry wrappers; `create.py`, `search.py`, `match.py`, `refresh.py`,
`completion.py`, `reconcile.py` and `notify.py` are the single owners of their
respective workflows, while `query.py`, `interaction.py`, `identity.py` and `policy.py`
own the remaining focused capabilities. Startup imports the subscription share Port
directly from `notify.py`; canonical callers import identity projection directly from
`identity.py`. The retired `app/chain/subscribe.py` monolith must not return, package
owners must not be re-exported, and `_music` must not import its concrete chain owner.
A concrete chain that exposes slash-command interaction inherits
`InteractionChainMixin`, injects its handler class via `_interaction_handler_type` and
implements only `_interaction_handler`; it must not re-export application-layer
interaction managers.

Download orchestration is owned by the same-named `app.chain.download` package.
Its root lazily exposes only the stable `DownloadChain`; `facade.py` composes the
owner classes and keeps the `DownloadFileDeleted` event wrapper on that stable
class identity. Selection, submission, batch execution, existence checks, failure
cooldown, history settlement, post-processing, subtitle handling, task control and
technical ports each have one focused single-word child module. Startup imports
the download ports directly from `ports.py`; canonical callers must not obtain
owners or ports from the package root. The retired `app/chain/download.py`
monolith must not return, and no `source.py` copy or duplicate implementation may
remain. Download history, file rows and the durable Outbox intent commit in one
transaction; notifications, background post-processing and immediate event
publication run only after that commit succeeds.

Search orchestration is owned by the same-named `app.chain.search` package. Its
root lazily exposes only the stable `SearchChain`; `facade.py` preserves the
direct `SearchChain -> ChainBase` MRO, event identity and the three exact private
provider hooks used by official plugins. Search planning, provider fan-out,
pagination, result projection, result-state adaptation, title/media/music/subtitle
workflows and AI recommendation coordination each have one focused single-word
owner. Provider list and stream APIs consume the same batch/event facts, pending
page tasks belong to the runtime task registry, and progress always converges in
`finally`. Canonical callers must not import owner classes through the package root.
The retired `app/chain/search.py` monolith, internal root re-exports and `source.py`
copies must not return. Search state normalization and persistence remain owned by
`app.application.search.state`; the Chain package only adapts its cache ports.

Media recognition orchestration is owned by the same-named `app.chain.media`
package. Its root lazily exposes only the stable `MediaChain`; `facade.py` preserves
the direct `MediaChain -> ChainBase` MRO, Singleton class identity and official
plugin method signatures. Recognition routing, plugin-event fallback, auxiliary
source aggregation, identity projection, search, music catalog, audio-path evidence,
album alignment and directory caching each have one focused single-word owner. The
directory cache is bounded, isolates cached values from caller mutation, fingerprints
file content metadata, preserves symbolic-link directory aliases and coalesces concurrent
misses without letting a cancelled follower cancel the shared flight. Music catalog
routing consumes the public `MusicMetadataSourceChain` contract instead of importing a
private class from another Chain owner. Canonical callers must not
import owner classes through the package root. The retired `app/chain/media.py`
monolith, internal root re-exports and duplicate compatibility implementations must
not return; historical scraping symbols continue only through the exact Compat
overlay, while `MediaChain` remains the single canonical plugin facade.

### Module layer

`app/modules/` contains pluggable downloaders, media servers, metadata sources,
message channels, indexers and storage providers. New direct module-to-module or
module-to-chain dependencies are forbidden; cross-module orchestration belongs
in a chain. Module internals stay sealed inside the module: shared constants,
exceptions and value domains used by both modules and upper layers live in
`schemas`, and module capabilities are exposed to chains only as dispatched
method names. The directory remains unchanged because discovery and plugin code
depend on this established runtime root.

`app.modules.filemanager` is a lazy compatibility entrypoint. The concrete
`FileManagerModule` implementation lives in `app.modules.filemanager.module`,
while the historical capability path and class module identity remain
`app.modules.filemanager:FileManagerModule`. Storage and transfer-handler
submodules must not import the concrete module implementation through the
package root.

`app/modules/_base/` hosts the shared template base classes for module families
(`downloader.py`, `mediaserver.py`, `notification.py`), each combining the
family mixin with `_ModuleBase` and typed by `TService` (usage:
`class QbittorrentModule(_DownloaderModuleBase[Qbittorrent])`). The base classes
carry only verbatim-duplicated boilerplate — connection test, scheduled
reconnect, torrent-info reading, query-status normalization for downloaders;
authentication, media-exists check, inactive-server handling for media servers;
admin resolution and command registration for message channels — while
subclasses keep the differentiated API calls and override small hooks such as
`_test_connection`, `_test_server` and `_is_inactive`. Discovery already skips
the package (module discovery only enumerates first-level submodules and skips
underscore-prefixed names), so no new exclusion rules are needed; do not grow
this package with per-module business logic.

Channels and storages that need login management or temporary-parameter
initialization follow one generic contract instead of per-target APIs: modules
implement `channel_manage(channel, action, **params)` or
`storage_manage(storage, action, **params)`, route by the requested target
identifier (returning `None` for other targets, accepting both enum members
and plain strings), and interpret actions from the shared
`schemas.types.NotificationAction` / `StorageAction` vocabulary plus opaque
form parameters themselves. All results use the unified
`{"success": bool, "message": ..., "data": ...}` shape.
`NotificationChain.manage_channel` and `StorageChain.manage_storage` forward
transparently and must stay free of any channel/storage-specific names or
logic; new channels or storages adopt the same contract without touching the
chains. The endpoint layer exposes this as two generic endpoints
(`POST /api/v1/notification/manage`, `POST /api/v1/storage/manage`) taking the
common `schemas.ManageRequest` body (`target` + `action` + `params`) and must
never define target-specific names, parameters or response fields — the
frontend supplies them and the endpoint passes them through untouched.

LLM providers follow the same contract: `LLMProviderManager.provider_manage`
dispatches actions from the shared `schemas.types.LlmProviderAction`
vocabulary, seals default-value filling, key sanitization and error rewriting
inside, and the endpoint layer exposes a single `POST /api/v1/llm/manage` with
the same `ManageRequest` body. The only exception is the named OAuth callback
route (`GET /api/v1/llm/provider-auth/callback/{provider_id}`), which stays
named because external browsers redirect to that URL; the endpoint builds the
callback URL from that route name and injects it as an action parameter.
`app/agent/llm/provider.py` is the stable Provider facade only. Catalog and model
metadata ownership lives in `catalog.py`; remote directory and SDK discovery I/O
lives in `discovery.py`; persistent authorization and external OAuth protocols live
in `auth.py`; temporary authorization state lives in `session.py`; unified client
parameters live in `runtime.py`. The facade preserves the historical public method
signatures while private behavior resolves directly to those owners. `LLMHelper`
consumes the registered runtime and must not recreate provider discovery or legacy
runtime fallbacks; management tests pass the facade directly to avoid a
`provider_manage -> helper -> gateway -> provider_manage` call loop.

`app.agent` and `app.agent.llm` package roots contain no implementation,
dynamic forwarding or host export list. Canonical callers import the owning
module directly, such as `app.agent.orchestrator`, `app.agent.llm.helper`,
`app.agent.llm.capability`, `app.agent.llm.auth` or `app.agent.llm.provider`. Historical Agent package
symbols and the verified `app.agent.llm.LLMHelper` plugin path are preserved only
by exact `SYMBOL_ALIASES`; `app.helper.llm` is an exact module alias to
`app.agent.llm.helper`, not an alias to the whole LLM package. Therefore
`app.agent.llm.LLMHelper`, `app.agent.llm.helper.LLMHelper` and
`app.helper.llm.LLMHelper` resolve to the same class without exposing provider,
auth or capability owners through either package root. The verified tool ABI
continues at `app.agent.tools.base.MoviePilotTool` and
`app.agent.tools.manager.moviepilot_tool_manager`, including the existing
`_load_tools()` compatibility call; those canonical owner modules are not
re-exported from `app.agent`.

### DB / Oper layer

SQLAlchemy models stay under `app/db/models/`; the data access classes live in
`app/db/oper/` and mirror them one-for-one (`models/subscribe.py` ↔
`oper/subscribe.py`), so a filename carries only the entity and the package name
carries the role. Two verified aggregation exceptions exist: the site family
(`Passkey`, `SiteIcon`, `SiteStatistic`, `SiteUserData`) is consolidated in
`oper/site.py`, and `AgentTaskRun` lives in `oper/agenttask.py`. DB adapters use
Oper classes for ordinary entity access. Adapter-owned cross-row locks and
compare-and-set transitions may issue focused SQLAlchemy statements when the
atomic persistence invariant cannot be expressed by an entity Oper; those
statements stay private to the adapter and require concurrency tests.

Site persistence crosses the DB boundary only through the contracts in
`app/application/site/contract.py`. `SessionSiteRepository` reuses a request
`AsyncSession` and only projects or stages changes, while
`TransactionalSiteRepository` owns one short Session/UoW per standalone
operation. Both adapters project `Site`, `SiteIcon`, `SiteStatistic`, and
`SiteUserData` to deeply frozen snapshots before the Session closes. Canonical
Application, Chain, Agent, API, and Startup code must not import `SiteOper` or
these ORM models. Historical plugin imports resolve through the exact SDK
Legacy/Compat manifest only.

Subscription persistence follows the same verified typed boundary.
`app/application/subscription/contract.py` is the single owner of the deeply
frozen subscription/history snapshots, media identity, write patch and shared
query/write/staging repository protocols. `TransactionalSubscriptionRepository`
owns a short Session/UoW for standalone Chain and Agent operations, while
`SessionSubscriptionRepository` and `SessionSubscriptionHistoryRepository`
reuse the request or Application-command Session and never commit it. All ORM
projection completes before that Session closes. Canonical Application, Chain,
Agent, API, Workflow and Startup code must not import the subscription Oper or
ORM models. The historical module paths and package-root Oper symbols resolve
only through `app/sdk/_legacy/subscribe.py` plus the exact Compat manifest and
are not added to canonical package `__all__` exports.

Application and Chain code reaches persistence through named Ports/Protocols;
concrete DB adapters are the only layer that adapts those Ports to Oper/Session
mechanics. Every schema change requires an Alembic migration under
`database/versions/`.

Oper classes take and return persistence values, not domain objects. Translating
`MediaInfo` / `MetaBase` into a row is business logic and belongs in
`app/application/` — see `application/subscription/write.py` and `application/history.py`
for the two write paths. Column-type coercion (numeric year to string, boolean
switches to integers) stays in the Oper because it follows the column, not the
caller.

Invariants that must hold for *every* write are enforced at the mapper rather
than at each call site: `app/db/models/_identity.py` normalizes
`media_source` / `media_id` on `before_insert` / `before_update`, so a new write
path cannot forget them. Identity representation rules themselves
(alias folding, trimming, rejecting zero) live in `app/schemas/media.py`
alongside the two identity mixins; `app/domain/media.py` keeps only source
policy. `app/db` therefore has no dependency on `app/domain`.

User identity is a single aggregate boundary. `app/application/security/user.py`
owns frozen user/auth snapshots and the atomic create/update/delete command;
`app/db/adapters/user.py` binds each mutation to one request UoW and locks the
active-superuser set before a destructive change. The database enforces unique
user names and cascades rename/delete to `UserConfig` and delete to `PassKey`.
The configured user-configuration repository publishes its in-memory snapshot
only after the user transaction commits, and reloads from the database if
publication fails.

History is a verified typed boundary: `app/application/history.py` owns deeply
frozen DownloadHistory and TransferHistory snapshots plus typed query/write and
staging ports. `app/db/adapters/history/download.py` and
`app/db/adapters/history/transfer.py` perform ORM projection and mutations inside
short Session scopes; durable transfer settlement uses the staging port in its
caller-owned transaction. Canonical callers never receive history ORM rows or
raw Oper objects, while old plugin imports resolve only through SDK Legacy and
the exact Compat manifest.

Durable post-commit side effects have a separate boundary:

- `app/application/outbox.py` separates `OutboxStager`, which only stages in the
  caller's business transaction, from `OutboxDispatchStore`, whose claim,
  complete and retry operations own independent short transactions.
- `app/db/adapters/outbox.py` implements both roles with SQLAlchemy;
  `app/startup/composition/subscription.py` injects transaction-scoped stagers and
  stores, while `app/startup/composition/outbox.py` owns the validated replay handlers
  and dispatcher factory.
- Immediate request-thread delivery and the dispatcher both claim before
  calling a handler. Lease acquisition is atomic, while complete/retry is fenced
  by the claimed attempt so an expired owner cannot settle a newer claim.
- `PostCommitResult` distinguishes the committed business value from completed
  and pending effects. This does not provide exactly-once delivery: a process may
  stop after an external sink succeeds but before complete is persisted. Outbox
  delivery is therefore at-least-once. Event payloads and the host correlation
  context carry the stable event key, and consumers that support deduplication
  should use it. Legacy notification plugins retain their existing method
  signature, so the host must not claim provider-level exactly-once delivery.
- Terminal history is part of the shared data-maintenance policy and is cleaned
  in bounded daily batches only when that policy is enabled. Completed intents
  default to 30-day retention and dead letters to 90 days; both values are
  user-configurable and `0` disables that status cleanup. Pending or processing
  intents must never be removed by retention cleanup.
- `app/runtime/tasks.py` is only the in-process TaskRegistry boundary. It owns
  cancellation and bounded shutdown waiting, but it is not a durable queue and
  must not replace an Outbox or persistent task table.

Transfer durable admission follows the same ownership direction without using
the Outbox as an execution queue: `app/application/transfer/workflow.py` owns the typed
admission and versioned planning-checkpoint contracts, while
`app/db/adapters/transfer/admission.py` commits admission and the
`accepted -> provider_pending -> planned` compare-and-set transitions in short
Session/UoW scopes. `app/modules/filemanager/` owns the
single pure-plan and checkpoint-execution implementation: all file writes occur
after checkpoint commit, and planned recovery consumes frozen resolved context,
target storage and ordered operations without online recognition or renaming.
Legacy plugin `transfer` providers are frozen by exact identity, order, and ABI
arguments in a provider-only checkpoint, then executed by the unified module
dispatcher only after commit. The dispatcher resolves every frozen reference
before the compatibility cleanup hook and propagates provider failures. Missing
or failing providers therefore remain `provider_pending`; only an all-empty
result permits host planning and a second CAS to `planned`. Host-only `plan_transfer` and
`execute_transfer_plan` contracts never dispatch to plugins. `ChainBase.transfer`
is the sole legacy caller facade and delegates the startup-injected durable
command; `FileManagerModule.transfer` and `TransHandler.transfer_media` must not
be recreated.

Transfer execution ownership is orthogonal to those planning phases.
`app/application/transfer/workflow.py` defines the claim, heartbeat, release and fenced
mutation Port; `app/db/adapters/transfer/admission.py` implements each operation in a
short UoW with a unique lease token. Any active lease rejects another claim,
including one from the same process owner. Expired leases may be taken over with
a new token and incremented attempt count, while the stale token cannot renew,
checkpoint, record failure, release or delete the task. Startup replay and
same-process recovery use the single scheduler owned by `TransferChain`; the
scheduler claims before enqueueing, and queued or executing claims are renewed
by one lifecycle-managed heartbeat owner. Lease ownership guarantees one
database-authorized worker, not physical exactly-once behavior for an already
issued file or legacy-plugin side effect; step idempotency and unknown outcomes
remain explicit execution concerns.

Canonical host chains never obtain `TransferPendingOper`. The canonical Model,
Oper and Application Port do not retain the historical `register`, `list_all`,
`discard`, `clear` or `list_by_*` surface. The exact
`app.db.transferpending_oper` mapping resolves instead to the private
`app/sdk/_legacy/transferpending.py` facade, which preserves the old no-Session
query ABI without becoming a host implementation. Its `register` delegates to
canonical durable admission without overwriting an existing row, while
`discard` and `clear` delete only rows whose `lease_token` is null; an active or
expired claimed task remains exclusively owned by fenced recovery APIs.

## Composition and Compatibility Boundaries

- Startup registers concrete cache factories before decorated business modules
  are imported. Cache contracts remain in `app/runtime/cache.py`; Redis/file
  implementations remain in `app/adapters/cache/backends.py`.
- Security-sensitive one-shot state uses the strict `AtomicCacheBackend`
  `store/consume` contract. Memory and Redis implement atomic consume; startup
  injects that capability through `PasskeyChallengeCache`, so Passkey
  Application code never identifies or imports the Redis implementation.
- `app/runtime/log.py` is a dependency leaf with no `app.*` imports. Foundation
  emits no runtime logs; upper-layer owners decide whether failures are
  operationally relevant.
- `app/adapters/system/resource.py` only reports whether installation occurred;
  `app/startup/initializers/modules.py` supplies the loaded site-resource
  versions and decides whether to restart. The adapter never imports the site
  application service.
- Configured notification discovery lives in
  `app/application/notification.py`. Web Push subscription and manual-send HTTP
  behavior stays in `app/api/endpoints/message.py`.
- System network testing lives in `app/application/network.py`: it owns the
  server-only target catalog, exact legacy URL matching, HTTPS and credential
  checks, per-target redirect allowlists and content validation. The System API
  only authenticates, maps request fields and projects the standard response;
  startup injects the concrete HTTP transport with certificate verification and
  automatic redirects disabled.
- System management use cases live in `app/application/system.py`: log discovery,
  reading and polling, Wiki market synchronization and merge, runtime/system
  setting mutation, configuration events, release queries, and update/restart
  orchestration all depend on explicit Ports. `app/startup/composition/system.py`
  injects the concrete HTTP, filesystem, MoviePilot Server, Rust and process-update
  adapters into `HostRuntime.system`. The System endpoint retains authentication,
  HTTP error/DTO projection, SSE framing and ZIP framing only; it must not import
  those concrete adapters or the event bus directly.
- `app/runtime/compat` stores string mappings and resolves aliases lazily. It may
  not eagerly import canonical MoviePilot modules.
- 已删除的 `app.db.<entity>_oper` 路径继续由精确模块映射提供给旧插件；其中订阅写入、
  整理历史写入和拆分后的用户认证依赖通过 `app.sdk._legacy` 薄门面委托 canonical
  Application/Oper，不把领域对象或 HTTP 依赖重新引回 DB 层。
- 物理模块仍存在但公开符号已经迁走时（例如 `app.domain.media` 的身份原语、
  `app.schemas` 的整理工作项），兼容 Finder 在标准 Loader 执行后叠加白名单符号路由；
  canonical 模块不得为兼容而反向 import `app.runtime.compat`。
- 符号级插件 ABI 只保证显式导入和属性访问；兼容符号不加入物理包的 `__all__`，
  不支持依赖 `from ... import *` 获得迁移符号。宿主源码不得消费 `SYMBOL_ALIASES`
  中的旧符号，必须直接导入 canonical owner，避免包根形成第二份宿主导出面。
- Canonical implementation packages may not import `app/runtime/compat` or
  `app/sdk`.
- Host code uses canonical paths. Only `app/plugins/` and compatibility tests
  may use `app.core`, `app.helper`, `app.utils` or `app.log`.
- New plugins use `app.sdk`. In DEBUG mode, a legacy plugin import remains
  functional and emits one actionable warning per plugin and legacy module.
- Plugin compatibility changes belong only in curated SDK/Legacy exports or the
  exact Compat manifest. `app/plugins/**` contains runtime plugin copies and is
  excluded from host refactors, dependency baselines and ownership migrations.
- Delayed imports are not accepted as a way to hide dependency cycles.

### Dependency facts and semantic policy

`tests/fixtures/architecture/dependency-baseline.json` is generated evidence: it
records the complete host module graph, edges and SCCs while excluding
`app/plugins/**`. It does not approve those facts. Human-reviewed classifications
live separately in `tests/fixtures/architecture/dependency-policy.json`, and
`scripts/architecture/baseline.py --write-host` must never create or update that
policy.

The semantic architecture test compares every SCC in the complete host graph with
the exact member sets in policy. A new SCC, member expansion, changed member set,
or stale policy entry fails. The current policy has one classification:

- `contained_vendor`: the exact 29-module TMDB vendored SCC. It may have ordinary
  one-way dependencies outside the package, but no outside module may join the SCC
  and its member set may not grow.

The target remains zero canonical host cycles except the precisely contained
vendor component. `ChainBase` lives in `app.chain.base`; the physical package root
has no eager export, and the old package-root symbol is available only through the
exact Compat overlay backed by `app.sdk.chain`.

The same fact/policy split governs direct Adapter imports. The generated
dependency baseline records the original runtime imports from `app.application`
and `app.chain` without parent-package expansion. Every current edge is an exact
`temporary_debt` entry in dependency policy with a removal leaf; the target state
is empty. New or replacement edges and stale policy entries fail. Application owns
the Port required by its use case, startup injects the concrete Adapter, and Chain
consumes the Application capability or an injected Port. A `canonical capability`
never means permission to import a concrete `app.adapters.*` implementation.

Direct egress is a separate boundary from Adapter imports. The generated
`direct_egress` facts scan the complete host `app` tree except `app.plugins` and
record raw transports, registered network SDKs and exact protocol operations.
Each identity contains import provenance plus stable callable/operation uses and
has no line number. The manual policy classifies every full fingerprint as either
`temporary_debt` with a removal leaf and empty target state, or an
`approved_exception` with an exact owner and reason. Canonical transports, SDKs,
streaming protocols, contained vendor code, diagnostics and control planes are
contained exceptions, not category-wide permissions. In policy, `owner: "$source"`
means the fact's exact `source` module is the owner; it does not authorize sibling
or child modules. Runtime wildcard imports from a registered egress root are
forbidden. Updating the generated baseline never updates this policy; additions,
fact changes, classification swaps and stale entries fail independently. Current
debt may shrink without changing a fixed count, but no initial edge may grow or be
reclassified. Tests independently freeze every initial edge fingerprint, so
refreshing both generated facts and manual policy cannot hide growth on the same
`source/target`; when debt is removed, its frozen edge and fingerprint must be
removed in the same reviewed change so that it cannot return.

## Permitted Call Directions

| Direction | Status |
|---|---|
| `entrypoint -> chain / application / injected persistence Port` | Allowed according to workflow complexity |
| `chain -> module (only via run_module dispatch) / application / injected Port / canonical capability` | Allowed; direct `chain -> module`, `chain -> Oper` and `chain -> concrete adapter` imports forbidden |
| `chain -> agent implementation` | Forbidden; chains reach Agent runtime only through `app/application/agent.py`; `app/startup/initializers/agent.py` registers lightweight providers during the explicit lifecycle stage and resets them on shutdown, while implementations are materialized only when the capability is enabled or first used |
| `agent.tools -> api / scheduler / command` | Forbidden; tools use `app/application/plugin/routes.py`, `plugin/folders.py`, `scheduling.py` and `commands.py` application services |
| `api -> factory` | Forbidden; the FastAPI route adapter is injected into `app/application/plugin/routes.py` by the composition root after creation |
| `api / chain -> app.workflow` | Forbidden; workflow consumers use `app/application/workflow.py`, while only `app/workflow/**` and `app/startup/initializers/workflow.py` access the concrete runtime |
| `application -> domain / runtime contract` | Allowed |
| `application -> DB / Oper / concrete adapter` | Forbidden; define a Protocol in Application and inject an implementation |
| `db.adapters -> application persistence Protocol / db.oper / UoW` | Allowed; this is dependency inversion, not an upper-layer use-case call |
| `module -> canonical capability / Application persistence Port` | Allowed; direct Oper imports are forbidden for new code |
| `module -> module / chain` | Forbidden for new code |
| `adapter -> application / runtime.extensions / sdk / compat` | Forbidden |
| `domain -> runtime / adapter / application / DB` | Forbidden |
| `foundation -> other app packages` | Forbidden |
| `canonical implementation -> sdk / compat` | Forbidden |
| `compat -> canonical implementation at module import time` | Forbidden |
| Any import that creates a module-level cycle | Forbidden; the complete host graph must match the exact reviewed SCC policy, and temporary debt must have a removal owner |

Event producer and consumer facts share `scripts/architecture/event_facts.py` as
their only collector. A send/register method name alone is not evidence: the
receiver must resolve to the canonical `app.runtime.events.eventmanager`, an
`EventManager` instance, or a proven injected Event publisher/manager port.
Unknown same-name receivers are ignored. Positional and keyword event arguments,
finite aliases and conditional enum choices must be resolved; only a proven
receiver whose event value remains unknowable may produce a dynamic fact. The
collector respects lexical shadowing and rebinds, distinguishes decorator
application from obtaining a decorator factory, and never scans `app/plugins/**`
as host code.

The generated runtime baseline preserves every line-free call fact and its
multiplicity. Consumer admission is separate and non-generated: every current
consumer fingerprint, owner, classification and concrete reason must exactly
match `runtime-contract-policy.json`. New, changed, duplicate, invalid and stale
consumer identities fail independently, and `--write-host` never modifies the
policy. The current host permits one dynamic consumer only: the configuration-
driven workflow registration.

## Key File Locations

| Path | Purpose |
|---|---|
| `app/application/agent.py` | Agent orchestration facade plus typed `AgentDataContext`; lightweight service providers register through `app/startup/initializers/agent.py`, with no static `application -> agent` edge or global persistence locator |
| `app/application/network.py` | Server-owned network-test catalog, public/private DTOs, URL and redirect admission, content validation and the injected HTTPS transport Port |
| `app/db/adapters/agent.py` | Agent task and plugin-data persistence implementations; ORM values are projected to Application snapshots before Session close |
| `app/agent/manager.py` | Stable `AgentManager` construction facade and singleton identity |
| `app/agent/session.py` | Session queue/worker/status/cancellation and deferred-cleanup owner |
| `app/agent/lifecycle.py` | Agent manager admission, startup, idle collection and bounded shutdown owner |
| `app/agent/tasks.py` | Background prompt, scheduled task and heartbeat execution owner |
| `app/agent/orchestrator.py` | Per-session `MoviePilotAgent` execution and LLM/tool/middleware orchestration only |
| `app/agent/shell.py` | Agent command-shell selection and subprocess UTF-8 policy, with Windows-only Git Bash/PowerShell 7 routing |
| `app/agent/loader.py` | Agent-specific capability discovery and canonical entrypoint/service materialization; reuses the generic Capability Runtime while keeping Agent ownership under `app/agent/` |
| `app/agent/__init__.py` | Implementation-free package root; exact historical Agent symbols are supplied by the Compat overlay only, while host callers import `orchestrator.py` or the relevant owner directly |
| `app/agent/llm/__init__.py` | Implementation-free package root; only the verified historical `LLMHelper` symbol is supplied by exact Compat routing |
| `app/agent/llm/helper.py` | Canonical `LLMHelper` owner and exact target of the historical `app.helper.llm` module path |
| `app/agent/llm/auth.py` | OAuth callback result presentation; escapes provider-controlled text before rendering HTML and is not re-exported from the package root |
| `app/application/subscription/contract.py` | Deeply frozen Subscription/History DTOs, media identity, write patch and typed query/write/staging Repository contracts |
| `app/application/subscription/write.py` | Subscription media translation and sync/async write-command orchestration |
| `app/db/adapters/subscription.py` | Standalone short-transaction and caller-Session subscription/history adapters; ORM projection remains inside the Session |
| `app/application/download/failures.py` | Frozen download-failure cooldown write/query DTOs and Chain persistence Port |
| `app/db/adapters/download.py` | Short-session download-failure snapshot and mutation adapter |
| `app/db/adapters/mediaserver.py` | Per-operation media-server cache query/upsert/cleanup transaction adapter |
| `app/application/history.py` | History use cases; deeply frozen DownloadHistory/TransferHistory DTOs and typed query/write/staging ports |
| `app/db/adapters/history/download.py` | DownloadHistory short-session snapshot, query and mutation adapter |
| `app/chain/download/` | Stable DownloadChain facade plus single-owner selection, submission, batch, existence, failure, history, post-processing, subtitle, task and technical-port modules |
| `app/chain/search/` | Stable SearchChain facade plus single-owner plan, provider, pagination, result, cache, title, media, music, subtitle, site and recommendation modules |
| `app/chain/media/` | Stable MediaChain facade plus single-owner recognition, plugin, auxiliary, projection, search, catalog, path, album and bounded cache modules |
| `app/db/adapters/history/transfer.py` | TransferHistory short-session snapshot/query/mutation adapter and caller-owned transaction stager |
| `app/application/security/user.py` | Frozen user/auth projections and atomic user aggregate service contracts |
| `app/db/adapters/user.py` | User projection plus request-UoW mutation adapter |
| `app/db/adapters/configuration.py` | Commit-after UserConfig snapshot publication and fact-source reload adapter |
| `app/application/outbox.py` | Durable intent, stager/store, claim fencing, topic handler and structured post-commit contracts |
| `app/db/adapters/outbox.py` | SQLAlchemy Outbox transaction-only stagers and short-transaction claim/settlement stores |
| `app/application/chain/events.py` | Chain durable-event write port, settlement projection and replayable payload conversion |
| `app/application/transfer/workflow.py` | Transfer task, durable admission, versioned planning input/checkpoint contracts and queue use case |
| `app/db/adapters/transfer/admission.py` | SQLAlchemy admission/checkpoint persistence, CAS state transition and detached snapshot adapter |
| `app/application/scheduling.py` | Runtime scheduler facade for Agent tools and endpoints; `Scheduler` class registered by `app/startup/initializers/scheduler.py` |
| `app/scheduler/` | Scheduler implementation package: stable facade plus catalog, execution/bridge/progress, registry, reconcile, lifecycle and maintenance owners; no business Chain construction |
| `app/startup/initializers/scheduler.py` | Registers the concrete Scheduler and injects the AgentTask repository plus frozen callable-only `SchedulerServices` before lifecycle start |
| `app/sdk/scheduler.py` | Narrow scheduling facade for new plugins; does not expose concrete Scheduler owners or lifecycle state |
| `app/application/commands.py` | Command registry facade for Agent tools and endpoints; `Command` class registered by `app/startup/initializers/command.py` |
| `app/application/workflow.py` | Workflow use cases, frozen query snapshot and typed runtime ports consumed by API, Agent, Chain and Scheduler; `WorkflowManager` is registered by `app/startup/initializers/workflow.py` |
| `app/db/adapters/workflow.py` | Short-session Workflow query projection and execution-state transaction adapters |
| `app/db/adapters/` | SQLAlchemy repository/UoW implementations for Application-owned persistence Protocols |
| `app/startup/composition/` | HostRuntime, configuration snapshots and cross-layer adapter wiring |
| `app/startup/initializers/` | Domain-scoped initialization and shutdown hooks |
| `app/chain/agent.py` | `AgentChain(ChainBase)`: the chain-layer entry for Agent sessions; Agent runtime stays in `app/agent/` |
| `app/runtime/config.py` | `ConfigModel`, `Settings` and deployment configuration |
| `app/runtime/cache.py` | Cache contracts and memory policy, including strict atomic store/consume for one-shot security state |
| `app/application/security/passkey.py` | Injected Passkey challenge cache port and one-shot challenge issue/consume policy |
| `app/runtime/tasks.py` | TaskRegistry owner, cancellation and bounded shutdown waiting |
| `app/runtime/execution.py` | Shared execution/thread-boundary helpers and context propagation |
| `app/runtime/correlation.py` | Correlation ID context and propagation boundary |
| `app/runtime/topology.py` | Single-worker full-runtime policy and safe-mode topology validation |
| `app/runtime/events.py` | `EventManager`/`Event` compatibility facade and global `eventmanager` identity |
| `app/runtime/event/registry.py` | Event subscriptions, enable/disable state and dispatch snapshots |
| `app/runtime/event/binding.py` | Explicit module/plugin/host handler resolvers; unresolved classes are diagnosed and skipped, never implicitly constructed by the bus |
| `app/runtime/event/dispatch.py` | Chain/broadcast ordering, concurrency, target-plugin filtering and isolated delivery |
| `app/runtime/event/errors.py` | Handler failure notification and non-recursive `SystemError` downgrade policy |
| `app/runtime/event/snapshot.py` | Read-only typed payload snapshots for the plugin SDK; never mutates or replaces the event ABI |
| `app/runtime/extensions/module/dispatcher.py` | Plugin-first invocation, short-circuit, list merge, signature relay and sync/async execution |
| `app/runtime/extensions/module/contracts.py` | High-frequency method families and frozen legacy fallback contract |
| `app/application/chain/context.py` | Injectable Chain dependencies, no-argument compatibility provider and legacy Transfer command Port |
| `app/startup/lifecycle/components.py` | Declarative normal/safe-mode lifecycle manifest, ordering and timeout budgets |
| `app/runtime/extensions/module/manager.py` | Module discovery and lifecycle |
| `app/runtime/extensions/plugin/manager.py` | Stable PluginManager ABI facade plus plugin discovery/lifecycle entrypoints; its constructor only consumes the startup-injected Runtime factory and does not build an environment or individual owner |
| `app/runtime/extensions/plugin/runtime.py` | Frozen typed aggregate and construction primitive for plugin registry, lifecycle, catalog, dependency, monitor, projection, sync and package-system owners; it does not choose concrete startup dependencies |
| `app/runtime/extensions/plugin/system.py` | Startup-injected market/package/dependency system-port facade, including delegation to the unique physical package deletion owner; it does not construct concrete adapters |
| `app/runtime/extensions/plugin/dependency.py` | Virtual-instance dependency classification and runtime-status persistence owner |
| `app/startup/initializers/plugins.py` | Plugin Runtime dependency-graph composition, concrete system/storage/catalog ports and lifecycle initialization |
| `app/runtime/extensions/plugin/monitor.py` | Plugin file-change aggregation and monitor-thread lifecycle |
| `app/runtime/extensions/plugin/projection.py` | Plugin commands, APIs, services, modules and actions projected from a running-registry snapshot |
| `app/runtime/extensions/plugin/storage.py` | Injected plugin configuration/data persistence port; runtime code does not import DB Oper classes |
| `app/application/plugin/catalog.py` | Plugin-market mapping, concurrent collection, generation merge, source/version deduplication and API catalog projection |
| `app/application/plugin/install.py` | Compatibility, package installation, reporting, installed-list persistence and runtime reload command |
| `app/application/plugin/routes.py` | Dynamic plugin-route registry protocol and registration/removal use cases; plugin response payloads remain raw unless the plugin chooses its own envelope |
| `app/application/plugin/folders.py` | Plugin-folder query/mutation and clone/uninstall membership maintenance, compatible with current dictionary and legacy list storage shapes |
| `app/application/plugin/release.py` | Trusted-source history and Release projection over startup-injected market/cache ports |
| `app/application/plugin/rating.py` | Plugin statistics and rating use cases over a startup-injected MoviePilot Server port |
| `app/application/plugin/runtime.py` | Plugin runtime port consumed by API, Agent and Workflow; the concrete `PluginManager` is registered only by startup |
| `app/application/module.py` | Host module runtime port consumed by entrypoints; the concrete `ModuleManager` is registered only by startup |
| `app/application/scheduling.py` | Scheduler runtime port consumed by API/Agent/application commands |
| `app/application/server/report.py` | Server reporting use cases over injected local readers and transport callbacks |
| `app/application/server/share.py` | Server sharing use cases over injected repositories and transport callbacks |
| `app/domain/plugin.py` | Pure local-source, generation-compatibility and Release-install-plan rules |
| `app/adapters/external/plugin/client.py` | `PluginMarketTransport`, the unique market transport, index/Release and local-candidate owner |
| `app/adapters/system/plugin/package.py` | `PluginPackageManager`, the unique install, checkpoint, backup, restore and physical-delete owner |
| `app/adapters/system/plugin/dependency.py` | `PluginDependencyInstaller`, the dependency manifest, missing-package inspection and installation owner |
| `app/adapters/system/plugin/health.py` | `PluginRuntimeHealth`, the runtime-environment guard, pre/post-install check and recovery owner |
| `app/startup/composition/plugin.py` | Constructs one market transport/client, package manager, runtime-health owner and dependency installer per lifespan; `PluginHelper` compatibility calls consume those same published owners |
| `app/runtime/extensions/resource.py` | Data-only managed-resource registry and sync/async lifecycle adapters |
| `app/runtime/resources.py` | Lightweight acquisition, state observation and shutdown facade |
| `app/foundation/reflection.py` | Generic reflection and Python module discovery |
| `app/adapters/network/http.py` | Shared synchronous and asynchronous HTTP clients |
| `app/adapters/network/browser.py` | Browser launch facade and browser session implementation |
| `app/adapters/system/display/` | On-first-use virtual display resource and legacy `DisplayHelper` facade |
| `app/application/rss.py` | Configured RSS retrieval and parsing |
| `app/application/site/sites.*` | Generated site catalog, authentication and index capability plus its colocated data bundle |
| `app/runtime/cache.py` | Cache contracts, memory backend, decorators and proxies |
| `app/adapters/cache/backends.py` | Redis and filesystem cache adapters |
| `app/adapters/system/resource.py` | Runtime resource detection/download/installation |
| `app/adapters/system/fsproxy.py` | Timeout-guarded local filesystem operations in a killable subprocess (with colocated `fsworker.py`) |
| `app/adapters/external/wechat.py` | WeChat enterprise-message XML encryption/decryption protocol |
| `app/application/rules.py` | Rule domain: user rule-group config access (`RuleHelper`), built-in torrent filter rule set and rule parser |
| `app/adapters/external/market.py` | Exact legacy `PluginHelper` compatibility facade and install Gateway; canonical market behavior lives in `plugin/client.py` and host code must not import this facade |
| `app/application/security/url.py` | URL/path validation, SSRF protection and signed image policy |
| `app/application/mediaserver.py` | Configured media-server discovery and identity matching |
| `app/runtime/compat/manifest.py` | Exact legacy-to-canonical import manifest |
| `app/sdk/` | Stable plugin imports, including provider-neutral browser launch functions |

Run `tests/test_architecture_dependencies.py` after every ownership or import
change. It rejects physical legacy or retired canonical sources, forbidden
upward dependencies, SDK/compat backreferences, any strongly connected
component containing a migrated module, module-to-module or module-to-chain
imports, entrypoint (`api`/`agent`/`monitor`/`workflow`/`doctor`) imports of
`app.modules` internals, chain imports of `app.modules` internals (chains reach
modules only through `run_module` dispatch), and downloader SDK
(`qbittorrentapi`, `transmission_rpc`) imports inside `app/chain`.

*Last Updated: 2026-08-29*
