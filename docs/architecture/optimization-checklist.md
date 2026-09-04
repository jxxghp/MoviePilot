# MoviePilot V3 架构优化清单

> 审计日期：2026-08-30
>
> 首次审计历史快照：`v3@9053db926d20`；历史问题描述用于追溯，当前事实与交付状态以
> 本文“当前架构画像”、路线图和生成 fixture 为准
>
> 文档性质：原始差距、治理结果和剩余交付边界的持续校准账本

## 0.1 当前残余收口目标（G-ARCH-RESIDUAL）

2026-08-30 已针对本账本识别的残余问题建立独立追踪目标。历史 S4 仍为
`CANCELLED`，本目标不将全量 mypy、Ruff、覆盖率或并发质量债务重新扩大为本轮完成依赖；
只处理下表所列、已由审计确认且会影响真实运行语义或治理可信度的缺口。

| ID | 状态 | 问题 | 收口要求 |
|---|---|---|---|
| RES-001 | `DELIVERED`（R1） | System API 仍混有日志、Wiki、配置、事件和更新编排 | `d50177f45`：Application service/Port 拥有业务与 I/O 决策，endpoint 只做传输适配；旧 helper 已删除 |
| RES-002 | `DELIVERED`（R2） | 复杂度门禁漏扫私有方法、类/文件和 Scheduler；原生并发清单不完整 | `f7ca7e517`、`5cd5780d3`：完整宿主 AST、canonical owner/count、低水位与零增长门禁已落地 |
| RES-003 | `DELIVERED`（R3） | Outbox after_commit 失败只有内存 pending 标记，重启不可恢复 | `9d06f91bb`：持久 intent、唯一 handler、claim/fencing、重启回放、幂等和失败观测完整闭环 |
| RES-004 | `DELIVERED`（R4） | Startup initializer 与插件市场存在多套 Transport/Adapter/Manager 构造 | `7f5b8b469` 至 `046b0b305`：构造回收到 composition，兼容门面消费同一 owner，canonical 无重复正式实现 |
| RES-005 | `DELIVERED`（R5） | 审计声明、机器门禁、CI 与远端交付状态需要重新校准 | 当前文档、固定 80% 覆盖率门禁与规则一致；Pylint `10.00/10`、架构/兼容、真实启动和最终 exact-head GitHub CI 闭环，远端 `0/0` |
R2 门禁现已完整覆盖私有、dunder、任意控制流嵌套方法、类、文件与
`app/scheduler/`；`concurrency.py` 扫描完整宿主源码，按 canonical import/alias、
TaskGroup、可证明的 loop/executor 来源和词法 owner 聚合数量。新增 owner、数量增长以及
复杂度与静态质量事实下降后未刷新低水位都会阻断 CI；覆盖率只要求 Application 与 Domain
达到固定 80%，行号移动和普通同名方法不会制造噪音。

## 1. 结论摘要

MoviePilot V3 已经形成较清晰的模块化单体：`foundation`、`domain`、`runtime`、
`adapters`、`application`、`chain`、`db`、`startup`、`sdk` 和 `compat` 的一级所有权
基本成立。旧物理兼容目录、Model/Oper 自建 Session/提交和直接跨层 DB 依赖等历史问题已有
硬门禁；门禁还能证明可识别的 TaskRegistry 调用已经声明稳定 owner。

截至当前校准，原审计识别的四类结构性债务已经按既定路线收口：

1. **类型化边界**：Chain/Agent 正式数据入口已使用冻结 DTO 与 typed Port；raw Oper/ORM/`Any`
   只留统一 Legacy/Compat，canonical 宿主不再依赖无 Session 写入口或全局数据 locator。
2. **职责拆分**：Transfer、Subscribe、Download、Search、Media、Scheduler、Plugin、Agent/LLM、
   Domain 投影和 Startup composition 均已有单一 owner；Facade 只保留稳定 ABI 与显式委托。
3. **可靠性语义**：Transfer E3 状态机、业务 UoW、post-commit 结构化结果、Outbox claim/settlement
   fencing 和人工确认边界均已落地，外部 sink 的 at-least-once 责任被明确记录。
4. **治理事实源**：依赖、SCC、Adapter、egress、Event、复杂度、类型、Ruff、生命周期和兼容基线
   已进入机器门禁；`app/plugins/**` 始终排除，插件兼容只由 SDK/Compat 承接。

本轮非取消项已经全部交付。原战略目标的 canonical `SearchChain` 类型债务与架构测试导入边界分别由
`4575b11d8`、`c204e2e97` 清零；残余审计目标又由 `87269610e`、`d50177f45`、
`f7ca7e517`、`5cd5780d3`、`9d06f91bb` 以及 `7f5b8b469` 至 `046b0b305` 结清 System、
复杂度/并发、Outbox 和 composition 缺口。最终锁定全量为 `7684 passed, 9 skipped`，Pylint
`10.00/10`，真实启动与健康探针通过。S0-S3、S5 及 ARCH-001、ARCH-101 至 ARCH-109、
ARCH-201 至 ARCH-204 均达到实现、验证、提交、推送和远端门禁闭环；S4/ARCH-110/111 按维护者
决定保持取消。

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
- 运行完整架构/兼容门禁、锁定全量测试 `7684 passed, 9 skipped`，并以精确 head GitHub Actions
  结果复核四个 Unit Tests 分片、Coverage Report、Architecture Contract Gate 与 Pylint。

本轮没有用生产流量做动态剖析；性能收益仍需由运行指标持续观察，但不再作为本轮架构交付的未完成项。

## 3. 当前架构画像

### 3.1 分层现状

| 层/区域 | 已形成的正确边界 | 当前主要缺口 |
|---|---|---|
| `foundation` | 无状态、无配置、无 I/O 的底层机制 | 当前未发现需要重做的结构性问题 |
| `domain` | 已与 DB、Application、Adapter 保持单向隔离；四类媒体来源投影已拆入独立 owner | `domain/context.py` 仍承载核心领域对象和兼容 setter，继续由复杂度门禁约束 |
| `runtime` | 已有任务、事件、缓存、托管资源和停止合同；资源由 lifecycle 显式装配 | 原生并发原语的全宿主清单属于已取消 ARCH-111，不在本轮扩张 |
| `adapters` | 技术与命名外部生态已有明确目录；Application/Chain 具体 Adapter 直连已清零 | 现存 direct egress 均为精确书面化协议例外，需由 zero-growth 门禁持续守护 |
| `application` | 命令、typed Port、Outbox 和安全能力边界已落地，具体 Adapter 与 raw data locator 已清零 | 新增能力必须继续使用明确 Protocol/DTO，不能恢复 `Any` 服务定位器 |
| `chain` | 多入口复用的用例编排与 Module 动态分发已稳定，热点 Chain 已拆为同名职责包 | Facade 仍需保持稳定 ABI，并由类型、复杂度和兼容门禁防止职责回流 |
| `db/oper` | Model/Oper 已不自建 Session、不自行提交，只在调用方 Session 内查询、stage 或 flush | 当前未发现需重做的结构性事务所有权问题 |
| `db/adapters` | Application Port、短 Session/UoW 与 DTO 投影已落地；Outbox stager/store 已分离 | 新用例需继续防止 ORM 越界和隐式事务回流 |
| `startup` | `composition/runtime.py` 已成为 HostRuntime、领域 Runtime 与旧 ApiData 投影唯一 owner；Chain/Agent 复用同一 RuntimeDependencies | `initializers/modules.py` 仍保留启动顺序与跨领域发布调用，高扇出需由生命周期验收继续约束 |
| API/Command/Scheduler | Agent/System/Plugin 用例已下沉 Application；Scheduler 单体已拆为同名职责包并由 startup 注入业务 callable | endpoint 与 Facade 需继续由架构门禁防止业务编排回流 |
| `sdk`/`compat` | 精确映射、稳定插件 ABI 和宿主 canonical 路径已落地 | SDK 仍暴露部分可变全局对象/具体 Manager，只能渐进收窄，不能直接删除 |

### 3.2 量化快照

| 指标 | 当前值 | 解释 |
|---|---:|---|
| 宿主 Python 模块 / 内部依赖边 | 969 / 8,156 | `dependency-baseline.json` 当前快照 |
| 非平凡 SCC | 1 | 仅保留精确 containment 的 29 模块 TMDB 移植包环 |
| 跨层 DB 边界债务 | 0 | Application、Chain、API、Agent、Runtime、Workflow 到 DB 的受控债务均为零 |
| Model/Oper 事务债务 | 0 | 自建 Session、自动事务装饰器、直接 commit/rollback 等基线均为零 |
| Module Contract | 215 specs / 211 methods / 262 calls | 动态方法名为 0；分类事实富化通过显式合同进入插件调度，旧 YAML 分类方法已移除 |
| Event Contract | 53 | 均已有 payload model，但当前全部是 diagnostic enforcement |
| Python 源码量 | 305,884 行 | 排除 `app/plugins/**`；61 个文件超过 1,000 行，11 个超过 2,000 行 |
| 长方法 | 290 个超过 80 行 | AST 统计排除 `app/plugins/**`；65 个超过 150 行，21 个超过 250 行 |
| 全量 mypy 历史债务 | 9,538 / 513 文件 | Agent API 重构后的现状基线；canonical Facade 与 endpoint 类型边界已补齐，低水位只允许继续下降 |
| Ruff 历史诊断 | 549 | 低水位门禁通过，但规则集只覆盖 `E4/E7/E9/F/I` |
| 覆盖率固定基线 | Application 80.00%，Domain 80.00% | Chain、Runtime、Agent、Adapter、Startup 未进入包级覆盖率门禁 |

### 3.3 热点文件

| 文件 | 行数 | 主要职责混合 |
|---|---:|---|
| `app/chain/subscribe/`（已治理） | Facade 98 行 | 搜索、匹配、刷新、完成、规则引用与通知已拆至单一 owner；原 3,895 行单文件已删除 |
| `app/agent/orchestrator.py` | 2,558 | 单 Agent 运行、流式输出、工具与中间件编排 |
| `app/agent/session.py` | 837 | 会话队列、worker、状态、取消与延迟清理 |
| `app/agent/tasks.py` | 208 | 后台 prompt、持久化定时任务与心跳 |
| `app/agent/lifecycle.py` | 166 | 接收门禁、启动、空闲回收与有界关闭 |
| `app/agent/manager.py` | 27 | 稳定 `AgentManager` 构造门面 |
| `app/agent/llm/provider.py` | 413 | 稳定 `LLMProviderManager` Facade 与兼容方法 |
| `app/agent/llm/catalog.py` | 1,571 | provider spec、预设和模型元数据 |
| `app/agent/llm/discovery.py` | 985 | 远端目录发现与 SDK 客户端 I/O |
| `app/agent/llm/auth.py` | 582 | 持久鉴权和外部授权协议 |
| `app/adapters/external/market.py`（已治理） | Facade 226 行 | `PluginHelper` 只保留精确旧 ABI、安装 Gateway 及 owner 委托 |
| `app/adapters/external/plugin/client.py` | 1,927 | 插件市场传输、索引、Release 与本地仓候选查询 |
| `app/adapters/system/plugin/package.py` | 2,391 | 插件安装、checkpoint、备份、恢复和物理删除事务 |
| `app/adapters/system/plugin/health.py` | 1,060 | 插件运行环境保护、安装前后检查与故障恢复 |
| `app/chain/transfer/`（已治理） | Facade 82 行 | 队列/恢复、规划、执行、结算、历史/通知已拆至单一 owner；旧 `transfer.py` 与 `_transfer.py` 已删除 |
| `app/chain/search/`（已治理） | Facade 473 行 | 搜索计划、并发 fan-out、状态、分页和结果处理已拆至单一 owner；旧 2,970 行单文件已删除 |
| `app/api/endpoints/agent.py`（已治理） | 577 | 只保留 HTTP/SSE、文件/音频传输映射；Agent 会话和事件编排已下沉 Application |
| `app/application/messaging/agent.py` | 2,195 | WebAgent 会话、文件/音频、事件与消息桥接用例；是后续复杂度治理热点，不回流 endpoint |
| `app/chain/download/`（已治理） | Facade 47 行 | 选择、提交、批量、历史、提交后处理、字幕和任务控制已拆至单一 owner；原 2,413 行单文件已删除 |
| `app/chain/media/`（已治理） | Facade 213 行 | 识别、来源投影、插件事件、音乐目录、路径证据与缓存已拆至单一 owner；旧 2,191 行单文件已删除 |
| `app/scheduler/`（已治理） | Facade 135 行 | 原 2,111 行单体已退役；catalog、execution、bridge、progress、registry、reconcile、lifecycle、maintenance 与注入合同已有独立 owner |

热点不是按行数机械拆文件的依据。只有在提取出稳定合同、保留旧入口委托并有行为测试时，拆分才算
降低复杂度；把长方法原样移动到新目录不算完成。

## 4. 优化清单总表

状态含义：`阻塞` 表示当前门禁已失败；`待执行` 表示尚未开始；`执行中` 表示只完成部分纵切面；
`已验证` 表示实现和本地验收已通过，但本叶提交、推送或精确 head 远端门禁尚未全部闭环；
`已交付` 表示实现、提交、推送、远端一致性和该叶要求的 CI 证据均已闭环；`已取消` 表示维护者明确
取消且不再作为本轮完成依赖。

| ID | 优先级 | 状态 | 事项 | 目标结果 |
|---|---|---|---|---|
| ARCH-001 | P0 | 已交付 | 恢复 mypy ratchet | `5df388719` 已推送，主线既有 CI gate 通过 |
| ARCH-101 | P1 | 已交付 | 统一规则、总览、基线和语义门禁 | `113355784` 已推送，Unit Tests `33031697902`、Pylint `33031697785` 全绿，远端 `0/0` |
| ARCH-102 | P1 | 已交付 | 将 Transfer pending 升级为真实 E3 状态机 | `e9de149db`、`a2e249f20` 已推送；Unit Tests `33092427327`、Pylint `33092427348` 全绿，崩溃结果未知时进入人工确认 |
| ARCH-103 | P1 | 已交付 | 类型化 Chain/Agent 数据 Port 与 DTO | Chain/Agent 正式数据入口已切换到冻结 DTO 与 typed Port，raw Oper/ORM/`Any` 仅留统一 Legacy/Compat；最终精确 head CI 已闭环 |
| ARCH-104 | P1 | 已交付 | 收口跨多次写入的业务事务 | 订阅、站点和规则引用变更由单一 UoW/CAS 拥有，失败整体回滚或幂等恢复 |
| ARCH-105 | P1 | 已交付 | 明确 post-commit 与 Outbox 完成语义 | 业务提交、effect 完成/pending 可区分；stager/store 分离且 claim/settlement 受 fencing，外部 sink 仍承担 at-least-once 幂等边界 |
| ARCH-106 | P1 | 已交付 | 让线程/队列/日志 writer 由 bootstrap/lifecycle 显式构造 | 日志、消息、任务、模块与插件资源由 lifecycle 显式创建、逆序关闭和失败回滚 |
| ARCH-107 | P1 | 已交付 | 消除 Chain SCC，强化循环门禁 | SCC 只剩精确豁免的 TMDB 移植包环 |
| ARCH-108 | P1 | 已交付 | 决策并收口 Application/Chain 到 Adapter 与 HTTP 边界 | Application/Chain 具体 Adapter 与普通 direct HTTP 债务清零，剩余出口均为精确政策例外 |
| ARCH-109 | P1 | 已交付 | 按用例拆分超大 Chain、Scheduler 和厚 API | Transfer、Subscribe、Scheduler、Download、Search、Media 与 Agent/System/Plugin API 均已按 owner 收敛；最终精确 head CI 已闭环 |
| ARCH-110 | P1 | 已取消 | Module/Event Contract 分可信级执行 | 随 S4 取消，不属于本轮完成依赖；现有宿主严格合同和插件兼容诊断继续保留 |
| ARCH-111 | P1 | 已取消 | 升级复杂度、类型、覆盖率和并发原语门禁 | 随 S4 取消，不扩张为全宿主质量债务清零；保留现有 ratchet |
| ARCH-201 | P2 | 已交付 | 收窄 PluginHelper/PluginManager 与 SDK 暴露面 | `fa40f29df` 已推送；ABI Facade 只委托，构造和具体服务归组合根 |
| ARCH-202 | P2 | 已交付 | 拆分 Agent/LLM provider 职责 | provider catalog、发现、认证、会话和运行时分离，Facade 只保留稳定 API；最终精确 head CI 已闭环 |
| ARCH-203 | P2 | 已交付 | 拆分 Domain 投影与 Startup 高扇出目录 | Domain 四来源投影与 Startup 单词型 composition owner 均已完成；最终精确 head CI 已闭环 |
| ARCH-204 | P2 | 已交付 | 合并重复 sync/async 核心逻辑并转换存量测试风格 | 双 ABI 只保留 I/O 外壳，共享解析、校验、映射和状态决策；最终精确 head CI 已闭环 |

S4/ARCH-110/111 的取消不撤销现有防回退门禁。后续按路线图的 **S4 Lite** 边界做增量治理：
第三方插件 Module/Event 合同校验继续 diagnostic，宿主 strict contract enforcement 只覆盖
独立评审的内置高风险能力和 durable internal event；这不改变现有 strict 异常传播接口仍可触达
插件 provider 的事实。现有插件 ABI、SDK/Compat/Legacy 行为及原始返回形状不得因宿主升级改变。
复杂度、并发、mypy、Ruff 和 coverage 继续使用当前 zero-growth/低水位 ratchet，但不再追求
一次性清空全宿主历史债务。

## 5. P0：先恢复主线

### ARCH-001 恢复 mypy ratchet

**问题与证据**

- 历史 `app/chain/media.py` 的标题键与位置键复用已修复，随后单体已退役为
  `app/chain/media/` 同名职责包；迁移门禁按错误码聚合并要求新 owner 不增加债务。
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
- S0-L2.6 已将 producer/consumer 合并为逐调用事实源；本轮删除重复 Agent Tool 事件发送点后为
  86 个 producer（85 静态、1 动态）与
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

**分叶状态**

- `S1-L1.1 Durable admission`：`DELIVERED`。已交付 persist-before-enqueue、Application-owned typed Port、
  DB adapter 与可逆 migration，宿主退出 raw/`Any` `TransferPendingOper` admission 路径。
- `S1-L1.2 Planning checkpoint`：`DELIVERED`。版本化请求与指纹先准入；无 legacy provider 时通过
  `accepted -> planned` CAS 提交完整目标和有序操作，有 provider 时先提交 `provider_pending`，全部
  返回空后再以第二次 CAS 提交 `planned`；planned 重放只消费冻结上下文和目标。
- `S1-L1.3 Lease 与恢复调度`：`DELIVERED`。已交付 token fencing 的
  claim/lease/heartbeat/attempt、过期接管、固定退避的唯一恢复入口和有界关闭 owner。
- `S1-L1.4 幂等执行与终态结算`：`DELIVERED`。已交付稳定 operation ledger、严格结果探测、
  唯一 retry owner、`manual_review` 人工判定和 history/pending/outbox 同 UoW 终态结算。
- `S1-L1.5 E3 全链收口`：`DELIVERED`。崩溃矩阵、3.0.17 升降级、重复回放、稳定计划身份、
  outcome/settlement 一致性和插件 ABI 已完成验收；旧 fail-open、重复状态与兼容层外旧入口已删除。

**问题与证据**

- `S1-L1.1` 已将任务准入改为独立事务先 commit、后写内存队列；admission、batch 或 enqueue
  异常均不会伪装成重复任务成功，失败记录可供恢复。
- 宿主 canonical Chain 只取得类型化 `TransferAdmissionRepository`；旧 Oper API 仅保留给统一兼容层，
  插件公开 `TransferTask.to_dict()` 字段未增加内部任务标识。
- `S1-L1.2` 已消除 checkpoint 前的文件副作用：目标路径、操作顺序及 resolved 识别上下文原子落库后，
  执行器才允许触发 cleanup、建目录和复制/移动；规划失败保留 `accepted` 并记录 `last_error`。
- 旧插件 `transfer` provider 的身份、顺序和原始 ABI 参数先冻结为 `provider_pending`；提交后才精确
  解析并严格执行，缺失或异常不 fallback。全部返回空后才生成宿主计划，并以第二次 CAS 提升为
  `planned` 后执行。旧 caller 只经 `ChainBase.transfer` 注入式兼容门面进入同一 durable command，
  宿主 FileManager/TransHandler 的旧执行入口已删除。
- `S1-L1.3` 已把执行所有权与 planning phase 正交：恢复任务入队前原子 claim，普通任务在任何业务
  副作用前 claim；heartbeat、checkpoint、失败留痕、release 和终态删除均受当前未过期 token
  fencing。启动和同进程恢复共享唯一 scheduler，确定性失败按固定轮询退避，关闭时 worker、replay、
  lease release 和 heartbeat 都由有界生命周期 owner 持有。损坏投影以无有效租约 CAS 留痕，同错不
  重复刷写，且不会阻塞后续健康任务。
- `S1-L1.4` 已增加 `TransferExecutionStep` 独立账本：每一步在副作用前冻结 intent 和稳定
  operation ID，以 lease + attempt 双 CAS 提交结果；重启遇到遗留 `STARTED` 时必须先严格探测，
  只有 `NOT_APPLIED` 能轮换 attempt 自动重试，`UNKNOWN/CONFLICT` 进入 `manual_review`。
- 文件 cleanup、目录创建、版本发现/删除、覆盖目标删除、目标物化和跨存储 move 的源删除均已拆为
  可重放步骤；本地复制使用完整内容比较，远端结果证据不足时不会伪造 exactly-once。
- 成功、失败及覆盖拒绝均通过 task-aware writer 在一个 UoW 内提交 history、pending、step cleanup
  与可选 outbox；revision 和确定性 occurrence key 使“文件已移动、历史未提交”在恢复后只补历史，
  不重复文件副作用。历史/API/Agent 重试只登记 durable retry intent，由唯一 scheduler 重新 claim。
- 管理员人工判定 API 只公开 `not_applied` 与带结果证据的 `applied`，并持久记录操作者、理由、结论
  和 revision；无租约人工路径不能直接伪造失败终态。
- 以上实现已满足 `docs/adr/0007-background-action-reliability.md:123-139` 对 E3 稳定身份、步骤状态、
  lease/heartbeat 和人工恢复的要求。`RETRY_WAIT`、重放、双重失败、人工放弃和结算崩溃窗口均有
  故障注入覆盖；计划指纹、步骤成员关系和所有状态写入使用精确 CAS，异常不再降级到旧执行路径。
- canonical 模块已按职责聚合为 `app/application/chain/events.py`、`app/application/transfer/execution.py`
  和 `app/runtime/resources.py`；`durable_events.py`、`transfer_execution.py`、`managed_resources.py`
  等旧物理模块已退役，仅允许精确 Compat manifest 和兼容测试引用旧导入名，宿主不保留重复导出。
- 交付提交为 `e9de149db`、`a2e249f20`；精确 head SHA 的 Unit Tests `33092427327` 与 Pylint
  `33092427348` 全绿，覆盖率低水位同步提升至 Application `78.71%`。

**目标与步骤**

- [x] 先在独立持久事务中 commit pending，再尝试放入内存队列；数据库事务不能与 `queue.Queue`
  原子提交，入队失败时必须保留 pending 供重放。
- [x] 初始登记保存稳定源身份、版本化请求和状态；目标与有序操作在纯规划完成后以 planning
  checkpoint 原子更新，任何文件副作用不得早于该提交。
- [x] 增加 claim/lease/heartbeat/attempt 与过期接管，同一任务同时只能有一个 worker owner。
- [x] 设计幂等文件操作和历史提交；只有所有必要步骤达到持久终态后才能删除记录。
- [x] 在持久状态机与现有失败历史/AI retry 之间指定唯一 retry owner，定义旧记录迁移和兼容规则。
- [x] E3 失败使用持久 `failed/manual_review`、最后稳定 checkpoint 和补偿边界，不直接套用 E2
  Outbox 的 dead-letter 语义；禁止按年龄通用清理 pending。
- [x] 当前 admission/planning 数据模型变更均配套 Alembic migration，并验证升级、降级和中断重跑。

**故障注入验收**

- [x] 登记后、内存入队前崩溃，重启可继续。
- [x] 持久登记成功但内存入队失败，重启可继续。
- [x] 文件移动后、历史提交前崩溃，在支持稳定身份/幂等操作的存储上不重复移动且可补齐历史。
- [x] worker 未知异常和 lease 超时后保留可诊断状态。
- [x] 重复回放、重复消息和人工重试都保持幂等。
- [x] 外部存储返回结果未知时进入 `manual_review`，不得伪装成 exactly-once 成功。

```bash
.venv/bin/python -m pytest \
  tests/test_transfer_pending_replay.py \
  tests/test_transfer_worker_lifecycle.py \
  tests/test_chain_durable_events.py -q
```

### ARCH-103 类型化 Chain/Agent Port 与 DTO

**问题与证据**

- Subscription 已迁入 `app/application/subscription/contract.py` 的深度冻结 DTO 与 typed
  query/write/staging Repository；Chain、API、Agent、Workflow 和 interaction 不再接收订阅 ORM。
- `app/startup/initializers/modules.py` 现向生产 Chain/Agent 注入
  `TransactionalSubscriptionRepository`，请求写入口通过 `SessionSubscriptionRepository` 复用
  当前 `AsyncSession`；原始 `SubscribeOper`/`SubscribeHistoryOper` 只留在 DB adapter 与 Legacy 层。
- standalone typed Repository 仍会为单次调用创建独立短事务；若一个业务操作连续调用多次，仍可能
  把“查询后更新”或批量更新拆成多个事务，必须由 S1-L4/S1-L5 的 Session-bound Command 收口。
- Workflow、User、DownloadHistory、TransferHistory、Site 和 Subscription 已迁入冻结 DTO 与
  adapter-owned Session 投影；剩余风险转为其他领域 locator/raw getter，以及 S1-L4/S1-L5 尚未
  完成的跨记录、跨配置原子事务，不能用本批类型化结果代替事务证明。
- S1-L2 由 `b4f873654`、`a01a35bcb` 交付；精确 head SHA 的 Unit Tests `33098869736` 与
  Pylint `33098869837` 全绿，Application 覆盖率低水位提升并固化至 `78.78%`。该证据只完成
  Workflow query 纵切面，不能替代 S1-L3 对其余 Chain/Agent raw data port 的清零。

**目标与步骤**

- [x] Subscription 按领域定义 Query/Write/Staging/History Protocol，不再使用通用
  `OperFactory = Callable[[], Any]` 或 raw Subscribe Oper factory。
- [x] 写 Port 由 `db/adapters` 创建单操作 Session/UoW；Oper 的 canonical 写方法只 stage/flush，
  读取方法仍可在调用方 Session 中查询。
- [x] Workflow 查询 Port 在 adapter Session 内映射为冻结 DTO/Projection，API、Agent、Chain、Scheduler、
  Workflow runtime 和中心服务分享均不接收 ORM。
- [x] Workflow 执行写端由 Chain 直连 `WorkflowExecutionPort` 和短 Session/UoW 事务服务；canonical
  `WorkflowOper` 只保留显式 Session query/stage，旧无 Session 五方法只存在于 SDK Legacy/Compat。
- [x] 删除 Chain registry 中零消费者 `*PortProxy`/动态转发和 `ChainRuntimeContext.data_ports`
  伪注入；Workflow 执行服务只在 Application owner 配置一次，不再重复注册到 `ChainDataPorts`。
- [x] DownloadFailure/MediaServer 两个 registry 字段改用冻结 DTO 与 typed Repository factory；
  ORM 不越过短 Session，媒体库远端枚举期间不持有事务，旧 Oper/Compat 与公开 Chain ABI 保持不变。
- [x] User Chain/Agent/认证查询改用冻结 `UserSnapshot`/`UserAuthSnapshot`；创建、更名和
  删除由请求级 UoW 原子提交，用户名唯一约束、最后一个启用超级管理员保护与
  UserConfig/PassKey 级联约束共同守住身份聚合。
- [x] DownloadHistory 与 TransferHistory 查询和写入改用深度冻结 DTO、typed Port 与短
  Session adapter；请求级删除和 durable 结算在各自单一 UoW 中处理，canonical 调用方不再
  接收 raw Oper/ORM，旧动态写入只保留在 SDK Legacy/Compat。
- [x] Site 配置、用户数据、图标与健康统计统一经 `app/application/site/contract.py` 的冻结 DTO
  和 typed Port；API 写用例复用请求 AsyncSession，Chain/Agent 使用短 Session adapter，旧
  `SiteOper` 导入与方法 ABI 只由 SDK Legacy/Compat 承接。
- [x] SubscriptionSnapshot/SubscriptionHistorySnapshot 的 JSON 列深度冻结；Identity/Patch 明确
  查询与写入形状，`TransactionalSubscriptionRepository` 与请求 Session adapter 在会话内完成
  ORM 投影，Chain/API/Agent/Workflow/interaction 全部消费 typed contract。
- [x] `app.db.subscribe_oper`、`app.db.subscribehistory_oper` 和 `app.db.oper` 包根旧符号统一经
  `app/sdk/_legacy/subscribe.py` 与精确 Compat 映射保留；canonical 包根不增加重复 `__all__` 导出。
- [x] 删除 `ChainDataPorts`/`AgentDataPorts` 及其全局 getter；Chain 使用实例级
  `ChainRuntimeContext`，Agent manager/memory/tool/scheduler 使用同一 `AgentDataContext`，字段均显式可类型检查。
- [x] User、History、Site、Subscription、Agent/Transfer locator 按纵切面逐批迁移并各自验证，
  未通过通用代理一次替换所有 Oper。
- [x] Subscription 增加 AST 门禁，禁止 canonical consumer 导入 raw Oper/ORM、以 `Any` 伪装
  Snapshot、复制 CRUD Protocol 或新增多词散落文件；全局 Agent/Transfer locator 已清零并由独立 AST 门禁守护。

**交付结果**

1. S1-L4 已收口 Subscription 新增、修改、删除、完成和批量修改的单一 UoW，canonical 自动提交写入口
   与运行时 locator 清零；Servarr 多季新增共享一个请求事务和 outbox，任一季失败整批回滚。
2. S1-L4 已逐用例验证 Session-bound Command 的提交、回滚和 post-commit 语义，不再以逐条短事务
   冒充批量原子事务。
3. S1-L5 已将站点、规则组及自定义规则改名涉及的 SystemConfig 与 Subscription 更新合并为原子命令，
   通过 CAS 拒绝过期快照，并在 commit 后一次发布配置快照。

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

- `app/chain/subscribe/reconcile.py` 只保留站点和规则组事件到原子 Application Command 的
  引用协调委托；旧单文件中的多次独立写入实现已删除。
- 类型化边界已经消除 ORM 泄漏，但 standalone Repository 的每次 update 仍是独立短事务；中途失败
  可能长期残留部分状态，直至同一事件再次触发或人工修复。处理本身具备一定幂等性，但触发事件不是
  durable owner，不能把 S1-L3.7 的通过误当作 ARCH-104 完成。

**目标与步骤**

- [x] 将引用分析与修改计划提取为纯函数/值对象。
- [x] SystemConfig 与 Subscribe 位于同一宿主数据库，首选一个 Application Command/UoW 和批量原子更新；
  复用 `SystemConfigOper.update_atomically()` 的锁行能力，而不是预设必须跨存储补偿。
- [x] 同一 UoW 内修改配置表后，在 commit 成功时一次性发布全部进程内配置快照，并明确
  `_write_lock`/`_snapshot_lock` 的顺序；读者只能观察完整旧快照或完整新快照。
- [x] 仅当未来确有无法共享事务的外部状态时，才使用持久、幂等、带 checkpoint 的 reconciliation job。
- [x] 每一步允许安全重试，返回明确的完成/冲突状态。
- [x] 在第 `k` 次写入注入异常，验证整体回滚或下次能恢复到完整状态。
- [x] 增加并发触发和并发读取测试，验证没有丢更新、死锁或配置中间组合。

```bash
.venv/bin/python -m pytest \
  tests/test_site_mutation_command.py \
  tests/test_subscription_mutation_outbox.py \
  tests/test_rule_group_media.py -q
```

### ARCH-105 明确 post-commit 与 Outbox 完成语义

**已实现事实与语义边界**

- `OutboxStager` 只在业务 Session 中 stage/flush；`OutboxDispatchStore` 的 claim、complete 和 retry
  每次使用独立短事务，业务与 dispatcher 的事务所有权已由类型分开。
- 请求线程即时投递与 dispatcher 均先按稳定 event key 原子 claim；同一 lease 期间只有
  一个 owner，过期 owner 不能用旧 attempt 覆盖新 owner 的 complete/retry。
- `PostCommitResult`/`PostCommitEffectError` 保留“业务已提交”事实，并逐项列出已完成和
  pending effect，后置效果失败不伪装成业务回滚。
- lease/attempt fencing 只保护宿主的认领与结算。外部调用成功但 complete 落库前崩溃时，
  intent 仍会重放；因此交付承诺是 at-least-once。事件载荷和宿主 correlation context
  携带稳定 event key，支持幂等的消费者应使用它；旧通知插件保持原方法签名，不能宣称外部
  provider 已获得 exactly-once 或统一幂等能力。

**目标与步骤**

- [x] Command 返回结构化结果：业务是否提交、哪些后置效果完成、哪些处于 pending。
- [x] commit 后先取得 delivery lease；未取得时跳过请求线程直投，确保与 dispatcher 排他。
- [x] 每个 post-commit effect 使用独立 intent 隔离和结算，避免效果 A 失败导致效果 B 被一起重放。
  单个外部效果仍是 at-least-once，必须有稳定幂等键和幂等 handler/消费者。
- [x] 按完成承诺、可重建性、外部不可逆性和业务重要性划分 E0-E3；用户可见性只是因素之一。
  完整分类、失败语义和恢复责任记录在 `docs/adr/0007-background-action-reliability.md`。
- [x] 拆分 `OutboxStager` 与 `OutboxDispatchStore`，避免业务 Session 调用自提交方法。
- [x] 在 commit、claim、publish、complete、通知和任务提交各断点注入异常/崩溃，验证声明等级与实际恢复一致。
- [x] 增加请求线程即时投递与 dispatcher 并发竞争测试，以及“外部调用成功、complete 前崩溃”的重放测试。

```bash
.venv/bin/python -m pytest \
  tests/test_outbox.py \
  tests/test_chain_durable_events.py \
  tests/test_subscription_completion_command.py \
  tests/test_subscription_mutation_outbox.py -q
```

### ARCH-106 让进程资源由 bootstrap/lifecycle 显式构造

**问题与证据**

- 日志 writer 已退出 `app.runtime.config` 导入路径，由 lifespan 显式创建、关闭并在真实收敛后释放身份。
- `ChainBase` 只绑定共享消息队列的轻量客户端；队列线程由 lifespan 唯一启动，不再保存首个 Chain 回调。
- 停止状态、WebPush registry 和主循环 owner 已拆为独立 runtime 合同；`global_vars` 只保留旧插件 ABI 门面。
- 当前 TaskRegistry 门禁检查 Registry 调用必须带 owner；全宿主原生 `create_task`、`Thread`、
  `Timer`、`Executor` 清单原属于已取消 ARCH-111，不作为 ARCH-106 的完成依赖。
- service locator 门禁继续覆盖五类 concrete runtime；独立 AST 门禁现已拒绝 canonical 宿主导入
  `global_vars`，只允许定义模块与 SDK 兼容出口。
- Workflow、Scheduler、Command、Agent provider 均由生命周期显式 configure/reset，冷导入不再改变注册状态。

**目标与步骤**

- [x] 日志 writer、消息队列及消息缓存由 bootstrap/lifecycle 显式构造、发布和关闭。
  现有关闭路径已能收口部分资源，当前要修的是隐式创建权，而不是重新发明 owner。
- [x] startup 唯一启动日志和消息资源；Chain 只接收无生命周期的发送 Port。
- [x] 普通模块 import 和构造 `MediaChain` 等非消息用例不得新增日志或消息线程。
- [x] 将 WebPush registry、主循环 execution gateway 和停止状态拆给各自 owner。
- [x] 保留 `global_vars` 作为兼容薄门面，但禁止 canonical 宿主新增依赖。
- [x] 将 initializer 的 provider 注册迁入显式装配阶段并提供测试 reset；冷导入回归测试覆盖
  Workflow、Scheduler、Command、Agent、技能目录和 LLM provider。
- [x] 对本叶迁移的日志、消息、Scheduler、Workflow、Agent、Plugin 资源建立 owner 与生命周期门禁；
  全宿主原生并发原语清单不在本叶偷扩范围，保留为已取消 ARCH-111 的历史定义。

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

- 当前宿主图只剩精确 containment 的 TMDB 移植包环；Chain 包内 SCC 已清零。
- `ChainBase` 已迁到 `app/chain/base.py`；物理包根仅保留包说明，旧符号由 Compat/SDK 惰性解析。
- `baseline.py --check-host` 与独立 SCC 语义测试共同拒绝 Chain 包内环、allowlist 成员增长和
  包外模块通过反向边加入 TMDB containment。

**目标与步骤**

- [x] 将 canonical `ChainBase` 实现移到 `app/chain/base.py`。
- [x] 宿主全部改用 `app.chain.base.ChainBase`；包根不做 eager re-export。
- [x] `from app.chain import ChainBase` 只通过 Compat/SDK lazy facade 兼容，并以独立测试
  证明它没有被宿主 canonical 路径使用；同时把这一精确例外写入“不得延迟导入隐藏循环”的规则。
- [x] 保证 `_messaging`、`_recognition`、`base` 任意冷导入顺序都不依赖部分初始化包。
- [x] 对排除 `app/plugins/**` 的完整宿主图断言 SCC 集合等于精确 allowlist，而不是继续追加根前缀。
- [x] TMDB 29 模块移植包允许正常单向包外依赖，但禁止 SCC 成员扩展到 allowlist 外、包外模块
  通过反向边加入该 SCC，或新增不符合分层方向的边。
- [x] 更新兼容、SDK、架构文档和 fixture 前先审查真实依赖变化。

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

- `app/application` 原有 8 个文件、13 条直接 Adapter 导入已全部迁移为 Application-owned Port，
  startup 统一注入具体实现；Passkey、Backup 与 DNS 纵切面也已退出临时 policy。
- `app/chain` 原有 8 个文件、13 条直接 Adapter 导入和 11 条普通 HTTP/Session bridge 已全部
  迁移至注入 Port 或统一技术边界，当前具体 Adapter 与普通 direct HTTP 债务均为零。
- Passkey Application 已改为消费启动注入的 `PasskeyChallengeCache`，不再判断 Redis 或导入
  具体 cache adapter；Memory/Redis 均实现严格 `AtomicCacheBackend.store/consume`。
- S0-L2.4b 已为 LLM streaming、第三方 SDK、移植库和本地控制面建立完整 egress identity 与
  zero-growth policy；11 条普通 HTTP/Session bridge 和 1 条 Application DNS I/O 债务已清零。
  当前 53 条出口事实均为精确 containment，每条指纹由独立上界冻结，不能靠同时刷新
  baseline/policy 掩盖调用面增长，已删除债务也不得恢复。

**目标与步骤**

- [x] 建立 Application/Chain 原始 Adapter 直连事实与精确临时 policy，冻结新增、替换和陈旧条目。
- [x] 建立全宿主 direct egress 事实；SDK/stream/vendor/local-control 例外精确到 bindings/uses 指纹。
- [x] 将 Passkey 原子领取提升为 runtime cache contract，由 Memory/Redis backend 分别实现。
- [x] 为 Backup 定义 Application-owned artifact store Port，由 startup 注入文件系统实现。
- [x] 将 policy 中 11 条普通 HTTP/Session bridge 债务迁移到统一网络能力并把目标收缩为空。
- [x] 为 Application SSRF 校验注入 DNS 解析 Port，清除 `socket.getaddrinfo` 直接 I/O。
- [x] Application 中命名外部产品、安全敏感能力及通用技术 Adapter 均改为注入 Port；不保留直连例外。
- [x] Chain 中命名外部产品、安全敏感能力及通用技术 Adapter 均改为注入 Port；不保留直连例外。
- [x] Adapter/direct HTTP 债务基线已收缩为零；其余 canonical transport、SDK、stream、vendor、
  diagnostic 和 control-plane 出口均按用途精确书面化，不使用宽泛豁免。

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
| `MediaChain` | recognition、plugin fallback、source projection、music alignment、cache | 保留公共识别 Facade，包根只惰性导出该类 |
| `Scheduler` | JobCatalog、ExecutionRegistry、domain reconciler、lifecycle Facade | `init` 约 336 行且直接构造多个 Chain |
| Agent API | WebAgent session/SSE/file/audio Application service | `_web_agent_stream_impl` 约 361 行 |
| System/Plugin API | nettest、logging、update、market use cases | 入口直接组合多个 Helper/Manager |

`SubscribeChain` 切片已完成：`app.chain.subscribe.facade.SubscribeChain` 只保留稳定类身份、
音乐构造接缝和事件入口委托；创建、搜索、匹配、刷新、完成、查询、交互、引用协调和通知实现
分别位于同名 package 的单词文件中。包根不导出 owner 类，旧 `app/chain/subscribe.py` 不再存在，
公开方法和双下划线兼容属性仍由同一 `SubscribeChain` 类解析。

`TransferChain` 切片已完成：`app.chain.transfer.facade.TransferChain` 只组合稳定 MRO 与三个可替换
Chain 构造点；队列/恢复、规划、执行、结算、历史/通知、请求构建及原 `_transfer.py` mixin
分别进入同名 package 的单词文件。包根只保留 `TransferChain` 与插件已使用的 `task_lock` 身份，
不重复导出 `JobManager`、durable runner 或内部 owner；旧 `transfer.py` 和 `_transfer.py` 均已删除。

`Scheduler` 切片已完成验证：旧 `app/scheduler.py` 已删除，稳定 Facade 与
`SchedulerChain` 兼容类型由 `app.scheduler` 包根惰性导出；catalog、执行、事件循环桥接、进度、
`ExecutionRegistry`、领域 reconcile、生命周期和维护任务分别位于同名 package 的单词文件。
`app/startup/initializers/scheduler.py` 统一构造 Chain 与 `SchedulerServices`，Scheduler 包内不再无参
构造业务 Chain；新插件使用 `app.sdk.scheduler` 的窄函数门面，内部 owner 不进入 SDK 或包根 ABI。
功能、生命周期、架构、兼容和文档批次均通过，官方插件基线语义未变化。

`DownloadChain` 切片已完成验证：旧 `app/chain/download.py` 已删除，包根只惰性保留稳定
`DownloadChain`；选择、提交、批量、缺集、失败冷却、历史结算、提交后处理、字幕、任务控制和
技术端口分别由同名 package 的单词文件拥有，Facade 只组合 owner 与稳定删除事件入口。历史、文件和
durable Outbox intent 在一个事务内提交，通知、后台处理和即时事件仅在 commit 成功后执行；提交失败
不会留下历史或后置副作用。主程序不保留 `source.py`、旧实现或内部 owner/port 重复导出。

`SearchChain` 切片已完成验证：旧 `app/chain/search.py` 已删除，包根只惰性保留稳定
`SearchChain`；plan、provider、pagination、result、cache、title、media、music、subtitle、site 与
recommendation 各有单一 owner。列表和流式 provider 入口共享同一批次事实，分页任务纳入统一
TaskRegistry，结果状态只由 `app.application.search.state` 持久化；Facade 保持直接 MRO、事件身份、
LunaTV 三个私有补丁点和 HRBlocker 八个公开补丁点。主程序无 `source.py`、旧实现或内部 owner
重复导出；Search 专项 223 项、架构/兼容 188 项、官方 LunaTV 6 项和锁定全量
`7222 passed, 9 skipped` 均通过。

`MediaChain` 切片已完成验证：旧 `app/chain/media.py` 已删除，包根只惰性保留
稳定 `MediaChain`；recognition、plugin、auxiliary、projection、search、catalog、path、album
与 cache 各有单一 owner。Facade 保持直接 MRO、Singleton 类身份、pickle 路径和官方插件
调用签名；目录缓存使用有界 LRU、内容签名、隔离副本与同步/异步单飞，并隔离等待者取消和
符号链接目录别名。跨 Chain 音乐来源复用只依赖公开 `MusicMetadataSourceChain`，门禁拒绝跨 owner
导入下划线私有合同。刮削兼容符号只由 Compat overlay 提供，主程序不恢复旧实现或内部 owner
重复导出。Media/架构/插件端点集中回归 `551 passed`，锁定全量 `7244 passed, 9 skipped`；
Pylint 10/10、Ruff、mypy/复杂度 ratchet、宿主与最新官方插件基线均通过，官方 V3 插件聚焦
验证 `130 passed`。

### ARCH-110 分可信级执行 Module/Event Contract（已取消）

本项随 S4 于 2026-08-28 取消。以下内容只保留为历史提案，未勾选项不是本轮未完成债务。

**问题与证据**

- Module 动态方法名已经归零，这是完成项；但 50 个结果形状仍是 `ANY`。
- `app/runtime/extensions/module/dispatcher.py:463-494` 对签名和结果偏差只告警并继续透传。
- Event 53 个 payload model 已覆盖，但当前全部为 diagnostic enforcement，输出合同也很少。

**原计划（已取消）**

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

### ARCH-111 升级质量与架构趋势门禁（已取消）

本项随 S4 于 2026-08-28 取消。现有 ratchet 继续生效，但以下扩张项不纳入本轮执行或完成计数。

**取消时记录的盲区**

- 复杂度脚本只检查 API、Application、Chain 的公共入口；私有长方法、类/文件规模和圈复杂度不受控。
- strict mypy 仅 41 个文件，高风险 lifecycle、Scheduler、Agent、Plugin Manager 多数不在 frontier。
- coverage ratchet 只聚合 Application 和 Domain。
- Task owner gate 尚未盘点原生并发原语；Event producer/consumer 已纳入统一事实源和人工 policy。
- Ruff 仅是有限规则集的历史低水位，不代表整体风格/正确性无债务。

**原计划（已取消）**

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

- [x] `PluginHelper` 继续作为兼容入口，但市场、包、依赖、备份/恢复、健康修复分别委托现有 owner。
- [x] `PluginManager` 的服务图构造移到 startup typed `PluginRuntime` factory，Facade 只保留稳定 API。
- [x] `PluginHelper` 不进入推荐 SDK，仅保留 `app.helper.plugin` 精确 Compat；`app.sdk.plugins` 只保留
  审计过的 Manager ABI，不重复导出 canonical 实现。
- [x] 用最新官方插件仓 baseline 和真实插件导入/兼容探针决定退场，不按宿主“无人引用”判断。

### ARCH-202 拆分 Agent 与 LLM provider

- [x] 冻结官方插件真实 Agent/LLM 导入与属性调用；三条 `LLMHelper` 路径保持同一身份，
  `MoviePilotTool`、`moviepilot_tool_manager._load_tools()` 保持原 owner 契约。
- [x] 删除 `app.agent`、`app.agent.llm` 包根动态/重复导出；宿主改为 owner 导入，旧符号仅由
  精确 Compat 承接，`app.helper.llm` 收窄为 `app.agent.llm.helper` 模块别名。
- [x] 将内置 provider spec 数据从 `LLMProviderManager` 分离为只读 catalog。
- [x] 模型发现、认证会话和运行时构建已拆至单词 owner。
- [x] WebAgent 会话、文件/音频和事件编排已由 `app/application/messaging/agent.py` 负责；
  API endpoint 仅保留 FastAPI 请求映射、HTTP 响应与 SSE framing。
- [x] `AgentManager` 会话、生命周期和后台任务已拆到单词 owner，公开方法由
  `app.agent.AgentManager` 稳定 Facade 精确保持，包根不再无边界转发。
- [x] `LLMProviderManager` 公开方法由稳定 Facade 精确保持；owner 已纳入 mypy 低水位。

### ARCH-203 拆分 Domain 投影与 Startup 高扇出

- [x] `MediaInfo`/`domain/context.py` 保留 canonical 类型路径；TMDB、豆瓣、Bangumi、AniList 规则已整体迁入
  `domain/projection/` 单词 owner，setter 只薄委托，输入不可变、多来源顺序、字段 golden 与宿主直接导入门禁覆盖；
  既有 `app.core.context` 精确兼容映射继续指向 canonical 类型，未新增 SDK/Compat。
- [x] L4.1 已将配置快照、`RuntimeSettingsService`、`DatabaseWorker`、兼容事务 runner、查询服务与
  插件持久化迁入既有 `startup/composition/{configuration,database}.py`；initializer 只调用组合 API。
  数据库 runtime 拒绝重入，Worker 成功后才发布事务入口；成功关闭与启动失败会对称撤销本批 provider、
  释放数据库引擎，并在模块 owner 收敛后清除 `app.state.host_runtime`。
- [x] L4.2 已将 network、security、agent、server、outbox 与 Chain 装配迁入单词型
  `startup/composition/*` owner；Agent composition 在 Worker 启动后读取容量并共享同一数据和任务对象，
  Chain 通过 provider 保持插件无参构造兼容，旧 Transfer command 只在真实调用时延迟导入。
- [x] L4.3 已将 HostRuntime、命名领域 Runtime 和旧 `ApiDataPorts` 投影统一移到单词型
  `startup/composition/runtime.py`；`RuntimeDependencies` 由 Agent、Chain、HostRuntime 复用，
  `HostRuntime.tasks` 显式绑定 lifecycle 的 TaskRegistry，Chain 不再二次构造 transfer execution。
- [x] 生命周期按显式逆序 reset manifest 撤销全部 Provider；数据库 worker 未收敛时保留
  HostRuntime、Provider 与连接供诊断重试，启动失败复用同一正式清理路径且不反向物化 owner。

### ARCH-204 收敛 sync/async 重复与测试存量

- [x] 重复 sync/async 方法已提取共享纯逻辑；双入口 ABI 只保留 I/O 外壳，共享解析、校验、映射和状态决策。
- [x] 已消除伪异步整段线程包装；Session/客户端按调用或事件循环持有，不跨并发边界复用。
- [x] 本轮触及的 parity 测试均为 pytest-native，没有新增 `unittest.TestCase` 或无行为收益的全库转换。
- [x] parity/生命周期测试通过 fixture 与 `try/finally` 恢复 singleton、cache、provider、事件循环和 `sys.modules`。

## 8. 建议实施顺序

### 阶段 0：恢复可信基线

1. 单独修复 ARCH-001，不刷新 mypy baseline。
2. 完成 ARCH-101 的文档/门禁语义决策。
3. 给 Adapter、原生并发、私有复杂度建立只读清单和 zero-growth 基线。

**退出条件**：当前全部 CI gate 通过；文档、fixture 和测试对 SCC、Adapter 例外、Oper 使用方式的描述一致。

### 阶段 1：先修可靠性和事务语义

1. ARCH-102 设计并迁移 Transfer E3 状态机。
2. 以站点/规则引用清理为 ARCH-103/104 的第一个 typed Port + UoW 纵切面。
3. ARCH-105 已完成 post-commit 结构化结果、独立 intent、Outbox 角色拆分、claim fencing 和
   commit/claim/publish/complete/通知/任务提交崩溃矩阵。

**退出条件**：故障注入覆盖清单列出的崩溃窗口；schema 有 migration；主路径不再 fail-open；调用方能区分
业务提交与后置效果 pending。

### 阶段 2：收口进程边界

1. ARCH-106 将日志、消息队列和主循环 gateway 纳入 lifecycle。
2. ARCH-107 拆出 `chain/base.py` 并消除新增 SCC。
3. ARCH-108 的 Passkey、Backup、DNS、Application/Chain Adapter 与普通 HTTP 纵切面均已完成；
   现存协议出口只保留精确书面化例外并由 zero-growth 门禁守护。

**退出条件**：冷导入不启动线程；SCC 只剩 TMDB 精确豁免；Application/Chain 到 Adapter 的债务只降不增。

### 阶段 3：按热点做纵向拆分

建议顺序：Transfer -> Subscribe -> Scheduler -> Download/Search -> Agent API。每次只迁移一个完整用例，
旧 Facade 委托并同时运行新旧入口契约测试。

**退出条件**：复杂度低水位下降，公开 ABI 未变，文件移动没有新增跨层边或兼容映射滥用。

### 阶段 4：取消记录与 S5 收口

1. ARCH-110/111 随 S4 于 2026-08-28 取消，不再作为本轮完成依赖。
2. ARCH-201 至 ARCH-204 的 Facade/Provider、Domain/Startup 与 sync/async 重复治理均已交付；
   S5-L6 已通过精确 head `c204e2e97` 的 Unit Tests `33269394727` 与 Pylint `33269394716` 完成闭环。
3. 保留现有 strict frontier、复杂度、mypy、Ruff、覆盖率和并发 ratchet，不借取消阶段接受回退。

**退出条件**：S5 全部叶子通过现有门禁与官方插件兼容验证，canonical 无旧实现和重复导出。

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

# 需要覆盖率证据时按 CI 的 8 个分片采集并合并，再检查低水位
for shard in 1/8 2/8 3/8 4/8 5/8 6/8 7/8 8/8; do
  uv run --locked --no-sync python -m coverage run --parallel-mode tests/run.py --shard "$shard"
done
uv run --locked --no-sync python -m coverage combine
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
