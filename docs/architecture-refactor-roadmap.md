# MoviePilot V3 架构重构路线图

> 战略目标：完成 `docs/architecture-optimization-checklist.md` 的全部宿主架构重构与治理任务。
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

G-ARCH 只有在以下条件全部满足后才可完成：

- [ ] `ARCH-001`、`ARCH-101` 至 `ARCH-111`、`ARCH-201` 至 `ARCH-204` 全部完成。
- [ ] 宿主依赖图没有未解释 SCC；只允许精确 containment 的 TMDB 移植包例外，成员不能增长。
- [ ] Application/Chain 到具体 Adapter、direct egress、raw concurrency 等所有例外均精确、可解释、不可增长。
- [ ] Chain/Agent 正式数据入口不再注入无 Session Oper，不再以 `Any`/ORM 作为跨层合同。
- [ ] 正式业务写入均有单一事务 owner；E2/E3 动作完成语义、幂等、恢复与人工决策路径一致。
- [ ] import、普通对象构造不启动进程资源；资源由 bootstrap/lifecycle 显式创建、发布、关闭和重试收口。
- [ ] 宿主 Module/Event 高风险合同 strict；宿主已知 `ANY` 结果形状归零，第三方插件保持诊断兼容。
- [ ] 全量 mypy 历史错误、当前 Ruff 治理规则诊断和所有新增 complexity-v2 超限归零。
- [ ] 高风险 Chain、Agent、Runtime、Startup、Adapter 纳入类型、复杂度、覆盖率和并发原语门禁。
- [ ] canonical 主程序无旧实现、重复导出和 legacy import；插件兼容检查基于同步后的官方插件仓 SHA 通过。
- [ ] 锁定全量测试、Pylint、依赖一致性/漏洞审计（若涉及依赖）和最终远端一致性全部通过。

## 3. 阶段与叶子

### S0：可信基线与事实源

退出条件：主线门禁全绿；架构规则、总览、机器快照和语义门禁一致；新增债务不能靠更新 fixture 进入。

| Leaf | 状态 | 依赖 | 完成定义 |
|---|---|---|---|
| S0-L1 可信基线恢复 | `VERIFIED` | 无 | 交付架构审计/路线图，修复 `ARCH-001` 两个 mypy 增量错误；现有专项和主线门禁通过 |
| S0-L2 规范事实统一 | `PLANNED` | S0-L1 | 修正 Oper、Adapter、HTTP、SCC、调用图规则冲突；规范目标与已执行门禁逐项对应 |
| S0-L3 完整宿主 SCC 门禁 | `PLANNED` | S0-L2 | 完整宿主 SCC 集合只允许 TMDB 精确 allowlist；Chain 环清零前门禁提供可审查低水位，最终由 S2-L2 清零 |
| S0-L4 Adapter/Egress 债务门禁 | `PLANNED` | S0-L2 | Application/Chain Adapter 导入和 direct HTTP egress 有精确分类；未解释项归零且禁止增长 |
| S0-L5 Event 扫描事实修复 | `PLANNED` | S0-L2 | consumer 只识别 EventManager 注册；动态误报归零，台账与真实生产/消费一致 |

### S1：可靠性、事务与数据合同

退出条件：Transfer 达到 E3；正式数据 Port 类型化；跨表业务操作有单一 UoW；post-commit/Outbox
竞争、失败呈现、at-least-once 和幂等语义全部闭环。

| Leaf | 状态 | 依赖 | 完成定义 |
|---|---|---|---|
| S1-L1 Transfer E3 状态机 | `PLANNED` | S0 | `ARCH-102` 全部完成：migration、持久状态、checkpoint、lease、唯一 retry owner、重放和 manual review 一次交付 |
| S1-L2 Workflow typed query | `PLANNED` | S0 | Workflow Application Port 不返回 `Any`/ORM，Session 内投影 DTO，正式调用方全部切换 |
| S1-L3 Chain/Agent typed data ports | `PLANNED` | S1-L2 | `ChainDataPorts`/`AgentDataPorts` 的 raw Oper/`Any` factory 全部清零，兼容调用进入 Legacy 层 |
| S1-L4 Subscription mutation UoW | `PLANNED` | S1-L3 | Subscription mutation 不跨 Session 传 ORM，正式写路径一个 UoW，旧自动事务入口退出 canonical 路径 |
| S1-L5 站点/规则引用原子清理 | `PLANNED` | S1-L4 | SystemConfig+Subscribe 同事务更新，commit 后快照原子发布，并发/故障注入无部分状态 |
| S1-L6 Outbox 完成语义 | `PLANNED` | S0 | claim 竞争双发清零；业务提交与 effect pending 可区分；stager/store 分离；handler 幂等与崩溃测试完整 |

### S2：进程生命周期、循环与 Adapter 边界

退出条件：导入/构造零资源副作用；Chain SCC 清零；Application/Chain 只依赖经批准的技术合同，
安全和命名外部能力全部经注入 Port。

| Leaf | 状态 | 依赖 | 完成定义 |
|---|---|---|---|
| S2-L1 日志/消息资源显式生命周期 | `PLANNED` | S0 | import 和非消息 Chain 构造零新增线程；bootstrap 显式创建，失败和正常关闭均收口 |
| S2-L2 ChainBase 与 SCC 清零 | `PLANNED` | S0-L3 | canonical `app.chain.base` 落地，包根无 eager/重复导出，宿主包根导入清零，Chain SCC 消失 |
| S2-L3 GlobalVar/provider 注册收口 | `PLANNED` | S2-L1 | `global_vars` canonical 消费清零，provider 注册进入显式装配阶段并可 reset；Legacy 入口精确保留 |
| S2-L4 Passkey 缓存边界 | `PLANNED` | S0-L4 | Application 不识别 Redis；原子 consume 由 runtime cache contract + backend 实现 |
| S2-L5 Backup artifact Port | `PLANNED` | S0-L4 | Application 不构造 `BackupFiles`，文件 I/O 由注入 Adapter 拥有 |
| S2-L6 Application Adapter 债务清零 | `PLANNED` | S2-L4,S2-L5 | Application 到具体 Adapter 的未批准边归零，批准通用机制有精确规则和门禁 |
| S2-L7 Chain Adapter/HTTP 债务清零 | `PLANNED` | S2-L6 | Chain 具体 Adapter 与普通 direct HTTP 依赖归零；SDK/stream/vendor 例外精确 containment |

### S3：大型编排器职责清零

退出条件：每个热点 Facade 只保留稳定公开入口；决策、I/O、状态和生命周期各有单一 owner；
旧实现从 canonical 文件删除，complexity-v2 对该所有权面无债务。

| Leaf | 状态 | 依赖 | 完成定义 |
|---|---|---|---|
| S3-L1 TransferChain | `PLANNED` | S1-L1 | queue/recovery、plan、execute、settle、history/notify 完整拆分，原 836 行执行方法消失 |
| S3-L2 SubscribeChain | `PLANNED` | S1-L5 | search、match、refresh、reconciliation、notification 完整拆分，原文件无跨域业务实现 |
| S3-L3 Scheduler | `PLANNED` | S2-L3 | JobCatalog、ExecutionRegistry、reconciler、lifecycle 分离，无参 Chain 构造清零 |
| S3-L4 DownloadChain | `PLANNED` | S1-L6 | selection、submission、history、post-processing 分离，提交后动作遵守新完成语义 |
| S3-L5 SearchChain | `PLANNED` | S2-L7 | plan、provider fan-out、result state、pagination 分离，状态 owner 唯一 |
| S3-L6 MediaChain | `PLANNED` | S2-L2 | recognition、source projection、music alignment、cache 分离，兼容仅经统一层 |
| S3-L7 Agent/System/Plugin API | `PLANNED` | S2,S1-L6 | WebAgent SSE/file/audio、nettest/log/update/market 用例进入 Application，endpoint 只做传输适配 |

### S4：可执行合同与质量债务清零

退出条件：高风险动态合同按信任级执行；所有 Python 源码进入一致的类型、复杂度、覆盖率和并发治理面；
历史 mypy/Ruff/复杂度债务为零。

| Leaf | 状态 | 依赖 | 完成定义 |
|---|---|---|---|
| S4-L1 Module strict contract | `PLANNED` | S2-L7 | 宿主 provider `ANY` 结果归零，宿主 admission strict，官方/第三方插件分级兼容 |
| S4-L2 Event strict contract | `PLANNED` | S0-L5,S1-L6 | 宿主事件输入/输出按风险 strict，诊断例外只属于第三方插件兼容 |
| S4-L3 Complexity v2 | `PLANNED` | S3 | 私有方法、class/file、圈复杂度进入门禁；所有超限通过职责拆分归零 |
| S4-L4 全量 mypy 清零 | `PLANNED` | S3,S4-L1,S4-L2 | `mypy-baseline.json` 归零并删除债务接受路径，全宿主 strict 类型通过 |
| S4-L5 Ruff 治理债务清零 | `PLANNED` | S3 | 当前受控 973 条诊断归零，规则集扩展经过独立审查且新增诊断为零 |
| S4-L6 Coverage/并发/质量证据 | `PLANNED` | S3,S4-L1,S4-L2 | 高风险包纳入 coverage；raw concurrency 分类清零；Module Quality 有真实 evidence test |

### S5：Plugin、Agent、Domain、Startup 与最终收口

退出条件：剩余 Facade/Manager/Domain/Startup 重复职责清零；官方插件兼容、全量测试和远端交付完成。

| Leaf | 状态 | 依赖 | 完成定义 |
|---|---|---|---|
| S5-L1 PluginHelper/PluginManager | `PLANNED` | S2,S4 | 市场/包/依赖/备份/健康服务各归 owner；Facade 只转发稳定 ABI，构造归 typed PluginRuntime |
| S5-L2 Agent/LLM provider | `PLANNED` | S3-L7,S4 | catalog、发现、认证、session、runtime 分离；Manager 只保留稳定 API |
| S5-L3 Domain projection | `PLANNED` | S3-L6,S4 | `MediaInfo` canonical 路径保留，来源投影规则拆分，重复 DTO/业务语义清零 |
| S5-L4 Startup composition | `PLANNED` | S2,S3,S5-L1,S5-L2 | `initializers/modules.py` 仅负责顺序/注册/重启决策，构造按领域进入 composition |
| S5-L5 Sync/async 重复清零 | `PLANNED` | S3,S5 | 双 ABI 外壳共享纯逻辑，重复业务实现清零，Session/客户端不跨并发边界复用 |
| S5-L6 最终兼容与交付 | `PLANNED` | 全部 | canonical 旧实现/重复导出清零；同步官方插件仓验证；锁定全量、Pylint、架构、类型、覆盖率全部通过并推送 |

## 4. 当前活动叶子

### S0-L1 可信基线恢复

**Status:** `VERIFIED`（本地验收完成，等待提交、推送和远端一致性确认）

**Outcome**

在不接受新债务的前提下恢复当前主线类型门禁，交付审计清单、Goal 路线图和与实际基线一致的架构总览。

**Ownership**

- `app/chain/media.py` 的音乐曲目匹配局部类型回归。
- `docs/architecture-optimization-checklist.md`。
- `docs/architecture-refactor-roadmap.md`。
- `docs/architecture-overview.md` 的当前事实与文档索引。

**Excluded**

- 不在本叶实现 ARCH-102 之后的业务重构。
- 不刷新 mypy、Ruff、依赖或复杂度 fixture。
- 不修改 `app/plugins/**` 或独立插件仓。

**Acceptance**

```bash
.venv/bin/python -m pytest tests/test_music_album_match.py -q
.venv/bin/mypy --config-file mypy.ini
.venv/bin/python scripts/architecture/mypy_ratchet.py
.venv/bin/python scripts/architecture/baseline.py --check-host
.venv/bin/python scripts/architecture/complexity.py
.venv/bin/python scripts/architecture/async_blocking.py
.venv/bin/python scripts/architecture/task_ownership.py
.venv/bin/python scripts/architecture/service_locator.py
.venv/bin/python scripts/architecture/ruff_ratchet.py
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
git diff --check
```

**Delivery**

- 单一提交主题：恢复可信架构基线并建立长期路线图。
- 推送 `origin/v3` 后确认提交祖先关系、远端 SHA 和 ahead/behind `0/0`。
