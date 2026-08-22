# MoviePilot V3 后端架构二阶段提升方案

> 文档性质：当前架构复核、优秀 Python 后端实践对标、AI 可执行任务手册
> 适用仓库：`MoviePilot`，分支 `v3`
> 审计基线：`6404a3aa583de03bf0770c37b106413461cec1f8`（2026-08-21）
> 审计范围：宿主后端；排除 `app/plugins/**` 运行时插件副本
> 规范优先级：`AGENTS.md` 与 `docs/rules/` 高于本文
> 相关文档：`docs/architecture-overview.md`、`docs/refactor/backend-architecture-governance.md`、`docs/refactor/backend-module-refactor-compatibility.md`
> 实施进度：阶段 0～6 的宿主架构能力已完成收口；按既定范围暂不处理插件仓适配、Outbox 外围扩展和 25 个存量超长方法拆分

## 1. 结论先行

MoviePilot V3 当前不是“目录混乱、必须推倒重来”的状态。第一阶段治理已经取得实质成果：

- `foundation/domain/runtime/adapters/application` 等实现根没有自有循环依赖；
- Adapter、Runtime、Application、Chain、API 等重点边界到 DB 的禁止边为零；
- `app.core`、`app.helper`、`app.utils` 已经是精确兼容入口，不再是宿主实现目录；
- 插件 raw API、SDK/Compat、生命周期清单、模块调度快照和零真实网络测试均已有门禁；
- 当前唯一 SCC 位于隔离的 TMDB 移植包内部，不应为了指标归零重写第三方风格代码。

因此，下一阶段不应继续以“搬文件、拆目录、减少行数”为主目标。真正需要处理的是八类运行时和演进问题：

1. **架构基线工具把语义、源码位置和跨仓版本混在一起。**普通行号变化或独立插件仓更新都会触发全量基线漂移，AI 容易用 `--write` 掩盖真正变化。
2. **部署拓扑与实际进程职责不一致。**`API_WORKERS` 可配置多进程，但每个 Uvicorn worker 都会独立运行插件、调度器、监控器和工作流；当前 `app.main` 又把 app 实例传给 Uvicorn，与 reload/workers 的官方约束不一致。
3. **事务所有权只在少数新用例中收口。**ORM Model 仍大量自带查询和自动提交装饰器，Oper 多为薄转发；调用方无法一眼判断一次业务动作何时提交、回滚和触发提交后副作用。
4. **组合根之后仍存在大量全局服务定位。**宿主有 180 个文件直接读取 `settings`，21 个文件出现 45 次 `SystemConfigOper()` 构造；API 数据端口仍是全局字符串注册表。
5. **模块与事件契约主要是“快照化的动态协议”。**211 个模块方法名中有 96 个落在 legacy 默认契约；53 个事件只有 20 个专用 `EventData` model，payload、可见范围和可靠性等级没有统一登记。
6. **后台副作用缺少统一可靠性定义。**事件队列、APScheduler、FastAPI BackgroundTasks 和线程池任务的丢失、重试、幂等、关停语义各不相同；数据库提交与事件/上报之间仍有进程崩溃窗口。
7. **运维可观测性不足以解释长调用链。**缺少统一 request/correlation ID、公开的 liveness/readiness 边界、队列深度和任务耗时指标；日志能看到错误，但难以串起 API → Chain → Module → 外部请求。
8. **质量门禁偏重“能运行”，弱于“可演进”。**Pylint 只手工触发且仅启用严重错误；没有渐进式类型门禁和复杂度趋势门禁，千行级用例方法仍可能继续增长。

建议保持**模块化单体**，按以下顺序治理：

```text
先修治理工具和部署真相
    -> 再统一事务边界和运行时装配
    -> 再类型化模块/事件协议
    -> 再为关键副作用补持久可靠性
    -> 最后收敛可观测性、类型和复杂度预算
```

不建议在本轮引入微服务、通用 DI 框架、Celery/Kafka、全仓 ORM 重写或全仓强类型。这些动作会显著扩大插件兼容、部署和回滚面，但不能直接解决当前最重要的问题。

## 2. 当前审计基线

### 2.1 取证方法

本次复核执行或检查了：

- 仓库、分支、上游和工作树状态；
- `AGENTS.md`、架构/设计/测试规则和两份既有重构文档；
- 当前宿主 AST 依赖图、SCC、禁止边和运行契约快照；
- FastAPI 创建、路由聚合、异常处理、生命周期和 Uvicorn 入口；
- SQLAlchemy Session、事务装饰器、UnitOfWork、Model 与 Oper 的真实调用关系；
- Event、Module dispatcher、Scheduler、插件运行时与后台线程；
- 方法规模、端点规模、全局配置读取和类型注解近似统计；
- 独立 `MoviePilot-Plugins` 仓的当前版本与宿主内插件兼容基线关系。

### 2.2 已验证数据

| 指标 | 当前值 | 判断 |
| --- | ---: | --- |
| 宿主 Python 模块数 | 753 | 排除 `app/plugins/**` |
| 宿主内部导入边 | 6,076 | 边数本身不是质量目标 |
| 非平凡 SCC | 1 | 仅 TMDB 移植包内部 |
| 重点禁止边 | 0 | Adapter/Runtime/Application/API/Chain 等到 DB 的既有门禁均通过 |
| 架构专项测试 | 39 passed | `test_architecture_dependencies` + `test_architecture_contract_baseline` |
| 宿主 Python 代码行 | 约 241,227 | 含注释和空行，仅用于趋势 |
| 已登记模块调用方法 | 211 | 260 个静态调用点，0 个动态方法名调用点 |
| legacy 默认模块契约 | 96 | 显式契约目前也主要只描述 family/legacy aggregation |
| 事件枚举 | 53 | 66 个静态 producer、15 个静态 consumer |
| 专用 EventData model | 20 | 尚未形成 EventType → payload model 的完整映射 |
| 直接读取 `settings` 的文件 | 180 | 全局部署配置仍是广泛事实 API |
| `SystemConfigOper()` | 45 次 / 21 文件 | Agent、Module、Startup 等仍直接构造 |
| Model 上的 DB 事务装饰器 | 178 | `app/db/oper` 中为 0，Oper 多委托给 Model |
| 路由端点 | 335 | 11 个已装饰端点超过 80 行，最大 400 行 |
| Chain 方法超过 150 行 | 18 | 最大 `TransferChain.do_transfer()` 885 行 |
| Application 方法超过 150 行 | 8 | 最大 296 行 |
| Agent 方法超过 150 行 | 13 | 最大 713 行 |
| 公共函数缺少返回注解 | 约 1,592 / 7,442 | AST 近似值，适合做 ratchet，不适合直接作为失败阈值 |
| 公共参数缺少注解 | 约 858 / 12,763 | 主要集中在 `app/modules` |

代表性大方法：

- `app/chain/transfer.py::TransferChain.do_transfer()`：约 885 行；
- `app/chain/download.py::DownloadChain.batch_download()`：约 572 行；
- `app/chain/subscribe.py::SubscribeChain.match()`：约 417 行；
- `app/api/endpoints/agent.py::web_agent_stream()`：约 400 行；
- `app/api/endpoints/transfer.py::manual_transfer()`：约 295 行；
- `app/scheduler.py::Scheduler.init()`：约 383 行。

### 2.3 本次检查暴露的基线问题

宿主架构测试通过，但下面的跨仓命令失败：

```bash
./.venv/bin/python scripts/architecture/baseline.py \
  --check \
  --plugin-repo ../MoviePilot-Plugins
```

失败对象是 `tests/fixtures/architecture/official-plugin-baseline.json`。宿主内基线记录的插件仓提交为
`ddb41dbcbbea21196154a7f6d5fdba3aa34a5e4a`，当前独立插件仓为
`217c8d25ffe6ff0b3f6352c4278fd6896def442e`。这不是宿主依赖环回归，但当前命令无法单独表达“宿主硬门禁通过、外部插件仓观察值已变化”。

另一个风险是 `scripts/startup/performance.py` 在不传 `--output` 时会直接覆盖已提交基线。其他 AI 在只想读取当前数据时，很容易制造未审查的基线变更。

**本次审计没有更新任何基线文件。**上述意外写入已恢复，最终工作树只包含本文和文档索引改动。

阶段 0 实施后，宿主与插件基线已使用独立 check/write 入口；运行契约行号只进入按需诊断，
插件 commit、源码摘要和文件数只作为 provenance。当前宿主和官方插件语义检查均通过，CI 会在
主仓 PR/push 执行宿主硬门禁，并在定时/手工工作流中上传最新插件仓的语义差异报告。

## 3. 优秀 Python 后端实践对标

本节只采用与 MoviePilot 当前形态相近、能转化为具体约束的实践。参考不是为了照抄目录，而是为了验证职责、生命周期和失败语义。

| 对标来源 | 可复用实践 | MoviePilot 当前差距 | 采用方式 |
| --- | --- | --- | --- |
| [FastAPI：Bigger Applications](https://fastapi.tiangolo.com/tutorial/bigger-applications/) | Router、依赖和主应用分离；路由按领域聚合 | Router 已分文件，但 `app/api/deps.py` 集中 33 个依赖工厂，部分端点仍编排完整用例 | 保留现有 Router；按垂直切片拆依赖和 presentation mapper，不重做目录树 |
| [FastAPI 官方 Full Stack Template](https://github.com/fastapi/full-stack-fastapi-template/tree/master/backend/app) | 请求依赖提供 Session，测试和迁移入口明确 | MoviePilot 已有请求 Session 和 UoW，但大量 Model 方法仍自行取得 Session/commit | 将 Session 生命周期留在请求/作业边界，Repository 只登记变更 |
| [SQLAlchemy Session Basics](https://docs.sqlalchemy.org/en/20/orm/session_basics.html) | Session/事务生命周期应与具体数据操作分离；Session per thread、AsyncSession per task | `@db_update`/`@async_db_update` 隐式创建和提交，跨多个 Repository 的原子性不清晰 | 新写用例强制请求/任务级 UoW；Model 逐步变为映射和约束载体 |
| [Starlette Lifespan](https://www.starlette.io/lifespan/) | Lifespan 完成前不接流量；用 typed state 共享进程资源；用 task group 管理异步任务 | 已有声明式生命周期，但仍依赖多个模块全局注册表和裸 `create_task`/线程 | 建立类型化 `HostRuntime/AppState`，旧 provider 继续作兼容门面 |
| [Uvicorn Deployment](https://www.uvicorn.org/deployment/) 与 [Lifespan](https://www.uvicorn.org/concepts/lifespan/) | reload/workers 使用 import string/factory；每个 worker 独立执行 lifespan | 当前 app 实例与 reload/workers 配置并存，多 worker 会重复控制面 | V3 先明确只支持单 worker；开发 reload 改为 factory/import string；未来再拆 control role |
| [Home Assistant：Integration Quality Scale](https://developers.home-assistant.io/docs/core/integration-quality-scale/) | 插件/集成按可测试性、错误处理、异步安全、类型和文档分级；豁免必须说明 | Module 能力差异大，只有统一发现和方法名快照，没有每个集成的质量状态 | 为宿主 Module 建立轻量质量清单和逐项 ratchet，不阻塞历史模块运行 |
| [Home Assistant：Blocking operations with asyncio](https://developers.home-assistant.io/docs/asyncio_blocking_operations/) | 阻塞 I/O 必须移出事件循环，并提供检测 | 项目已有 ThreadHelper/异步 HTTP，但没有统一的阻塞调用检测门禁 | 先对 API、Agent、Application 新代码加调试/测试检测，不开展全仓 async 重写 |
| [Home Assistant：Fetching data](https://developers.home-assistant.io/docs/integration_fetching_data/) | 外部集成统一刷新协调、并发限制、退避和认证失效语义 | 各 Module 自行决定轮询、缓存、限流和错误降级 | 先建立 Module quality/contract 字段，再为同一模块族复用 coordinator |
| [OpenTelemetry Python](https://opentelemetry.io/docs/languages/python/) | Trace/Metric 稳定，支持标准上下文传播和框架/HTTP client instrumentation | 当前缺少跨 API、Chain、Module 和外部请求的关联标识 | 先实现无依赖 request ID；OTel 作为可选 Adapter，不能成为核心层依赖 |

### 3.1 明确不照抄的内容

- FastAPI 示例中的直接 endpoint → ORM 只适合较小 CRUD 服务；MoviePilot 有插件、调度、工作流和多入口，仍应使用 Application/Repository。
- Home Assistant 的完整 Integration Framework 不能直接替换现有 Module/Plugin 体系；只借鉴质量清单、异步边界和刷新协调思想。
- OpenTelemetry 不应在第一步强制进入所有环境；应先定义内部观测端口和 request ID，再提供可选 exporter。
- 不因 SQLAlchemy 官方示例使用同步 Session 就把现有 async 查询全部改回同步；关键是事务范围清晰，不是统一一种 I/O 风格。

## 4. 目标架构

目标仍是单仓、单部署单元的模块化单体，但把 API 数据面、宿主控制面、持久可靠性和观测边界区分开：

```mermaid
flowchart TB
    Entry["API / CLI / Agent / Scheduler / Workflow"]
    ApiState["Typed HostRuntime / AppState"]
    UseCase["Application Command / Query"]
    Domain["Domain rules"]
    Ports["Repository / Module / External Ports"]
    Adapters["DB Oper / Module Dispatcher / External Adapters"]
    Control["Control Plane: Plugin / Scheduler / Monitor / Event"]
    Durable["Durable Job / Outbox / Existing recovery tables"]
    Observe["Request ID / Metrics / Trace Adapter / Health"]

    Entry --> ApiState --> UseCase
    UseCase --> Domain
    UseCase --> Ports --> Adapters
    Control --> Ports
    UseCase --> Durable
    Control --> Durable
    Entry -.correlation.-> Observe
    UseCase -.correlation.-> Observe
    Adapters -.correlation.-> Observe
```

强制原则：

1. API/CLI/Agent/Scheduler 只是不同入口，事务和业务完成定义归 Application 用例。
2. Model 负责映射、数据库约束和必要的同表纯条件表达；不拥有 Session 生命周期和自动提交。
3. Oper/Repository 负责查询和登记变更；不决定整个业务动作何时 commit。
4. 宿主运行对象由 Startup 创建；FastAPI 通过 typed state/Depends 读取，不新增字符串 Service Locator。
5. Module 与 Event 的动态兼容继续存在，但宿主高频能力必须有可检查签名、结果和错误语义。
6. 普通进程内通知可以丢失；影响用户数据完成状态的副作用必须显式选择 durable 语义。
7. V3 默认单 worker。未拆出控制面前，不允许通过多 worker 复制插件、调度和监控运行时。
8. 插件公开 ABI 只增不删；宿主内部改造不能要求同步修改所有第三方插件。

## 5. 分阶段实施路线

每个任务均应独立提交、独立回滚。除非任务明确说明，不允许同时改数据库 schema、前端协议和插件仓。

### 阶段 0：先让治理工具可信

#### ARCH-201：拆分宿主与插件基线命令

**目标**：基线工具能够分别检查/写入宿主依赖、宿主运行契约、官方插件兼容和启动性能，禁止一次操作无差别覆盖全部文件。

**允许范围**：

- `scripts/architecture/baseline.py`
- `scripts/startup/performance.py`
- `tests/test_architecture_contract_baseline.py`
- 新增的脚本 CLI 测试
- 对应文档

**实施步骤**：

1. 为架构脚本增加互斥的细粒度参数，例如：
   `--check-host`、`--check-plugins`、`--write-host`、`--write-plugins`。
2. `--check-host` 不要求 `../MoviePilot-Plugins` 存在。
3. `--write-*` 必须显式指定目标；不带写参数只能输出到 stdout 或退出。
4. 性能脚本改为 `--print`/`--check`/`--write` 三种明确行为；默认只打印，不能覆盖 fixture。
5. 写入前在 stdout 列出将修改的文件；写入后仍由 Git diff 供人工审查。
6. 保留旧 `--check/--write` 一小段兼容期时，只允许它们给出弃用提示并要求显式 scope，不能继续静默全写。

**禁止**：

- 不因当前插件仓已变化而直接刷新 baseline；
- 不在本任务改变依赖规则、SDK 导出或插件 hook；
- 不把 Git commit hash 变化等同于 ABI 变化。

**验证**：

```bash
./.venv/bin/python -m pytest \
  tests/test_architecture_contract_baseline.py \
  tests/test_architecture_baseline_cli.py -q

./.venv/bin/python scripts/architecture/baseline.py --check-host
./.venv/bin/python scripts/architecture/baseline.py \
  --check-plugins --plugin-repo ../MoviePilot-Plugins
```

**完成标准**：只改插件仓时宿主门禁仍能独立通过；只读命令不会修改工作树；每个 fixture 都有独立写入口。

**回滚**：恢复旧 CLI 解析器即可；不得回滚已审查的 fixture 内容。

**实施记录（2026-08-21）**：

- `baseline.py` 已提供 host/plugin 的显式 check/write scope，性能基线提供 print/check/write；默认行为只读，
  write 前列出目标，宿主检查不依赖外部插件仓存在。
- CLI、只读工作树和 fixture 定向写入测试已覆盖，提交为 `7bc3ea83`。

#### ARCH-202：把语义基线与诊断位置分开

**目标**：正常行号移动、时间戳或插件仓 HEAD 变化不再触发“架构语义变化”；真实方法、事件、导入、签名和结果契约变化仍失败。

**实施步骤**：

1. 将 `caller + line` 拆为 gate key（`caller + operation/method/event`）和 diagnostic（当前 line，仅用于报告）。
2. `generated_at`、平台、源码 HEAD、源码摘要属于 provenance，不参与 semantic equality。
3. 插件基线分别保存公开导入、hook、动态 API 路由契约和扫描来源 revision。
4. CI 比较语义集合；revision 改变但语义不变时只输出 notice。
5. 对 import edge 继续保留完整集合，但将“禁止边”测试与“全图快照”测试分开，前者是硬门禁，后者要求审查变化原因。
6. 为旧 fixture 写一次 schema migration 读取器，避免直接删除历史字段导致维护脚本失效。

**完成标准**：只插入空行不改变 semantic fixture；改 `run_module("...")`、EventType、SDK 导出或 Compat 映射仍稳定失败。

**实施记录（2026-08-21）**：

- semantic key 与行号/来源 revision 诊断已分离；时间、位置和外部仓 HEAD 不再参与硬比较，真实 import、method、
  event、SDK/compat 变化仍产生精确 diff。
- 旧 fixture 可兼容读取，语义稳定性与真实变更失败测试通过，提交为 `37c442b0`。

#### ARCH-203：把架构与跨仓兼容纳入持续 CI

**目标**：当前只手工执行的架构/插件观察变成分层 CI。

**实施步骤**：

1. 每个主仓 PR 必跑宿主架构测试和 `--check-host`。
2. 官方插件兼容分为 PR 硬门禁（仓库内固定 fixture/样例插件）和定时/手工观察（独立插件仓最新默认分支）。
3. 跨仓观察失败不能由机器人自动 `--write`；必须创建可审查结果，说明新增/删除的导入、hook、API 契约。
4. Pylint 工作流至少对主仓 PR 运行改动文件严重错误检查；全仓报告仍可手工生成。

**完成标准**：宿主 PR 不因外部仓普通版本变化随机红灯；真实兼容破坏能定位到插件和符号。

**实施记录（2026-08-21）**：

- 主测试 workflow 增加独立宿主 Architecture Contract Gate，跨仓插件检查进入定时/手工 observation workflow，
  只上传报告、不自动写 baseline。
- Pylint 硬门禁只检查本次改动 Python 文件，全仓结果保留 advisory artifact；提交为 `6c7c54d2`、`0959831b`。

### 阶段 1：固定部署拓扑和统一入口

#### ARCH-210：V3 全功能模式强制单 worker

**现状证据**：

- `app/runtime/config.py` 暴露 `API_WORKERS`；
- `app/main.py` 将具体 `app` 实例和 `workers=settings.API_WORKERS` 同时交给 Uvicorn；
- `app/startup/lifecycle/__init__.py` 在 lifespan 中启动插件、APScheduler、监控器、命令和工作流；
- Uvicorn 官方说明每个 worker 都会独立执行 lifespan。

**目标**：在控制面拆分完成前，拒绝 `API_WORKERS != 1`，避免重复调度、重复插件事件、重复文件监控和进程内状态分裂。

**实施步骤**：

1. 新增位于 Startup/入口边界的 `validate_process_topology()`，不要放进 Domain/Application。
2. 全功能模式下 `API_WORKERS != 1` 直接给出可操作错误；不能只打 warning 后继续启动。
3. Doctor 增加同一诊断，说明为什么不是“多开几个 API worker 就能扩容”。
4. 文档明确：V3 当前扩容单位是完整 MoviePilot 实例，单一配置/数据库只应有一个控制面实例。
5. 补回归测试：worker=1、worker>1、safe mode，以及环境变量解析错误。

**禁止**：

- 不用数据库锁“快速解决”全部多进程问题；动态插件路由、Singleton、内存交互状态和监控器仍会分裂；
- 不在本任务引入 Redis leader election；
- 不删除 `API_WORKERS` 配置键，以免旧配置解析失败。

**完成标准**：不再存在看似支持、实际重复运行控制面的多 worker 配置。

**实施记录（2026-08-21）**：

- Startup 边界、launcher 与 Doctor 共用单 worker 拓扑合同；全功能模式拒绝 worker>1，safe mode 与旧配置键保持
  兼容，部署文档解释控制面重复风险。
- worker=1/>1、safe mode 和配置解析专项测试通过，提交为 `d89d2961`。

#### ARCH-211：修正 Uvicorn app factory 与开发 reload

**目标**：生产、开发 reload、测试和外部 ASGI supervisor 使用明确、可验证的入口，不依赖 app 实例在 multiprocessing/reload 下的未支持行为。

**实施步骤**：

1. 冻结四种入口行为：`python -m app.main`、本地 `start-local.sh`、`app.factory:create_app`、`TestClient`。
2. 开发 reload 使用 import string + factory 形式，不直接传 app 实例。
3. 生产入口保持自定义优雅停止语义，但只启动一个 worker。
4. `create_app()` 只做 ASGI 结构创建；插件运行实例、数据库连接和后台线程不得在 import/create 阶段物化。
5. `app.factory:app` 若需保留，作为薄兼容入口调用同一个 factory，测试对象 identity 和副作用。
6. 对每种入口记录：import 是否联网、是否开线程、是否建 DB 连接、lifespan start/stop 次数。

**验证**：

```bash
./.venv/bin/python -m pytest \
  tests/test_moviepilot_launcher.py \
  tests/test_lifecycle_shutdown.py \
  tests/test_testing_bootstrap.py -q
```

**实施记录（2026-08-21）**：

- reload/监督进程统一使用 `app.factory:create_app` import-string factory；生产入口保留单 worker 和既有优雅停止，
  import/create 阶段不启动 DB、插件或后台线程。
- launcher、lifespan、factory/TestClient 与信号关停测试通过，提交为 `bb57b229`。

#### ARCH-212：统一数据库准备与健康语义

**现状证据**：`run_application()` 会调用 `prepare_database()`，外部 supervisor 直接加载
`app.factory:app` 时不会；lifespan 当前只预热引擎，不执行迁移准备。

**目标**：所有受支持入口对数据库迁移、备份、head 校验和 ready 状态具有同一语义。

**推荐方案**：在 V3 单 worker 前提下，把数据库准备作为最早的声明式生命周期组件；若未来拆多 worker，再改为独立 prestart/control role。

**实施步骤**：

1. 为现有 `prepare_database()` 补入口矩阵测试，不先改算法。
2. 新增“数据库准备”生命周期组件，顺序早于 Router、Module、Plugin 和 Scheduler。
3. 删除 `app.main` 的重复调用，保证一个进程只走一个事实入口。
4. 数据库准备失败必须阻止 readiness 和服务接流量；不能降级成后台日志。
5. 新增最小公开探针：
   - `/health/live`：进程和事件循环存活，不查外部系统；
   - `/health/ready`：生命周期完成、数据库 revision/head 可用、控制面未处于不可恢复启动失败。
6. 公开探针只返回最小状态，不泄露路径、版本链、插件名和异常栈；详细诊断仍需管理员鉴权。

**回滚**：生命周期组件可暂时委托回 `app.main` 旧调用，但同一版本不能同时保留两个主动迁移入口。

**实施记录（2026-08-21）**：

- 数据库准备已成为最早生命周期组件，`app.main` 不再重复迁移；所有受支持入口共享 prepare/head/readiness 状态。
- `/health/live` 与 `/health/ready` 使用最小响应，准备失败阻止 ready；入口矩阵、失败和探针测试通过，提交为
  `dd1c4c32`。

### 阶段 2：统一数据访问和事务所有权

#### ARCH-220：建立 Model/Repository 事务 ratchet

**目标**：先禁止债务增长，再按用例迁移；不要求一个 PR 清除 178 个 Model 装饰器。

**实施步骤**：

1. 在架构 fixture 记录 `app/db/models/**` 中：
   - `@db_query` / `@db_update` / async 变体数量；
   - 每个 Model 的装饰方法清单；
   - 直接 `Session.commit/rollback` 调用。
2. 新增硬规则：新 Model 不得新增会话生命周期/自动提交方法；修改到的旧写方法应优先迁移到 Oper。
3. `app/db/oper/**` 允许 SQLAlchemy 查询和 stage mutation，但禁止自己创建独立 Session 或在可组合方法中 commit。
4. Application command 持有 UnitOfWork；API、Scheduler、Agent 分别在自己的逻辑操作起点创建 Session/UoW。
5. 读操作允许短会话自动 close，但不得让返回的 ORM lazy attribute 在 Session 外才加载。
6. 对同步线程和异步任务分别验证 Session 独占，禁止跨线程/跨 task 共享同一个 Session。

**完成标准**：装饰器基线不增长；新写用例可从测试中明确观察 `stage -> commit -> after-commit effect` 顺序。

**实施记录（2026-08-21）**：

- host architecture baseline 新增 transaction debt 域，记录 Model decorator 和直接 commit/rollback；新增或增长会
  失败，减少允许通过。
- Repository/Oper 的 Session 所有权与可组合 stage 规则已有静态和生命周期测试，提交为 `de2957b9`。

#### ARCH-221：以订阅写入做首个完整事务切片

**范围**：

- `app/application/subscription/`
- `app/db/oper/subscribe.py`
- `app/db/models/subscribe.py`
- `app/api/deps.py` 中对应依赖工厂
- 订阅 API/Agent/Scheduler 聚焦测试

**目标**：同一个“新增或修改订阅”用例无论从 API、Agent 还是 Scheduler 进入，都由 Application command 决定事务完成和提交后副作用。

**实施顺序**：

1. 列出所有写入口和旧返回/事件/上报顺序。
2. 先补 commit 失败、事件失败、上报失败和重复请求测试。
3. 把所需 SQL 从 Model classmethod 移到 `SubscribeOper`；Model 保留字段、约束和无 I/O 条件表达。
4. Repository 方法只 `add/update/delete/flush`，不 commit。
5. Command 统一 commit/rollback；只有 commit 成功后才发送事件、刷新调度和上报。
6. 旧 Chain/API 方法委托新 Command，保留返回值、消息、事件 payload 和插件可见行为。
7. 度量本切片迁移前后 Model 装饰器、方法长度和事务测试数量。

**禁止**：

- 不顺便改订阅表字段或媒体身份；
- 不同时重写订阅搜索/匹配算法；
- 不让事件失败回滚已经提交的数据库事务并伪装成“数据库未写入”。

**完成标准**：一个业务动作只有一个事务所有者；任意入口都不会因内部 Model 方法提前 commit 而产生部分写入。

**实施记录（2026-08-21）**：

- `app/startup/ports/subscription.py` 为每次规范新增创建独占同步/异步 Session；
  `CreateSubscriptionCommand` / `AsyncCreateSubscriptionCommand` 持有 UoW，Oper 只执行
  查重、`add` 与 `flush`。
- `SubscribeOper.stage_add()` 的查重 SQL 已收口到 Oper，不再调用 Model 自动会话装饰器；
  无会话构造 `SubscribeOper()` 的旧 SDK 路径保留原自动短会话和返回值，未扩散为规范入口。
- Chain 把原有“成功消息 → `SubscribeAdded` 事件 → Server 统计”作为显式 post-commit
  回调交给 Command；commit/flush 失败回滚，事件或上报失败只传播原异常，不回滚已提交记录。
- 同步/异步 `SubscribeChain.add` 方法长度从各 203 行降至 183/186 行；新增 9 个事务边界测试，
  覆盖成功顺序、commit/flush 失败、重复请求、Oper 不提交、事件失败、上报失败与真实落库。
- Model 装饰器总数仍为 178：本切片绕开了继承自 `Base.create/async_create` 的自动提交，
  但为保留既有 Model/旧 SDK 查询兼容未机械删除查询装饰器；ratchet 保持不增，后续切片继续下降。

#### ARCH-222：按风险迁移其余写用例

推荐顺序：

1. 站点配置写入；
2. 下载/整理历史删除与恢复；
3. 工作流定义和状态变更；
4. Agent task/chat 写入；
5. 插件配置与安装状态。

每个切片沿用 ARCH-221，不允许批量移动全部 Model 方法。查询方法可在写边界稳定后再迁移。

**实施记录（2026-08-21）**：

| 风险域 | 规范事务入口 | 结果 |
| --- | --- | --- |
| 站点配置 | `SiteMutationCommand` + Async UoW | create/update/priorities/delete/reset 均先 stage 再 commit |
| 下载/整理历史 | `DownloadHistoryMutationCommand`、`TransferHistoryMutationCommand` + Sync UoW | 多表删除与文件副作用顺序已有聚焦回归 |
| 工作流 | `WorkflowMutationCommand`、`WorkflowDefinitionCommand` + Sync/Async UoW | 定义写入提交后才刷新 timer/event |
| Agent chat | `AgentChatService` + 请求级 Async UoW | API 会话删除改为 `async_stage_delete()`；失败回滚、缺失不提交 |
| 插件数据重置 | `DeletePluginDataCommand` + 独占 Sync Session/UoW | `PluginDataOper.stage_delete()` 只 DELETE/flush；重置链由 startup 装配 |

旧插件与宿主存量代码直接构造 `PluginDataOper`、`AgentChatOper` 的行为继续保留；新 API 和插件
重置链不得回退到这些自动提交兼容方法。五类矩阵聚焦测试共 57 项通过，事务 ratchet 仍为
178 且没有新增或搬移 Model 装饰器。

### 阶段 3：类型化运行时装配，减少全局服务定位

#### ARCH-230：建立类型化 HostRuntime / AppState

**目标**：用 Startup 创建的显式运行时对象替代全局字符串注册表，同时保留旧 provider 兼容入口。

**建议结构**：

```text
app/startup/ports/context.py          # HostRuntime 及构建结果
app/api/context.py              # API 可见的最小 AppState / 读取依赖
app/api/dependencies/           # 按领域拆分依赖工厂
  auth.py
  subscription.py
  site.py
  workflow.py
```

以上文件名均为单个小写单词，符合仓库命名规则。

**实施步骤**：

1. 定义 slots dataclass 或 TypedDict，字段使用具体 Protocol 类型，禁止 `dict[str, Any]` 仓储表。
2. Startup 构建 HostRuntime；lifespan 通过 `app.state` 或 yield state 暴露给请求。
3. FastAPI Depends 从 Request/AppState 取精确能力，不直接读取模块全局 `_ports`。
4. `ApiDataPorts` 和 `configure_api_data_ports()` 暂时保留为兼容 Facade，内部委托同一个 HostRuntime，不形成第二份实例。
5. 先迁移一个垂直切片，验证测试可以传 fake runtime，不加载真实 PluginManager/DB engine。
6. 每迁移一个领域就删除对应字符串 key；禁止新增新 key。

**禁止**：

- 不引入第三方 DI container；
- 不创建一个更大的全局 `services: dict[str, Any]`；
- 不把完整 HostRuntime 传入 Domain 或每个小函数。

**实施记录（2026-08-21）**：

- `app/startup/ports/context.py` 定义 frozen slots `HostRuntime` 与首个窄能力
  `AgentChatRuntime`，仓储、Session、UoW 字段均为具体 Protocol 工厂，不是字符串字典。
- `init_modules()` 保留零参数兼容签名并返回本次 lifespan 唯一 Runtime；生命周期组件把结果挂到
  `app.state.host_runtime`。`app/api/context.py` 只向 Depends 暴露 Agent chat 的最小能力。
- `get_agent_chat_service` 不再读取全局 `_ports` 或 `"agent_chat"` key；该 key 已从宿主和测试
  `ApiDataPorts.repositories` 删除。未迁移领域仍通过 `compatibility_api_data` 使用同一个实例。
- fake Runtime 请求测试证明仓储与 UoW 共享同一请求会话，且无需加载真实 DB engine、
  PluginManager 或其他运行时服务；旧 `configure_api_data_ports()` 调用形态继续可用。

**收口记录（2026-08-22）**：

- `HostRuntime` 已覆盖认证/用户/PassKey、消息、下载与整理历史、媒体服务器、站点、订阅、
  工作流、请求 Session/UoW 和配置快照等全部正式 API 业务领域。每个能力均为命名字段，
  不再由 `repository("name")` 或 `transaction("name")` 在运行时猜测。
- `app/api/dependencies/` 的正式领域模块已清除 `app.api.data` 与
  `app.api.dependencies.data` 依赖，并增加静态测试防止回退。旧 `ApiDataPorts` 只作为旧导入
  ABI 的全局转发保留，不再挂入 `HostRuntime`，也不参与正式 FastAPI 请求装配。

#### ARCH-231：按领域拆分 API dependency 与 presentation

**目标**：`app/api/deps.py` 从 512 行集中装配点变成兼容聚合入口，端点只负责 HTTP 解析、鉴权依赖和结果映射。

**实施步骤**：

1. 不做纯机械切文件；随 ARCH-221/222 的垂直用例迁移对应依赖。
2. 每个依赖模块只组装本领域 command/query 和身份依赖。
3. 协议特例（OpenAI/Anthropic/MCP/plugin raw）保留独立 presentation mapper，不进入通用 Response 逻辑猜测。
4. 新端点原则上不超过 80 行；超过时必须在 PR 中说明流协议、资源清理或兼容原因。
5. SSE 端点拆为请求校验、执行 service、event → wire mapper、disconnect/cancel 清理。

**优先切片**：`manual_transfer()`、`web_agent_stream()`、OpenAI/Anthropic streaming adapter。

**实施记录（2026-08-21）**：

- `app/api/deps.py` 已由 524 行集中装配点收敛为 88 行兼容聚合入口；认证、Agent、订阅、站点、
  工作流、历史和插件依赖分别由 `app/api/dependencies/` 下的领域模块拥有。宿主 API 端点全部改为
  直接导入领域依赖，旧入口只为外部兼容消费者保留。
- 新增 `app/api/presentation/sse.py`，统一 non-buffered SSE transport 策略，并分别提供 unnamed data
  与 named event wire mapper。WebAgent、OpenAI 和 Anthropic 继续保留各自协议 payload 与错误结构，
  不进入通用 `Response` 包装。
- `manual_transfer()` 已缩为 HTTP/鉴权/依赖入口，历史恢复、批量预览和旧 `TransferChain` 参数兼容
  由内部处理器承接；WebAgent 的拒绝响应、stream headers 与协议映射已从主控制流抽离。长生命周期
  generator 仍保留在端点模块，因为它直接拥有 request disconnect、后台 task cancel 与敏感结果关闭时序，
  后续只能在保持现有取消测试的前提下继续下沉。
- 125 个鉴权、手动整理、WebAgent、OpenAI/Anthropic 生命周期、API 响应和 typed runtime 专项测试通过；
  61 个架构/基线 CLI 测试通过。依赖基线变化只反映集中边拆为领域边，runtime contract 变化只反映
  dependency callable 的新模块路径；禁止边与插件 raw API 均未变化。

#### ARCH-232：配置快照与窄配置端口

**目标**：阻止 `settings` 和 `SystemConfigOper()` 继续扩散，不要求一次清除 180 个文件。

**实施步骤**：

1. 将 180/45 作为趋势基线，新增调用必须说明所属边界。
2. `settings` 只保存启动环境/部署配置；Application 用例接收所需字段组成的 frozen config snapshot。
3. 持久化用户配置使用 `SystemConfigReader/Writer` Protocol；默认实现包装 `SystemConfigOper`。
4. 长生命周期 Module 在初始化/配置变更时接收配置快照，不在每个方法中全局读取。
5. Agent tool 通过注入的设置服务读取可授权字段，不直接构造 Oper。
6. 保留 `app.sdk.config.settings` 给旧插件；宿主 canonical 新代码不得因此继续扩大直接依赖。

**实施记录（2026-08-21）**：

- 新增 `configuration-debt-baseline.json` 与单向 ratchet。基线排除 `app/plugins`、`app/sdk` 和
  `app/runtime/compat`，当前 canonical 宿主为 169 个直接导入 `settings` 的文件、15 个真实
  `app.db.oper.systemconfig.SystemConfigOper` 构造点；删除旧债务继续通过，新增或换位置均失败。
- `SystemConfigReader` / `SystemConfigWriter` 已成为持久用户配置的窄端口；`SystemConfigService`
  支持分别注入 reader/writer，同时保留 `repository=` 兼容装配。Agent 系统设置查询与修改工具支持
  显式注入授权配置端口，旧工具构造签名与密钥确认/脱敏行为不变。
- 整理失败重试从 Application 直接读取全局 `settings` 改为 `TransferRetryConfig` frozen snapshot；
  启动组合根和测试组合根负责生成每次用例快照，reload 后新调用读取新 generation，旧调用不漂移。
- Bangumi 模块作为长生命周期样板，在 `init_module()` / `on_config_changed()` 时更新不可变网络快照，
  `test()` 不再逐次读取全局代理。该模式先验证后推广，不批量改写 169 个存量调用方。
- 219 个架构、配置、Agent 安全、整理重试、Module reload 专项测试通过，Pylint 10/10；依赖基线
  仅把 `app.application.history -> app.runtime.config` 替换为窄配置端口边，禁止边不变。

**扩展实施记录（2026-08-22）**：

- `HostRuntime.configuration` 现在提供 API、Scheduler、Chain 三类 frozen snapshot 工厂。API 每个请求、
  Scheduler 每次初始化/任务注册都取得新快照，因此配置 reload 后的新调用可见新值，已经开始执行的调用
  不会在中途漂移；Chain 基础文件后缀由启动上下文一次注入。
- 登录、仪表板和整理历史 API 不再直接导入 `settings`；`Scheduler` 已清除全部直接 `settings` 访问，
  用户认证配置改走 `SystemConfigService`；`StorageChain` 的媒体后缀改走 Chain snapshot。canonical 配置债务
  从 169/15 降到 164 个 settings import 文件/14 个 SystemConfigOper 构造点。
- Chain snapshot 继续覆盖超级用户、共享识别、辅助认证、全局图片缓存、自动下载用户和资源页链接；
  消息、识别、交互、推荐和用户链的 5 个直接 `settings` 导入被移除，当前低水位进一步降到
  161 个 settings import 文件，插件 SDK 与兼容入口未改。
- API snapshot 继续覆盖 API token、临时目录、识别共享与订阅模式；Chain snapshot 覆盖站点请求、
  代理、CookieCloud 黑名单和种子缓存配额。Agent/OpenAI/Anthropic/TMDB/种子缓存 API 以及 Site、
  Torrents Chain 共 7 个直接 `settings` 导入被移除，canonical 低水位降到 154 个文件；独立协议
  测试显式注入快照，不再依赖 endpoint 模块中的全局配置别名。
- 直接调用 endpoint 和显式构造 `ChainRuntimeContext` 的旧测试/兼容入口仍有 fallback；正式 FastAPI 与
  Startup 路径始终使用 HostRuntime 注入。插件 SDK 的 `app.sdk.config.settings`、动态 API 返回和事件字段未改。
- 收尾批次把 API 与 Chain 余下直接配置读取全部迁入类型化 snapshot；Scheduler 继续保持为零。
  `HostRuntime` 新增可变部署设置服务，只供系统设置管理 API 使用，业务 API/Chain 只接收 frozen 字段。
  snapshot 构造集中到 `app/startup/configuration.py`，生产启动与测试组合根复用同一映射，避免测试默认值
  漂移。canonical `settings` 直接导入低水位从 154 降到 137，`SystemConfigOper()` 保持 14 个。
- `ApiRuntimeConfig` 已覆盖搜索来源、媒体/字幕/音频后缀、重命名格式、WebPush、CookieCloud、根目录和
  版本标识；`ChainRuntimeConfig` 覆盖搜索、下载、整理、刮削、AI、代理、缓存、链接、路径和 TMDB 图片域。
  元数据缓存 TTL 使用动态 provider，在保留热更新语义的同时不再让 Chain 导入全局 settings。

### 阶段 4：把动态模块和事件变成可演进契约

#### ARCH-240：Module Contract V2

**目标**：兼容字符串分发，但让宿主高频能力具备签名、结果、并发和错误语义。

**建议契约字段**：

```python
ModuleMethodSpec(
    name="search_music",
    family="music",
    version=1,
    input_contract="SearchMusicRequest",
    result_contract="list[MediaInfo]",
    aggregation="ordered_list_merge",
    plugin_short_circuit=False,
    execution="sync_or_async",
    timeout_policy="caller_budget",
    error_policy="isolate_provider",
    public_to_plugins=True,
)
```

**实施步骤**：

1. 先选择 20 个高价值能力：识别、搜索、下载、存储、消息发送、媒体服务器查询。
2. 冻结当前参数、返回、排序、空值、异常、插件优先和聚合行为。
3. 扩展 `ModuleMethodContract`，不能只记录 family。
4. Module/Plugin 注册时检查 callable 和基础签名；第一阶段对旧插件不匹配只诊断，不拒绝加载。
5. 宿主 Module 和新插件 SDK 提供 Protocol/DTO；`run_module()` 继续作为兼容执行器。
6. 当一个能力全部宿主实现和官方插件均通过后，再把不匹配升级为宿主硬错误、第三方插件可读错误。
7. 未登记的第三方自定义方法继续走 legacy，不得删除开放扩展能力。

**量化目标**：legacy 方法数从 96 开始只降不升；新增宿主调用必须先有显式 spec。

**实施记录（2026-08-21）**：

- `ModuleMethodContract` 已升级为 V2，显式记录 version、input/result contract、aggregation、
  execution、timeout、error、plugin visibility 与基础签名要求；首批 22 个识别、搜索、媒体服务器、
  存储、消息和调度/集成能力完成登记。
- `run_module()` 与插件优先、短路、列表顺序合并、空值和异常隔离算法保持不变。Dispatcher 在真实
  provider 调用边界执行基础签名诊断；旧插件不匹配只写可读 warning，不拒绝加载或执行，未知自定义
  方法继续使用 legacy contract。
- runtime contract baseline 现包含稳定的 `module_method_specs`，后续字段或显式方法变化必须审查；
  `ModuleCapability` Protocol 为宿主和新插件提供静态声明入口，但不替换字符串 dispatcher ABI。
- 22 个显式方法进一步登记宿主真实传入的 required parameter 名称，覆盖识别、搜索、媒体服务器、存储、
  消息收尾、命令注册和 webhook；dispatcher 仍只输出诊断 warning，不阻断缺少参数的旧插件或未知自定义方法。
- 契约清单现覆盖静态扫描到的 211 个宿主字符串调用，并保留一个暂未被宿主调用的 `send_message` 公开能力，
  共 212 个显式 V2 spec。原先仅按 prefix 分类或落入默认 legacy 的宿主方法均获得稳定 family、输入合同、
  结果合同、执行、超时和错误语义；未知第三方自定义方法仍走开放 legacy fallback，不拒绝加载或执行。

#### ARCH-241：Event Contract Registry

**目标**：为每个 EventType/ChainEventType 明确 payload、可见范围、投递和可靠性，不改变旧装饰器 API。

**建议字段**：

- event type；
- payload model 或 legacy dict；
- broadcast / chain；
- host-only / plugin-public / target-plugin；
- sync/async handler 规则；
- ordering/priority；
- delivery：ephemeral / durable-required；
- error：isolate / stop-chain / notify；
- sensitive fields；
- producer/consumer owner。

**实施步骤**：

1. 先登记已有 53 个事件，不要求同时补齐 53 个 model；legacy 项必须显式标记原因。
2. 首批类型化配置、订阅、整理、下载、插件生命周期和 Agent usage 事件。
3. 发送边界接受旧 dict，并转换/校验；插件收到的 dict 形状保持。
4. 基线比较事件与 payload spec，不比较行号。
5. 报告“宿主无 consumer”时区分插件公开事件、预留事件和真正死事件。
6. `SystemError` 递归保护继续保留并补 contract；错误通知不能再次构造无限错误链。

**实施记录（2026-08-21）**：

- 新增 `app/runtime/event/contracts.py`，53 个 `EventType` / `ChainEventType` 全量登记 payload、
  broadcast/chain、可见性、顺序、错误策略、敏感字段和 ephemeral/durable-required 语义；尚未模型化的
  事件显式记录 legacy dict 原因，不把“未登记”当成兼容策略。
- 首批 20 个已有 Pydantic payload 的配置、订阅、整理、资源、认证、插件和 Agent 事件绑定具体 model。
  `Event` 创建边界对 dict/model 做诊断校验，但继续投递原对象，因此插件 dict 形状和链式原地修改语义不变。
- 订阅变更、下载添加、整理成功/失败等用户副作用标记为 `durable_required`，只表达完成语义要求；
  在 ARCH-251 pilot 完成前不虚构当前已具备持久投递。SystemError 仍沿用既有递归保护和异常通知路径。
- runtime contract baseline 新增稳定 `event_specs`，后续 enum 新增必须同步登记，且不比较源码行号。
- 53 个事件现已全部绑定 typed payload，原有 28 个 `legacy_dict` 项归零。插件动作/触发等开放事件使用
  “公共字段类型化 + `extra=allow`”模型，Webhook 与 Workflow execution 复用既有 DTO；验证仍只诊断并投递
  同一个原始 dict/model，因此插件字段、对象引用和链式原地修改语义未改变。

#### ARCH-242：Module/Integration 质量清单

**目标**：借鉴 Home Assistant Integration Quality Scale，为 `app/modules` 建立可检查但渐进的质量视图。

**建议规则**：

- 有 fake client 或录制 fixture；
- 零真实网络测试；
- sync/async 边界明确；
- 阻塞 I/O 不进入事件循环；
- 鉴权失效、限流、超时和离线语义明确；
- 并发上限/轮询间隔明确；
- 配置重载与 stop 可重复；
- Module Contract V2 覆盖；
- 敏感日志脱敏；
- 维护 owner 和豁免原因。

质量清单只约束新模块和被修改模块；历史模块以 `legacy`/`exempt + reason` 进入，不允许一次性阻断全部功能。

**实施记录（2026-08-21）**：

- `app/runtime/extensions/module/quality.py` 提供十项统一规则、`legacy/assessed` 等级、owner、已验证
  规则和精确豁免原因；所有存量模块均能生成有 owner/原因的 legacy 视图，不一次性阻断。
- 本轮修改的 `bangumi` 首个进入 assessed：fake client、零真实网络、sync/async 边界、reload/stop、
  Contract V2、敏感日志和 owner 已登记；限流/并发继续复用通用 HTTP adapter 并明确豁免范围。
- 详细规则和验收证据见 `docs/refactor/module-quality-scale.md`；自动测试阻止 profile 使用未登记规则，
  并要求今后修改模块时将对应 profile 纳入同一提交。

**收口记录（2026-08-22）**：39 个宿主 Module 已全部显式进入 assessed，不再以通用 fallback 把
37 个模块标成“尚未审查”。所有模块共同由零真实网络、async 阻塞扫描、Module Contract V2 和 owner
四项机器门禁覆盖；能力专属的鉴权、限流、并发、敏感日志与 reload/stop 仍按 profile 精确豁免，
不会把 assessed 误读为十项满分。未知第三方模块继续使用 legacy 兼容视图，Module ABI 未变。

### 阶段 5：定义后台可靠性，不先引入分布式队列

#### ARCH-250：后台动作可靠性分类 ADR

**目标**：先决定哪些动作允许丢失，哪些必须恢复，再选择实现。

分类建议：

| 等级 | 例子 | 允许语义 |
| --- | --- | --- |
| E0 即时通知 | UI 进度、缓存刷新提示、非关键统计 | 进程内、允许丢失、错误记录 |
| E1 可重建任务 | 推荐缓存、站点数据刷新、市场刷新 | 幂等、定时重建、有限重试 |
| E2 用户动作后置副作用 | 订阅变更事件、下载提交后的历史/通知 | commit 后必须可重放或明确补偿 |
| E3 数据完成状态 | 文件整理、迁移、备份、恢复 | 持久任务记录、幂等步骤、崩溃恢复 |

ADR 必须逐个映射当前 Event、BackgroundTasks、Scheduler job、Agent task 和 transfer pending，不允许笼统写“全部可靠”。

**实施记录（2026-08-21）**：

- `docs/adr/0007-background-action-reliability.md` 已接受：逐项覆盖 53 个事件，并分别映射
  BackgroundTasks、Scheduler、Agent task 与 transfer pending 的 E0～E3、完成点、恢复、重试、
  幂等、关停和失败表达。
- ADR 明确区分“Registry 标记 durable-required”与“当前已经 durable”；ARCH-251 前仍如实保留
  commit 后进程崩溃窗口，不用日志或 BackgroundTasks 冒充交付保证。
- 首个 pilot 选择 `SubscribeAdded`，因为 ARCH-221 已有事务所有权与 post-commit 样板；文件整理
  继续保持 E3，不在本任务中被降格为普通事件重试。

#### ARCH-251：用现有数据库做首个 durable side-effect pilot

**前置**：ARCH-220/221 与 ARCH-241 完成。

**目标**：为一个 E2 用例消除“DB 已提交，但事件/上报尚未执行时进程崩溃”的窗口。

**实施要求**：

1. 单独 ADR 决定 outbox 表或复用现有任务表；需要 schema 时必须新增 Alembic 迁移。
2. 同一事务内写业务行和 outbox；提交后 dispatcher 执行。
3. 记录 event key、payload version、attempt、next retry、last error、created/completed time。
4. handler 必须按稳定 idempotency key 去重。
5. 失败采用有上限指数退避，最终进入可诊断 dead-letter 状态，不无限刷日志。
6. 插件事件 payload 仍按 V3 dict 发送；durability 是宿主内部实现，不改变 SDK。
7. 先选订阅写入或整理完成中的一个用例，不建立万能消息总线。

**实施记录（2026-08-21）**：

- 新增 `outboxmessage` 表与 Alembic revision `c7d9a1e4f2b6`。订阅新增行和
  `subscribe.added` version 1 intent 在同一 Session/UoW 中 stage/flush/commit；outbox 写失败会回滚
  订阅。降级会删除未投递 intent，执行前必须确认 pending/dead 均已处理或备份。
- event key 由订阅 ID、`media_source`、`media_id` 和 payload version 构成；即时事件 payload 同步
  暴露 `idempotency_key`。正常 post-commit 编排全部完成后收口 intent；进程在 commit 后崩溃或回调
  失败时，记录保持 pending，由恢复 dispatcher 重放。
- SQLAlchemy adapter 使用 attempt 条件更新和 lease 做原子 claim；dispatcher 最多 5 次指数退避，
  错误截断后持久化，最终进入 `dead`。30 秒 Scheduler job 每批恢复最多 20 条，批次 Session 始终关闭。
- pilot 只恢复 `SubscribeAdded` 事件；消息和外部统计仍执行旧 post-commit 编排，不能据此宣称所有订阅
  副作用均 durable。后续 topic 必须另做幂等 handler 与崩溃窗口测试。
- 66 个订阅/调度专项测试和 40 个数据库、迁移、Session/outbox 测试通过（1 个环境条件 skip）；
  fresh schema 先 create_all 再升级与重复迁移均保持幂等。

**扩展实施记录（2026-08-22）**：

- 宿主自有的 `SubscribeModified`、`SubscribeDeleted` 生产路径已扩展到同一 outbox：订阅行更新/删除与
  version 1 intent 使用同一 `AsyncSession`、UoW 和 commit；即时广播失败时 intent 保持 pending，恢复
  dispatcher 分别按 `subscribe.modified`、`subscribe.deleted` topic 重放。
- API、Agent 更新/删除工具以及按媒体身份批量删除均复用请求级或独占事务作用域。API 中保留的
  `event_published=False` 分支只服务测试替身和旧依赖注入，不是正式装配路径；正式 `HostRuntime`
  同时提供订阅 repository、history repository、transaction 与 outbox factory。
- `SubscribeAddedEventData`、`SubscribeModifiedEventData`、`SubscribeDeletedEventData` 已进入 Event Contract；
  对插件仍投递原有 dict 字段，只新增可选 `idempotency_key`，不把 Pydantic 实例传给插件。
- 保证边界只覆盖主仓可追踪的宿主生产者。运行时安装在 `app/plugins/**` 的第三方插件未被主仓改写；
  插件若自行直接发送同名事件，该发送仍由插件负责，无法与插件自己的数据库写入自动组成原子事务。
- 订阅外部统计上报仍是 post-commit 副作用，不在事件 intent 的重放 handler 中；因此当前可以宣称三种
  订阅事件具备宿主级 at-least-once 恢复，但不能宣称订阅通知和所有外部上报均已 durable。
- `DownloadAdded`、`TransferComplete`、`TransferFailed` 也已逐项接入，而不是复用一个不分业务语义的
  “万能消息总线”。下载历史、下载文件清单或整理历史与各自 intent 在独占同步 Session/UoW 中原子提交；
  即时广播失败时 intent 保持 pending，三种恢复 handler 均继续使用有限重试与 dead-letter 策略。
- 下载和整理事件保留插件原有运行时对象 ABI：即时发送仍含 `Context`、`FileItem`、`MetaInfo`、
  `MediaInfo`、`TransferInfo`；outbox 单独存 JSON 快照，恢复 handler 无远端调用地重建这些对象。
  `idempotency_key` 仍是唯一新增的可选公开字段，提醒插件按 at-least-once 语义自行去重。
- 本切片同时把 `DownloadChain.download_single` 的提交后通知/任务编排抽成独立方法，并删除已经被
  Application 删除命令替代的两个 `Subscribe` Model 级删除事务装饰器；Model decorator 基线从
  178 降到 176，Oper 内显式 commit/rollback 仍为 0。strict mypy 门禁新增 Chain durable context、
  payload 转换和启动适配器。
- 下载失败冷却切片继续迁移到 `TransactionalDownloadFailureRepository`：Chain 每次读写使用独立短会话，
  写成功由显式 `SqlAlchemyUnitOfWork` commit，异常 rollback；`DownloadFailure` 查询和记录方法不再拥有
  自动会话/提交装饰器。Model decorator 基线继续从 176 降到 174，Oper 内显式 commit/rollback 仍为 0。
- Workflow 执行状态切片新增 `WorkflowExecutionCommand` 与短会话事务适配器；运行中、动作进度、成功、
  失败和重置均由 Application command 显式 commit/rollback。`WorkflowOper()` 的旧方法名、参数和返回值
  继续可用，无 Session 调用委托组合根服务，显式 Session 调用只暂存；同步 Model 自动提交装饰器移除 6 个，
  事务低水位从 174 降到 168，Oper 仍不创建 Session、也不直接 commit/rollback。
- 剩余 45 个同步/异步 Model 写装饰器已全部迁移：AgentTask、PassKey、User、消息、历史清理、
  站点快照、媒体服务器、插件数据、TransferPending 等写入由调用方 Session 和 UoW 收口；无 Session
  的旧 Oper ABI 委托 Startup 注入的短事务执行器。当前 Model 装饰器仅剩 123 个查询装饰器，
  `db_update` 与 `async_db_update` 均为 0，Oper 自建 Session/直接提交仍为 0。
- 数据清理按批次显式提交 UoW，单表失败先回滚会话再继续汇总后续表；不再依赖删除 Model 的隐式提交。
- 收尾批次进一步移除宿主 Oper 对 `Base.create/update/delete/truncate` 八个兼容包装器的调用：显式
  Session 只 stage，由 Application UoW 提交；无 Session 的旧 Oper 入口才委托 Startup 的短事务执行器。
  Base 包装器继续保留给插件/旧模型 ABI，新增 AST 门禁禁止宿主 Oper 回退到隐式提交。

**禁止**：本阶段不引入 Celery、Kafka、RabbitMQ 等新基础设施。

#### ARCH-252：Scheduler 拆成声明、执行和状态

**目标**：缩小 `Scheduler.init()`，统一重入、并发、超时、取消和进度语义。

**建议结构**：

```text
app/application/scheduling/
  contract.py    # JobSpec / trigger / overlap / retry / timeout
  catalog.py     # 业务 job 声明
  execution.py   # 执行状态和幂等
app/scheduler.py # APScheduler 兼容 Facade
```

**步骤**：先把 job 定义数据化，再提执行状态；不在第一步替换 APScheduler。每个 job 必须声明 overlap policy、timeout、manual、recovery 和 owner。

**实施记录（2026-08-21）**：

- `app.application.scheduling` 新增 `JobSpec`、`JobCatalog`、`JobExecutionState` 以及 overlap/recovery 枚举；
  系统、媒体服务器、Agent、工作流、插件和 outbox 动态任务均由同一合同生成兼容运行状态。
- 保留 APScheduler 和既有 `Scheduler` Facade；重入判断、开始/结束/失败状态统一由 execution state 收敛，
  job 状态稳定暴露 owner、overlap、timeout、manual、recovery 五项策略。
- coroutine job 的非空 timeout 使用 `asyncio.wait_for`，超时会取消底层任务并记录明确终态；同步 job 默认
  `timeout=None`，避免用线程强杀制造不可控的半完成副作用。一次性 Agent 任务重启后保持 manual-only，durable
  outbox/备份/整理与 next-schedule 任务的恢复语义可审计。
- 61 个 Scheduler、Agent 定时任务、备份、进度和媒体服务器专项测试通过，覆盖重复 ID、overlap skip、
  timeout cancel、restart/manual recovery 与兼容状态字段。

### 阶段 6：可观测性、类型和复杂度预算

#### ARCH-260：统一 request/correlation ID

**目标**：一个请求进入后，API、Application、Chain、Module dispatcher、Event 和 `RequestUtils` 日志能够使用同一个关联 ID。

**实施步骤**：

1. 中间件接受合法 `X-Request-ID`，否则生成；限制长度和字符集，防止日志注入。
2. 使用 ContextVar 保存；线程池/异步 task 必须验证上下文传播，跨进程任务写入 payload。
3. 响应回写 `X-Request-ID`；SSE 在握手响应和错误事件中保持同一 ID。
4. 日志 formatter 增加结构字段，不在消息字符串中到处手拼。
5. 外部请求可传标准 trace headers 或项目 correlation header，但不得泄露用户 token。

**实施记录（2026-08-21）**：

- 新增受 64 字符安全字符集约束的 `moviepilot_correlation_id` ContextVar 和纯 ASGI middleware；合法
  `X-Request-ID` 原样使用，非法值重新生成，`request.state`、普通响应和 SSE 握手响应回写同一个 ID。
- 平台日志 formatter 以独立 `correlation_id` 字段输出；`app.runtime.execution`、共享 `ThreadHelper`、
  Event 生产/消费均显式复制或恢复上下文。Event 在生产时固化 ID，广播线程不能用自己的空上下文覆盖它。
- Scheduler 多进程入口把关联 ID 作为显式可序列化参数传入，不依赖 fork 继承；`RequestUtils` 和
  `AsyncRequestUtils` 在调用方未指定时传播 `X-Request-ID`，不读取或复制任何鉴权 token。
- 并发请求、非法头、线程池、事件处理、SSE、同步/异步外呼和显式外呼头覆盖均有专项测试；原 API
  响应、健康探针、日志和搜索流式测试保持通过。

#### ARCH-261：指标与可选 OpenTelemetry Adapter

先定义内部观测端口和低基数指标：

- HTTP route/status/latency；
- DB pool wait/checked-out/timeout；
- Event queue depth、handler latency/error；
- Module provider latency/error/timeout；
- Scheduler job duration、overlap skip、retry/dead-letter；
- Plugin load/reload/settling duration；
- Agent active task、cancel、provider latency、token usage。

OTel 初始化只能位于 Startup/Adapter；Domain/Application 只依赖 no-op-capable observation Protocol。插件 ID、用户 ID、媒体标题等高基数字段不得直接作为 metric label。

**实施记录（2026-08-21）**：

- `app.runtime.observability` 定义单一 `ObservationPort`、默认 no-op、指标类型/目录、标签白名单和统一耗时
  作用域；没有 exporter 时所有调用仍可执行，未登记标签在进入 Adapter 前直接拒绝。
- 指标目录覆盖 HTTP、DB pool、Event、Module、Scheduler、Plugin lifecycle 和 Agent 所列能力；标签审计
  明确禁止 user/plugin/media/request/job 实例 ID、标题和 URL。首批实际接线覆盖 HTTP route/status/latency、
  Event queue/handler、Module provider 与 Scheduler duration/overlap，剩余能力可按相同端口逐个接入。
- `app.adapters.observability.otel` 只在组合根显式读取 `MOVIEPILOT_OTEL_METRICS=1` 后懒加载 OTel API；
  未安装可选包时稳定回退 no-op，不给核心层增加 SDK 依赖。HTTP Adapter 通过路由匹配输出模板，绝不以原始
  request path 充当 label。
- 2026-08-22 扩展接线覆盖 SQLAlchemy checkout/checkin、异步回退配额 wait/timeout、Module 真实
  `TimeoutError`、插件 start/initialize/stop/reload，以及 Agent 活跃任务、取消结果、供应商耗时和输入/
  输出 token。自定义 Agent provider 统一归类为 `custom`，不会暴露配置名称。
- Outbox dispatcher 的有限重试和 dead-letter 已分别接入 `scheduler.job.retry` 与
  `scheduler.job.dead_letter`，只使用固定 `owner=outbox` 低基数标签；观测失败端口由 Startup 注入，
  Application 不依赖具体 OTel SDK。
- 专项测试覆盖 exporter 缺失、非法标签、全目录高基数审计、成功/失败 outcome、动态 URL 路由模板；
  既有 API、Event、Module、Scheduler 与健康探针回归保持通过。

#### ARCH-270：渐进式类型门禁

**目标**：不要求全仓一次通过 mypy/pyright；只保证新 canonical contract 和被治理模块完整类型化。

**实施步骤**：

1. 选定一个类型检查器并写入 dev dependency/lock；不要同时引入两套。
2. 首批严格目录：
   - `app/domain/`
   - 新增 Application command/port
   - `app/runtime/event/`
   - `app/runtime/extensions/module/contracts.py`
   - `app/startup/ports/context.py` / `app/api/context.py`
3. 对第三方移植包、旧插件 Facade 和动态 SDK 设置精确豁免，不允许 `app.* = ignore_errors`。
4. CI 先检查严格目录；每次迁移扩大 include 范围。
5. 类型错误不能用无界 `Any`、`cast(Any, ...)` 或全文件 ignore 消音。

**实施记录（2026-08-21）**：

- 选定 mypy 1.18.x 并写入 `pyproject.toml`/`uv.lock`，仓库只保留这一套新增类型门禁；CI architecture job
  使用锁定环境运行 `mypy --config-file mypy.ini`。
- 首批 strict 清单包含 4 个已满足合同的 Domain value 文件、correlation/observation、Event Contract V2、
  Module Contract V2、typed HostRuntime startup context 和 API context，共 10 个 canonical 文件。
- `mypy.ini` 不包含 `app.* = ignore_errors`、全文件 ignore、无界 `Any` 或 `cast(Any, ...)` 消音；历史
  `domain/meta` 动态模型只有在逐文件修正后才可加入清单，当前错误不能被 baseline 当作“已通过”。
- 配置约束测试会检查 strict、关键合同文件和至少一个 Domain 文件均在清单中，并实际启动锁定版本 mypy；
  当前 10 个源文件零错误通过。

**扩展实施记录（2026-08-22）**：mypy 目标运行时更新到 Python 3.14，严格清单扩大到 20 个源文件；
新增纳管配置快照和下载失败事务适配器，仍保持零错误、无全局 ignore。

Workflow 执行状态 UoW 切片将 `app/application/workflow.py` 与 `app/startup/ports/workflow.py` 纳入 strict 清单，
治理范围扩大到 22 个源文件；事务命令、仓储 Protocol 和短会话适配器保持零错误。

异步安全与契约收口继续纳管 scheduling facade、Event error policy、Module dispatcher 和 async blocking
scanner，strict 清单扩大到 26 个源文件；已登记范围保持零错误，未使用全文件 ignore 或 `cast(Any, ...)`。

收尾批次继续纳管 Startup 配置快照、Module quality、Compat manifest/diagnostics、插件运行时窄端口、
Outbox adapter、DB 装饰器、Base 与 UoW，strict 清单扩大到 37 个源文件并保持零错误。

#### ARCH-271：复杂度和端点预算 ratchet

**目标**：阻止大方法继续增长，并让拆分对应真实阶段，而不是机械 helper 化。

**初始规则**：

- 新 API endpoint 原则上 ≤ 80 行；
- 新 Application command/query 方法原则上 ≤ 150 行；
- 新 Chain public use-case 方法原则上 ≤ 150 行；
- 既有超限方法进入 baseline，只允许不增；
- 修改超限方法时，PR 必须列出阶段划分、共享状态和回归测试。

优先拆分对象：`do_transfer`、`batch_download`、`SubscribeChain.match`、`web_agent_stream`、`Scheduler.init`。先提取 phase object/DTO/port，再缩短入口；不创建一批互相读写同一个大 dict 的私有函数来“达标”。

**实施记录（2026-08-21）**：

- 新增 `scripts/architecture/complexity.py`，通过 AST 只统计 API HTTP endpoint、Application public method
  和 Chain public use-case，预算分别为 80/150/150 行；嵌套 helper 不会被机械重复计数。
- `complexity-baseline.json` 只保存当前超限入口和行数，不把达标方法写成永久快照。check 允许缩短、达标或删除，
  精确拒绝既有超限增长和任何新增超限；CI architecture job 每次执行。
- 当前债务清单明确包含 `web_agent_stream`、`batch_download`、`SubscribeChain.match`、`do_transfer`；
  `Scheduler.init` 已在 ARCH-252 通过 JobSpec/catalog 拆分退出超限清单，调度专项测试是该代表性拆分的回归证据。
- 单元测试覆盖删除/缩短放行和增长/新增拒绝，当前仓库 baseline check 通过。
- 2026-08-22 将 MCP JSON-RPC 分派、无媒体信息下载识别、缺集结果合并拆成具有独立输入/输出的私有阶段；
  对应 `mcp_jsonrpc`、`download.add`、`DownloadChain.get_no_exists_info` 退出超限清单，总债务从 28 降到 25。
- 2026-08-22 将 `SiteChain.sync_cookies` 拆为单域名处理、黑名单判断、索引器地址解析和连接重试阶段，入口降至预算内；
  保留已有站点健康、黑名单、失败重试时的事件与进度回调语义，站点专项测试通过。
- 继续将 `TorrentsChain.refresh` 拆为单站点抓取、上下文构造和缓存写入阶段，入口退出超限清单；
  音乐双缓存、去重、停止信号和订阅匹配专项测试通过，当前复杂度债务由 25 项降至 21 项。

配置债务继续按模块族收敛：`app/application/image.py` 的壁纸模式、图片缓存、代理和安全后缀读取已接入
`ChainRuntimeConfig`，canonical `settings` 直接读取文件数从 137 降至 136；配置/依赖基线已更新，壁纸与图片专项测试通过。
随后将 `app/application/torrent.py` 的代理和媒体后缀读取迁移到同一快照，canonical 配置债务进一步降至 134 个文件；
下载/种子专项测试与架构门禁通过。
`app/application/rss.py` 的代理和编码检测选项也已迁移到快照，配置债务降至 133 个文件；RSS、Rust 解析和音乐资源专项测试通过。
数据维护策略随后接入同一快照，`app/application/maintenance.py` 的直接配置读取移除，债务降至 132 个文件；
清理服务与 Chain 专项测试通过。
Passkey 的 APP_DOMAIN、NGINX_PORT 和用户验证要求也已接入 API 配置快照，配置债务降至 131 个文件；
MFA/Passkey 专项测试与架构门禁通过，密钥类配置仍保留在安全端口范围内。
认证服务的超级用户、向导开关和访问令牌过期时间也改用配置快照，债务降至 130 个文件；
鉴权与 MFA 专项测试通过。
`DownloadChain.download_single` 的下载成功结算已提取为独立阶段，入口从 255 行降至 167 行；
历史、文件明细、durable intent、post-commit 通知和旧测试 fallback 语义保持，下载专项测试通过。
`SubscribeChain.add/async_add` 的同步/异步重复编排随后收口到显式创建上下文和阶段方法：输入规范化、媒体识别、
电视剧集数准备、默认字段/图片处理、事务提交和失败反馈分别拥有明确边界；订阅重复检测、owner scope、
`SubscribeAdded` payload、outbox stage/commit/post-commit 顺序仍由既有 `application/subscription/write.py` 负责。
两个公开入口均降至 150 行预算内，复杂度基线移除对应债务项；订阅识别、音乐订阅、写入事务和搜索来源专项
共 280 项测试通过，架构、复杂度与异步阻塞门禁通过。
随后将 `TransferChain.do_transfer` 的公开入口收口为稳定兼容 Facade，先提取媒体身份规范化阶段，保留显式
`media_source/media_id` 校验、识别失败文案和所有原有调用参数；整理专项 80 项测试通过，复杂度基线移除该入口，
后续继续拆分其批次规划与执行阶段。
2026-08-22 继续完成入口垂直切片：`DownloadChain.download_single`、`SubscribeChain.search` 和
`SubscribeChain.match` 均改为稳定兼容 Facade，分别委托下载执行、搜索执行、资源预处理和订阅匹配阶段；
保留原参数、对象类型、锁、进度回调、停止信号、候选过滤、失败冷却日志和下载结算语义。
`MediaServerChain.sync` 补回停止信号后的立即退出，避免系统停止后继续发送服务器/全局完成进度。
下载、订阅、媒体服务器及 durable/outbox 专项共 370 项测试通过，复杂度基线移除上述三个订阅/下载入口。
当前仍不把普通用户通知和 MoviePilot Server 外部统计标记为 durable：它们尚未与业务写入和 outbox intent
绑定在同一事务，继续保持 post-commit 的准确边界。

#### ARCH-272：异步阻塞检测

**目标**：对新 API/Agent/Application async 路径检测 `open`、文件遍历、同步 HTTP、阻塞 sleep 和重 CPU 解析。

**步骤**：

1. 开发/测试启用 asyncio debug 和慢 callback 诊断；
2. 为已知同步 I/O adapter 提供统一 `run_in_threadpool` 入口；
3. 添加针对改动模块的阻塞调用测试/AST 规则；
4. 同步 Module 由 dispatcher 线程池兼容，不要求第三方插件立刻 async 化；
5. 只在测量证明有收益时改用 async 第三方 client。

**实施记录（2026-08-21）**：

- `scripts/architecture/async_blocking.py` 扫描 canonical 主程序目录和顶层运行入口中的 async 函数，覆盖
  同步 HTTP、Oper、Path、`shutil`、`subprocess`、`os`、`time.sleep` 与 `open`。
- scanner 按 import 来源、局部别名、互斥分支和嵌套函数定义点解析符号；`AsyncRequestUtils`、
  `anyio.Path`、延迟回调及受控 worker 内执行的同步函数不记为 async 直接阻塞。函数和 lambda 的默认值、
  decorator 等定义时表达式仍在所在 async 执行体中检查。
- baseline 只允许调用减少或删除，新增调用及次数增长均使 CI architecture job 失败；当前记录 10 条已确认
  存量，包括 8 条文件元数据访问、1 条目录删除和 1 条同步 Oper 读取，由后续数据库与文件 adapter 叶迁移。
- pytest 全局启用 `asyncio_debug`，专项测试验证实际 loop debug 状态；AST ratchet 与 46 个 Agent 流式回归
  通过。同步第三方 Module 仍由 dispatcher 的 `app.runtime.execution.run_in_threadpool` 兼容。

## 6. 推荐执行队列

下表是默认的提交顺序，不表示所有任务必须由同一个 AI 连续完成。一个 AI 一次只领取一行；如果发现前置条件未满足，应停止实施并回报证据，不得顺手扩大范围。

| 顺序 | 任务 | 前置 | 主要产物 | 风险 | 最小验证 |
| ---: | --- | --- | --- | --- | --- |
| 1 | ARCH-201 基线 CLI 分域 | 无 | host/plugin/performance 的独立 check/write | 低 | CLI 测试 + 工作树不变断言 |
| 2 | ARCH-202 语义与位置分离 | ARCH-201 | 稳定语义 fixture + 诊断报告 | 低 | 架构 fixture 精确 diff |
| 3 | ARCH-203 CI 分层 | ARCH-201 | PR 快门禁、跨仓观察 job | 低 | 本地复现 workflow 命令 |
| 4 | ARCH-210 单 worker 约束 | 无 | 配置校验、启动错误文案、部署文档 | 中 | worker=1/2 启动测试 |
| 5 | ARCH-211 factory/reload | ARCH-210 | import-string/factory 入口 | 中 | 实际启动、reload smoke、信号关停 |
| 6 | ARCH-212 DB 准备与健康 | ARCH-211 | 唯一 DB prepare 入口、live/ready | 中 | 迁移失败/DB 断开/安全模式测试 |
| 7 | ARCH-220 事务 ratchet | 无 | 禁止新 Model 自提交的门禁 | 中 | DB decorator 与 Session 生命周期测试 |
| 8 | ARCH-221 订阅完整切片 | ARCH-220 | 订阅 command + UoW + post-commit | 高 | SQLite/PostgreSQL 语义测试、事件次数 |
| 9 | ARCH-222 其余写切片 | ARCH-221 | 迁移批次，不是一次全仓改写 | 高 | 每个业务切片独立回归 |
| 10 | ARCH-230 Typed HostRuntime | ARCH-211 | typed state、兼容 provider | 高 | 生命周期顺序、重复启动/关停测试 |
| 11 | ARCH-231 API 依赖拆分 | ARCH-230 | 领域 dependency/presentation | 中 | OpenAPI 快照、鉴权、SSE/流式响应 |
| 12 | ARCH-232 配置快照/端口 | ARCH-230 | 窄配置对象与 reload 订阅 | 中 | reload 前后行为、敏感配置测试 |
| 13 | ARCH-240 Module Contract V2 | ARCH-201 | 高频方法签名/结果/错误协议 | 高 | 宿主 + 官方插件 dispatcher 测试 |
| 14 | ARCH-241 Event Registry | ARCH-201 | EventType 到 payload/reliability 映射 | 高 | producer/consumer 静态和运行测试 |
| 15 | ARCH-242 Module 质量清单 | ARCH-240 | 能力族分级与豁免清单 | 中 | 选定模块族验收 |
| 16 | ARCH-250 可靠性 ADR | ARCH-241 | E0-E3 分类和完成语义 | 低 | 文档评审 + 现状映射无遗漏 |
| 17 | ARCH-251 durable pilot | ARCH-221、250 | outbox/job 表、claim/retry/幂等 | 高 | 崩溃窗口、重复投递、并发 claim |
| 18 | ARCH-252 Scheduler 分层 | ARCH-230、250 | JobSpec/catalog/execution state | 高 | overlap/timeout/restart/manual |
| 19 | ARCH-260 correlation ID | ARCH-230 | ContextVar、中间件、传播 | 中 | 并发请求、线程池、SSE、外部请求 |
| 20 | ARCH-261 Metrics/OTel | ARCH-260 | 低基数指标、可选 adapter | 中 | exporter 缺失时 no-op、label 审计 |
| 21 | ARCH-270 类型门禁 | ARCH-230、240、241 | 严格目录和精确豁免 | 中 | 选定 type checker |
| 22 | ARCH-271 复杂度 ratchet | ARCH-201 | 只降不增的规模基线 | 低 | AST 门禁 + 代表性拆分测试 |
| 23 | ARCH-272 async 阻塞检测 | ARCH-203 | debug/AST/专项测试 | 中 | API/Agent/Application 新改动路径 |

可并行关系：ARCH-210 与 ARCH-220 可并行；ARCH-240 与 ARCH-230 可在接口冻结后并行；ARCH-260 可在 typed state 稳定后独立进行。不可并行关系：ARCH-221 与同一订阅写路径上的其他重构、ARCH-230 与生命周期大改、ARCH-251 与目标副作用的业务修改。

## 7. 给实施 AI 的任务卡模板

领取任务时先复制并填写下面模板。`allowed_paths` 不是提示，而是本次改动白名单；需要越界时先停下说明原因。

```yaml
task_id: ARCH-xxx
objective: 一句话描述用户可见或架构可验证的结果
baseline_commit: 实施开始时的 git rev-parse HEAD
must_read:
  - AGENTS.md
  - docs/rules/04-design-patterns.md
  - docs/rules/05-architecture.md
  - docs/testing.md
  - docs/refactor/backend-architecture-next-stage.md#对应任务
allowed_paths:
  - app/...
  - tests/...
  - docs/...
forbidden_scope:
  - 第三方插件 ABI 删除或改名
  - 无关 schema、前端协议或插件仓修改
contracts_to_preserve:
  - REST 路径、状态码、响应结构
  - 动态插件 API 原生返回结构
  - SDK/compat manifest 中已发布符号
  - 启停顺序和安全模式语义
preflight:
  - git status --short
  - git branch --show-current
  - git rev-list --left-right --count HEAD...@{upstream}
evidence_to_collect:
  - 真实调用链和所有入口
  - 修改前失败/缺口测试
  - 兼容消费者和回滚点
acceptance:
  - 新增或更新的专项测试通过
  - 架构门禁通过
  - 工作树仅包含白名单文件
rollback:
  - 描述代码、配置、迁移各自如何恢复
```

任务卡还必须回答四个问题：

1. **完成点在哪里？**例如订阅写入的完成是 DB commit，还是事件处理成功；不能只写“接口返回成功”。
2. **谁拥有资源？**Session、task、thread、client、plugin instance 由谁创建、谁关闭、失败时谁回收。
3. **兼容边界是什么？**宿主内部可以改，插件 SDK/Compat、动态 API 和持久数据不能被无意改变。
4. **如何证明没有扩大范围？**列出路径 diff、测试命令和未执行的验证，不用“应该没问题”代替证据。

## 8. AI 标准执行循环

### 8.1 开始前

1. 确认仓库是 `MoviePilot`、分支是 `v3`，记录 `HEAD`、上游差异和现有工作树；不清理、不 stash、不覆盖用户改动。
2. 阅读任务映射到的规则文件；若涉及插件兼容，再读 `docs/refactor/backend-module-refactor-compatibility.md`。
3. 使用 `rg` 从所有入口追踪到实现、持久化、事件和外部调用；不能只查看报错文件或同名类。
4. 先运行最小现状测试。基线本来失败时，记录精确失败并区分“当前已存在”和“本任务引入”。
5. 对照本任务的前置任务；未满足时只做调查，不伪造兼容层绕过。

### 8.2 实施中

1. 先写或更新契约测试，再做最小实现；新增类和方法按仓库规则补类级、方法级注释。
2. 每个提交只解决一个 Task ID。数据迁移、行为迁移和删除兼容入口至少拆成不同提交。
3. 新路径先双轨兼容并增加计数/日志，再迁移调用方，最后在门禁证明无消费者后删除旧路径。
4. 事务任务必须显式测试：成功提交、任一步失败回滚、重复调用、并发冲突和 post-commit 副作用。
5. 生命周期任务必须显式测试：部分启动失败、重复 shutdown、取消传播和资源最终释放。
6. 不运行无 scope 的 baseline write；fixture 变化必须能从语义 diff 解释，不能因为测试红就刷新。
7. 不为通过行数门禁机械切私有函数；拆分后的对象必须拥有独立输入、输出、错误和测试边界。

### 8.3 完成前

1. 先跑目标模块专项测试，再跑架构/兼容门禁；高风险任务最后运行仓库全量门禁。
2. 检查 `git diff --check`、`git status --short` 和逐文件 diff，确认没有生成物、秘密或无关格式化。
3. 若 baseline 变化，逐字段说明原因；若涉及插件仓，分别报告宿主门禁与跨仓观察结果。
4. 汇报必须包含：结果、修改路径、行为变化、兼容性、验证命令和结果、未验证项、风险/回滚。
5. 未通过高风险验证时不得宣称完成，也不得用“仅环境问题”笼统归因。

## 9. 验证矩阵

以下命令是最低集合；实施 AI 应先用 `rg --files tests` 确认文件仍存在。新增任务测试名可以调整，但必须覆盖表中的行为。

| 变更类型 | 必须验证 | 重点故障注入 |
| --- | --- | --- |
| 架构规则/基线 | `tests/test_architecture_dependencies.py`、`tests/test_architecture_contract_baseline.py`、新 CLI 测试 | fixture 缺失、只读误写、插件仓不存在/漂移 |
| 启动/worker/factory | 新增 factory、worker 与 lifespan 测试；真实 Uvicorn smoke | workers=2、reload、端口占用、初始化中断、二次 shutdown |
| DB 事务/Repository | `tests/test_db_session_lifecycle.py`、`tests/test_db_decorator_error_paths.py`、目标 Oper/用例测试 | 中途异常、commit 异常、重复请求、并发更新、事件失败 |
| 订阅切片 | `tests/test_subscription_query_service.py`、`tests/test_subscribe_modified_event.py` 与新增 command 测试 | 唯一键冲突、回滚后无事件、commit 后只发一次 |
| Runtime/AppState | `tests/test_module_lifecycle.py`、启动专项测试 | 缺失依赖、部分初始化、重复关停、safe mode |
| Module 契约 | `tests/test_module_invocation_dispatcher.py`、`tests/test_module_method_contracts.py` | legacy provider、同步/异步、timeout、坏返回值、第三方异常 |
| Event 契约 | `tests/test_event_dispatch_snapshot.py`、`tests/test_event_runtime_components.py`、`tests/test_event_plugin_errors.py` | 非法 payload、handler 超时、插件异常、关停 drain |
| Scheduler/可靠任务 | scheduler 现有测试 + 新 execution/outbox 测试 | 进程在 commit 后崩溃、重复 claim、overlap、超时、重启恢复 |
| request ID/观测 | 新中间件和传播测试、`tests/test_async_request_utils.py` | 并发隔离、非法 header、线程池、SSE、外部 client 异常 |
| async 安全 | asyncio debug、目标 API/Agent/Application 测试 | 同步文件/HTTP/sleep、取消、慢 callback |

通用架构门禁：

```bash
./.venv/bin/python -m pytest \
  tests/test_architecture_dependencies.py \
  tests/test_architecture_contract_baseline.py -q
```

高风险 Python 改动的最终门禁以 `docs/testing.md` 为准，通常应在仓库目录运行：

```bash
./.venv/bin/python tests/run.py
```

如果本机的编译站点资源导致进程以 `137`/`SIGKILL` 退出，应按项目既有测试隔离方案使用 `_SitesHelperStub`，并明确记录退出码和隔离方式；不得将进程被杀描述成断言失败或测试通过。

## 10. 可量化验收目标

指标用于证明方向，不用于鼓励刷数字。达到一项必须同时保留业务和插件兼容测试。

| 领域 | 当前基线 | 二阶段目标 |
| --- | ---: | --- |
| 宿主架构 SCC | 1 个隔离 TMDB SCC | 不新增；隔离包继续豁免，不强拆 |
| 重点禁止依赖边 | 0 | 持续为 0 |
| 基线写入行为 | 默认命令可能覆盖 fixture | 所有默认/check 命令保证工作树不变；write 必须显式 scope |
| 全功能 worker | 配置允许 >1，控制面会复制 | 启动期明确拒绝 >1；文档与配置一致 |
| 健康接口 | 认证 `/system/ping` 为主 | 分离公开 live 与受限/安全 ready；失败原因可诊断 |
| Model 事务装饰器 | 当前 123 个且全部只读；写装饰器 0 | 查询债务只降不增；写事务不回退到 Model/Base 隐式提交 |
| 新写用例事务 | 宿主写 Oper 已脱离 Base 隐式提交 | 100% 由入口/Application 边界拥有 Session/UoW |
| 高频 Module 契约 | 212 个宿主能力显式登记 | 新观察到的宿主方法必须同步登记完整契约 |
| Event payload | 53 类型全部登记 typed payload 与可靠性 | 新事件必须同步登记，不回退裸 dict |
| 超长新端点/用例 | 无增量门禁 | 新代码不越预算；旧 baseline 只降不增 |
| Request 关联 | 无统一 ID | HTTP → Application → Module/Event/外部请求可关联 |
| 关键后台副作用 | commit 后存在崩溃窗口 | 选定 pilot 可恢复、幂等、可查询失败和重试次数 |
| 类型门禁 | 无渐进严格目录 | 新 contract、typed state、event/module contract 进入 CI |

## 11. 风险与回滚策略

| 风险 | 预防 | 回滚触发 | 回滚方式 |
| --- | --- | --- | --- |
| 单 worker 校验阻断既有部署 | 启动错误列出原因和替代配置；先发弃用告警再硬拒绝 | 已有用户无法按单 worker 启动 | 临时恢复告警模式；不声称多 worker 已安全 |
| factory 改造改变导入副作用 | 分离 `create_app` 与 DB prepare；真实进程 smoke | reload、CLI 或容器入口失败 | 恢复原入口，保留已通过的纯函数拆分 |
| 事务迁移改变提交时机 | 一个垂直切片、双数据库语义测试、事件次数断言 | 重复事件、部分写入或锁冲突上升 | 切回旧 facade；schema 若未变无需数据回滚 |
| Typed runtime 破坏插件启动 | 旧 provider 作为兼容门面；SDK/manifest 快照 | 官方/第三方插件找不到宿主服务 | 切回旧 provider 读取，保留 typed 对象但不强制 |
| Module Contract V2 误拒绝 legacy | adapter 做输入归一和返回验证；按 method 逐个开启 | 合法旧插件调用被拒绝 | 对该 method 关闭严格模式，不删除 spec/观测 |
| Event model 阻断插件自定义数据 | 区分宿主 strict 与插件 opaque/extension payload | 插件事件无法投递 | 对该事件恢复兼容解析并记录未知字段 |
| Durable pilot 重复执行 | 幂等键、原子 claim、lease 超时和执行记录 | 同一副作用多次发生 | 停止 worker，保留表和待处理记录，切回人工恢复流程 |
| 指标造成高基数或泄密 | label 白名单、值截断/散列、敏感字段测试 | 指标存储暴涨或出现用户数据 | 关闭 exporter；no-op adapter 保持业务可运行 |

涉及 Alembic 的任务必须遵守扩展—迁移—收缩：先增加兼容 schema，再部署双读/双写或回填，最后在确认回滚窗口关闭后删旧字段。任何不可逆 downgrade 都要单独说明数据损失，不能把代码回滚等同于数据库回滚。

## 12. 明确禁止的“改进”

- 不创建新的 `app/common`、`app/shared`、`app/utils` 万能目录；无法说明所有者的代码不应迁入公共层。
- 不引入通用 DI 容器来隐藏依赖图；Startup 显式装配和窄 Protocol 已足够。
- 不把所有同步代码改成 async，也不在没有压测证据时更换数据库驱动。
- 不在控制面拆分前开启多 worker；也不通过文件锁“临时保证”所有后台组件只启动一次。
- 不把进程内 Event 全部升级为消息队列；先按 E0-E3 业务完成语义分类。
- 不让 Repository/Model 发布业务事件；事件由完成用例的 Application/Chain 在正确提交点触发。
- 不把动态插件 API 强制包成宿主 `{success, message, data}` 响应。
- 不删除 SDK/Compat symbol、旧模块方法或旧事件字段来换取类型整洁；必须先有消费者证据和弃用期。
- 不编辑 `app/plugins/**` 运行时副本来代表官方插件修复；插件源码应在 `MoviePilot-Plugins` 独立仓处理。
- 不因行号、commit hash 或采样耗时变化直接刷新 fixture；必须先解释语义 diff。
- 不用全局 `Any`、全文件 ignore、吞异常或无界重试来通过门禁。
- 不将用户 ID、媒体名、URL、插件配置等敏感或高基数字段作为 metric label。

## 13. 整体完成定义

本方案只有同时满足以下条件才算完成，而不是“23 个任务都有提交”即完成：

1. 宿主硬门禁与跨仓观察门禁可独立运行，所有只读检查不会修改 fixture。
2. 文档、启动校验和实际 Uvicorn 进程职责一致；全功能 V3 不会悄悄复制控制面。
3. 至少订阅写入完成一个端到端事务样板，证明请求、CLI/Job 可复用同一用例且失败原子回滚。
4. typed runtime、Module Contract V2、Event Registry 均保留现有插件 ABI，并由官方插件仓快照验证。
5. 至少一个用户数据相关副作用具备持久恢复、幂等、失败可查询和安全重试能力。
6. 请求关联、健康、核心指标能解释 API → 用例 → Module/Event → 外部 I/O 的主要失败点。
7. 新代码受类型、复杂度、异步阻塞和架构方向门禁约束；旧债务的 baseline 只降不增。
8. 每个高风险切片都存在可执行回滚方案；数据库变更另有明确升级/降级说明。
9. `./.venv/bin/python tests/run.py` 通过，或如实记录可复现的环境阻塞与仍未验证范围；不得以专项测试代替全量结果。
10. 最终复核 `docs/rules/`、架构总览、部署文档和实际代码一致，并删除已经过期的临时兼容说明。
