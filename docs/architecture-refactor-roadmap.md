# MoviePilot V3 架构重构路线图

> 战略目标：完成 S0-S3 与 S5 的宿主架构重构和最终收口；S4 按维护者决定取消，
> S3 完成后直接进入 S5，S5 完成即结束本轮战略任务。
>
> 执行分支：`v3`；每个叶子完成后独立验证、提交、推送并确认远端 SHA。
>
> 排除范围：`app/plugins/**` 是插件运行时副本，不参与宿主重构。
>
> 兼容原则：新插件只使用 `app.sdk`；旧插件导入、符号和行为只由统一 Compat/Legacy 层承接。

## 1. 治理合同

### 1.1 Goal 层级

- **G-ARCH（父目标）**：宿主架构、模块职责、持久化、生命周期、合同和质量债务全部达到本路线图终态。
- **Stage（阶段）**：一组有共同退出条件的能力面，可包含多个独立叶子。
- **Leaf（叶子）**：单一所有权面、可独立实现、验证、提交、推送和回滚的最小交付单元。

原生 Codex Goal 保存 G-ARCH 的稳定目标；本文件保存阶段、叶子、依赖、状态和停止条件。

### 1.2 状态

| 状态 | 含义 |
|---|---|
| `ACTIVE` | 当前唯一允许编辑的叶子 |
| `PLANNED` | 已定义边界，依赖满足后可激活 |
| `BLOCKED` | 有明确阻塞证据，且没有可推进的替代叶子 |
| `VERIFIED` | 本地验收完成，尚未推送或尚未确认远端 |
| `DELIVERED` | 已提交、推送，远端 SHA 与本地交付一致 |
| `COMPLETE` | 叶子验收和后续依赖均已结清 |
| `CANCELLED` | 经维护者明确取消，不进入执行、验收或完成计数 |

任何时刻只允许一个 `ACTIVE` 叶子。子代理可以并行做只读审计、测试设计和下一叶准备；只有当前
叶子的明确文件所有者可以写入源码。共享工作树中发现的并发改动必须先确认归属，不能覆盖。

### 1.3 叶子完成条件

一个叶子只有同时满足以下条件才可标记 `DELIVERED`：

1. 所有定义在该叶子内的债务计数降为零，或变为有业务理由、精确路径、机器约束的非债务例外。
2. 真实调用链已切换到新 owner；不能只增加新类、Port、DTO、门禁或测试而保留主路径走旧实现。
3. canonical 主程序删除被替代的旧实现、兼容分支和重复导出，不保留“新旧两套正式入口”。
4. 插件兼容只落在 `app/sdk`、`app/sdk/_legacy`、`app/runtime/compat` 或经批准的稳定 Facade；
   canonical 实现不得为了兼容反向依赖这些层。
5. 受影响专项、架构门禁、strict mypy、Ruff/mypy ratchet、scoped pylint 全部通过。
6. 广泛架构、启动、生命周期、持久化或兼容变更运行锁定全量测试；插件 ABI 变更独立检查官方插件仓。
7. diff、工作树、提交范围经过主线程审查；提交推送后 `HEAD == origin/v3` 且 ahead/behind 为 `0/0`。

不得以以下状态宣称完成：只建立 baseline、只新增抽象未迁移调用方、只留下 TODO、只让新测试通过、
只完成目录移动、只在兼容层外保留旧导出，或用 `--write` 接受新增债务。

## 2. 最终停止条件

G-ARCH 只有在 S3、S5 及以下条件全部满足后才可完成；S4 不属于本轮完成范围：

- [x] `ARCH-001`、`ARCH-101` 至 `ARCH-109`、`ARCH-201` 至 `ARCH-204` 全部完成；`ARCH-110/111` 随 S4 取消且不作为完成依赖。
- [x] 宿主依赖图没有未解释 SCC；只允许精确 containment 的 TMDB 移植包例外，成员不能增长。
- [x] Application/Chain 到具体 Adapter、direct egress、raw concurrency 等所有例外均精确、可解释、不可增长。
- [x] Chain/Agent 正式数据入口不再注入无 Session Oper，不再以 `Any`/ORM 作为跨层合同。
- [x] 正式业务写入均有单一事务 owner；E2/E3 动作完成语义、幂等、恢复与人工决策路径一致。
- [x] import、普通对象构造不启动进程资源；资源由 bootstrap/lifecycle 显式创建、发布、关闭和重试收口。
- [x] S3、S5 各叶子定义的类型、复杂度、覆盖率和并发债务清零，现有全局 ratchet 不发生回退。
- [x] canonical 主程序无旧实现、重复导出和 legacy import；插件兼容检查基于同步后的官方插件仓 SHA 通过。
- [x] 锁定全量测试、Pylint、依赖一致性和最终远端一致性全部通过；本轮未修改依赖。

## 3. 阶段与叶子

### S0：可信基线与事实源

退出条件：主线门禁全绿；架构规则、总览、机器快照和语义门禁一致；新增债务不能靠更新 fixture 进入。

| Leaf | 状态 | 依赖 | 完成定义 |
|---|---|---|---|
| S0-L1 可信基线恢复 | `DELIVERED` | 无 | `5df388719`：交付架构审计/路线图，修复 `ARCH-001` 两个 mypy 增量错误；远端 `0/0` |
| S0-L2.1 Host Oper/UoW 规范 | `DELIVERED` | S0-L1 | `3bf94ffed`：宿主无 Session Oper 规范债务归零，远端 `0/0` |
| S0-L2.2 完整宿主 SCC policy | `DELIVERED` | S0-L2.1 | `a884ab5c2`：完整宿主 SCC 精确 policy 生效，远端 `0/0` |
| S0-L2.3 Adapter 直连事实 | `DELIVERED` | S0-L2.1 | `e1483e85d`：锁定 28 条原始 Adapter import 事实，远端 `0/0` |
| S0-L2.4 Adapter zero-growth | `DELIVERED` | S0-L2.3 | `2553226f3`：冻结 28 条直连及 owner，收缩/新增/stale policy 门禁生效，远端 `0/0` |
| S0-L2.4b HTTP/Egress 事实与政策 | `DELIVERED` | S0-L2.4 | `47f0de745`、`43d52a35b`、`8d602149f`：冻结 66 条出口事实，消除 CI 类型/覆盖率漂移；远端全绿且 `0/0` |
| S0-L2.5 Event consumer 识别 | `DELIVERED` | S0-L2.1 | `86157be2a`：consumer 只识别可静态证明的 EventManager 注册；全量 6,459 passed / 6 skipped，CI `33029645165`/`33029645254` 全绿，远端 `0/0` |
| S0-L2.6 事实源与 CI 投影 | `DELIVERED` | S0-L2.2,S0-L2.4b,S0-L2.5 | `113355784`：99/17 条逐调用事实、17 条 consumer policy 与 CI 分层交付；Unit Tests `33031697902`、Pylint `33031697785` 全绿，远端 `0/0` |

### S1：可靠性、事务与数据合同

退出条件：Transfer 达到 E3；正式数据 Port 类型化；跨表业务操作有单一 UoW；post-commit/Outbox
竞争、失败呈现、at-least-once 和幂等语义全部闭环。

`S1-L1` 是 ARCH-102 的 Transfer E3 父项，当前状态为 **DELIVERED**。`S1-L1.1` 至
`S1-L1.5` 已全部交付，真实调用链已完成迁移，旧 fail-open、重复状态和兼容层外旧入口已退出
canonical 主程序；兼容只经统一 Compat/SDK 门面提供。

| Leaf | 状态 | 依赖 | 完成定义 |
|---|---|---|---|
| S1-L1.1 Durable admission | `VERIFIED` | S0 | Application-owned typed Port + DB adapter + migration 落地；先持久 commit 再入队，入队失败保留可恢复记录；宿主不再通过 raw/`Any` `TransferPendingOper` 处理 admission |
| S1-L1.2 Planning checkpoint | `VERIFIED` | S1-L1.1 | 版本化输入与指纹先持久化；无 legacy provider 时以 `accepted -> planned` CAS 提交完整计划，有 provider 时先提交 `provider_pending`，全部返回空后再以第二次 CAS 提交 `planned`；重放只执行冻结目标，所有文件副作用晚于对应 checkpoint commit |
| S1-L1.3 Lease 与恢复调度 | `VERIFIED` | S1-L1.2 | claim/lease/heartbeat/attempt 与过期接管规则落地；启动回放和同进程恢复共用唯一调度入口，同一任务同时只有一个 worker owner |
| S1-L1.4 幂等执行与终态结算 | `VERIFIED` | S1-L1.3 | 文件操作、历史提交和 checkpoint 可重放；唯一 retry owner 生效，未知外部结果进入 `manual_review`，仅完整终态删除 pending |
| S1-L1.5 E3 全链收口 | `DELIVERED` | S1-L1.4 | `e9de149db`、`a2e249f20`：崩溃矩阵、3.0.17 升降级、重复回放和插件 ABI 验收完整；旧 fail-open、重复状态与兼容层外旧入口删除；Unit Tests `33092427327`、Pylint `33092427348` 全绿，ARCH-102 债务归零 |
| S1-L2 Workflow typed query | `DELIVERED` | S0 | `b4f873654`、`a01a35bcb`：Workflow Application Port 不返回 `Any`/ORM，Session 内投影冻结 DTO，正式调用方全部切换；Unit Tests `33098869736`、Pylint `33098869837` 全绿，覆盖率低水位提升至 Application `78.78%` |
| S1-L3 Chain/Agent typed data ports | `VERIFIED` | S1-L2 | User、History、Site、Subscription 的冻结 DTO/typed Port 已完成；`ChainDataPorts`/`AgentDataPorts` 与 raw Oper/`Any` factory 清零，旧公开 ABI 仅由 Legacy/Compat 承接 |
| S1-L3.1 Workflow typed execution | `DELIVERED` | S1-L2 | `17d8be2af`、`b33b29876`：Chain 直连类型化事务服务且单次执行只取一个 port；canonical Oper 删除旧 writer/无 Session 写方法，旧 ABI 只在 `_legacy/workflow.py` 与 Compat overlay；Unit Tests `33103913838`、Pylint `33103913935` 全绿，Application 覆盖率低水位提升至 `78.79%` |
| S1-L3.2 Chain registry/DI | `VERIFIED` | S1-L3.1 | 显式类型化 context，删除 PortProxy、全局数据 getter 与失效的双重注入，构造器注入真实控制调用 |
| S1-L3.2.1 Registry hygiene | `DELIVERED` | S1-L3.1 | `ac7a20132`：删除零消费者 PortProxy/动态转发和 `ChainRuntimeContext.data_ports` 伪注入；Workflow 退出 Chain registry，只保留 Application owner 单一配置入口；Unit Tests `33120205586`、Pylint `33120205581` 全绿 |
| S1-L3.3 DownloadFailure/MediaServer | `DELIVERED` | S1-L3.2 | `5fb62108a`：两组 raw factory 已替换为冻结 DTO/typed Port；失败冷却在 Session 内投影，媒体库查询只返回标量且每个 upsert/cleanup 独立短事务，远端枚举不持有 Session；旧 Oper 与插件可见 Chain ABI 保持不变；Unit Tests `33127544925`、Pylint `33127544927` 全绿，Application 覆盖率低水位提升至 `78.95%` |
| S1-L3.4 User | `VERIFIED` | S1-L3.3 | User Chain/Agent/认证改用冻结 typed snapshot；创建、更名、删除与最后一个启用超级管理员保护归并到单 UoW；用户名唯一索引及 UserConfig/PassKey 级联迁移落地，UserConfig 在 commit 后持写锁重载数据库事实源并发布内存快照 |
| S1-L3.5 History | `VERIFIED` | S1-L3.4 | DownloadHistory 与 TransferHistory 均已迁入深度冻结 DTO、typed query/write/staging Port 与短 Session adapter；请求级删除和 durable 结算各自保持单一 UoW，canonical 调用方不再接收 raw Oper/ORM，旧写入 ABI 只由 SDK Legacy/Compat 提供 |
| S1-L3.6 Site | `VERIFIED` | S1-L3.5 | Site 配置、用户数据、图标和统计统一为深度冻结 DTO 与 typed query/write/staging Port；请求写入复用 AsyncSession，Chain/Agent 使用独立短事务，canonical 不再接收 raw Oper/ORM，旧插件 ABI 只经 SDK Legacy/Compat |
| S1-L3.7 Subscription | `VERIFIED` | S1-L3.6 | `application/subscription/contract.py` 统一深度冻结 Snapshot/History/Identity/Patch 与 typed query/write/staging Repository；DB adapter 在 Session 内完成 ORM 投影，Chain/API/Agent/Workflow/interaction 不再消费 raw Oper/ORM/`Any`；旧 `SubscribeOper`/`SubscribeHistoryOper` 只经同一 SDK Legacy/Compat 门面保留插件 ABI |
| S1-L3.8 Agent/Transfer locator gate | `VERIFIED` | S1-L3.7 | `chain/data.py`、`agentdata.py` 及其 getter 已删除；Chain/Agent 改用显式 typed context，AST 门禁确认 canonical 无 raw getter/Oper/Any；全量有效 `6955 passed, 9 skipped`，Application 覆盖率提升至 `79.83%`，依赖边降至 `7,062` |
| S1-L4 Subscription mutation UoW | `DELIVERED` | S1-L3.8 | `d9e4a973c`：新增、修改、删除、完成均由显式 Session-bound Command/UoW 拥有事务；Servarr 多季新增在一个请求事务内提交全部订阅和 durable intents，任一季失败整批回滚；canonical 自动提交写入口与 locator 已退出，专项联合测试 327 项通过；当前依赖事实为 7,091 条边，远端 `0/0` |
| S1-L5 站点/规则引用原子清理 | `DELIVERED` | S1-L4 | `d9e4a973c`：站点、规则组及自定义规则改名的 SystemConfig+Subscribe 共享一个 UoW，双事实 CAS 防止过期覆盖，commit 后一次发布配置快照；故障注入、通配重置、并发冲突与事件循环心跳测试通过，远端 `0/0` |
| S1-L6 Outbox 完成语义 | `VERIFIED` | S0 | 事务内 `OutboxStager` 与独立短事务 `OutboxDispatchStore` 已分离；即时投递与 dispatcher 均先 claim，complete/retry 受 attempt fencing；`PostCommitResult` 区分已提交业务、已完成与 pending effect。事件载荷和宿主 correlation context 携带稳定 event key；旧通知插件保持原签名并承认 at-least-once 重复边界 |

### S2：进程生命周期、循环与 Adapter 边界

退出条件：导入/构造零资源副作用；Chain SCC 清零；Application/Chain 只依赖经批准的技术合同，
安全和命名外部能力全部经注入 Port。

| Leaf | 状态 | 依赖 | 完成定义 |
|---|---|---|---|
| S2-L1 日志/消息资源显式生命周期 | `VERIFIED` | S0 | 配置冷导入和非消息 Chain 构造零新增日志/消息线程；lifespan 显式创建共享 owner，启动失败、关闭超时、重试及连续两轮 lifespan 均收口；Chain 轻量客户端不保留首个回调 |
| S2-L2 ChainBase 与 SCC 清零 | `VERIFIED` | S0-L2.2 | canonical `app.chain.base` 已落地，包根无 eager/重复导出，宿主包根导入清零，旧符号只经 `app.sdk.chain` 与 Compat 保持同一类身份；完整宿主 SCC 只剩精确 containment 的 TMDB 移植包 |
| S2-L3 GlobalVar/provider 注册收口 | `VERIFIED` | S2-L1 | 停止、主循环和 WebPush 各有唯一 runtime owner，canonical `global_vars` 消费清零并由 AST 门禁冻结；Workflow、Scheduler、Command、Agent、技能和 LLM provider 均在生命周期显式 configure/reset，冷导入保持未注册；Legacy SDK 门面继续共享同一事实源 |
| S2-L4 Passkey 缓存边界 | `VERIFIED` | S0-L2.4 | `PasskeyChallengeCache` 由 startup 注入，Application 不识别 Redis；严格 `AtomicCacheBackend.store/consume` 由 Memory/Redis backend 分别实现，challenge 仅能被原子领取一次 |
| S2-L5 Backup artifact Port | `VERIFIED` | S0-L2.4 | Application-owned `BackupArtifactStore`/factory 覆盖创建、发布、清理、列举、解析、删除和元数据读取；startup 注入唯一 `BackupFiles` 实现，Application 具体 Adapter 直连由 14 降至 13，完整备份调用链 62 项通过 |
| S2-L6 Application Adapter/DNS 债务清零 | `VERIFIED` | S2-L4,S2-L5 | Application 自有 DNS、图片、消息回环、RSS、站点登录、种子、系统加速 Port，startup 统一注入；13 条具体 Adapter 边及 Application DNS I/O 均归零，未装配和失败回滚语义已验证 |
| S2-L7 Chain Adapter/宿主 HTTP 债务清零 | `VERIFIED` | S2-L6 | Chain 具体 Adapter 与 11 条普通 direct HTTP/Session bridge 均归零；当前 55 条出口事实全部属于精确 SDK/stream/vendor/canonical containment，policy 无 unreviewed、stale 或指纹漂移 |

### S3：大型编排器职责清零

退出条件：每个热点 Facade 只保留稳定公开入口；决策、I/O、状态和生命周期各有单一 owner；
旧实现从 canonical 文件删除，complexity-v2 对该所有权面无债务。

| Leaf | 状态 | 依赖 | 完成定义 |
|---|---|---|---|
| S3-L1 TransferChain | `VERIFIED` | S1-L1 | 旧 `transfer.py`/`_transfer.py` 单体退役，由 `app.chain.transfer` 同名职责包承接；queue/recovery、plan、execute、settle、history/notify 各有唯一 owner，原 836 行执行方法消失，包根只保留稳定 ABI |
| S3-L2 SubscribeChain | `VERIFIED` | S1-L5 | 旧 `subscribe.py` 单体退役，由 `app.chain.subscribe` 同名职责包承接；search、match、refresh、completion、reference reconciliation、notification 各有唯一 owner，包根只保留稳定 `SubscribeChain` |
| S3-L3 Scheduler | `VERIFIED` | S2-L3 | 旧 `app/scheduler.py` 已退役并迁入 `app.scheduler` 同名包；catalog、execution/bridge/progress、`ExecutionRegistry`、reconciler、lifecycle 与 maintenance 已分离，业务 callable 由 startup 经 `SchedulerServices` 注入，包内无参 Chain 构造清零；功能/生命周期 215 项、架构/兼容/文档 189 项通过，官方插件基线语义未变化 |
| S3-L4 DownloadChain | `DELIVERED` | S1-L6 | `7941fd921`：旧单体退役；selection、submission、batch、history、post-processing 等职责进入同名包单一 owner，提交后动作遵守新完成语义；锁定全量 `7162 passed, 9 skipped`，远端 `0/0` |
| S3-L5 SearchChain | `DELIVERED` | S2-L7 | `1c145d716`：旧单体退役并由同名职责包承接；稳定 ABI、官方插件兼容、类型/复杂度门禁和锁定全量 `7222 passed, 9 skipped` 通过；远端 `0/0` |
| S3-L5.1 Runtime dependencies 命名治理 | `DELIVERED` | S3-L5 | `13fba96fd`：平铺模块退役为 `runtime/dependencies/` 同名包；单词子模块、包根无重复导出、旧快照回滚兼容、命名规则与机器门禁均已交付，锁定全量 `7224 passed, 9 skipped`，远端 `0/0` |
| S3-L6 MediaChain | `DELIVERED` | S2-L2 | `13c4a71f4`：旧单体退役并由同名职责包承接；稳定 ABI、官方插件兼容、缓存并发/别名语义、类型/复杂度门禁和锁定全量 `7244 passed, 9 skipped` 通过；远端 `0/0` |
| S3-L7 Agent/System/Plugin API | `DELIVERED` | S2,S1-L6 | `f38637dce`：WebAgent 会话/file/audio、System nettest 和 Plugin market/rating/release/folders 用例进入 Application；endpoint 只保留传输适配，锁定全量 `7256 passed, 9 skipped`、官方插件基线和远端 `0/0` 通过 |

### S4：可执行合同与质量债务清零

**阶段状态：`CANCELLED`（2026-08-28 维护者决定）**

本阶段不再执行，也不作为 S5 或父目标完成的依赖。S4 原定义保留为取消记录，
不得把 `CANCELLED` 写成 `COMPLETE` 或用 S5 顺带扩张为全宿主质量债务清零。

| Leaf | 状态 | 依赖 | 完成定义 |
|---|---|---|---|
| S4-L1 Module strict contract | `CANCELLED` | — | 原计划取消 |
| S4-L2 Event strict contract | `CANCELLED` | — | 原计划取消 |
| S4-L3 Complexity v2 | `CANCELLED` | — | 原计划取消 |
| S4-L4 全量 mypy 清零 | `CANCELLED` | — | 原计划取消 |
| S4-L5 Ruff 治理债务清零 | `CANCELLED` | — | 原计划取消 |
| S4-L6 Coverage/并发/质量证据 | `CANCELLED` | — | 原计划取消 |

### S5：Plugin、Agent、Domain、Startup 与最终收口

退出条件：剩余 Facade/Manager/Domain/Startup 重复职责清零；官方插件兼容、全量测试和远端交付完成。

| Leaf | 状态 | 依赖 | 完成定义 |
|---|---|---|---|
| S5-L1 PluginHelper/PluginManager | `DELIVERED` | S2,S3 | `fa40f29df`：市场/包/依赖/备份/健康服务各归 owner；Facade 只转发稳定 ABI，构造归 typed PluginRuntime |
| S5-L2 Agent/LLM provider | `DELIVERED` | S3-L7 | `c000c4fff`、`3a33da0b3`、`dbdf7d058`：catalog、发现、认证、session、runtime 分离；Manager/Facade 只保留稳定 API，旧包根符号仅由精确 Compat 承接 |
| S5-L3 Domain projection | `DELIVERED` | S3-L6 | `MediaInfo` canonical 路径和 setter ABI 保留；四来源纯投影归入单词 owner，输入不可变、重复业务语义清零 |
| S5-L4 Startup composition | `DELIVERED` | S2,S3,S5-L1,S5-L2 | 领域构造全部归单词型 composition owner；HostRuntime/ApiData 同源；显式逆序 reset manifest 在 worker 收敛后撤销 Provider，失败保留完整诊断边界 |
| S5-L5 Sync/async 重复清零 | `DELIVERED` | S3,S5 | 双 ABI 外壳共享纯逻辑，重复业务实现清零，Session/客户端不跨并发边界复用 |
| S5-L6 最终兼容与交付 | `DELIVERED` | S3,S5-L1,S5-L2,S5-L3,S5-L4,S5-L5 | canonical 旧实现/重复导出清零；同步官方插件仓验证；锁定全量、Pylint、架构、现有类型/覆盖率门禁全部通过并推送；完成后结束本轮战略任务 |

## 4. 叶子合同与当前活动项

### S3-L5.1 Runtime dependencies 命名治理

**Status:** `DELIVERED`

**Outcome**

退役平铺且语义相似的 `app/runtime/dependencies.py` 与
`app/runtime/native_dependencies.py`，由同名 `app.runtime.dependencies` 包承接。
`profile.py` 只拥有运行环境依赖分组与同步参数，`native.py` 只拥有已加载原生发行版快照和变更探测；
包根不放实现、不重复导出内部符号，canonical 宿主调用方直接导入职责子模块。

同步把该决策写入 `docs/rules/07-naming-conventions.md`：新增生产模块优先使用准确的单个小写单词；
同一能力需要多个文件时建立同名单词目录包，再使用单词职责文件；禁止在父目录平铺相似复合文件名、
用 `source.py` 保存旧实现，或通过包根重复导出内部 owner。架构测试锁定本包结构和退役文件，
让后续 Agent 的错误命名在 CI 中直接失败。

**Acceptance**

- `app/runtime/dependencies.py` 与 `app/runtime/native_dependencies.py` 删除且不得复活。
- `app/runtime/dependencies/` 只包含 `__init__.py`、`profile.py`、`native.py`；包根没有实现或重复导出。
- 所有宿主 canonical 导入改为明确子模块；最新官方插件仓扫描决定是否需要精确 Compat 映射。
- 命名规范、架构文档、依赖基线和结构门禁同步；`app/plugins/**` 不扫描、不修改。
- 运行依赖分组、插件原生依赖激活、插件安装/包管理专项及架构/质量门禁全部通过。
- 提交推送后确认远端 SHA、祖先关系和 ahead/behind `0/0`。

**Evidence（2026-08-29）**

- 平铺实现已删除，`app/runtime/dependencies/` 仅有 `__init__.py`、`profile.py`、`native.py`；包根只有包说明，宿主导入全部直达职责子模块。
- Docker 更新边界同时覆盖新包载荷与旧版本 `dependencies.py` 快照回滚，未把旧实现恢复到主程序。
- 官方插件仓最新远端 `aa40b961a2093474a77edd9eed88844ff07425ac` 对旧路径及全部公开符号零命中，因此未新增无消费者的 SDK/Compat 债务。
- 运行依赖、原生激活、插件安装/包管理及架构专项 `406 passed`；最终架构/CI/Docker 门禁 `182 passed`。
- 锁定全量 `7224 passed, 9 skipped`；Pylint `10.00/10` 且重复率 `0.000%`；mypy、Ruff（历史诊断降至 `667`）、复杂度和依赖基线门禁全部通过。
- 交付提交 `13fba96fd82f4e94dbda8cff5dc635966e06fa67` 已推送；本地、`origin/v3` 与远端引用一致，ahead/behind `0/0`。

### S3-L5 SearchChain

**Status:** `DELIVERED`

**Outcome**

退役 `app/chain/search.py` 单体，由同名 `app.chain.search` 包承接搜索计划、provider fan-out、
分页、结果状态、AI 推荐、字幕与音乐搜索等职责。包根只惰性保留稳定 `SearchChain`，Facade 只组合
职责 owner 与稳定入口；主程序不保留 `source.py`、旧实现或内部 owner/port 重复导出。

同步、异步和流式入口必须复用同一组纯规则与状态 owner；provider 并发、取消、分页推进和结果发布
各自只有一个执行所有者，不能通过复制现有大方法形成多套业务实现。

**Acceptance**

- 公开 `SearchChain` 类身份、方法签名、MRO、pickle 身份和官方插件调用形态不变。
- `app/plugins/**` 不扫描、不修改；官方插件仓通过独立宿主兼容基线验证。
- 搜索计划、provider fan-out、分页、取消和状态发布的同步/异步/流式行为保持一致并有回归覆盖。
- Search 专项、架构/兼容、mypy/Ruff/complexity、scoped Pylint 全部通过；广泛变更运行锁定全量。
- 本叶提交推送后确认远端 SHA、祖先关系和 ahead/behind `0/0`。

### S3-L6 MediaChain

**Status:** `DELIVERED`

**Outcome**

退役 `app/chain/media.py` 单体，由同名 `app.chain.media` 包承接 recognition、plugin event、
auxiliary、projection、search、catalog、path、album 和 cache。包根只惰性暴露稳定
`MediaChain`；Facade 保持直接 `ChainBase` MRO、Singleton/pickle 类身份和官方插件真实调用签名，
内部 owner 与刮削兼容符号不从包根重复导出。

目录缓存使用有界 LRU、文件内容签名、深拷贝隔离与同步/异步单飞；等待者取消不会反向取消共享
flight，符号链接目录别名不会因物理路径相同而错误共享专辑线索。跨 Chain 音乐来源复用只依赖公开
`MusicMetadataSourceChain`，架构门禁拒绝跨 owner 导入下划线私有合同。

**Evidence**

- Media/架构/插件端点集中回归 `551 passed`；锁定全量 `7244 passed, 9 skipped`。
- Pylint `10.00/10`、零重复；mypy 低水位 `10686 / 597`，Ruff 低水位 `661`，复杂度、宿主
  dependency/runtime contract baseline 全部通过。
- 最新官方插件仓 `aa40b961a2093474a77edd9eed88844ff07425ac` 的 MediaChain 消费者 15 个，
  official-plugin baseline 与 V3 聚焦测试 `130 passed`；`app/plugins/**` 未扫描、未修改。
- 交付提交 `13c4a71f42d189f618ac8663afb967d1517a79bb` 已推送；本地、`origin/v3` 与远端引用
  一致，ahead/behind `0/0`。

### S3-L7 Agent/System/Plugin API

**Status:** `DELIVERED`

**Outcome**

把 WebAgent SSE/file/audio、System nettest/log/update 与 Plugin market 入口中的可复用用例编排下沉到
Application owner；endpoint 只保留请求鉴权、参数/响应映射和流式传输适配，不直接组合具体 Adapter、
Helper、Manager 或跨多个领域状态。

**Acceptance**

- 先以真实入口调用图确认厚方法、I/O、状态和生命周期边界，再按独立用例迁移，不复制 endpoint 实现。
- Application 自有明确 Port/DTO，具体 Adapter 和 Runtime manager 只由 startup/composition 注入。
- 旧插件兼容只经 SDK/Compat；canonical endpoint 不保留旧业务实现或重复导出，`app/plugins/**` 排除。
- Agent/System/Plugin API 专项、架构/外联/类型/复杂度门禁及锁定全量通过后才可交付。

**Evidence**

- `app/api/endpoints/agent.py` 从 2,000 余行收敛为传输层；WebAgent 事件、会话、附件和音频用例由
  `app/application/messaging/agent.py` 承担，运行时实现进入单词命名的 `app/agent/web.py`。
- System 网络探测目录、HTTPS/重定向准入和内容校验进入 `app/application/network.py`；Plugin 市场投影、
  Release、评分和目录读写分别由 `catalog.py`、`release.py`、`rating.py`、`folders.py` 唯一拥有。
- 提交 `f38637dceaa2b7fedc8ab30a41aaad3d67392ec5` 已推送；本地锁定全量 `7256 passed, 9 skipped`，
  Pylint `10.00/10`、架构/外联/类型/复杂度门禁和官方插件基线通过，远端 ahead/behind `0/0`。

### S5-L1 PluginHelper/PluginManager

**Status:** `DELIVERED`

**Outcome**

清除 `PluginHelper`、`PluginManager` 中仍混合的市场、包、依赖、备份、健康检查和运行时构造职责；
每项能力归入现有单一 owner，稳定插件 ABI 只由受控 Facade/SDK/Compat 暴露。

**Acceptance**

- 先审计真实宿主与官方插件调用图；现有 owner 能承接的能力不得新建近义文件或重复 Facade。
- 多文件能力使用同名包和单词子模块，包根不得为宿主提供重复导出；`app/plugins/**` 排除。
- PluginHelper/PluginManager 专项、兼容基线、架构门禁及受影响测试全部通过后才可交付。

**Delivery (2026-08-29)**

- `PluginHelper` 仅保留精确旧 ABI、安装 Gateway 和 owner 委托；市场传输、索引、Release 与本地候选
  由 `PluginMarketTransport` 唯一拥有。
- `PluginPackageManager`、`PluginDependencyInstaller`、`PluginRuntimeHealth` 分别唯一拥有包事务、依赖安装
  与运行环境检查/恢复；API、Agent 与 Manager 不再保留物理删除或具体 Adapter 构造。
- `PluginManager` 只消费 startup 提前注入的 typed `PluginRuntime` factory；Compat/SDK 无重复 canonical 导出。
- 最新官方插件仓 `d5a786882` 基线与兼容探针通过；架构全集 `220 passed`，锁定覆盖率全量
  `7302 passed, 9 skipped`，Pylint `10.00/10` 且零重复，Ruff `654`、mypy `10496`，覆盖率低水位
  Application `80.92%`、Domain `79.87%`。实现提交 `fa40f29df` 已推送，远端 ahead/behind `0/0`；
  GitHub 工作流已触发，不把 queued/in_progress 状态记作远端绿色。

### S5-L3 Domain projection

**Status:** `VERIFIED`

**Outcome**

`app/domain/context.py` 继续唯一拥有 canonical `MediaInfo` 类型、构造参数、属性和四个历史 setter ABI；
TMDB、豆瓣、Bangumi、AniList 详情到统一字段的规则一次迁入 `app/domain/projection/`，接收只读快照并
返回字段映射，不修改来源或当前输入。TMDB 图片构造器归 `tmdb.py` owner，宿主调用方直接导入来源 owner。

**Compatibility and gates**

- `app.core.context` 继续由现有精确 Compat 映射到 `app.domain.context`，未新增 SDK/Compat 或包根重复导出。
- `app/schemas/context.py` 仍是传输 DTO，不复制 setter 或投影业务；`app/plugins/**` 未改动。
- 架构门禁固定投影包文件集合、空包根、setter 薄委托和宿主禁用 `MediaInfo` helper；golden、输入不可变、
  多来源顺序、字段对齐与 setter 签名测试共同守住行为和 ABI。

**Local verification (2026-08-29)**

- 基于 `origin/v3@d17d62e95` 锁定全量 `7375 passed, 9 skipped`；投影/响应/来源/Compat/架构专项全绿。
- scoped Pylint `10.00/10` 且零重复；新 `domain/projection` owner mypy 零错误，strict frontier、
  Ruff、mypy、复杂度和宿主依赖低水位均通过。
- 最新官方插件仓 `2d6946eb1` 兼容基线通过；`app/plugins/**` 零改动，包根只有说明且无重复导出。

### S5-L4 Startup composition

**Status:** `VERIFIED`

- configuration、database、network、security、agent、server、outbox、subscription 与 Chain 的具体构造
  全部归入单词型 `startup/composition/*` owner；initializer 只消费 composition API。
- `RuntimeDependencies`、HostRuntime、命名领域 Runtime 与旧 `ApiDataPorts` 投影共享同一对象身份，
  不再由 initializer、Facade 或兼容入口二次构造。
- lifecycle 通过显式逆序 reset manifest 撤销 Provider；数据库 worker 未收敛时保留 owner 与连接供诊断，
  启动失败复用同一正式清理路径。
- 2026-08-29 真实进程启动达到 `Application startup complete`，`/health/live` 与 `/health/ready`
  均返回 HTTP 200，随后达到 `Application shutdown complete`；验证未加入 revision 白名单、自动 stamp、
  跳过迁移或环境判断脚本。

### S5-L5 Sync/async 重复清零

**Status:** `VERIFIED`

- Search/TVDB、MusicBrainz、TheAudioDB、AcoustID、AniList、Bangumi、ServerReport、Memory、Yema、
  通知、豆瓣、媒体识别、目录源、Indexer、TMDB API 与 Recommend 的双 ABI 共用请求计划、响应投影、
  回退条件和业务状态机；同步/异步入口只保留真实 I/O 差异。
- 移除 TVDB 整段同步方法的伪异步线程包装；网络客户端和数据库 Session 均按调用或事件循环持有，
  不跨事件循环复用。
- 新增或改造的 parity/生命周期测试均为 pytest-native，并恢复 provider、singleton、cache、事件循环与
  `sys.modules`；本轮未新增 `unittest.TestCase`。
- `5ba7e6170` 完成本叶最后一批共享决策，mypy 低水位降至 10,008，Ruff 低水位降至 633，
  Pylint 保持 `10.00/10` 且重复代码为零。

### S5-L6 最终兼容与交付

**Status:** `DELIVERED`

- 22 个不符合规范的 canonical 文件名已归并为单词文件或同名职责包；旧实现文件和主程序包根重复导出
  已删除，宿主调用方直接导入真实 owner，兼容只由精确 SDK/Compat manifest 提供。
- 官方插件仓已同步至 `161fce34caa31deb7d82dd50a31f217d5e6784c2`；285 个 Python 文件的
  ABI/导入语义基线不变，仅更新 provenance。
- `app/plugins/**` 始终作为插件副本排除，未修改、未纳入基线写入或提交。
- 锁定串行覆盖率全量 `7659 passed, 9 skipped`；Application/Domain 低水位提升并固化为
  `81.61%` / `81.03%`。Host 架构、Event/Egress policy、复杂度、async 阻塞、TaskRegistry、
  service locator、mypy/Ruff ratchet、启动性能、Pylint `10.00/10` 与依赖一致性均通过。
- 本叶随最终收口提交推送到 `v3`；以 exact head 的 GitHub Unit Tests/Pylint 终态和远端 `0/0`
  作为 G-ARCH 结束证据。

### S1-L1.1 Durable admission

**Status:** `VERIFIED`

**Outcome**

把“接受整理任务”变成真正的 durable admission：Application 先通过类型化 Port 在独立事务中
commit pending，再尝试写入进程内队列。队列写入失败或进程在 commit 后退出时，持久记录仍能成为
后续恢复起点；持久化失败则不允许任务进入队列。该叶只结清 admission 所有权和顺序，不预先宣称
planning、lease、幂等执行或终态恢复已经完成。

**Ownership**

- `app/application/transfer/workflow.py` 拥有 admission DTO、Protocol、结果语义与 persist-before-enqueue 编排。
- `app/db/adapters/` 提供短 Session/UoW 的 Transfer pending 持久化实现；`app/db/oper/` 只接收
  adapter 拥有的 Session 并 stage/flush。
- `app/startup/` 负责构造并注入 adapter，宿主 Chain 不再取得 raw/`Any` `TransferPendingOper`。
- `app/db/models/transferpending.py` 与配套 Alembic migration 只承载该叶需要的 durable admission
  schema，并验证既有记录升级及 downgrade。
- Transfer queue、pending repository、migration、架构边界及兼容回归测试。

**Excluded**

- 不在本叶实现 planning checkpoint、lease/heartbeat、文件操作幂等、历史结算或 `manual_review`；
  这些分别由 `S1-L1.2` 至 `S1-L1.4` 完整交付。
- 不修改插件公开 Transfer ABI、旧插件导入路径或第三方插件行为；必要兼容只通过统一 Compat/Legacy
  层委托新的 canonical admission 实现。
- 不修改或扫描 `app/plugins/**` 插件副本。
- 不保留宿主 canonical raw/`Any` pending port 与新 typed Port 两套正式入口。

**Acceptance**

```bash
.venv/bin/python -m pytest \
  tests/test_transfer_queue_service.py \
  tests/test_transfer_pending_replay.py \
  tests/test_transfer_admission_migration.py \
  tests/test_db_transferpending_queries.py \
  tests/test_database_migration_startup.py \
  tests/test_architecture_dependencies.py \
  tests/test_legacy_import_compat.py -q
.venv/bin/mypy --config-file mypy.ini
.venv/bin/python scripts/architecture/baseline.py --check-host --diagnostics
.venv/bin/python scripts/architecture/ruff_ratchet.py
.venv/bin/python scripts/architecture/mypy_ratchet.py
uv run --locked --no-sync python tests/run.py
git diff --check
```

**Delivery**

- 单一提交主题：交付 Transfer durable admission、类型化持久化边界及可逆 migration。
- 提交前证明 pending commit 发生在 enqueue 之前；持久化失败不入队，enqueue 失败或 commit 后崩溃
  均保留可恢复记录；宿主 canonical 路径不再导入或取得 raw/`Any` `TransferPendingOper`。
- 运行锁定全量测试与 scoped Pylint；插件公开 Transfer ABI 和统一兼容导入测试必须保持通过。
- 推送 `origin/v3` 后确认提交祖先关系、远端 SHA 和 ahead/behind `0/0`。

**Local verification (2026-08-27)**

- 锁定全量：`6,496 passed, 7 skipped`；跳过项包含本机未配置隔离库的 PostgreSQL migration
  用例，SQLite upgrade/downgrade/re-upgrade 已真实执行。
- scoped Pylint：`10.00/10`；host dependency baseline、Ruff ratchet、mypy ratchet 与
  `git diff --check` 全部通过。
- failure injection 已覆盖 admission 失败不入队、batch/enqueue 失败保留记录、批次返回失败、
  queue -> worker -> terminal discard 稳定身份，以及 Legacy TransferTask 序列化字段不变。

### S1-L1.2 Planning checkpoint

**Status:** `VERIFIED`

**Outcome**

把 durable admission 推进为可独立恢复的 `accepted -> provider_pending -> planned` 状态：准入时冻结
版本化请求 JSON 和 SHA-256 指纹；存在旧插件 provider 时先 CAS 提交精确身份、顺序和原始 ABI 参数，
全部返回空后才由 FileManager 只读规划目标及有序叶操作，并通过第二次 CAS 提交宿主 checkpoint。
任何对应 checkpoint 提交前都不允许进入其文件副作用；`provider_pending` 重放只消费冻结调用，
`planned` 重放只消费冻结 resolved 上下文、目标和操作，不重新访问在线识别、目录选择或重命名配置。

**Ownership and compatibility**

- `app/application/transfer/workflow.py` 拥有 planning input、plan item、checkpoint 和状态错误合同；JSON 版本、
  指纹及 resolved 上下文均可跨进程 round-trip。
- `app/modules/filemanager/transhandler.py` 是唯一目标规划与文件执行实现；`FileManagerModule.transfer`
  与 `TransHandler.transfer_media` 已删除，不保留第二套重命名、覆盖或目录递归逻辑。
- `app/db/adapters/transfer/admission.py` 通过短 Session/UoW 提交 checkpoint；Oper 只负责带状态和指纹条件的
  stage，3.0.14 migration 可升级、降级并在中断后重跑。
- cleanup intent 随准入输入冻结。宿主路径由 FileManager 在 `TransferIntercept` 放行后、任何文件写入前
  执行；legacy provider 路径为保持旧 ABI 顺序，在全部冻结引用解析成功后、调用 provider 前执行。
  strict 查询确认目标不存在才视为幂等成功，查询或删除失败抛错并保留对应 checkpoint 供重试；provider
  全空后提升的宿主 checkpoint 会记录 cleanup 已完成，禁止二次查询或删除。
- 插件公开 Transfer 方法签名、事件类型和 payload 不变；旧 provider 身份和顺序随 checkpoint
  冻结，提交后由统一 dispatcher 精确解析并严格执行，缺失或异常时明确失败而不静默换路；全部返回空
  才生成宿主 plan，并以第二次 CAS 提交 `planned` checkpoint 后执行。`ChainBase.transfer` 仅委托启动
  组合根注入的 canonical durable
  command，内部 plan/execute 合同不向插件调度；新 DTO 不从包根重复导出，`app/plugins/**` 插件
  副本不参与改造。

**Excluded**

- 本叶不引入 claim、lease、heartbeat、attempt、执行步骤幂等或 `manual_review`；这些由
  `S1-L1.3` 和 `S1-L1.4` 交付。
- 本叶当时不单独承诺文件操作成功后到历史结算前的未知结果；该能力现已由 `S1-L1.4` 和
  `S1-L1.5` 的持久步骤账本、严格探测、`manual_review` 与 task-aware settlement 完整交付。

**Local verification (2026-08-27)**

- planning、持久化、迁移、兼容、replay 和 worker 聚焦回归：`224 passed, 2 skipped`；跳过项仅为
  本机未配置隔离 PostgreSQL，SQLite upgrade/downgrade/re-upgrade 已覆盖。
- 完整本地套件：`6,578 passed, 8 skipped`；架构回归：`174 passed`；scoped Pylint `10.00/10`；
  host baseline、Ruff/mypy ratchet 与 `git diff --check` 通过。
- failure injection 覆盖 commit 前零文件副作用、commit 后崩溃重放、离线 resolved context 恢复、
  配置漂移仍使用冻结 target storage、规划失败留痕、旧 provider 提交后短路、严格异常、空结果两阶段
  fallback、缺失引用零 cleanup，以及 cleanup 顺序/幂等/瞬时失败。

### S1-L1.3 Lease 与恢复调度

**Status:** `VERIFIED`

**Outcome**

为 durable transfer 增加可过期、可接管且带 fencing token 的执行租约。`owner` 只用于标识执行者，
每次有效 claim 生成的新 token 才是后续 heartbeat、checkpoint、失败留痕和终态删除的授权；所有写入
必须同时匹配当前且未过期的 token，过期 worker 即使稍后恢复也不能修改新 owner 的记录。

`accepted`、`provider_pending`、`planned` 仍是业务 planning phase，lease 与其正交：claim、heartbeat
和 takeover 不改变 phase，恢复 worker 继续按冻结 checkpoint 决定执行路径。启动回放与同进程恢复共用
唯一调度入口：恢复任务在入队前完成原子 claim 并绑定 token，新 admission 由 worker 在任何副作用前
claim。已经 claim 的恢复任务不在 worker 内二次 claim。

**State machine**

| 当前租约 | 操作 | 结果与 fencing 约束 |
|---|---|---|
| 无租约 | `claim` | 生成新 token、设置 owner/到期时间并递增 attempt；phase 不变 |
| 当前租约未过期 | 同 token `heartbeat` | 只延长当前租约；owner、token、attempt 和 phase 不变 |
| 当前租约未过期 | 任意再次 `claim`，包括同 owner | 原子拒绝，不递增 attempt、不入队、不执行副作用 |
| 当前租约已过期 | `heartbeat` 或旧 token 写入 | 原子拒绝；旧租约不可复活 |
| 当前租约已过期 | 新 worker `claim` | 生成新 token 并递增 attempt；旧 token 永久失效，phase 不变 |
| 当前 token 有效 | checkpoint、失败留痕或终态删除 | 仅精确 token CAS 成功；状态提交后不得由旧 worker 覆盖 |

任意时刻一条 pending 记录最多只有一个数据库认可的有效 fencing owner。该保证不等同于外部文件、
插件或历史副作用 exactly-once；租约在不可中断调用期间过期时，旧调用的外部结果仍可能未知。

**Scheduling and shutdown**

- 启动回放和同进程恢复只调用一个 claim-and-schedule 入口；禁止另建 list-then-enqueue 回放路径或
  第二个 retry owner。批量恢复必须先原子 claim，再把已绑定 token 的任务交给唯一 worker 队列。
- heartbeat 由 Transfer worker 生命周期拥有，不创建游离后台 owner；停止时先封口新 claim 和调度，
  再通知并有限等待 worker，heartbeat 在 worker 存活期间继续维持其租约。worker 收敛后才停止
  heartbeat；超时时保留仍存活的 worker/heartbeat owner 并返回失败。
- checkpoint、失败留痕和 pending 删除在同次持久化写入中执行 token CAS；租约丢失后 worker 停止
  继续提交状态，不以预查询替代 fencing，也不把 stale mutation 伪装成成功。
- 确定性失败只确保唯一 scheduler 存在，不即时唤醒；新建的失败恢复 owner 首次扫描先等待固定轮询
  周期。损坏持久投影按单任务事务回滚，再以无有效租约 CAS 留下去重诊断，不能饿死后续健康记录。
- 精确旧 `app.db.transferpending_oper` 导入只解析到 `app/sdk/_legacy` 门面；canonical Model、Oper、
  Application Port 不保留旧 list-then-act 或无 fencing mutation。兼容 `discard/clear` 也不得删除任何
  带 token 的有效或过期 claim。

**Excluded**

- 本叶不承诺文件、legacy provider、历史写入或其他外部副作用 exactly-once，也不以延长 lease
  掩盖不可判定结果。
- 文件步骤幂等键、逐步结果 checkpoint、崩溃后未知结果判定、唯一 retry owner 和
  `manual_review` 终态由 `S1-L1.4` 完整交付。
- 不增加 `processing` 等与 planning phase 重复的业务状态；不修改插件公开 Transfer ABI，不扫描或
  修改 `app/plugins/**`，不恢复宿主旧 pending 入口或重复导出。

**Acceptance matrix**

| 场景 | 必须证明 |
|---|---|
| 新 admission 与恢复记录竞争 | 只有 claim 成功者入队并执行；失败者零文件副作用 |
| 两进程同时 claim 同一记录 | 仅一个新 token 成功，attempt 只按真实新 claim 增长 |
| heartbeat 与 takeover 竞争 | 未过期 heartbeat 可续租；过期租约不可复活，接管 token 唯一有效 |
| 旧 worker 延迟提交 | checkpoint、失败留痕和删除均被 token CAS 拒绝，不覆盖新 owner |
| 三种 planning phase 恢复 | claim/续租/接管保持 phase，并消费各自冻结输入或 checkpoint |
| 启动与同进程恢复同时触发 | 只经过唯一 scheduler 入口，同一任务只有一个 worker owner |
| 正常与超时关闭 | 先封口后有限等待；存活 scheduler/heartbeat/worker 不丢失 owner、不报告成功 |

**Failure injection matrix**

| 注入点 | 预期持久结果 |
|---|---|
| claim commit 前崩溃 | 无新 token、attempt 不变，可由后续 worker claim |
| claim commit 后、入队前崩溃 | 租约到期后可接管，新 token fencing 旧 worker |
| worker 执行前 lease 丢失 | 不执行文件副作用，不提交失败或终态状态 |
| heartbeat commit 前后崩溃 | 仅已提交到期时间生效；过期后不可用旧 token 续租 |
| checkpoint/失败留痕提交时被接管 | stale CAS 失败，新 owner 的 phase、错误和 token 不被覆盖 |
| 终态删除提交时被接管 | stale delete 为零行，pending 保留给当前 owner |
| shutdown 时 scheduler、worker 或 lease release 阻塞 | 有限等待返回失败并保留 owner/heartbeat，禁止清句柄后重建重复 owner |

本叶验收还必须运行 lease persistence、replay/worker、startup lifecycle、migration、架构与兼容聚焦
测试，以及锁定全量测试和 scoped Pylint；插件 ABI 只经统一 Compat/SDK 验证。
