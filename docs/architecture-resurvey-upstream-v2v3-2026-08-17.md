# 官方 v2 → 官方 v3 结构对比：重构换了地基，没动房子

> 比对对象（按要求以官方为准）：
> - **官方 v2 最后一版**：`upstream/v2` @ `6a02e7de2`（2026-08-16）
> - **官方 v3 当前版**：`upstream/v3` @ `5128ae9e1`（2026-08-18，顶端提交即"refactor: 推进后端分层架构治理"——官方治理仍在进行时）
>
> 前提：忽略既有文档中的一切约束性结论，唯一硬性要求是"单向 import"。
> 只做梳理、诊断与设计方案，不含任何源码修改。
> 测量方法：对两棵树各做 AST 级全量 import 扫描与逐包统计；扩展机制/插件 API 面
> 的机制级判据在 fork 树（继承官方 v3 结构）排查后，逐项回官方 v3 树复核。
> *Date: 2026-08-17*

---

## 一、规模对比：v3 比 v2 大 20%，"核心"没有最小化

| 包 | 官方 v2 | 官方 v3 | 变化 | 说明 |
|---|---:|---:|---:|---|
| `modules/` | 61,774 | 67,392 | +5,618 | v2 原样保留，继续生长 |
| `agent/` | 33,040 | 40,492 | +7,452 | v2 原样保留，全树第二大包 |
| `chain/` | 27,910 | 29,637 | +1,727 | v2 原样保留 |
| `api/` | 15,235 | 16,738 | +1,503 | v2 原样保留 |
| `core/` + `helper/` + `utils/` | **34,858** | （物理删除） | — | v2 的"内核三包" |
| `foundation/domain/application/adapters/runtime/sdk` | — | **53,556** | **+53.6%** | v3 的"新六层"（含 compat） |
| `schemas/` | 4,865 | 7,689 | +2,824 | |
| `db/` | 6,858 | 8,181 | +1,323 | |
| 根散件（scheduler/cli/command/factory/main） | 4,052 | 3,605 | -447 | 仅 `log.py` 收编入 runtime，其余原样 |
| workflow / monitor / doctor / startup / testing / plugins | ~6,269 | ~8,113 | +1,844 | plugins 两边都只有 343 行基类（生态在市场仓库） |
| **合计** | **~193,700** | **~232,400** | **+38,700（+20%）** | |

三个硬事实：

1. **v2 的"内核"三包（core/helper/utils，34,858 行）被重新分层成六包 53,556 行**——
   同一批职责换了名字和目录，膨胀 53.6%，没有任何职责被移出"内核"。
2. **v2 的上层单体一行没少**：chain / modules / agent / api / 根散件合计约 15.8 万行
   原样进入 v3（占 v3 全树 68%），并各自继续生长。
3. **v3 相对 v2 净增 3.9 万行**，其中含 compat 兼容层、sdk 门面、capability 清单——
   全部是"为兼容两套结构而生"的胶水。

**结论：官方 v3 重构是"换地基"，不是"微内核化"。**
方向口号是"核心最小化 + 一切皆扩展"，实测是核心变大 20%，扩展机制一套没减。

---

## 二、依赖方向对比：9 对循环 → 4 对循环 + 2 条新反向边

对两棵树做包级 import 聚合（文件级明细已抽样核对）：

### v2 的包级双向循环（9 对）

`core↔db`（7/5）、`core↔utils`（15/4）、`core↔helper`（6/23）、
`helper↔chain`（13/1）、`helper↔agent`（16/1）、`helper↔schemas`（15/2）、
`chain↔agent`（27/3）、`api↔agent`（6/1）、`chain↔workflow`（1/9）。
另有 agent→scheduler 8、agent→command 3、api→modules 4、api→factory 1 等入口互捅。
**v2 的依赖图是一团循环，"内核三包"核心地卷入其中（core 同时被 db/utils/helper 反向依赖）。**

### v3 的包级双向循环（4 对）与新反向边

| 违规 | 具体边（官方 v3 实测） |
|---|---|
| `schemas ↔ runtime` | `schemas/response.py`、`schemas/dashboard.py` → `runtime.localization`；runtime→schemas 15 文件 |
| `application ↔ agent` | `application/messaging/skill.py` → `agent.skills.registry`；agent→application 18 文件 |
| `chain ↔ workflow` | `chain/workflow.py` → `app.workflow`；workflow→chain 9 文件 |
| `db ↔ application` | **`db/oper/user.py`、`db/oper/subscribe.py` → application**（数据层反向依赖应用层，v3 新增）；application→db 14 文件 |
| `modules → application`（反向边） | **29 个模块文件**引用 DirectoryHelper / MessageHelper / ImageHelper / AudioMetadataHelper / MediaServerIdentityHelper / messaging.agent 等——名义分层是 application→modules 单向，实际反了 |
| `sdk → api`（向上） | `sdk/_legacy/user.py` → `app.api.deps`（旧 `app.db.user_oper` 混装 DB 与认证依赖的遗产） |
| 运行态双向 | `ChainBase.__init__` 把 `self.multicast` 注入 application 层的 MessageQueueManager（静态分析不可见） |

### 评价

- **进步是真实的**：chain→agent、agent→api、agent→scheduler、agent→command、
  api→modules、api→factory 在 v3 全部清零（部分由门禁测试强制），
  helper 系 6 对循环随包消失。9 对 → 4 对。
- **但方向治理没有完成，且出现了新债**：`db↔application` 与 `modules→application`
  都是 v3 分层之后**新产生**的反向边——新六层之间的方向规则本身就没定清楚，
  门禁矩阵（`FORBIDDEN_IMPORT_PREFIXES`）对 `app.modules` 没有任何出向禁令、
  对 `app.runtime`/`db` 之间也未设防。
- **"单向 import"目前只在文件级成立**（Tarjan 强连通分量断言），
  包级依赖图在 v2、v3 都不是 DAG；v3 的门禁自陈"允许的环只存在于单一包内部"，
  等于承认包级方向不受治理。

---

## 三、"一切皆扩展"检验：五项均不及格

以下五项全部在官方 v3 树上直接复核：

1. **上帝基类只拆了文件，没拆继承**。v2 `ChainBase` 单类 89 个方法；
   v3 拆成 64 个方法 + 3 个 Mixin（Recognition/MessageProcessing/Notification），
   MRO 合并后规模依旧，横跨 8 个业务域的能力端口仍无条件下发给全部子类。
   分发模型（继承获得 run_module）与 v2 完全一致。
2. **capability.toml 是"清单"不是"扩展点"**。42 份 manifest（modules 37 + agent 4 + adapters 1），
   但 `host_module_adapter.py` 把 `metadata.subtype` 焊死在 6 个内核枚举
   （DownloaderType / StorageSchema / OtherModulesType…，定义在 `schemas/types.py`）；
   `CapabilityRuntime` 被实例化 3 次（module / managed_resource / agent），注册表互不共享。
   新增一个"族"必须改内核枚举 + 静态表——微内核的骨架、白名单的灵魂。
3. **SDK 使用率为 0**。`app/sdk`（649 行）在官方 v3 宿主生产代码中零消费者；
   compat manifest 113 条 ModuleAlias + meta path finder（`app/__init__.py` 无条件安装）
   才是插件的事实 API。**官方自己的 `_PluginBase` 就 import `ChainBase`、
   `app.core.config`、`app.core.event`、`app.helper.message`——插件基类走的全是兼容层虚拟路径**，
   每个插件哪怕一行不写就已锚定 v2 旧世界。
4. **扩展机制仍是 11 套并存**（机制判据在 fork 排查、官方逐项复核一致）：
   Host Module（半焊死）、Plugin（自由）、配置化服务（新族改 3 处静态表）、
   Managed Resource（使用方硬编码 id）、Agent Capability（硬编码常量）、
   Workflow Action（自由）、Agent 内建工具（**82 项 `BUILTIN_TOOL_CLASSES` 静态元组焊死**）、
   Agent 插件/MCP 工具与 Skills（自由）、Storage 后端（`StorageSchema` 枚举焊死）、
   Indexer Parser/Spider（**spider 14 处 elif 分支硬编码**）、
   静态清单族（API 路由 / 调度作业 / 命令 preset / Doctor 检查 / 渠道能力表）。
   真正"新增不动内核"的只有 4-5 处，全部集中在插件侧。
5. **编排面有四个**：chain（v2 内核）、application（v3 服务库，65 文件中仅个位数
   是 chain 专属，实际被 api/agent/modules/sdk 全体直接消费）、
   `scheduler.py`（1,500+ 行根散件，自带 SchedulerChain，import 6 个 Chain）、
   `command.py`（自带 CommandChain）。api 端点层另有直接 ORM / 直接踢调度器 /
   `endpoints/agent.py` 2,300+ 行内嵌业务的"第三业务层"问题（v2 带入，v3 未清）。

---

## 四、诊断："两套产物的强行兼容"的精确形态

把测量拼起来，官方 v3 的真实构成是：

```
官方 v3（23.2 万行）
├── v2 的上半身（原样保留，~15.8 万行，占 68%）
│     chain（继承式广播内核）· modules（37 内建后端）· agent（40k 内嵌子系统）
│     api（含业务的端点层）· scheduler.py / command.py（编外编排面）· workflow / monitor
├── v3 的新地基（重新分层，~5.4 万行，占 23%）
│     foundation / domain / application / adapters / runtime（含 capability 骨架）/ sdk
└── 胶水（为让上下两半共存而生）
      runtime/compat（113 条映射 + meta path finder）· sdk/_legacy · 42 份 capability.toml
      · 门禁特判测试 · 墓碑清单
```

五对新旧双轨，每一对都是"v2 机制没退役、v3 机制没接管"：

| # | 旧轨（v2 血统） | 新轨（v3 血统） | 现状 |
|---|---|---|---|
| 1 | chain 继承式编排（ChainBase 能力端口） | application 组合式服务 | 并存，application 被越级消费，另有 scheduler/command 编外编排 |
| 2 | modules 枚举族身份体系 | plugins 方法名探测 + 市场 | 分发已同权，身份/发现/生命周期/能力申明各一套 |
| 3 | compat 旧路径（事实 API） | sdk 官方门面（0 使用） | 完全倒挂；_PluginBase 自己走 compat |
| 4 | 能力=枚举（ModuleType/subtype/StorageSchema） | 能力=方法名索引 + manifest | 三重表达并存，枚举把 6 个族焊死在内核 |
| 5 | 治理=墓碑+特判测试 | 治理=manifest 校验 + 分层门禁 | 结构不能自证，规则散在测试里 |

**根因**：官方的重构路径是"迁移下层、冻结上层"——把 core/helper/utils 拆到新六层、
装上 compat 保生态，但 chain 的继承分发模型、modules 的枚举族、agent 的静态工具表、
api 的业务下沉这些**上层的结构决定**一个都没动。新地基与旧房子之间靠 compat、
sdk/_legacy、42 份清单和门禁特判黏合——这就是"两套产物强行兼容"的观感来源，
而且从提交历史看（顶端即"推进后端分层架构治理"），官方自己也承认治理未完成。

---

## 五、目标架构：六环模型与唯一硬规则

（该目标模型对官方 v3 与 fork 同样适用；fork 基于官方 v3，结构同源。）

### 5.1 设计原则

1. **内核只认识一种东西：扩展。** "内建"与"三方"只是发行方式（预装 vs 市场）不同，机制唯一。
2. **唯一硬规则：包级单向 import。** 一张显式"允许依赖矩阵"取代全部方向类特判；
   文件级无环由包级 DAG 自然蕴含。
3. **静态依赖收敛到契约。** 服务层与扩展层互相不 import，双向通信全经内核
   （能力分发 + 事件总线）。
4. **一份知识只表达一次。** 能力申明/路由/调度作业不得多处硬编码。

### 5.2 六环分层与允许依赖矩阵

```
edge        api · cli · startup(组合根) · compat(冻结)
extensions  modules/* + plugins/* + agent + workflow + monitor + doctor + servarr…
services    chain + application 合并后的用例层
domain      domain + schemas（纯语义）
platform    adapters + runtime 的机制部分（thread/rate/scheduling/…）
kernel      contracts · 唯一 ExtensionHost · dispatch · events · config/log（目标 ≤8k 行）
foundation  无状态原语（现状已达标）
```

| ↓源 \ 目标→ | foundation | kernel | platform | domain | services | extensions | edge |
|---|:-:|:-:|:-:|:-:|:-:|:-:|:-:|
| foundation | — | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ |
| kernel | ✓ | — | ✗ | ✗ | ✗ | ✗ | ✗ |
| platform | ✓ | 契约✓ | — | ✗ | ✗ | ✗ | ✗ |
| domain | ✓ | ✗ | ✗ | — | ✗ | ✗ | ✗ |
| services | ✓ | ✓ | ✓ | ✓ | — | **✗** | ✗ |
| extensions | ✓ | ✓ | ✓ | ✓ | **仅 SDK 门面** | **✗（跨扩展禁）** | ✗ |
| edge | ✓ | ✓ | ✓ | ✓ | ✓ | 生命周期✓ | — |

对照现状的三处关键裁定：

- **services ✗ extensions**：编排触达扩展只经 kernel dispatch
  （fork 已有三级分发 broadcast/multicast/unicast，语义保留、位置下沉）。
- **extensions ✗ services 实现**：今天 `modules → application` 29 文件反向边
  与市场插件直捅 Chain 类，统一收敛到受版本承诺的 SDK 门面。
- **extensions ✗ extensions**：跨扩展协作经事件或能力分发。

### 5.3 核心设计决策

**D1 runtime 解体**：extensions（module/plugin 管理器）升为 kernel 的唯一 ExtensionHost，
三个 CapabilityRuntime 实例合一；机制文件归 platform；compat/deprecation 归 edge；
`runtime.localization` 从 schemas 的依赖里摘除（Translator 回调注入），消 `schemas↔runtime`。

**D2 编排合并**：chain + application 合并为 services；分发原语脱离继承
（ChainBase 方法体系拆为 kernel dispatch 自由函数 + 按域薄客户端）；
零分发的 chain 文件（workflow/interaction/recommend/site 等）降为普通服务；
scheduler.py/command.py 收编——调度基础设施归 platform，内建 job 的定义权
归还各 services/extension，SchedulerChain/CommandChain 消失；
`db/oper/user.py`、`db/oper/subscribe.py` 的应用层依赖倒转（翻译逻辑上收 services），消 `db↔application`。

**D3 统一扩展模型**：`_ModuleBase` 与 `_PluginBase` 合并为单一 Extension 契约
（identity + manifest + lifecycle + 反射能力 + 12 hook）；
subtype/StorageSchema/SiteSchema 等枚举从"分发依据"降级为"展示元数据"
（能力索引键本就是方法名）；storages/parser/spider 二级扩展上浮为一级，
spider 的 14 处 elif + 静态字典收敛为注册表查找；Agent 82 项工具静态元组改目录发现。

**D4 agent 摘出为预装扩展**：控制面（惰性物化 + 门面）已达标；
清数据面（3 张 agent 表 + `schemas/agent.py` 迁入 agent 自持）、
工具面（工具直捅 16 种 Chain / 11 种 Oper 收敛为 HostGateway 门面）、
入向面（端点绕过门面直取的 7 类符号补进门面）。
workflow / monitor / doctor / servarr 按同一模板处理（doctor 依赖面最窄，试点首选）。

**D5 SDK/compat 定调**：承认 compat 是事实 SDK，`app/sdk` 的 re-export 改由
compat manifest 生成/校验（消除"replacement 推荐 sdk、target 绕过 sdk"的自相矛盾）；
把市场插件直捅面合法化（Chain 门面、事件、Oper 列名导出）或给出替代
（整理管线拦截事件族补齐后，monkey-patch 场景可退役）；
`_PluginBase` 改走 canonical，让零代码插件不再吃 compat 路径；compat 冻结不删。

**D6 治理换轨**：门禁主体换成 5.2 允许依赖矩阵一条断言；
现存 4 对循环 + 2 类反向边进显式豁免清单（负债表化），清一条删一条；
幽灵目录（fork 树上的 `app/service`、`app/managers`）立即删除。

---

## 六、fork 的处境与跟进策略

fork（`feat/plugin-multi-instance` 系）基于官方 v3 基线，与官方共享上述全部结构性问题，另有：

- **fork 自有债务**：插件多实例改造把 plugin_manager 拆包并引入 `runtime → db` 8 文件依赖，
  形成官方没有的 `runtime↔db` 包级循环——D1 落地时一并用 ExtensionStateStore 端口消除。
- **fork 自有资产**：三级分发（能力索引/多播修复/诊断接口）、插件自管理表（`app/db/plugin/`，
  官方仍无，是 D4 中 agent 表自持的现成机制）、内置插件样本
  （借其测得插件真实 API 面 76% compat / 17% 直捅 / 0% sdk 的量化证据）。
- **跟进成本判断**：官方治理是进行时（顶端提交即分层治理），fork 的结构改造需分两类——
  ①"官方也该修、可提 PR 的"：包级循环修复、db 反向边、modules→application 下沉、
  ChainBase 拆解等方向治理（与官方当前治理方向同向，冲突风险低）；
  ②"官方不会合的"：统一 Extension 模型、枚举退场、SDK 生成化
  （官方 capability 白名单是刻意设计，subtype 开放曾被明确拒绝）——这类改造留在 fork，
  每次 sync 上游按"行为移植"方法论消化冲突。

## 七、迁移路径（以官方 v3 结构为基线，fork 上执行）

| 阶段 | 内容 | 完成判据 |
|---|---|---|
| **P0 清障** | 删幽灵目录；修 5 条具体违规边（schemas→runtime 注入化、messaging/skill.py→agent 走注册表、sdk/_legacy/user.py 认证符号下沉、db/oper 两文件的 application 依赖倒转、fork 的 runtime→db 端口化） | 包级循环 5→0（含 fork 特有 1 对） |
| **P1 门禁换轨** | 允许依赖矩阵断言 + 豁免清单（modules→application 29 文件入表） | 新门禁绿，方向类特判删除 |
| **P2 runtime 解体** | ExtensionHost 上收、三 Runtime 合一、机制归 platform、compat 归 edge | runtime 目录消失，kernel 无 db/adapters import |
| **P3 编排合并** | 分发脱离继承、零分发文件降级、scheduler/command 收编、命名共振清除 | ChainBase 拆除，编排面 4→1 |
| **P4 反向边清偿** | modules 消费的 7 个 Helper 下沉或经 SDK 门面 | 豁免清单 modules 段清零 |
| **P5 扩展统一** | Extension 契约合并、枚举降级、二级扩展上浮、agent 工具目录化 | "新增扩展不动内核"覆盖 11/11 |
| **P6 子系统扩展化** | doctor 试点 → workflow → agent（表/schema/工具面三清） | agent 自持，宿主入向仅 SDK 门面 |
| **P7 SDK 定调** | manifest 生成 sdk、直捅面合法化、compat 冻结公告 | 插件通路 4→2 |

依赖：P1←P0；P3←P2；P5←P2；P6←P3+P5。每阶段保持测试基线绿（当前 0 failed）。
P0/P1 及 P3 的方向治理部分可同步向官方提 PR（与其进行中的治理同向）。

---

## 附录　数据留档

- 官方 v2/v3 逐包规模与包级 import 边数：见本文一、二章表格（AST 全量扫描）。
- v2 内核三包构成：`core/`（config/event/cache/context/meta/module/plugin/auth/security）、
  `helper/` 39 文件、`utils/` 29 文件。
- 官方 v3 复核坐标：`chain/__init__.py`（ChainBase 64 def + 3 Mixin）、
  `agent/tools/factory.py:104`（BUILTIN_TOOL_CLASSES 82 项）、
  `modules/indexer/__init__.py`（14 处 elif parser）、`schemas/types.py:619`（StorageSchema）、
  `runtime/extensions/host_module_adapter.py`（subtype 枚举白名单）、
  `runtime/compat/manifest.py`（113 条 ModuleAlias）、`app/__init__.py`（compat hook 安装）、
  `db/oper/user.py`、`db/oper/subscribe.py`（db→application 反向边）、
  `plugins/__init__.py`（_PluginBase 走 compat 路径 + 继承 ChainBase）。
- 插件真实 API 面量化（fork 内置插件样本，318 条 import）：
  compat 76.4% / canonical 直捅 17.3% / 符号 overlay 6.3% / sdk 0%；
  另有 826 行运行时 monkey-patch 通路。官方树内无插件样本，但 _PluginBase 与
  compat/manifest 的结构性事实两边一致。
