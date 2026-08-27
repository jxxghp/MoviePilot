# MoviePilot V3 架构优化清单

> 审计日期：2026-08-26
>
> 首次审计历史快照：`v3@9053db926d20`；当前交付状态以路线图和生成 fixture 为准
>
> 文档性质：当前源码的差距清单与分阶段执行说明，不是历史重构结项账本

## 1. 结论摘要

MoviePilot V3 已经形成较清晰的模块化单体：`foundation`、`domain`、`runtime`、
`adapters`、`application`、`chain`、`db`、`startup`、`sdk` 和 `compat` 的一级所有权
基本成立。旧物理兼容目录、Model/Oper 自建 Session/提交和直接跨层 DB 依赖等历史问题已有
硬门禁；门禁还能证明可识别的 TaskRegistry 调用已经声明稳定 owner。

当前主要问题不再是“目录没有分层”，而是以下四类更深层的债务：

1. **边界有形、合同偏弱**：部分 Application Port 仍是 `Callable[[], Any]`，返回 ORM 或
   动态代理；生产路径因此继续依赖无 Session Oper、全局 provider 和隐式构造。
2. **用例编排过度集中**：Chain、Scheduler、Plugin、Agent、LLM 和部分 API 文件同时承担决策、
   I/O、状态、生命周期与兼容职责，私有长方法又处于复杂度门禁盲区。
3. **可靠性声明强于实现**：整理 pending 目前只能做最小重放，尚不满足 ADR 中声明的 E3
   状态机语义；部分 commit 后副作用仍存在“业务已提交但调用方收到失败”或进程退出后丢失的窗口。
4. **治理事实源不一致**：架构规则、总览、AST 基线和 CI 语义存在漂移；快照门禁能证明“没有变化”，
   但不能自动证明依赖合理、没有新环或所有运行资源都有生命周期 owner。

审计时唯一 P0 的主线 `mypy` ratchet 失败已由 `5df388719` 修复并推送。后续架构优化按可靠性、
边界类型化、生命周期、复杂度的顺序推进；不建议再做一次大规模目录搬迁。

## 2. 范围与证据边界

### 2.1 本次范围

- 宿主源码 `app/**`，架构统计排除运行时插件副本 `app/plugins/**`。
- 架构、质量、测试与 CI 门禁，以及当前规则文档之间的一致性。
- 插件只审查 SDK、Compat、稳定 ABI 和宿主边界，不把官方插件仓代码混入宿主统计。
- 数据持久化、后台动作完成语义、进程生命周期和外部传输边界。

### 2.2 审计方法

- 读取当前规则、总览、ADR、关键调用链和组合根。
- 检查依赖图、SCC、Module/Event Contract、事务/configuration 基线。
- 对文件、类、方法长度及类型/覆盖率治理范围做静态统计。
- 运行架构、复杂度、异步阻塞、TaskRegistry owner、service locator、Ruff 和 mypy ratchet。
- 运行 9 个架构/质量专项测试文件，共 `158 passed`。

本次没有运行完整测试套件，也没有用生产流量做动态剖析。因此，清单中的性能收益和低频并发故障
仍需在对应实施阶段通过专项故障注入、全量测试和运行指标确认。

## 3. 当前架构画像

### 3.1 分层现状

| 层/区域 | 已形成的正确边界 | 当前主要缺口 |
|---|---|---|
| `foundation` | 无状态、无配置、无 I/O 的底层机制 | 当前未发现需要重做的结构性问题 |
| `domain` | 已与 DB、Application、Adapter 保持单向隔离 | `domain/context.py` 等核心对象过大，来源投影与领域模型仍集中 |
| `runtime` | 已有任务、事件、缓存、托管资源和停止合同 | 导入/构造时启动线程；`global_vars` 仍混合停止、主循环和 WebPush 状态 |
| `adapters` | 技术与命名外部生态已有明确目录 | Application/Chain 仍直接识别部分具体 Adapter；例外规则没有形式化 |
| `application` | 已有命令、Port、Outbox 和安全能力 | 部分 Port 是 `Any` 服务定位器，ORM/具体 Adapter 仍会穿透边界 |
| `chain` | 多入口复用的用例编排与 Module 动态分发已稳定 | God object、私有长方法、包根 SCC、无参构造和资源物化过重 |
| `db/oper` | Model/Oper 已不自建 Session、不自行提交 | 无 Session Facade 仍被宿主组合根注入，业务操作可能拆成多个事务 |
| `db/adapters` | 已有显式 Session/UoW 的参考切片 | Port 返回类型不够稳定；Outbox 的 stage 与自提交 dispatcher store 混在一个类型 |
| `startup` | 已是 HostRuntime 和生命周期组合根 | `initializers/modules.py` 高扇出，仍有导入期 provider 注册和具体对象目录装配 |
| API/Command/Scheduler | 多数入口已转向 Chain/Application | Agent/System/Plugin 等入口仍承担较多业务与 I/O 编排 |
| `sdk`/`compat` | 精确映射、稳定插件 ABI 和宿主 canonical 路径已落地 | SDK 仍暴露部分可变全局对象/具体 Manager，只能渐进收窄，不能直接删除 |

### 3.2 量化快照

| 指标 | 当前值 | 解释 |
|---|---:|---|
| 宿主 Python 模块 / 内部依赖边 | 835 / 6,817 | `dependency-baseline.json` 当前快照 |
| 非平凡 SCC | 2 | 新增 Chain 包根环；另一个是隔离的 29 模块 TMDB 移植包环 |
| 跨层 DB 边界债务 | 0 | Application、Chain、API、Agent、Runtime、Workflow 到 DB 的受控债务均为零 |
| Model/Oper 事务债务 | 0 | 自建 Session、自动事务装饰器、直接 commit/rollback 等基线均为零 |
| Module Contract | 215 specs / 214 methods / 264 calls | 动态方法名为 0；仍有 50 个结果形状为 `ANY` |
| Event Contract | 53 | 均已有 payload model，但当前全部是 diagnostic enforcement |
| Python 源码量 | 约 271,400 行 | 60 个文件超过 1,000 行，14 个超过 2,000 行 |
| 长方法 | 281 个超过 80 行 | 67 个超过 150 行，23 个超过 250 行；大量是私有方法 |
| 全量 mypy 历史债务 | 11,983 / 601 文件 | strict frontier 当前只覆盖 41 个文件，且 ratchet 已新增 2 个错误 |
| Ruff 历史诊断 | 972 | 低水位门禁通过，但规则集只覆盖 `E4/E7/E9/F/I` |
| 覆盖率低水位 | Application 77.82%，Domain 79.24% | Chain、Runtime、Agent、Adapter、Startup 未进入包级覆盖率门禁 |

### 3.3 热点文件

| 文件 | 行数 | 主要职责混合 |
|---|---:|---|
| `app/chain/subscribe.py` | 3,895 | 搜索、匹配、状态、刷新、规则引用、通知和交互 |
| `app/agent/orchestrator.py` | 3,678 | 会话、运行、流式输出、工具、任务与 Manager |
| `app/agent/llm/provider.py` | 3,528 | provider 目录、认证、模型发现、配置和运行时构建 |
| `app/adapters/external/market.py` | 3,488 | 插件市场、包下载、依赖、备份恢复、健康和兼容入口 |
| `app/chain/transfer.py` | 3,250 | 队列、恢复、规划、执行、历史、通知和清理 |
| `app/chain/search.py` | 2,970 | 搜索计划、并发 fan-out、状态、分页和结果处理 |
| `app/api/endpoints/agent.py` | 2,346 | HTTP/SSE、文件/音频、Agent 会话和事件编排 |
| `app/chain/download.py` | 2,230 | 选择、提交、历史、通知、模块后处理和批量执行 |
| `app/chain/media.py` | 2,191 | 识别、来源投影、缓存、音乐匹配和兼容入口 |
| `app/scheduler.py` | 2,111 | 作业目录、执行状态、恢复、生命周期和领域任务 |

热点不是按行数机械拆文件的依据。只有在提取出稳定合同、保留旧入口委托并有行为测试时，拆分才算
降低复杂度；把长方法原样移动到新目录不算完成。

## 4. 优化清单总表

状态含义：`阻塞` 表示当前门禁已失败；`待执行` 表示尚未开始；`渐进` 表示应按触碰路径逐步收敛。

| ID | 优先级 | 状态 | 事项 | 目标结果 |
|---|---|---|---|---|
| ARCH-001 | P0 | 已交付 | 恢复 mypy ratchet | `5df388719` 已推送，主线既有 CI gate 通过 |
| ARCH-101 | P1 | 执行中 | 统一规则、总览、基线和语义门禁 | 文档声明与机器拒绝条件一一对应 |
| ARCH-102 | P1 | 待执行 | 将 Transfer pending 升级为真实 E3 状态机 | 崩溃窗口可判定恢复，结果未知时进入人工确认 |
| ARCH-103 | P1 | 待执行 | 类型化 Chain/Agent 数据 Port 与 DTO | 宿主主路径不再注入无 Session Oper，不向入口泄漏 ORM |
| ARCH-104 | P1 | 待执行 | 收口跨多次写入的业务事务 | 站点/规则引用清理可整体回滚或幂等恢复 |
| ARCH-105 | P1 | 待执行 | 明确 post-commit 与 Outbox 完成语义 | “业务已提交、后置效果 pending”可被调用方正确识别 |
| ARCH-106 | P1 | 待执行 | 让线程/队列/日志 writer 由 bootstrap/lifecycle 显式构造 | 导入或普通 Chain 构造不再启动进程资源 |
| ARCH-107 | P1 | 待执行 | 消除 Chain SCC，强化循环门禁 | SCC 只剩精确豁免的 TMDB 移植包环 |
| ARCH-108 | P1 | 待执行 | 决策并收口 Application/Chain 到 Adapter 与 HTTP 边界 | 依赖倒置有明确例外、低水位和迁移顺序 |
| ARCH-109 | P1 | 待执行 | 按用例拆分超大 Chain、Scheduler 和厚 API | 稳定 Facade 保留，决策/I/O/状态/生命周期各有 owner |
| ARCH-110 | P1 | 待执行 | Module/Event Contract 分可信级执行 | 宿主 provider 严格，第三方插件仍兼容诊断 |
| ARCH-111 | P1 | 待执行 | 升级复杂度、类型、覆盖率和并发原语门禁 | 高风险私有路径也进入只降不增的治理面 |
| ARCH-201 | P2 | 渐进 | 收窄 PluginHelper/PluginManager 与 SDK 暴露面 | ABI Facade 只委托，构造和具体服务归组合根 |
| ARCH-202 | P2 | 渐进 | 拆分 Agent/LLM provider 职责 | provider catalog、发现、认证、会话和运行时分离 |
| ARCH-203 | P2 | 渐进 | 拆分 Domain 投影与 Startup 高扇出目录 | 保留 canonical 类型和生命周期顺序，降低修改扩散 |
| ARCH-204 | P2 | 渐进 | 合并重复 sync/async 核心逻辑并转换存量测试风格 | 保留双 ABI 外壳，共享纯业务核心 |

## 5. P0：先恢复主线

### ARCH-001 恢复 mypy ratchet

**问题与证据**

- `app/chain/media.py:1233` 把局部变量 `key` 推断为 `str`，`1269` 又赋值为
  `tuple[int, int]`，随后 `1271` 触发 `dict.get` overload 错误。
- 当前 ratchet 报告：`[assignment] 15 -> 16`，并新增 `[call-overload] x1`。
- `.github/workflows/test.yml:71-72` 在主 CI 中执行该门禁。

**执行要求**

- [x] 将标题键和位置键使用不同、带语义的变量名，修复真实类型错误。
- [x] 未使用 `mypy_ratchet.py --write` 接受增长。
- [x] 保留音乐专辑“精确标题优先、碟号/曲序回退”的现有行为。

**验收**

```bash
.venv/bin/python scripts/architecture/mypy_ratchet.py
.venv/bin/python -m pytest tests/test_music_album_match.py -q
```

## 6. P1：结构性问题

### ARCH-101 统一架构事实源

**问题与证据**

- 审计时 `docs/rules/04-design-patterns.md` 仍示范 `SubscribeOper()` 无 Session 调用，
  与生产路径显式 UoW 规则冲突；该项已由 S0-L2.1 修复并增加文档门禁。
- 审计时规则禁止 `application -> concrete adapter`，但 RSS 段落又要求直接消费 network adapter；
  S0-L2.4 已统一为 Application-owned Port + startup 注入，并将现有直连全部列为临时债务。
- 审计时完整 SCC 只进入生成快照，语义测试只覆盖特定根；S0-L2.2 已增加完整宿主 SCC policy 门禁。
- 架构总览此前仍记录 811 模块、6,572 条边和 1 个 SCC，已经落后于当前基线。
- Event consumer 扫描曾把任意同名 `.register()` 调用当成事件注册；S0-L2.5 已改为证明
  canonical EventManager receiver，10 个动态误报归零并保留唯一 workflow 动态注册。
- S0-L2.6 已将 producer/consumer 合并为逐调用事实源：99 个 producer（98 静态、1 动态）与
  17 个 consumer（16 静态、1 动态）；consumer 由不可自动写入的精确人工 policy 管理。

**目标与步骤**

- [x] 指定统一 Event 机器事实源；生成快照与人工 consumer policy 分离且互相不能覆盖。
- [x] 修正 Oper 示例，分别展示宿主显式 UoW 与插件兼容 Facade，并以文档测试禁止回退。
- [x] 明确 Application/Chain 不永久直连具体 Adapter；业务层拥有 Port，startup 注入实现。
- [x] 让 SCC 规则、精确 policy 和文档声明一致；Chain 临时债务与 TMDB vendor containment 分开治理。
- [x] Event 扫描只识别 EventManager 实例/别名和事件装饰器；未知 receiver 不再污染动态事实。
- [x] CI 分开报告“Event 语义 policy”与“宿主快照一致”，禁止把后者表述为架构完全正确。

**验收**

```bash
.venv/bin/python -m pytest \
  tests/test_architecture_dependencies.py \
  tests/test_architecture_contract_baseline.py \
  tests/test_architecture_baseline_cli.py -q
.venv/bin/python scripts/architecture/baseline.py --check-host --diagnostics
```

### ARCH-102 将 Transfer pending 升级为真实 E3 状态机

**问题与证据**

- `app/application/transfer.py:139-145` 当前先把任务接受到内存结构，再执行持久登记回调。
- `app/chain/transfer.py:1012-1024` 吞掉登记失败并继续执行；
  `tests/test_transfer_pending_replay.py:62-71` 固化了这一 fail-open 行为。
- worker 未知异常最终也会在 `app/chain/transfer.py:1107-1113,1262-1272` 删除 pending。
- `app/db/models/transferpending.py:9-34` 只有 `storage/src_path/created_at`，没有目标、模式、
  step、lease、attempt、last_error，无法判定“文件已移动、历史未提交”等中间态。
- 这与 `docs/adr/0007-background-action-reliability.md:123-139` 对 E3 的稳定身份、步骤状态、
  lease/heartbeat 和人工恢复要求不一致。

**目标与步骤**

- [ ] 先在独立持久事务中 commit pending，再尝试放入内存队列；数据库事务不能与 `queue.Queue`
  原子提交，入队失败时必须保留 pending 供重放。
- [ ] 初始登记保存稳定源身份、模式、状态和 attempt/lease；目标在规划完成后以 planning checkpoint
  更新，不能要求任务刚入队时已经具备尚未计算的目标路径。
- [ ] 设计幂等文件操作和历史提交；只有所有必要步骤达到持久终态后才能删除记录。
- [ ] 在持久状态机与现有失败历史/AI retry 之间指定唯一 retry owner，定义旧记录迁移和兼容规则。
- [ ] E3 失败使用持久 `failed/manual_review`、最后稳定 checkpoint 和补偿边界，不直接套用 E2
  Outbox 的 dead-letter 语义；禁止按年龄通用清理 pending。
- [ ] 数据模型变更必须配套 Alembic migration，并验证升级与降级路径。

**故障注入验收**

- [ ] 登记后、内存入队前崩溃，重启可继续。
- [ ] 持久登记成功但内存入队失败，重启可继续。
- [ ] 文件移动后、历史提交前崩溃，在支持稳定身份/幂等操作的存储上不重复移动且可补齐历史。
- [ ] worker 未知异常和 lease 超时后保留可诊断状态。
- [ ] 重复回放、重复消息和人工重试都保持幂等。
- [ ] 外部存储返回结果未知时进入 `manual_review`，不得伪装成 exactly-once 成功。

```bash
.venv/bin/python -m pytest \
  tests/test_transfer_pending_replay.py \
  tests/test_transfer_worker_lifecycle.py \
  tests/test_chain_durable_events.py -q
```

### ARCH-103 类型化 Chain/Agent Port 与 DTO

**问题与证据**

- `app/application/chain/data.py:14-29,134-176` 的 Oper factory 和 getter 基本都是 `Any`；
  `app/application/agentdata.py:91-120` 还通过 `__dict__.update()` 动态组装端口。
- `app/startup/initializers/modules.py:848-863,896-910` 仍向生产 Chain/Agent 注入多个无 Session Oper。
- 无 Session Oper 会为单次调用独立创建事务；一个业务操作的“查询后更新”可能被拆成多个事务。
- Workflow query 和 Chain/Agent raw data port 仍返回 `Any`/ORM，Subscription mutation 内部也消费 ORM，
  因而存在 Session 生命周期外 detached/lazy-load 的潜在风险。公开 Subscription、Site、History
  QueryService 已经投影 DTO，属于完成项，不应重做。

**目标与步骤**

- [ ] 按领域定义 Query/Command Protocol，不再使用通用 `OperFactory = Callable[[], Any]`。
- [ ] 写 Port 由 `db/adapters` 创建单操作 Session/UoW；Oper 的 canonical 写方法只 stage/flush，
  读取方法仍可在调用方 Session 中查询。
- [ ] 查询 Port 在 adapter Session 内映射为冻结 DTO/Projection，Application 和 API 不接收 ORM。
- [ ] `ChainDataPorts`/`AgentDataPorts` 可暂时保留为兼容聚合器，但字段必须显式、可类型检查。
- [ ] 以一个业务纵切面迁移并验证后，再迁移下一组，禁止一次替换所有 Oper。
- [ ] 增加 AST 门禁，禁止向 `ChainDataPorts`、`AgentDataPorts` 和新的 canonical use-case service
  注入裸 Oper；SystemConfig singleton、legacy transaction runner 等兼容边界使用精确 allowlist。

**首批建议**

1. Workflow query DTO。
2. Chain/Agent 的 Subscribe/History/User/Site raw port。
3. Subscription mutation 与站点、规则组引用更新。
4. Agent 数据能力。

**验收**

- 关闭 adapter Session 后序列化 DTO，确认不会触发 lazy load。
- 对迁移的 Application Port 运行 strict mypy，并验证 API response contract 与插件 ABI。

```bash
.venv/bin/python -m pytest \
  tests/test_db_workflow_queries.py \
  tests/test_agent_data_ports.py \
  tests/test_subscription_query_service.py \
  tests/test_site_query_service.py \
  tests/test_history_query.py -q
.venv/bin/mypy --config-file mypy.ini
```

### ARCH-104 收口跨多次写入的业务事务

**问题与证据**

- `app/chain/subscribe.py:3010-3047` 删除站点引用时先写 SystemConfig，再逐条更新订阅。
- `app/chain/subscribe.py:3073-3110` 清理规则组时依次更新多项配置和多条订阅。
- 当前无 Session Facade 让这些步骤各自提交；中途失败可能长期残留部分状态，直至同一事件再次触发
  或人工修复。处理本身具备一定幂等性，但触发事件不是 durable owner。

**目标与步骤**

- [ ] 将引用分析与修改计划提取为纯函数/值对象。
- [ ] SystemConfig 与 Subscribe 位于同一宿主数据库，首选一个 Application Command/UoW 和批量原子更新；
  复用 `SystemConfigOper.update_atomically()` 的锁行能力，而不是预设必须跨存储补偿。
- [ ] 同一 UoW 内修改配置表后，在 commit 成功时一次性发布全部进程内配置快照，并明确
  `_write_lock`/`_snapshot_lock` 的顺序；读者只能观察完整旧快照或完整新快照。
- [ ] 仅当未来确有无法共享事务的外部状态时，才使用持久、幂等、带 checkpoint 的 reconciliation job。
- [ ] 每一步允许安全重试，返回明确的完成/待恢复状态。
- [ ] 在第 `k` 次写入注入异常，验证整体回滚或下次能恢复到完整状态。
- [ ] 增加并发触发和并发读取测试，验证没有丢更新、死锁或配置中间组合。

```bash
.venv/bin/python -m pytest \
  tests/test_site_mutation_command.py \
  tests/test_subscription_mutation_outbox.py \
  tests/test_rule_group_media.py -q
```

### ARCH-105 明确 post-commit 与 Outbox 完成语义

**问题与证据**

- `app/application/outbox.py:167-192` 在业务提交后直接执行 `after_commit()`、即时 publish 和完成标记；
  某一步抛错时，调用方可能收到失败，但业务行和 intent 已经提交。
- 通用 `DurableEventCommand` commit 后没有先调用已经定义的 `claim_by_event_key()`；dispatcher 可在
  commit 与请求线程即时 publish 之间先 claim 并投递，随后请求线程再次 publish，形成双发窗口。
  `app/application/subscription/complete.py:111-145` 已提供先 claim 的正确参考。
- 下载历史及事件 intent 已原子提交；通知在 commit 后同步执行，模块后处理和字幕再投进线程池。
  进程在提交与这些动作完成之间退出时，未持久化的动作不会自动恢复。
- `SqlAlchemyOutboxRepository` 同时提供不提交的 `stage()` 和内部自提交的 dispatcher 方法，
  事务所有权没有由类型清楚表达。

**目标与步骤**

- [ ] Command 返回结构化结果：业务是否提交、哪些后置效果完成、哪些处于 pending。
- [ ] commit 后先取得 delivery lease；未取得时跳过请求线程直投，确保与 dispatcher 排他。
- [ ] 每个 post-commit effect 使用独立 intent 隔离和结算，避免效果 A 失败导致效果 B 被一起重放。
  单个外部效果仍是 at-least-once，必须有稳定幂等键和幂等 handler/消费者。
- [ ] 按完成承诺、可重建性、外部不可逆性和业务重要性划分 E0-E3；用户可见性只是因素之一。
- [ ] 拆分 `OutboxStager` 与 `OutboxDispatchStore`，避免业务 Session 调用自提交方法。
- [ ] 在 commit、claim、publish、complete、通知和任务提交各断点注入异常/崩溃，验证声明等级与实际恢复一致。
- [ ] 增加请求线程即时投递与 dispatcher 并发竞争测试，以及“外部调用成功、complete 前崩溃”的重放测试。

```bash
.venv/bin/python -m pytest \
  tests/test_outbox.py \
  tests/test_chain_durable_events.py \
  tests/test_subscription_completion_command.py \
  tests/test_subscription_mutation_outbox.py -q
```

### ARCH-106 让进程资源由 bootstrap/lifecycle 显式构造

**问题与证据**

- 导入 `app.runtime.config` 会构造日志 handler，并启动 `LogBatchWriter` 线程；普通 import 已有副作用。
- `ChainBase.__init__` 无条件构造消息队列 manager，任意一个 Chain 都可能启动消息线程。
- `global_vars` 同时承担停止兼容、WebPush 订阅和主事件循环 owner，多层代码直接消费。
- 当前 TaskRegistry 门禁只检查 Registry 调用是否带 owner，不会扫描全部原生 `create_task`、
  `Thread`、`Timer`、`Executor`。
- 当前 service locator 门禁只覆盖 scheduler/module/plugin/command/workflow 五类 concrete runtime，
  没有覆盖 `global_vars`，门禁通过不能证明全局状态已经收口。
- Workflow、Scheduler、Command、Agent initializer 仍有导入期 provider 注册；需要明确这是兼容设计，
  还是迁入显式装配阶段并提供 test reset，不能长期处于未声明状态。

**目标与步骤**

- [ ] 日志 writer、消息队列及其他长生命周期资源由 bootstrap/lifecycle 显式构造、发布和关闭。
  现有关闭路径已能收口部分资源，当前要修的是隐式创建权，而不是重新发明 owner。
- [ ] startup 唯一创建/启动资源；Chain 只接收无生命周期的发送 Port。
- [ ] 普通模块 import 和构造 `MediaChain` 等非消息用例不得新增线程。
- [ ] 将 WebPush registry、主循环 execution gateway 和停止状态拆给各自 owner。
- [ ] 保留 `global_vars` 作为兼容薄门面，但禁止 canonical 宿主新增依赖。
- [ ] 将 initializer 的 provider 注册迁入显式 `configure_runtime_ports` 装配阶段并提供测试 reset；
  若某项必须保留导入期兼容行为，需精确记录调用者、原因和退场条件。
- [ ] 建立原生并发原语清单：结构化局部等待、TaskRegistry、显式生命周期 owner 或受限上下文；
  未分类的新原语由 ratchet 拒绝。

**验收**

- 隔离进程冷导入前后比较线程名。
- 正常模式和 safe mode 均验证启动顺序、有限关闭、超时保留 owner 和重试关闭。
- 运行消息、Scheduler、Workflow、Agent、Plugin 和 lifecycle 专项测试。
- 在 startup 到达 `yield` 前失败时，已经创建的日志/队列资源也必须收口。

```bash
.venv/bin/python -m pytest \
  tests/test_lifecycle_shutdown.py \
  tests/test_module_lifecycle.py \
  tests/test_system_notification_dispatch.py \
  tests/test_scheduler_lifecycle.py \
  tests/test_workflow_execution.py \
  tests/test_agent_lifecycle.py \
  tests/test_plugin_lifecycle_coordinator.py -q
```

### ARCH-107 消除 Chain SCC 并强化循环门禁

**问题与证据**

- 当前新增 SCC 为 `app.chain -> app.chain._messaging/_recognition -> app.chain`。
- `app/chain/__init__.py` 同时是 1,000 行以上的 `ChainBase` 实现和插件/宿主导入入口，
  又在包初始化时导入 mixin。
- `baseline.py --check-host` 会确认该 SCC 与 fixture 一致，但现有语义测试没有拒绝 Chain 包内环。

**目标与步骤**

- [ ] 将 canonical `ChainBase` 实现移到 `app/chain/base.py`。
- [ ] 宿主全部改用 `app.chain.base.ChainBase`；包根不得 eager re-export，否则父包与子模块仍形成 SCC。
- [ ] 若 `from app.chain import ChainBase` 必须继续兼容，使用仅限 ABI 的 lazy facade，并以独立测试
  证明它没有被宿主 canonical 路径使用；同时把这一精确例外写入“不得延迟导入隐藏循环”的规则。
- [ ] 保证 `_messaging`、`_recognition`、`base` 任意冷导入顺序都不依赖部分初始化包。
- [ ] 对排除 `app/plugins/**` 的完整宿主图断言 SCC 集合等于精确 allowlist，而不是继续追加根前缀。
- [ ] TMDB 29 模块移植包允许正常单向包外依赖，但禁止 SCC 成员扩展到 allowlist 外、包外模块
  通过反向边加入该 SCC，或新增不符合分层方向的边。
- [ ] 更新兼容、SDK、架构文档和 fixture 前先审查真实依赖变化。

**验收**

- 冷导入 `app.chain.base`、`app.chain._messaging`、`app.chain._recognition` 的全部顺序组合。
- 验证宿主当前包根导入全部迁移，并保留 `app/plugins/__init__.py` 等插件兼容探针。

```bash
.venv/bin/python -m pytest \
  tests/test_architecture_dependencies.py \
  tests/test_architecture_contract_baseline.py \
  tests/test_legacy_import_compat.py \
  tests/test_plugin_sdk.py -q
.venv/bin/python scripts/architecture/baseline.py --check-host
```

### ARCH-108 收口具体 Adapter 与 HTTP 边界

**问题与证据**

- 当前 `app/application` 有 10 个文件、15 条直接 Adapter 导入，代表路径包括
  `security/passkey.py`、`backup.py`、`image.py`、`rss.py`、`security/cookie.py`。
- `app/chain` 有 8 个文件、13 条直接 Adapter 导入，使用 `RequestUtils`、Browser、Cloudflare、
  CookieCloud、ServerHelper 等具体能力。
- Passkey Application 服务直接判断 Redis 后端并调用 `RedisHelper.pop()`，安全策略识别了具体实现。
- 审计时 LLM streaming、第三方 SDK、移植库和本地控制面没有精确例外表；S0-L2.4b 已建立
  66 条完整 egress identity 与 zero-growth policy，其中 11 条普通 HTTP/Session bridge 和 1 条
  Application DNS I/O 是清零债务；每条初始边另有独立指纹上界，不能靠同时刷新 baseline/policy
  掩盖同一边的调用面增长，债务删除后也不得恢复。

**目标与步骤**

- [x] 建立 Application/Chain 原始 Adapter 直连事实与精确临时 policy，冻结新增、替换和陈旧条目。
- [x] 建立全宿主 direct egress 事实；SDK/stream/vendor/local-control 例外精确到 bindings/uses 指纹。
- [ ] 将 Passkey 原子领取提升为 runtime cache contract，由 Memory/Redis backend 分别实现。
- [ ] 为 Backup 定义 Application-owned artifact store Port，由 startup 注入文件系统实现。
- [ ] 将 policy 中 11 条普通 HTTP/Session bridge 债务迁移到统一网络能力并把目标收缩为空。
- [ ] 为 Application SSRF 校验注入 DNS 解析 Port，清除 `socket.getaddrinfo` 直接 I/O。
- [ ] 命名外部产品、安全敏感能力及通用技术 Adapter 均改为注入 Port；不在 Application/Chain 保留直连例外。
- [ ] 最终把基线收缩到零或少量书面化例外，而不是一次性禁止后再大量豁免。

```bash
.venv/bin/python -m pytest \
  tests/test_passkey_challenge.py \
  tests/test_mfa_passkey_transactions.py \
  tests/test_database_backup_service.py \
  tests/test_database_backup_adapters.py \
  tests/test_rss_helper.py -q
.venv/bin/python scripts/architecture/baseline.py --check-host
```

### ARCH-109 按用例拆分超大编排器

**共同规则**

- 保留旧 Chain/Manager/Scheduler 的公开方法和插件 ABI，内部委托新服务。
- 先提取合同和行为测试，再迁移一条主调用链；不按文件行数机械切块。
- 纯决策进入 Domain/Application policy，I/O 进入 Adapter/Port，运行状态进入明确 owner。
- 每个切片应能独立验证、提交和回滚。

**建议切片**

| 热点 | 建议所有权拆分 | 首要长方法/耦合点 |
|---|---|---|
| `SubscribeChain` | search、match、refresh、reference reconciliation、notification | 匹配和站点/规则引用更新 |
| `TransferChain` | queue/recovery、plan、execute、settle、history/notify | `_execute_transfer` 约 836 行 |
| `SearchChain` | plan、provider fan-out、result state、pagination | 并发状态与结果处理 |
| `DownloadChain` | selection、submission、history、post-processing | batch download 约 449 行 |
| `MediaChain` | recognition、source projection、music alignment、cache | 保留公共识别 Facade |
| `Scheduler` | JobCatalog、ExecutionRegistry、domain reconciler、lifecycle Facade | `init` 约 336 行且直接构造多个 Chain |
| Agent API | WebAgent session/SSE/file/audio Application service | `_web_agent_stream_impl` 约 361 行 |
| System/Plugin API | nettest、logging、update、market use cases | 入口直接组合多个 Helper/Manager |

### ARCH-110 分可信级执行 Module/Event Contract

**问题与证据**

- Module 动态方法名已经归零，这是完成项；但 50 个结果形状仍是 `ANY`。
- `app/runtime/extensions/module/dispatcher.py:463-494` 对签名和结果偏差只告警并继续透传。
- Event 53 个 payload model 已覆盖，但当前全部为 diagnostic enforcement，输出合同也很少。

**目标与步骤**

- [ ] 按“宿主内置 provider / 官方插件 / 第三方未知插件”划分可信级。
- [ ] 下载、存储、消息等高风险宿主能力使用真实 Protocol/DTO，并在 admission 阶段 fail fast。
- [ ] 官方插件先以 CI baseline 验证，再逐组切 strict。
- [ ] 第三方插件继续诊断兼容，错误指标必须包含 method、provider、ABI source。
- [ ] 不得为了严格化改变 `run_module` 字符串 ABI 或吞掉旧插件原始返回值。

```bash
.venv/bin/python -m pytest \
  tests/test_module_method_contracts.py \
  tests/test_module_invocation_dispatcher.py \
  tests/test_module_quality.py \
  tests/test_event_contracts.py -q
.venv/bin/python scripts/architecture/baseline.py --check-host --diagnostics
```

### ARCH-111 升级质量与架构趋势门禁

**当前盲区**

- 复杂度脚本只检查 API、Application、Chain 的公共入口；私有长方法、类/文件规模和圈复杂度不受控。
- strict mypy 仅 41 个文件，高风险 lifecycle、Scheduler、Agent、Plugin Manager 多数不在 frontier。
- coverage ratchet 只聚合 Application 和 Domain。
- Task owner gate 尚未盘点原生并发原语；Event producer/consumer 已纳入统一事实源和人工 policy。
- Ruff 仅是有限规则集的历史低水位，不代表整体风格/正确性无债务。

**目标与步骤**

- [ ] complexity v2 增加私有方法、class/file 行数和圈复杂度；先记录低水位，再只降不增。
- [ ] strict mypy frontier 按 `runtime extensions -> startup -> workflow/scheduler -> messaging -> Agent`
  扩大；每批必须先清零再加入配置。
- [ ] coverage 增加 Chain、Runtime、Agent、Startup 的高风险子包或关键文件组，不用低价值行数冲百分比。
- [ ] 原生并发门禁按 ARCH-101/106 修正。
- [x] Event consumer 扫描证明 canonical EventManager receiver，清除同名方法误报。
- [x] Event producer 别名/关键字/有限条件识别和 consumer exact policy 纳入统一事实源门禁。
- [ ] Module Quality Scale 增加 capability -> required rules -> evidence tests 映射，避免“已登记”等同“已验证”。
- [ ] 修改 CI 或门禁脚本时同时运行 `tests/test_architecture_ci.py` 和对应脚本单元测试。

## 7. P2：持续可维护性优化

### ARCH-201 收窄 Plugin 与 SDK 边界

- [ ] `PluginHelper` 继续作为兼容入口，但市场、包、依赖、备份/恢复、健康修复分别委托现有 owner。
- [ ] `PluginManager` 的服务图构造移到 startup typed `PluginRuntime` factory，Facade 只保留稳定 API。
- [ ] 新 SDK 提供只读配置、插件查询和主循环提交等窄合同；旧 `settings/global_vars` 和具体 Manager
  只能标记弃用，不能在当前大版本直接删除。
- [ ] 用官方插件仓 baseline 和真实插件导入探针决定退场，不按宿主“无人引用”判断。

### ARCH-202 拆分 Agent 与 LLM provider

- [ ] 将内置 provider spec 数据从 `LLMProviderManager` 分离为只读 catalog。
- [ ] 模型发现、认证会话、运行时构建和用户配置分成可替换服务。
- [ ] 将 WebAgent SSE、文件/音频与会话桥接移出 API endpoint。
- [ ] 保留 `AgentManager`/`LLMProviderManager` 公开方法为稳定 Facade，并逐批纳入 strict mypy。

### ARCH-203 拆分 Domain 投影与 Startup 高扇出

- [ ] `MediaInfo`/`domain/context.py` 保留 canonical 类型路径，把各外部来源 setter 的纯投影规则提取到
  Domain 内部模块，旧方法委托，避免 DTO 再复制一套领域语义。
- [ ] 将 `startup/initializers/modules.py` 的对象构造按领域移到 `startup/composition/*`；initializer
  只负责顺序、注册和是否重启的决策。
- [ ] 保留生命周期 manifest 和顺序快照；高扇出在组合根是允许的，但业务实现不能继续沉积其中。

### ARCH-204 收敛 sync/async 重复与测试存量

- [ ] 对重复 sync/async 方法先识别共享纯逻辑；保留双入口 ABI，只共享解析、校验、映射和状态决策。
- [ ] 不用在线程包装里假装异步，也不因去重而跨事件循环复用 Session/客户端。
- [ ] 修改到 `unittest.TestCase` 文件时按项目规则渐进转 pytest-native；不发起无行为收益的全库转换。
- [ ] 每次转换都恢复 monkeypatch 的全局状态、singleton、cache 和 `sys.modules`。

## 8. 建议实施顺序

### 阶段 0：恢复可信基线

1. 单独修复 ARCH-001，不刷新 mypy baseline。
2. 完成 ARCH-101 的文档/门禁语义决策。
3. 给 Adapter、原生并发、私有复杂度建立只读清单和 zero-growth 基线。

**退出条件**：当前全部 CI gate 通过；文档、fixture 和测试对 SCC、Adapter 例外、Oper 使用方式的描述一致。

### 阶段 1：先修可靠性和事务语义

1. ARCH-102 设计并迁移 Transfer E3 状态机。
2. 以站点/规则引用清理为 ARCH-103/104 的第一个 typed Port + UoW 纵切面。
3. 完成 ARCH-105 的 post-commit 结构化结果与 Outbox 角色拆分。

**退出条件**：故障注入覆盖清单列出的崩溃窗口；schema 有 migration；主路径不再 fail-open；调用方能区分
业务提交与后置效果 pending。

### 阶段 2：收口进程边界

1. ARCH-106 将日志、消息队列和主循环 gateway 纳入 lifecycle。
2. ARCH-107 拆出 `chain/base.py` 并消除新增 SCC。
3. ARCH-108 先迁移 Passkey、Backup，再按风险迁移外部调用。

**退出条件**：冷导入不启动线程；SCC 只剩 TMDB 精确豁免；Application/Chain 到 Adapter 的债务只降不增。

### 阶段 3：按热点做纵向拆分

建议顺序：Transfer -> Subscribe -> Scheduler -> Download/Search -> Agent API。每次只迁移一个完整用例，
旧 Facade 委托并同时运行新旧入口契约测试。

**退出条件**：复杂度低水位下降，公开 ABI 未变，文件移动没有新增跨层边或兼容映射滥用。

### 阶段 4：扩大强类型与严格合同

1. ARCH-110 对宿主高风险 Module/Event 开 strict。
2. ARCH-111 扩大 strict mypy 和 coverage frontier。
3. 执行 ARCH-201 至 ARCH-204 的渐进收敛。

**退出条件**：新增代码全部位于强治理面；历史低水位持续下降；官方插件兼容验证无回退。

## 9. 每个实施切片的统一门禁

先运行受影响测试，再按风险扩大。架构、持久化、启动、生命周期、兼容层或跨模块改动必须运行全量套件。

```bash
# 当前宿主架构与趋势门禁
.venv/bin/python scripts/architecture/baseline.py --check-host
.venv/bin/mypy --config-file mypy.ini
.venv/bin/python scripts/architecture/complexity.py
.venv/bin/python scripts/architecture/async_blocking.py
.venv/bin/python scripts/architecture/task_ownership.py
.venv/bin/python scripts/architecture/service_locator.py
.venv/bin/python scripts/architecture/ruff_ratchet.py
.venv/bin/python scripts/architecture/mypy_ratchet.py
.venv/bin/python scripts/startup/performance.py --check --repeat 3

# 架构专项
.venv/bin/python -m pytest \
  tests/test_architecture_dependencies.py \
  tests/test_architecture_contract_baseline.py \
  tests/test_architecture_baseline_cli.py \
  tests/test_complexity_gate.py \
  tests/test_async_blocking_gate.py \
  tests/test_task_ownership_gate.py \
  tests/test_quality_ratchets.py \
  tests/test_mypy_gate.py \
  tests/test_architecture_ci.py -q

# 广泛变更的最终本地回归
uv run --locked --no-sync python tests/run.py

# 需要覆盖率证据时按 CI 串行生成真实报告，再检查低水位
uv run --locked --no-sync python -m coverage erase
uv run --locked --no-sync python -m coverage run tests/run.py --serial
uv run --locked --no-sync python -m coverage json
uv run --locked --no-sync python scripts/architecture/coverage_ratchet.py

# Python 源码改动对实际变更路径运行（按切片替换示例路径）
uv run --locked --no-sync pylint app/path/to/changed.py
```

附加要求：

- fixture 只有在依赖/合同变化经过人工审查后才能用 `--write-host` 更新，不能为转绿接受新增债务。
- 所有 DB schema 变化必须新增 Alembic migration。
- Plugin ABI、SDK/Compat 映射和官方插件仓验证独立执行；宿主绿灯不能替代插件兼容证据。
- 正式插件 ABI 验证前先同步独立插件仓并记录所验 SHA，再运行：
  `.venv/bin/python scripts/architecture/baseline.py --check-plugins --plugin-repo ../MoviePilot-Plugins`。
- 每个阶段使用独立、可回滚的提交；不要把一个专项绿灯表述成整个长期目标完成。
- 完成一次所有权迁移后，同步更新 canonical import、架构规则、SDK/Compat（仅在确有公共迁移时）和测试。
- 依赖变更还必须运行 `uv lock --check`、锁定环境检查和
  `docs/rules/03-commands.md` 规定的运行依赖漏洞审计。

## 10. 明确不做

- 不恢复物理 `app/core`、`app/helper`、`app/utils`、`app/log.py`。
- 不删除精确 Compat 映射、`app.sdk._legacy`、Chain/Manager 稳定 Facade 或旧插件 ABI。
- 不把 `app/plugins/**` 当作宿主实现重构；官方插件仓单独验证。
- 不把所有 `run_module` 字符串调用一次性替换成静态 import。
- 不把所有后台工作改造成持久队列；仅按 ADR 判定为需要跨重启完成的 E2/E3 动作进入 durable 机制。
- 不机械拆 TMDB 移植包 SCC；当前只做精确 containment，未来通过替换/升级移植库处理。
- 不为降低行数进行无合同、无行为测试的目录搬运。
- 不一次性删除 sync/async 双入口、全局兼容对象或无参构造 ABI。
- 不用更新 baseline、降低门禁或增加宽泛 allowlist 掩盖回归。

## 11. 已完成且应继续保护的边界

- [x] 旧 `core/helper/utils/log` 物理源码已移除，Compat 使用精确、惰性映射。
- [x] canonical 实现层当前无 SDK/Compat 反向违规，`app/__init__.py` 和插件启动扫描等 bootstrap
  例外保持精确；插件旧导入仍可用。
- [x] Model/Oper 自建 Session、自动写事务和直接 commit/rollback 债务为零。
- [x] HostRuntime、显式 Session/UoW、订阅/站点/Workflow 等参考切片已经落地。
- [x] TaskRegistry owner、Workflow 有界关闭、Scheduler/Agent/Plugin/Message 生命周期已有专项保护。
- [x] durable event/outbox 已覆盖关键事件切片；后续是按完成语义扩面，不是推倒重写。
- [x] Module 动态方法名为零；Event payload model 已覆盖全部登记事件。
- [x] `app/plugins/**` 与宿主架构基线分离，SDK/Plugin ABI 是独立验收面。

这些完成项是后续优化的护栏。任何阶段若重新引入 Model/Oper 自建事务、物理旧路径、无 owner 资源
或宿主到插件实现的依赖，即使局部测试通过，也应视为架构回退。
